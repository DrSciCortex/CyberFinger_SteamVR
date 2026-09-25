# SPDX-License-Identifier: GPL-3.0-only
"""Key Postures — recognise a few IMPORTANT hand postures (open, fist, point, pinch, victory) from the forearm EMG + wrist
tilt, so they can be reconstructed exactly when the camera cannot see the hand. Numpy only.

Why a separate recogniser: the continuous EMG→curl regressor is taught by the camera, and the camera cannot label a
fist / point / victory (the curled fingers hide behind the hand) nor anything out of view. For a handful of FIXED
postures the label is simply what you hold ("record fist"), so the data can be recorded exactly where it will be used —
at the hip, at the side, behind the back — which also handles the limb-position effect (EMG changes with arm posture).

Features (43): the same Hudgins TD features (8 ch × MAV, RMS, WL, ZC, SSC) + wrist tilt R(q_wrist)ᵀ·[0,−1,0] that the
Pose Hand regressor uses, so one live feature vector feeds both. Model: class-balanced softmax regression trained on
real + physically-grounded synthetic EMG (electrode roll ±1, per-channel gain, contraction intensity, noise, tilt jitter)
— the recipe already validated in Posture Hand. A time-persistence gate turns probabilities into a stable decision.
Each posture has a TEMPLATE = per-finger curls for the anatomical hand (`_procedural_hand`); where the camera saw the
posture clearly during recording, the template is the camera's median curls instead."""
import glob
import math
import os
import time

import numpy as np

POSTURES = ("open", "fist", "point", "pinch", "victory", "other")
POSITIONS = ("front", "hip", "side", "back", "moving")
CLIP_SECONDS = 12.0
EMG_WIN = 64
AUG_WEIGHT = 0.4
TEMPLATES = {"open": (0.0, 0.0, 0.0, 0.0, 0.0), "fist": (0.90, 0.95, 0.95, 0.95, 0.90), "point": (0.85, 0.0, 0.95, 0.95, 0.95),
             "pinch": (0.60, 0.62, 0.12, 0.08, 0.05), "victory": (0.85, 0.0, 0.0, 0.95, 0.95)}
COLORS = {"open": "#00e676", "fist": "#ff9100", "point": "#448aff", "pinch": "#e6007e", "victory": "#b388ff", "other": "#888888"}
SW_TOP, SW_MARGIN, HOLD_T, OFF_P, OFF_T = 0.75, 0.25, 0.15, 0.50, 0.20
FAST_P, FAST_T = 0.97, 0.04              # a very confident leader is committed after FAST_T instead of HOLD_T (latency)


def qR(q):
    w, x, y, z = np.asarray(q, float) / (np.linalg.norm(q) + 1e-9)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def tilt(wq):
    """The wrist-tilt feature exactly as the Pose Hand regressor uses it (kept identical on purpose)."""
    return qR(wq).T @ np.array([0.0, -1.0, 0.0])


def tdfeat(E, zc_thr=8.0):
    """Hudgins TD features on raw windows (B,8,W) → (B,40); identical to hand_avatar.td_feature_vector."""
    x = np.asarray(E, float); dx = np.diff(x, axis=2)
    mav = np.abs(x).mean(2); rms = np.sqrt((x * x).mean(2)); wl = np.abs(dx).sum(2)
    zc = ((np.sign(x[:, :, :-1]) != np.sign(x[:, :, 1:])) & (np.abs(dx) >= zc_thr)).sum(2).astype(float)
    ssc = (((x[:, :, 1:-1] - x[:, :, :-2]) * (x[:, :, 1:-1] - x[:, :, 2:])) >= zc_thr).sum(2).astype(float)
    return np.stack([mav, rms, wl, zc, ssc], 2).reshape(len(x), -1)


# ── clips ────────────────────────────────────────────────────────────────────────────────────────────────────────
def save_clip(folder, posture, position, buf):
    """buf: dict of lists t, X(40), E(8,64), wq(4), kq(4), acc(3), C, local(26,3). → path."""
    os.makedirs(folder, exist_ok=True)
    fn = os.path.join(folder, f"kp_{posture}_{position}_{time.strftime('%Y%m%d_%H%M%S')}.npz")
    f32 = np.float32
    np.savez_compressed(fn, t=np.asarray(buf["t"], float), X=np.asarray(buf["X"], f32), E=np.asarray(buf["E"], f32),
                        wq=np.asarray(buf["wq"], f32), kq=np.asarray(buf["kq"], f32), acc=np.asarray(buf["acc"], f32),
                        C=np.asarray(buf["C"], f32), local=np.asarray(buf["local"], f32),
                        posture=np.asarray(posture), position=np.asarray(position), version=np.int64(1))
    return fn


