# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
Per-finger optical-reliability gate for the 360° hand-tracking fusion.

Given the optical (Quest/OpenXR) hand estimate + camera pose each frame, produce a
per-joint confidence c_j in [0,1] and a per-finger blend weight beta_f, where 1 =
trust optical, 0 = fall back to the fused (EMG/IMU) estimate. The exact math is in
docs/gate-spec.md; this module is the reference implementation.

Cues per joint j (each in [0,1], 1 = reliable):
  g_fov  : inside the camera frustum (blind-spot detector)
  g_occ  : NOT self-occluded, by ray-casting to a sphere-per-joint hand model
  g_tmp  : temporally plausible (physiological speed/accel, not frozen)
and a coarse per-hand OpenXR liveness kill h in {0,1}.

  c_j = h * g_fov * g_occ * g_tmp          (product = logical AND)
Then EMA smoothing, distal-weighted per-finger aggregation, and a hysteresis ramp
to beta_f. Stateful (keeps history for velocity/accel/freeze/EMA) — one instance
per hand, call update() once per frame.
"""

import numpy as np

# OpenXR XR_HAND_JOINT_SET_DEFAULT_EXT: 0 PALM, 1 WRIST, then 5 fingers x (meta,
# prox, [inter,] dist, tip). Thumb has no intermediate.
FINGER_JOINTS = {
    "thumb":  [2, 3, 4, 5],
    "index":  [6, 7, 8, 9, 10],
    "middle": [11, 12, 13, 14, 15],
    "ring":   [16, 17, 18, 19, 20],
    "pinky":  [21, 22, 23, 24, 25],
}
N_JOINTS = 26

# Chain adjacency: a joint is not "occluded" by the bone it belongs to, so its own
# chain-neighbours are excluded from its occluder set.
_ADJ = {j: set() for j in range(N_JOINTS)}
for _js in ([1, 2, 3, 4, 5], [1, 6, 7, 8, 9, 10], [1, 11, 12, 13, 14, 15],
            [1, 16, 17, 18, 19, 20], [1, 21, 22, 23, 24, 25], [0, 1]):
    for _a, _b in zip(_js, _js[1:]):
        _ADJ[_a].add(_b); _ADJ[_b].add(_a)

# Distal-heavy aggregation weights within a finger (tip matters most for expression)
_LEVEL_W = {"meta": 0.6, "prox": 0.8, "inter": 1.0, "dist": 1.2, "tip": 1.4}


def _finger_weights():
    w = {}
    for f, js in FINGER_JOINTS.items():
        levels = (["meta", "prox", "dist", "tip"] if f == "thumb"
                  else ["meta", "prox", "inter", "dist", "tip"])
        w[f] = np.array([_LEVEL_W[l] for l in levels], float)
    return w


def _ramp(x):
    """clip(x, 0, 1): 0 at/below 0, linear to 1."""
    return np.clip(x, 0.0, 1.0)


class OcclusionGate:
    def __init__(self,
                 fov_h=np.radians(55.0), fov_v=np.radians(45.0),  # effective optical half-FOV
                 fov_soft=np.radians(6.0),                        # soft edge width delta
                 joint_radius=0.010,                              # sphere radius per joint (m)
                 v_max=2.5, a_max=60.0,                           # physiological speed/accel caps (m/s, m/s^2)
                 freeze_eps=2e-4, freeze_n=6, hand_move_eps=0.03, # frozen-inference detector
                 tau_s=0.10,                                      # EMA time constant (s)
                 tau_lo=0.35, tau_hi=0.70,                        # hysteresis ramp thresholds
                 tau_cal=0.85):                                   # calibration-buffer gate
        self.fov_h, self.fov_v, self.fov_soft = fov_h, fov_v, fov_soft
        self.joint_radius = joint_radius
        self.v_max, self.a_max = v_max, a_max
        self.freeze_eps, self.freeze_n, self.hand_move_eps = freeze_eps, freeze_n, hand_move_eps
        self.tau_s, self.tau_lo, self.tau_hi, self.tau_cal = tau_s, tau_lo, tau_hi, tau_cal
        self._fw = _finger_weights()
        self.reset()

    def reset(self):
        self._p_prev = None
        self._v_prev = None
        self._cbar = np.zeros(N_JOINTS)   # EMA of c_j
        self._freeze = np.zeros(N_JOINTS, int)

    # ── cues ────────────────────────────────────────────────────────────────
    def _g_fov(self, p, cam_pos, cam_R):
        """cam_R columns = (x_cam, y_cam, z_cam); camera looks along -z_cam."""
        q = (p - cam_pos) @ cam_R          # world->camera: R^T (p-c), rows of p
        fwd = -q[:, 2]                      # depth in front of camera
        az = np.arctan2(q[:, 0], np.maximum(fwd, 1e-9))
        el = np.arctan2(q[:, 1], np.maximum(fwd, 1e-9))
        infront = (fwd > 0).astype(float)
        return (infront
                * _ramp((self.fov_h - np.abs(az)) / self.fov_soft)
                * _ramp((self.fov_v - np.abs(el)) / self.fov_soft))

    def _g_occ(self, p, cam_pos, radius, other_pos=None, other_radius=None):
        """Ray-cast each joint to the camera; a joint is occluded if another
        sphere lies on the ray in front of it. Returns (g_occ, blocker) where
        g_occ in [0,1] (1 = visible) and blocker[j] in {-1 none, 0 self, 1 other}
        records WHICH hand caused the strongest block — the provenance that turns
        'occluded' into a named type (self-occlusion vs occluded-by-other)."""
        eps = 0.002
        occ = np.zeros(N_JOINTS)
        blocker = np.full(N_JOINTS, -1, int)
        if other_pos is not None:
            orad = (np.full(N_JOINTS, self.joint_radius) if other_radius is None
                    else np.asarray(other_radius, float))
        for j in range(N_JOINTS):
            dj = p[j] - cam_pos
            L = np.linalg.norm(dj)
            if L < 1e-6:
                continue
            u = dj / L
            best, best_src = 0.0, -1
            for k in range(N_JOINTS):                        # same-hand (self-occlusion)
                if k == j or k in _ADJ[j]:
                    continue
                wk = p[k] - cam_pos
                sk = wk @ u                                  # along-ray projection
                if not (eps < sk < L - eps):                 # must be in front of joint
                    continue
                dk = np.linalg.norm(wk - sk * u)             # perpendicular distance
                rk = radius[k]
                ov = _ramp((rk - dk) / max(rk, 1e-6))        # 1 through centre, 0 at edge
                if ov > best:
                    best, best_src = ov, 0
            if other_pos is not None:                        # other hand (inter-hand)
                for k in range(N_JOINTS):
                    wk = other_pos[k] - cam_pos
                    sk = wk @ u
                    if not (eps < sk < L - eps):
                        continue
                    dk = np.linalg.norm(wk - sk * u)
                    rk = orad[k]
                    ov = _ramp((rk - dk) / max(rk, 1e-6))
                    if ov > best:
                        best, best_src = ov, 1
            occ[j] = best
            blocker[j] = best_src if best > 0 else -1
        return 1.0 - occ, blocker

    def _g_tmp(self, p, dt):
        if self._p_prev is None or dt <= 0:
            return np.ones(N_JOINTS)
        v = (p - self._p_prev) / dt
        sp = np.linalg.norm(v, axis=1)
        g_spd = _ramp(1.0 - (sp - self.v_max) / self.v_max)      # 1 up to v_max, 0 by 2*v_max
        if self._v_prev is not None:
            a = np.linalg.norm((v - self._v_prev) / dt, axis=1)
            g_acc = _ramp(1.0 - (a - self.a_max) / self.a_max)
        else:
            g_acc = np.ones(N_JOINTS)
        # frozen-inference: a joint pinned in place while the hand overall moves
        hand_moving = np.median(sp) > self.hand_move_eps
        self._freeze = np.where(sp < self.freeze_eps, self._freeze + 1, 0)
        g_frz = np.where((self._freeze >= self.freeze_n) & hand_moving, 0.0, 1.0)
        self._v_prev = v
        return g_spd * g_acc * g_frz

    # ── main step ─────────────────────────────────────────────────────────────
    def update(self, p_opt, cam_pos, cam_R, is_active=True, valid_mask=None,
               radius=None, dt=1 / 60.0, geom_pos=None,
               other_pos=None, other_radius=None):
        """p_opt: (26,3) optical joint positions (world). geom_pos: (26,3) reference
        geometry to CAST THE OCCLUSION RAY AGAINST — pass the fused / last-confident
        pose here (the optical is exactly what's unreliable during occlusion, so
        casting against it is counterproductive; defaults to p_opt if omitted).
        other_pos: (26,3) the OTHER hand's reference geometry (its last-confident
        pose), so inter-hand occlusion is attributed to it. Returns per-joint c_j
        (raw & smoothed), per-finger c_f/beta_f, and occlusion provenance."""
        p_opt = np.asarray(p_opt, float).reshape(N_JOINTS, 3)
        radius = (np.full(N_JOINTS, self.joint_radius) if radius is None
                  else np.asarray(radius, float))
        occ_pos = p_opt if geom_pos is None else np.asarray(geom_pos, float).reshape(N_JOINTS, 3)
        other_pos = (None if other_pos is None
                     else np.asarray(other_pos, float).reshape(N_JOINTS, 3))
        h = 1.0 if is_active else 0.0
        if valid_mask is not None:      # coarse OpenXR whole-hand kill (wrist/palm valid)
            if not (valid_mask[0] and valid_mask[1]):
                h = 0.0

        g_fov = self._g_fov(p_opt, cam_pos, cam_R)
        g_occ, blocker = self._g_occ(occ_pos, cam_pos, radius, other_pos, other_radius)
        g_tmp = self._g_tmp(p_opt, dt)
        c = h * g_fov * g_occ * g_tmp                       # per-joint confidence

        lam = np.exp(-dt / max(self.tau_s, 1e-6))
        self._cbar = lam * self._cbar + (1 - lam) * c       # EMA smoothing
        self._p_prev = p_opt

        c_f, beta_f = {}, {}
        for f, js in FINGER_JOINTS.items():
            w = self._fw[f]
            cf = float((self._cbar[js] * w).sum() / w.sum())      # distal-weighted mean
            c_f[f] = cf
            beta_f[f] = float(_ramp((cf - self.tau_lo) / (self.tau_hi - self.tau_lo)))

        # Occlusion provenance: of the joints actually occluded (g_occ < 0.5),
        # what fraction is blocked by this hand vs the other. Drives the type.
        occluded = g_occ < 0.5
        frac_self = float(np.mean(occluded & (blocker == 0)))
        frac_other = float(np.mean(occluded & (blocker == 1)))
        return {
            "c_j": c, "c_j_smooth": self._cbar.copy(),
            "g_fov": g_fov, "g_occ": g_occ, "g_tmp": g_tmp,
            "blocker": blocker, "occ_by_self": frac_self, "occ_by_other": frac_other,
            "fov_mean": float(np.mean(g_fov)),
            "c_f": c_f, "beta_f": beta_f,
            "calib_mask": self._cbar > self.tau_cal,          # gate for the online-calib buffer
        }

    @staticmethod
    def blend(angles_opt, angles_fus, beta_f):
        """Per-finger convex blend of joint angles: beta*optical + (1-beta)*fused."""
        out = {}
        for f in angles_opt:
            b = beta_f[f]
            out[f] = b * np.asarray(angles_opt[f]) + (1 - b) * np.asarray(angles_fus[f])
        return out
