"""Publisher agent — posts content to each social platform.

One ``publish`` entry point dispatches to a per-platform method. Each
method makes the real API call for that network, returns the platform's
post id, and lets exceptions propagate so the caller can mark the post
failed and move on (one platform failing never blocks the others).

Set ``DRY_RUN=true`` to log what *would* be posted without calling any
external API — useful for local development and first deploys.

Model choice: not a Claude model. Publishing is plain HTTP calls to each
network's API — no LLM involved — so the Claude text-model tiering
(Sonnet/Haiku) doesn't apply here.

Notes on the platform APIs:
  * Instagram  — Graph API: create a media container, then publish it.
  * X/Twitter  — API v2 POST /2/tweets (OAuth 1.0a user context).
  * LinkedIn   — UGC Posts API (/v2/ugcPosts) with an author URN.
  * YouTube    — Data API v3 videos.insert (resumable upload of the file).
  * TikTok     — Content Posting API (/v2/post/publish/video/init/).

Several of these require media hosted at a public URL and platform-specific
review/upload steps; the methods below implement the canonical happy path
and are structured so you can slot in storage/upload details for your setup.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from core.config import Config, config
from core.meta_token import get_user_token
from core.models import Platform, Post, PostStatus

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------
_SUPABASE_HOST_SUFFIX = ".supabase.co"
_ALLOWED_URL_SCHEMES = {"https"}
# Redact credential-like patterns from exception messages before storing them.
_SENSITIVE_PATTERN = re.compile(r"(?i)(token|key|bearer|secret|auth|password)\s*[=:]\s*\S+")


def _validate_media_url(url: str | None, label: str = "url") -> str:
    """Validate that a media URL is an HTTPS Supabase storage URL.

    Raises ``ValueError`` if the URL is empty, not HTTPS, or not hosted on
    Supabase — preventing SSRF via tampered DB values. Local file paths are
    NOT accepted here; callers that need to handle local paths (e.g.
    _download_to_temp) must check os.path.exists before calling this.
    """
    if not url:
        raise ValueError(f"{label} is empty")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"{label} must use HTTPS (got scheme {parsed.scheme!r}): {url!r}")
    if not parsed.netloc.endswith(_SUPABASE_HOST_SUFFIX):
        raise ValueError(
            f"{label} must be a Supabase storage URL (got host {parsed.netloc!r}): {url!r}"
        )
    return url


logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Return *dt* with UTC tzinfo attached; no-op if already timezone-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class PublishError(RuntimeError):
    """Raised when a platform rejects a publish request."""


class PublisherAgent:
    """Publishes a ``Post`` to its target platform."""

    def __init__(self, cfg: Config = config) -> None:
        self._cfg = cfg

    @staticmethod
    def _ig_fb_caption(post: Post) -> str:
        """Caption + hashtags hard-capped at 5 for Instagram and Facebook compliance."""
        if not post.hashtags:
            return post.caption
        tags = " ".join(h if h.startswith("#") else f"#{h}" for h in post.hashtags[:5])
        return f"{post.caption}\n\n{tags}".strip()

    @staticmethod
    def _strip_emoji(text: str) -> str:
        """Remove emoji and non-Latin-1 characters that violate the no-emoji posting rule."""
        return re.sub(r"[^\x00-\xFF]", "", text).strip()

    def publish(self, post: Post) -> Post:
        """Publish ``post`` to its platform; set status and platform id.

        Idempotent: if the post is already published (or already carries a
        platform post id from a prior attempt), this is a no-op. Combined with
        ``Database.claim_for_publishing`` this guarantees a post is published
        at most once, even across overlapping runs or multiple workers.
        """
        if post.status == PostStatus.PUBLISHED.value or post.platform_post_id:
            logger.info(
                "Post %s already published (id=%s); skipping",
                post.id,
                post.platform_post_id,
            )
            return post

        # Enforce no-emoji rule and hard 5-hashtag cap before any platform logic
        # runs. caption_with_hashtags and _ig_fb_caption both read from these
        # fields, so this single point covers every platform path.
        post.caption = self._strip_emoji(post.caption)
        post.hashtags = [self._strip_emoji(h) for h in post.hashtags[:5]]

        post.mark(PostStatus.PUBLISHING)

        if self._cfg.dry_run:
            logger.info(
                "[DRY_RUN] Would publish to %s: %r",
                post.platform,
                post.caption_with_hashtags[:120],
            )
            post.platform_post_id = "dry-run"
            post.published_time = datetime.now(UTC)
            post.mark(PostStatus.PUBLISHED)
            return post

        # Instagram: route to Telegram for manual native publishing by default.
        # Graph API-published posts receive consistently lower algorithmic reach
        # than content uploaded natively in the app. When the API-mode flag is
        # set in Supabase Storage the Graph API path is used instead (e.g. when
        # the user can't check Telegram for a period).
        if post.platform == Platform.INSTAGRAM.value and not self._is_instagram_api_mode():
            return self._route_instagram_to_telegram(post)

        # Per-platform Telegram mode (Facebook, Twitter, LinkedIn)
        if post.platform in (
            Platform.FACEBOOK.value,
            Platform.TWITTER.value,
            Platform.LINKEDIN.value,
        ):
            tg_since = self._get_platform_telegram_since(post.platform)
            if tg_since is not None:
                post_created = post.created_at
                if post_created is None or _ensure_utc(post_created) >= tg_since:
                    return self._route_to_telegram(post)

        dispatch = {
            Platform.INSTAGRAM.value: self._publish_instagram,
            Platform.FACEBOOK.value: self._publish_facebook,
            Platform.TWITTER.value: self._publish_twitter,
            Platform.LINKEDIN.value: self._publish_linkedin,
            Platform.YOUTUBE.value: self._publish_youtube,
            Platform.TIKTOK.value: self._publish_tiktok,
        }
        handler = dispatch.get(post.platform)
        if handler is None:
            raise PublishError(f"Unsupported platform: {post.platform}")

        try:
            post_id = handler(post)
        except (httpx.HTTPError, PublishError) as exc:
            # Include the actual API response body so the error shown in the
            # dashboard contains the platform's own error code/message.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                try:
                    body = exc.response.json()
                    api_err = body.get("error") or body
                    raw = f"{exc} | api_error: {api_err}"[:600]
                except Exception:
                    raw = f"{exc} | {exc.response.text[:300]}"[:600]
            else:
                raw = str(exc)[:500]
            detail = _SENSITIVE_PATTERN.sub(r"\1=[REDACTED]", raw)[:400]
            logger.exception("Publish failed for post %s on %s", post.id, post.platform)
            post.mark(PostStatus.FAILED, error=f"publish failed on {post.platform}: {detail}")
            raise

        post.platform_post_id = post_id
        post.published_time = datetime.now(UTC)
        post.mark(PostStatus.PUBLISHED)
        logger.info("Published post %s to %s (id=%s)", post.id, post.platform, post_id)
        return post

    # --- Instagram (Graph API) ------------------------------------------

    @staticmethod
    def _await_instagram_container(
        client: httpx.Client,
        container_id: str,
        token: str,
        label: str = "container",
        max_attempts: int = 10,
        max_sleep: int = 16,
    ) -> None:
        """Poll until the Instagram media container is FINISHED (or raise on ERROR/timeout).

        Uses exponential backoff (1 s, 2 s, 4 s … capped at max_sleep) instead
        of a fixed interval. Images are usually ready in < 5 s; videos (Reels)
        can take up to 90 s, so callers should pass max_attempts=20, max_sleep=30.
        """
        for attempt in range(max_attempts):
            resp = client.get(
                f"https://graph.facebook.com/v22.0/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status_code", "")
            if status == "FINISHED":
                return
            if status == "ERROR":
                detail = data.get("status", "no detail returned")
                raise PublishError(f"Instagram {label} entered ERROR state: {detail}")
            time.sleep(min(2**attempt, max_sleep))
        raise PublishError(f"Instagram {label} did not reach FINISHED status after timeout")

    def _publish_instagram(self, post: Post) -> str:
        self._cfg.require("instagram_access_token", "instagram_business_account_id")
        if post.post_type in ("reel", "infographic_reel"):
            return self._publish_instagram_reel(post)
        if post.post_type == "carousel" and post.slides:
            if len(post.slides) < 2:
                raise PublishError(
                    f"Instagram carousel requires at least 2 slides; this post only has "
                    f"{len(post.slides)} — wait for the nightly image refresh to complete it"
                )
            return self._publish_instagram_carousel(post)
        if not post.thumbnail_url:
            raise PublishError("Instagram requires an image (thumbnail_url)")
        _validate_media_url(post.thumbnail_url, label="thumbnail_url")

        account = self._cfg.instagram_business_account_id
        token = get_user_token(self._cfg)
        base = f"https://graph.facebook.com/v22.0/{account}"

        with httpx.Client(timeout=60.0) as client:
            # 1. Create a media container.
            create = client.post(
                f"{base}/media",
                data={
                    "image_url": post.thumbnail_url,
                    "caption": self._ig_fb_caption(post),
                    "access_token": token,
                },
            )
            create.raise_for_status()
            container_id = create.json().get("id")
            if not container_id:
                raise PublishError("Instagram did not return a container id")

            # 2. Wait for Instagram to finish processing the image.
            self._await_instagram_container(client, container_id, token)

            # 3. Publish the container.
            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": container_id, "access_token": token},
            )
            publish.raise_for_status()
            return publish.json().get("id", container_id)

    def _publish_instagram_carousel(self, post: Post) -> str:
        """Publish a carousel post via the Instagram Graph API (3-step flow)."""
        account = self._cfg.instagram_business_account_id
        token = get_user_token(self._cfg)
        base = f"https://graph.facebook.com/v22.0/{account}"

        with httpx.Client(timeout=120.0) as client:
            # 1. Create an item container for each slide.
            item_ids = []
            for i, slide in enumerate(post.slides):
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
                _validate_media_url(image_url, label=f"slide {i} image_url")
                resp = client.post(
                    f"{base}/media",
                    data={
                        "image_url": image_url,
                        "is_carousel_item": "true",
                        "access_token": token,
                    },
                )
                resp.raise_for_status()
                item_id = resp.json().get("id")
                if item_id:
                    self._await_instagram_container(client, item_id, token, label=f"slide {i}")
                    item_ids.append(item_id)

            if len(item_ids) < 2:
                raise PublishError(
                    f"Instagram carousel needs at least 2 images; got {len(item_ids)}"
                )

            # 2. Create the carousel container.
            resp = client.post(
                f"{base}/media",
                data={
                    "media_type": "CAROUSEL",
                    "caption": self._ig_fb_caption(post),
                    "children": ",".join(item_ids),
                    "access_token": token,
                },
            )
            resp.raise_for_status()
            carousel_id = resp.json().get("id")
            if not carousel_id:
                raise PublishError("Instagram did not return a carousel container id")

            # 3. Wait for the carousel container to be ready.
            self._await_instagram_container(client, carousel_id, token, label="carousel")

            # 4. Publish the carousel.
            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": carousel_id, "access_token": token},
            )
            publish.raise_for_status()
            return publish.json().get("id", carousel_id)

    def _publish_instagram_reel(self, post: Post) -> str:
        """Publish a Reel to Instagram using the REELS container flow."""
        if not post.video_url:
            raise PublishError("Instagram Reel requires a video (video_url)")
        _validate_media_url(post.video_url, label="video_url")

        account = self._cfg.instagram_business_account_id
        token = get_user_token(self._cfg)
        base = f"https://graph.facebook.com/v22.0/{account}"

        with httpx.Client(timeout=120.0) as client:
            # 1. Create the Reels container (Instagram fetches the video from the URL).
            create = client.post(
                f"{base}/media",
                data={
                    "media_type": "REELS",
                    "video_url": post.video_url,
                    "caption": self._ig_fb_caption(post),
                    "share_to_feed": "true",
                    "access_token": token,
                },
            )
            create.raise_for_status()
            container_id = create.json().get("id")
            if not container_id:
                raise PublishError("Instagram did not return a Reels container id")

            # 2. Wait — video processing takes longer than images.
            self._await_instagram_container(
                client,
                container_id,
                token,
                label="reel",
                max_attempts=20,
                max_sleep=30,
            )

            # 3. Publish.
            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": container_id, "access_token": token},
            )
            publish.raise_for_status()
            return publish.json().get("id", container_id)

    @staticmethod
    def _is_instagram_api_mode() -> bool:
        """Return True if the Instagram Graph API publish flag is active in Supabase Storage."""
        try:
            from core.storage import get_storage

            return get_storage().download("config/instagram.api_mode") is not None
        except Exception:
            return False

    @staticmethod
    def _get_platform_telegram_since(platform: str) -> datetime | None:
        """Return enablement datetime for Telegram mode on *platform*, or None."""
        try:
            from core.storage import get_storage

            data = get_storage().download(f"config/telegram_mode.{platform}")
            if data is None:
                return None
            ts = data.decode().strip()
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return None

    def _route_to_telegram(self, post: Post) -> Post:
        """Send post to Telegram and mark MANUAL_READY for manual platform posting."""
        from core.telegram_notify import send_post_to_telegram

        notified = send_post_to_telegram(post, post.platform, self._cfg)
        post.meta = {**post.meta, "delivery": "telegram", "telegram_notified": notified}
        post.mark(PostStatus.MANUAL_READY)
        logger.info(
            "%s post %s queued for manual publishing (Telegram notified=%s)",
            post.platform,
            post.id,
            notified,
        )
        return post

    def _route_instagram_to_telegram(self, post: Post) -> Post:
        """Send post to Telegram and mark MANUAL_READY for native Instagram publishing."""
        from core.telegram_notify import send_instagram_post

        notified = send_instagram_post(post, self._cfg)
        post.meta = {**post.meta, "delivery": "telegram", "telegram_notified": notified}
        post.mark(PostStatus.MANUAL_READY)
        logger.info(
            "Instagram post %s queued for manual publishing (Telegram notified=%s)",
            post.id,
            notified,
        )
        return post

    # --- Facebook Pages (Graph API) -------------------------------------

    def _facebook_page_token(self, client: httpx.Client) -> str:
        """Resolve a Page access token for posting *as the page*.

        Facebook rejects Page posts made with a user token ("Unpublished
        posts must be posted to a page as the page itself"). If
        FACEBOOK_PAGE_ACCESS_TOKEN is set we use it directly; otherwise we
        derive one at runtime from the user token via GET /{page-id}. That
        derivation needs the user token to carry pages_show_list,
        pages_read_engagement and pages_manage_posts.
        """
        if self._cfg.facebook_page_access_token:
            return self._cfg.facebook_page_access_token

        resp = client.get(
            f"https://graph.facebook.com/v22.0/{self._cfg.facebook_page_id}",
            params={"fields": "access_token", "access_token": get_user_token(self._cfg)},
        )
        resp.raise_for_status()
        page_token = resp.json().get("access_token")
        if not page_token:
            raise PublishError(
                "Could not obtain a Facebook Page access token. The token in "
                "INSTAGRAM_ACCESS_TOKEN needs pages_show_list, "
                "pages_read_engagement and pages_manage_posts permissions, or "
                "set FACEBOOK_PAGE_ACCESS_TOKEN directly."
            )
        return page_token

    def _publish_facebook(self, post: Post) -> str:
        self._cfg.require("facebook_page_id", "instagram_access_token")
        if post.post_type in ("reel", "infographic_reel"):
            return self._publish_facebook_reel(post)
        if post.post_type == "carousel" and post.slides:
            if len(post.slides) < 2:
                raise PublishError(
                    f"Facebook carousel requires at least 2 slides; this post only has "
                    f"{len(post.slides)} — wait for the nightly image refresh to complete it"
                )
            return self._publish_facebook_carousel(post)

        if not post.thumbnail_url:
            raise PublishError("Facebook requires an image (thumbnail_url)")

        page_id = self._cfg.facebook_page_id
        base = f"https://graph.facebook.com/v22.0/{page_id}"

        with httpx.Client(timeout=60.0) as client:
            token = self._facebook_page_token(client)
            if post.thumbnail_url:
                _validate_media_url(post.thumbnail_url, label="thumbnail_url")
                resp = client.post(
                    f"{base}/photos",
                    data={
                        "url": post.thumbnail_url,
                        "caption": self._ig_fb_caption(post),
                        "access_token": token,
                    },
                )
            else:
                resp = client.post(
                    f"{base}/feed",
                    data={
                        "message": self._ig_fb_caption(post),
                        "access_token": token,
                    },
                )
            resp.raise_for_status()
            return resp.json().get("id", "")

    def _publish_facebook_carousel(self, post: Post) -> str:
        """Publish a multi-image carousel to a Facebook Page."""
        import json as _json

        page_id = self._cfg.facebook_page_id
        base = f"https://graph.facebook.com/v22.0/{page_id}"

        with httpx.Client(timeout=120.0) as client:
            token = self._facebook_page_token(client)
            # 1. Upload each slide as an unpublished photo.
            photo_ids = []
            for slide in post.slides:
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
                _validate_media_url(image_url, label="slide image_url")
                resp = client.post(
                    f"{base}/photos",
                    data={
                        "url": image_url,
                        "published": "false",
                        "access_token": token,
                    },
                )
                resp.raise_for_status()
                photo_id = resp.json().get("id")
                if photo_id:
                    photo_ids.append({"media_fbid": photo_id})

            if not photo_ids:
                raise PublishError("Facebook carousel: no slide images could be uploaded")

            # 2. Publish a feed post attaching all photos.
            resp = client.post(
                f"{base}/feed",
                data={
                    "message": self._ig_fb_caption(post),
                    "attached_media": _json.dumps(photo_ids),
                    "access_token": token,
                },
            )
            resp.raise_for_status()
            return resp.json().get("id", "")

    def _publish_facebook_reel(self, post: Post) -> str:
        """Publish a Reel to a Facebook Page via the video_reels endpoint.

        Flow: (1) initialize an upload session to get a video_id + upload_url,
        (2) download the MP4 locally and POST the raw bytes to the rupload URL,
        (3) call finish to publish.  Bytes-upload avoids Facebook having to
        re-fetch the file from Supabase through its own crawler (which can fail
        if the URL is not yet warm in the CDN).
        """
        if not post.video_url:
            raise PublishError("Facebook Reel requires a video (video_url)")
        _validate_media_url(post.video_url, label="video_url")

        page_id = self._cfg.facebook_page_id
        base = f"https://graph.facebook.com/v22.0/{page_id}"

        local_path: str | None = None
        try:
            local_path = self._download_to_temp(post.video_url, post.id)
            file_size = os.path.getsize(local_path)

            with httpx.Client(timeout=120.0) as client:
                token = self._facebook_page_token(client)

                # 1. Initialize upload session.
                init = client.post(
                    f"{base}/video_reels",
                    params={"upload_phase": "start", "access_token": token},
                )
                init.raise_for_status()
                init_data = init.json()
                video_id = init_data.get("video_id")
                upload_url = init_data.get("upload_url")
                if not video_id or not upload_url:
                    raise PublishError(
                        f"Facebook Reels init returned unexpected response: {init_data}"
                    )

                # 2. Upload raw MP4 bytes to the rupload endpoint.
                with open(local_path, "rb") as f:
                    upload = client.post(
                        upload_url,
                        headers={
                            "Authorization": f"OAuth {token}",
                            "offset": "0",
                            "file_size": str(file_size),
                        },
                        content=f.read(),
                    )
                upload.raise_for_status()

                # 3. Publish.
                finish = client.post(
                    f"{base}/video_reels",
                    params={
                        "upload_phase": "finish",
                        "video_id": video_id,
                        "video_state": "PUBLISHED",
                        "description": self._ig_fb_caption(post)[:2200],
                        "access_token": token,
                    },
                )
                finish.raise_for_status()
                return video_id

        finally:
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)

    # --- X / Twitter (API v2) -------------------------------------------

    def _publish_twitter(self, post: Post) -> str:
        self._cfg.require(
            "twitter_api_key",
            "twitter_api_secret",
            "twitter_access_token",
            "twitter_access_secret",
        )
        # OAuth 1.0a user-context signing via requests-oauthlib.
        from requests_oauthlib import OAuth1Session

        oauth = OAuth1Session(
            self._cfg.twitter_api_key,
            client_secret=self._cfg.twitter_api_secret,
            resource_owner_key=self._cfg.twitter_access_token,
            resource_owner_secret=self._cfg.twitter_access_secret,
        )
        # X enforces 280 chars; trust the content agent but hard-trim as a guard.
        text = post.caption_with_hashtags[:280]
        resp = oauth.post("https://api.twitter.com/2/tweets", json={"text": text})
        if resp.status_code >= 300:
            raise PublishError(f"Twitter error {resp.status_code}: {resp.text}")
        return resp.json().get("data", {}).get("id", "")

    # --- LinkedIn (UGC Posts) -------------------------------------------

    def _linkedin_member_id(self, client: httpx.Client) -> str | None:
        """Return the authenticated member's id from the OpenID userinfo endpoint.

        The ``sub`` field is the canonical id for the person who authorised the
        token. Deriving the author URN from this — rather than trusting a
        hand-entered LINKEDIN_AUTHOR_URN — means a mistyped/stale value can't
        silently break publishing. Requires the token to carry the ``openid``
        (and ``profile``) scope; returns ``None`` if it doesn't so the caller
        can fall back to the configured URN.
        """
        try:
            resp = client.get(
                "https://api.linkedin.com/v2/userinfo",
                headers={"Authorization": f"Bearer {self._cfg.linkedin_access_token}"},
            )
            resp.raise_for_status()
            return resp.json().get("sub") or None
        except httpx.HTTPError as exc:
            logger.warning("LinkedIn userinfo lookup failed (%s); using configured URN", exc)
            return None

    def _linkedin_register_and_upload(
        self, client: httpx.Client, author: str, image_url: str
    ) -> str | None:
        """Register an image asset, upload its bytes, and return the asset URN.

        Returns ``None`` if LinkedIn rejects the *owner* URN format, so the
        caller can retry with the other author prefix. Raises on any other
        failure. This is the 3-step ugcPosts image flow: registerUpload →
        PUT the binary to the returned uploadUrl → reference the asset in the
        post with shareMediaCategory IMAGE.
        """
        register = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": author,
                "serviceRelationships": [
                    {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                ],
            }
        }
        resp = client.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers={
                "Authorization": f"Bearer {self._cfg.linkedin_access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json",
            },
            json=register,
        )
        if resp.status_code in (400, 422) and "owner" in resp.text.lower():
            return None
        resp.raise_for_status()
        value = resp.json()["value"]
        asset = value["asset"]
        upload_url = value["uploadMechanism"][
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
        ]["uploadUrl"]

        # Fetch the image from storage and PUT the raw bytes to LinkedIn.
        img = client.get(image_url)
        img.raise_for_status()
        up = client.put(
            upload_url,
            headers={"Authorization": f"Bearer {self._cfg.linkedin_access_token}"},
            content=img.content,
        )
        up.raise_for_status()
        return asset

    def _publish_linkedin(self, post: Post) -> str:
        # Uses the classic UGC Posts API (/v2/ugcPosts), not the newer
        # versioned /rest/posts endpoint. /rest/posts requires the gated
        # Community Management API product, which consumer-tier apps don't
        # get — it returns a bare 403. /v2/ugcPosts works with a plain
        # "Share on LinkedIn" app and the w_member_social scope.
        self._cfg.require("linkedin_access_token")
        headers = {
            "Authorization": f"Bearer {self._cfg.linkedin_access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        has_image = bool(post.thumbnail_url)
        if has_image:
            _validate_media_url(post.thumbnail_url, label="thumbnail_url")

        with httpx.Client(timeout=60.0) as client:
            # Prefer the id derived from the token. LinkedIn validates the
            # author URN strictly: it must be the real member/person id (and
            # the prefix it accepts varies — some apps want urn:li:person:,
            # others urn:li:member:). Try the derived id under both prefixes,
            # then fall back to whatever is configured.
            member_id = self._linkedin_member_id(client)
            if member_id:
                candidates = [f"urn:li:person:{member_id}", f"urn:li:member:{member_id}"]
            elif self._cfg.linkedin_author_urn:
                candidates = [self._cfg.linkedin_author_urn]
            else:
                raise PublishError(
                    "Cannot determine the LinkedIn author URN: the token lacks the "
                    "'openid'/'profile' scope (so the member id can't be derived) and "
                    "LINKEDIN_AUTHOR_URN is not set. Regenerate the token with the "
                    "openid and profile scopes, or set LINKEDIN_AUTHOR_URN."
                )

            last_resp: httpx.Response | None = None
            for author in candidates:
                share_content: dict = {
                    "shareCommentary": {"text": post.caption_with_hashtags[:3000]},
                    "shareMediaCategory": "NONE",
                }
                if has_image:
                    asset = self._linkedin_register_and_upload(client, author, post.thumbnail_url)
                    if asset is None:
                        # Owner URN format rejected — try the next prefix.
                        logger.warning(
                            "LinkedIn rejected image owner %r; trying next format", author
                        )
                        continue
                    media_item: dict = {"status": "READY", "media": asset}
                    title = (post.title or post.topic or "").strip()
                    if title:
                        media_item["title"] = {"text": title[:200]}
                    share_content["shareMediaCategory"] = "IMAGE"
                    share_content["media"] = [media_item]

                payload: dict = {
                    "author": author,
                    "lifecycleState": "PUBLISHED",
                    "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
                    "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
                }
                resp = client.post(
                    "https://api.linkedin.com/v2/ugcPosts",
                    headers=headers,
                    json=payload,
                )
                if resp.is_success:
                    # Log the author URN that worked so the real member id can
                    # be copied into LINKEDIN_AUTHOR_URN for consistency.
                    logger.info("LinkedIn published as author %s", author)
                    return resp.headers.get("x-restli-id") or resp.json().get("id", "")
                last_resp = resp
                # Only worth trying the next prefix when LinkedIn specifically
                # rejected the author format; any other error won't be fixed by
                # swapping the prefix.
                if not (resp.status_code == 422 and "author" in resp.text.lower()):
                    break
                logger.warning(
                    "LinkedIn rejected author %r (%s); trying next format",
                    author,
                    resp.status_code,
                )

            assert last_resp is not None  # candidates is never empty
            logger.error("LinkedIn %s — body: %s", last_resp.status_code, last_resp.text[:500])
            last_resp.raise_for_status()
            return ""

    # --- YouTube (Data API v3) ------------------------------------------

    def _publish_youtube(self, post: Post) -> str:
        self._cfg.require("youtube_client_id", "youtube_client_secret", "youtube_refresh_token")
        if not post.video_url:
            raise PublishError("YouTube requires a video (video_url)")

        # Build an authorised client from the stored refresh token, then
        # resumable-upload the rendered video file.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = Credentials(
            token=None,
            refresh_token=self._cfg.youtube_refresh_token,
            client_id=self._cfg.youtube_client_id,
            client_secret=self._cfg.youtube_client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
        )
        youtube = build("youtube", "v3", credentials=creds)

        local_path = self._download_to_temp(post.video_url, post.id)
        try:
            body = {
                "snippet": {
                    "title": (post.title or post.topic or post.pillar)[:100],
                    "description": post.caption_with_hashtags[:5000],
                    "tags": post.hashtags[:5],
                    "categoryId": "28",  # Science & Technology
                },
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            }
            media = MediaFileUpload(local_path, chunksize=-1, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = request.execute()
            return response.get("id", "")
        finally:
            # Always clean up the temp file after upload
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)

    # --- TikTok (Content Posting API) -----------------------------------

    def _publish_tiktok(self, post: Post) -> str:
        self._cfg.require("tiktok_access_token")
        if not post.video_url:
            raise PublishError("TikTok requires a video (video_url)")
        _validate_media_url(post.video_url, label="video_url")

        headers = {
            "Authorization": f"Bearer {self._cfg.tiktok_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "post_info": {
                "title": post.caption_with_hashtags[:2200],
                "privacy_level": "PUBLIC_TO_EVERYONE",
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": post.video_url,
            },
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                "https://open.tiktokapis.com/v2/post/publish/video/init/",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            publish_id = data.get("publish_id")
            if not publish_id:
                raise PublishError(f"TikTok did not return a publish_id: {resp.text}")
            return publish_id

    # --- Helpers ---------------------------------------------------------

    @staticmethod
    def _download_to_temp(url: str, post_id: str) -> str:
        """Download a validated Supabase media URL to a named temp file.

        Security: validates the URL is HTTPS + Supabase before fetching.
        Resource: uses a NamedTemporaryFile so callers can delete after use;
        the path is returned and the caller is responsible for cleanup.
        """
        # Already a local path (test / dry-run)? Use it directly.
        if os.path.exists(url):
            return url

        _validate_media_url(url, label="video_url")

        suffix = ".mp4"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix=f"btl_{post_id[:8]}_")
        try:
            with httpx.stream("GET", url, timeout=120.0) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    tmp.write(chunk)
        finally:
            tmp.close()
        return tmp.name
