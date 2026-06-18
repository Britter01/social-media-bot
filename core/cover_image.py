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

# Pre-measured screen corners for each scene file (verified via OpenCV contour detection).
# Format: filename → (is_white_screen, [TL, TR, BR, BL]) in image pixel coordinates.
# These replace runtime detection for the four known scenes, giving exact perspective
# warping without being sensitive to lighting variations or threshold tuning.
_SCENE_CORNERS: dict[str, tuple[bool, list[tuple[int, int]]]] = {
    # Corners stored in original image pixel coordinates: [TL, TR, BR, BL].
    # Portrait scenes (1744×2336) → cropped to 1744×1744 → resized 1080×1080:
    #   scale ≈ 0.619×, so 10 original px ≈ 6 output px.
    # Scene 3 (2048×2048 square) → resized directly to 1080×1080:
    #   scale ≈ 0.527×, so 12 original px ≈ 6 output px.
    # All four corners pulled inward by those amounts on 2026-06-18 to remove
    # a ~6 px overhang visible on each edge of the rendered card.
    "scene1_library.png": (
        False,
        [(330, 894), (1103, 858), (1153, 1398), (390, 1481)],
    ),
    "scene2_cafe_iced.png": (
        False,
        [(401, 1024), (1174, 1021), (1182, 1607), (408, 1628)],
    ),
    "scene3_woman.png": (
        True,
        [(1276, 945), (1903, 960), (1812, 1465), (1177, 1378)],
    ),
    "scene4_cafe_latte.png": (
        False,
        [(163, 730), (1028, 666), (1088, 1233), (231, 1361)],
    ),
}

# Brightness thresholds for screen detection
_BLACK_THRESH = 28  # pixels darker than this → "black screen" candidate
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


def _longest_run(arr) -> tuple[int, int]:
    """Return (start, end) of the longest contiguous True region in a 1-D bool array."""
    import numpy as np

    padded = np.concatenate([[False], arr.astype(bool), [False]])
    diffs = np.diff(padded.astype(np.int8))
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]
    if len(starts) == 0:
        return 0, 0
    lengths = ends - starts
    best = int(np.argmax(lengths))
    return int(starts[best]), int(ends[best])


