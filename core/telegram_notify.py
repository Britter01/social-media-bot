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


# Telegram limits: media caption ≤ 1024 chars, plain text message ≤ 4096 chars.
_TG_MEDIA_CAPTION_MAX = 1024
_TG_TEXT_MAX = 4096
_PHOTO_ICON = "\U0001f4f8"
_FILM_ICON = "\U0001f3ac"


def _tg(token: str, method: str, payload: dict) -> dict:
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _chunk_text(text: str, size: int) -> list[str]:
    """Split *text* into chunks of at most *size* chars.

    Prefers to break on a paragraph, then a line, then a space, so a story is
    never cut mid-word across Telegram messages.
    """
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    while len(text) > size:
        window = text[:size]
        cut = size
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > size * 0.5:
                cut = idx
                break
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def _send_caption_messages(token: str, chat_id: str, caption_text: str) -> None:
    """Send a (possibly long) caption as one or more plain-text messages.

    Plain text (no Markdown) so special characters in the caption can never
    trigger a Telegram parse error and drop the message.
    """
    for chunk in _chunk_text(caption_text, _TG_TEXT_MAX - 16):
        if chunk:
            _tg(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def send_instagram_post(post, cfg: Config = config) -> bool:
    """Send an Instagram post to Telegram for manual native publishing.

    Thin wrapper around :func:`send_post_to_telegram` for the Instagram label.
    """
    return send_post_to_telegram(post, "instagram", cfg)


def send_post_to_telegram(
    post, platform: str, cfg: Config = config, label_override: str | None = None
) -> bool:
    """Send a post to Telegram for manual native publishing on *platform*.

    Generalised version of ``send_instagram_post`` — supports Instagram,
    Facebook, X/Twitter, and LinkedIn. The header text in the Telegram message
    is adjusted to reflect the target platform; pass *label_override* to name a
    more specific destination (e.g. a particular LinkedIn company page).

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
    platform_label = label_override or _PLATFORM_LABELS.get(platform, platform.capitalize())

    token = cfg.telegram_bot_token
    chat_id = cfg.telegram_chat_id
    caption_text = post.caption_with_hashtags

    def _fits_inline(header: str) -> bool:
        # True if header + caption fit in a single media caption (≤ 1024).
        return len(f"{header}\n\n{caption_text}") <= _TG_MEDIA_CAPTION_MAX

    try:
        if post.post_type == "carousel" and post.slides:
            header = f"{_PHOTO_ICON} {platform_label} carousel ready to post"
            inline = _fits_inline(header)
            # Short caption → inline on the first image. Long caption → header
            # note on the image, then the full caption as separate text
            # message(s) so nothing is cut off at Telegram's 1024 caption cap.
            first_caption = (
                f"{header}\n\n{caption_text}" if inline else f"{header}\n\nFull caption below ⬇️"
            )
            media = []
            for i, slide in enumerate(post.slides):
                image_url = slide.get("image_url", "")
                if not image_url:
                    continue
                item: dict = {"type": "photo", "media": image_url}
                if i == 0:
                    item["caption"] = first_caption[:_TG_MEDIA_CAPTION_MAX]
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
                        "caption": first_caption[:_TG_MEDIA_CAPTION_MAX],
                    },
                )
            else:
                logger.warning("Carousel post %s has no slide images; skipping Telegram", post.id)
                return False
            if not inline:
                _send_caption_messages(token, chat_id, caption_text)

        elif post.post_type in ("reel", "infographic_reel"):
            # Video files are large, so send a header + download link, then the
            # full caption as its own message(s).
            if post.video_url:
                head = (
                    f"{_FILM_ICON} {platform_label} Reel ready to post\n\n"
                    f"Download video: {post.video_url}"
                )
            else:
                head = f"{_FILM_ICON} {platform_label} Reel ready (no video yet)"
            _tg(token, "sendMessage", {"chat_id": chat_id, "text": head[:_TG_TEXT_MAX]})
            _send_caption_messages(token, chat_id, caption_text)

        elif post.thumbnail_url:
            header = f"{_PHOTO_ICON} {platform_label} post ready to post"
            inline = _fits_inline(header)
            _tg(
                token,
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": post.thumbnail_url,
                    "caption": (
                        f"{header}\n\n{caption_text}"
                        if inline
                        else f"{header}\n\nFull caption below ⬇️"
                    )[:_TG_MEDIA_CAPTION_MAX],
                },
            )
            if not inline:
                _send_caption_messages(token, chat_id, caption_text)
        else:
            _tg(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"{_PHOTO_ICON} {platform_label} post ready (no image yet)",
                },
            )
            _send_caption_messages(token, chat_id, caption_text)

        logger.info("Telegram notification sent for %s post %s", platform, post.id)
        return True

    except Exception:
        logger.exception("Telegram notification failed for post %s", post.id)
        return False
