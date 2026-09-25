#!/usr/bin/env python3
"""
MindRove 8-Channel EMG Dashboard — v1
=====================================

A dark, card-based real-time dashboard for the MindRove EMG armband:

  * an 8-lane scrolling EMG scope (small-multiples, one colour per channel)
  * a per-channel activation (RMS) panel
  * an IMU panel (3-axis accelerometer + 3-axis gyroscope)
  * a live status header (data source, sample rate, throughput, battery, uptime)

It runs against three data sources so you can develop with or without hardware:

  --source armband     the real MindRove Armband over Wi-Fi   (needs `mindrove`)
  --source synthetic   the SDK's built-in synthetic board      (needs `mindrove`)
  --source sim         a pure-Python EMG simulator             (no SDK, no device)

Examples
--------
    python emg_dashboard.py                      # real armband (default)
    python emg_dashboard.py --source sim         # no hardware needed
    python emg_dashboard.py --source sim --screenshot preview.png

Requires: numpy, matplotlib   (plus `mindrove` for the real/synthetic sources).
Colours follow a validated dark data-viz palette. Built to grow feature by feature.
"""

import argparse
import os
import sys
import time

import numpy as np

# ----------------------------------------------------------------------------
# Palette (validated dark data-viz theme)
# ----------------------------------------------------------------------------
PAGE      = "#0d0d0d"   # page plane (figure background)
CARD      = "#1a1a19"   # panel / card surface
CARD_HI   = "#201f1e"   # slightly raised card
BORDER    = "#2c2c2a"   # hairline card border / gridline
AXIS      = "#383835"   # baseline / separators
INK       = "#ffffff"   # primary text
INK2      = "#c3c2b7"   # secondary text
MUTED     = "#898781"   # muted (labels, ticks)

# 8 categorical hues (dark column), fixed slot order -> one per EMG channel
CH_COLORS = ["#3987e5", "#199e70", "#c98500", "#3aa53a",
             "#9085e9", "#e66767", "#d55181", "#d95926"]

# status palette (fixed, never themed)
GOOD, WARN, SERIOUS, CRIT = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"

# connection state -> (colour, label) shown in the header status light
STATUS_MAP = {
    "live":         (GOOD, "LIVE"),
    "demo":         ("#9085e9", "SIMULATED"),   # fake data, NOT a real device
    "connecting":   (WARN, "CONNECTING…"),
    "stalled":      (WARN, "NO SIGNAL"),
    "disconnected": (CRIT, "DISCONNECTED"),
}

# IMU axis colours (x / y / z) — a small consistent sub-palette
IMU_XYZ = ["#3987e5", "#199e70", "#d95926"]

# latest hand pose shared with the optional in-dashboard web-hand server.
# hand_quat / wrist_quat (each [w,x,y,z] or None) carry glove-IMU orientation for
# the browser hand; imu_ok gates whether hand3d.html drives rotation from them.
# They are written by the host GUI (CyberFinger), not by the dashboard itself.
_WEB_POSE = {"fingers": {f: 0.0 for f in ["thumb", "index", "middle", "ring", "pinky"]},
             "gesture": "", "connected": False, "v": "poses-v2",
             "hand_quat": None, "wrist_quat": None, "imu_ok": False}

# The web-hand HTTP server is a process-wide singleton: when the dashboard is
# embedded (e.g. in the CyberFinger GUI) and the data source is switched, a new
# Dashboard is built but the server must NOT try to re-bind the port. Started
# once, it keeps serving the module-global _WEB_POSE that every Dashboard writes.
_WEB_SRV = {"server": None, "url": None}


# ============================================================================
#  Biquad filters (RBJ cookbook) — streaming, stateful, no scipy required
# ============================================================================
def _rbj(kind, fs, f0, q):
    """Return normalised (b0,b1,b2,a1,a2) for a 2nd-order RBJ biquad."""
    w0 = 2.0 * np.pi * f0 / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == "hp":
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
    elif kind == "lp":
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    elif kind == "notch":
        b0, b1, b2 = 1.0, -2.0 * cw, 1.0
    else:
        raise ValueError(kind)
    a0, a1, a2 = 1 + alpha, -2.0 * cw, 1 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


class BiquadCascade:
    """A cascade of biquads applied per channel, keeping state between calls.

    Processing only the *new* samples each frame keeps this O(new_samples),
    so real-time display filtering is essentially free.
    """

    def __init__(self, n_ch, stages):
        self.b = np.array([[s[0], s[1], s[2]] for s in stages])   # (S,3)
        self.a = np.array([[s[3], s[4]] for s in stages])         # (S,2)
        self.S = len(stages)
        # Direct Form II transposed state: z1,z2 per stage per channel
        self.z1 = np.zeros((self.S, n_ch))
        self.z2 = np.zeros((self.S, n_ch))

    def process(self, block):
        """block: (n_ch, n) -> filtered (n_ch, n).  n is small (new samples)."""
        n_ch, n = block.shape
        out = np.empty_like(block, dtype=float)
        for i in range(n):
            x = block[:, i].astype(float)
            for s in range(self.S):
                b0, b1, b2 = self.b[s]
                a1, a2 = self.a[s]
                y = b0 * x + self.z1[s]
                self.z1[s] = b1 * x - a1 * y + self.z2[s]
                self.z2[s] = b2 * x - a2 * y
                x = y
            out[:, i] = x
        return out


