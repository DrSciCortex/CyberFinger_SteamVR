# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""T2.2 — IMU↔optical orientation fuser (complementary filter on the extrinsic).

The glove's VQF already integrates the fast dynamics and gravity-locks tilt on-device
and only sends the fused quaternion (no raw gyro), so there is nothing fast left to
integrate. The fused hand orientation is therefore ALWAYS

    q_hat = q_off ⊗ q_imu

and the only thing worth filtering is the slowly-varying extrinsic q_off, whose
roll/pitch are physically constant (both worlds gravity-aligned) and whose yaw drifts
(6-axis, no magnetometer). This is a ~1-line complementary update on q_off:

    CLEAR      → q_off ← slerp(q_off, q_opt ⊗ q_imu⁻¹, α),   α = 1 − e^(−dt/τ)
                 (τ≈1s averages ~20 frames, killing wrist-vs-palm articulation noise
                  while tracking the slow yaw), re-anchor position from optical
    occluded   → freeze q_off; q_hat rides the still-flowing q_imu (dead-reckoning)

`gate_type` is the SOLE trust switch — Quest keeps is_active=1 and emits a moving,
plausible-but-wrong pose during occlusion, so only the gate marks a real dropout.
Quaternions are scalar-first [w,x,y,z], Hamilton.
"""

import math

import numpy as np

from extrinsic import hand_frame_wrist, hand_frame_metacarpal_plane, quat_to_R, _R_to_quat

IDENTITY = np.array([1.0, 0.0, 0.0, 0.0])


def qmul(a, b):
    """Hamilton product a ⊗ b (both scalar-first)."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw])


def qconj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def qnorm(q):
    return np.asarray(q, float) / (np.linalg.norm(q) or 1.0)


def slerp(a, b, t):
    """Shortest-arc SLERP from a to b by fraction t (both unit, scalar-first)."""
    a = qnorm(a)
    b = qnorm(b)
    d = float(np.dot(a, b))
    if d < 0:                       # take the shorter arc
        b = -b
        d = -d
    if d > 0.9995:                  # nearly aligned → lerp + renormalise
        return qnorm(a + t * (b - a))
    th = math.acos(max(-1.0, min(1.0, d)))
    s = math.sin(th)
    return (math.sin((1 - t) * th) / s) * a + (math.sin(t * th) / s) * b


class OrientationFuser:
    """One per hand. Tracks the extrinsic q_off; outputs the fused hand orientation."""

    def __init__(self, right=True, slot="g2", tau=1.0):
        self.right = right
        self._frame = hand_frame_wrist if slot == "g2" else hand_frame_metacarpal_plane
        self.tau = tau                       # complementary time constant (s)
        self.q_off = IDENTITY.copy()
        self._have_off = False

    def set_R_off(self, R_off):
        """Seed q_off from a pre-computed extrinsic (e.g. extrinsic.calibrate)."""
        self.q_off = qnorm(_R_to_quat(np.asarray(R_off, float)))
        self._have_off = True

    def optical_quat(self, P):
        """Optical hand orientation quaternion from the 26 world joints, or None."""
        R = self._frame(np.asarray(P, float), self.right)
        if np.linalg.det(R) < 0.99:
            return None
        return qnorm(_R_to_quat(R))

    def update(self, gate_type, P, q_imu, dt=1 / 22.0):
        """One frame. gate_type: the gate verdict; P: (26,3) world joints or None; q_imu:
        glove quat (w,x,y,z). Returns the fused hand orientation quat q_hat."""
        q_imu = qnorm(q_imu)
        if gate_type == "CLEAR" and P is not None:
            q_opt = self.optical_quat(P)
            if q_opt is not None:
                meas = qmul(q_opt, qconj(q_imu))          # R_off = R_opt · R_imuᵀ
                if np.dot(meas, self.q_off) < 0:          # hemisphere-align
                    meas = -meas
                if not self._have_off:                    # first CLEAR: snap, don't crawl
                    self.q_off = meas
                    self._have_off = True
                else:
                    alpha = 1.0 - math.exp(-dt / max(self.tau, 1e-6))
                    self.q_off = qnorm(slerp(self.q_off, meas, alpha))
        # non-CLEAR: q_off frozen → q_hat dead-reckons through the flowing IMU
        return qnorm(qmul(self.q_off, q_imu))


def geodesic_quat_deg(a, b):
    """Angle (deg) between two orientations given as quaternions."""
    d = abs(float(np.dot(qnorm(a), qnorm(b))))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, d))))
