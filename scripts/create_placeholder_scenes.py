"""Create synthetic placeholder scene images for testing the cover_image pipeline.

Run once to generate test scenes so carousel cover detection can be verified
without the real photography assets:

    python scripts/create_placeholder_scenes.py

Each placeholder is a 960×1280 (portrait) image with a realistic black rectangle
representing a laptop screen, placed in the position and at the scale typical of
the real scene photos.  Replace with the real scene PNGs when available.

Real scene filenames (drop into assets/scenes/):
  scene1_library.png    — library desk with tea mug, bookshelf backdrop
  scene2_cafe_iced.png  — industrial café, iced coffee, plants
  scene3_woman.png      — woman with coffee cup, white-screen laptop in café
  scene4_cafe_latte.png — wooden café table with latte, phone, smartwatch
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PIL import Image, ImageDraw

_SCENES_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "scenes")

# Each entry: (filename, bg_color, screen_xywh, screen_is_white, label)
_SCENE_SPECS = [
    (
        "scene1_library.png",
        (210, 195, 178),          # warm library beige
        (180, 280, 600, 370),     # (x, y, w, h) of screen inside 960×1280
        False,                    # black screen
        "Library",
    ),
    (
        "scene2_cafe_iced.png",
        (190, 185, 175),          # concrete grey café
        (160, 260, 640, 400),
        False,
        "Café (iced)",
    ),
    (
        "scene3_woman.png",
        (170, 145, 120),          # warm café brown
        (530, 320, 400, 280),     # screen on right side (woman is on left)
        True,                     # white screen
        "Woman (white screen)",
    ),
    (
        "scene4_cafe_latte.png",
        (130, 100, 75),           # dark wooden café
        (140, 250, 620, 390),
        False,
        "Café (latte)",
    ),
]

IMG_W, IMG_H = 960, 1280


def _make_scene(bg: tuple, screen_xywh: tuple, is_white: bool, label: str) -> Image.Image:
    img = Image.new("RGB", (IMG_W, IMG_H), bg)
    draw = ImageDraw.Draw(img)

    # Simple background detail (desk surface)
    desk_y = IMG_H - 300
    desk_color = tuple(max(0, c - 30) for c in bg)
    draw.rectangle([(0, desk_y), (IMG_W, IMG_H)], fill=desk_color)

    # Laptop body (dark silver rectangle behind screen)
    sx, sy, sw, sh = screen_xywh
    body_pad = 30
    body_color = (80, 85, 90)
    draw.rounded_rectangle(
        [(sx - body_pad, sy - body_pad), (sx + sw + body_pad, sy + sh + body_pad + 40)],
        radius=8, fill=body_color,
    )

    # Laptop screen (the region cover_image.py will detect)
    screen_color = (255, 255, 255) if is_white else (3, 3, 6)
    draw.rectangle([(sx, sy), (sx + sw, sy + sh)], fill=screen_color)

    # Label (so test images are visually identifiable)
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((sx + 10, sy + 10), f"[PLACEHOLDER — {label}]",
              fill=(200, 200, 200) if not is_white else (60, 60, 60), font=font)

    return img


def main():
    os.makedirs(_SCENES_DIR, exist_ok=True)
    for filename, bg, xywh, is_white, label in _SCENE_SPECS:
        path = os.path.join(_SCENES_DIR, filename)
        img = _make_scene(bg, xywh, is_white, label)
        img.save(path, format="PNG", optimize=True)
        print(f"Created {path}")
    print("\nDone. Replace these placeholders with the real scene photos when ready.")


if __name__ == "__main__":
    main()