def make_emg_filter(n_ch, fs, mains=50.0):
    """Band-pass 20–200 Hz (below the 250 Hz Nyquist) + a mains notch."""
    lp = min(200.0, 0.45 * fs)          # keep well under Nyquist
    stages = [
        _rbj("hp", fs, 20.0, 0.707),    # high-pass 20 Hz (drift / motion)
        _rbj("lp", fs, lp,   0.707),    # low-pass  200 Hz
        _rbj("notch", fs, mains, 30.0), # mains notch (50/60 Hz)
    ]
    return BiquadCascade(n_ch, stages)


# ============================================================================
#  Data sources
# ============================================================================
class BaseSource:
    """Common interface. Subclasses set fs and the channel-index attributes,
    and implement read() -> (num_rows, n_new) float array (may be empty)."""
    name = "base"
    fs = 500
    emg_idx = list(range(8))
    accel_idx = [20, 21, 22]
    gyro_idx = [23, 24, 25]
    battery_idx = 18
    ts_idx = 27
    state = "live"      # live | demo | connecting | stalled | disconnected
    detail = ""
    is_demo = False     # True => fake data (simulator / SDK synthetic), watermarked

    def start(self): ...
    def read(self): raise NotImplementedError
    def stop(self): ...


class SimSource(BaseSource):
    """Pure-Python EMG simulator — realistic-looking bursts + mains hum + IMU.

    Mirrors the real board's row layout so the dashboard indexes it identically.
    """
    name = "simulator"
    is_demo = True

    def __init__(self, fs=500, seed=7):
        self.fs = fs
        self.num_rows = 39
        self.rng = np.random.default_rng(seed)
        self._t0 = None
        self._last = None
        self._n = 0
        # per-channel activation parameters (each lane pulses at its own rate)
        self._freq = 0.10 + 0.06 * np.arange(8)
        self._phase = np.linspace(0, 2 * np.pi, 8, endpoint=False)

    def start(self):
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.state = "demo"
        self.detail = "simulated data — device NOT connected"

    def read(self):
        now = time.perf_counter()
        n = int((now - self._last) * self.fs)
        if n <= 0:
            return np.empty((self.num_rows, 0))
        n = min(n, self.fs)                       # cap a burst after a stall
        t = (self._n + np.arange(n)) / self.fs
        self._n += n
        self._last = now

        out = np.zeros((self.num_rows, n))
        # --- EMG: activation envelope * band noise + baseline + mains hum ---
        for c in range(8):
            env = np.clip(np.sin(2 * np.pi * self._freq[c] * t + self._phase[c]), 0, 1) ** 2
            burst = env * self.rng.standard_normal(n) * 180.0     # active ~µV
            base = self.rng.standard_normal(n) * 4.0              # resting noise
            hum = 12.0 * np.sin(2 * np.pi * 50.0 * t)             # mains 50 Hz
            drift = 6.0 * np.sin(2 * np.pi * 0.3 * t + c)         # slow baseline
            out[c] = burst + base + hum + drift
        # --- IMU: gravity + slow motion (accel), small rates + spikes (gyro) ---
        out[self.accel_idx[0]] = 0.05 * self.rng.standard_normal(n) + 0.30 * np.sin(2 * np.pi * 0.5 * t)
        out[self.accel_idx[1]] = 0.05 * self.rng.standard_normal(n) + 0.20 * np.sin(2 * np.pi * 0.3 * t + 1)
        out[self.accel_idx[2]] = 1.0 + 0.05 * self.rng.standard_normal(n)     # ~1 g on Z
        for k, gi in enumerate(self.gyro_idx):
            out[gi] = 6.0 * np.sin(2 * np.pi * (0.4 + 0.2 * k) * t + k) + 2.0 * self.rng.standard_normal(n)
        out[self.battery_idx] = 84.0                                          # % (flat)
        out[self.ts_idx] = self._t0 + t
        return out


