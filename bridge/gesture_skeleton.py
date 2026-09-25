# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""EMG gesture → skeleton pose.

Custom, calibratable EMG gesture recognition whose output is a full 26-joint hand
skeleton (the SAME representation as the Quest optical hand), so it can be drawn in
SkeletonPanel, oriented by the glove IMU, and later fused with optical.

Calibration (option A): while the user holds a gesture, we record BOTH the EMG
time-domain features AND the optical 26-joint pose (wrist-relative, model space, as
src.hands[h]). Training fits the field-standard shrinkage-LDA GestureClassifier on
the EMG features; each gesture keeps the optical pose it was shown. At inference we
classify the live EMG and recall that gesture's stored skeleton — the EMG isn't
inventing finger angles, it's replaying the real optical skeleton it learned.
"""

import os
import sys

import numpy as np

# The classifier + feature extractor already live in the armband package.
_ARM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "armband")
if _ARM not in sys.path:
    sys.path.insert(0, _ARM)
try:
    from hand_avatar import td_feature_vector, GestureClassifier
    HAS_GESTURE = True
except Exception:                       # pragma: no cover - optional dependency
    td_feature_vector = None
    GestureClassifier = None
    HAS_GESTURE = False

EMG_WIN = 64         # samples per feature window (same for train + inference). ~0.13 s @500 Hz
                     # → about half the detection latency of 128, at the cost of slightly noisier
                     # features. If detection starts to flicker, bump back toward 96–128.
MIN_SAMPLES = 5      # min recorded windows per gesture before it can train


def emg_features(buf_emg):
    """(n_ch, N) filtered EMG → Hudgins TD feature vector over the last window,
    or None if the extractor/data is unavailable."""
    if td_feature_vector is None or buf_emg is None:
        return None
    try:
        buf_emg = np.asarray(buf_emg, float)
        w = buf_emg[:, -EMG_WIN:] if buf_emg.shape[1] >= EMG_WIN else buf_emg
        return td_feature_vector(w)
    except Exception:
        return None


class GestureSkeleton:
    """Custom EMG gestures → recalled 26-joint skeleton. Pure logic (no UI/Tk)."""

    def __init__(self):
        self.gestures = []          # ordered gesture names
        self.samples = {}           # name -> list of EMG feature vectors
        self.poses = {}             # name -> (26,3) model-space joints (from optical)
        self.clf = None             # trained GestureClassifier (or None)
        self._recording = None      # name currently being recorded
        self._min_conf = 0.3            # below this posterior → keep the last confident gesture
        self._last_stable = None
        self._cand = None              # candidate gesture awaiting confirmation
        self._cand_n = 0               # consecutive frames the candidate has held
        self._switch_n = 3             # a NEW gesture must persist this many frames before it is
                                       # shown → rejects 1–2 frame flickers (e.g. one 'open' frame
                                       # during a held fist). Adds NO steady-state lag; only ~this
                                       # many frames of delay on a real gesture change.

    # ── build the gesture set ────────────────────────────────────────────────
    def add_gesture(self, name):
        name = (name or "").strip()
        if not name:
            return False
        if name not in self.gestures:
            self.gestures.append(name)
            self.samples.setdefault(name, [])
        return True

    def remove_gesture(self, name):
        self.gestures = [g for g in self.gestures if g != name]
        self.samples.pop(name, None)
        self.poses.pop(name, None)
        self.clf = None             # any change invalidates the trained model

    # ── recording ────────────────────────────────────────────────────────────
    def start_record(self, name):
        self.add_gesture(name)
        self.samples[name] = []     # fresh take overwrites the previous one
        self._recording = name

    def stop_record(self):
        self._recording = None

    @property
    def recording(self):
        return self._recording

    def record_sample(self, feat, pose=None):
        """One tick while recording: store the EMG feature window, and keep the
        latest valid optical pose (26,3 model joints) shown for this gesture."""
        g = self._recording
        if g is None or feat is None:
            return
        self.samples[g].append(np.asarray(feat, float))
        if pose is not None:
            p = np.asarray(pose, float)
            if p.shape == (26, 3) and np.all(np.isfinite(p)):
                self.poses[g] = p

    def n_samples(self, name):
        return len(self.samples.get(name, []))

    def trained_gestures(self):
        return [g for g in self.gestures if self.n_samples(g) >= MIN_SAMPLES]

    def can_train(self):
        return HAS_GESTURE and len(self.trained_gestures()) >= 2

    # ── train / infer ────────────────────────────────────────────────────────
    def train(self):
        if not self.can_train():
            return False
        X, y = [], []
        for g in self.trained_gestures():
            for f in self.samples[g]:
                X.append(f)
                y.append(g)
        # Moderate shrinkage (0.6 vs default 0.2): regularizes the strongly correlated EMG
        # channels so the boundary is stable without collapsing to pure magnitude. NOTE: no
        # shrinkage value overcomes electrode/session DRIFT — if the live muscle pattern differs
        # from what was recorded, the trained boundary won't match. The real remedy is to record
        # and USE the model in the same session with steady, consistent contractions.
        self.clf = GestureClassifier.fit(np.asarray(X, float), np.asarray(y), shrink=0.6)
        return True

    def predict(self, feat, hold=False):
        """feat → (gesture_name, pose or None). (None, None) if untrained.
        Stabilizers: (1) confidence-reject — an ambiguous window keeps the last gesture; (2) a
        DEBOUNCE — a new gesture must persist `_switch_n` consecutive frames before it's shown, so
        a 1–2 frame misprediction (e.g. one 'open' frame during a held fist) is rejected with no
        steady-state lag; (3) hold=True (fast wrist rotation) FREEZES on the last gesture, since
        the EMG is transiently corrupted by the posture change (limb-position effect)."""
        if self.clf is None or feat is None:
            return None, None
        try:
            if hold and self._last_stable is not None:
                return self._last_stable, self.poses.get(self._last_stable)
            post = self.clf.posteriors(np.asarray(feat, float))
            k = int(np.argmax(post))
            raw = self.clf.names[k] if post[k] >= self._min_conf else self._last_stable
            if raw is None:
                raw = self.clf.names[k]
            if raw == self._last_stable:
                self._cand, self._cand_n = None, 0            # steady → nothing pending
            else:
                self._cand_n = self._cand_n + 1 if raw == self._cand else 1
                self._cand = raw
                if self._cand_n >= self._switch_n:            # confirmed → accept the switch
                    self._last_stable, self._cand, self._cand_n = raw, None, 0
            name = self._last_stable if self._last_stable is not None else self.clf.names[k]
        except Exception:
            return None, None
        return name, self.poses.get(name)

    # ── persistence ──────────────────────────────────────────────────────────
    def save(self, path):
        if self.clf is None:
            return False
        self.clf.save(path)                                   # names, mu, sd, W, b
        stem = path[:-4] if path.endswith(".npz") else path
        np.savez(stem + "_poses.npz",
                 **{g: self.poses[g] for g in self.poses if g in self.poses})
        # persist the RAW training samples too, so the model can be re-fit / re-tuned later
        # without the user re-recording everything.
        try:
            np.savez(stem + "_samples.npz", _emg_win=np.array([EMG_WIN]),
                     **{g: np.asarray(self.samples[g], float)
                        for g in self.samples if self.samples[g]})
        except Exception:
            pass
        return True

    def load(self, path):
        if not HAS_GESTURE:
            return False
        self.clf = GestureClassifier.load(path)
        self.gestures = list(self.clf.names)
        self.samples = {g: [] for g in self.gestures}
        stem = path[:-4] if path.endswith(".npz") else path
        pp = stem + "_poses.npz"
        if os.path.exists(pp):
            d = np.load(pp, allow_pickle=True)
            self.poses = {k: np.asarray(d[k], float) for k in d.files}
        sp = stem + "_samples.npz"
        if os.path.exists(sp):
            try:
                d = np.load(sp, allow_pickle=True)
                win = int(d["_emg_win"][0]) if "_emg_win" in d.files else EMG_WIN
                if win == EMG_WIN:                    # ignore samples recorded at a DIFFERENT
                    for k in d.files:                 # window — they won't match the model
                        if k == "_emg_win":
                            continue
                        self.samples[k] = [np.asarray(r, float) for r in d[k]]
            except Exception:
                pass
        return True
