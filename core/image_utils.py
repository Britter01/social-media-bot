"""Image post-processing — brand overlay compositing via Pillow.

Composites a pre-made transparent-background PNG logo onto Imagen-generated
photos.  No text rendering, no font downloads.

Two logo variants are available:
  • final_logo_transparent_allwhite.png — white text; use on dark/photo backgrounds
  • final_logo_transparent_allblack.png — black text; use on light/white backgrounds

The logo PNG is 1024×1024 with the actual wordmark centred inside it.
The content bounding box (from alpha inspection) is (264, 386, 758, 623), so
the logo is cropped to that region before scaling to keep sizing predictable.
"""

from __future__ import annotations

import io
import logging
import os

logger = logging.getLogger(__name__)

# Resolve logo paths relative to this file's package root
_ASSETS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets", "logos"))
_LOGO_WHITE = os.path.join(_ASSETS_DIR, "final_logo_transparent_allwhite.png")
_LOGO_BLACK = os.path.join(_ASSETS_DIR, "final_logo_transparent_allblack.png")

# Layout constants
_LOGO_WIDTH_RATIO = 0.32  # logo width as a fraction of the photo width
_LOGO_MIN_WIDTH = 120  # pixels
_LOGO_MAX_WIDTH = 380  # pixels
_PAD_BOTTOM = 28  # pixels from the bottom edge of the photo

# Pre-computed content bbox inside the 1024×1024 logo canvas (alpha getbbox result)
_LOGO_CONTENT_BOX = (264, 386, 758, 623)


def add_brand_overlay(
    image_bytes: bytes,
    brand_name: str = "Brite Tech Lifestyle",
    tagline: str = "",
    bar_opacity: int = 255,
    dark_logo: bool = False,
) -> bytes:
    """Composite the brand logo PNG onto *image_bytes*.

    Scales the logo to fit the image width and places it at the bottom-centre.
    Pass ``dark_logo=True`` when overlaying on a light/white background.

    Returns PNG bytes with the logo applied.  If Pillow is unavailable or the
    logo file is missing, the original bytes are returned unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; skipping brand overlay")
        return image_bytes

    logo_path = _LOGO_BLACK if dark_logo else _LOGO_WHITE
    if not os.path.exists(logo_path):
        logger.warning("Brand logo not found at %s; skipping overlay", logo_path)
        return image_bytes

    try:
        logo_full = Image.open(logo_path).convert("RGBA")
    except Exception:
        logger.warning("Could not load brand logo; skipping overlay", exc_info=True)
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = img.size

    # Crop to just the visible wordmark — removes blank transparent margins
    logo_cropped = logo_full.crop(_LOGO_CONTENT_BOX)

    # Scale proportionally to a sensible width for this image
    target_w = int(max(_LOGO_MIN_WIDTH, min(_LOGO_MAX_WIDTH, width * _LOGO_WIDTH_RATIO)))
    target_h = int(logo_cropped.height * target_w / logo_cropped.width)
    logo_scaled = logo_cropped.resize((target_w, target_h), Image.LANCZOS)

    # Bottom-centre position with padding
    paste_x = (width - target_w) // 2
    paste_y = height - target_h - _PAD_BOTTOM

    # Composite logo onto a blank RGBA layer then merge with photo
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.paste(logo_scaled, (paste_x, paste_y), logo_scaled)
    img = Image.alpha_composite(img, overlay)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    logger.debug("Brand overlay applied (%dx%d)", width, height)
    return out.getvalue()
