"""Carousel cover slide — composites headline text onto a laptop screen mockup.

For each carousel, one of four scene photos is selected by hashing the post_id
(so the same post always gets the same scene and scenes are distributed evenly).
The laptop screen is located by brightness thresholding on the grayscale image.
A text card (headline + subtext in brand fonts) is perspective-warped into the
detected screen area, then the whole image is square-cropped to 1080×1080.

Falls back gracefully (returns None) when:
  • numpy is not installed
  • the scene file is missing from assets/scenes/
  • screen detection fails to find a qualifying quadrilateral

The caller (carousel_agent) renders a dark text card in all fallback cases.

Scene files (place in assets/scenes/):
  scene1_library.png    — library desk with tea mug and bookshelf
  scene2_cafe_iced.png  — industrial café with iced coffee, plants
  scene3_woman.png      — woman with coffee cup, white-screen laptop in café
  scene4_cafe_latte.png — wooden café table with latte, phone, smartwatch

Dependencies: Pillow (already present), numpy (add to requirements)
"""

from __future__ import annotations

import io
import logging
import os

logger = logging.getLogger(__name__)

_ASSETS_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_SCENES_DIR = os.path.join(_ASSETS_ROOT, "scenes")

_SCENE_FILES = [
    "scene1_library.png",
    "scene2_cafe_iced.png",
    "scene3_woman.png",
    "scene4_cafe_latte.png",
]

# Brightness thresholds for screen detection
_BLACK_THRESH = 28   # pixels darker than this → "black screen" candidate
_WHITE_THRESH = 225  # pixels brighter than this → "white screen" candidate

# Screen must be between 4% and 60% of the total image area
_MIN_AREA_RATIO = 0.04
_MAX_AREA_RATIO = 0.60

# Screen aspect ratio for a laptop (width / height): 16:9 ≈ 1.78, 16:10 ≈ 1.6
_MIN_ASPECT = 1.1
_MAX_ASPECT = 2.3

# Minimum fill density of the mask inside its bounding box (filters scattered noise)
_MIN_FILL_DENSITY = 0.45


def _pick_scene(post_id: str) -> str:
    """Return the scene file path for this post (deterministic round-robin)."""
    idx = int(post_id.replace("-", "")[:8], 16) % len(_SCENE_FILES)
    return os.path.join(_SCENES_DIR, _SCENE_FILES[idx])