def load_clips(folder):
    out = []
    for f in sorted(glob.glob(os.path.join(folder, "kp_*.npz")), key=os.path.getmtime):
        try:
            d = np.load(f, allow_pickle=False)
            out.append({"path": f, "posture": str(d["posture"]), "position": str(d["position"]), "t": np.asarray(d["t"], float),
                        "X": np.asarray(d["X"], float), "E": np.asarray(d["E"], float), "wq": np.asarray(d["wq"], float),
                        "C": np.asarray(d["C"], float), "local": np.asarray(d["local"], float)})
        except Exception:
            continue
    return out


def counts(clips):
    """→ {posture: {position: frames}}, {posture: n_clips}."""
    c = {p: {q: 0 for q in POSITIONS} for p in POSTURES}; n = {p: 0 for p in POSTURES}
    for cl in clips:
        if cl["posture"] in c:
            c[cl["posture"]][cl["position"] if cl["position"] in POSITIONS else "moving"] += len(cl["t"]); n[cl["posture"]] += 1
    return c, n


# ── features / training ──────────────────────────────────────────────────────────────────────────────────────────
def finger_flex(P, flex_max=(130.0, 270.0, 270.0, 270.0, 270.0)):
    chains = ((1, 2, 3, 4, 5), (1, 6, 7, 8, 9, 10), (1, 11, 12, 13, 14, 15), (1, 16, 17, 18, 19, 20), (1, 21, 22, 23, 24, 25))
    out = []
    for ci, ch in enumerate(chains):
        a = 0.0
        for i in range(1, len(ch) - 1):
            v1 = P[ch[i]] - P[ch[i - 1]]; v2 = P[ch[i + 1]] - P[ch[i]]
            c = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
            a += math.degrees(math.acos(max(-1.0, min(1.0, c))))
        out.append(max(0.0, min(1.0, (a - 15.0) / (flex_max[ci] - 15.0))))
    return np.array(out, float)


def _features(cl, rng, augment=True):
    """One clip → (real F (n,43), synthetic Fa (m,43)) with the Posture Hand augmentation on the RAW windows."""
    X = cl["X"]; T = np.array([tilt(q) for q in cl["wq"]], float)
    ok = np.isfinite(X).all(1) & np.isfinite(T).all(1)
    X, T = X[ok], T[ok]; F = np.hstack([X, T]); Fa = []
    E = cl["E"][ok][:, :, -EMG_WIN:] if (augment and cl["E"].ndim == 3 and cl["E"].shape[2] >= EMG_WIN) else None
    if E is not None and len(E):
        blocks = [(tdfeat(np.roll(E, k, axis=1)), T) for k in (-1, 1)]
        gg = np.exp(rng.uniform(np.log(0.8), np.log(1.25), (len(E), 8)))[:, :, None]
        blocks.append((tdfeat(E * gg), T))
        blocks.append((tdfeat(E * rng.uniform(0.7, 1.45, (len(E), 1, 1))), T))
        blocks.append((tdfeat(E + rng.standard_normal(E.shape) * E.std(2, keepdims=True) * 0.12), T))
        Tj = T.copy()
        for i in range(len(T)):
            ax = rng.standard_normal(3); ax /= np.linalg.norm(ax) + 1e-9; th = math.radians(rng.normal(0, 8))
            K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
            Tj[i] = (np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K) @ T[i]
        blocks.append((X.copy(), Tj))
        Fa = [np.hstack([a, b]) for a, b in blocks]
    return F, (np.vstack(Fa) if Fa else np.zeros((0, 43)))


def _fit(F, y, sw, K, iters=600, lr=0.5, l2=1e-3):
    mu = F.mean(0); sd = F.std(0) + 1e-9
    Z = np.hstack([(F - mu) / sd, np.ones((len(F), 1))]); Yh = np.eye(K)[y]
    cnt = np.maximum(np.array([(y == k).sum() for k in range(K)], float), 1.0)
    w = (len(y) / K) / cnt[y] * sw; w = (w / w.mean())[:, None]
    W = np.zeros((Z.shape[1], K))
    for _ in range(iters):
        S = Z @ W; S -= S.max(1, keepdims=True); P = np.exp(S); P /= P.sum(1, keepdims=True)
        W -= lr * (Z.T @ (w * (P - Yh)) / len(Z) + l2 * W)
    return {"W": W, "mu": mu, "sd": sd}


def _assemble(clips, classes, rng, augment=True):
    F, y, sw = [], [], []
    for cl in clips:
        if cl["posture"] not in classes:
            continue
        k = classes.index(cl["posture"]); Fr, Fa = _features(cl, rng, augment)
        F.append(Fr); y += [k] * len(Fr); sw += [1.0] * len(Fr)
        if len(Fa):
            F.append(Fa); y += [k] * len(Fa); sw += [AUG_WEIGHT] * len(Fa)
    return (np.vstack(F) if F else np.zeros((0, 43))), np.asarray(y, int), np.asarray(sw, float)


