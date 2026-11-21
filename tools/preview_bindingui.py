# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Draw the input profile's binding_image_points on its binding UI images, as SteamVR places them.

    python tools/preview_bindingui.py [out.png]

SteamVR takes each point in the right image's pixels and mirrors it for the left hand (x -> width - x),
so both hands must show the same picture, mirrored: the left image is the right one with
"transform": "scale(-1,1)", like Valve's Vive wand profile. Sources with a "side" appear on that hand only.
"""

import json
import os
import sys

from PIL import Image, ImageDraw, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, "resources", "input", "cyberfinger_profile.json")


def image_path(entry):
    return os.path.join(ROOT, "resources", entry["image"].replace("{cyberfinger}/", "").replace("/", os.sep))


def panel(profile, side):
    entry = profile["input_bindingui_" + side]
    im = Image.open(image_path(entry)).convert("RGBA")
    if side == "left" and "scale(-1" in entry.get("transform", "").replace(" ", ""):
        im = ImageOps.mirror(im)
    bg = Image.new("RGBA", im.size, (30, 30, 30, 255))
    bg.alpha_composite(im)
    d = ImageDraw.Draw(bg)
    labels = {}
    for path, src in profile["input_source"].items():
        if src.get("side", side) != side or "binding_image_point" not in src:
            continue
        x, y = src["binding_image_point"]
        if side == "left":
            x = im.width - x
        labels.setdefault((x, y), []).append(path.replace("/input/", "").replace("/output/", "out/"))
    for (x, y), names in labels.items():
        d.ellipse([x - 10, y - 10, x + 10, y + 10], outline=(255, 230, 0, 255), width=4)
        text = ", ".join(names)
        tx = x + 14 if side == "right" else x - 14 - 7 * len(text)
        d.text((tx, y - 6), text, fill=(255, 230, 0, 255))
    d.text((10, 10), side, fill=(255, 255, 255, 255))
    return bg


def main(argv):
    out = argv[1] if len(argv) > 1 else os.path.join(ROOT, "out", "bindingui_preview.png")
    with open(PROFILE, encoding="utf-8") as f:
        profile = json.load(f)
    left, right = panel(profile, "left"), panel(profile, "right")
    sheet = Image.new("RGB", (left.width + right.width, max(left.height, right.height)), (30, 30, 30))
    sheet.paste(left.convert("RGB"), (0, 0))
    sheet.paste(right.convert("RGB"), (left.width, 0))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sheet.save(out)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
