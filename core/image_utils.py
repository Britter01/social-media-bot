"""Image post-processing — brand overlay compositing and carousel slide rendering.

Brand overlay:
  Composites a pre-made transparent-background PNG logo onto Imagen-generated
  photos.  No text rendering, no font downloads for the overlay itself.

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

  Logo colour:
    After the corner is chosen, the average brightness of that region is
    measured.  Light backgrounds get the dark/black logo; dark backgrounds
    get the white logo.  No manual override needed.

Carousel slide rendering (Pillow text compositing):
  Three slide types are produced entirely in memory — no Imagen call needed
  for dark text cards:

    make_cover_card(photo_bytes, headline, subtext)
      Full-bleed Imagen photo + gradient overlay + bold headline + swipe cue.
      Used for carousel slide 0 (the hook).

    make_dark_text_card(headline, body, slide_number, brand_name, brand_tagline, size)
      Solid near-black brand card with a giant watermark slide number and
      typeset headline + body.  Used for numbered content slides and the CTA.
      No Imagen call — zero risk of AI-hallucinated text.

    make_photo_text_card(photo_bytes, headline, body, slide_number)
      Imagen photo with a semi-transparent dark strip at the bottom carrying
      a numbered headline + body.  Used for alternating content slides.
"""

from __future__ import annotations

import io
import logging
import os

logger = logging.getLogger(__name__)

_ASSETS_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))

# ── Logo paths ───────────────────────────────────────────────────────────────
_LOGOS_DIR = os.path.join(_ASSETS_ROOT, "logos")
_LOGO_WHITE = os.path.join(_LOGOS_DIR, "final_logo_transparent_allwhite.png")
_LOGO_BLACK = os.path.join(_LOGOS_DIR, "final_logo_transparent_allblack.png")

# Logo layout constants
_LOGO_WIDTH_RATIO = 0.15
_LOGO_MIN_WIDTH = 80
_LOGO_MAX_WIDTH = 160
_PAD = 18
_LIGHT_THRESHOLD = 140
_LOGO_CONTENT_BOX = (264, 386, 758, 623)

# Bar-crop settings (hallucinated solid-colour header/footer detection)
_MAX_BAR_RATIO = 0.22
_MIN_BAR_ROWS = 4
_BAR_VAR_FRACTION = 0.20

# ── Brand font paths (bundled in assets/fonts/) ───────────────────────────────
_FONTS_DIR = os.path.join(_ASSETS_ROOT, "fonts")
_FONT_HEADLINE = os.path.join(_FONTS_DIR, "BigShoulders-Bold.ttf")
_FONT_BODY = os.path.join(_FONTS_DIR, "BricolageGrotesque-Regular.ttf")

# ── Dark card colour palette ──────────────────────────────────────────────────
_CARD_BG = (12, 14, 20)  # near-black, slight blue tint — tech feel
_TEXT_WHITE = (255, 255, 255, 255)
_TEXT_GRAY = (155, 163, 178, 220)  # body copy
_TEXT_NUM = (255, 255, 255, 18)  # giant watermark slide number, barely visible


# ── Internal helpers ──────────────────────────────────────────────────────────


