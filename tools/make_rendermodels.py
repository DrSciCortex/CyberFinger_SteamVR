# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Generate the CyberFinger SteamVR render models.

    python tools/make_rendermodels.py

Writes resources/rendermodels/cyberfinger_{left,right}/: a small marker sphere at the wrist and the pose
locators (tip, grip, handgrip, base, openxr_*). The device's /pose/raw follows the Index controller's frame
relative to the hand (the driver's wrist bone uses the Index offset), so the locators are the Index
controller's: apps binding /pose/tip or /pose/grip get the same poses they would with an Index.
"""

import json
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "resources", "rendermodels")

# Index controller locators (valve_controller_knu_1_0_left), mirrored for the right hand below.
LOCATORS_LEFT = {
    "tip":              ([0.006, -0.015, 0.02], [-40.0, -5.0, 0.0]),
    "base":             ([0.004758, -0.037977, 0.200466], [-155.4, -0.427, 7.081]),
    "handgrip":         ([-0.003851, 0.003715, 0.075948], [15.392, -2.071, 0.303]),
    "grip":             ([0.0, -0.015, 0.13], [15.392, -2.071, 0.303]),
    "openxr_handmodel": ([-0.015, -0.015, 0.13], [-40.0, -5.0, 0.0]),
    "openxr_pinch":     ([0.05, 0.006, 0.0675], [-70.0, -15.0, 35.0]),
    "openxr_poke":      ([-0.04, -0.0575, 0.0039], [-60.0, 0.0, 0.0]),
}
WRIST_LEFT = [-0.034038, 0.036503, 0.164722]   # raw → wrist offset (SkeletonSynth wrist bone)
RADIUS = 0.012
COLOR = (230, 0, 126)                           # CyberFinger pink


def mirror(origin, rot):
    """Left → right hand: negate X of the position and the Y/Z rotations."""
    return [-origin[0], origin[1], origin[2]], [rot[0], -rot[1], -rot[2]]


def sphere_obj(center, radius, rings=8, segments=12):
    v, vt, vn, f = [], [], [], []
    for i in range(rings + 1):
        th = math.pi * i / rings
        for j in range(segments + 1):
            ph = 2 * math.pi * j / segments
            n = (math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph))
            v.append(tuple(center[k] + radius * n[k] for k in range(3)))
            vn.append(n)
            vt.append((j / segments, 1 - i / rings))
    row = segments + 1
    for i in range(rings):
        for j in range(segments):
            a, b = i * row + j + 1, i * row + j + 2
            c, d = (i + 1) * row + j + 1, (i + 1) * row + j + 2
            f.append((a, c, b))
            f.append((b, c, d))
    lines = ["# CyberFinger wrist marker", "mtllib body.mtl", "o body"]
    lines += [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in v]
    lines += [f"vt {s:.4f} {t:.4f}" for s, t in vt]
    lines += [f"vn {x:.4f} {y:.4f} {z:.4f}" for x, y, z in vn]
    lines += ["usemtl body"]
    lines += ["f " + " ".join(f"{k}/{k}/{k}" for k in tri) for tri in f]
    return "\n".join(lines) + "\n"


def png(width, height, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    for side in ("left", "right"):
        name = f"cyberfinger_{side}"
        folder = os.path.join(OUT, name)
        os.makedirs(folder, exist_ok=True)
        wrist = WRIST_LEFT if side == "left" else [-WRIST_LEFT[0], WRIST_LEFT[1], WRIST_LEFT[2]]
        components = {"body": {"filename": "body.obj"}}
        for loc, (origin, rot) in LOCATORS_LEFT.items():
            if side == "right":
                origin, rot = mirror(origin, rot)
            components[loc] = {"component_local": {"origin": origin, "rotate_xyz": rot}}
        with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8", newline="\n") as fp:
            json.dump({"components": components}, fp, indent=4)
            fp.write("\n")
        with open(os.path.join(folder, "body.obj"), "w", encoding="utf-8", newline="\n") as fp:
            fp.write(sphere_obj(wrist, RADIUS))
        with open(os.path.join(folder, "body.mtl"), "w", encoding="utf-8", newline="\n") as fp:
            fp.write("newmtl body\nKa 1.0 1.0 1.0\nKd 1.0 1.0 1.0\nKs 0.3 0.3 0.3\nNs 64\nd 1.0\nillum 2\nmap_Kd body.png\n")
        with open(os.path.join(folder, "body.png"), "wb") as fp:
            fp.write(png(4, 4, COLOR))
        print("wrote", os.path.relpath(folder, os.path.dirname(HERE)))


if __name__ == "__main__":
    main()
