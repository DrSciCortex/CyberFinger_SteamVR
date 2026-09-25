# SPDX-License-Identifier: GPL-3.0-only
"""Camera-taught MOUNTING calibration of a glove IMU (numpy only).

The bridge's OrientationFuser holds a ONE-sided offset  R_hand = R_off · R_imu.  That form absorbs the sensor's mounting
on the world side, so it is exact only at the pose where it was set and degrades as the segment rotates (measured on the
recorded clips: the knuckle sensor drifts 30–50° within 3 s of losing the camera, > 90° in 10–20 % of cases — the
"hand folded back onto the forearm" picture).  A sensor strapped to a segment needs the TWO-sided form

        R_seg(t) = Ryaw(α) · C · R_imu(t) · M

with M the fixed sensor→segment mounting (constant while the strap stays put), C the IMU-world (z-up) → XR-world (y-up)
relabel and α the sensor's arbitrary heading (6-axis IMU, no magnetometer; drifts ~1°/min).  While the camera sees the
hand, (α, M) are solved from a rolling window of (R_imu, R_camera) pairs: for each heading on a grid, M is the Procrustes
optimum; the best heading wins.  α is then nudged continuously toward the camera; M is kept.  Measured on the clips
(self-test below): the knuckle sensor's held estimate stays within ~5–14° (median) of the camera hand frame for minutes.

For the WRIST sensor the same fit against the camera HAND frame yields the forearm frame centred on the mean wrist
posture: column 0 = forearm axis (≈ elbow → wrist).  Angle between the knuckle hand frame and that axis = live wrist bend.

    python mount_calib.py            # causal evaluation on bridge/position_data/pos_*.npz
"""
import math

import numpy as np

C_IMU_TO_XR = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
WINDOW_S, SAMPLE_S, SOLVE_EVERY_S, MIN_PAIRS, MIN_SPREAD_DEG = 90.0, 0.1, 3.0, 40, 12.0


def ryaw(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def heading(v):
    return math.atan2(float(v[0]), float(v[2]))


def wrap(a):
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def quat_wxyz_to_R(q):
    w, x, y, z = [float(v) for v in q]
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def rot_angle_deg(A, B):
    """Angle of the rotation taking frame A to frame B (both 3×3)."""
    c = (float(np.trace(A.T @ B)) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _procrustes(A, B):
    """M = argmin Σ‖A_i M − B_i‖  over rotations (A, B: n×3×3)."""
    H = np.einsum("nji,njk->ik", A, B)
    U, S, Vt = np.linalg.svd(H)
    D = np.diag([1.0, 1.0, float(np.sign(np.linalg.det(U @ Vt)) or 1.0)])
    return U @ D @ Vt, S


def _mean_angle(est, tgt):
    c = (np.einsum("nii->n", np.einsum("nji,njk->nik", est, tgt)) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))).mean())


