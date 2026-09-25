# SPDX-License-Identifier: GPL-3.0-only
"""Hybrid wrist position for when the camera cannot see the hand ('Hybrid Position' tab) — numpy only.

Idea. The wrist is  shoulder + elbow offset + L_f·f  and the forearm direction f comes straight from the wrist IMU, so the
forearm's part of any motion is EXACT forward kinematics. The only thing nobody measures is how the ELBOW moved since
the camera last saw the hand. So, per anchor a (a camera-visible frame) and current frame j:

    base(a→j) = Wb[a] + L_f·(f_j − f_a)                 FK with the elbow held where the camera implied it
    geo(a→j)  = base(a→j) + MLP(x_aj)                   small nets predict ONLY the elbow's change since the anchor

  * two net families, averaged: A in the body frame ('Set forward'), C in an anchor-heading frame that needs no body
    heading at all; 3 seeds each; a variant without the armband accelerometer is used when the band is not streaming
  * MULTI-ANCHOR: the frames 0.25 / 0.5 / 1 / 2 s before the loss are anchors too; weights (h/(h+δ))^4
  * STILL-ARM GATE: everything learned is multiplied by g = 1 − exp(−G/0.2), G = forearm-axis path length since the loss
    above a 0.1 rad/s floor → if the arm does not move the answer is exactly the last camera position (no drift)
  * never reads where the head is LOOKING (only the neck pivot position), so turning the head away changes nothing

Measured in the position lab (leave-one-clip-out, head turned away after the loss, mean wrist error at 0.5/1/2/4/8 s):
    this method 11.3 / 12.7 / 14.6 / 16.5 / 17.8 cm   ·   learned XYZ + anchor (Learned Position tab) 20.1 / 19.7 / 22.7 /
    24.5 / 26.1   ·   FK elbow held 18.4 / 20.4 / 26.1 / 31.5 / 36.2   ·   freeze 25.9 / 27.8 / 36.0 / 43.6 / 51.4

Frames (verified on recordings, see fk_position.py): WORLD = OpenXR y-up; glove quats [w,x,y,z]; `qoff` = the wrist fuser's
camera↔IMU offset so R(qoff ⊗ qw) has columns (wrist→knuckles ≈ forearm axis, side, normal) in WORLD; body frame rows
Bm = [right, up, forward]; neck = hmd_p + hmd_R·(0, −0.10, +0.08); Wb = Bm·(wrist − neck).
The vectorised feature code below is the SAME code the nets were trained with (train_hybrid_position.py imports it)."""
import math
import os

import numpy as np

SH = np.array([0.165, -0.19, 0.05])          # shoulder from the neck (right, up, fwd), m — a constant of the trained nets
LF = 0.26                                    # elbow → wrist, m — a constant of the trained nets
ACC_K, VEL_DT = 8, 0.15                      # armband-accel smoothing (frames) · velocity look-back (s)
DELTAS, MA_Q = (0.0, 0.25, 0.5, 1.0, 2.0), 4.0
GATE_P0, GATE_FLOOR = 0.2, 0.1               # rad of forearm-axis path · rad/s tremor floor
NECK_OFFSET = np.array([0.0, -0.10, 0.08])
SEEDS = (0, 1, 2)
KEEP_S = 4.0                                 # camera-visible history kept (s): 2 s of anchors + look-backs + margin
H_MAX = 10.0                                 # elapsed time fed to the nets is capped at the longest horizon they were trained on


def body_axes(fwd):
    f = np.array([float(fwd[0]), 0.0, float(fwd[2])]); f /= (np.linalg.norm(f) + 1e-12)
    up = np.array([0.0, 1.0, 0.0]); r = np.cross(f, up); r /= (np.linalg.norm(r) + 1e-12)
    return np.stack([r, up, f])


def qmul_v(a, b):
    aw, ax, ay, az = a.T; bw, bx, by, bz = b.T
    return np.stack([aw*bw-ax*bx-ay*by-az*bz, aw*bx+ax*bw+ay*bz-az*by, aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw], 1)


