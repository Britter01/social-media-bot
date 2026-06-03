"""Image post-processing — brand overlay compositing via Pillow.

Composites a pre-made transparent-background PNG logo onto Imagen-generated
photos.  No text rendering, no font downloads.

Two logo variants are available:
  • final_logo_transparent_allwhite.png — white text; use on dark/photo backgrounds
  • final_logo_transparent_allblack.png — black text; use on light/white backgrounds

The logo PNG is 1024×1024 with the actual wordmark centred inside it.
The content bounding box (from alpha inspection) is (264, 386, 758, 623), so
the logo is cropped to that region before scaling to keep sizing predictable.

Logo placement:
  The four corners of the image are scored by edge density (text and busy
  graphics produce high-contrast edges).  The logo is placed in whichever
  corner has the least activity, keeping it away from any writing already
  present in the photo.
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

# Layout constants — small, placed in the quietest corner
_LOGO_WIDTH_RATIO = 0.15  # logo width as a fraction of the photo width
_LOGO_MIN_WIDTH = 80  # pixels
_LOGO_MAX_WIDTH = 160  # pixels
_PAD = 18  # pixels of padding from each edge

# Pre-computed content bbox inside the 1024×1024 logo canvas (alpha getbbox result)
_LOGO_CONTENT_BOX = (264, 386, 758, 623)


def _quietest_corner(
    img,
    logo_w: int,
    logo_h: int,
    pad: int,
) -> tuple[int, int]:
    """Return (x, y) for the corner with the lowest edge density.

    Each corner is sampled over the exact region the logo would occupy.
    Edge density is a proxy for text and busy graphics — the logo is placed
    in the corner where the sum of edge intensities is lowest.
    """
    from PIL import ImageFilter

    width, height = img.size

    candidates = {
        "top_right": (width - logo_w - pad, pad),
        "top_left": (pad, pad),
        "bottom_right": (width - logo_w - pad, height - logo_h - pad),
        "bottom_left": (pad, height - logo_h - pad),
    }

    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)

    best_pos = candidates["top_right"]  # default preference
    best_score: float = float("inf")

    for pos in candidates.values():
        x, y = pos
        region = edges.crop((x, y, x + logo_w, y + logo_h))
        score = sum(region.getdata())
        if score < best_score:
            best_score = score
            best_pos = pos

    return best_pos


def add_brand_overlay(
    image_bytes: bytes,
    brand_name: str = "Brite Tech Lifestyle",
    tagline: str = "",
    bar_opacity: int = 255,
    dark_logo: bool = False,
) -> bytes:
    """Composite the brand logo PNG onto *image_bytes*.

    Scales the logo to ~15% of the image width and places it in whichever
    corner has the least visual activity (text and busy areas score high;
    the logo lands in the quietest corner to stay out of the way).
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

    # Pick the corner furthest from any writing or busy graphics
    paste_x, paste_y = _quietest_corner(img, target_w, target_h, _PAD)

    # Composite logo onto a blank RGBA layer then merge with photo
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.paste(logo_scaled, (paste_x, paste_y), logo_scaled)
    img = Image.alpha_composite(img, overlay)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    logger.debug("Brand overlay applied (%dx%d) at (%d,%d)", width, height, paste_x, paste_y)
    return out.getvalue()