def train(clips, seed=0):
    """→ (model, report). Held-out check: for every posture with ≥ 2 clips its NEWEST clip is held out; one model is
    trained without all of them and scored per posture (accuracy + confusion); then the final model uses everything."""
    rng = np.random.RandomState(seed)
    classes = sorted({cl["posture"] for cl in clips if cl["posture"] in POSTURES})
    if len(classes) < 2:
        return None, {"error": "record at least two different postures first"}
    K = len(classes)
    held = []
    for c in classes:
        cls_ = [cl for cl in clips if cl["posture"] == c]
        if len(cls_) >= 2:
            held.append(max(cls_, key=lambda cl: cl["t"][0]))
    report = {"classes": classes, "held_out": None}
    if held:
        rest = [cl for cl in clips if cl not in held]
        if len({cl["posture"] for cl in rest}) >= 2:
            F, y, sw = _assemble(rest, classes, rng); m = _fit(F, y, sw, K)
            conf = np.zeros((K, K), int); per = {}; per_pos = {}
            for cl in held:
                Fh, _ = _features(cl, rng, augment=False); k = classes.index(cl["posture"])
                pr = predict(m, Fh, classes=None).argmax(1)
                for p_ in pr:
                    conf[k, p_] += 1
                per[cl["posture"]] = float((pr == k).mean()) if len(pr) else float("nan")
                per_pos[cl["posture"]] = cl["position"]
            report["held_out"] = {"acc_per_posture": per, "position_per_posture": per_pos, "confusion": conf.tolist(),
                                  "acc": float(np.trace(conf) / max(conf.sum(), 1)), "n_frames": int(conf.sum())}
    F, y, sw = _assemble(clips, classes, rng)
    model = _fit(F, y, sw, K); model["classes"] = classes
    tem = np.zeros((K, 5)); src = []; tj = np.zeros((K, 26, 3)); has_j = np.zeros(K, bool)
    for k, c in enumerate(classes):
        tem[k] = np.asarray(TEMPLATES.get(c, (0.3,) * 5), float); s = "anatomical prior"
        if c != "fist":                                              # the camera cannot see a fist's fingers
            curls = []; shapes = []
            for cl in clips:
                if cl["posture"] == c and cl["local"].ndim == 3:
                    for i in np.where(cl["C"] >= 0.9)[0]:
                        L = cl["local"][i]
                        if np.isfinite(L).all() and np.abs(L).sum() > 0:
                            curls.append(finger_flex(L)); shapes.append(L - L[1])
            if len(curls) >= 30:                                     # the camera's own median SHAPE is the template (a pinch's
                tem[k] = np.median(np.asarray(curls), 0)             # thumb really meets the index — curls alone can't say that)
                tj[k] = np.median(np.asarray(shapes), 0); has_j[k] = True; s = f"camera shape ({len(curls)} frames)"
        src.append(s)
    model["templates"] = tem; model["template_src"] = src; model["template_joints"] = tj; model["template_has_joints"] = has_j
    report["n_real"] = int((sw == 1.0).sum()); report["n_syn"] = int((sw < 1.0).sum())
    report["train_acc"] = float((predict(model, F).argmax(1) == y).mean())
    return model, report


def predict(model, F, classes=None):
    Z = np.hstack([(np.atleast_2d(np.asarray(F, float)) - model["mu"]) / model["sd"], np.ones((len(np.atleast_2d(F)), 1))])
    S = Z @ model["W"]; S -= S.max(1, keepdims=True); P = np.exp(S)
    return P / P.sum(1, keepdims=True)


def save(path, model, report=None):
    out = {"W": model["W"].astype(np.float32), "mu": model["mu"].astype(np.float32), "sd": model["sd"].astype(np.float32),
           "classes": np.asarray(model["classes"]), "templates": model["templates"].astype(np.float32),
           "template_src": np.asarray(model["template_src"]), "trained": np.asarray(time.strftime("%Y-%m-%d %H:%M")),
           "template_joints": np.asarray(model.get("template_joints", np.zeros((len(model["classes"]), 26, 3))), np.float32),
           "template_has_joints": np.asarray(model.get("template_has_joints", np.zeros(len(model["classes"]), bool)), bool)}
    if report:
        ho = report.get("held_out") or {}
        out["report_acc"] = np.float32(ho.get("acc", np.nan)); out["report_n"] = np.int64(ho.get("n_frames", 0))
        out["report_per"] = np.asarray([ho.get("acc_per_posture", {}).get(c, np.nan) for c in model["classes"]], np.float32)
        out["report_pos"] = np.asarray([ho.get("position_per_posture", {}).get(c, "") for c in model["classes"]])
        out["report_conf"] = np.asarray(ho.get("confusion", np.zeros((len(model["classes"]),) * 2)), np.int64)
        out["n_real"] = np.int64(report.get("n_real", 0)); out["n_syn"] = np.int64(report.get("n_syn", 0))
    np.savez(path, **out)


