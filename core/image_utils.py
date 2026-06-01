"""Image post-processing — brand overlay compositing via Pillow.

Applies the Brite Tech Lifestyle wordmark directly onto Imagen-generated
photos. No background bar — just the brand text floating over the image
with a subtle drop shadow for legibility on any background.

Overlay design (matches brand-kit typography spec):
  • "Brite" — Figtree Bold 700, white, tight tracking (−4%)
  • "TECH LIFESTYLE" — Figtree Regular, UPPERCASE, wide tracking (+22%),
    white at 55% opacity
  • Soft dark drop shadow on both lines for contrast on bright backgrounds
  • Positioned bottom-centre with comfortable padding

Fonts are downloaded from the jsDelivr/Fontsource CDN on first use and cached
in the system temp directory so Railway containers pick them up automatically.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Brand tokens (from brand-kit CSS variables)                                 #
# --------------------------------------------------------------------------- #
_WHITE = (255, 255, 255)
_SHADOW = (0, 0, 0)

# Wordmark opacities
_BRITE_ALPHA  = 230   # "Brite" — near-full white, slightly soft
_SUB_ALPHA    = 140   # "TECH LIFESTYLE" — white at ~55% opacity

# Shadow
_SHADOW_ALPHA  = 120  # shadow darkness
_SHADOW_OFFSET = 1    # pixels offset for drop shadow

# Bottom padding from image edge
_PAD_BOTTOM = 28

# Font sizes
_SIZE_BRITE = 36
_SIZE_SUB   = 11

# Letter-spacing simulation (extra pixels between characters)
_TRACKING_BRITE = -1   # slight tightening
_TRACKING_SUB   = 3    # wide tracking for subtitle

# --------------------------------------------------------------------------- #
# Font loading                                                                 #
# --------------------------------------------------------------------------- #
_FONT_CACHE_DIR = os.path.join(tempfile.gettempdir(), "btl_fonts")
_FONT_URLS = {
    "figtree-bold":    "https://cdn.jsdelivr.net/fontsource/fonts/figtree@latest/latin-700-normal.ttf",
    "figtree-regular": "https://cdn.jsdelivr.net/fontsource/fonts/figtree@latest/latin-400-normal.ttf",
}


def _get_font(name: str, size: int):
    """Load a TrueType font, downloading and caching it on first use."""
    from PIL import ImageFont

    os.makedirs(_FONT_CACHE_DIR, exist_ok=True)
    path = os.path.join(_FONT_CACHE_DIR, f"{name}.ttf")

    if not os.path.exists(path):
        url = _FONT_URLS.get(name)
        if url:
            try:
                import requests
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(resp.content)
                logger.info("Downloaded font %s -> %s", name, path)
            except Exception:
                logger.warning("Could not download font %s; using default", name, exc_info=True)
                path = None

    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    # Fallback — Pillow built-in bitmap font (Pillow 10.1+ supports size param)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text_width(font, text: str, tracking: int = 0) -> int:
    """Measure the rendered width of ``text`` including extra tracking."""
    try:
        bbox = font.getbbox(text)
        base_w = bbox[2] - bbox[0]
    except Exception:
        base_w = len(text) * (font.size if hasattr(font, "size") else 10)
    return base_w + tracking * max(0, len(text) - 1)


def _draw_tracked(draw, xy: tuple, text: str, font, fill: tuple, tracking: int = 0) -> None:
    """Draw text with per-character letter-spacing (tracking).

    Pillow doesn't support CSS letter-spacing natively, so we place each
    character individually with the extra gap applied between them.
    """
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        try:
            bbox = font.getbbox(char)
            char_w = bbox[2] - bbox[0]
        except Exception:
            char_w = font.size if hasattr(font, "size") else 10
        x += char_w + tracking


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def add_brand_overlay(
    image_bytes: bytes,
    brand_name: str = "Brite Tech Lifestyle",
    tagline: str = "",
    bar_opacity: int = 255,
) -> bytes:
    """Composite the Brite Tech Lifestyle wordmark onto an image.

    Text floats directly on the photo — no background bar.
    A soft drop shadow ensures legibility on bright or busy backgrounds.

    Returns PNG bytes with the wordmark applied.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; skipping brand overlay")
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = img.size

    font_brite = _get_font("figtree-bold",    _SIZE_BRITE)
    font_sub   = _get_font("figtree-regular", _SIZE_SUB)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # ── "TECH LIFESTYLE" — small, wide-tracked, positioned first ────────────
    sub_text = "TECH LIFESTYLE"
    sub_w    = _text_width(font_sub, sub_text, _TRACKING_SUB)
    sub_x    = (width - sub_w) // 2
    sub_y    = height - _PAD_BOTTOM - _SIZE_SUB

    # Shadow
    _draw_tracked(draw, (sub_x + _SHADOW_OFFSET, sub_y + _SHADOW_OFFSET),
                  sub_text, font_sub, (*_SHADOW, _SHADOW_ALPHA), _TRACKING_SUB)
    # Text
    _draw_tracked(draw, (sub_x, sub_y),
                  sub_text, font_sub, (*_WHITE, _SUB_ALPHA), _TRACKING_SUB)

    # ── "Brite" — bold, above subtitle ──────────────────────────────────────
    brite_w = _text_width(font_brite, "Brite", _TRACKING_BRITE)
    brite_x = (width - brite_w) // 2
    brite_y = sub_y - _SIZE_BRITE - 4

    # Shadow
    _draw_tracked(draw, (brite_x + _SHADOW_OFFSET, brite_y + _SHADOW_OFFSET),
                  "Brite", font_brite, (*_SHADOW, _SHADOW_ALPHA), _TRACKING_BRITE)
    # Text
    _draw_tracked(draw, (brite_x, brite_y),
                  "Brite", font_brite, (*_WHITE, _BRITE_ALPHA), _TRACKING_BRITE)

    img = Image.alpha_composite(img, overlay)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    logger.debug("Brand overlay applied (%dx%d)", width, height)
    return out.getvalue()