class MindRoveSource(BaseSource):
    """Real MindRove Armband (or the SDK synthetic board). Needs `mindrove`.

    Connects and streams on a BACKGROUND THREAD so the UI never blocks or
    crashes: if the armband isn't reachable the header shows DISCONNECTED and
    the thread keeps retrying; when the link comes up it flips to LIVE by itself.
    """

    def __init__(self, synthetic=False):
        self.name = "SDK synthetic" if synthetic else "armband"
        self._synthetic = synthetic
        self.is_demo = synthetic          # SDK synthetic board is also fake data
        self._board = None
        self.state = "connecting"
        self.detail = "opening Wi-Fi link…"
        self._blocks = []
        self._lock = None
        self._thread = None
        self._stop = False
        self._last_data = 0.0

    def start(self):
        import threading
        self._lock = threading.Lock()
        self._stop = False
        self.state = "connecting"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _connect(self):
        from mindrove.board_shim import BoardShim, MindRoveInputParams, BoardIds
        board_id = BoardIds.SYNTHETIC_BOARD if self._synthetic else BoardIds.MINDROVE_WIFI_BOARD
        BoardShim.enable_dev_board_logger()
        board = BoardShim(board_id, MindRoveInputParams())   # empty params = correct
        try:
            board.prepare_session()                          # raises if not joined / busy
            board.start_stream(450000)
        except Exception:
            # free the board so the next retry can re-init (the WiFi board is a
            # singleton — re-creating it without releasing raises "init once")
            try:
                board.release_session()
            except Exception:
                pass
            raise
        self.fs = BoardShim.get_sampling_rate(board_id)
        self.emg_idx = BoardShim.get_emg_channels(board_id)
        self.accel_idx = BoardShim.get_accel_channels(board_id)
        self.gyro_idx = BoardShim.get_gyro_channels(board_id)
        try:
            self.battery_idx = BoardShim.get_battery_channel(board_id)
        except Exception:
            self.battery_idx = None
        self.ts_idx = BoardShim.get_timestamp_channel(board_id)
        self._board = board

    def _sleep(self, secs):
        """Sleep up to `secs`, but return True immediately if asked to stop."""
        end = time.perf_counter() + secs
        while time.perf_counter() < end:
            if self._stop:
                return True
            time.sleep(0.05)
        return False

    def _run(self):
        while not self._stop:
            if self._board is None:                          # (re)connect
                self.state, self.detail = "connecting", "opening Wi-Fi link…"
                try:
                    self._connect()
                    self._last_data = time.perf_counter()
                    self.state, self.detail = "live", ""
                except ImportError:
                    self.state = "disconnected"
                    self.detail = "mindrove SDK not installed — pip install mindrove"
                    self._board = None
                    if self._sleep(2.0):
                        return
                    continue
                except Exception:
                    self.state = "disconnected"
                    self.detail = "can't open armband — check Wi-Fi & close other apps using it"
                    self._board = None
                    if self._sleep(2.0):
                        return
                    continue
            try:                                             # pull data
                if self._board.get_board_data_count() > 0:
                    blk = self._board.get_board_data()
                    with self._lock:
                        self._blocks.append(blk)
                    self._last_data = time.perf_counter()
                    self.state, self.detail = "live", ""
                elif time.perf_counter() - self._last_data > 1.5:
                    self.state = "stalled"
                    self.detail = "no samples — check Wi-Fi rate (>100 kbps)"
                time.sleep(0.02)
            except Exception:
                self.state, self.detail = "disconnected", "link lost — reconnecting"
                try:
                    self._board.release_session()
                except Exception:
                    pass
                self._board = None

    def read(self):
        if not self._blocks:
            return np.empty((0, 0))
        with self._lock:
            blocks, self._blocks = self._blocks, []
        return np.hstack(blocks) if blocks else np.empty((0, 0))

    def stop(self):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._board is not None:
            try:
                self._board.stop_stream()
            except Exception:
                pass
            try:
                self._board.release_session()
            except Exception:
                pass


