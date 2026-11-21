# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""What the bridge GUIs draw of a hand (Tk canvas), shared by the Windows and the Linux bridge.

  the palette              COLOR_*
  draw_triad               one IMU's orientation: its axes against a horizon ring
  SkeletonRenderer         one hand's tracked skeleton and its position dome, into a box of a canvas
  StreamView, StreamHand   stream mode: both CyberFingers at work, filling the window

The GUIs differ in their font and in where the skeletons come from (SteamVR through OpenVR; the WiVRn driver's
status stream), so the font is passed in and the skeletons arrive as plain tuples. The hand states are the GUIs'
HandState objects.
"""

import math
import time
import tkinter as tk
from tkinter import ttk

import cyberfinger_report
from cf_protocol import BTN_TRIGGER, BTN_GRIP, BTN_C, BTN_D, BTN_E, BTN_MENU, BTN_STICK, BTN_STSEL
from haptics_view import draw_haptic_meter

# Brand colors
COLOR_BG       = "#1a1a1a"
COLOR_BG2      = "#242424"
COLOR_BG3      = "#2e2e2e"
COLOR_FG       = "#e0e0e0"
COLOR_FG_DIM   = "#888888"
COLOR_ACCENT   = "#e6007e"  # CyberFinger pink
COLOR_ACCENT2  = "#ff2d9b"
COLOR_GREEN    = "#00e676"
COLOR_RED      = "#ff1744"
COLOR_ORANGE   = "#ff9100"
COLOR_BLUE     = "#448aff"


def blend(c1, c2, t):
    """Blend two #rrggbb colors; t=0 → c1, t=1 → c2."""
    t = max(0.0, min(1.0, t))
    a, b = int(c1[1:], 16), int(c2[1:], 16)
    parts = []
    for shift in (16, 8, 0):
        va = (a >> shift) & 255
        vb = (b >> shift) & 255
        parts.append(int(va + (vb - va) * t))
    return "#%02x%02x%02x" % tuple(parts)


# ── Minimal 3D helpers (orthographic, no external deps) ──────────────────

# Fixed camera angles — a three-quarter view so all three axes stay distinct.
_VIEW_YAW   = math.radians(35.0)
_VIEW_PITCH = math.radians(20.0)


def quat_to_matrix(q):
    """Unit quaternion (w, x, y, z) → 3x3 rotation matrix as row tuples."""
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = w / n, x / n, y / n, z / n
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z),       2.0 * (x * z + w * y)),
        (2.0 * (x * y + w * z),       1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
        (2.0 * (x * z - w * y),       2.0 * (y * z + w * x),       1.0 - 2.0 * (x * x + y * y)),
    )


def quat_to_euler_deg(q):
    """Unit quaternion (w, x, y, z) → (roll, pitch, yaw) in degrees."""
    w, x, y, z = q

    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Clamp guards against gimbal-lock inputs drifting just past ±1.
    sinp = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sinp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def rotate_vec(m, v):
    """Apply a 3x3 row-major matrix to a 3-vector."""
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def project(v, cx, cy, scale):
    """World point → (screen_x, screen_y, depth). Larger depth is nearer."""
    x, y, z = v

    # Yaw about world Y, then pitch about the camera's X.
    cyaw, syaw = math.cos(_VIEW_YAW), math.sin(_VIEW_YAW)
    xe = x * cyaw - z * syaw
    ze = x * syaw + z * cyaw

    cp, sp = math.cos(_VIEW_PITCH), math.sin(_VIEW_PITCH)
    ye = y * cp - ze * sp
    depth = y * sp + ze * cp

    # Screen y is inverted so +Y points up on the canvas.
    return (cx + xe * scale, cy - ye * scale, depth)