def q2R_v(q):
    q = q / (np.linalg.norm(q, axis=1, keepdims=True) + 1e-12); w, x, y, z = q.T
    R = np.empty((len(q), 3, 3))
    R[:, 0, 0] = 1-2*(y*y+z*z); R[:, 0, 1] = 2*(x*y-z*w); R[:, 0, 2] = 2*(x*z+y*w)
    R[:, 1, 0] = 2*(x*y+z*w); R[:, 1, 1] = 1-2*(x*x+z*z); R[:, 1, 2] = 2*(y*z-x*w)
    R[:, 2, 0] = 2*(x*z-y*w); R[:, 2, 1] = 2*(y*z+x*w); R[:, 2, 2] = 1-2*(x*x+y*y)
    return R


def acc_smooth(acc, k=ACC_K):
    g = np.array(acc, float); g[~np.isfinite(g)] = 0.0; cs = np.cumsum(g, 0)
    return (cs - np.concatenate([np.zeros((k, 3)), cs[:-k]])[:len(cs)]) / k


def pair_arrays(t, ok, qw, qoff, Wb, accs, Bm, S, J, sh=SH):
    """Vectorised features for pairs (S = anchor frames, J = now frames). Same code for training and inference."""
    Rh = lambda I: np.einsum("ij,njk->nik", Bm, q2R_v(qmul_v(qoff[S], qw[I])))   # calibration HELD at the anchor
    Rs, Rj = Rh(S), Rh(J); fs, fj = Rs[:, :, 0], Rj[:, :, 0]
    back = lambda I: np.minimum(np.searchsorted(t, t[I] - VEL_DT), np.maximum(I - 1, 0))
    Ks, Kj = back(S), back(J)
    dts = np.maximum(t[S]-t[Ks], 1e-3)[:, None]; dtj = np.maximum(t[J]-t[Kj], 1e-3)[:, None]
    good = (ok[Ks] & (dts[:, 0] < 0.4))[:, None]
    vws = np.where(good, (Wb[S]-Wb[Ks])/dts, 0.0)
    vfs = np.where(dts < 0.4, (fs - Rh(Ks)[:, :, 0])/dts, 0.0)
    vfj = np.where(dtj < 0.4, (fj - Rh(Kj)[:, :, 0])/dtj, 0.0)
    es = Wb[S] - LF*fs - sh
    ecl = np.einsum("nkj,nk->nj", Rs, es)                                         # e_s in the anchor's forearm frame
    h = np.minimum(np.maximum(t[J]-t[S], 0.03), H_MAX)      # the nets were trained on 0.25–10 s; never let them extrapolate in time
    return dict(fs=fs, fj=fj, es=es, h=h, vws=np.nan_to_num(vws), vfs=vfs, vfj=vfj, accs=accs[S], accj=accs[J], ecl=ecl,
                Rj=Rj, Rs=Rs, ws=Wb[S])


def canon(p):
    """Second copy of the features that does not depend on the body heading: the elbow offset is taken from the NECK and
    every vector is turned about the vertical so the horizontal neck→wrist direction AT THE ANCHOR is +z."""
    th = np.arctan2(p["ws"][:, 0], p["ws"][:, 2]); c, s_ = np.cos(th), np.sin(th); R = np.zeros((len(th), 3, 3))
    R[:, 0, 0] = c; R[:, 0, 2] = -s_; R[:, 1, 1] = 1; R[:, 2, 0] = s_; R[:, 2, 2] = c
    rot = lambda v: np.einsum("nij,nj->ni", R, v); q = dict(p)
    es0 = p["ws"] - LF * p["fs"]
    q["ecl"] = np.einsum("nkj,nk->nj", p["Rs"], es0)
    q["es"] = rot(es0); q["Rj"] = np.einsum("nij,njk->nik", R, p["Rj"])
    for k in ("fs", "fj", "vws", "vfs", "vfj"):
        q[k] = rot(p[k])
    if "y" in p:
        q["y"] = rot(p["y"])
    return q, R


