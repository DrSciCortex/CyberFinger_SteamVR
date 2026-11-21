# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Hand skeletons for the Linux bridge's Skeleton tab, from the driver's context stream (CFOP).

The Windows bridge reads the skeleton SteamVR tracks through OpenVR (OpenVRSkeletonSource). Here the CyberFinger
driver in WiVRn already sends what it sees of the headset's hand tracking to the bridge, ~120 times a second
(driver_link.DriverLink, cf_protocol.unpack_context): per hand the pose in tracking space and 31 parent-space bones,
and the headset's pose in the same space. DriverSkeletonSource turns the latest packet into what the SkeletonPanel
draws, with OpenVRSkeletonSource's contract:

    .hands       [left, right]  each None, or a tuple of (x, y, z) joint positions in the space of the hand's pose
    .pose_info   [left, right]  each None, or (rot_3x3_rows, head_local_pos, dist, head_local_vel); the rotation
                                is the hand's in the panel's world (see relative_pose)
    .status      short string for the panel placeholder
    .modes       [left, right]  the driver's output mode per hand (cf_protocol.MODE_NAMES), None without the driver

The bones: 0 is the root (identity), 1 the wrist relative to the hand's pose, 2..25 the finger joints relative to
their parents, in the SteamVR hand skeleton's order (26..30, its aux bones, are unused). Only the joint positions
that forward kinematics gives are used: the bones' rotations follow OpenXR's joint axes, not SteamVR's.
"""

import math

import cf_protocol

# Parent of each bone 0..25: root, wrist, then thumb 2-5, index 6-10, middle 11-15, ring 16-20, pinky 21-25.
BONE_PARENTS = (-1, 0,
                1, 2, 3, 4,
                1, 6, 7, 8, 9,
                1, 11, 12, 13, 14,
                1, 16, 17, 18, 19,
                1, 21, 22, 23, 24)
NUM_JOINTS = len(BONE_PARENTS)

IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)


def _unit(q):
    """q (w, x, y, z) normalised, or None when it isn't a rotation (all zero, not finite)."""
    n = math.sqrt(sum(c * c for c in q))
    if not math.isfinite(n) or n < 1e-6:
        return None
    return tuple(c / n for c in q)


def _quat_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def quat_to_matrix(q):
    """Unit quaternion (w, x, y, z) → 3x3 rotation matrix as row tuples."""
    w, x, y, z = q
    return ((1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z),       2.0 * (x * z + w * y)),
            (2.0 * (x * y + w * z),       1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
            (2.0 * (x * z - w * y),       2.0 * (y * z + w * x),       1.0 - 2.0 * (x * x + y * y)))


def _rotate(m, v):
    return tuple(m[r][0] * v[0] + m[r][1] * v[1] + m[r][2] * v[2] for r in range(3))


def model_joints(bones):
    """Forward kinematics over the hand skeleton: parent-space bones (px, py, pz, qw, qx, qy, qz) → the positions of
    joints 0..25 in the space of the hand's pose. None with fewer than 26 bones."""
    if bones is None or len(bones) < NUM_JOINTS:
        return None
    pos, rot = [], []
    for j, parent in enumerate(BONE_PARENTS):
        b = bones[j]
        p, q = tuple(b[0:3]), _unit(b[3:7]) or IDENTITY_QUAT
        if parent >= 0:
            offset = _rotate(quat_to_matrix(rot[parent]), p)
            p = tuple(pos[parent][i] + offset[i] for i in range(3))
            q = _quat_mul(rot[parent], q)
        pos.append(p)
        rot.append(q)
    return tuple(pos)


def _panel_world(hrot):
    """Rows of the rotation from tracking space into the world the SkeletonPanel draws: +y up, the user's heading
    along +z, so +x to the user's left.

    The panel looks along +z of the rotation it is given, which shows the hands as their owner sees them when the
    user faces that way: in the Windows bridge, a matter of how SteamVR's universe lies in the room. The tracking
    space here can lie any way round, so the heading is taken from the headset: its right-hand axis on the floor
    plane, which holds whatever the head's pitch (looking down at the hands).
    """
    a, b = hrot[0][0], hrot[2][0]
    n = math.hypot(a, b)
    if n < 1e-6:                     # the head on its side: no heading, the tracking space's own
        return ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0))
    a, b = a / n, b / n
    return ((-a, 0.0, -b), (0.0, 1.0, 0.0), (b, 0.0, -a))


