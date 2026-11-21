# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Cut the SteamVR status icons out of the icon sheet.

    python tools/make_status_icons.py [images/steamvr_status_icons_sheet.png]

The sheet is icons on black: row 1 grey (inactive), row 2 the same icons blue (active). Column 2 (the OK sign)
becomes the device's status icons: black turns transparent, the hand drawn is the left one and is mirrored for
the right. Writes resources/icons/cyberfinger_{left,right}_{off,ready}.png at 32x32 (what SteamVR asks of
controllers) and _2x at 64x64, as Valve's Index controller icons do. The driver sets them in Activate.
"""

import os
import sys

from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHEET = os.path.join(ROOT, "images", "steamvr_status_icons_sheet.png")
OUT = os.path.join(ROOT, "resources", "icons")

# The cells on the sheet (x0, y0, x1, y1): column 2, rows 1 and 2.
CELLS = {"off": (140, 85, 270, 275), "ready": (140, 290, 270, 480)}
SIZES = {"": 32, "_2x": 64}
MARGIN = 0.04           # of the icon's side, each way
NOISE, OPAQUE = 12, 70  # brightest channel: below NOISE transparent, from OPAQUE opaque


def black_to_alpha(im):
    """RGB on black -> RGBA: brightness is coverage near the edges, colour un-premultiplied."""
    out = Image.new("RGBA", im.size)
    src, dst = im.load(), out.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = src[x, y]
            a = min(1.0, max(0.0, (max(r, g, b) - NOISE) / (OPAQUE - NOISE)))
            if a <= 0:
                dst[x, y] = (0, 0, 0, 0)
                continue
            k = 1.0 / a if a < 1 else 1.0
            dst[x, y] = (min(255, round(r * k)), min(255, round(g * k)), min(255, round(b * k)), round(255 * a))
    return out


def main(argv):
    sheet = Image.open(argv[1] if len(argv) > 1 else SHEET).convert("RGB")
    icons = {name: black_to_alpha(sheet.crop(box)) for name, box in CELLS.items()}
    # One square for both, so grey and blue line up when SteamVR swaps them.
    boxes = [im.getbbox() for im in icons.values()]
    w = max(b[2] - b[0] for b in boxes)
    h = max(b[3] - b[1] for b in boxes)
    side = round(max(w, h) * (1 + 2 * MARGIN))
    for name, im in icons.items():
        l, t, r, b = im.getbbox()
        square = Image.new("RGBA", (side, side))
        square.paste(im.crop((l, t, r, b)), ((side - (r - l)) // 2, (side - (b - t)) // 2))
        for hand, pic in (("left", square), ("right", ImageOps.mirror(square))):
            for suffix, size in SIZES.items():
                # Resize premultiplied, or the transparent black bleeds into the edges.
                small = pic.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")
                path = os.path.join(OUT, f"cyberfinger_{hand}_{name}{suffix}.png")
                small.save(path)
                print("wrote", os.path.relpath(path, ROOT))


if __name__ == "__main__":
    main(sys.argv)
