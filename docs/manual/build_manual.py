# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Build the CyberFinger instruction manual as a standalone web page.

    python docs/manual/build_manual.py                # docs/manual/build/cyberfinger_manual.html + img/
    python docs/manual/build_manual.py --single-file  # one self-contained HTML file, images embedded

The source is docs/manual/manual.html: the page body (title, styles, content), written for claude.ai's artifact
publisher, which wraps it in a document skeleton. This script adds that skeleton, so the page renders the same when
opened from disk. The two CyberFinger images are resources/icons' (the same pictures SteamVR's binding UI shows; the
callouts in manual.html are placed in their pixel coordinates): manual.html refers to them there, so it shows them
opened as it is, and the built page gets copies in img/ (or embedded). Edit manual.html, then rebuild.
"""

import argparse
import base64
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SOURCE = os.path.join(HERE, "manual.html")
IMAGES = {  # src in manual.html (relative to it) -> name in the built page
    "../../resources/icons/steamvr_cybrfngr_left_transp.png": "img/left.png",
    "../../resources/icons/steamvr_cybrfngr_right_transp.png": "img/right.png",
}

# What the artifact publisher's skeleton provides: charset, viewport, a light colour scheme by default (the page's own
# dark tokens switch it), safe-area padding, no body margin, images that never overflow, and [hidden].
SKELETON_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
  :root { color-scheme: light; padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }
  body { margin: 0; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
"""


def build(out_dir, single_file):
    with open(SOURCE, encoding="utf-8") as f:
        body = f.read()
    # The page body starts with <title>, <link>s and <style> (head material), then the content.
    split = body.index('<div class="page">')
    head, content = body[:split], body[split:]
    for ref, name in IMAGES.items():
        src = os.path.normpath(os.path.join(HERE, ref))
        if not os.path.exists(src):
            sys.exit(f"missing image: {src}")
        if f'src="{ref}"' not in content:
            sys.exit(f"manual.html no longer shows {ref}: update IMAGES")
        if single_file:
            with open(src, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            content = content.replace(f'src="{ref}"', f'src="data:image/png;base64,{data}"')
        else:
            dst = os.path.join(out_dir, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            content = content.replace(f'src="{ref}"', f'src="{name}"')
    # Screenshots and other pictures kept beside the manual, in docs/manual/images: copied (or embedded) as they are.
    for ref in sorted(set(re.findall(r'src="(images/[^"]+)"', content))):
        src = os.path.join(HERE, ref)
        if not os.path.exists(src):
            sys.exit(f"missing image: {src}")
        if single_file:
            kind = "jpeg" if ref.lower().endswith((".jpg", ".jpeg")) else "png"
            with open(src, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            content = content.replace(f'src="{ref}"', f'src="data:image/{kind};base64,{data}"')
        else:
            dst = os.path.join(out_dir, ref)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    html = SKELETON_HEAD + head + "</head>\n<body>\n" + content + "\n</body>\n</html>\n"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "cyberfinger_manual.html")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(HERE, "build"), help="output folder (default: docs/manual/build)")
    ap.add_argument("--single-file", action="store_true", help="embed the images: one self-contained HTML file")
    a = ap.parse_args()
    out = build(a.out, a.single_file)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
