# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
CyberFinger Bridge GUI — Linux version

Uses bleak (cross-platform BLE) and python-evdev/uinput for virtual gamepad.
Combines VR (BLE→UDP) and Gamepad (BLE→uinput Xbox 360) bridge modes.

Prerequisites:
    pip install bleak pystray pillow
    pip install evdev          # for gamepad mode
    sudo modprobe uinput       # load uinput kernel module

    # To use uinput without root:
    sudo groupadd -f uinput
    sudo usermod -aG uinput "$USER"
    echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | \
        sudo tee /etc/udev/rules.d/99-uinput.rules
    sudo udevadm control --reload-rules
    # Then log out and back in

Usage:
    python cyberfinger_gui_linux.py
    python cyberfinger_gui_linux.py --debug
"""

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
import struct
import socket
import time
import sys
import os
import queue
import json

try:
    from bleak import BleakScanner, BleakClient
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    import evdev
    from evdev import UInput, ecodes, AbsInfo
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

# ── BLE protocol (matches ESP32 firmware) ────────────────────────────────

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"
VR_CTRL_UUID    = "0000cf02-0000-1000-8000-00805f9b34fb"

GAMEPAD_MAGIC    = 0x50474643
GAMEPAD_PACK_FMT = "<IBBhhBB"
INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)

BTN_TRIGGER = 0x01
BTN_GRIP    = 0x02
BTN_B       = 0x04
BTN_JCLICK  = 0x08
BTN_A       = 0x10

BUTTON_NAMES = {
    BTN_TRIGGER: "TRIG",
    BTN_GRIP:    "GRIP",
    BTN_B:       "B",
    BTN_JCLICK:  "JCLK",
    BTN_A:       "A",
}

# Brand colors
COLOR_BG       = "#1a1a1a"
COLOR_BG2      = "#242424"
COLOR_BG3      = "#2e2e2e"
COLOR_FG       = "#e0e0e0"
COLOR_FG_DIM   = "#888888"
COLOR_ACCENT   = "#e6007e"
COLOR_ACCENT2  = "#ff2d9b"
COLOR_GREEN    = "#00e676"
COLOR_RED      = "#ff1744"
COLOR_ORANGE   = "#ff9100"
COLOR_BLUE     = "#448aff"


def fmt_buttons(btn):
    parts = [name for bit, name in BUTTON_NAMES.items() if btn & bit]
    return "+".join(parts) if parts else "none"


def resource_path(relative):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


# ── Tray icon helpers ────────────────────────────────────────────────────

def _load_tray_icon_running():
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32.png")))
    except Exception:
        return _generate_fallback_icon((230, 0, 126))


def _load_tray_icon_idle():
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32_bw.png")))
    except Exception:
        return _generate_fallback_icon((128, 128, 128))


def _generate_fallback_icon(color):
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 30, 30], fill=color, outline=(255, 255, 255, 200), width=1)
    return img


# ── Hand state ───────────────────────────────────────────────────────────

class HandState:
    def __init__(self):
        self.buttons = 0
        self.joy_x = 0
        self.joy_y = 0
        self.trigger = 0
        self.battery = 100
        self.packet_count = 0
        self.timestamp = 0.0
        self.connected = False
        self.name = ""
        self.address = ""

    @property
    def joy_x_float(self):
        return max(-1.0, min(1.0, self.joy_x / 32767.0))

    @property
    def joy_y_float(self):
        return max(-1.0, min(1.0, self.joy_y / 32767.0))

    @property
    def trigger_float(self):
        if self.trigger > 10:
            return self.trigger / 255.0
        return 1.0 if (self.buttons & BTN_TRIGGER) else 0.0


# ── BLE Manager (bleak-based, runs in asyncio thread) ────────────────────

class BLEManager:
    """Manages BLE connections using bleak (cross-platform)."""

    def __init__(self, app):
        self.app = app
        self.left = HandState()
        self.right = HandState()
        self._thread = None
        self._loop = None
        self._running = False
        self._clients = []  # (label, BleakClient)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            self.app.log(f"BLE thread error: {e}")
        finally:
            # Disconnect clients
            try:
                for label, client in self._clients:
                    if client.is_connected:
                        loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self):
        if not HAS_BLEAK:
            self.app.log("ERROR: bleak not installed! pip install bleak")
            self.app.set_status("bleak not installed")
            return

        self.app.log("Scanning for CyberFinger devices (5s)...")
        self.app.set_status("Scanning...")

        # Scan for BLE devices
        try:
            devices = await BleakScanner.discover(timeout=5.0)
        except Exception as e:
            self.app.log(f"Scan failed: {e}")
            self.app.set_status("Scan failed")
            return

        # Filter CyberFinger devices
        cf_devices = []
        for dev in devices:
            name = dev.name or ""
            if "cyberfinger" in name.lower():
                cf_devices.append(dev)
                self.app.log(f"  Found: \"{name}\" ({dev.address})")

        if not cf_devices:
            self.app.log("No CyberFinger devices found!")
            self.app.log("Make sure ESP32s are powered on and advertising.")
            self.app.set_status("No devices found")
            return

        self.app.log(f"Found {len(cf_devices)} CyberFinger device(s)")

        # Assign left/right by name
        left_dev = right_dev = None
        for dev in cf_devices:
            name = (dev.name or "").lower()
            if "left" in name and not left_dev:
                left_dev = dev
                self.app.log(f"  LEFT  ← \"{dev.name}\" ({dev.address})")
            elif "right" in name and not right_dev:
                right_dev = dev
                self.app.log(f"  RIGHT ← \"{dev.name}\" ({dev.address})")

        if not left_dev and not right_dev:
            self.app.log("No devices with 'left' or 'right' in name!")
            self.app.set_status("Assignment failed")
            return

        # Connect and subscribe
        self._clients = []
        tasks = []
        if left_dev:
            tasks.append(self._connect_device("LEFT", left_dev, self.left))
        if right_dev:
            tasks.append(self._connect_device("RIGHT", right_dev, self.right))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                self.app.log(f"Connection error: {r}")

        active = [label for label, _ in self._clients]
        if not active:
            self.app.log("Failed to connect to any device!")
            self.app.set_status("Connection failed")
            return

        self.app.log(f"Connected: {', '.join(active)}")
        self.app.set_status("Connected")

        # Keep running until stopped
        try:
            while self._running:
                # Check connections are still alive
                for label, client in self._clients:
                    if not client.is_connected:
                        self.app.log(f"{label}: Disconnected!")
                        self.app.set_status(f"{label} disconnected")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _connect_device(self, label, dev, state):
        """Connect to a CyberFinger device and subscribe to CF01 notifications."""
        self.app.log(f"{label}: Connecting to {dev.address}...")

        client = BleakClient(dev.address, timeout=15.0)
        try:
            await client.connect()
        except Exception as e:
            self.app.log(f"{label}: Connection failed: {e}")
            return

        if not client.is_connected:
            self.app.log(f"{label}: Not connected")
            return

        state.connected = True
        state.name = dev.name or dev.address
        state.address = dev.address

        # Find CF01 characteristic
        cf01_char = None
        for service in client.services:
            if "cf00" in str(service.uuid).lower():
                for char in service.characteristics:
                    if "cf01" in str(char.uuid).lower():
                        cf01_char = char
                        break

        if not cf01_char:
            self.app.log(f"{label}: CF01 characteristic not found!")
            self.app.log(f"{label}: Services: {[str(s.uuid) for s in client.services]}")
            return

        # Subscribe to notifications
        hand_idx = 0 if label == "LEFT" else 1

        def on_notify(sender, data):
            self._handle_data(data, hand_idx, state)

        try:
            await client.start_notify(cf01_char.uuid, on_notify)
            self.app.log(f"{label}: Notifications active")
            self._clients.append((label, client))
        except Exception as e:
            self.app.log(f"{label}: Notify subscribe failed: {e}")

    def _handle_data(self, data, hand_override, state):
        if len(data) < INPUT_REPORT_SIZE:
            return

        hand, buttons, joy_x, joy_y, trigger, battery, seq = \
            struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])

        # Use the hand_override (from L/R assignment) not the packet's hand byte,
        # in case firmware reports wrong hand index
        h = hand_override

        old_buttons = state.buttons
        state.buttons = buttons
        state.joy_x = joy_x
        state.joy_y = joy_y
        state.trigger = trigger
        state.battery = battery
        state.timestamp = time.time()
        state.packet_count += 1

        if buttons != old_buttons:
            hn = "L" if h == 0 else "R"
            self.app.log(f"{hn} BTN: {fmt_buttons(buttons)}")

        self.app.on_input(h, state)


# ── VR Mode (UDP forwarding) ─────────────────────────────────────────────

class VRMode:
    def __init__(self, port=27015):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = ("127.0.0.1", port)

    def on_input(self, hand, state):
        pkt = struct.pack(GAMEPAD_PACK_FMT,
                          GAMEPAD_MAGIC, hand, state.buttons,
                          state.joy_x, state.joy_y, state.trigger, state.battery)
        try:
            self.sock.sendto(pkt, self.target)
        except Exception:
            pass

    def stop(self):
        self.sock.close()


# ── Gamepad Mode (evdev/uinput virtual Xbox 360) ─────────────────────────

class GamepadMode:
    """Creates a virtual Xbox 360-like gamepad via Linux uinput."""

    def __init__(self):
        self.device = None
        self.available = False

        if not HAS_EVDEV:
            return

        try:
            # Define capabilities matching an Xbox 360 controller
            cap = {
                ecodes.EV_ABS: [
                    # Left stick
                    (ecodes.ABS_X,  AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                    (ecodes.ABS_Y,  AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                    # Right stick
                    (ecodes.ABS_RX, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                    (ecodes.ABS_RY, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                    # Triggers (not used in current mapping but reserved)
                    (ecodes.ABS_Z,  AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                    (ecodes.ABS_RZ, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                    # D-pad
                    (ecodes.ABS_HAT0X, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
                    (ecodes.ABS_HAT0Y, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
                ],
                ecodes.EV_KEY: [
                    ecodes.BTN_A,           # btn 1 (south)
                    ecodes.BTN_B,           # btn 2 (east)
                    ecodes.BTN_X,           # btn 3 (west)
                    ecodes.BTN_Y,           # btn 4 (north)
                    ecodes.BTN_TL,          # btn 5 (LB)
                    ecodes.BTN_TR,          # btn 6 (RB)
                    ecodes.BTN_SELECT,      # btn 7 (back)
                    ecodes.BTN_START,       # btn 8 (start)
                    ecodes.BTN_THUMBL,      # btn 9 (L3)
                    ecodes.BTN_THUMBR,      # btn 10 (R3)
                    ecodes.BTN_MODE,        # guide button
                ],
            }

            self.device = UInput(cap, name="CyberFinger Virtual Gamepad",
                                vendor=0x045e, product=0x028e, version=0x0110)
            self.available = True
        except PermissionError:
            pass  # Need uinput permissions
        except Exception:
            pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available or not self.device:
            return

        dev = self.device

        # Sticks (Y inverted for standard gamepad convention)
        dev.write(ecodes.EV_ABS, ecodes.ABS_X,  left.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Y,  -left.joy_y)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RX, right.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RY, -right.joy_y)

        # Right hand buttons
        dev.write(ecodes.EV_KEY, ecodes.BTN_A,      1 if (right.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_B,      1 if (right.buttons & BTN_GRIP) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TR,     1 if (right.buttons & BTN_B) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBR, 1 if (right.buttons & BTN_JCLICK) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_START,  1 if (right.buttons & BTN_A) else 0)

        # Left hand buttons
        dev.write(ecodes.EV_KEY, ecodes.BTN_X,      1 if (left.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_Y,      1 if (left.buttons & BTN_GRIP) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TL,     1 if (left.buttons & BTN_B) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBL, 1 if (left.buttons & BTN_JCLICK) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_SELECT, 1 if (left.buttons & BTN_A) else 0)

        dev.syn()

    def stop(self):
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None


# ── GUI Application ──────────────────────────────────────────────────────

class CyberFingerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Bridge")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("680x580")
        self.root.minsize(600, 500)

        # Set window icon
        try:
            icon_path = resource_path(os.path.join("assets", "icon_32x32.png"))
            icon_img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon_img)
            self._icon_ref = icon_img
        except Exception:
            pass

        self.log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self._current_status = "Idle"
        self._window_visible = True

        # Config persistence
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        self._config_dir = os.path.join(config_home, "cyberfinger-bridge")
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = VRMode()
        self.gamepad_mode = GamepadMode()
        self.active_mode = None

        self._build_ui()

        # System tray
        self._tray_icon = None
        if HAS_TRAY:
            self._setup_tray()

        self._poll_queues()

        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Auto-start
        if self._config.get("autostart", False):
            self.root.after(500, self._start_bridge)

    def _load_config(self):
        try:
            with open(self._config_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            with open(self._config_path, "w") as f:
                json.dump(self._config, f)
        except Exception:
            pass

    # ── System Tray ──────────────────────────────────────────────────────

    def _setup_tray(self):
        self._tray_icon_running = _load_tray_icon_running()
        self._tray_icon_idle = _load_tray_icon_idle()

        menu = pystray.Menu(
            pystray.MenuItem("Show/Hide", self._tray_toggle_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start", self._tray_start),
            pystray.MenuItem("Stop", self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._tray_exit),
        )

        self._tray_icon = pystray.Icon(
            "CyberFingerBridge",
            self._tray_icon_idle,
            "CyberFinger Bridge — Idle",
            menu
        )

        tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        tray_thread.start()

    def _set_tray_running(self, running):
        if self._tray_icon:
            try:
                self._tray_icon.icon = self._tray_icon_running if running else self._tray_icon_idle
            except Exception:
                pass

    def _update_tray_tooltip(self):
        if self._tray_icon:
            mode = self.mode_var.get().upper() if hasattr(self, 'mode_var') else ""
            self._tray_icon.title = f"CyberFinger Bridge — {self._current_status}" + \
                                    (f" ({mode})" if self.active_mode else "")

    def _tray_toggle_window(self, icon=None, item=None):
        if self._window_visible:
            self.root.after(0, self._hide_window)
        else:
            self.root.after(0, self._show_window)

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self._start_bridge)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self._stop_bridge)

    def _tray_exit(self, icon=None, item=None):
        self.root.after(0, self._quit_app)

    def _hide_window(self):
        self.root.withdraw()
        self._window_visible = False

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._window_visible = True

    def _on_window_close(self):
        if HAS_TRAY and self._tray_icon:
            self._hide_window()
            self.log("Minimized to system tray")
        else:
            self._quit_app()

    def _quit_app(self):
        self._config["mode"] = self.mode_var.get()
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()

        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass

        self.root.destroy()

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(".", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG, font=("monospace", 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT,
                        font=("monospace", 14, "bold"))
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_FG_DIM,
                        font=("monospace", 9))
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("monospace", 10), focuscolor=COLOR_BG)
        style.map("TRadiobutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("monospace", 9), focuscolor=COLOR_BG)
        style.map("TCheckbutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="white",
                        font=("monospace", 11, "bold"), padding=(20, 8))
        style.map("Accent.TButton",
                  background=[("active", COLOR_ACCENT2), ("disabled", COLOR_BG3)])
        style.configure("Stop.TButton", background=COLOR_RED, foreground="white",
                        font=("monospace", 11, "bold"), padding=(20, 8))
        style.map("Stop.TButton",
                  background=[("active", "#ff4444"), ("disabled", COLOR_BG3)])

        # Header
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(12, 4))
        ttk.Label(header, text="⬡ CyberFinger Bridge (Linux)", style="Title.TLabel").pack(side=tk.LEFT)
        self.status_label = ttk.Label(header, text="Idle", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # Mode selection + Start/Stop
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, padx=16, pady=(4, 4))

        self.mode_var = tk.StringVar(value=self._config.get("mode", "vr"))
        ttk.Radiobutton(ctrl_frame, text="VR Mode (BLE→SteamVR)",
                        variable=self.mode_var, value="vr").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(ctrl_frame, text="Gamepad Mode (BLE→uinput Xbox 360)",
                        variable=self.mode_var, value="gamepad").pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", style="Stop.TButton",
                                   command=self._stop_bridge, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.start_btn = ttk.Button(ctrl_frame, text="Start", style="Accent.TButton",
                                    command=self._start_bridge)
        self.start_btn.pack(side=tk.RIGHT)

        # Options row
        opts_frame = ttk.Frame(self.root)
        opts_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.autostart_var = tk.BooleanVar(value=self._config.get("autostart", False))
        ttk.Checkbutton(opts_frame, text="Auto-start on launch",
                        variable=self.autostart_var,
                        command=self._on_autostart_changed).pack(side=tk.LEFT)

        if HAS_TRAY:
            ttk.Label(opts_frame, text="(close button minimizes to tray)",
                     style="Status.TLabel").pack(side=tk.RIGHT)

        # Hands visualization
        hands_frame = ttk.Frame(self.root)
        hands_frame.pack(fill=tk.X, padx=16, pady=4)

        self.left_panel = HandPanel(hands_frame, "LEFT", side=tk.LEFT)
        self.right_panel = HandPanel(hands_frame, "RIGHT", side=tk.RIGHT)

        # Log console
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8,
            bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
            font=("monospace", 9), relief=tk.FLAT, borderwidth=0,
            selectbackground=COLOR_ACCENT, selectforeground="white",
            state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _on_autostart_changed(self):
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

    def _start_bridge(self):
        if self.active_mode:
            return

        if not HAS_BLEAK:
            self.log("ERROR: bleak not installed!")
            self.log("Run: pip install bleak")
            return

        mode = self.mode_var.get()
        if mode == "gamepad":
            if not HAS_EVDEV:
                self.log("ERROR: python-evdev not installed!")
                self.log("Run: pip install evdev")
                return
            if not self.gamepad_mode.available:
                self.log("ERROR: Cannot create uinput device!")
                self.log("Run: sudo modprobe uinput")
                self.log("See --help for uinput permissions setup")
                return

        self._config["mode"] = mode
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

        self.active_mode = self.vr_mode if mode == "vr" else self.gamepad_mode
        self.log(f"Starting {mode.upper()} mode...")

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_tray_running(True)

        self.ble.start()

    def _stop_bridge(self):
        if not self.active_mode:
            return

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self.active_mode = None

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        self.left_panel.set_disconnected()
        self.right_panel.set_disconnected()
        self.set_status("Stopped")
        self.log("Bridge stopped")
        self._set_tray_running(False)

        # Recreate for next start
        self.ble = BLEManager(self)
        if HAS_EVDEV:
            self.gamepad_mode = GamepadMode()

    def on_input(self, hand, state):
        if self.active_mode:
            if isinstance(self.active_mode, GamepadMode):
                self.active_mode.update_gamepad(self.ble.left, self.ble.right)
            else:
                self.active_mode.on_input(hand, state)

    def log(self, msg):
        self.log_queue.put(msg)

    def set_status(self, status):
        self._current_status = status
        self.status_queue.put(status)
        self._update_tray_tooltip()

    def _poll_queues(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                ts = time.strftime("%H:%M:%S")
                self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
            except queue.Empty:
                break

        while not self.status_queue.empty():
            try:
                status = self.status_queue.get_nowait()
                color = COLOR_GREEN if status == "Connected" else \
                        COLOR_RED if "error" in status.lower() or "failed" in status.lower() else \
                        COLOR_ORANGE if "Scanning" in status else COLOR_FG_DIM
                self.status_label.configure(text=status, foreground=color)
            except queue.Empty:
                break

        if self.ble:
            self.left_panel.update_state(self.ble.left)
            self.right_panel.update_state(self.ble.right)

        self.root.after(33, self._poll_queues)

    def run(self):
        self.log("CyberFinger Bridge (Linux) ready")
        self.log(f"BLE: {'bleak available' if HAS_BLEAK else 'NOT available (pip install bleak)'}")
        self.log(f"Gamepad: {'evdev/uinput available' if self.gamepad_mode.available else 'NOT available'}")
        if not HAS_TRAY:
            self.log("System tray: not available (pip install pystray pillow)")
        if not self.gamepad_mode.available and HAS_EVDEV:
            self.log("uinput: permission denied — see README for setup")
        self.root.mainloop()


# ── Hand visualization panel ─────────────────────────────────────────────

class HandPanel:
    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True, padx=(0, 4) if side == tk.LEFT else (4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, height=180)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def update_state(self, state: HandState):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        is_left = self.label == "LEFT"

        if state.connected:
            c.create_text(w // 2, 14, text=f"{self.label}", fill=COLOR_ACCENT,
                         font=("monospace", 11, "bold"))
        else:
            c.create_text(w // 2, 14, text=f"{self.label} (disconnected)",
                         fill=COLOR_FG_DIM, font=("monospace", 10))
            return

        # Battery
        bat = state.battery
        bat_color = COLOR_GREEN if bat > 50 else COLOR_ORANGE if bat > 20 else COLOR_RED
        c.create_text(w - 10, 14, text=f"{bat}%", fill=bat_color,
                     font=("monospace", 9), anchor=tk.E)

        # Packet counter
        c.create_text(10, 14, text=f"#{state.packet_count}", fill=COLOR_FG_DIM,
                     font=("monospace", 8), anchor=tk.W)

        # Joystick
        joy_cx = w // 4 if is_left else 3 * w // 4
        joy_cy = 80
        joy_r = 35

        c.create_oval(joy_cx - joy_r, joy_cy - joy_r,
                     joy_cx + joy_r, joy_cy + joy_r,
                     outline=COLOR_BG3, width=2, fill=COLOR_BG)
        c.create_line(joy_cx - joy_r, joy_cy, joy_cx + joy_r, joy_cy,
                     fill=COLOR_BG3, width=1)
        c.create_line(joy_cx, joy_cy - joy_r, joy_cx, joy_cy + joy_r,
                     fill=COLOR_BG3, width=1)

        jx = state.joy_x_float * (joy_r - 6)
        jy = state.joy_y_float * (joy_r - 6)
        dot_r = 6
        c.create_oval(joy_cx + jx - dot_r, joy_cy + jy - dot_r,
                     joy_cx + jx + dot_r, joy_cy + jy + dot_r,
                     fill=COLOR_ACCENT, outline=COLOR_ACCENT2, width=1)

        # Buttons
        btn_x = 3 * w // 4 if is_left else w // 4
        btn_y_start = 50
        btn_spacing = 24
        btn_names_bits = [
            ("TRIG", BTN_TRIGGER),
            ("GRIP", BTN_GRIP),
            ("B", BTN_B),
            ("A:ST/SE", BTN_A),
            ("JCLK", BTN_JCLICK),
        ]

        for i, (name, bit) in enumerate(btn_names_bits):
            by = btn_y_start + i * btn_spacing
            pressed = bool(state.buttons & bit)
            fill = COLOR_ACCENT if pressed else COLOR_BG
            outline = COLOR_ACCENT if pressed else COLOR_BG3
            c.create_oval(btn_x - 8, by - 8, btn_x + 8, by + 8,
                         fill=fill, outline=outline, width=2)
            c.create_text(btn_x + 18, by, text=name, fill=COLOR_FG if pressed else COLOR_FG_DIM,
                         font=("monospace", 9), anchor=tk.W)

        # Trigger bar
        trig_x = w // 2
        trig_y = 160
        trig_w = w - 40
        trig_h = 10
        trig_val = state.trigger_float

        c.create_rectangle(trig_x - trig_w // 2, trig_y,
                          trig_x + trig_w // 2, trig_y + trig_h,
                          fill=COLOR_BG, outline=COLOR_BG3)
        if trig_val > 0.01:
            fill_w = int(trig_val * trig_w)
            c.create_rectangle(trig_x - trig_w // 2, trig_y,
                              trig_x - trig_w // 2 + fill_w, trig_y + trig_h,
                              fill=COLOR_ACCENT, outline="")
        c.create_text(trig_x, trig_y - 6, text=f"Trigger: {int(trig_val * 100)}%",
                     fill=COLOR_FG_DIM, font=("monospace", 8))

    def set_disconnected(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w > 10:
            c.create_text(w // 2, h // 2, text=f"{self.label}\n(disconnected)",
                         fill=COLOR_FG_DIM, font=("monospace", 10), justify=tk.CENTER)


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