class MountCalib:
    """One sensor. add() pairs while the camera sees the segment, solve() now and then, estimate() any time."""

    def __init__(self, window_s=WINDOW_S):
        self.window_s = float(window_s)
        self.buf = []                     # (t, R_imu, R_cam)
        self.alpha = None; self.M = None
        self.fit_deg = None               # mean residual of the last solve
        self.resid = None                 # EMA of the live residual (deg) while the camera sees the segment
        self.n_pairs = 0; self.span_s = 0.0; self.spread_deg = 0.0
        self._t_add = None; self._t_solve = None; self._dirty = False

    @property
    def ready(self):
        return self.M is not None and self.alpha is not None

    def reset(self):
        self.__init__(self.window_s)

    def add(self, t, R_imu, R_cam):
        if self._t_add is not None and t - self._t_add < SAMPLE_S:
            return
        self._t_add = t; self.buf.append((float(t), np.asarray(R_imu, float), np.asarray(R_cam, float))); self._dirty = True
        while self.buf and self.buf[0][0] < t - self.window_s:
            self.buf.pop(0)

    def solve(self, t, force=False):
        """→ True when a new (α, M) was accepted."""
        if not force and (not self._dirty or (self._t_solve is not None and t - self._t_solve < SOLVE_EVERY_S)):
            return False
        self._t_solve = t; self._dirty = False
        if len(self.buf) < MIN_PAIRS:
            return False
        Ri = np.array([b[1] for b in self.buf]); Rc = np.array([b[2] for b in self.buf])
        # variety check: the sensor must have rotated, else (α, M) trade off
        f = Ri[:, :, 0]; m = f.mean(0); nm = float(np.linalg.norm(m)) or 1.0
        spread = float(np.degrees(np.arccos(np.clip(f @ (m / nm), -1.0, 1.0))).std())
        best = None
        grid = (self.alpha + np.radians(np.arange(-10.0, 10.5, 2.0))) if self.ready else np.radians(np.arange(0.0, 360.0, 6.0))
        for a in grid:
            A = np.einsum("ij,njk->nik", ryaw(a) @ C_IMU_TO_XR, Ri); M, _ = _procrustes(A, Rc)
            r = _mean_angle(np.einsum("nij,jk->nik", A, M), Rc)
            if best is None or r < best[0]:
                best = (r, a, M)
        r0, a0, _ = best
        for a in a0 + np.radians(np.arange(-5.0, 5.5, 1.0)):
            A = np.einsum("ij,njk->nik", ryaw(a) @ C_IMU_TO_XR, Ri); M, _ = _procrustes(A, Rc)
            r = _mean_angle(np.einsum("nij,jk->nik", A, M), Rc)
            if r < best[0]:
                best = (r, a, M)
        r, a, M = best
        self.n_pairs = len(self.buf); self.span_s = self.buf[-1][0] - self.buf[0][0]; self.spread_deg = spread
        if spread < MIN_SPREAD_DEG and self.ready:
            return False                                     # keep the earlier, better-conditioned solution
        self.fit_deg = r
        if self.alpha is not None:                           # keep the live-nudged heading continuous
            a = self.alpha + wrap(a - self.alpha)
        self.alpha, self.M = float(a), M
        return True

    def estimate(self, R_imu):
        if not self.ready:
            return None
        return ryaw(self.alpha) @ C_IMU_TO_XR @ np.asarray(R_imu, float) @ self.M

    def nudge(self, R_imu, R_cam, gain):
        """Camera visible: turn the heading a fraction `gain` toward the camera frame, update the residual EMA."""
        est = self.estimate(R_imu)
        if est is None:
            return
        Rc = np.asarray(R_cam, float); e = rot_angle_deg(est, Rc)
        self.resid = e if self.resid is None else (1.0 - gain) * self.resid + gain * e
        # yaw (about world +y) that best turns the estimate onto the camera frame, from the whole frame — well defined
        # whatever the hand points at (a single axis' heading is meaningless when that axis is near vertical)
        E = Rc @ est.T
        self.alpha = wrap(self.alpha + gain * math.atan2(float(E[0, 2] - E[2, 0]), float(E[0, 0] + E[2, 2])))

    def status(self):
        if not self.ready:
            return f"learning the mount ({self.n_pairs} pairs)" if self.buf else "no camera pairs yet"
        return f"mount fit {self.fit_deg:.0f}° · live residual {self.resid:.0f}°" if self.resid is not None else f"mount fit {self.fit_deg:.0f}°"


# ─────────────────────────────────────────── self-test / evaluation ───────────────────────────────────────────
def _hand_frame(Pi):
    x = Pi[12] - Pi[1]; x /= np.linalg.norm(x) or 1.0
    n = np.cross(Pi[7] - Pi[1], Pi[22] - Pi[1]); n -= x * (n @ x); n /= np.linalg.norm(n) or 1.0
    return np.stack([x, np.cross(n, x), n], 1)


