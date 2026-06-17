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
import urllib.request

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
# Brand-kit typefaces (see britetechlifestyle brand kit):
#   • Headline/hero — the kit's embedded display bold (bundled as BriteHero-Bold).
#   • Body          — Figtree (variable, default Light 300 — the kit's body weight).
#   • Tagline       — Playfair Display Italic (the kit's italic accent / banner tagline).
# Google Fonts download URLs are a fallback if the bundled TTFs are ever missing
# from the deployment container (should not normally happen — files are in git).
_FONT_URLS = {
    "Figtree-Regular.ttf": (
        "https://github.com/google/fonts/raw/main/ofl/figtree/Figtree%5Bwght%5D.ttf"
    ),
    "PlayfairDisplay-Italic.ttf": (
        "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/"
        "PlayfairDisplay-Italic%5Bwght%5D.ttf"
    ),
}
_FONT_HEADLINE = os.path.join(_FONTS_DIR, "BriteHero-Bold.ttf")
_FONT_BODY = os.path.join(_FONTS_DIR, "Figtree-Regular.ttf")
_FONT_TAGLINE = os.path.join(_FONTS_DIR, "PlayfairDisplay-Italic.ttf")

# ── Dark card colour palette (brand kit) ──────────────────────────────────────
# Dark brand layouts (platform banners / cards) use pure black per the kit.
_CARD_BG = (0, 0, 0)  # #000000 — brand black
_TEXT_WHITE = (255, 255, 255, 255)  # #FFFFFF headline
_TEXT_GRAY = (161, 161, 166, 235)  # #A1A1A6 silver — body copy on dark
_TEXT_NUM = (255, 255, 255, 18)  # giant watermark slide number, barely visible


# ── Internal helpers ──────────────────────────────────────────────────────────


