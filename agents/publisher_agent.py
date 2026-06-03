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
import tempfile
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from core.config import Config, config
from core.models import Platform, Post, PostStatus

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------
_SUPABASE_HOST_SUFFIX = ".supabase.co"
_ALLOWED_URL_SCHEMES = {"https"}


def _validate_media_url(url: str | None, label: str = "url") -> str:
    """Validate that a media URL is an HTTPS Supabase storage URL.

    Raises ``ValueError`` if the URL is empty, not HTTPS, not a local path,
    or not hosted on Supabase — preventing SSRF via tampered DB values.
    """
    if not url:
        raise ValueError(f"{label} is empty")
    # Allow local paths only in test / dry-run scenarios
    if os.path.exists(url):
        return url
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"{label} must use HTTPS (got scheme {parsed.scheme!r}): {url!r}")
    if not (parsed.netloc.endswith(_SUPABASE_HOST_SUFFIX)):
        raise ValueError(
            f"{label} must be a Supabase storage URL (got host {parsed.netloc!r}): {url!r}"
        )
    return url


logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Raised when a platform rejects a publish request."""


class PublisherAgent:
    """Publishes a ``Post`` to its target platform."""

    def __init__(self, cfg: Config = config) -> None:
        self._cfg = cfg

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
            detail = str(exc)[:200]
            logger.exception("Publish failed for post %s on %s", post.id, post.platform)
            post.mark(PostStatus.FAILED, error=f"publish failed on {post.platform}: {detail}")
            raise

        post.platform_post_id = post_id
        post.published_time = datetime.now(UTC)
        post.mark(PostStatus.PUBLISHED)
        logger.info("Published post %s to %s (id=%s)", post.id, post.platform, post_id)
        return post

    # --- Instagram (Graph API) ------------------------------------------

    def _publish_instagram(self, post: Post) -> str:
        self._cfg.require("instagram_access_token", "instagram_business_account_id")
        if post.post_type == "carousel" and post.slides:
            return self._publish_instagram_carousel(post)
        if not post.thumbnail_url:
            raise PublishError("Instagram requires an image (thumbnail_url)")
        _validate_media_url(post.thumbnail_url, label="thumbnail_url")

        account = self._cfg.instagram_business_account_id
        token = self._cfg.instagram_access_token
        base = f"https://graph.facebook.com/v19.0/{account}"

        with httpx.Client(timeout=60.0) as client:
            # 1. Create a media container.
            create = client.post(
                f"{base}/media",
                data={
                    "image_url": post.thumbnail_url,
                    "caption": post.caption_with_hashtags,
                    "access_token": token,
                },
            )
            create.raise_for_status()
            container_id = create.json().get("id")
            if not container_id:
                raise PublishError("Instagram did not return a container id")

            # 2. Publish the container.
            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": container_id, "access_token": token},
            )
            publish.raise_for_status()
            return publish.json().get("id", container_id)

    def _publish_instagram_carousel(self, post: Post) -> str:
        """Publish a carousel post via the Instagram Graph API (3-step flow)."""
        account = self._cfg.instagram_business_account_id
        token = self._cfg.instagram_access_token
        base = f"https://graph.facebook.com/v19.0/{account}"

        with httpx.Client(timeout=120.0) as client:
            # 1. Create an item container for each slide.
            item_ids = []
            for slide in post.slides:
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
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
                    "caption": post.caption_with_hashtags,
                    "children": ",".join(item_ids),
                    "access_token": token,
                },
            )
            resp.raise_for_status()
            carousel_id = resp.json().get("id")
            if not carousel_id:
                raise PublishError("Instagram did not return a carousel container id")

            # 3. Publish the carousel.
            publish = client.post(
                f"{base}/media_publish",
                data={"creation_id": carousel_id, "access_token": token},
            )
            publish.raise_for_status()
            return publish.json().get("id", carousel_id)

    # --- Facebook Pages (Graph API) -------------------------------------

    def _publish_facebook(self, post: Post) -> str:
        self._cfg.require("facebook_page_id", "instagram_access_token")
        if post.post_type == "carousel" and post.slides:
            return self._publish_facebook_carousel(post)

        page_id = self._cfg.facebook_page_id
        token = self._cfg.instagram_access_token
        base = f"https://graph.facebook.com/v19.0/{page_id}"

        with httpx.Client(timeout=60.0) as client:
            if post.thumbnail_url:
                _validate_media_url(post.thumbnail_url, label="thumbnail_url")
                resp = client.post(
                    f"{base}/photos",
                    data={
                        "url": post.thumbnail_url,
                        "caption": post.caption_with_hashtags,
                        "access_token": token,
                    },
                )
            else:
                resp = client.post(
                    f"{base}/feed",
                    data={
                        "message": post.caption_with_hashtags,
                        "access_token": token,
                    },
                )
            resp.raise_for_status()
            return resp.json().get("id", "")

    def _publish_facebook_carousel(self, post: Post) -> str:
        """Publish a multi-image carousel to a Facebook Page."""
        import json as _json

        page_id = self._cfg.facebook_page_id
        token = self._cfg.instagram_access_token
        base = f"https://graph.facebook.com/v19.0/{page_id}"

        with httpx.Client(timeout=120.0) as client:
            # 1. Upload each slide as an unpublished photo.
            photo_ids = []
            for slide in post.slides:
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
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
                    "message": post.caption_with_hashtags,
                    "attached_media": _json.dumps(photo_ids),
                    "access_token": token,
                },
            )
            resp.raise_for_status()
            return resp.json().get("id", "")

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

    def _publish_linkedin(self, post: Post) -> str:
        self._cfg.require("linkedin_access_token", "linkedin_author_urn")
        headers = {
            "Authorization": f"Bearer {self._cfg.linkedin_access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        payload = {
            "author": self._cfg.linkedin_author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": post.caption_with_hashtags},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            # LinkedIn returns the URN in the X-RestLi-Id header (or body id).
            return resp.headers.get("x-restli-id") or resp.json().get("id", "")

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
                    "tags": post.hashtags[:15],
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
