# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Ray-cast per-finger occlusion / confidence (proposal Task 1.2).

The Quest's own per-joint validity flags are coarse (whole-hand), so we compute per-joint
visibility GEOMETRICALLY: cast a ray from the HMD camera to each hand joint and test whether the
hand's OWN geometry (bones as capsules, a thicker palm/knuckle ridge) blocks the line of sight
before the ray reaches the joint (Graf & Barthet 2023, ray-cast self-occlusion). A joint whose ray
is blocked is one the camera can't actually see, so its optical position is unreliable.

This gives per-finger confidence — the quality filter Task 3's rolling buffer needs so it only
learns from optical finger poses the camera genuinely saw. It also principally explains the
"Quest can't label a closed fist": in a fist the curled fingertips sit behind the knuckle ridge.

Pure numpy, no GUI/torch. `per_joint_visibility(P, cam)` → (26,) in [0,1]; `finger_confidence` → (5,).
"""

import numpy as np

# 26-joint layout shared with the rest of the bridge: 0 root, 1 wrist, five finger chains, tips 5/10/15/20/25.
SKELETON_CHAINS = ((1, 2, 3, 4, 5), (1, 6, 7, 8, 9, 10), (1, 11, 12, 13, 14, 15),
                   (1, 16, 17, 18, 19, 20), (1, 21, 22, 23, 24, 25))
TIPS = (5, 10, 15, 20, 25)
MCP = (6, 11, 16, 21)                       # the four finger metacarpal heads (knuckle ridge)


def _bones():
    """List of (a, b, is_palm) occluder capsules: every skeletal bone, plus knuckle-ridge cross
    links between adjacent metacarpal heads (they occlude a curled fist's fingertips)."""
    bones = []
    for ch in SKELETON_CHAINS:
        for i in range(1, len(ch)):
            is_palm = (i == 1)              # wrist→metacarpal spans the palm → thicker
            bones.append((ch[i - 1], ch[i], is_palm))
    for a, b in ((6, 11), (11, 16), (16, 21)):   # knuckle ridge across the palm
        bones.append((a, b, True))
    return bones


_BONES = _bones()


def _seg_seg(p1, q1, p2, q2):
    """Closest distance between segments [p1,q1] and [p2,q2]; also returns s = param on segment 1
    (the camera→joint ray), 0 at the camera, 1 at the joint. Ericson, Real-Time Collision Detection."""
    d1 = q1 - p1
    d2 = q2 - p2
    r = p1 - p2
    a = float(d1 @ d1)
    e = float(d2 @ d2)
    f = float(d2 @ r)
    eps = 1e-12
    if a <= eps and e <= eps:
        return float(np.linalg.norm(p1 - p2)), 0.0
    if a <= eps:
        s, t = 0.0, min(1.0, max(0.0, f / e))
    else:
        c = float(d1 @ r)
        if e <= eps:
            t = 0.0
            s = min(1.0, max(0.0, -c / a))
        else:
            b = float(d1 @ d2)
            denom = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / denom)) if denom > eps else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))
    cp1 = p1 + s * d1
    cp2 = p2 + t * d2
    return float(np.linalg.norm(cp1 - cp2)), s


def per_joint_visibility(P, cam, r_finger=0.010, r_palm=0.018, s_max=0.95):
    """P: (26,3) world joints (metres). cam: (3,) HMD position. Returns vis (26,) in [0,1] —
    1 = the camera clearly sees the joint, 0 = fully self-occluded. A bone occludes a joint when
    the camera→joint ray passes within the bone's radius at a point IN FRONT of the joint
    (s < s_max excludes only the sliver right at the joint), and never its own incident bones.
    Occlusion strength = how deeply the ray penetrates the bone (1 − dist/radius)."""
    P = np.asarray(P, float)
    cam = np.asarray(cam, float)
    vis = np.ones(26)
    for j in range(2, 26):                          # skip root(0); wrist(1) stays visible
        Pj = P[j]
        worst = 0.0
        for (a, b, is_palm) in _BONES:
            if j == a or j == b:                    # a joint is never occluded by a bone it's on
                continue
            r = r_palm if is_palm else r_finger
            d, s = _seg_seg(cam, Pj, P[a], P[b])
            if s < s_max and d < r:                 # blocker sits between camera and joint
                worst = max(worst, 1.0 - d / r)     # deeper into the bone → more occluded
        vis[j] = max(0.0, 1.0 - worst)
    return vis


def finger_confidence(vis):
    """Per-finger optical confidence (5,) from per-joint visibility, weighted toward the fingertip
    (the joint fusion cares about most and the first to be self-occluded)."""
    vis = np.asarray(vis, float)
    out = []
    for ch in SKELETON_CHAINS:
        js = ch[2:]                                 # movable joints (skip wrist + metacarpal)
        w = np.linspace(1.0, 2.0, len(js))          # weight distal joints more
        out.append(float((vis[list(js)] * w).sum() / w.sum()))
    return np.array(out)


if __name__ == "__main__":
    import math

    def _roty(a):                                     # rotate about +y: +x → −z for a > 0 (curl to palm)
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])

    def hand(curl):
        """Synthetic right hand, wrist at origin. Extended fingers along +x, spread in y, back of
        hand +z / palm −z. `curl` bends each joint a BOUNDED amount so the fingertip curls into the
        palm (−z) under the knuckle ridge — a realistic fist, not an over-rotation."""
        P = np.zeros((26, 3))
        P[1] = [0, 0, 0]
        maxa = [50.0, 72.0, 55.0]                     # MCP, PIP, DIP bend (deg) at full curl
        for fi, ch in enumerate(SKELETON_CHAINS):
            base_y = -0.05 if fi == 0 else (fi - 2) * 0.020
            P[ch[1]] = np.array([0.02, base_y, 0.0])
            for k in range(2, len(ch)):
                P[ch[k]] = P[ch[k - 1]] + np.array([0.022, 0.0, 0.0])
            for pos in range(2, len(ch) - 1):         # curl about +y at each interior joint
                ang = math.radians(curl * maxa[min(pos - 2, 2)])
                piv = P[ch[pos]].copy()
                R = _roty(ang)
                for j in range(pos + 1, len(ch)):
                    P[ch[j]] = piv + R @ (P[ch[j]] - piv)
        return P

    # direct primitive check: a joint straight behind a bar must read occluded
    d, s = _seg_seg(np.array([0, 0, 0.35]), np.array([0, 0, -0.1]),
                    np.array([-0.05, 0, 0]), np.array([0.05, 0, 0.]))
    print(f"primitive: ray through a bar → dist {d:.3f} (want ~0), s {s:.2f} (want ~0.78, in front)")

    cam = np.array([0.0, 0.0, 0.35])                  # camera on the BACK-of-hand side (+z)
    for name, curl in (("OPEN", 0.0), ("FIST", 1.0)):
        P = hand(curl)
        vis = per_joint_visibility(P, cam)
        fc = finger_confidence(vis)
        tips = vis[list(TIPS)]
        print(f"{name}: fingertip z {np.round(P[list(TIPS),2],3)}  tip-vis {np.round(tips,2)} "
              f"mean {tips.mean():.2f}  finger-conf {np.round(fc,2)}")
    print("expect: OPEN tips visible (~1), FIST tips occluded (low)")