def _load_font(path: str, size: int):
    """Load a TrueType font at *size* pts.

    Resolution order:
    1. Load from the bundled path (assets/fonts/).
    2. If that fails, attempt a one-time download from Google Fonts and cache
       the file locally so subsequent calls don't re-download.
    3. If all else fails, return PIL's scaled default font at the requested
       size (much better than load_default() which is always 10px bitmap).
    """
    from PIL import ImageFont

    # Fast path — bundled file is present and readable.
    if os.path.isfile(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception as exc:
            logger.warning("TTF load failed for %s at size %d: %s", path, size, exc)

    # Slow path — try to download the font once.
    filename = os.path.basename(path)
    url = _FONT_URLS.get(filename)
    if url:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            logger.info("Downloading missing font %s from Google Fonts…", filename)
            urllib.request.urlretrieve(url, path)
            return ImageFont.truetype(path, size)
        except Exception as exc:
            logger.warning("Font download failed for %s: %s", filename, exc)

    # Last resort — PIL's FreeType default scaled to *size* (readable, at least).
    logger.error("Using PIL fallback font for %s — carousel text will not use brand typeface", path)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
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


def _fit_lines(
    draw,
    text: str,
    path: str,
    base_size: int,
    min_size: int,
    max_width: int,
    max_lines: int | None = None,
):
    """Wrap *text* and shrink the font until it fits within *max_lines*.

    Returns ``(font, lines, size)``. The font is shrunk (never below
    *min_size*) until the wrapped text needs no more than *max_lines* lines, so
    headlines and body copy always fit the card instead of running off the
    edge. If it still overflows at *min_size*, the surplus is trimmed and the
    last visible line gets an ellipsis rather than being silently cut by the
    image border.
    """
    size = max(min_size, base_size)
    font = _load_font(path, size)
    lines = _wrap_text(draw, text, font, max_width)
    while max_lines is not None and len(lines) > max_lines and size > min_size:
        size = max(min_size, size - 2)
        font = _load_font(path, size)
        lines = _wrap_text(draw, text, font, max_width)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        lines[-1] = (last[:-1] if last.endswith("…") else last) + "…"
    return font, lines, size


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
    corner: str | None = None,
    crop_bars: bool = True,
    logo_scale: float = 1.0,
) -> bytes:
    """Composite the brand logo PNG onto *image_bytes*.

    Scales the logo to ~15% of the image width, places it in whichever corner
    has the least visual activity, then auto-selects white or black logo based
    on the brightness of that corner region.

    Pass ``corner`` (one of "top_right", "top_left", "bottom_right",
    "bottom_left") to force a fixed placement instead of auto-detection. The
    carousel renderer uses this so the logo never lands on top of the slide
    text, whose position is known in advance. The white/black logo colour is
    still chosen automatically from the chosen corner's brightness.

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

    # Hallucinated-bar cropping is only meaningful for AI-generated photos.
    # Deterministic text cards have uniform dark margins that this would
    # mistake for bars — cropping + rescaling then clips the tagline — so
    # callers rendering brand text cards pass crop_bars=False.
    if crop_bars:
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

    # logo_scale lets specific renderers (e.g. carousels) enlarge the logo so the
    # thin "TECH LIFESTYLE" subtitle stays crisp instead of anti-aliasing to grey.
    target_w = int(
        max(_LOGO_MIN_WIDTH, min(_LOGO_MAX_WIDTH, width * _LOGO_WIDTH_RATIO)) * logo_scale
    )
    target_h = int(logo_cropped_ref.height * target_w / logo_cropped_ref.width)

    if corner in {"top_right", "top_left", "bottom_right", "bottom_left"}:
        fixed = {
            "top_right": (width - target_w - _PAD, _PAD),
            "top_left": (_PAD, _PAD),
            "bottom_right": (width - target_w - _PAD, height - target_h - _PAD),
            "bottom_left": (_PAD, height - target_h - _PAD),
        }
        paste_x, paste_y = fixed[corner]
    else:
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
    cue_size = max(15, int(h * 0.026))
    font_cue = _load_font(_FONT_BODY, cue_size)

    # Fit headline and subtext so they never run off the bottom of the image.
    font_hl, hl_lines, hl_sz = _fit_lines(
        draw,
        headline.upper(),
        _FONT_HEADLINE,
        max(44, int(h * 0.088)),
        max(30, int(h * 0.05)),
        max_text_w,
        max_lines=3,
    )
    hl_line_h = int(hl_sz * 1.07)

    font_sub = None
    sub_lines: list[str] = []
    sub_line_h = 0
    sub_sz = 0
    if subtext:
        font_sub, sub_lines, sub_sz = _fit_lines(
            draw,
            subtext,
            _FONT_BODY,
            max(20, int(h * 0.038)),
            max(15, int(h * 0.026)),
            max_text_w,
            max_lines=3,
        )
        sub_line_h = int(sub_sz * 1.3)

    gap = int(sub_sz * 0.55) if sub_lines else 0
    block_h = hl_line_h * len(hl_lines) + gap + sub_line_h * len(sub_lines)

    # Bottom-anchor the text block just above the swipe cue so it can never
    # overflow the image, but never let it climb above ~30% of the height.
    bottom_limit = h - pad - cue_size * 2
    text_y = max(bottom_limit - block_h, int(h * 0.30))

    for line in hl_lines:
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += hl_line_h

    if sub_lines:
        text_y += gap
        for line in sub_lines:
            draw.text((pad, text_y), line, font=font_sub, fill=(210, 215, 225, 225))
            text_y += sub_line_h

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

    # Thin accent line — top-left corner. Composited on its own transparent
    # layer so its alpha survives the final convert("RGB") flatten.
    line_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(line_layer).line(
        [(pad, int(h * 0.06)), (int(w * 0.20), int(h * 0.06))],
        fill=(255, 255, 255, 70),
        width=2,
    )
    img = Image.alpha_composite(img, line_layer)
    draw = ImageDraw.Draw(img)

    # Giant watermark slide number — faint, lower-LEFT so it never collides with
    # the brand logo (which is composited top-right). Drawn on its own
    # transparent layer: drawing low-alpha text straight onto the card and then
    # convert("RGB") would DROP the alpha and render it solid white (the cause
    # of the over-bright "01" overlapping the logo).
    if slide_number is not None:
        num_str = f"{slide_number:02d}"
        num_size = max(110, int(h * 0.16))
        font_num = _load_font(_FONT_HEADLINE, num_size)
        nbbox = draw.textbbox((0, 0), num_str, font=font_num)
        num_h = nbbox[3] - nbbox[1]
        num_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(num_layer).text(
            (pad, int(h * 0.30) - num_h),
            num_str,
            font=font_num,
            fill=(255, 255, 255, 28),
        )
        img = Image.alpha_composite(img, num_layer)
        draw = ImageDraw.Draw(img)

    # Headline — sits in the lower half of the card, auto-fitted so the
    # headline and body never collide with the tagline or run off the card.
    # Tagline uses Playfair Display Italic (brand accent face); size it a touch
    # larger since serif italics read smaller than the sans body.
    tag_size = max(15, int(h * 0.028))
    font_tag = _load_font(_FONT_TAGLINE, tag_size)

    max_w = w - 2 * pad

    font_hl, hl_lines, hl_sz = _fit_lines(
        draw,
        headline,
        _FONT_HEADLINE,
        max(38, int(h * 0.072)),
        max(28, int(h * 0.045)),
        max_w,
        max_lines=4,
    )
    hl_line_h = int(hl_sz * 1.1)

    font_body = None
    body_lines: list[str] = []
    body_line_h = 0
    body_sz = 0
    if body:
        font_body, body_lines, body_sz = _fit_lines(
            draw,
            body,
            _FONT_BODY,
            max(18, int(h * 0.036)),
            max(14, int(h * 0.026)),
            max_w,
            max_lines=4,
        )
        body_line_h = int(body_sz * 1.38)

    text_y = int(h * 0.40)
    for line in hl_lines:
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += hl_line_h

    if body_lines:
        text_y += int(body_sz * 0.7)
        for line in body_lines:
            draw.text((pad, text_y), line, font=font_body, fill=_TEXT_GRAY)
            text_y += body_line_h

    # Brand tagline — positioned so its lowest ink (including descenders) sits a
    # clear margin above the bottom edge. textbbox[3] is the bottom extent from
    # the draw origin, so y = h - margin - bbox[3] guarantees no clipping.
    if brand_tagline:
        tbbox = draw.textbbox((0, 0), brand_tagline, font=font_tag)
        margin = int(h * 0.05)
        draw.text(
            (pad, h - margin - tbbox[3]),
            brand_tagline,
            font=font_tag,
            fill=(120, 128, 145),
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

    draw = ImageDraw.Draw(img)
    pad = int(w * 0.06)
    max_w = w - 2 * pad

    num_size = max(22, int(h * 0.045))
    font_num = _load_font(_FONT_HEADLINE, num_size)
    num_h = int(num_size * 1.5) if slide_number is not None else 0

    # Fit headline + body first so the strip can be sized to actually cover them.
    font_hl, hl_lines, hl_sz = _fit_lines(
        draw,
        headline,
        _FONT_HEADLINE,
        max(30, int(h * 0.065)),
        max(22, int(h * 0.045)),
        max_w,
        max_lines=3,
    )
    hl_line_h = int(hl_sz * 1.08)

    font_body = None
    body_lines: list[str] = []
    body_line_h = 0
    body_sz = 0
    if body:
        font_body, body_lines, body_sz = _fit_lines(
            draw,
            body,
            _FONT_BODY,
            max(16, int(h * 0.033)),
            max(13, int(h * 0.026)),
            max_w,
            max_lines=3,
        )
        body_line_h = int(body_sz * 1.35)

    gap = int(body_sz * 0.3) if body_lines else 0
    content_h = num_h + hl_line_h * len(hl_lines) + gap + body_line_h * len(body_lines)

    # Strip auto-sizes to the content (capped at 55% of the image) so the text
    # is always fully covered and never clipped by the image edge.
    strip_pad = int(h * 0.05)
    strip_h = min(int(h * 0.55), content_h + 2 * strip_pad)
    strip_top = h - strip_h

    strip = Image.new("RGBA", (w, strip_h), (10, 12, 18, 218))
    img.paste(strip, (0, strip_top), strip)

    text_y = strip_top + strip_pad

    if slide_number is not None:
        draw.text(
            (pad, text_y),
            f"{slide_number:02d}",
            font=font_num,
            fill=(255, 255, 255, 130),
        )
        text_y += num_h

    for line in hl_lines:
        draw.text((pad, text_y), line, font=font_hl, fill=_TEXT_WHITE)
        text_y += hl_line_h

    if body_lines:
        text_y += gap
        for line in body_lines:
            draw.text((pad, text_y), line, font=font_body, fill=_TEXT_GRAY)
            text_y += body_line_h

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