def load(path):
    d = np.load(path, allow_pickle=False)
    m = {"W": np.asarray(d["W"], float), "mu": np.asarray(d["mu"], float), "sd": np.asarray(d["sd"], float),
         "classes": [str(c) for c in d["classes"]], "templates": np.asarray(d["templates"], float),
         "template_src": [str(c) for c in d["template_src"]], "trained": str(d["trained"]) if "trained" in d.files else ""}
    for k in ("report_acc", "report_n", "report_per", "report_pos", "report_conf", "n_real", "n_syn"):
        if k in d.files:
            m[k] = d[k]
    m["template_joints"] = np.asarray(d["template_joints"], float) if "template_joints" in d.files else np.zeros((len(m["classes"]), 26, 3))
    m["template_has_joints"] = np.asarray(d["template_has_joints"], bool) if "template_has_joints" in d.files else np.zeros(len(m["classes"]), bool)
    return m


class PostureGate:
    """Probabilities → a STABLE decision: a posture becomes active after it has led by a clear margin for HOLD_T
    seconds, and stays active until its probability has been below OFF_P for OFF_T seconds. 'other' never snaps."""

    def __init__(self, classes):
        self.classes = list(classes); self.cur = None; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0

    def update(self, probs, dt):
        p = np.asarray(probs, float); top = int(p.argmax()); second = float(np.sort(p)[-2]) if len(p) > 1 else 0.0
        strong = p[top] > SW_TOP and (p[top] - second) > SW_MARGIN
        if self.cur is not None and top == self.cur:
            self.cand = None; self.cand_t = 0.0
            self.low_t = self.low_t + dt if p[self.cur] < OFF_P else 0.0
            if self.low_t >= OFF_T:
                self.cur = None; self.low_t = 0.0
        elif strong:
            if self.cand == top:
                self.cand_t += dt
            else:
                self.cand, self.cand_t = top, dt
            if self.cand_t >= HOLD_T or (p[top] >= FAST_P and self.cand_t >= FAST_T):
                self.cur = top; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0
        else:
            self.cand = None; self.cand_t = 0.0
            if self.cur is not None:
                self.low_t = self.low_t + dt if p[self.cur] < OFF_P else 0.0
                if self.low_t >= OFF_T:
                    self.cur = None; self.low_t = 0.0
        name = self.classes[self.cur] if self.cur is not None else None
        return (name if name != "other" else None), float(p[top]), self.classes[top]

    def reset(self):
        self.cur = None; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0


def default_model_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures_model.npz")


def default_model2_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures_model2.npz")


# ═══════════════════════════════ v2: covariance features + MLP + camera self-teaching ═══════════════════════════════
# Measured on the recordings (Sep 24 = session 1, Sep 25 = session 2): the 43-d linear recogniser separates the five
# postures WITHIN a session (85 % at unseen arm positions) but collapses across sessions (open 2 %, point 30 %): the
# same posture is held with a different contraction on another day and its channel pattern changes. Adding the
# channel-COVARIANCE of the raw windows lifts within-session to 94 %; a 64-unit MLP on those features, adapted with
# frames the CAMERA labels during the session, scores 86 % on held-out session-2 clips including their hidden frames
# (open 82 / pinch 94 / point 82) against 45 % for the shipped recogniser.
FEAT2_DIM = 79                       # 40 TD + 8 log channel std + 28 channel correlations + 3 tilt
FEAT_WIN = 48                        # v2 feature window (samples @ 500 Hz = 96 ms; 64 = 128 ms scored the same, 32 ms slower)
MLP_H, MLP_EPOCHS, MLP_LR = 64, 60, 2e-3
PRIOR_ROWS = 12000                   # class-balanced subset of the training rows kept in the model for live replay
TEACH_DWELL, TEACH_CONF, TEACH_WEIGHT, BUF_PER_CLASS = 0.4, 0.9, 3.0, 1500
TEACH_STEPS = 3                      # Adam mini-batch steps per taught frame (live)