def _detect_screen(pil_img):
    """Locate the laptop screen in *pil_img*.

    Returns (corners, is_white) where corners is a (4, 2) float32 numpy array
    ordered [top-left, top-right, bottom-right, bottom-left], or (None, False)
    if detection fails.

    Uses row/column histogram analysis to find the densest axis-aligned run of
    very dark (black screen) or very bright (white screen) pixels, then refines
    the four corners by scanning the top and bottom quintiles of the mask to
    capture perspective lean.
    """
    try:
        import numpy as np
    except ImportError:
        return None, False

    gray = np.array(pil_img.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    total = h * w

    for is_white, cond in [
        (False, gray < _BLACK_THRESH),
        (True, gray > _WHITE_THRESH),
    ]:
        ys, xs = np.where(cond)
        n = len(xs)
        if not (total * _MIN_AREA_RATIO < n < total * _MAX_AREA_RATIO):
            continue

        y_min = int(np.percentile(ys, 1))
        y_max = int(np.percentile(ys, 99))
        x_min = int(np.percentile(xs, 1))
        x_max = int(np.percentile(xs, 99))

        box_area = max((x_max - x_min) * (y_max - y_min), 1)
        fill = n / box_area
        if fill < _MIN_FILL_DENSITY:
            continue

        scr_w = x_max - x_min
        scr_h = y_max - y_min
        if scr_h < 20 or scr_w < 20:
            continue
        aspect = scr_w / scr_h
        if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
            continue

        # Refine corners from top-quintile and bottom-quintile of the mask
        q = scr_h * 0.2
        top_mask = ys < y_min + q
        bot_mask = ys > y_max - q

        if top_mask.any() and bot_mask.any():
            tl = (float(np.percentile(xs[top_mask], 5)),
                  float(np.percentile(ys[top_mask], 5)))
            tr = (float(np.percentile(xs[top_mask], 95)),
                  float(np.percentile(ys[top_mask], 5)))
            br = (float(np.percentile(xs[bot_mask], 95)),
                  float(np.percentile(ys[bot_mask], 95)))
            bl = (float(np.percentile(xs[bot_mask], 5)),
                  float(np.percentile(ys[bot_mask], 95)))
        else:
            tl = (float(x_min), float(y_min))
            tr = (float(x_max), float(y_min))
            br = (float(x_max), float(y_max))
            bl = (float(x_min), float(y_max))

        corners = np.array([tl, tr, br, bl], dtype=np.float32)
        return corners, is_white

    return None, False


def _perspective_coeffs(src_pts, dst_pts):
    """Compute the 8 Pillow PERSPECTIVE transform coefficients.

    Pillow maps output (dst) coordinates back to input (src) coordinates:
        x_src = (a0*x + a1*y + a2) / (a6*x + a7*y + 1)
        y_src = (a3*x + a4*y + a5) / (a6*x + a7*y + 1)

    Given 4 point pairs (dst[i] → src[i]) we solve the 8×8 linear system.
    """
    import numpy as np

    A = []
    b = []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        A.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        A.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        b.extend([sx, sy])
    coeffs = np.linalg.solve(np.array(A, dtype=np.float64),
                             np.array(b, dtype=np.float64))
    return tuple(float(c) for c in coeffs)


def _render_text_card(card_w: int, card_h: int, headline: str, subtext: str,
                      is_white: bool, brand_tagline: str):
    """Render headline + subtext as a Pillow RGBA image sized card_w × card_h."""
    from PIL import Image, ImageDraw
    from core.image_utils import _FONT_BODY, _FONT_HEADLINE, _FONT_TAGLINE, _fit_lines, _load_font

    bg = (255, 255, 255, 255) if is_white else (4, 4, 8, 255)
    fg_hl = (15, 15, 20, 255) if is_white else (255, 255, 255, 255)
    fg_body = (70, 70, 90, 210) if is_white else (175, 180, 200, 210)
    fg_tag = (120, 120, 140, 180) if is_white else (100, 108, 126, 180)

    img = Image.new("RGBA", (card_w, card_h), bg)
    draw = ImageDraw.Draw(img)

    pad_x = max(18, int(card_w * 0.08))
    pad_y = max(14, int(card_h * 0.08))
    max_w = card_w - 2 * pad_x

    font_hl, hl_lines, hl_sz = _fit_lines(
        draw, headline, _FONT_HEADLINE,
        max(26, int(card_h * 0.14)), max(16, int(card_h * 0.07)),
        max_w, max_lines=3,
    )
    hl_lh = int(hl_sz * 1.1)

    sub_lines: list[str] = []
    sub_lh = 0
    font_sub = None
    sub_sz = 0
    if subtext:
        font_sub, sub_lines, sub_sz = _fit_lines(
            draw, subtext, _FONT_BODY,
            max(13, int(card_h * 0.065)), max(10, int(card_h * 0.046)),
            max_w, max_lines=3,
        )
        sub_lh = int(sub_sz * 1.35)

    gap = int(sub_sz * 0.55) if sub_lines else 0
    block_h = hl_lh * len(hl_lines) + (gap + sub_lh * len(sub_lines) if sub_lines else 0)
    text_y = max(pad_y, (card_h - block_h) // 2)

    for line in hl_lines:
        draw.text((pad_x, text_y), line, font=font_hl, fill=fg_hl)
        text_y += hl_lh
    if sub_lines:
        text_y += gap
        for line in sub_lines:
            draw.text((pad_x, text_y), line, font=font_sub, fill=fg_body)
            text_y += sub_lh

    if brand_tagline:
        font_tag = _load_font(_FONT_TAGLINE, max(9, int(card_h * 0.05)))
        tbbox = draw.textbbox((0, 0), brand_tagline, font=font_tag)
        ty = card_h - pad_y - (tbbox[3] - tbbox[1])
        draw.text((pad_x, ty), brand_tagline, font=font_tag, fill=fg_tag)

    return img


def _crop_to_square(pil_img, corners=None, target: int = 1080):
    """Square-crop *pil_img* centred on the detected screen and resize to *target*."""
    w, h = pil_img.size
    side = min(w, h)

    if corners is not None:
        try:
            import numpy as np
            cx = float(np.mean(corners[:, 0]))
            cy = float(np.mean(corners[:, 1]))
        except Exception:
            cx, cy = w / 2, h / 2
    else:
        cx, cy = w / 2, h / 2

    if h > w:
        # Portrait — crop height around screen centre
        y1 = int(cy - side / 2)
        y1 = max(0, min(y1, h - side))
        pil_img = pil_img.crop((0, y1, w, y1 + side))
    elif w > h:
        # Landscape — crop width around screen centre
        x1 = int(cx - side / 2)
        x1 = max(0, min(x1, w - side))
        pil_img = pil_img.crop((x1, 0, x1 + side, h))

    if side != target:
        from PIL import Image
        pil_img = pil_img.resize((target, target), Image.LANCZOS)
    return pil_img


def make_scene_cover(
    post_id: str,
    headline: str,
    subtext: str,
    brand_name: str = "",
    brand_tagline: str = "",
) -> bytes | None:
    """Render a carousel cover by placing headline text on a scene's laptop screen.

    Returns PNG bytes on success, None on any failure — the caller falls back
    to make_dark_text_card so carousels always produce a cover slide.
    """
    try:
        import numpy as np  # noqa: F401 — needed by helpers; fail fast if missing
    except ImportError:
        logger.debug("numpy not installed; scene cover unavailable")
        return None

    scene_path = _pick_scene(post_id)
    if not os.path.isfile(scene_path):
        logger.debug("Scene file missing: %s", scene_path)
        return None

    try:
        from PIL import Image
        scene = Image.open(scene_path).convert("RGBA")
    except Exception as exc:
        logger.warning("Could not open scene %s: %s", scene_path, exc)
        return None

    scene_w, scene_h = scene.size

    corners, is_white = _detect_screen(scene)
    if corners is None:
        logger.info("Screen not detected in %s; skipping scene cover", os.path.basename(scene_path))
        return None

    tl, tr, br, bl = corners

    # Compute the pixel dimensions of the detected screen area
    import numpy as np

    scr_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    scr_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if scr_w < 60 or scr_h < 40:
        logger.info("Detected screen too small (%dx%d)", scr_w, scr_h)
        return None

    # Render text card at screen resolution
    card = _render_text_card(scr_w, scr_h, headline, subtext, is_white, brand_tagline)

    # Perspective-warp the card into the scene at the screen corners.
    # Pillow PERSPECTIVE maps output → input, so dst=scene corners, src=card corners.
    card_corners = [(0, 0), (scr_w, 0), (scr_w, scr_h), (0, scr_h)]
    screen_corners = [tuple(p) for p in [tl, tr, br, bl]]
    try:
        coeffs = _perspective_coeffs(card_corners, screen_corners)
    except Exception as exc:
        logger.warning("Perspective coefficient computation failed: %s", exc)
        return None

    # Transform the card (card-sized input → scene-sized output)
    try:
        from PIL import Image as PILImage
        warped = card.transform(
            (scene_w, scene_h), PILImage.PERSPECTIVE, coeffs, PILImage.BICUBIC
        )
    except Exception as exc:
        logger.warning("Perspective transform failed: %s", exc)
        return None

    # Build a mask (alpha=255 inside the warped screen, 0 outside) for compositing.
    mask_src = PILImage.new("L", (scr_w, scr_h), 255)
    try:
        mask = mask_src.transform(
            (scene_w, scene_h), PILImage.PERSPECTIVE, coeffs, PILImage.NEAREST
        )
    except Exception:
        mask = PILImage.new("L", (scene_w, scene_h), 0)

    # Composite: paste warped card onto scene using mask
    composite = scene.copy()
    warped_rgb = warped.convert("RGBA")
    composite.paste(warped_rgb, (0, 0), mask)

    # Square-crop centred on the screen, resize to 1080×1080
    result = _crop_to_square(composite.convert("RGB"), corners, target=1080)

    out = io.BytesIO()
    result.save(out, format="PNG", optimize=True)
    logger.info(
        "Scene cover rendered: %s (screen %dx%d, %s screen)",
        os.path.basename(scene_path), scr_w, scr_h, "white" if is_white else "black",
    )
    return out.getvalue()