def draw_triad(c, cx, cy, scale, quat, font):
    """Render one orientation as an XYZ axis triad against a horizon ring."""
    m = quat_to_matrix(quat)

    # Reference ground ring so rotation reads against a fixed horizon.
    ring = []
    for i in range(32):
        a = 2.0 * math.pi * i / 32
        px, py, _ = project((math.cos(a), 0.0, math.sin(a)), cx, cy, scale)
        ring.extend((px, py))
    c.create_polygon(ring, outline=COLOR_BG3, fill="", width=1)

    axes = [
        ((1.0, 0.0, 0.0), COLOR_RED,   "X"),
        ((0.0, 1.0, 0.0), COLOR_GREEN, "Y"),
        ((0.0, 0.0, 1.0), COLOR_BLUE,  "Z"),
    ]

    # Paint far-to-near so nearer arms overlap correctly.
    drawn = []
    for vec, color, name in axes:
        px, py, depth = project(rotate_vec(m, vec), cx, cy, scale)
        drawn.append((depth, px, py, color, name))
    drawn.sort(key=lambda t: t[0])

    ox, oy, _ = project((0.0, 0.0, 0.0), cx, cy, scale)
    for depth, px, py, color, name in drawn:
        # Nearer arms draw thicker — a cheap depth cue without shading.
        width = 3 if depth >= 0 else 2
        c.create_line(ox, oy, px, py, fill=color, width=width)
        c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=color, outline="")
        c.create_text(px + 9, py - 7, text=name, fill=color,
                     font=(font, 8, "bold"))


# ── Hand skeleton ────────────────────────────────────────────────────────

# Hand skeleton indices: 0 root/palm, 1 wrist, then five finger chains off
# the wrist, tips at 5/10/15/20/25. This layout is shared by SteamVR's native
# 31-bone skeleton (26-30 are aux bones, not drawn), the 26-bone OpenXR-style
# set Steam Link reports, and the bones the WiVRn driver sends the Linux bridge.
SKELETON_CHAINS = (
    (1, 2, 3, 4, 5),          # thumb
    (1, 6, 7, 8, 9, 10),      # index
    (1, 11, 12, 13, 14, 15),  # middle
    (1, 16, 17, 18, 19, 20),  # ring
    (1, 21, 22, 23, 24, 25),  # pinky
)
SKELETON_TIPS = frozenset((5, 10, 15, 20, 25))