def feat_raw(E, T):
    """Raw windows (n,8,W) + tilt (n,3) → (n,79): Hudgins TD, log channel std, channel correlations, tilt."""
    E = np.asarray(E, float); E = E - E.mean(2, keepdims=True); n = len(E)
    C = np.einsum("nct,ndt->ncd", E, E) / E.shape[2]; d = np.sqrt(np.einsum("ncc->nc", C)) + 1e-6
    R = C / (d[:, :, None] * d[:, None, :]); iu = np.triu_indices(8, 1)
    return np.hstack([tdfeat(E), np.log(d), R[:, iu[0], iu[1]], np.asarray(T, float).reshape(n, 3)])


def cam_label(L):
    """Posture name from the camera's 26 local joints (geometry only), None when it is none of the five. On the
    recordings it agrees with the recorded label on 98–100 % of clearly seen frames and refuses 'other'."""
    L = np.asarray(L, float)
    if not np.isfinite(L).all() or np.abs(L).sum() == 0:
        return None
    th, ix, mi, ri, pi = finger_flex(L); d_ti = float(np.linalg.norm(L[5] - L[10]))
    if ix < 0.35 and mi < 0.35 and ri > 0.6 and pi > 0.6:
        return "victory"
    if ix < 0.35 and mi > 0.6 and ri > 0.6 and pi > 0.6:
        return "point"
    if d_ti < 0.035 and mi < 0.5 and ri < 0.5 and pi < 0.5:
        return "pinch"
    if ix > 0.6 and mi > 0.6 and ri > 0.6 and pi > 0.6:
        return "fist"
    if ix < 0.3 and mi < 0.3 and ri < 0.3 and pi < 0.3 and d_ti > 0.05:
        return "open"
    return None


def _features_raw(cl, rng, augment=True):
    """One clip → (real F (n,79), synthetic Fa (m,79), ok mask) with the same physical augmentation on the raw windows."""
    X = cl["X"]; E_all = cl["E"]
    ok = np.isfinite(X).all(1) & (np.isfinite(E_all).all(axis=(1, 2)) if E_all.ndim == 3 else False)
    T = np.array([tilt(q) for q in cl["wq"]], float); ok &= np.isfinite(T).all(1)
    if not ok.any():
        return np.zeros((0, FEAT2_DIM)), np.zeros((0, FEAT2_DIM)), ok
    E = E_all[ok][:, :, -FEAT_WIN:].astype(float); T = T[ok]; F = feat_raw(E, T); Fa = []
    if augment:
        for k in (-1, 1):
            Fa.append(feat_raw(np.roll(E, k, axis=1), T))
        Fa.append(feat_raw(E * np.exp(rng.uniform(np.log(0.8), np.log(1.25), (len(E), 8)))[:, :, None], T))
        Fa.append(feat_raw(E * rng.uniform(0.6, 1.7, (len(E), 1, 1)), T))
        Fa.append(feat_raw(E + rng.standard_normal(E.shape) * E.std(2, keepdims=True) * 0.12, T))
        Tj = T.copy()
        for i in range(len(T)):
            ax = rng.standard_normal(3); ax /= np.linalg.norm(ax) + 1e-9; th = math.radians(rng.normal(0, 8))
            K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
            Tj[i] = (np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K) @ T[i]
        Fa.append(np.hstack([F[:, :-3], Tj]))
    return F, (np.vstack(Fa) if Fa else np.zeros((0, FEAT2_DIM))), ok


def _assemble_raw(clips, classes, rng, augment=True, labeller=None):
    F, y, sw = [], [], []
    for cl in clips:
        if labeller is None and cl["posture"] not in classes:
            continue
        Fr, Fa, ok = _features_raw(cl, rng, augment)
        if not len(Fr):
            continue
        if labeller is None:
            labs = [cl["posture"]] * len(Fr)
        else:
            idx = np.where(ok)[0]
            labs = [(labeller(cl["local"][i]) if float(cl["C"][i]) >= TEACH_CONF else None) for i in idx]
        keep = np.array([j for j, l in enumerate(labs) if l is not None and l in classes], int)
        if not len(keep):
            continue
        ks = [classes.index(labs[j]) for j in keep]
        F.append(Fr[keep]); y += ks; sw += [1.0] * len(keep)
        nb = len(Fa) // len(Fr)
        for b in range(nb):
            F.append(Fa[b * len(Fr):(b + 1) * len(Fr)][keep]); y += ks; sw += [AUG_WEIGHT] * len(keep)
    return (np.vstack(F) if F else np.zeros((0, FEAT2_DIM))), np.asarray(y, int), np.asarray(sw, float)


def _mlp_forward(M, F):
    z = (np.atleast_2d(np.asarray(F, float)) - M["mu"]) / M["sd"]; h = np.maximum(z @ M["W1"] + M["b1"], 0.0)
    s = h @ M["W2"] + M["b2"]; s -= s.max(1, keepdims=True); p = np.exp(s)
    return p / p.sum(1, keepdims=True)


