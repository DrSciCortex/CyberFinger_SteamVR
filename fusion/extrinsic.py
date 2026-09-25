# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""T2.2 — IMU↔optical extrinsic calibration (R_off).

The glove IMU reports orientation in its OWN frame (a physical mount angle + its own
gravity-aligned world with an arbitrary yaw origin). R_off is the constant rotation
that maps IMU orientation into the optical/headset frame, so `R_off · R_imu` is the
hand's orientation the way the camera sees it — the piece that lets the two be fused.

Calibrated on CLEAR frames (optical trusted), where we have BOTH the IMU quaternion
and the optical hand orientation (built from the 26 world joints):

    R_off,t = R_opt,t · R_imu,tᵀ           (per frame)
    R_off   = robust SO(3) mean over CLEAR  (SVD projection + Karcher/Huber refine)

Both worlds are gravity-aligned, so R_off's roll/pitch (tilt) are physically constant;
only its yaw drifts (6-axis VQF, no magnetometer) → swing_twist() separates them.

Quaternions/matrices: scalar-first [w,x,y,z], Hamilton; R columns are basis-in-world
(matches cyberfinger_gui.quat_to_matrix). Joint order is OpenXR XR_HAND_JOINT_*_EXT.
"""

import math

import numpy as np

# OpenXR joint indices used to build the optical hand frame.
WRIST = 1
IDX_MC, MID_MC, RING_MC, PINK_MC = 6, 11, 16, 21     # metacarpal bases (rigid to the palm)


def quat_to_R(q):
    """Unit quaternion (w, x, y, z) → 3x3 rotation (columns = basis-in-world)."""
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z),     2 * (x * z + w * y)],
        [2 * (x * y + w * z),     1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y),     2 * (y * z + w * x),     1 - 2 * (x * x + y * y)]])


def geodesic_deg(Ra, Rb):
    """Angle (deg) of the rotation taking Ra to Rb — the proper SO(3) distance."""
    c = (np.trace(Ra.T @ Rb) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def hand_frame_wrist(P, right=True):
    """Optical WRIST frame for the g2/BODY-2 IMU, from the 26 world joints P (26,3):
    forward = wrist→metacarpal-midpoint, across = index→pinky metacarpal (handedness
    signed), normal = across×forward. Metacarpals are rigid to the palm, so the frame
    doesn't move with finger splay. Returns R (columns fwd, side, n)."""
    w = P[WRIST]
    ik, pk = P[IDX_MC], P[PINK_MC]
    fwd = 0.5 * (ik + pk) - w
    fwd = fwd / (np.linalg.norm(fwd) or 1.0)
    across = (ik - pk) if right else (pk - ik)
    n = np.cross(across, fwd)
    n = n / (np.linalg.norm(n) or 1.0)
    side = np.cross(n, fwd)
    return np.column_stack([fwd, side, n])


def hand_frame_metacarpal_plane(P, right=True):
    """Optical frame for the gj/JOINT (knuckle) IMU: fit a plane to the four metacarpal
    heads (6,11,16,21) for the dorsal normal, forward toward the fingertips, across in
    the plane. The knuckle IMU sits on the back of the hand, so this dorsal-plane frame
    fits it better than the wrist frame (which fits gj ~2x worse)."""
    heads = np.array([P[IDX_MC], P[MID_MC], P[RING_MC], P[PINK_MC]])
    c = heads.mean(axis=0)
    # plane normal = smallest-singular-vector of the centered heads
    _, _, Vt = np.linalg.svd(heads - c)
    n = Vt[2]
    fwd = c - P[WRIST]
    fwd = fwd - np.dot(fwd, n) * n            # project forward into the plane
    fwd = fwd / (np.linalg.norm(fwd) or 1.0)
    # orient the normal dorsally (away from the palm side) using handedness
    across = (P[IDX_MC] - P[PINK_MC]) if right else (P[PINK_MC] - P[IDX_MC])
    if np.dot(np.cross(across, fwd), n) < 0:
        n = -n
    side = np.cross(n, fwd)
    return np.column_stack([fwd, side, n])