def _evaluate(paths, horizons=(1.0, 3.0, 7.0, 15.0, 30.0), loss_every=5.0):
    """Causal: walk each clip; every `loss_every` s pretend the camera is lost, hold (α, M) and score the held knuckle
    hand frame and the one-sided held offset (R_off = R_cam·R_imuᵀ at the loss) against the camera truth."""
    pooled = {h: {"mount": [], "onesided": []} for h in horizons}; bend = []
    for p in paths:
        d = np.load(p, allow_pickle=False); P = np.asarray(d["P"], float); imu = np.asarray(d["imu"], float); t = d["t"] - d["t"][0]
        gj, g2 = imu[:, 9:13], imu[:, 5:9]
        ok = np.isfinite(P).all(axis=(1, 2)) & np.isfinite(gj).all(1) & np.isfinite(g2).all(1)
        Rc = [(_hand_frame(P[i]) if ok[i] else None) for i in range(len(t))]
        Rk = [(quat_wxyz_to_R(gj[i]) if ok[i] else None) for i in range(len(t))]
        Rw = [(quat_wxyz_to_R(g2[i]) if ok[i] else None) for i in range(len(t))]
        mk, mw = MountCalib(), MountCalib(); next_loss = 20.0; res = {h: {"mount": [], "onesided": []} for h in horizons}; bends = []
        for i in range(len(t)):
            if not ok[i]:
                continue
            dt = float(t[i] - t[i - 1]) if i else 0.03
            mk.add(t[i], Rk[i], Rc[i]); mw.add(t[i], Rw[i], Rc[i]); mk.solve(t[i]); mw.solve(t[i])
            g = 1.0 - math.exp(-max(dt, 1e-3) / 2.0); mk.nudge(Rk[i], Rc[i], g); mw.nudge(Rw[i], Rc[i], g)
            if mk.ready and mw.ready:
                hk = mk.estimate(Rk[i]); fw = mw.estimate(Rw[i])[:, 0]
                bends.append(math.degrees(math.acos(max(-1.0, min(1.0, float(hk[:, 0] @ fw))))))
            if t[i] >= next_loss and mk.ready and mk.resid is not None and mk.resid < 25.0:
                next_loss = t[i] + loss_every; Roff = Rc[i] @ Rk[i].T
                for h in horizons:
                    j = int(np.searchsorted(t, t[i] + h))
                    if j < len(t) and ok[j]:
                        res[h]["mount"].append(rot_angle_deg(mk.estimate(Rk[j]), Rc[j]))
                        res[h]["onesided"].append(rot_angle_deg(Roff @ Rk[j], Rc[j]))
        name = p.split("/")[-1][4:-4]
        print(f"{name:28s} knuckle {mk.status():40s} · wrist {mw.status():36s} · live bend med {np.median(bends) if bends else float('nan'):4.0f}° 90th {np.percentile(bends, 90) if bends else float('nan'):4.0f}°")
        for h in horizons:
            for k in ("mount", "onesided"):
                pooled[h][k] += res[h][k]
    print("\nknuckle hand frame vs camera truth after the camera is lost (pooled over clips; median / 90th / % > 60°):")
    print(f"   {'after':8s}{'mount (camera-taught, held)':34s}{'one-sided offset held (current fuser form)':44s}n")
    for h in horizons:
        a = np.array(pooled[h]["mount"]); b = np.array(pooled[h]["onesided"])
        if len(a):
            print(f"   {h:5.0f} s  {np.median(a):5.1f} / {np.percentile(a, 90):5.1f} / {np.mean(a > 60) * 100:3.0f} %{'':13s}{np.median(b):5.1f} / {np.percentile(b, 90):5.1f} / {np.mean(b > 60) * 100:3.0f} %{'':20s}{len(a)}")


if __name__ == "__main__":
    import glob
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(here, "position_data", "pos_*.npz")))
    _evaluate(paths)