def _adam_step(M, opt, F, y, w, rng, lr, K, drop=0.2, l2=1e-4):
    """One mini-batch step of the MLP in place (Adam). opt: dict of moments + t."""
    z = (F - M["mu"]) / M["sd"]; h = np.maximum(z @ M["W1"] + M["b1"], 0.0)
    mask = (rng.rand(*h.shape) > drop) / (1.0 - drop) if drop > 0 else 1.0; hd = h * mask
    s = hd @ M["W2"] + M["b2"]; s -= s.max(1, keepdims=True); p = np.exp(s); p /= p.sum(1, keepdims=True)
    g = (p - np.eye(K)[y]) * w[:, None] / len(y)
    grads = {"W2": hd.T @ g, "b2": g.sum(0)}; gh = (g @ M["W2"].T) * mask * (h > 0)
    grads["W1"] = z.T @ gh + l2 * M["W1"]; grads["b1"] = gh.sum(0)
    opt["t"] += 1; t = opt["t"]
    for k, gr in grads.items():
        opt["m"][k] = 0.9 * opt["m"][k] + 0.1 * gr; opt["v"][k] = 0.999 * opt["v"][k] + 0.001 * gr * gr
        M[k] -= lr * (opt["m"][k] / (1 - 0.9 ** t)) / (np.sqrt(opt["v"][k] / (1 - 0.999 ** t)) + 1e-8)


def _new_opt(M):
    return {"t": 0, "m": {k: np.zeros_like(M[k]) for k in ("W1", "b1", "W2", "b2")},
            "v": {k: np.zeros_like(M[k]) for k in ("W1", "b1", "W2", "b2")}}


def _fit_mlp(F, y, sw, K, H=MLP_H, epochs=MLP_EPOCHS, seed=0):
    rng = np.random.RandomState(seed); mu = F.mean(0); sd = F.std(0) + 1e-9; n, d = F.shape
    cnt = np.maximum(np.bincount(y, minlength=K), 1); w = (n / K) / cnt[y] * sw; w = w / w.mean()
    M = {"mu": mu, "sd": sd, "W1": rng.standard_normal((d, H)) * np.sqrt(2.0 / d), "b1": np.zeros(H),
         "W2": np.zeros((H, K)), "b2": np.zeros(K)}
    opt = _new_opt(M); bs = 512
    for _ in range(epochs):
        perm = rng.permutation(n)
        for i in range(0, n, bs):
            b = perm[i:i + bs]; _adam_step(M, opt, F[b], y[b], w[b], rng, MLP_LR, K)
    return M