class SkeletonRenderer:
    """Draws one hand's tracked skeleton, and its position dome, into a box of a canvas (the Skeleton tab's panels,
    and the stream view). font: the GUI's font family."""

    def __init__(self, label, font):
        self.label = label
        self.font = font
        # Projection axes, chosen from the first tracked frame and then kept
        # fixed — re-deriving per frame makes the view twitch between axes
        # whenever the hand tilts past 45°.
        self._axes = None
        # Dome trail: recent (time, head-local unit direction) samples for
        # this panel's own hand, redrawn with fading color each frame.
        self._trail = []

    def _pick_axes(self, joints):
        """Choose the two model-space axes to project onto: the widest-spread
        axis is drawn vertically (finger direction), the runner-up across.
        Sign puts fingertips at the top of the canvas."""
        pts = joints[1:26]  # skip root and aux bones
        ext = []
        for a in range(3):
            vals = [p[a] for p in pts]
            ext.append((max(vals) - min(vals), a))
        ext.sort(reverse=True)
        v_axis, h_axis = ext[0][1], ext[1][1]
        tips = [joints[t][v_axis] for t in SKELETON_TIPS]
        v_sign = -1.0 if (sum(tips) / len(tips)) >= joints[1][v_axis] else 1.0
        self._axes = (h_axis, v_axis, v_sign)

    def draw_in(self, c, x0, y0, w, h, joints, status, pose=None, other_pose=None, mode=None, weight=1.0,
                title=True):
        """Draw into the box (x0, y0, w, h) of canvas c. pose/other_pose: (rot_3x3, head_local_pos, dist, velocity)
        from pose_info. mode: the driver's output mode for this hand (cf_protocol.MODE_NAMES; the Linux bridge's), shown
        in the corner.
        weight: the stroke scale (1 = the Skeleton tab's)."""
        if not joints or len(joints) < 26:
            self._axes = None
            c.create_text(x0 + w // 2, y0 + h // 2,
                          text=f"{self.label} skeleton\n({status})",
                          fill=COLOR_FG_DIM, font=(self.font, 9),
                          justify=tk.CENTER)
        else:
            if title:
                c.create_text(x0 + w // 2, y0 + 12, text=f"{self.label} SKELETON",
                              fill=COLOR_BLUE, font=(self.font, 8, "bold"))
            if pose is not None:
                to_px = self._orient_to_px(joints, pose[0], w, h, dist=pose[2])
            else:
                to_px = self._autofit_to_px(joints, w, h)

            def in_box(j):
                x, y = to_px(j)
                return x0 + x, y0 + y

            self._draw_bones(c, in_box, weight)

        if pose is not None:
            self._draw_dome(c, x0, y0, w, h, pose, other_pose)

        if mode:
            c.create_text(x0 + w - 8, y0 + 12, text=f"driver: {mode}", fill=COLOR_FG_DIM,
                          font=(self.font, 7), anchor=tk.E)

    # ── skeleton projections ──

    def _orient_to_px(self, joints, rot, w, h, dist=None):
        """World-oriented view: wrist-relative joints rotated by the hand
        device's world rotation, then through the same fixed three-quarter
        camera the IMU triads use. Base scale is physical (no zooming as the
        hand turns); on top of that, distance from the head scales the whole
        render like the dome dot — closer hand draws bigger."""
        scale = min(w - 24, h - 36) / 0.28  # px per metre, hand span ~0.25 m
        if dist is not None:
            scale *= max(0.6, min(1.5, 0.55 / max(dist, 0.2)))
        wx, wy, wz = joints[1]
        # Straight-on camera, unlike the IMU triads' three-quarter view: its
        # 35° yaw reads as "the hand is rotated wrong", not as perspective.
        # A touch of pitch keeps some depth without skewing the heading.
        cy_, sy_ = 1.0, 0.0
        cp_, sp_ = math.cos(_VIEW_PITCH), math.sin(_VIEW_PITCH)

        def to_px(j):
            lx, ly, lz = joints[j]
            lx, ly, lz = lx - wx, ly - wy, lz - wz
            x = rot[0][0] * lx + rot[0][1] * ly + rot[0][2] * lz
            y = rot[1][0] * lx + rot[1][1] * ly + rot[1][2] * lz
            z = rot[2][0] * lx + rot[2][1] * ly + rot[2][2] * lz
            # 180° about vertical: without it the render is left/right
            # mirrored relative to the user's own view of their hand.
            x, z = -x, -z
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            y1 = y * cp_ - z1 * sp_
            return w / 2 + x1 * scale, h / 2 + 6 - y1 * scale

        return to_px

    def _autofit_to_px(self, joints, w, h):
        """Fallback when no device pose is available: original auto-fit."""
        if self._axes is None:
            self._pick_axes(joints)
        h_axis, v_axis, v_sign = self._axes

        pts = joints[1:26]
        hs = [p[h_axis] for p in pts]
        vs = [p[v_axis] * v_sign for p in pts]
        cx_m = (max(hs) + min(hs)) / 2.0
        cy_m = (max(vs) + min(vs)) / 2.0
        span_h = max(max(hs) - min(hs), 0.05)
        span_v = max(max(vs) - min(vs), 0.05)
        scale = min((w - 24) / span_h, (h - 32) / span_v)

        def to_px(j):
            p = joints[j]
            return (w / 2 + (p[h_axis] - cx_m) * scale,
                    h / 2 + 4 + (p[v_axis] * v_sign - cy_m) * scale)

        return to_px

    @staticmethod
    def _draw_bones(c, to_px, weight=1.0):
        px = {j: to_px(j) for chain in SKELETON_CHAINS for j in chain}
        for chain in SKELETON_CHAINS:
            for a, b in zip(chain, chain[1:]):
                c.create_line(*px[a], *px[b], fill=COLOR_FG_DIM, width=2 * weight, capstyle=tk.ROUND)

        tip, joint = 3 * weight, 2 * weight
        for chain in SKELETON_CHAINS:
            for j in chain[1:]:
                x, y = px[j]
                if j in SKELETON_TIPS:
                    c.create_oval(x - tip, y - tip, x + tip, y + tip,
                                  fill=COLOR_ACCENT, outline="")
                else:
                    c.create_oval(x - joint, y - joint, x + joint, y + joint,
                                  fill=COLOR_BLUE, outline="")

        wx, wy = px[1]
        c.create_rectangle(wx - tip, wy - tip, wx + tip, wy + tip,
                           fill=COLOR_GREEN, outline="")

    # ── position inset: 180° dome as seen from the head ──

    def _draw_dome(self, c, x0, y0, w, h, pose, other_pose):
        """Azimuthal-equidistant projection of the front hemisphere: centre is
        straight ahead of the gaze, rings at 30°/60°/90° off-forward, the rim
        is beside/behind the head. Dot size encodes distance."""
        r = max(24, min(44, h // 3))
        margin = 8
        cx = x0 + ((margin + r) if self.label == "LEFT" else (w - margin - r))
        cy = y0 + h - margin - r

        for k in (1 / 3, 2 / 3, 1.0):
            rr = r * k
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                          outline=COLOR_BG3, width=1)
        for deg in range(0, 360, 45):
            a = math.radians(deg)
            c.create_line(cx + (r / 3) * math.cos(a), cy + (r / 3) * math.sin(a),
                          cx + r * math.cos(a), cy + r * math.sin(a),
                          fill=COLOR_BG3, width=1)
        c.create_line(cx - 3, cy, cx + 3, cy, fill=COLOR_FG_DIM)
        c.create_line(cx, cy - 3, cx, cy + 3, fill=COLOR_FG_DIM)

        # Fading trail of the own hand's last second of motion.
        now = time.time()
        self._trail.append((now, pose[1]))
        while self._trail and self._trail[0][0] < now - 1.0:
            self._trail.pop(0)
        pts = [self._dome_project(cx, cy, r, p)[:2] for _, p in self._trail]
        for i in range(1, len(pts)):
            age = (now - self._trail[i][0])  # 0 = fresh, 1 = oldest
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=blend(COLOR_ACCENT, COLOR_BG2, age), width=1)

        if other_pose is not None:
            self._dome_dot(c, cx, cy, r, other_pose, COLOR_FG_DIM)
        self._dome_dot(c, cx, cy, r, pose, COLOR_ACCENT)

        c.create_text(cx, cy - r - 7, text=f"{pose[2]:.2f}m",
                      fill=COLOR_FG_DIM, font=(self.font, 7))

    @staticmethod
    def _dome_project(cx, cy, r, local):
        """Head-local point → dome pixel position (+ whether behind 90°)."""
        x, y, z = local
        n = math.sqrt(x * x + y * y + z * z)
        if n < 1e-6:
            return cx, cy, False
        ux, uy, uz = x / n, y / n, z / n
        # Head frame: +x right, +y up, -z forward. Angle off forward-gaze:
        theta = math.acos(max(-1.0, min(1.0, -uz)))
        rr = min(theta / (math.pi / 2), 1.0) * r
        phi = math.atan2(uy, ux)
        return (cx + rr * math.cos(phi), cy - rr * math.sin(phi),
                theta > math.pi / 2)

    def _dome_dot(self, c, cx, cy, r, pose, color):
        _, local, dist, vel = pose
        px, py, behind = self._dome_project(cx, cy, r, local)

        # Velocity whisker: where the hand will be in 0.15 s, projected the
        # same way, so the whisker curves with the dome rather than lying.
        speed = math.sqrt(sum(v * v for v in vel))
        if speed > 0.05:
            ahead = tuple(local[i] + vel[i] * 0.15 for i in range(3))
            qx, qy, _ = self._dome_project(cx, cy, r, ahead)
            c.create_line(px, py, qx, qy, fill=color, width=1)

        size = max(2.5, 7.0 - dist * 6.0)  # closer → bigger
        if behind:
            c.create_oval(px - size, py - size, px + size, py + size,
                          outline=color, width=1)
        else:
            c.create_oval(px - size, py - size, px + size, py + size,
                          fill=color, outline="")


