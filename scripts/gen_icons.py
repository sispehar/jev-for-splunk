#!/usr/bin/env python3
"""Draw the app icons: a white "J" on the nav colour, from shapes only (no font files).

    uv run --no-project --with pillow python scripts/gen_icons.py

Writes jev_for_splunk/static/appIcon*.png, the sizes Splunk Web reads. Replace
them with a designed logo whenever there is one; nothing else depends on this
script.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATIC = os.path.join(REPO, "jev_for_splunk", "static")
TEAL = (40, 182, 164, 255)      # #28b6a4, the nav colour in default/data/ui/nav/default.xml
WHITE = (255, 255, 255, 255)
MASTER = 1024


def _mark(background, ink):
    """A rounded square holding a geometric J: a stem, a bowl and a top bar."""
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, MASTER - 1, MASTER - 1], radius=int(MASTER * 0.22), fill=background)
    stroke = int(MASTER * 0.14)
    stem_right = int(MASTER * 0.68)
    stem_left = stem_right - stroke
    top = int(MASTER * 0.20)
    bowl_bottom = int(MASTER * 0.80)
    bowl_left = int(MASTER * 0.26)
    # top bar, then the stem down to where the bowl starts
    draw.rectangle([int(MASTER * 0.40), top, stem_right, top + stroke], fill=ink)
    bowl_top = bowl_bottom - (stem_right - bowl_left)
    center_y = (bowl_top + bowl_bottom) // 2
    draw.rectangle([stem_left, top, stem_right, center_y], fill=ink)
    # the bowl: the lower half of a ring from the stem round to the left
    draw.arc([bowl_left, bowl_top, stem_right, bowl_bottom], start=0, end=180, fill=ink, width=stroke)
    # a short upturn on the left end of the bowl
    draw.rectangle([bowl_left, center_y - int(stroke * 0.6), bowl_left + stroke, center_y], fill=ink)
    return image


def _save(image, path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.resize((size, size), Image.LANCZOS).save(path, optimize=True)
    print("wrote %s (%dx%d)" % (os.path.relpath(path, REPO), size, size))


def main():
    icon = _mark(TEAL, WHITE)
    alt = _mark(WHITE, TEAL)   # for dark backgrounds
    _save(icon, os.path.join(STATIC, "appIcon.png"), 36)
    _save(icon, os.path.join(STATIC, "appIcon_2x.png"), 72)
    _save(alt, os.path.join(STATIC, "appIconAlt.png"), 36)
    _save(alt, os.path.join(STATIC, "appIconAlt_2x.png"), 72)


if __name__ == "__main__":
    main()