def assemble(p, ec, use_acc, xp=np):
    cat = (lambda a: np.concatenate(a, 1)) if xp is np else (lambda a: xp.cat(a, 1))
    d = (p["es"]**2).sum(1, keepdims=True)**0.5
    hh = p["h"][:, None]
    x = [p["fs"], p["fj"], p["fj"]-p["fs"], p["es"], d, xp.log(hh),
         xp.clip(hh, None, 10.0)/10 if xp is np else xp.clamp(hh, max=10.0)/10, p["vws"], p["vfs"], p["vfj"], ec]
    if use_acc:
        x += [p["accs"], p["accj"]]
    return cat(x)


def mlp(Ws, x):
    for k, (W, b) in enumerate(Ws):
        x = x @ W.T + b
        if k < len(Ws) - 1:
            x = np.maximum(x, 0.0)
    return x


def load_weights(path):
    """→ {'A': {True: [net, net, net], False: [...]}, 'C': {...}}, net = [(W, b), ...]; True = with armband accel."""
    d = np.load(path, allow_pickle=False)
    out = {}
    for fam in ("A", "C"):
        out[fam] = {}
        for ua, tag in ((True, "acc"), (False, "noacc")):
            nets = []
            for i in SEEDS:
                layers = []; l = 0
                while f"{fam}_{tag}_{i}_W{l}" in d.files:
                    layers.append((np.asarray(d[f"{fam}_{tag}_{i}_W{l}"], float), np.asarray(d[f"{fam}_{tag}_{i}_b{l}"], float))); l += 1
                if layers:
                    nets.append(layers)
            out[fam][ua] = nets
    out["meta"] = {k[5:]: d[k] for k in d.files if k.startswith("meta_")}
    return out


def geo(nets, p, use_acc):
    """→ (FK elbow-held base, geo estimate) for every pair in p — body frame, neck origin."""
    ec = np.einsum("nij,nj->ni", p["Rj"], p["ecl"])
    out = np.mean([mlp(Ws, assemble(p, ec, use_acc)) for Ws in nets["A"][use_acc]], 0)
    if nets["C"][use_acc]:
        q, R = canon(p); x = assemble(q, np.einsum("nij,nj->ni", q["Rj"], q["ecl"]), use_acc)
        out = 0.5 * out + 0.5 * np.einsum("nji,nj->ni", R, np.mean([mlp(Ws, x) for Ws in nets["C"][use_acc]], 0))
    out = out * np.minimum(1.0, p["h"] / 0.25)[:, None]
    base = SH + p["es"] + LF * p["fj"]
    return base, base + out


class _OneEuro:
    """One-Euro filter on a small vector: heavy smoothing when it moves slowly, almost none when it moves fast (so little lag)."""

    def __init__(self, mincutoff, beta, dcutoff=1.0):
        self.mc, self.beta, self.dc = float(mincutoff), float(beta), float(dcutoff); self.y = None; self.dx = None

    def reset(self):
        self.y = None; self.dx = None

    def __call__(self, x, dt):
        x = np.asarray(x, float)
        if self.y is None or not np.isfinite(self.y).all():
            self.y = x.copy(); self.dx = np.zeros_like(x); return self.y.copy()
        dt = max(float(dt), 1e-3); al = lambda fc: 1.0 / (1.0 + (1.0 / (2.0 * math.pi * fc)) / dt)
        self.dx = self.dx + al(self.dc) * ((x - self.y) / dt - self.dx)
        self.y = self.y + al(self.mc + self.beta * float(np.linalg.norm(self.dx))) * (x - self.y)
        return self.y.copy()