# ============================================================================
#  Dashboard
# ============================================================================
class Dashboard:
    def __init__(self, source, window=4.0, mains=50.0, do_filter=True,
                 emg_scale=250.0, show_hand=False, calib="hand_calib.npz",
                 hand_mode="finger", gesture_model="gesture_model.npz",
                 web_hand=False, web_port=8770, no_browser=False, embed=False):
        # embed=True: build a bare matplotlib Figure with no pyplot manager, so
        # the host app (e.g. CyberFinger's Tk GUI) can place it in a
        # FigureCanvasTkAgg and drive updates from its own event loop. run() /
        # plt.show() are then unused.
        self.embed = embed
        self.src = source
        self.window = window
        self.do_filter = do_filter
        self.emg_scale = emg_scale          # µV full-scale per lane (±)
        self.fs = source.fs
        self.N = int(window * self.fs)      # samples in the visible window
        self.n_emg = len(source.emg_idx)

        # 3D hand: matplotlib panel (--hand) and/or browser (--web-hand); both
        # reuse the same EMG->pose decoder
        self.show_hand = show_hand
        self.web_hand = web_hand
        self.hand_enabled = show_hand or web_hand
        if self.hand_enabled:
            from hand_avatar import (HandModel, Decoder, GripFallback,
                                     FINGERS, FINGER_COLORS)
            self._FINGERS, self._FCOL = FINGERS, FINGER_COLORS
            self.hand_model = HandModel()
            self.hand_mode = hand_mode
            if hand_mode == "gesture" and os.path.exists(gesture_model):
                from hand_avatar import GesturePredictor
                self.hand_dec = GesturePredictor(gesture_model)
            elif hand_mode == "gesture":
                print("[i] No gesture model found; using grip. Train it with:\n"
                      "    py hand_avatar.py --source armband --train-gestures")
                self.hand_dec = GripFallback(); self.hand_mode = "grip"
            elif hand_mode == "grip" or not os.path.exists(calib):
                self.hand_dec = GripFallback(); self.hand_mode = "grip"
            else:
                self.hand_dec = Decoder(calib); self.hand_mode = "finger"
            # decode from a SHORT recent window (~0.25 s) so movements show up
            # promptly — not the 4 s display buffer
            self._hand_win = max(1, int(0.25 * self.fs))
        if web_hand:
            self._start_web_hand(web_port, open_browser=not no_browser)

        # rolling display buffers
        self.buf_emg = np.zeros((self.n_emg, self.N))
        self.buf_acc = np.zeros((3, self.N))
        self.buf_gyr = np.zeros((3, self.N))
        self.filt = make_emg_filter(self.n_emg, self.fs, mains) if do_filter else None

        self.total_samples = 0
        self.t_start = time.perf_counter()
        self.batt = None
        self._last_stat = self.t_start
        self._rate = 0.0

        self._build()

    # ---- layout ------------------------------------------------------------
    def _build(self):
        from matplotlib.patches import FancyBboxPatch

        self.FIGW, self.FIGH = 15.6, 9.0
        if self.embed:
            # No pyplot: a bare Figure has no manager/window, so nothing pops up
            # and the host owns the frame clock. The host wraps self.fig in a
            # FigureCanvasTkAgg and calls _push()/_refresh() itself.
            from matplotlib.figure import Figure
            self.plt = None
            fig = Figure(figsize=(self.FIGW, self.FIGH), facecolor=PAGE)
        else:
            import matplotlib.pyplot as plt
            self.plt = plt
            fig = plt.figure(figsize=(self.FIGW, self.FIGH), facecolor=PAGE)
        self.fig = fig

        # background axes in inch coordinates for even rounded cards
        bg = fig.add_axes([0, 0, 1, 1], zorder=0)
        bg.set_xlim(0, self.FIGW); bg.set_ylim(0, self.FIGH)
        bg.axis("off")
        self.bg = bg

        def card(x, y, w, h, fc=CARD):
            bg.add_patch(FancyBboxPatch(
                (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.12",
                linewidth=1.0, edgecolor=BORDER, facecolor=fc, zorder=1,
                mutation_aspect=1.0))

        def ax_in(x, y, w, h):
            a = fig.add_axes([x / self.FIGW, y / self.FIGH,
                              w / self.FIGW, h / self.FIGH], zorder=2)
            a.set_facecolor("none")
            for s in a.spines.values():
                s.set_visible(False)
            a.tick_params(colors=MUTED, labelsize=8, length=0)
            return a

        m, gap = 0.22, 0.20
        Hh = 0.95                                   # header height
        Himu = 1.75                                 # imu height
        y_head = self.FIGH - m - Hh
        y_imu = m
        main_b = y_imu + Himu + gap
        main_t = y_head - gap
        main_h = main_t - main_b
        scope_w = 9.9
        act_x = m + scope_w + gap
        act_w = self.FIGW - act_x - m

        # right column: activation full-height, unless the hand panel shares it
        if self.show_hand:
            act_h = main_h * 0.46
            hand_b = main_b + act_h + gap
            hand_h = main_t - hand_b
        else:
            act_h = main_h

        # cards
        card(m, y_head, self.FIGW - 2 * m, Hh, CARD_HI)          # header
        card(m, main_b, scope_w, main_h)                         # scope
        card(act_x, main_b, act_w, act_h)                        # activation
        if self.show_hand:
            card(act_x, hand_b, act_w, hand_h)                   # 3D hand
        imu_w = (self.FIGW - 2 * m - gap) / 2
        card(m, y_imu, imu_w, Himu)                              # accel
        card(m + imu_w + gap, y_imu, imu_w, Himu)                # gyro

        self._build_header(m, y_head, self.FIGW - 2 * m, Hh)
        self._build_scope(m + 0.28, main_b + 0.15, scope_w - 0.5, main_h - 0.55)
        self._build_activation(act_x + 0.25, main_b + 0.15, act_w - 0.5, act_h - 0.55)
        if self.show_hand:
            self._build_hand(act_x + 0.15, hand_b + 0.1, act_w - 0.3, hand_h - 0.5)
        self._build_imu(m, y_imu, imu_w, Himu, gap)

        # titles on the cards
        self._card_title(m + 0.30, main_t - 0.03, "EMG CHANNELS")
        filt_txt = (f"band-pass 20–{int(min(200, 0.45*self.fs))} Hz · notch"
                    if self.do_filter else "raw (unfiltered)")
        self.bg.text((m + scope_w - 0.30) / self.FIGW, (main_t - 0.05) / self.FIGH,
                     filt_txt, ha="right", va="top", color=MUTED, fontsize=8.5,
                     transform=self.bg.transAxes)
        act_title_y = (main_b + act_h - 0.03) if self.show_hand else (main_t - 0.03)
        self._card_title(act_x + 0.25, act_title_y, "ACTIVATION  (RMS µV)")
        if self.show_hand:
            self._card_title(act_x + 0.20, hand_b + hand_h - 0.03, "3D HAND  (live)")
            self.gesture_text = self.bg.text(
                act_x + 0.20, hand_b + hand_h - 0.32, "", color="#3987e5",
                fontsize=13, fontweight="bold", va="top")
        self._card_title(m + 0.22, y_imu + Himu - 0.05, "ACCELEROMETER  (g)")
        self._card_title(m + imu_w + gap + 0.22, y_imu + Himu - 0.05, "GYROSCOPE  (°/s)")

    def _card_title(self, x_in, y_in, text):
        self.bg.text(x_in, y_in, text, ha="left", va="top",
                     color=INK2, fontsize=9.5, fontweight="bold")

    def _build_header(self, x, y, w, h):
        cy = y + h / 2
        # title + status dot
        self.dot = self.bg.text(x + 0.30, cy + 0.16, "●", color=MUTED,
                                fontsize=13, va="center")
        self.bg.text(x + 0.52, cy + 0.18, "EMG DASHBOARD", color=INK,
                     fontsize=15, fontweight="bold", va="center")
        self.status_sub = self.bg.text(x + 0.52, cy - 0.22, "connecting…",
                                       color=MUTED, fontsize=8.5, va="center")
        # stat tiles
        self._tiles = {}
        labels = [("SOURCE", self.src.name), ("SAMPLE RATE", f"{self.fs} Hz"),
                  ("THROUGHPUT", "— sps"), ("SAMPLES", "0"),
                  ("BATTERY", "—"), ("UPTIME", "0:00")]
        x0 = x + 4.2
        step = (x + w - 0.3 - x0) / len(labels)
        for i, (lab, val) in enumerate(labels):
            tx = x0 + i * step
            self.bg.text(tx, cy + 0.20, lab, color=MUTED, fontsize=8,
                         va="center", fontweight="bold")
            self._tiles[lab] = self.bg.text(
                tx, cy - 0.16, val, color=INK, fontsize=12.5, va="center")

    def _build_scope(self, x, y, w, h):
        ax = self._ax_inch(x, y, w, h)
        ax.set_xlim(-self.window, 0)
        ax.set_ylim(0, self.n_emg)
        ax.set_xticks([-self.window, -self.window/2, 0])
        ax.set_xticklabels([f"-{self.window:g}s", f"-{self.window/2:g}s", "now"])
        ax.set_yticks([])
        for spine in ("bottom",):
            ax.spines[spine].set_visible(True)
            ax.spines[spine].set_color(AXIS)
        self.ax_emg = ax
        if getattr(self.src, "is_demo", False):
            ax.text(0.5, 0.5, "SIMULATED  DATA", transform=ax.transAxes,
                    ha="center", va="center", fontsize=52, color="#9085e9",
                    alpha=0.11, rotation=8, fontweight="bold", zorder=2)
        t = np.linspace(-self.window, 0, self.N)
        chip = dict(boxstyle="round,pad=0.18", facecolor=CARD, edgecolor="none",
                    alpha=0.72)
        self.emg_lines, self.emg_labels, self.emg_vals = [], [], []
        for c in range(self.n_emg):
            center = self.n_emg - 0.5 - c
            ax.axhline(center - 0.5, color=BORDER, lw=0.8, zorder=1)
            (ln,) = ax.plot(t, np.full(self.N, center), color=CH_COLORS[c],
                            lw=1.0, zorder=3, solid_capstyle="round")
            self.emg_lines.append(ln)
            # labels sit in the reserved strip at the top of each lane, on a chip
            self.emg_labels.append(ax.text(
                -self.window + 0.03, center + 0.42, f"CH{c+1}", zorder=5,
                color=CH_COLORS[c], fontsize=9, fontweight="bold", va="center",
                bbox=chip))
            self.emg_vals.append(ax.text(
                -0.03, center + 0.42, "0 µV", color=INK2, fontsize=8, zorder=5,
                ha="right", va="center", bbox=chip))

    def _build_activation(self, x, y, w, h):
        ax = self._ax_inch(x, y, w, h)
        ax.set_xlim(0, 1.0)
        ax.set_ylim(0, self.n_emg)
        ax.set_yticks([]); ax.set_xticks([])
        self.ax_act = ax
        self.act_bars, self.act_tracks, self.act_texts = [], [], []
        for c in range(self.n_emg):
            yb = self.n_emg - 1 - c + 0.28
            ax.barh(yb, 1.0, height=0.44, color=CARD_HI, edgecolor=BORDER,
                    lw=0.6, align="edge", zorder=1)                       # track
            bar = ax.barh(yb, 0.0, height=0.44, color=CH_COLORS[c],
                          align="edge", zorder=2)[0]
            self.act_bars.append(bar)
            ax.text(0.0, yb + 0.55, f"CH{c+1}", color=INK2, fontsize=8,
                    va="center")
            self.act_texts.append(ax.text(
                0.98, yb + 0.55, "0", color=MUTED, fontsize=8.5,
                ha="right", va="center"))
        self.act_max = 150.0     # µV that maps to a full bar (auto-grows)

    def _build_imu(self, m, y_imu, imu_w, Himu, gap):
        self.ax_acc = self._ax_inch(m + 0.25, y_imu + 0.18, imu_w - 0.5, Himu - 0.55)
        self.ax_gyr = self._ax_inch(m + imu_w + gap + 0.25, y_imu + 0.18,
                                    imu_w - 0.5, Himu - 0.55)
        t = np.linspace(-self.window, 0, self.N)
        self.acc_lines, self.gyr_lines = [], []
        for ax, store, rng in ((self.ax_acc, self.acc_lines, 1.5),
                               (self.ax_gyr, self.gyr_lines, 60.0)):
            ax.set_xlim(-self.window, 0); ax.set_ylim(-rng, rng)
            ax.set_xticks([]); ax.set_yticks([-rng, 0, rng])
            ax.tick_params(labelsize=7)
            ax.axhline(0, color=AXIS, lw=0.7)
            for k, lab in enumerate("XYZ"):
                (ln,) = ax.plot(t, np.zeros(self.N), color=IMU_XYZ[k], lw=1.0,
                                label=lab)
                store.append(ln)
            leg = ax.legend(loc="upper right", ncol=3, fontsize=7,
                            frameon=False, labelcolor=INK2,
                            handlelength=1.0, columnspacing=1.0,
                            borderaxespad=0.2)

    def _ax_inch(self, x, y, w, h):
        a = self.fig.add_axes([x / self.FIGW, y / self.FIGH,
                               w / self.FIGW, h / self.FIGH], zorder=2)
        a.set_facecolor("none")
        for s in a.spines.values():
            s.set_visible(False)
        a.tick_params(colors=MUTED, labelsize=8, length=0)
        return a

    def _build_hand(self, x, y, w, h):
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
        ax = self.fig.add_axes([x / self.FIGW, y / self.FIGH,
                                w / self.FIGW, h / self.FIGH],
                               projection="3d", zorder=2)
        ax.set_facecolor("none")
        self.ax_hand = ax
        J = self.hand_model.fk({f: 0.0 for f in self._FINGERS})
        self.hand_palm = Poly3DCollection([self.hand_model.palm_polygon(J)],
                                          facecolor="#20304a", edgecolor="#3987e5",
                                          linewidths=1.0, alpha=0.85)
        ax.add_collection3d(self.hand_palm)
        segs, cols = [], []
        for f in self._FINGERS:
            p = J[f]
            for i in range(3):
                segs.append([p[i], p[i + 1]]); cols.append(self._FCOL[f][:3])
        self.hand_lines = Line3DCollection(segs, colors=cols, linewidths=5)
        ax.add_collection3d(self.hand_lines)
        pts = np.vstack([J[f] for f in self._FINGERS])
        self.hand_scat = ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2],
                                    s=14, c="#cbd5f0", depthshade=True)
        ax.set_xlim(-4.5, 4.5); ax.set_ylim(-3.5, 9.5); ax.set_zlim(-5.5, 6.5)
        ax.set_box_aspect((9, 13, 12))
        ax.view_init(elev=16, azim=-72)
        ax.set_axis_off()

    def _update_hand(self, flex):
        J = self.hand_model.fk(flex)
        self.hand_palm.set_verts([self.hand_model.palm_polygon(J)])
        segs = []
        for f in self._FINGERS:
            p = J[f]
            for i in range(3):
                segs.append([p[i], p[i + 1]])
        self.hand_lines.set_segments(segs)
        pts = np.vstack([J[f] for f in self._FINGERS])
        self.hand_scat._offsets3d = (pts[:, 0], pts[:, 1], pts[:, 2])

    # ---- data + refresh ----------------------------------------------------
    def _push(self, block):
        if block.shape[1] == 0:
            return
        n = block.shape[1]
        emg = block[self.src.emg_idx, :]
        if self.filt is not None:
            emg = self.filt.process(emg)
        acc = block[self.src.accel_idx, :]
        gyr = block[self.src.gyro_idx, :]
        for buf, data in ((self.buf_emg, emg), (self.buf_acc, acc),
                          (self.buf_gyr, gyr)):
            if n >= self.N:
                buf[:, :] = data[:, -self.N:]
            else:
                buf[:, :-n] = buf[:, n:]
                buf[:, -n:] = data
        self.total_samples += n
        if self.src.battery_idx is not None:
            self.batt = float(block[self.src.battery_idx, -1])

    def _refresh(self):
        artists = []
        # EMG lanes
        for c in range(self.n_emg):
            center = self.n_emg - 0.5 - c
            y = center + np.clip(self.buf_emg[c] / self.emg_scale, -0.38, 0.38)
            self.emg_lines[c].set_ydata(y)
            rms = float(np.sqrt(np.mean(self.buf_emg[c] ** 2)))
            self.emg_vals[c].set_text(f"{rms:.0f} µV")
            artists += [self.emg_lines[c], self.emg_vals[c]]
        # Activation panel
        rmss = np.sqrt(np.mean(self.buf_emg ** 2, axis=1))
        self.act_max = max(self.act_max * 0.98, float(rmss.max()) * 1.15, 30.0)
        for c in range(self.n_emg):
            frac = min(1.0, rmss[c] / self.act_max)
            self.act_bars[c].set_width(frac)
            self.act_texts[c].set_text(f"{rmss[c]:.0f}")
            artists += [self.act_bars[c], self.act_texts[c]]
        # 3D hand: decode a pose from a short recent window; feed matplotlib &/or web
        if self.hand_enabled:
            win = self.buf_emg[:, -self._hand_win:]
            if self.hand_mode == "gesture":
                name, flex = self.hand_dec.decode_window(win)
            elif self.hand_mode == "grip":
                flex = self.hand_dec.decode(np.sqrt(np.mean(win ** 2, axis=1))); name = "GRIP"
            else:
                flex = self.hand_dec.decode(np.sqrt(np.mean(win ** 2, axis=1))); name = ""
            if self.show_hand:
                self.gesture_text.set_text((name or "").upper())
                self._update_hand(flex)
                artists += [self.hand_palm, self.hand_lines, self.hand_scat, self.gesture_text]
            if self.web_hand:
                _WEB_POSE["fingers"] = {k: round(float(v), 3) for k, v in flex.items()}
                _WEB_POSE["gesture"] = name or ""
                _WEB_POSE["connected"] = getattr(self.src, "state", "live") in ("live", "demo")
        # IMU
        for k in range(3):
            self.acc_lines[k].set_ydata(self.buf_acc[k])
            self.gyr_lines[k].set_ydata(self.buf_gyr[k])
            artists += [self.acc_lines[k], self.gyr_lines[k]]
        # connection status light
        color, label = STATUS_MAP.get(self.src.state, (MUTED, self.src.state.upper()))
        self.dot.set_color(color)
        det = self.src.detail
        self.status_sub.set_text(f"{label}  —  {det}" if det else f"{label}  —  {self.src.name}")
        self.status_sub.set_color(color)
        artists += [self.dot, self.status_sub]

        # header stats
        now = time.perf_counter()
        if now - self._last_stat > 0.5:
            self._rate = self.total_samples / max(1e-6, now - self.t_start)
            self._last_stat = now
        up = int(now - self.t_start)
        self._tiles["THROUGHPUT"].set_text(f"{self._rate:,.0f} sps")
        self._tiles["SAMPLES"].set_text(f"{self.total_samples:,}")
        self._tiles["UPTIME"].set_text(f"{up // 60}:{up % 60:02d}")
        if self.batt is not None:
            col = GOOD if self.batt > 40 else (WARN if self.batt > 15 else CRIT)
            self._tiles["BATTERY"].set_text(f"{self.batt:.0f}%")
            self._tiles["BATTERY"].set_color(col)
        artists += list(self._tiles.values())
        return artists

    # ---- run ---------------------------------------------------------------
    def _start_web_hand(self, port, open_browser=True):
        """Serve the smooth 3D hand (hand3d.html) + /pose to the browser, in a
        background thread — so the dashboard and the 3D hand share one armband."""
        import json
        import threading
        import webbrowser
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        # Already serving (e.g. after an embedded source switch)? Don't re-bind —
        # every Dashboard writes the same module-global _WEB_POSE the running
        # server reads, so the browser hand keeps working. Just (re)open if asked.
        if _WEB_SRV["server"] is not None:
            if open_browser and _WEB_SRV["url"]:
                try:
                    webbrowser.open(_WEB_SRV["url"])
                except Exception:
                    pass
            return

        here = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(here, "hand3d.html")
        if not os.path.exists(html_path):
            print("[web hand] hand3d.html not found — skipping browser hand.")
            return

        def render_page():   # read fresh each load so edits show on a plain refresh
            body = open(html_path, encoding="utf-8").read()
            return ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>EMG Hand</title></head><body>" + body + "</body></html>").encode("utf-8")

        class H(BaseHTTPRequestHandler):
            def do_GET(s):
                if s.path.startswith("/pose"):
                    s._send(json.dumps(_WEB_POSE).encode("utf-8"), "application/json")
                elif s.path in ("/", "/index.html"):
                    s._send(render_page(), "text/html; charset=utf-8")
                else:
                    s.send_response(404); s.end_headers()

            def _send(s, b, ctype):
                s.send_response(200)
                s.send_header("Content-Type", ctype)
                s.send_header("Cache-Control", "no-store")
                s.send_header("Content-Length", str(len(b)))
                s.end_headers(); s.wfile.write(b)

            def log_message(s, *a):
                pass

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError as e:
            print(f"[web hand] could not start server on port {port}: {e}")
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/"
        _WEB_SRV["server"], _WEB_SRV["url"] = srv, url
        print(f"[web hand] 3D hand -> {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def run(self, blit=True):
        from matplotlib.animation import FuncAnimation
        # 3D axes can't be blitted, so the hand panel forces a full redraw
        blit = blit and not self.show_hand
        interval = 45 if self.show_hand else 33
        self.src.start()

        def _update(_frame):
            self._push(self.src.read())
            return self._refresh()

        self.anim = FuncAnimation(self.fig, _update, interval=interval,
                                  blit=blit, cache_frame_data=False)
        self.fig.canvas.manager.set_window_title("MindRove EMG Dashboard")
        self.plt.show()
        self.src.stop()

    def screenshot(self, path, seconds=None):
        """Prime the buffers with `seconds` of data, then save one frame."""
        seconds = seconds if seconds is not None else self.window
        self.src.start()
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            self._push(self.src.read())
            time.sleep(0.01)
        self._refresh()
        self.src.stop()
        self.fig.savefig(path, dpi=110, facecolor=PAGE)
        print(f"wrote {path}")


# ============================================================================
#  CLI
# ============================================================================
def _pick_backend():
    """Return a matplotlib backend whose GUI toolkit is actually importable,
    or None with a helpful hint printed."""
    import importlib
    for mod, backend in (("PyQt6", "QtAgg"), ("PySide6", "QtAgg"),
                         ("PyQt5", "QtAgg"), ("tkinter", "TkAgg")):
        try:
            importlib.import_module(mod)
            return backend
        except Exception:
            continue
    print(
        "No interactive GUI backend is available, so a live window can't open.\n"
        "Fix (WSL / Debian / Ubuntu) — installs the Tk toolkit via apt, not pip:\n"
        "    sudo apt update && sudo apt install -y python3-tk\n"
        "Then re-run this command. (numpy/matplotlib are already installed.)\n"
        "Meanwhile you can render a still image with:\n"
        "    python emg_dashboard.py --source sim --screenshot preview.png",
        file=sys.stderr)
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description="MindRove EMG dashboard (v1)")
    p.add_argument("--source", choices=["armband", "synthetic", "sim"],
                   default="armband", help="data source (default: armband)")
    p.add_argument("--window", type=float, default=4.0, help="seconds shown")
    p.add_argument("--mains", type=float, default=50.0, choices=[50.0, 60.0],
                   help="mains frequency for the notch (50 EU/Asia, 60 Americas)")
    p.add_argument("--scale", type=float, default=250.0,
                   help="EMG lane full-scale in µV")
    p.add_argument("--no-filter", action="store_true", help="show raw signal")
    p.add_argument("--no-blit", action="store_true", help="disable blitting")
    p.add_argument("--hand", action="store_true",
                   help="add a live 3D hand panel driven by your EMG")
    p.add_argument("--web-hand", action="store_true", dest="web_hand",
                   help="also serve the smooth 3D hand to your browser (same armband)")
    p.add_argument("--web-port", type=int, default=8770, dest="web_port")
    p.add_argument("--no-browser", action="store_true", dest="no_browser",
                   help="don't auto-open the browser for --web-hand")
    p.add_argument("--calib", default="hand_calib.npz",
                   help="hand calibration file for --hand (else grip mode)")
    p.add_argument("--hand-mode", choices=["finger", "grip", "gesture"],
                   default="finger", dest="hand_mode",
                   help="'gesture' = recognize trained poses (recommended); "
                        "'grip' = whole-hand open/close; 'finger' = per-finger decoder")
    p.add_argument("--gesture-model", default="gesture_model.npz", dest="gesture_model",
                   help="gesture model file for --hand-mode gesture")
    p.add_argument("--fs", type=int, default=500, help="sample rate for --source sim")
    p.add_argument("--screenshot", metavar="PATH", help="render one frame and exit")
    p.add_argument("--seconds", type=float, default=None,
                   help="seconds of data to prime for --screenshot")
    args = p.parse_args(argv)

    import matplotlib
    if args.screenshot:
        matplotlib.use("Agg")
    else:
        backend = _pick_backend()
        if backend is None:
            return 1
        matplotlib.use(backend)

    if args.source == "sim":
        src = SimSource(fs=args.fs)
    else:
        src = MindRoveSource(synthetic=(args.source == "synthetic"))

    dash = Dashboard(src, window=args.window, mains=args.mains,
                     do_filter=not args.no_filter, emg_scale=args.scale,
                     show_hand=args.hand, calib=args.calib, hand_mode=args.hand_mode,
                     gesture_model=args.gesture_model, web_hand=args.web_hand,
                     web_port=args.web_port, no_browser=args.no_browser)

    if args.screenshot:
        dash.screenshot(args.screenshot, seconds=args.seconds)
    else:
        try:
            dash.run(blit=not args.no_blit)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
