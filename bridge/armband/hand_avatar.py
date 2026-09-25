#!/usr/bin/env python3
"""
MindRove EMG -> 3D Hand Avatar  (per-finger, calibrated)
========================================================

Drives a 3D hand from the armband. A short calibration (flex each finger once)
builds a linear EMG->finger decoder; the hand then flexes per finger, live.

Rendering
  * live    : realistic 3D hand in an OpenGL window (pyqtgraph.opengl)
  * preview : static matplotlib PNG of sample poses (no Qt) — to check geometry

Data source is shared with the dashboard (armband / synthetic / sim).

Usage
  python hand_avatar.py --preview poses.png              # no hardware: render poses
  python hand_avatar.py --source armband --calibrate     # calibrate, then run live
  python hand_avatar.py --source armband                 # live with saved calibration
  python hand_avatar.py --source sim                      # live demo (auto-animates)

Windows deps for the live OpenGL view:
  pip install numpy matplotlib mindrove pyqtgraph PyOpenGL PySide6
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
FINGER_COLORS = {
    "thumb": (0.902, 0.404, 0.404, 1.0),   # red
    "index": (0.224, 0.529, 0.898, 1.0),   # blue
    "middle": (0.098, 0.620, 0.439, 1.0),  # green
    "ring": (0.788, 0.522, 0.0, 1.0),      # amber
    "pinky": (0.835, 0.318, 0.506, 1.0),   # magenta
}
SKIN = (0.86, 0.72, 0.62, 1.0)


# ----------------------------------------------------------------------------
# math helpers
# ----------------------------------------------------------------------------
def _rot(v, axis, deg):
    """Rodrigues rotation of vector v about `axis` by `deg` degrees."""
    a = np.radians(deg)
    axis = np.asarray(axis, float)
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    c, s = np.cos(a), np.sin(a)
    v = np.asarray(v, float)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1 - c)


def _rodrigues_matrix(axis, ang):
    x, y, z = axis
    c, s = np.cos(ang), np.sin(ang)
    C = 1 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


# ----------------------------------------------------------------------------
# Hand model (procedural right-hand skeleton + forward kinematics)
# ----------------------------------------------------------------------------
class HandModel:
    """Right hand, palm-down: fingers extend +Y, palm back faces +Z, curl -> +Z."""

    def __init__(self):
        self.wrist = np.array([0.0, -2.5, 0.0])
        base_x = {"index": 2.4, "middle": 0.8, "ring": -0.8, "pinky": -2.3}
        top_y = {"index": 6.1, "middle": 6.6, "ring": 6.2, "pinky": 5.5}
        lengths = {"index": [3.9, 2.3, 1.7], "middle": [4.3, 2.7, 1.8],
                   "ring": [3.9, 2.5, 1.7], "pinky": [3.1, 1.8, 1.4]}
        splay = {"index": -7, "middle": -2, "ring": 4, "pinky": 11}  # deg about Z
        radius = {"index": 0.62, "middle": 0.66, "ring": 0.60, "pinky": 0.52}
        self.defs = {}
        for f in ["index", "middle", "ring", "pinky"]:
            bd = _rot([0, 1.0, 0], [0, 0, 1.0], splay[f])
            self.defs[f] = dict(base=np.array([base_x[f], top_y[f], 0.0]),
                                bdir=bd, axis=np.array([1.0, 0, 0]),
                                L=lengths[f], m=[88, 105, 78], r=radius[f])
        # thumb: radial side, angled across the palm; two flexing joints (+ metacarpal)
        tb = _rot([0, 1.0, 0], [0, 0, 1.0], 58)
        self.defs["thumb"] = dict(
            base=np.array([3.7, 0.4, 0.5]),
            bdir=tb, axis=_rot([1.0, 0, 0], [0, 0, 1.0], 58),
            L=[3.3, 2.6, 2.0], m=[35, 55, 55], r=0.72)
        self.order = ["thumb", "index", "middle", "ring", "pinky"]

    def fk(self, flex):
        """flex: dict finger->[0..1]. Returns dict finger->(4,3) joint points."""
        out = {}
        for f, d in self.defs.items():
            fv = float(np.clip(flex.get(f, 0.0), 0.0, 1.0))
            pts = [d["base"].copy()]
            cum = 0.0
            for i, L in enumerate(d["L"]):
                cum += fv * d["m"][i]
                dirv = _rot(d["bdir"], d["axis"], cum)
                pts.append(pts[-1] + dirv * L)
            out[f] = np.array(pts)
        return out

    def palm_polygon(self, joints):
        order = ["index", "middle", "ring", "pinky"]
        top = [joints[f][0] for f in order]
        wl = self.wrist + np.array([3.0, 0, 0])
        wr = self.wrist + np.array([-3.2, 0, 0])
        thumb_b = self.defs["thumb"]["base"]
        return np.array([wr, top[3], top[2], top[1], top[0], thumb_b, wl])


# ----------------------------------------------------------------------------
# matplotlib preview (verify geometry without Qt/OpenGL)
# ----------------------------------------------------------------------------
def preview(path, poses=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection

    model = HandModel()
    poses = poses or {
        "OPEN": {f: 0.0 for f in FINGERS},
        "FIST": {f: 1.0 for f in FINGERS},
        "POINT": {"index": 0.0, "middle": 1.0, "ring": 1.0, "pinky": 1.0, "thumb": 0.7},
        "PINCH": {"index": 0.55, "thumb": 0.75, "middle": 0.05, "ring": 0.05, "pinky": 0.05},
    }
    fig = plt.figure(figsize=(14, 4.2), facecolor="#0d0d0d")
    for k, (name, flex) in enumerate(poses.items()):
        ax = fig.add_subplot(1, len(poses), k + 1, projection="3d")
        ax.set_facecolor("#0d0d0d")
        J = model.fk(flex)
        ax.add_collection3d(Poly3DCollection(
            [model.palm_polygon(J)], facecolor="#20304a",
            edgecolor="#3987e5", linewidths=1.2, alpha=0.85))
        segs, cols = [], []
        for f in FINGERS:
            p = J[f]
            for i in range(3):
                segs.append([p[i], p[i + 1]])
                cols.append(FINGER_COLORS[f][:3])
            ax.scatter(p[:, 0], p[:, 1], p[:, 2],
                       color=[FINGER_COLORS[f][:3]], s=28, depthshade=True)
        ax.add_collection3d(Line3DCollection(segs, colors=cols, linewidths=7))
        ax.set_title(name, color="#ffffff", fontsize=12, fontweight="bold", pad=0)
        _style3d(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor="#0d0d0d")
    print("wrote", path)


def _style3d(ax):
    ax.set_xlim(-4.5, 4.5); ax.set_ylim(-3.5, 9.5); ax.set_zlim(-5.5, 6.5)
    ax.set_box_aspect((9, 13, 12))
    ax.view_init(elev=16, azim=-72)
    ax.set_axis_off()


# ----------------------------------------------------------------------------
# EMG activation + calibration + decoder
# ----------------------------------------------------------------------------
class Activation:
    """Rolling per-channel RMS of the filtered EMG (muscle-effort vector)."""

    def __init__(self, src, win_s=0.25):
        from emg_dashboard import make_emg_filter
        self.src = src
        self.n = len(src.emg_idx)
        self.N = max(1, int(win_s * src.fs))
        self.buf = np.zeros((self.n, self.N))
        self.filt = make_emg_filter(self.n, src.fs)

    def update(self):
        blk = self.src.read()
        if blk.shape[1]:
            emg = self.filt.process(blk[self.src.emg_idx, :])
            m = emg.shape[1]
            if m >= self.N:
                self.buf[:] = emg[:, -self.N:]
            else:
                self.buf[:, :-m] = self.buf[:, m:]
                self.buf[:, -m:] = emg
        return np.sqrt(np.mean(self.buf ** 2, axis=1))


def _wait_ready(src, timeout=20.0):
    """Block until the source is actually streaming (or a demo). False on timeout."""
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        if getattr(src, "state", "live") in ("live", "demo"):
            return True
        time.sleep(0.15)
    return False


def _no_signal_msg():
    print("\n[X] No live EMG signal. Check that:")
    print("    - the armband is ON and Windows is joined to its Wi-Fi, and")
    print("    - NO other program is using it (close the dashboard window, the")
    print("      MindRove Connect/Visualizer app, or other terminals).")
    print("    Only ONE program can use the armband at a time.")


def calibrate(src, path, per_finger_s=4.0, rest_s=3.0):
    """Guided calibration: relax, then flex each finger. Saves an npz decoder."""
    src.start()
    print("Connecting to the armband...")
    if not _wait_ready(src):
        _no_signal_msg(); src.stop(); return
    act = Activation(src)
    t0 = time.perf_counter()               # warm up the filter briefly
    while time.perf_counter() - t0 < 0.5:
        act.update(); time.sleep(0.03)

    def collect(sec):
        vals = []
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            vals.append(act.update())
            time.sleep(0.03)
        return np.mean(vals, axis=0)

    print("\n=== Hand calibration ===")
    input("1) RELAX your hand completely, then press Enter...")
    base = collect(rest_s)
    T = []
    for f in FINGERS:
        input(f"2) Get ready to repeatedly flex ONLY your {f.upper()} — press Enter, "
              f"then flex it for ~{per_finger_s:.0f}s...")
        v = np.clip(collect(per_finger_s) - base, 0, None)
        T.append(v)
        print(f"   captured {f}: activation sum = {v.sum():.0f} µV")
    T = np.array(T).T  # (n_emg, 5)
    np.savez(path, T=T, base=base, fingers=np.array(FINGERS))
    src.stop()
    print(f"\nSaved calibration -> {path}\n")


class Decoder:
    """Linear EMG->finger decoder from a saved calibration (pinv of templates)."""

    def __init__(self, path):
        d = np.load(path, allow_pickle=True)
        self.T = d["T"].astype(float)          # (n_emg, 5)
        self.base = d["base"].astype(float)
        self.fingers = [str(x) for x in d["fingers"]]
        self.M = np.linalg.pinv(self.T)        # (5, n_emg)
        self.f = np.zeros(len(self.fingers))
        self.alpha = 0.35                       # EMA smoothing

    def decode(self, rms):
        a = np.clip(rms - self.base, 0, None)
        raw = np.clip(self.M @ a, 0, 1.4)
        self.f = (1 - self.alpha) * self.f + self.alpha * raw
        return {fn: float(np.clip(self.f[i], 0, 1)) for i, fn in enumerate(self.fingers)}


class GripFallback:
    """Whole-hand open/close from overall effort, AUTO-scaled to your signal.

    Tracks a slow floor (rest) and a slow-decaying peak (max clench) so the
    hand spans fully open<->closed after you relax and clench once — no
    hardcoded thresholds. Reliable when per-finger decoding is too noisy.
    """

    def __init__(self):
        self.lo = None
        self.hi = None
        self.f = 0.0

    def decode(self, rms):
        m = float(np.mean(rms))
        if self.lo is None:
            self.lo, self.hi = m, m + 1.0
        # floor snaps down to new minima, drifts up slowly; peak snaps up, decays slowly
        self.lo = m if m < self.lo else self.lo + 0.02
        self.hi = m if m > self.hi else self.hi - 0.15
        rng = max(self.hi - self.lo, 1e-3)
        g = np.clip((m - self.lo) / rng, 0.0, 1.0)
        self.f = 0.55 * self.f + 0.45 * g
        return {fn: float(self.f) for fn in FINGERS}


# ----------------------------------------------------------------------------
# Gesture recognition (EMG-only: you label by performing named poses)
# ----------------------------------------------------------------------------
# Each gesture maps to a target finger-flex pose for the 3D hand.
GESTURE_POSES = {
    "rest":      {"thumb": 0.05, "index": 0.05, "middle": 0.05, "ring": 0.05, "pinky": 0.05},
    "open":      {"thumb": 0.00, "index": 0.00, "middle": 0.00, "ring": 0.00, "pinky": 0.00},
    "fist":      {"thumb": 1.00, "index": 1.00, "middle": 1.00, "ring": 1.00, "pinky": 1.00},
    "point":     {"thumb": 0.80, "index": 0.00, "middle": 1.00, "ring": 1.00, "pinky": 1.00},
    "pinch":     {"thumb": 1.00, "index": 0.72, "middle": 0.88, "ring": 0.92, "pinky": 0.92},
    "peace":     {"thumb": 0.90, "index": 0.00, "middle": 0.00, "ring": 1.00, "pinky": 1.00},
    "thumbs_up": {"thumb": 0.00, "index": 1.00, "middle": 1.00, "ring": 1.00, "pinky": 1.00},
}
DEFAULT_GESTURES = ["rest", "open", "fist", "point", "pinch"]


def td_feature_vector(window, zc_thr=8.0):
    """Hudgins time-domain features per channel -> flat vector.

    window: (n_ch, W) filtered EMG (microvolts). Returns (n_ch*5,) of
    [MAV, RMS, WL, ZC, SSC] per channel. Cheap, standard, robust for gestures.
    """
    feats = []
    for ch in range(window.shape[0]):
        x = window[ch].astype(float)
        dx = np.diff(x)
        mav = np.mean(np.abs(x))
        rms = np.sqrt(np.mean(x * x))
        wl = np.sum(np.abs(dx))
        zc = np.sum((np.sign(x[:-1]) != np.sign(x[1:])) & (np.abs(dx) >= zc_thr))
        ssc = np.sum(((x[1:-1] - x[:-2]) * (x[1:-1] - x[2:])) >= zc_thr)
        feats += [mav, rms, wl, float(zc), float(ssc)]
    return np.asarray(feats, dtype=float)


class GestureClassifier:
    """Regularized (shrinkage) LDA in standardized feature space — numpy only.

    The field-standard myoelectric classifier (Hudgins/Englehart, libEMG). It uses
    a pooled within-class covariance to decorrelate the strongly-correlated EMG
    features (which plain nearest-centroid ignores), shrunk toward its diagonal so
    it stays invertible with few samples.
    """

    def __init__(self, names, mu, sd, W, b):
        self.names = list(names)
        self.mu = mu
        self.sd = sd
        self.W = W        # (K, d) linear discriminant weights
        self.b = b        # (K,)  bias

    @classmethod
    def fit(cls, X, y, shrink=0.2):
        X = np.asarray(X, float)
        y = np.asarray(y)
        mu = X.mean(0)
        sd = X.std(0) + 1e-6
        Xs = (X - mu) / sd                       # z-score (persist mu/sd!)
        names = sorted(set(y.tolist()))
        d = Xs.shape[1]
        means, Sw = [], np.zeros((d, d))
        for n in names:
            Xk = Xs[y == n]
            mk = Xk.mean(0)
            means.append(mk)
            dif = Xk - mk
            Sw += dif.T @ dif
        Sw /= max(1, len(y) - len(names))         # pooled within-class covariance
        # shrink toward diagonal (Ledoit-Wolf style) so it's invertible on small n
        Sw = (1 - shrink) * Sw + shrink * np.diag(np.diag(Sw)) + 1e-6 * np.eye(d)
        Sinv = np.linalg.inv(Sw)
        means = np.array(means)
        W = means @ Sinv                          # w_k = Sinv @ mu_k
        b = -0.5 * np.sum((means @ Sinv) * means, axis=1)   # equal priors
        return cls(names, mu, sd, W, b)

    def scores(self, x):
        xs = (x - self.mu) / self.sd
        return self.W @ xs + self.b               # (K,) linear discriminants

    def posteriors(self, x):
        s = self.scores(x)
        s = s - s.max()
        e = np.exp(s)
        return e / (e.sum() + 1e-12)

    def predict(self, x):
        s = self.scores(x)
        return self.names[int(np.argmax(s))], s

    def save(self, path):
        np.savez(path, names=np.array(self.names), mu=self.mu, sd=self.sd,
                 W=self.W, b=self.b)

    @classmethod
    def load(cls, path):
        d = np.load(path, allow_pickle=True)
        return cls([str(n) for n in d["names"]], d["mu"], d["sd"], d["W"], d["b"])


def train_gestures(src, path="gesture_model.npz", gestures=None, secs=4.0):
    """Guided EMG-only gesture training: hold each pose while we record features."""
    gestures = gestures or DEFAULT_GESTURES
    src.start()
    print("Connecting to the armband...")
    if not _wait_ready(src):
        _no_signal_msg(); src.stop(); return
    act = Activation(src)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:      # warm up the filter
        act.update(); time.sleep(0.03)

    X, y = [], []
    print("\n=== Gesture training (EMG only) ===")
    print("Hold each pose steadily while it records. Keep the armband in the same")
    print("rotation you'll use later.\n")
    for g in gestures:
        input(f"  Get ready to HOLD '{g.upper()}' — press Enter, then hold ~{secs:.0f}s...")
        # let the contraction settle — skip the ramp-up so training sees steady state
        t = time.perf_counter()
        while time.perf_counter() - t < 0.5:
            act.update(); time.sleep(0.03)
        t = time.perf_counter()
        while time.perf_counter() - t < secs:
            act.update()
            X.append(td_feature_vector(act.buf.copy()))
            y.append(g)
            time.sleep(0.05)
        print(f"    captured {g}: {sum(1 for v in y if v == g)} windows")
    clf = GestureClassifier.fit(X, y)
    clf.save(path)
    src.stop()
    print(f"\nSaved gesture model -> {path}  ({len(gestures)} gestures, {len(X)} samples)\n")


class GesturePredictor:
    """Live gesture -> smoothed hand pose, with majority vote + confidence reject.

    decode_window() takes an EMG window; low-confidence windows fall back to rest
    so a relaxed/ambiguous hand doesn't fire a random gesture.
    """

    def __init__(self, path, vote=5, alpha=0.55, min_conf=0.3):
        from collections import deque
        self.clf = GestureClassifier.load(path)
        self.votes = deque(maxlen=vote)
        self.flex = {f: 0.0 for f in FINGERS}
        self.alpha = alpha
        self.min_conf = min_conf
        self._rest = "rest" if "rest" in self.clf.names else self.clf.names[0]
        self.current = self._rest

    def decode_window(self, window):
        return self.decode(td_feature_vector(window))

    def decode(self, featurevec):
        from collections import Counter
        post = self.clf.posteriors(featurevec)
        k = int(np.argmax(post))
        name = self.clf.names[k] if post[k] >= self.min_conf else self._rest
        self.votes.append(name)
        self.current = Counter(self.votes).most_common(1)[0][0]
        target = GESTURE_POSES.get(self.current, GESTURE_POSES["rest"])
        for f in FINGERS:
            self.flex[f] = (1 - self.alpha) * self.flex[f] + self.alpha * target.get(f, 0.0)
        return self.current, dict(self.flex)


# ----------------------------------------------------------------------------
# OpenGL live view (pyqtgraph)
# ----------------------------------------------------------------------------
def _bone_transform(p0, p1):
    """4x4 that maps a unit +Z cylinder (len 1) to span p0->p1."""
    d = np.asarray(p1) - np.asarray(p0)
    L = float(np.linalg.norm(d)) + 1e-9
    dz = d / L
    zc = np.array([0, 0, 1.0])
    axis = np.cross(zc, dz)
    s = np.linalg.norm(axis)
    if s < 1e-8:
        R = np.eye(3) if dz[2] >= 0 else np.diag([1.0, -1.0, -1.0])
    else:
        R = _rodrigues_matrix(axis / s, np.arccos(np.clip(dz[2], -1, 1)))
    M = np.eye(4)
    M[:3, :3] = R @ np.diag([1.0, 1.0, L])
    M[:3, 3] = p0
    return M


def live_gl(src, calib_path, auto_demo=False):
    try:
        import pyqtgraph as pg
        import pyqtgraph.opengl as gl
        from pyqtgraph.Qt import QtCore
    except ImportError:
        print("\n[i] The standalone 3D OpenGL window needs extra libraries:")
        print("      pip install pyqtgraph PyOpenGL PySide6")
        if calib_path and os.path.exists(calib_path):
            print("\n    Your calibration is saved. To see the hand INSIDE the "
                  "dashboard\n    (no OpenGL needed), just run:")
            print("      py emg_dashboard.py --source armband --hand")
        return

    app = pg.mkQApp("EMG Hand Avatar")
    view = gl.GLViewWidget()
    view.setWindowTitle("MindRove EMG — 3D Hand Avatar")
    view.setCameraPosition(distance=32, elevation=18, azimuth=-82)
    try:
        view.setBackgroundColor(pg.mkColor("#0d0d0d"))
    except Exception:
        pass
    view.resize(900, 800)
    view.show()

    model = HandModel()

    # one cylinder per bone (3 per finger) + one sphere per joint (4 per finger)
    bones, joints = [], []
    for f in FINGERS:
        r = model.defs[f]["r"]
        col = FINGER_COLORS[f]
        for _ in range(3):
            md = gl.MeshData.cylinder(rows=1, cols=16, radius=[r, r * 0.85], length=1.0)
            it = gl.GLMeshItem(meshdata=md, smooth=True, color=col,
                               shader="shaded", glOptions="opaque")
            view.addItem(it); bones.append((f, it))
        for _ in range(4):
            md = gl.MeshData.sphere(rows=10, cols=16, radius=r * 1.05)
            it = gl.GLMeshItem(meshdata=md, smooth=True, color=col,
                               shader="shaded", glOptions="opaque")
            view.addItem(it); joints.append(it)

    # palm slab (a flattened ellipsoid)
    palm_md = gl.MeshData.sphere(rows=10, cols=20, radius=1.0)
    palm = gl.GLMeshItem(meshdata=palm_md, smooth=True, color=SKIN,
                         shader="shaded", glOptions="opaque")
    Mp = np.eye(4)
    Mp[:3, :3] = np.diag([3.3, 4.6, 1.2])
    Mp[:3, 3] = np.array([0.0, 2.0, 0.0])
    palm.setTransform(pg.Transform3D(Mp))
    view.addItem(palm)

    src.start()
    act = Activation(src)
    dec = Decoder(calib_path) if (calib_path and os.path.exists(calib_path)) else GripFallback()
    if isinstance(dec, GripFallback) and not auto_demo:
        print("[i] No calibration found — running whole-hand GRIP mode. "
              "Run with --calibrate for per-finger control.")

    state = {"t": 0.0}

    def update():
        if auto_demo:
            state["t"] += 0.033
            flex = _demo_pose(state["t"])
        else:
            flex = dec.decode(act.update())
        J = model.fk(flex)
        bi = ji = 0
        for f in FINGERS:
            p = J[f]
            for k in range(3):
                bones[bi][1].setTransform(pg.Transform3D(_bone_transform(p[k], p[k + 1])))
                bi += 1
            for k in range(4):
                Ms = np.eye(4); Ms[:3, 3] = p[k]
                joints[ji].setTransform(pg.Transform3D(Ms))
                ji += 1

    timer = QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(33)
    update()
    pg.exec()
    src.stop()


def _demo_pose(t):
    """A looping wave/fist animation so `--source sim` shows motion."""
    out = {}
    for i, f in enumerate(FINGERS):
        out[f] = 0.5 + 0.5 * np.sin(2 * np.pi * 0.3 * t - i * 0.9)
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="MindRove EMG -> 3D hand avatar")
    p.add_argument("--source", choices=["armband", "synthetic", "sim"], default="armband")
    p.add_argument("--calibrate", action="store_true",
                   help="run the per-finger calibration before the live view")
    p.add_argument("--calib", default="hand_calib.npz", help="calibration file path")
    p.add_argument("--train-gestures", action="store_true", dest="train_gestures",
                   help="record named poses and train an EMG-only gesture classifier")
    p.add_argument("--gesture-model", default="gesture_model.npz", dest="gesture_model",
                   help="gesture model file path")
    p.add_argument("--gestures", default="", help="comma-separated gesture names to train")
    p.add_argument("--preview", metavar="PATH",
                   help="render sample poses to a PNG (matplotlib) and exit")
    p.add_argument("--fs", type=int, default=500, help="sample rate for --source sim")
    args = p.parse_args(argv)

    if args.preview:
        preview(args.preview)
        return

    from emg_dashboard import SimSource, MindRoveSource
    if args.source == "sim":
        src = SimSource(fs=args.fs)
    else:
        src = MindRoveSource(synthetic=(args.source == "synthetic"))

    if args.train_gestures:
        gestures = [g.strip() for g in args.gestures.split(",") if g.strip()] or None
        train_gestures(src, args.gesture_model, gestures=gestures)
        return

    if args.calibrate:
        calibrate(src, args.calib)

    # sim has no real fingers -> auto-demo animation; real/synthetic -> decode EMG
    live_gl(src, args.calib, auto_demo=(args.source == "sim" and not os.path.exists(args.calib)))


if __name__ == "__main__":
    main()