class HybridTracker:
    """Streaming version for the GUI: call update() once per frame. While the camera sees the hand it only records; once
    the hand is lost it returns the hybrid estimate. For a LEFT arm every body-frame vector is mirrored in x (the nets were
    trained on a right arm) and the armband-free nets are used."""

    def __init__(self, nets, right=True):
        self.nets = nets; self.right = bool(right)
        # SMOOTHING (optional, default on). The forearm part of the estimate is exact and fast, so it is NOT filtered; the
        # learned ELBOW part is (a real elbow moves smoothly), plus a very light pass on the sum. On 98 unseen blind spans:
        # frame-to-frame jitter 0.57 → 0.31 cm (the camera's own track: 0.90 cm) for +0.16 cm of error (14.48 → 14.64 cm).
        self.smooth = True
        self._f_elbow = _OneEuro(1.0, 6.0); self._f_all = _OneEuro(4.0, 2.0); self._t_prev = None
        self.reset()

    def reset(self):
        self.T = []; self.QW = []; self.QOFF = []; self.WN = []; self.OK = []; self.ACC = []     # camera-visible history + lost frames
        self.s = None                      # index (into the lists) of the last camera-visible frame, once lost
        self.G = 0.0; self.axes = []       # gate: path length since the loss · (t, IMU-world sensor x axis) since the loss
        self.q_held = None
        self._f_elbow.reset(); self._f_all.reset(); self._t_prev = None

    # ── one frame ──
    def update(self, t, qw, qoff, hmd_p, hmd_R, fwd, cam_wrist=None, acc=None):
        """t seconds · qw wrist-IMU quat [w,x,y,z] or None · qoff = the wrist fuser's offset quat or None · headset pose ·
        fwd = body-forward (world, horizontal) · cam_wrist = camera wrist (world) ONLY when it is trustworthy, else None ·
        acc = armband accel (3, g) or None.  → dict(pos, base, src, g, n_anchor, lost_t) or None when nothing can be said."""
        if qw is None or hmd_p is None or hmd_R is None or fwd is None:
            return None
        qw = np.asarray(qw, float); n = float(np.linalg.norm(qw))
        if n < 1e-6 or not np.isfinite(qw).all():
            return None
        qw = qw / n
        neck = np.asarray(hmd_p, float) + np.asarray(hmd_R, float) @ NECK_OFFSET
        a3 = np.asarray(acc, float) if (acc is not None and np.isfinite(acc).all() and float(np.abs(acc).sum()) > 1e-6) else np.full(3, np.nan)
        seen = cam_wrist is not None and qoff is not None and np.isfinite(qoff).all()
        if seen:
            if self.s is not None:                                  # re-acquired → the lost span stays in the lists as not-ok frames
                self.s = None; self.G = 0.0; self.axes = []
            self.q_held = np.asarray(qoff, float) / (np.linalg.norm(qoff) + 1e-12)
            self._push(t, qw, self.q_held, np.asarray(cam_wrist, float) - neck, True, a3)
            self._trim_visible()
            return {"pos": np.asarray(cam_wrist, float), "base": None, "src": "camera", "g": 0.0, "n_anchor": 0, "lost_t": 0.0}
        if self.q_held is None or not any(self.OK):
            return None                                             # the camera has not seen the hand yet
        if self.s is None:                                          # first lost frame: latch the last camera-visible frame
            self.s = max(i for i, o in enumerate(self.OK) if o)
            self._f_elbow.reset(); self._f_all.reset(); self._t_prev = None
            self.G = 0.0
            self.axes = [(self.T[self.s], q2R_v(self.QW[self.s][None])[0][:, 0])]
        self._push(t, qw, self.q_held, np.zeros(3), False, a3)
        self._gate_step(t, qw)
        self._trim_lost()
        return self._estimate(neck, fwd)

    # ── internals ──
    def _push(self, t, qw, qoff, wn, ok, acc):
        self.T.append(float(t)); self.QW.append(qw); self.QOFF.append(qoff); self.WN.append(wn); self.OK.append(bool(ok)); self.ACC.append(acc)

    def _drop(self, k):
        for L in (self.T, self.QW, self.QOFF, self.WN, self.OK, self.ACC):
            del L[:k]
        if self.s is not None:
            self.s -= k

    def _trim_visible(self):
        k = 0
        while len(self.T) - k > ACC_K + 2 and self.T[k] < self.T[-1] - KEEP_S:
            k += 1
        if k:
            self._drop(k)

    def _trim_lost(self):
        """Keep the anchor region [.. s] intact; of the lost frames keep only the last ~1 s (look-back + accel window)."""
        first_lost = self.s + 1; keep_from = first_lost
        while len(self.T) - keep_from > ACC_K + 4 and self.T[keep_from] < self.T[-1] - 1.0:
            keep_from += 1
        if keep_from > first_lost:
            for L in (self.T, self.QW, self.QOFF, self.WN, self.OK, self.ACC):
                del L[first_lost:keep_from]

    def _gate_step(self, t, qw):
        a = q2R_v(qw[None])[0][:, 0]                                # sensor x axis in the IMU world: its path needs no calibration
        t_prev = self.axes[-1][0]
        self.axes.append((float(t), a))
        k = 0                                                       # first stored frame with time ≥ t − 0.15 s, at most the previous one
        while k < len(self.axes) - 2 and self.axes[k][0] < t - VEL_DT:
            k += 1
        rate = float(np.linalg.norm(a - self.axes[k][1])) / max(t - self.axes[k][0], 1e-3)
        if np.isfinite(rate):
            self.G += max(rate - GATE_FLOOR, 0.0) * max(t - t_prev, 0.0)
        while len(self.axes) > 3 and self.axes[1][0] < t - 2 * VEL_DT - 0.2:     # frames that can never be a look-back target again
            self.axes.pop(0)

    def _estimate(self, neck, fwd):
        t = np.asarray(self.T); ok = np.asarray(self.OK); s = self.s; j = len(t) - 1
        Bm = body_axes(fwd); M = np.diag([1.0 if self.right else -1.0, 1.0, 1.0])
        A, D = [s], [0.0]
        for d in DELTAS[1:]:
            a = min(int(np.searchsorted(t, t[s] - d)), s)
            if a >= ACC_K and ok[a] and abs(t[s] - t[a] - d) < 0.1:
                A.append(a); D.append(d)
        A = np.array(A); D = np.array(D)
        qw = np.asarray(self.QW); qoff = np.asarray(self.QOFF); acc = np.asarray(self.ACC)
        Wb = np.asarray(self.WN) @ Bm.T
        if not self.right:                                           # mirror the arm into the trained (right-arm) geometry
            Wb = Wb @ M; Bm_use = M @ Bm
        else:
            Bm_use = Bm
        idx = np.concatenate([A, [j]])
        use_acc = bool(self.right and all(np.isfinite(acc[max(0, i - ACC_K + 1): i + 1]).all()
                                          and float(np.abs(acc[max(0, i - ACC_K + 1): i + 1]).sum()) > 1e-6 for i in idx))
        p = pair_arrays(t, ok, qw, qoff, Wb, acc_smooth(acc), Bm_use, A, np.full(len(A), j))
        base, est = geo(self.nets, p, use_acc)
        h = max(float(t[j] - t[s]), 1e-3); w = (h / (h + D)) ** MA_Q; w[~np.isfinite(est).all(1)] = 0.0
        if float(w.sum()) <= 0.0 or not np.isfinite(base[0]).all():
            return None
        w /= w.sum()
        y = (w[:, None] * np.nan_to_num(est)).sum(0)
        g = 1.0 - math.exp(-self.G / GATE_P0)
        y = base[0] + g * (y - base[0])
        y_raw = y.copy()
        if self.smooth:
            dt = (float(t[j]) - self._t_prev) if self._t_prev is not None else 1.0 / 30.0
            y = self._f_all(base[0] + self._f_elbow(y - base[0], dt), dt)
        self._t_prev = float(t[j])
        to_world = lambda v: neck + Bm.T @ (M @ v if not self.right else v)
        return {"pos": to_world(y), "pos_raw": to_world(y_raw), "base": to_world(base[0]), "src": "hybrid", "g": float(g),
                "n_anchor": int(len(A)), "lost_t": float(h), "use_acc": use_acc}


def default_weights_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "hybrid_position_weights.npz")