# ── Stream mode ──────────────────────────────────────────────────────────

def round_rect(c, x0, y0, x1, y1, r, **kw):
    """A rectangle with rounded corners (a smoothed polygon)."""
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return c.create_polygon(
        x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r, x1, y1 - r, x1, y1 - r,
        x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r,
        x0, y0 + r, x0, y0, smooth=True, **kw)


class IdleHand:
    """What a CyberFinger that isn't connected shows: nothing pressed, the stick at rest, no motion sensors."""
    buttons = buttons2 = 0
    joy_x_float = joy_y_float = trigger_float = 0.0

    @staticmethod
    def active_imus():
        return []


class StreamHand:
    """One CyberFinger in the stream view, on a canvas of its own. Top to bottom: name, battery and report rate; the
    tracked hand skeleton, with its position dome (while there is a source for it: SteamVR, or the WiVRn driver);
    stick, trigger and buttons; the motion sensors; the vibration apps ask for (VR mode). The right hand's is the
    left's mirror image. font: the GUI's font family. black_hold_s: the driver's long press of the black button, in
    seconds (0 = none)."""

    GLOW_S = 0.25     # a released button fades out over this long, so a quick tap can be seen
    TRAIL_S = 0.35    # the stick's trail
    HEAD_H = 34
    HAPTIC_H = 34

    def __init__(self, parent, label, side, font, black_hold_s):
        self.label = label
        self.font = font
        self.black_hold_s = black_hold_s
        self.mirror = label == "RIGHT"
        self.canvas = tk.Canvas(parent, bg=COLOR_BG2, highlightthickness=0, width=1, height=1)
        self.canvas.pack(side=side, fill=tk.BOTH, expand=True, padx=(0, 4) if side == tk.LEFT else (4, 0))
        self._skeleton = SkeletonRenderer(label, font)
        self._lit = {}               # button → when it was last seen pressed
        self._pink_until = 0.0       # the pink click (~80 ms), shown lit until then
        self._black_since = None     # the black button held since then (its long press)
        self._trail = []             # (time, x, y) of the stick, -1..1
        self._rate = (0.0, 0, 0.0)   # reports per second: (measured at, packet count then, rate)

    def _mx(self, w, d):
        """x at d from the hand's outer edge (the left one's left, the right one's right)."""
        return w - d if self.mirror else d

    def _level(self, name, pressed, now):
        """1 while pressed, fading to 0 after the release."""
        if pressed:
            self._lit[name] = now
            return 1.0
        return max(0.0, 1.0 - (now - self._lit.get(name, -1e9)) / self.GLOW_S)

    def draw(self, state, haptic, pink_text, skeleton):
        """state: the GUI's HandState. skeleton: (joints, status, pose, other_pose, mode) while there is a source for
        it, else None (its space then goes to the controls and the motion sensors); mode as SkeletonRenderer's.
        haptic: the HapticTracker state in VR mode, else None."""
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 120 or h < 200:
            return
        now = time.perf_counter()
        live = state.connected
        if not live:
            state = IdleHand

        body = h - self.HEAD_H - (self.HAPTIC_H if haptic is not None else 0)
        if skeleton is not None:
            ctrl_h = max(124.0, min(190.0, body * 0.27))
            imu_h = max(104.0, min(170.0, body * 0.24))
        else:
            ctrl_h = imu_h = body / 2.0
        ctrl_y = h - (self.HAPTIC_H if haptic is not None else 0) - imu_h - ctrl_h
        imu_y = ctrl_y + ctrl_h

        if skeleton is not None:
            joints, status, pose, other_pose, mode = skeleton
            box_h = ctrl_y - self.HEAD_H
            self._skeleton.draw_in(c, 0, self.HEAD_H, w, box_h, joints, status, pose, other_pose, mode=mode,
                                   weight=max(1.0, min(2.2, min(w, box_h) / 170.0)), title=False)
            # A hand close to the headset is drawn large: what leaves its box goes under the rest.
            c.create_rectangle(0, 0, w, self.HEAD_H, fill=COLOR_BG2, outline="")
            c.create_rectangle(0, ctrl_y, w, h, fill=COLOR_BG2, outline="")
            c.create_line(16, ctrl_y, w - 16, ctrl_y, fill=COLOR_BG3)

        self._draw_header(c, w, state, live, now)
        self._draw_controls(c, w, ctrl_y, ctrl_h, state, pink_text, now)
        c.create_line(16, imu_y, w - 16, imu_y, fill=COLOR_BG3)
        self._draw_imus(c, w, imu_y, imu_h, state, live)
        if haptic is not None:
            c.create_line(16, h - self.HAPTIC_H, w - 16, h - self.HAPTIC_H, fill=COLOR_BG3)
            draw_haptic_meter(c, 10, h - self.HAPTIC_H + 6, w - 20, 22, haptic, now, label="HAPTIC",
                              accent=COLOR_ACCENT, bg=COLOR_BG, dim=COLOR_FG_DIM, fg=COLOR_FG, line=COLOR_BG3,
                              font=(self.font, 8))

    def _draw_header(self, c, w, state, live, now):
        outer, inner = (tk.E, tk.W) if self.mirror else (tk.W, tk.E)
        y = self.HEAD_H / 2 + 1
        c.create_text(self._mx(w, 14), y, text=self.label, anchor=outer, font=(self.font, 13, "bold"),
                      fill=COLOR_ACCENT if live else COLOR_FG_DIM)
        if not live:
            c.create_text(self._mx(w, w - 14), y, text="offline", anchor=inner, fill=COLOR_FG_DIM,
                          font=(self.font, 9))
            return

        # Reports per second, measured over half a second: 0 (red) means the Bluetooth link has stalled.
        t0, n0, rate = self._rate
        if now - t0 >= 0.5:
            rate = (state.packet_count - n0) / (now - t0) if state.packet_count >= n0 else 0.0
            self._rate = (now, state.packet_count, rate)

        bat = max(0, min(100, state.battery))
        color = COLOR_GREEN if bat > 50 else COLOR_ORANGE if bat > 20 else COLOR_RED
        bx0, bx1 = sorted((self._mx(w, w - 14), self._mx(w, w - 36)))       # the battery, at the inner edge
        c.create_rectangle(bx0, y - 5, bx1, y + 5, outline=COLOR_FG_DIM)
        c.create_rectangle(bx1, y - 2, bx1 + 2, y + 2, fill=COLOR_FG_DIM, outline="")
        if bat:
            c.create_rectangle(bx0 + 2, y - 3, bx0 + 2 + (bx1 - bx0 - 3) * bat / 100.0, y + 4, fill=color, outline="")
        c.create_text(self._mx(w, w - 44), y, text=f"{bat}%", anchor=inner, fill=color, font=(self.font, 9))
        c.create_text(self._mx(w, w - 84), y, text=f"{rate:.0f} Hz", anchor=inner, font=(self.font, 8),
                      fill=COLOR_FG_DIM if rate >= 1.0 else COLOR_RED)

    def _draw_controls(self, c, w, y0, zh, state, pink_text, now):
        """The stick at the hand's outer edge (lit while clicked, with a short trail), the trigger's bar beside it,
        then the buttons."""
        pad = 12.0
        r = max(26.0, min(zh / 2.0 - 20.0, w * 0.17))
        scx, scy = self._mx(w, pad + r), y0 + zh / 2.0

        click = self._level("JCLK", state.buttons & BTN_STICK, now)
        c.create_oval(scx - r, scy - r, scx + r, scy + r, width=2 + round(click),
                      fill=blend(COLOR_BG, COLOR_ACCENT, 0.3 * click), outline=blend(COLOR_BG3, COLOR_ACCENT, click))
        c.create_line(scx - r, scy, scx + r, scy, fill=COLOR_BG3)
        c.create_line(scx, scy - r, scx, scy + r, fill=COLOR_BG3)
        dot = max(6.0, r * 0.16)
        reach = r - dot
        jx, jy = state.joy_x_float, state.joy_y_float
        self._trail.append((now, jx, jy))
        while self._trail[0][0] < now - self.TRAIL_S:
            self._trail.pop(0)
        for (t, ax, ay), (_, bx, by) in zip(self._trail, self._trail[1:]):
            if (ax, ay) != (bx, by):
                c.create_line(scx + ax * reach, scy + ay * reach, scx + bx * reach, scy + by * reach, width=2,
                              fill=blend(COLOR_ACCENT, COLOR_BG, (now - t) / self.TRAIL_S))
        px, py = scx + jx * reach, scy + jy * reach
        if abs(jx) > 0.03 or abs(jy) > 0.03:
            c.create_line(scx, scy, px, py, fill=COLOR_ACCENT2, width=2)
        c.create_oval(px - dot, py - dot, px + dot, py + dot, fill=COLOR_ACCENT, outline=COLOR_ACCENT2)
        c.create_text(scx, scy + r + 10, text="STICK", fill=COLOR_FG_DIM, font=(self.font, 7))

        # Trigger: how far it's pulled, bottom up; the frame lights with the TRIG button.
        bar_w = max(12.0, r * 0.26)
        tcx = self._mx(w, pad + 2 * r + 14 + bar_w / 2)
        top, bottom = scy - r, scy + r
        pull = state.trigger_float
        trig = self._level("TRIG", state.buttons & BTN_TRIGGER, now)
        c.create_rectangle(tcx - bar_w / 2, top, tcx + bar_w / 2, bottom, fill=COLOR_BG,
                           outline=blend(COLOR_BG3, COLOR_ACCENT, trig), width=1 + round(trig))
        if pull > 0.01:
            c.create_rectangle(tcx - bar_w / 2 + 2, bottom - (bottom - top - 4) * pull - 2, tcx + bar_w / 2 - 2,
                               bottom - 2, fill=COLOR_ACCENT, outline="")
        c.create_text(tcx, top - 9, text=f"{int(pull * 100)}%", font=(self.font, 8),
                      fill=COLOR_FG if pull > 0.01 else COLOR_FG_DIM)
        c.create_text(tcx, bottom + 10, text="TRIG", fill=blend(COLOR_FG_DIM, COLOR_FG, trig), font=(self.font, 7))

        # Buttons: two columns, then the black and the pink button across both.
        gx0, gx1 = sorted((self._mx(w, pad + 2 * r + 14 + bar_w + 16), self._mx(w, w - pad)))
        gap = 5.0
        row_h = min(44.0, (zh - 16 - 4 * gap) / 5.0)
        gy = y0 + (zh - 5 * row_h - 4 * gap) / 2.0
        col_w = (gx1 - gx0 - gap) / 2.0
        font = (self.font, max(8, min(12, round(row_h * 0.38))), "bold")

        def chip(row, col, span, name, level, pressed, text=None):
            x0 = gx0 + col * (col_w + gap)
            x1 = x0 + col_w * span + gap * (span - 1)
            y = gy + row * (row_h + gap)
            round_rect(c, x0, y, x1, y + row_h, 7, fill=blend(COLOR_BG, COLOR_ACCENT, level),
                        outline=blend(COLOR_BG3, COLOR_ACCENT2 if pressed else COLOR_ACCENT, level))
            c.create_text((x0 + x1) / 2, y + row_h / 2, text=text or name, font=font,
                          fill="white" if pressed else blend(COLOR_FG_DIM, COLOR_FG, level))
            return x0, y, x1

        for i, (name, bit) in enumerate((("GRIP", BTN_GRIP), ("MENU", BTN_MENU), ("C", BTN_C), ("D", BTN_D),
                                         ("E", BTN_E), ("JCLK", BTN_STICK))):
            chip(i // 2, i % 2, 1, name, self._level(name, state.buttons & bit, now), bool(state.buttons & bit))

        # The black button, with its long press as the driver sees it: the line along the bottom grows while it's
        # held, and at the threshold the button reads HOLD.
        black = bool(state.buttons & BTN_STSEL)
        if not black:
            self._black_since = None
        elif self._black_since is None:
            self._black_since = now
        held = now - self._black_since if black else 0.0
        hold = min(1.0, held / self.black_hold_s) if self.black_hold_s > 0 else 0.0
        x0, y, x1 = chip(3, 0, 2, "ST/SE", self._level("ST/SE", black, now), black,
                         text="ST/SE · HOLD" if hold >= 1.0 else None)
        if hold > 0:
            c.create_line(x0 + 7, y + row_h - 3, x0 + 7 + (x1 - x0 - 14) * hold, y + row_h - 3, fill="white",
                          width=3 if hold >= 1.0 else 2, capstyle=tk.ROUND)

        if state.buttons2 & cyberfinger_report.PINK:
            self._pink_until = now + 0.3
        pink = now < self._pink_until
        chip(4, 0, 2, "PINK", self._level("PINK", pink, now), pink, text=pink_text)

    def _draw_imus(self, c, w, y0, zh, state, live):
        """Each fitted motion sensor's orientation: its axes against a horizon ring, and roll, pitch, yaw."""
        imus = state.active_imus()
        if not imus:
            c.create_text(w / 2, y0 + zh / 2, text="no IMU installed" if live else "", fill=COLOR_FG_DIM,
                          font=(self.font, 9))
            return
        col_w = w / len(imus)
        scale = max(18.0, min(col_w * 0.30, zh / 2.0 - 26.0))
        block_h = 2 * scale + 52            # name, axes, angles: kept together, in the middle of a tall space
        top = y0 + max(0.0, (zh - block_h) / 2.0)
        for i, (label, quat) in enumerate(imus):
            cx = col_w * (i + 0.5)
            c.create_text(cx, top + 13, text=label, fill=COLOR_FG_DIM, font=(self.font, 8, "bold"))
            draw_triad(c, cx, top + block_h / 2.0 + 2, scale, quat, self.font)
            roll, pitch, yaw = quat_to_euler_deg(quat)
            c.create_text(cx, top + block_h - 11, text=f"{roll:+4.0f} {pitch:+4.0f} {yaw:+4.0f}",
                          fill=COLOR_FG_DIM, font=(self.font, 8))


class StreamView:
    """Stream mode's view: both CyberFingers side by side, filling the window."""

    def __init__(self, parent, font, black_hold_s):
        self.frame = ttk.Frame(parent)
        self.hands = (StreamHand(self.frame, "LEFT", tk.LEFT, font, black_hold_s),
                      StreamHand(self.frame, "RIGHT", tk.RIGHT, font, black_hold_s))

    def draw(self, states, haptics, pink_texts, skeletons=None):
        """Per hand, left then right: its HandState, haptic state (or None), pink button's label, and
        (joints, status, pose, other_pose, mode) in skeletons, which is None without a source for them."""
        for i, hand in enumerate(self.hands):
            hand.draw(states[i], haptics[i], pink_texts[i], skeletons[i] if skeletons else None)