def train2(clips, seed=0, log=None):
    """v2 recogniser → (model, report): same held-out protocol as train() (newest clip per posture held out), then the
    final MLP on everything + a class-balanced subset of its training rows for live replay (`prior_F`, `prior_y`)."""
    rng = np.random.RandomState(seed)
    clips = [cl for cl in clips if cl["posture"] in POSTURES and cl.get("E") is not None and cl["E"].ndim == 3]
    classes = sorted({cl["posture"] for cl in clips})
    if len(classes) < 2:
        return None, {"error": "record at least two different postures first"}
    K = len(classes); held = []
    for c in classes:
        cls_ = [cl for cl in clips if cl["posture"] == c]
        if len(cls_) >= 2:
            held.append(max(cls_, key=lambda cl: cl["t"][0]))
    report = {"classes": classes, "held_out": None, "kind": "mlp"}
    if held:
        rest = [cl for cl in clips if cl not in held]
        if len({cl["posture"] for cl in rest}) >= 2:
            if log: log("held-out check…")
            F, y, sw = _assemble_raw(rest, classes, rng); m = _fit_mlp(F, y, sw, K, seed=seed)
            conf = np.zeros((K, K), int); per = {}; per_pos = {}
            for cl in held:
                Fh, _, _ = _features_raw(cl, rng, augment=False); k = classes.index(cl["posture"])
                pr = _mlp_forward(m, Fh).argmax(1) if len(Fh) else np.zeros(0, int)
                for p_ in pr:
                    conf[k, p_] += 1
                per[cl["posture"]] = float((pr == k).mean()) if len(pr) else float("nan"); per_pos[cl["posture"]] = cl["position"]
            report["held_out"] = {"acc_per_posture": per, "position_per_posture": per_pos, "confusion": conf.tolist(),
                                  "acc": float(np.trace(conf) / max(conf.sum(), 1)), "n_frames": int(conf.sum())}
    if log: log("final model…")
    F, y, sw = _assemble_raw(clips, classes, rng)
    model = _fit_mlp(F, y, sw, K, seed=seed); model["classes"] = classes; model["kind"] = "mlp"; model["win"] = int(FEAT_WIN)
    # class-balanced replay subset
    per_c = max(PRIOR_ROWS // K, 1); sel = []
    for k in range(K):
        idx = np.where(y == k)[0]
        sel.append(idx if len(idx) <= per_c else rng.choice(idx, per_c, replace=False))
    sel = np.concatenate(sel); model["prior_F"] = F[sel].astype(np.float32); model["prior_y"] = y[sel].astype(np.int64)
    # templates: identical rule to train()
    tem = np.zeros((K, 5)); src = []; tj = np.zeros((K, 26, 3)); has_j = np.zeros(K, bool)
    for k, c in enumerate(classes):
        tem[k] = np.asarray(TEMPLATES.get(c, (0.3,) * 5), float); s_ = "anatomical prior"
        if c != "fist":
            curls = []; shapes = []
            for cl in clips:
                if cl["posture"] == c and cl["local"].ndim == 3:
                    for i in np.where(cl["C"] >= 0.9)[0]:
                        L = cl["local"][i]
                        if np.isfinite(L).all() and np.abs(L).sum() > 0:
                            curls.append(finger_flex(L)); shapes.append(L - L[1])
            if len(curls) >= 30:
                tem[k] = np.median(np.asarray(curls), 0); tj[k] = np.median(np.asarray(shapes), 0); has_j[k] = True
                s_ = f"camera shape ({len(curls)} frames)"
        src.append(s_)
    model["templates"] = tem; model["template_src"] = src; model["template_joints"] = tj; model["template_has_joints"] = has_j
    report["n_real"] = int((sw == 1.0).sum()); report["n_syn"] = int((sw < 1.0).sum())
    report["train_acc"] = float((_mlp_forward(model, F).argmax(1) == y).mean())
    return model, report


def predict2(model, F):
    """Probabilities for v1 (linear, 43-d) or v2 (MLP, 79-d) models."""
    return _mlp_forward(model, F) if model.get("kind") == "mlp" else predict(model, F)


def save2(path, model, report=None):
    out = {"kind": np.asarray("mlp"), "win": np.int64(model.get("win", FEAT_WIN)), "mu": model["mu"].astype(np.float32), "sd": model["sd"].astype(np.float32),
           "W1": model["W1"].astype(np.float32), "b1": model["b1"].astype(np.float32),
           "W2": model["W2"].astype(np.float32), "b2": model["b2"].astype(np.float32),
           "prior_F": model["prior_F"].astype(np.float32), "prior_y": model["prior_y"].astype(np.int64),
           "classes": np.asarray(model["classes"]), "templates": model["templates"].astype(np.float32),
           "template_src": np.asarray(model["template_src"]), "trained": np.asarray(time.strftime("%Y-%m-%d %H:%M")),
           "template_joints": np.asarray(model["template_joints"], np.float32), "template_has_joints": np.asarray(model["template_has_joints"], bool)}
    if report:
        ho = report.get("held_out") or {}
        out["report_acc"] = np.float32(ho.get("acc", np.nan)); out["report_n"] = np.int64(ho.get("n_frames", 0))
        out["report_per"] = np.asarray([ho.get("acc_per_posture", {}).get(c, np.nan) for c in model["classes"]], np.float32)
        out["report_pos"] = np.asarray([ho.get("position_per_posture", {}).get(c, "") for c in model["classes"]])
        out["report_conf"] = np.asarray(ho.get("confusion", np.zeros((len(model["classes"]),) * 2)), np.int64)
        out["n_real"] = np.int64(report.get("n_real", 0)); out["n_syn"] = np.int64(report.get("n_syn", 0))
    np.savez_compressed(path, **out)


def load2(path):
    d = np.load(path, allow_pickle=False)
    m = {k: np.asarray(d[k], float) for k in ("mu", "sd", "W1", "b1", "W2", "b2")}
    m.update({"kind": "mlp", "win": int(d["win"]) if "win" in d.files else EMG_WIN, "prior_F": np.asarray(d["prior_F"], float), "prior_y": np.asarray(d["prior_y"], int),
              "classes": [str(c) for c in d["classes"]], "templates": np.asarray(d["templates"], float),
              "template_src": [str(c) for c in d["template_src"]], "trained": str(d["trained"]) if "trained" in d.files else "",
              "template_joints": np.asarray(d["template_joints"], float), "template_has_joints": np.asarray(d["template_has_joints"], bool)})
    for k in ("report_acc", "report_n", "report_per", "report_pos", "report_conf", "n_real", "n_syn"):
        if k in d.files:
            m[k] = d[k]
    return m


class OnlineTeacher:
    """Camera self-teaching of a v2 model during the session. observe() collects frames whose posture the camera
    labelled (clear view, same label for TEACH_DWELL s); step() takes one Adam mini-batch step on a copy of the model
    with half prior rows (the model's replay subset) and half session rows (weight TEACH_WEIGHT). The live model is
    `self.M`; the prior stays untouched (reset() returns to it)."""

    def __init__(self, model, seed=0):
        self.prior = model; self.rng = np.random.RandomState(seed); self.reset()

    def reset(self):
        P = self.prior; self.M = dict(P)
        for k in ("mu", "sd", "W1", "b1", "W2", "b2"):                          # only the weights are copied (trained)
            self.M[k] = np.array(P[k], float, copy=True)
        self.opt = _new_opt(self.M); self.K = len(P["classes"])
        self.buf = [[] for _ in range(self.K)]; self.n_taught = np.zeros(self.K, int); self.steps = 0
        self._last = None; self._last_t = 0.0; self._dwell_ok = False

    def observe(self, F, label, t):
        """F (79,) live features; label from cam_label() or None; t = time. → True when the frame was kept."""
        if label is None or label not in self.M["classes"]:
            self._last = None; self._dwell_ok = False; return False
        if label != self._last:
            self._last, self._last_t, self._dwell_ok = label, t, False; return False
        if not self._dwell_ok and t - self._last_t < TEACH_DWELL:
            return False
        self._dwell_ok = True; k = self.M["classes"].index(label); b = self.buf[k]
        b.append(np.asarray(F, float)); self.n_taught[k] += 1
        if len(b) > BUF_PER_CLASS:
            del b[0]
        return True

    @property
    def n_session(self):
        return int(sum(len(b) for b in self.buf))

    def step(self, n_steps=1, bs=32):
        if self.n_session < 30:
            return False
        P = self.prior; have = [k for k in range(self.K) if self.buf[k]]
        for _ in range(n_steps):
            ip = self.rng.randint(0, len(P["prior_F"]), bs); Fp = P["prior_F"][ip]; yp = P["prior_y"][ip]
            ks = self.rng.choice(have, bs); Fs = np.array([self.buf[k][self.rng.randint(len(self.buf[k]))] for k in ks])
            F = np.vstack([Fp, Fs]); y = np.concatenate([yp, ks]); w = np.concatenate([np.ones(bs), np.full(bs, TEACH_WEIGHT)])
            _adam_step(self.M, self.opt, F, y, w / w.mean(), self.rng, MLP_LR, self.K, drop=0.0)
        self.steps += n_steps
        return True

    def status(self):
        parts = [f"{c} {int(n)}" for c, n in zip(self.M["classes"], self.n_taught) if n]
        return ("taught today: " + " · ".join(parts) if parts else "nothing taught yet") + (f" · {self.steps} steps" if self.steps else "")


def default_data_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures")


# ── self-test on synthetic data ──────────────────────────────────────────────────────────────────────────────────
def _selftest():
    rng = np.random.RandomState(0); clips = []
    for c in ("open", "fist", "point", "pinch", "victory"):
        base = rng.uniform(20, 60, 8) * (1 + 0.6 * rng.rand(8))
        for rep in range(3):
            n = 200; E = rng.standard_normal((n, 8, 64)) * base[None, :, None] * (1 + 0.1 * rep)
            X = tdfeat(E); wq = np.tile([0.7, 0.1, 0.7, 0.05], (n, 1)) + 0.02 * rng.standard_normal((n, 4))
            clips.append({"path": f"{c}{rep}", "posture": c, "position": POSITIONS[rep % 3], "t": np.arange(n) / 30.0 + rep * 1000,
                          "X": X, "E": E, "wq": wq, "C": np.zeros(n), "local": np.zeros((n, 26, 3))})
    m, rep = train(clips)
    assert rep["held_out"]["acc"] > 0.95, rep
    g = PostureGate(m["classes"]); F, _ = _features(clips[3], rng, False)
    seq = [g.update(predict(m, F[i:i + 1])[0], 1 / 30.0)[0] for i in range(60)]
    assert seq[0] is None and seq[-1] == "fist", seq[:8]
    seq2 = [g.update(np.full(len(m["classes"]), 1.0 / len(m["classes"])), 1 / 30.0)[0] for _ in range(20)]
    assert seq2[-1] is None and seq2[0] == "fist", seq2
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kp_selftest.npz"); save(p, m, rep); m2 = load(p); os.remove(p)
    assert m2["classes"] == m["classes"] and np.allclose(m2["W"], m["W"], atol=1e-6)
    return rep["held_out"]["acc"], rep["train_acc"]


if __name__ == "__main__":
    a, b = _selftest()
    print(f"key_postures self-test OK — synthetic held-out accuracy {a * 100:.0f} %, train {b * 100:.0f} %; gate + save/load pass")
