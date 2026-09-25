# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
Armband tab for the CyberFinger Bridge GUI.

Embeds the MindRove 8-channel EMG dashboard (bridge/armband/emg_dashboard.py)
INSIDE the CyberFinger Tk window as a notebook tab, so opening cyberfinger_gui.py
also shows the armband's live EMG scope, activation bars and IMU — plus the
gesture-recognised 3D hand served to the browser.

Design
------
* The EMG dashboard's data layer + matplotlib Figure are reused verbatim; only
  its own event loop (run()/plt.show()) is skipped. We build a Dashboard in
  ``embed=True`` mode, drop its ``fig`` into a FigureCanvasTkAgg, and drive
  ``_push()``/``_refresh()`` from the host's existing ~30 Hz tick.
* Heavy deps (numpy/matplotlib/mindrove) are imported lazily in start(), so a
  CyberFinger install without them still launches — the tab just reports what to
  ``pip install`` when you press Start.
* The MindRove armband (Wi-Fi/UDP) and the CyberFinger gloves (BLE) are
  independent radios, so both can stream in one process at once. Only ONE process
  may own the armband, so the 3D hand is served in-process (via --web-hand's
  server) and opened with a button, never by launching a second program.
"""

import os
import sys
import webbrowser

import tkinter as tk
from tkinter import ttk

# Where the copied EMG modules + assets live (emg_dashboard.py, hand_avatar.py,
# hand3d.html, gesture_model.npz, hand_calib.npz).
ARMBAND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "armband")
if ARMBAND_DIR not in sys.path:
    sys.path.insert(0, ARMBAND_DIR)

WEB_PORT = 8770  # in-process 3D-hand server (matches emg_dashboard default)

# Sources offered in the tab. 'armband' is the real device; 'sim' needs no deps
# beyond numpy/matplotlib; 'synthetic' is the SDK's fake board.
_SOURCES = (("armband", "Armband"), ("synthetic", "Synthetic"), ("sim", "Simulator"))


class ArmbandTab:
    """Owns the Armband notebook page and the embedded EMG dashboard."""

    # Redraw the (heavy) matplotlib canvas every Nth host tick while visible.
    # The host ticks ~30 Hz; drawing every 2nd tick ≈ 15 fps keeps the EMG scope
    # smooth while leaving CPU for the glove panels.
    DRAW_EVERY = 2

    def __init__(self, parent, log=None):
        self.parent = parent
        self.log = log or (lambda _m: None)

        self.dash = None            # emg_dashboard.Dashboard (embed mode)
        self._emg = None            # emg_dashboard module (for the shared _WEB_POSE)
        self.canvas = None          # FigureCanvasTkAgg
        self.running = False
        self.visible = False        # set by the host on <<NotebookTabChanged>>
        self._tick_n = 0
        self._draw_fails = 0        # consecutive canvas render failures
        self._draw_disabled = False # give up repainting after too many failures

        self.source_var = tk.StringVar(value="armband")
        self._build_ui()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = ttk.Frame(self.parent)
        bar.pack(fill=tk.X, padx=12, pady=(10, 6))

        ttk.Label(bar, text="Source:", style="Status.TLabel").pack(side=tk.LEFT)
        for value, label in _SOURCES:
            ttk.Radiobutton(bar, text=label, style="Small.TRadiobutton",
                            variable=self.source_var, value=value).pack(side=tk.LEFT, padx=2)

        self.start_btn = ttk.Button(bar, text="Start", style="Accent.TButton",
                                    command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=(14, 4))
        self.stop_btn = ttk.Button(bar, text="Stop", style="Stop.TButton",
                                   command=self.stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        self.hand_btn = ttk.Button(bar, text="Show 3D hand in browser",
                                   style="Console.TButton",
                                   command=self.open_hand, state=tk.DISABLED)
        self.hand_btn.pack(side=tk.RIGHT)

        # Figure goes here once started; a hint sits in its place until then.
        self.holder = ttk.Frame(self.parent)
        self.holder.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
        self.placeholder = ttk.Label(
            self.holder, anchor="center", justify=tk.CENTER, style="Status.TLabel",
            text=("MindRove EMG armband\n\n"
                  "Pick a source and press Start.\n"
                  "‘Armband’ streams the real device over Wi-Fi; "
                  "‘Simulator’ needs no hardware."))
        self.placeholder.pack(fill=tk.BOTH, expand=True)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        if self.running:
            return
        source = self.source_var.get()

        # Lazy, guarded imports so a bare CyberFinger install still launches.
        try:
            import matplotlib
            matplotlib.use("TkAgg")          # embed uses a bare Figure, but be explicit
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            import emg_dashboard as emg
            self._emg = emg
        except Exception as e:
            self._fail("Armband needs numpy + matplotlib (and 'mindrove' for the "
                       "real board).\n\n    pip install numpy matplotlib mindrove\n\n"
                       f"Import failed: {e!r}")
            self.log(f"Armband: dependencies missing — {e!r}")
            return

        try:
            src = (emg.SimSource() if source == "sim"
                   else emg.MindRoveSource(synthetic=(source == "synthetic")))
            self.dash = emg.Dashboard(
                src,
                show_hand=False,                 # 3D hand goes to the browser, not the figure
                web_hand=True, no_browser=True,  # serve /pose in-process; open via the button
                hand_mode="gesture",
                gesture_model=os.path.join(ARMBAND_DIR, "gesture_model.npz"),
                calib=os.path.join(ARMBAND_DIR, "hand_calib.npz"),
                web_port=WEB_PORT,
                embed=True,
            )
        except Exception as e:
            self._fail(f"Could not build the EMG dashboard:\n\n{e!r}")
            self.log(f"Armband: build failed — {e!r}")
            self.dash = None
            return

        # Swap the hint for the live figure.
        if self.placeholder is not None:
            self.placeholder.destroy()
            self.placeholder = None
        self.canvas = FigureCanvasTkAgg(self.dash.fig, master=self.holder)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.dash.src.start()
        self.running = True
        self._tick_n = 0
        self._draw_fails = 0
        self._draw_disabled = False
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.hand_btn.configure(state=tk.NORMAL)
        self.log(f"Armband: started ({source})")

    def tick(self):
        """Called from the host's ~30 Hz poll loop. Always drains the source and
        updates artists + the web pose; repaints the canvas only while visible."""
        if not self.running or self.dash is None:
            return

        # Data side: drain even when hidden (the source thread keeps buffering,
        # so skipping read() would leak memory) and update _WEB_POSE so the
        # browser hand keeps moving from any tab. This never rasterises, so it
        # can't hit a font/render error — those live in the draw below.
        try:
            self.dash._push(self.dash.src.read())
            self.dash._refresh()
        except Exception as e:
            self.log(f"Armband: update error — {e!r}")
            return

        self._tick_n += 1
        if self._draw_disabled or not self.visible or self.canvas is None:
            return
        if self._tick_n % self.DRAW_EVERY:
            return

        # Paint side: a SYNCHRONOUS draw so any backend/font error is caught here
        # (draw_idle would raise later inside Tk's idle callback, past this
        # try/except). Data + the browser hand keep working regardless.
        try:
            self.canvas.draw()
            self._draw_fails = 0
        except Exception as e:
            self._draw_fails += 1
            if self._draw_fails >= 5:
                self._draw_disabled = True
                self.log("Armband: canvas rendering keeps failing "
                         f"({e!r}); stopped repainting the EMG figure. The signal "
                         "still streams and the browser 3D hand still updates.")

    def push_hand_orientation(self, hand_quat, wrist_quat, ok):
        """Feed glove-IMU orientation into the web-hand /pose stream (in-process).
        hand_quat/wrist_quat are [w,x,y,z] lists (or None). Called from the host
        GUI's tick; the dashboard's _refresh never touches these keys."""
        if not self.running or self._emg is None:
            return
        wp = self._emg._WEB_POSE
        wp["hand_quat"] = hand_quat
        wp["wrist_quat"] = wrist_quat
        wp["imu_ok"] = bool(ok)

    def open_hand(self):
        try:
            webbrowser.open(f"http://127.0.0.1:{WEB_PORT}/")
        except Exception as e:
            self.log(f"Armband: could not open browser — {e!r}")

    def stop(self):
        if self.dash is not None:
            try:
                self.dash.src.stop()
            except Exception:
                pass
        self.running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.hand_btn.configure(state=tk.DISABLED)
        self.log("Armband: stopped")

    def shutdown(self):
        """Called when the whole app closes — stop the source thread cleanly."""
        if self.dash is not None:
            try:
                self.dash.src.stop()
            except Exception:
                pass
        self.running = False

    # ── helpers ─────────────────────────────────────────────────────────────
    def _fail(self, message):
        """Replace the figure area with an error/hint message."""
        if self.canvas is not None:
            try:
                self.canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.canvas = None
        if self.placeholder is None:
            self.placeholder = ttk.Label(self.holder, anchor="center",
                                         justify=tk.CENTER, style="Status.TLabel")
            self.placeholder.pack(fill=tk.BOTH, expand=True)
        self.placeholder.configure(text=message)