def relative_pose(hmd_pos, hmd_rot, pos, rot, hmd_vel=(0.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0)):
    """(hand rotation as matrix rows, head-local position, distance, head-local velocity relative to the head):
    the SkeletonPanel's pose. Rotations are unit quaternions (w, x, y, z) in tracking space; the one returned is
    the hand's in the panel's world (_panel_world).

    The head frame is the headset's own, in OpenXR as in OpenVR: +x right, +y up, -z forward — what the dome
    inset projects.
    """
    hrot = quat_to_matrix(hmd_rot)
    rel = tuple(pos[i] - hmd_pos[i] for i in range(3))
    relv = tuple(vel[i] - hmd_vel[i] for i in range(3))
    # Columns of hrot are the head axes in tracking space, so head-local is R^T · v.
    local = tuple(sum(hrot[r][i] * rel[r] for r in range(3)) for i in range(3))
    local_v = tuple(sum(hrot[r][i] * relv[r] for r in range(3)) for i in range(3))
    dist = math.sqrt(sum(v * v for v in rel))
    world, drot = _panel_world(hrot), quat_to_matrix(rot)
    oriented = tuple(tuple(sum(world[r][k] * drot[k][c] for k in range(3)) for c in range(3)) for r in range(3))
    return oriented, local, dist, local_v


class DriverSkeletonSource:
    """The skeletons and poses of the latest CFOP, for the Skeleton tab.

    link: a callable returning the bridge's current DriverLink (None while it isn't in VR mode). Nothing runs in
    the background: update() from the UI thread, before drawing, reads the link's latest packet.
    """

    def __init__(self, link, driver="WiVRn driver"):
        self._link = link
        self._driver = driver
        self._ctx = None             # the packet .hands and .pose_info were made from
        self.hands = [None, None]    # 0 = left, 1 = right
        self.pose_info = [None, None]
        self.modes = [None, None]
        self.status = "starting..."

    def start(self):
        pass                         # the DriverLink is the bridge's: started and stopped with VR mode

    def stop(self):
        self.feed(None)

    @property
    def connected(self):
        """The driver is sending its status (as of the last update(); whether or not a hand is tracked)."""
        return self._ctx is not None

    def update(self):
        link = self._link()
        if link is None or not link.running:
            self.feed(None)
            self.status = "start the bridge in VR mode" if link is None else f"UDP {link.port} is busy"
            return
        ctx = link.context()
        self.feed(ctx)
        if ctx is None:
            self.status = f"{self._driver} not running"
        elif any(self.hands):
            self.status = "connected"
        else:
            self.status = "hands not in view"

    def feed(self, ctx):
        """ctx: a cf_protocol.unpack_context() dict, or None without a (recent) packet."""
        if ctx is self._ctx:
            return
        self._ctx = ctx
        if ctx is None:
            self.hands, self.pose_info, self.modes = [None, None], [None, None], [None, None]
            return
        hmd_rot = _unit(ctx["hmd_rot"]) if ctx["hmd_valid"] else None
        hands, poses = [None, None], [None, None]
        for hand, tap in enumerate(ctx["tap"][:2]):
            if tap["skel_valid"]:
                hands[hand] = model_joints(tap["bones"])
            # The bones hang off the hand's pose, so a skeleton implies one.
            rot = _unit(tap["raw_rot"]) if tap["pose_valid"] or tap["skel_valid"] else None
            if rot is not None and hmd_rot is not None:
                poses[hand] = relative_pose(ctx["hmd_pos"], hmd_rot, tap["raw_pos"], rot,
                                            ctx["hmd_lin_vel"], tap["lin_vel"])
        # Whole lists swapped in, as OpenVRSkeletonSource does.
        self.hands, self.pose_info = hands, poses
        self.modes = [cf_protocol.MODE_NAMES.get(m, str(m)) for m in ctx["mode"]]
