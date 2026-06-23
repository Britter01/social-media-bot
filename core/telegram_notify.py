"""Telegram notification helper — delivers posts for manual native publishing.

When Instagram posts are routed away from the Graph API (to avoid algorithmic
reach suppression), this module sends the image + caption directly to the user's
Telegram chat so they can post natively from the Instagram app.

The generalised ``send_post_to_telegram`` function supports all platforms
(Facebook, X/Twitter, LinkedIn, Instagram) and is used by the per-platform
Telegram delivery mode.
"""

from __future__ import annotations

import logging

import httpx

from core.config import Config, config

logger = logging.getLogger(__name__)


def _tg(token: str, method: str, payload: dict) -> dict:
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def send_instagram_post(post, cfg: Config = config) -> bool:
    """Send an Instagram post to Telegram for manual native publishing.

    Handles standard posts (single photo), carousels (media group), and
    Reels (text message with download link, since video files are large).
    Returns True if the notification was sent, False on failure or misconfiguration.
    """
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        logger.warning(
            "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "in Railway to enable Instagram manual-publish notifications"
        )
        return False

    token = cfg.telegram_bot_token
    chat_id = cfg.telegram_chat_id
    caption_text = post.caption_with_hashtags

    try:
        if post.post_type == "carousel" and post.slides:
            media = []
            for i, slide in enumerate(post.slides):
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
                item: dict = {"type": "photo", "media": image_url}
                if i == 0:
                    item["caption"] = (
                        f"\U0001f4f8 *Instagram carousel ready to post*\n\n{caption_text}"
                    )[:1024]
                    item["parse_mode"] = "Markdown"
                media.append(item)
            if len(media) >= 2:
                _tg(token, "sendMediaGroup", {"chat_id": chat_id, "media": media})
            elif media:
                _tg(
                    token,
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "photo": media[0]["media"],
                        "caption": (f"\U0001f4f8 *Instagram post ready to post*\n\n{caption_text}")[
                            :1024
                        ],
                        "parse_mode": "Markdown",
                    },
                )
            else:
                logger.warning("Carousel post %s has no slide images; skipping Telegram", post.id)
                return False

        elif post.post_type in ("reel", "infographic_reel"):
            if post.video_url:
                msg = (
                    f"\U0001f3ac *Instagram Reel ready to post*\n\n"
                    f"{caption_text}\n\n"
                    f"[Download video]({post.video_url})"
                )
            else:
                msg = f"\U0001f3ac *Instagram Reel ready* (no video yet)\n\n{caption_text}"
            _tg(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": msg[:4096], "parse_mode": "Markdown"},
            )

        elif post.thumbnail_url:
            _tg(
                token,
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": post.thumbnail_url,
                    "caption": (f"\U0001f4f8 *Instagram post ready to post*\n\n{caption_text}")[
                        :1024
                    ],
                    "parse_mode": "Markdown",
                },
            )
        else:
            _tg(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (f"\U0001f4f8 Instagram post ready (no image yet)\n\n{caption_text}")[
                        :4096
                    ],
                },
            )

        logger.info("Telegram notification sent for Instagram post %s", post.id)
        return True

    except Exception:
        logger.exception("Telegram notification failed for post %s", post.id)
        return False


def send_post_to_telegram(post, platform: str, cfg: Config = config) -> bool:
    """Send a post to Telegram for manual native publishing on *platform*.

    Generalised version of ``send_instagram_post`` — supports Instagram,
    Facebook, X/Twitter, and LinkedIn. The header text in the Telegram message
    is adjusted to reflect the target platform.

    Returns True if the notification was sent, False on failure or misconfiguration.
    """
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        logger.warning(
            "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "in Railway to enable %s manual-publish notifications",
            platform,
        )
        return False

    _PLATFORM_LABELS = {
        "facebook": "Facebook",
        "twitter": "X / Twitter",
        "linkedin": "LinkedIn",
        "instagram": "Instagram",
    }
    platform_label = _PLATFORM_LABELS.get(platform, platform.capitalize())

    token = cfg.telegram_bot_token
    chat_id = cfg.telegram_chat_id
    caption_text = post.caption_with_hashtags

    try:
        if post.post_type == "carousel" and post.slides:
            media = []
            for i, slide in enumerate(post.slides):
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
                item: dict = {"type": "photo", "media": image_url}
                if i == 0:
                    item["caption"] = (
                        f"\U0001f4f8 *{platform_label} carousel ready to post*\n\n{caption_text}"
                    )[:1024]
                    item["parse_mode"] = "Markdown"
                media.append(item)
            if len(media) >= 2:
                _tg(token, "sendMediaGroup", {"chat_id": chat_id, "media": media})
            elif media:
                _tg(
                    token,
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "photo": media[0]["media"],
                        "caption": (
                            f"\U0001f4f8 *{platform_label} post ready to post*\n\n{caption_text}"
                        )[:1024],
                        "parse_mode": "Markdown",
                    },
                )
            else:
                logger.warning("Carousel post %s has no slide images; skipping Telegram", post.id)
                return False

        elif post.post_type in ("reel", "infographic_reel"):
            if post.video_url:
                msg = (
                    f"\U0001f3ac *{platform_label} Reel ready to post*\n\n"
                    f"{caption_text}\n\n"
                    f"[Download video]({post.video_url})"
                )
            else:
                msg = f"\U0001f3ac *{platform_label} Reel ready* (no video yet)\n\n{caption_text}"
            _tg(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": msg[:4096], "parse_mode": "Markdown"},
            )

        elif post.thumbnail_url:
            _tg(
                token,
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": post.thumbnail_url,
                    "caption": (
                        f"\U0001f4f8 *{platform_label} post ready to post*\n\n{caption_text}"
                    )[:1024],
                    "parse_mode": "Markdown",
                },
            )
        else:
            _tg(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"\U0001f4f8 {platform_label} post ready (no image yet)\n\n{caption_text}"
                    )[:4096],
                },
            )

        logger.info("Telegram notification sent for %s post %s", platform, post.id)
        return True

    except Exception:
        logger.exception("Telegram notification failed for post %s", post.id)
        return False
