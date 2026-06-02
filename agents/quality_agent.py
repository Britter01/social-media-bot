"""Quality-control agent — reviews posts before they are scheduled.

Two checks run on every post before it gets a scheduled time:

  * Text: Claude (Haiku) scans the caption for double writing or repeated
    phrases and rewrites it in-place when issues are found.  Hashtags are
    preserved unchanged.

  * Image: Claude vision (Haiku) checks the thumbnail for a legible brand
    overlay / logo.  Image failures raise QualityError so the pipeline can
    mark the post failed and surface it for human review in the dashboard.
    If the image URL cannot be fetched the visual check is skipped rather
    than blocking the post.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx
from anthropic import Anthropic

from core.config import Config, ConfigError, config
from core.models import Post

logger = logging.getLogger(__name__)


class QualityError(RuntimeError):
    """Raised when a post fails visual quality control."""


class QualityAgent:
    """Reviews generated posts for text and image quality before scheduling."""

    _TEXT_SYSTEM = (
        "You are a social-media content editor. "
        "Check the caption for duplicated or repeated sentences and phrases. "
        "If the caption is fine, reply with exactly: OK\n"
        "If there are problems, reply with the corrected caption only — "
        "no commentary, no explanation. Do not include hashtags."
    )

    _IMAGE_SYSTEM = (
        "You are a brand quality-control reviewer. "
        "Examine the social media image and check whether a brand logo or "
        "text overlay is visible and legible. "
        'Reply with JSON only: {"ok": true} if the overlay looks fine, or '
        '{"ok": false, "issue": "<one sentence describing the problem>"} '
        "if the logo is cut off, unreadable, missing, or badly positioned."
    )

    def __init__(self, cfg: Config = config) -> None:
        if not cfg.anthropic_api_key:
            raise ConfigError("anthropic_api_key required for QualityAgent")
        self._cfg = cfg
        self._client = Anthropic(api_key=cfg.anthropic_api_key)

    def review(self, post: Post) -> None:
        """Run text and image QC on *post*, mutating it in-place when fixes apply.

        Raises QualityError if the thumbnail fails the visual check — the
        caller should mark the post failed so a human can review it in the
        dashboard.  Text fixes are applied silently and the post continues.
        """
        if post.caption:
            self._check_text(post)
        if post.thumbnail_url:
            self._check_image(post, post.thumbnail_url, label="thumbnail")

    def fix_text(self, post: Post) -> None:
        """Run text QC only; fix caption in-place if needed. Never raises."""
        if post.caption:
            try:
                self._check_text(post)
            except Exception:
                logger.exception("Text QC failed for post %s; caption unchanged", post.id)

    def check_image_bytes(self, post: Post, image_bytes: bytes, label: str = "thumbnail") -> None:
        """QC the brand overlay directly from bytes — no HTTP fetch, no upload needed.

        Raises QualityError if the overlay looks wrong.  Call this while the
        image is still in memory (before uploading) to avoid wasting a
        Supabase upload on an image that will be rejected.
        """
        image_data = base64.standard_b64encode(image_bytes).decode()
        self._check_image_data(post, image_data, media_type="image/png", label=label)

    # --- Text ---------------------------------------------------------------

    def _check_text(self, post: Post) -> None:
        """Detect and fix repeated phrases in the caption."""
        resp = self._client.messages.create(
            model=self._cfg.model_fast,
            max_tokens=1024,
            system=self._TEXT_SYSTEM,
            messages=[{"role": "user", "content": post.caption}],
        )
        reply = resp.content[0].text.strip()
        if reply.upper() == "OK":
            return
        post.caption = reply
        logger.info("QC: rewrote caption for post %s (double writing detected)", post.id)

    # --- Image --------------------------------------------------------------

    def _check_image(self, post: Post, url: str, label: str = "image") -> None:
        """Ask Claude vision whether the brand overlay looks correct (URL path)."""
        image_data, media_type = self._fetch_image(url)
        if image_data is None:
            logger.warning(
                "QC: could not fetch %s for post %s — skipping visual check",
                label,
                post.id,
            )
            return
        self._check_image_data(post, image_data, media_type=media_type, label=label)

    def _check_image_data(
        self,
        post: Post,
        image_data: str,
        media_type: str = "image/png",
        label: str = "image",
    ) -> None:
        """Core vision check — shared by URL and bytes paths."""
        resp = self._client.messages.create(
            model=self._cfg.model_fast,
            max_tokens=128,
            system=self._IMAGE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": "Check this image."},
                    ],
                }
            ],
        )

        raw = resp.content[0].text.strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "QC: non-JSON image response for post %s: %r — skipping",
                post.id,
                raw,
            )
            return

        if not result.get("ok", True):
            issue = result.get("issue", "unknown image quality issue")
            raise QualityError(f"{label}: {issue}")

    @staticmethod
    def _fetch_image(url: str) -> tuple[str | None, str]:
        """Download *url* and return (base64_data, media_type), or (None, '') on error."""
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(url)
                r.raise_for_status()
                data = base64.standard_b64encode(r.content).decode()
                media_type = r.headers.get("content-type", "image/jpeg").split(";")[0]
                return data, media_type
        except httpx.HTTPError:
            return None, ""