def _detect_screen(pil_img):
    """Locate the laptop screen in *pil_img*.

    Returns (corners, is_white) where corners is a (4, 2) float32 numpy array
    ordered [top-left, top-right, bottom-right, bottom-left], or (None, False)
    if detection fails.

    Strategy: build a per-row and per-column coverage vector (fraction of pixels
    at extreme brightness), then find the *longest contiguous run* of high-coverage
    rows — this band corresponds to the screen and ignores scattered dark/bright
    background pixels (book spines, shadows, windows, table reflections).  The
    column band is then extracted from within those rows.  This is far more robust
    than a global bounding-box approach for real lifestyle photography.
    """
    try:
        import numpy as np
    except ImportError:
        return None, False

    gray = np.array(pil_img.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    total = h * w

    # What fraction of pixels in a single row/column must be extreme-brightness
    # for that row/col to count as "inside the screen".  0.25 is deliberately low
    # to handle scenes where the screen occupies only ~35% of each row (e.g. the
    # side-positioned white screen in scene3_woman).
    _ROW_COV_THRESH = 0.25
    _COL_COV_THRESH = 0.25

    for is_white, raw_mask in [
        (False, gray < _BLACK_THRESH),
        (True, gray > _WHITE_THRESH),
    ]:
        mask = raw_mask.astype(np.float32)

        # --- Row band: find the longest run of rows with high coverage ---
        row_cov = mask.mean(axis=1)  # (h,) — fraction of dark/bright per row
        y1, y2 = _longest_run(row_cov > _ROW_COV_THRESH)
        if y2 - y1 < 30:
            continue

        # --- Column band: within those rows, find the longest high-coverage run ---
        col_cov = mask[y1:y2, :].mean(axis=0)  # (w,)
        x1, x2 = _longest_run(col_cov > _COL_COV_THRESH)
        if x2 - x1 < 40:
            continue

        scr_w = x2 - x1
        scr_h = y2 - y1

        # Area and aspect-ratio checks
        box_area = scr_w * scr_h
        if not (total * _MIN_AREA_RATIO < box_area < total * _MAX_AREA_RATIO):
            continue
        aspect = scr_w / scr_h
        if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
            continue

        # Fill density — now measured *inside* the identified band, so scattered
        # pixels elsewhere in the image do not dilute this score.
        fill = mask[y1:y2, x1:x2].mean()
        if fill < _MIN_FILL_DENSITY:
            continue

        # Refine the four perspective corners using the diagonal-sort method:
        # TL=min(x+y), TR=min(y−x), BR=max(x+y), BL=max(y−x).
        # This correctly handles screens at any rotation, unlike the old approach
        # which forced horizontal top/bottom edges (TL.y always == TR.y).
        # Average the extreme 5 % to reduce noise from scattered pixels.
        ys_r, xs_r = np.where(raw_mask[y1:y2, x1:x2])
        ys_r = ys_r + y1
        xs_r = xs_r + x1

        d_sum = xs_r.astype(np.float64) + ys_r  # min → TL, max → BR
        d_dif = ys_r.astype(np.float64) - xs_r  # min → TR, max → BL

        def _corner(score, pct, _xs=xs_r, _ys=ys_r):
            thresh = np.percentile(score, pct)
            m = (score <= thresh) if pct < 50 else (score >= thresh)
            return (float(np.mean(_xs[m])), float(np.mean(_ys[m])))

        tl = _corner(d_sum, 5)
        tr = _corner(d_dif, 5)
        br = _corner(d_sum, 95)
        bl = _corner(d_dif, 95)

        corners = np.array([tl, tr, br, bl], dtype=np.float32)
        logger.debug(
            "Screen detected (%s): (%d,%d)→(%d,%d) aspect=%.2f fill=%.2f",
            "white" if is_white else "black",
            x1,
            y1,
            x2,
            y2,
            aspect,
            fill,
        )
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
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts, strict=False):
        A.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        A.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        b.extend([sx, sy])
    coeffs = np.linalg.solve(np.array(A, dtype=np.float64), np.array(b, dtype=np.float64))
    return tuple(float(c) for c in coeffs)


def _render_text_card(
    card_w: int, card_h: int, headline: str, subtext: str, is_white: bool, brand_tagline: str
):
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
        draw,
        headline,
        _FONT_HEADLINE,
        max(26, int(card_h * 0.14)),
        max(16, int(card_h * 0.07)),
        max_w,
        max_lines=3,
    )
    hl_lh = int(hl_sz * 1.1)

    sub_lines: list[str] = []
    sub_lh = 0
    font_sub = None
    sub_sz = 0
    if subtext:
        font_sub, sub_lines, sub_sz = _fit_lines(
            draw,
            subtext,
            _FONT_BODY,
            max(13, int(card_h * 0.065)),
            max(10, int(card_h * 0.046)),
            max_w,
            max_lines=3,
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

    scene_basename = os.path.basename(scene_path)
    if scene_basename in _SCENE_CORNERS:
        is_white, corner_list = _SCENE_CORNERS[scene_basename]
        import numpy as np

        corners = np.array(corner_list, dtype=np.float32)
        logger.debug("Using pre-measured corners for %s", scene_basename)
    else:
        corners, is_white = _detect_screen(scene)
        if corners is None:
            logger.info(
                "Screen not detected in %s; skipping scene cover",
                os.path.basename(scene_path),
            )
            return None

    tl, tr, br, bl = corners

    # Compute the pixel dimensions of the detected screen area
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

        warped = card.transform((scene_w, scene_h), PILImage.PERSPECTIVE, coeffs, PILImage.BICUBIC)
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
        os.path.basename(scene_path),
        scr_w,
        scr_h,
        "white" if is_white else "black",
    )
    return out.getvalue()