def mean_rotation_svd(Rs):
    """Chordal-L2 SO(3) mean: average the matrices, project to SO(3) via SVD."""
    M = np.mean(Rs, axis=0)
    U, _, Vt = np.linalg.svd(M)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    return R


def mean_rotation_robust(Rs, iters=3, huber_deg=15.0):
    """Karcher/IRLS robust mean: start from the SVD mean, then re-weight each sample by
    a Huber weight on its geodesic distance so articulation excursions are downweighted
    (not fit). Downweighting the tail, not removing it, keeps it stable with few frames."""
    R = mean_rotation_svd(Rs)
    for _ in range(iters):
        num = np.zeros((3, 3))
        wsum = 0.0
        for Ri in Rs:
            d = geodesic_deg(R, Ri)
            wt = min(1.0, huber_deg / max(d, 1e-6))     # Huber: flat inside, 1/d outside
            num += wt * Ri
            wsum += wt
        U, _, Vt = np.linalg.svd(num / max(wsum, 1e-9))
        Rn = U @ Vt
        if np.linalg.det(Rn) < 0:
            U[:, -1] *= -1
            Rn = U @ Vt
        if geodesic_deg(R, Rn) < 0.05:
            R = Rn
            break
        R = Rn
    return R


def swing_twist(R, axis=np.array([0.0, 1.0, 0.0])):
    """Split a rotation into a TWIST about `axis` (world-up +y by default → the drifting
    yaw) and the remaining SWING (roll/pitch tilt → gravity-constant). Returns
    (R_twist, R_swing, twist_deg) with R = R_swing · R_twist."""
    # quaternion of R (Shepperd), then project its vector part onto the axis
    q = _R_to_quat(R)
    v = q[1:]
    axis = axis / (np.linalg.norm(axis) or 1.0)
    proj = np.dot(v, axis) * axis
    tw = np.array([q[0], proj[0], proj[1], proj[2]])
    n = np.linalg.norm(tw) or 1.0
    tw = tw / n
    if tw[0] < 0:                                  # keep twist angle in (-pi, pi]
        tw = -tw
    R_tw = quat_to_R(tw)
    R_sw = R @ R_tw.T
    twist_deg = math.degrees(2.0 * math.atan2(np.linalg.norm(tw[1:]), tw[0]))
    return R_tw, R_sw, twist_deg


def _R_to_quat(R):
    """3x3 rotation → quaternion (w, x, y, z), Shepperd's numerically-stable method."""
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    q = np.array([w, x, y, z])
    return q / (np.linalg.norm(q) or 1.0)


def calibrate(P_list, q_imu_list, right=True, slot="g2"):
    """Fit R_off from paired CLEAR-frame samples.

    P_list: list of (26,3) world joints; q_imu_list: list of IMU quats (w,x,y,z) for the
    same frames; slot 'g2' (wrist frame) or 'gj' (metacarpal-plane frame). Returns a dict
    with R_off, R_off_tilt, R_off_twist (yaw), and geodesic residual stats (median/p90/max)."""
    frame = hand_frame_wrist if slot == "g2" else hand_frame_metacarpal_plane
    Ropt, Rimu = [], []
    for P, q in zip(P_list, q_imu_list):
        Ro = frame(np.asarray(P, float), right)
        if np.linalg.det(Ro) < 0.99:
            continue                                  # skip a degenerate frame
        Ropt.append(Ro)
        Rimu.append(quat_to_R(q))
    if len(Ropt) < 5:
        return None
    per = [Ro @ Ri.T for Ro, Ri in zip(Ropt, Rimu)]
    R_off = mean_rotation_robust(per)
    resid = sorted(geodesic_deg(Ro, R_off @ Ri) for Ro, Ri in zip(Ropt, Rimu))
    R_tw, R_sw, yaw_deg = swing_twist(R_off)
    return {
        "R_off": R_off, "R_off_tilt": R_sw, "R_off_twist": R_tw, "yaw_deg": yaw_deg,
        "n": len(Ropt), "slot": slot,
        "residual_median": resid[len(resid) // 2],
        "residual_p90": resid[int(0.9 * len(resid))],
        "residual_max": resid[-1],
    }