def _load_font(path: str, size: int):
    """Load a TrueType font; fall back to PIL default if unavailable."""
    try:
        from PIL import ImageFont

        return ImageFont.truetype(path, size)
    except Exception:
        from PIL import ImageFont

        return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Word-wrap *text* so each line fits within *max_width* pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _crop_hallucinated_bars(img):
    """Detect and remove solid-colour header/footer bars hallucinated by Imagen.

    Measures per-row pixel variance in grayscale.  A flat-colour bar (text
    header/footer added by Imagen) has very low variance; real photo content
    has high variance.  Consecutive low-variance rows at the top and/or bottom
    are cropped off and the image is resized back to its original dimensions so
    the aspect ratio is preserved.

    Returns the (possibly cropped+resized) image unchanged if no bars are found.
    """
    from PIL import Image

    gray = img.convert("L")
    w, h = img.size
    max_rows = int(h * _MAX_BAR_RATIO)
    sample_step = max(1, w // 64)

    def _row_var(y: int) -> float:
        row = [gray.getpixel((x, y)) for x in range(0, w, sample_step)]
        mean = sum(row) / len(row)
        return sum((p - mean) ** 2 for p in row) / len(row)

    mid_ys = range(h // 3, 2 * h // 3, max(1, h // 30))
    mid_vars = [_row_var(y) for y in mid_ys]
    if not mid_vars:
        return img
    mid_median = sorted(mid_vars)[len(mid_vars) // 2]
    bar_threshold = mid_median * _BAR_VAR_FRACTION

    top_crop = 0
    run = 0
    for y in range(max_rows):
        if _row_var(y) < bar_threshold:
            run += 1
        else:
            break
    if run >= _MIN_BAR_ROWS:
        top_crop = run

    bottom_crop = h
    run = 0
    for y in range(h - 1, h - 1 - max_rows, -1):
        if _row_var(y) < bar_threshold:
            run += 1
        else:
            break
    if run >= _MIN_BAR_ROWS:
        bottom_crop = h - run

    if top_crop == 0 and bottom_crop == h:
        return img

    cropped = img.crop((0, top_crop, w, bottom_crop))
    result = cropped.resize((w, h), Image.LANCZOS)
    logger.info(
        "Cropped hallucinated bars: top=%dpx bottom=%dpx on %dx%d image",
        top_crop,
        h - bottom_crop,
        w,
        h,
    )
    return result


def _quietest_corner(img, logo_w: int, logo_h: int, pad: int) -> tuple[int, int]:
    """Return (x, y) for the corner with the lowest edge density."""
    from PIL import ImageFilter

    width, height = img.size
    candidates = {
        "top_right": (width - logo_w - pad, pad),
        "top_left": (pad, pad),
        "bottom_right": (width - logo_w - pad, height - logo_h - pad),
        "bottom_left": (pad, height - logo_h - pad),
    }
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    best_pos = candidates["top_right"]
    best_score: float = float("inf")
    for pos in candidates.values():
        x, y = pos
        region = edges.crop((x, y, x + logo_w, y + logo_h))
        score = sum(region.getdata())
        if score < best_score:
            best_score = score
            best_pos = pos
    return best_pos


def _corner_is_light(img, x: int, y: int, w: int, h: int) -> bool:
    """Return True if the average greyscale brightness of the region exceeds the threshold."""
    region = img.crop((x, y, x + w, y + h)).convert("L")
    pixels = list(region.getdata())
    return (sum(pixels) / len(pixels)) > _LIGHT_THRESHOLD


# ── Public: brand logo overlay ────────────────────────────────────────────────


def add_brand_overlay(
    image_bytes: bytes,
    brand_name: str = "Brite Tech Lifestyle",
    tagline: str = "",
    bar_opacity: int = 255,
    dark_logo: bool = False,
) -> bytes:
    """Composite the brand logo PNG onto *image_bytes*.

    Scales the logo to ~15% of the image width, places it in whichever corner
    has the least visual activity, then auto-selects white or black logo based
    on the brightness of that corner region.

    Returns PNG bytes with the logo applied.  If Pillow is unavailable or the
    logo file is missing, the original bytes are returned unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; skipping brand overlay")
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = img.size

    img = _crop_hallucinated_bars(img)

    logo_ref_path = _LOGO_WHITE
    if not os.path.exists(logo_ref_path):
        logger.warning("Brand logo not found at %s; skipping overlay", logo_ref_path)
        return image_bytes

    try:
        logo_full_ref = Image.open(logo_ref_path).convert("RGBA")
    except Exception:
        logger.warning("Could not load brand logo; skipping overlay", exc_info=True)
        return image_bytes

    logo_cropped_ref = logo_full_ref.crop(_LOGO_CONTENT_BOX)

    target_w = int(max(_LOGO_MIN_WIDTH, min(_LOGO_MAX_WIDTH, width * _LOGO_WIDTH_RATIO)))
    target_h = int(logo_cropped_ref.height * target_w / logo_cropped_ref.width)

    paste_x, paste_y = _quietest_corner(img, target_w, target_h, _PAD)

    light_bg = _corner_is_light(img, paste_x, paste_y, target_w, target_h)
    logo_path = _LOGO_BLACK if light_bg else _LOGO_WHITE
    logger.debug(
        "Corner brightness: %s → using %s logo",
        "light" if light_bg else "dark",
        "black" if light_bg else "white",
    )

    try:
        logo_full = Image.open(logo_path).convert("RGBA")
    except Exception:
        logger.warning("Could not load brand logo %s; skipping overlay", logo_path, exc_info=True)
        return image_bytes

    logo_cropped = logo_full.crop(_LOGO_CONTENT_BOX)
    logo_scaled = logo_cropped.resize((target_w, target_h), Image.LANCZOS)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.paste(logo_scaled, (paste_x, paste_y), logo_scaled)
    img = Image.alpha_composite(img, overlay)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    logger.debug("Brand overlay applied (%dx%d) at (%d,%d)", width, height, paste_x, paste_y)
    return out.getvalue()


# ── Public: carousel slide renderers ─────────────────────────────────────────


def make_cover_card(photo_bytes: bytes, headline: str, subtext: str = "") -> bytes:
    """Carousel cover slide: full-bleed photo + gradient overlay + bold headline.

    The brand logo is applied separately via ``add_brand_overlay`` — call that
    after this function.  Returns PNG bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; returning photo unchanged")
        return photo_bytes

    img = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
    w, h = img.size

    # Gradient: transparent at top, heavy dark at bottom 70%
    gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_g = ImageDraw.Draw(gradient)
    fade_start = int(h * 0.28)
    for y in range(fade_start, h):
        t = (y - fade_start) / (h - fade_start)
        alpha = int(210 * (t**0.65))
        draw_g.line([(0, y), (w, y)], fill=(10, 12, 18, alpha))
    img = Image.alpha_composite(img, gradient)

    draw = ImageDraw.Draw(img)
    pad = int(w * 0.07)
    max_text_w = int(w * 0.86)

    hl_size = max(44, int(h * 0.088))
    sub_size = max(20, int(h * 0.038))
    cue_size = max(15, int(h * 0.026))

    font_hl = _load_font(_FONT_HEADLINE, hl_size)
    font_sub = _load_font(_FONT_BODY, sub_size)
    font_cue = _load_font(_FONT_BODY, cue_size)

    # Headline starts at ~56% of image height
    text_y = int(h * 0.56)
    line_h = int(hl_size * 1.07)
    for line in _wrap_text(draw, headline.upper(), font_hl, max_text_w):
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += line_h

    text_y += int(sub_size * 0.55)

    if subtext:
        for line in _wrap_text(draw, subtext, font_sub, max_text_w)[:2]:
            draw.text((pad, text_y), line, font=font_sub, fill=(210, 215, 225, 225))
            text_y += int(sub_size * 1.3)

    # Swipe cue — bottom-left
    draw.text(
        (pad, h - cue_size * 2 - pad // 2),
        "swipe to see  →",
        font=font_cue,
        fill=(175, 180, 192, 175),
    )

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def make_dark_text_card(
    headline: str,
    body: str = "",
    slide_number: int | None = None,
    brand_name: str = "",
    brand_tagline: str = "",
    size: tuple[int, int] = (1080, 1080),
) -> bytes:
    """Dark brand card — used for numbered content slides and the CTA slide.

    No Imagen call needed: creates a polished near-black card with a giant
    watermark slide number, bold headline, and body copy in the brand fonts.
    The brand logo is applied separately via ``add_brand_overlay``.
    Returns PNG bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; cannot create dark text card")
        return b""

    w, h = size
    img = Image.new("RGBA", (w, h), (*_CARD_BG, 255))

    # Subtle top-to-transparent vignette so the card isn't completely flat
    grd = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_g = ImageDraw.Draw(grd)
    fade = int(h * 0.35)
    for y in range(fade):
        alpha = int(28 * (1 - y / fade))
        draw_g.line([(0, y), (w, y)], fill=(255, 255, 255, alpha))
    img = Image.alpha_composite(img, grd)

    draw = ImageDraw.Draw(img)
    pad = int(w * 0.08)

    # Thin accent line — top-left corner
    draw.line(
        [(pad, int(h * 0.055)), (int(w * 0.22), int(h * 0.055))],
        fill=(255, 255, 255, 55),
        width=2,
    )

    # Giant watermark slide number (right-aligned, extremely faint)
    if slide_number is not None:
        num_str = f"{slide_number:02d}"
        num_size = max(130, int(h * 0.22))
        font_num = _load_font(_FONT_HEADLINE, num_size)
        bbox = draw.textbbox((0, 0), num_str, font=font_num)
        num_w = bbox[2] - bbox[0]
        draw.text(
            (w - num_w - pad, int(h * 0.03)),
            num_str,
            font=font_num,
            fill=_TEXT_NUM,
        )

    # Headline — centred vertically in the lower half of the card
    hl_size = max(38, int(h * 0.072))
    body_size = max(18, int(h * 0.036))
    tag_size = max(13, int(h * 0.024))

    font_hl = _load_font(_FONT_HEADLINE, hl_size)
    font_body = _load_font(_FONT_BODY, body_size)
    font_tag = _load_font(_FONT_BODY, tag_size)

    max_w = w - 2 * pad
    text_y = int(h * 0.40)

    for line in _wrap_text(draw, headline, font_hl, max_w):
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += int(hl_size * 1.1)

    if body:
        text_y += int(body_size * 0.7)
        for line in _wrap_text(draw, body, font_body, max_w)[:3]:
            draw.text((pad, text_y), line, font=font_body, fill=_TEXT_GRAY)
            text_y += int(body_size * 1.38)

    # Brand tagline at the very bottom
    if brand_tagline:
        draw.text(
            (pad, h - tag_size * 2 - pad // 2),
            brand_tagline,
            font=font_tag,
            fill=(120, 128, 145, 160),
        )

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def make_photo_text_card(
    photo_bytes: bytes,
    headline: str,
    body: str = "",
    slide_number: int | None = None,
) -> bytes:
    """Photo content slide: Imagen background + dark bottom strip with numbered headline.

    The brand logo is applied separately via ``add_brand_overlay``.
    Returns PNG bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; returning photo unchanged")
        return photo_bytes

    img = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
    w, h = img.size

    strip_ratio = 0.36
    strip_h = int(h * strip_ratio)
    strip_top = h - strip_h

    # Semi-transparent dark strip
    strip = Image.new("RGBA", (w, strip_h), (10, 12, 18, 218))
    img.paste(strip, (0, strip_top), strip)

    draw = ImageDraw.Draw(img)
    pad = int(w * 0.06)
    max_w = w - 2 * pad

    hl_size = max(30, int(h * 0.065))
    body_size = max(16, int(h * 0.033))
    num_size = max(22, int(h * 0.045))

    font_hl = _load_font(_FONT_HEADLINE, hl_size)
    font_body = _load_font(_FONT_BODY, body_size)
    font_num = _load_font(_FONT_HEADLINE, num_size)

    text_y = strip_top + int(strip_h * 0.1)

    if slide_number is not None:
        draw.text(
            (pad, text_y),
            f"{slide_number:02d}",
            font=font_num,
            fill=(255, 255, 255, 130),
        )
        text_y += int(num_size * 1.5)

    for line in _wrap_text(draw, headline, font_hl, max_w)[:3]:
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += int(hl_size * 1.08)

    if body:
        text_y += int(body_size * 0.3)
        for line in _wrap_text(draw, body, font_body, max_w)[:2]:
            draw.text((pad, text_y), line, font=font_body, fill=_TEXT_GRAY)
            text_y += int(body_size * 1.35)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
