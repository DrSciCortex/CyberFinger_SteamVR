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
    pip install pynput         # optional, F12 screenshot in VRChat mode
    pip install python-osc     # optional, VRChat OSC (UseLeft, Grab, Voice)
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
import subprocess

try:
    from bleak import BleakScanner, BleakClient
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

try:
    # gi (PyGObject) enables pystray's GTK/AppIndicator backend which supports
    # menus on Linux. If not in the venv, try the system site-packages.
    import gi
except ImportError:
    try:
        import subprocess, sys as _sys
        _gi_path = subprocess.run(
            ["python3", "-c",
             "import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))"],
            capture_output=True, text=True
        ).stdout.strip()
        if _gi_path and _gi_path not in _sys.path:
            _sys.path.insert(0, _gi_path)
        import gi  # noqa: F811
    except Exception:
        pass

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

try:
    from pynput.keyboard import Key, Controller as KeyboardController
    _keyboard = KeyboardController()
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

# ── BLE protocol (matches ESP32 firmware) ────────────────────────────────

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"
VR_CTRL_UUID    = "0000cf02-0000-1000-8000-00805f9b34fb"

GAMEPAD_MAGIC    = 0x50474643
GAMEPAD_PACK_FMT = "<IBBhhBB"
INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)

BTN_TRIGGER = 0x01  # bit0
BTN_GRIP    = 0x02  # bit1
BTN_C       = 0x04  # bit2
BTN_D       = 0x08  # bit3
BTN_E       = 0x10  # bit4
BTN_MENU    = 0x20  # bit5
BTN_JCLICK  = 0x40  # bit6
BTN_STSEL   = 0x80  # bit7

BUTTON_NAMES = {
    BTN_TRIGGER: "TRIG",
    BTN_GRIP:    "GRIP",
    BTN_C:       "C",
    BTN_D:       "D",
    BTN_E:       "E",
    BTN_MENU:    "MENU",
    BTN_JCLICK:  "JCLK",
    BTN_STSEL:   "ST/SE",
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

def _load_tray_icon(filename, bg_color):
    """Load a black-silhouette PNG and composite it as white over bg_color."""
    path = resource_path(os.path.join("assets", filename))
    try:
        raw = Image.open(path).convert("RGBA").resize((32, 32), Image.LANCZOS)
        # The PNGs are black silhouettes on transparent — make them
        # white-on-brand-color so they're visible on any tray background.
        bg = Image.new("RGB", (32, 32), bg_color)
        white = Image.new("RGB", (32, 32), (255, 255, 255))
        alpha = raw.split()[3]          # use icon's alpha as mask
        bg.paste(white, mask=alpha)
        return bg
    except Exception as e:
        print(f"[tray] Failed to load {path}: {e}", flush=True)
        return _generate_fallback_icon(bg_color)


def _load_tray_icon_running():
    return _load_tray_icon("icon_32x32.png", (230, 0, 126))


def _load_tray_icon_idle():
    return _load_tray_icon("icon_32x32_bw.png", (80, 80, 80))


def _generate_fallback_icon(color):
    img = Image.new("RGB", (32, 32), color)
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
        self._clients = []  # (label, BleakClient, char_uuid)

    def start(self):
        self._running = True
        self._task = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running() and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            self.app.log(f"BLE thread error: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._task = None

    async def _run(self):
        self._task = asyncio.current_task()
        try:
            await self._main()
        except asyncio.CancelledError:
            pass
        finally:
            for label, client, char_uuid in self._clients:
                try:
                    if client.is_connected:
                        await client.stop_notify(char_uuid)
                except Exception:
                    pass
                # Do NOT disconnect — the device stays connected as HID.
                # Disconnecting would tear down the physical BLE link and force
                # a slow re-pair/reconnect on the next start.
            self._clients = []
            self.left.connected = False
            self.right.connected = False

    def _get_paired_cyberfinger_devices(self):
        """Return list of objects with .address/.name for paired CyberFinger devices."""
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices", "Paired"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()
        except Exception as e:
            self.app.log(f"bluetoothctl failed: {e}")
            return []

        class _Dev:
            def __init__(self, address, name):
                self.address = address
                self.name = name

        devices = []
        for line in lines:
            # Format: "Device XX:XX:XX:XX:XX:XX DeviceName"
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                address, name = parts[1], parts[2]
                self.app.log(f"  Paired: \"{name}\" ({address})")
                if "cyberfinger" in name.lower():
                    devices.append(_Dev(address, name))
        return devices

    async def _main(self):
        if not HAS_BLEAK:
            self.app.log("ERROR: bleak not installed! pip install bleak")
            self.app.set_status("bleak not installed")
            return

        self.app.log("Looking up paired CyberFinger devices...")
        self.app.set_status("Looking up paired devices...")

        cf_devices = self._get_paired_cyberfinger_devices()

        if not cf_devices:
            self.app.log("No paired CyberFinger devices found!")
            self.app.log("Pair the ESP32s via bluetoothctl first.")
            self.app.set_status("No paired devices found")
            return

        self.app.log(f"Found {len(cf_devices)} CyberFinger device(s)")


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

        self._clients = []
        for label, dev, state in [("LEFT", left_dev, self.left), ("RIGHT", right_dev, self.right)]:
            if dev is None:
                continue
            try:
                await self._connect_device(label, dev, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.app.log(f"Connection error: {e}")

        active = [label for label, *_ in self._clients]
        if not active:
            self.app.log("Failed to connect to any device!")
            self.app.set_status("Connection failed")
            return

        self.app.log(f"Connected: {', '.join(active)}")
        self.app.set_status("Connected")

        try:
            while self._running:
                for label, client, _ in self._clients:
                    if not client.is_connected:
                        self.app.log(f"{label}: Disconnected!")
                        self.app.set_status(f"{label} disconnected")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _connect_device(self, label, dev, state):
        self.app.log(f"{label}: Connecting to {dev.address}...")

        # bleak 3.x always does an active scan in connect() if _device_path is
        # not set, which fails for already-connected paired devices that aren't
        # advertising.  Look up the real D-Bus path from the manager instead.
        manager = await get_global_bluez_manager()
        addr_key = dev.address.upper().replace(":", "_")
        device_path = next(
            (k for k in manager._properties if k.endswith(f"/dev_{addr_key}")),
            None
        )
        if not device_path:
            self.app.log(f"{label}: Device not found in BlueZ manager")
            return
        self.app.log(f"{label}: D-Bus path: {device_path}")

        client = BleakClient(dev.address, timeout=15.0)
        client._backend._device_path = device_path
        try:
            await client.connect()
        except Exception as e:
            # AlreadyConnected is fine — device is still up as HID, just reuse it
            if "AlreadyConnected" not in str(e):
                self.app.log(f"{label}: Connection failed: {e}")
                return

        if not client.is_connected:
            self.app.log(f"{label}: Not connected")
            return

        state.connected = True
        state.name = dev.name or dev.address
        state.address = dev.address

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

        hand_idx = 0 if label == "LEFT" else 1

        def on_notify(sender, data):
            self._handle_data(data, hand_idx, state)

        try:
            await client.start_notify(cf01_char.uuid, on_notify)
            self.app.log(f"{label}: Notifications active")
            self._clients.append((label, client, cf01_char.uuid))
        except Exception as e:
            self.app.log(f"{label}: Notify subscribe failed: {e}")

    def _handle_data(self, data, hand_override, state):
        if len(data) < INPUT_REPORT_SIZE:
            return

        hand, buttons, joy_x, joy_y, trigger, battery, seq = \
            struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])

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


# ── uinput device factory ────────────────────────────────────────────────

def _make_uinput():
    """Create a UInput Xbox 360-like virtual gamepad. Returns (device, available)."""
    if not HAS_EVDEV:
        return None, False
    try:
        cap = {
            ecodes.EV_ABS: [
                (ecodes.ABS_X,     AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_Y,     AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RX,    AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RY,    AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_Z,     AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_RZ,    AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_HAT0X, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_HAT0Y, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
            ],
            ecodes.EV_KEY: [
                ecodes.BTN_A,
                ecodes.BTN_B,
                ecodes.BTN_X,
                ecodes.BTN_Y,
                ecodes.BTN_TL,
                ecodes.BTN_TR,
                ecodes.BTN_SELECT,
                ecodes.BTN_START,
                ecodes.BTN_THUMBL,
                ecodes.BTN_THUMBR,
                ecodes.BTN_MODE,
            ],
        }
        dev = UInput(cap, name="CyberFinger Virtual Gamepad",
                     vendor=0x045e, product=0x028e, version=0x0110)
        return dev, True
    except PermissionError:
        return None, False
    except Exception:
        return None, False


# ── Gamepad Mode (evdev/uinput virtual Xbox 360, Resonite) ───────────────

class GamepadMode:
    """Creates a virtual Xbox 360-like gamepad via Linux uinput."""

    def __init__(self):
        self.device, self.available = _make_uinput()

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available or not self.device:
            return

        dev = self.device

        dev.write(ecodes.EV_ABS, ecodes.ABS_X,  left.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Y,  left.joy_y)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RX, right.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RY, right.joy_y)

        # Triggers (analog)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Z,  int(left.trigger_float  * 255))
        dev.write(ecodes.EV_ABS, ecodes.ABS_RZ, int(right.trigger_float * 255))

        # Right hand buttons (mirrors Windows vgamepad mapping)
        dev.write(ecodes.EV_KEY, ecodes.BTN_A,      1 if (right.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_B,      1 if (right.buttons & BTN_GRIP)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TR,     1 if (right.buttons & BTN_MENU)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBR, 1 if (right.buttons & BTN_JCLICK)  else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_START,  1 if (right.buttons & BTN_STSEL)   else 0)

        # Left hand buttons
        dev.write(ecodes.EV_KEY, ecodes.BTN_X,      1 if (left.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_Y,      1 if (left.buttons & BTN_GRIP)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TL,     1 if (left.buttons & BTN_MENU)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBL, 1 if (left.buttons & BTN_JCLICK)  else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_SELECT, 1 if (left.buttons & BTN_STSEL)   else 0)

        # C/D/E → D-pad + guide (mirrors Windows wButtons bit mapping)
        # Right C→UP, Right D→DOWN, Right E→LEFT, Left C→RIGHT, Left D→GUIDE
        hat_x = 0
        hat_y = 0
        if right.buttons & BTN_C: hat_y = -1   # up
        if right.buttons & BTN_D: hat_y =  1   # down
        if right.buttons & BTN_E: hat_x = -1   # left
        if left.buttons  & BTN_C: hat_x =  1   # right
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0X, hat_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0Y, hat_y)
        dev.write(ecodes.EV_KEY, ecodes.BTN_MODE, 1 if (left.buttons & BTN_D) else 0)

        dev.syn()

    def stop(self):
        self.available = False      # prevent racing BLE callbacks from touching closed fd
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None


# ── Gamepad Mode VRChat (evdev/uinput + OSC) ─────────────────────────────

class GamepadModeVRChat:
    """
    uinput Xbox 360 gamepad with VRChat-optimal button mapping + OSC.

    VRChat gamepad layout (Xbox reference):
      Left  stick        → Move
      Right stick X      → Smooth turn
      Right stick Y      → Look up/down
      RT (right trigger) → Use / Interact (right hand)
      A                  → Jump
      R3                 → Action Menu right
      Start              → Quick Menu

    CyberFinger → Xbox mapping:
      Right TRIGGER  → RT  (use/interact right)
      Left  TRIGGER  → OSC UseLeft (no LT gamepad — avoids duplicate events)
      Right GRIP     → OSC GrabRight (tap=toggle, hold≥200ms=release on lift)
      Left  GRIP     → OSC GrabLeft
      Right MENU     → R3  (Action Menu right)
      Left  MENU     → Start (Quick Menu)
      Right JCLICK / Left JCLICK → A (Jump)
      Right C        → F12 screenshot (rising edge, via pynput)
      Left  C        → X (Mute)
      Right D        → DPAD_RIGHT
      Left  D        → DPAD_LEFT
      Right E        → DPAD_UP
      Left  E        → DPAD_DOWN
      Right ST/SE    → OSC chatbox open (rising edge)
      Left  ST/SE    → OSC Voice mute toggle
    """

    def __init__(self):
        self.device, self.available = _make_uinput()

        self._osc = None
        try:
            from pythonosc import udp_client
            self._osc = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        except ImportError:
            pass

        self._prev_use_l             = False
        self._prev_grab_r            = False
        self._prev_grab_l            = False
        self._grab_right_toggled     = False
        self._grab_left_toggled      = False
        self._grab_right_press_time  = 0.0
        self._grab_left_press_time   = 0.0
        self._prev_c_r               = False
        self._prev_stsel_r           = False
        self._prev_stsel_l           = False

    def _osc_send(self, address, value):
        if self._osc:
            try:
                self._osc.send_message(address, value)
            except Exception:
                pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available or not self.device:
            return

        dev = self.device

        dev.write(ecodes.EV_ABS, ecodes.ABS_X,  left.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Y,  left.joy_y)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RX, right.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RY, right.joy_y)

        # Right trigger → RT; left trigger → OSC UseLeft only (no LT gamepad)
        trig_r = max(right.trigger_float, 1.0 if (right.buttons & BTN_TRIGGER) else 0.0)
        trig_l = max(left.trigger_float,  1.0 if (left.buttons  & BTN_TRIGGER) else 0.0)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RZ, int(trig_r * 255))
        dev.write(ecodes.EV_ABS, ecodes.ABS_Z,  0)  # suppressed — UseLeft via OSC

        # OSC: UseLeft
        use_l = trig_l > 0.1
        if use_l != self._prev_use_l:
            self._osc_send("/input/UseLeft", int(use_l))
            self._prev_use_l = use_l

        # OSC: GrabRight / GrabLeft (tap=toggle, hold≥200ms=release on lift)
        grab_r = bool(right.buttons & BTN_GRIP)
        grab_l = bool(left.buttons  & BTN_GRIP)

        if grab_r and not self._prev_grab_r:
            self._grab_right_press_time = time.time()
            self._osc_send("/input/GrabRight", 1)
        elif not grab_r and self._prev_grab_r:
            held_ms = (time.time() - self._grab_right_press_time) * 1000
            if held_ms < 200:
                self._grab_right_toggled = not self._grab_right_toggled
                self._osc_send("/input/GrabRight", int(self._grab_right_toggled))
            else:
                self._grab_right_toggled = False
                self._osc_send("/input/GrabRight", 0)

        if grab_l and not self._prev_grab_l:
            self._grab_left_press_time = time.time()
            self._osc_send("/input/GrabLeft", 1)
        elif not grab_l and self._prev_grab_l:
            held_ms = (time.time() - self._grab_left_press_time) * 1000
            if held_ms < 200:
                self._grab_left_toggled = not self._grab_left_toggled
                self._osc_send("/input/GrabLeft", int(self._grab_left_toggled))
            else:
                self._grab_left_toggled = False
                self._osc_send("/input/GrabLeft", 0)

        self._prev_grab_r = grab_r
        self._prev_grab_l = grab_l

        # Jump (either jclick → A)
        jclick = bool(right.buttons & BTN_JCLICK) or bool(left.buttons & BTN_JCLICK)
        dev.write(ecodes.EV_KEY, ecodes.BTN_A, 1 if jclick else 0)

        # Action Menu R (right MENU → R3)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBR, 1 if (right.buttons & BTN_MENU) else 0)

        # Quick Menu (left MENU → Start)
        dev.write(ecodes.EV_KEY, ecodes.BTN_START, 1 if (left.buttons & BTN_MENU) else 0)

        # Mute (left C → X)
        dev.write(ecodes.EV_KEY, ecodes.BTN_X, 1 if (left.buttons & BTN_C) else 0)

        # D-pad
        hat_x = 0
        hat_y = 0
        if right.buttons & BTN_D: hat_x =  1   # right
        if left.buttons  & BTN_D: hat_x = -1   # left
        if right.buttons & BTN_E: hat_y = -1   # up
        if left.buttons  & BTN_E: hat_y =  1   # down
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0X, hat_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0Y, hat_y)

        dev.syn()

        # Right C → F12 screenshot (rising edge)
        c_r = bool(right.buttons & BTN_C)
        if c_r and not self._prev_c_r and HAS_PYNPUT:
            try:
                _keyboard.press(Key.f12)
                _keyboard.release(Key.f12)
            except Exception:
                pass
        self._prev_c_r = c_r

        # Right ST/SE → open VRChat chatbox (rising edge)
        stsel_r = bool(right.buttons & BTN_STSEL)
        if stsel_r and not self._prev_stsel_r:
            self._osc_send("/chatbox/input", ["", False, False])
        self._prev_stsel_r = stsel_r

        # Left ST/SE → OSC Voice (1 on press, 0 on release)
        stsel_l = bool(left.buttons & BTN_STSEL)
        if stsel_l != self._prev_stsel_l:
            self._osc_send("/input/Voice", int(stsel_l))
        self._prev_stsel_l = stsel_l

    def stop(self):
        self.available = False      # prevent racing BLE callbacks from touching closed fd
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        for addr in ("/input/UseLeft", "/input/GrabRight", "/input/GrabLeft", "/input/Voice"):
            self._osc_send(addr, 0)


# ── GUI Application ──────────────────────────────────────────────────────

class CyberFingerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Bridge")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("680x620")
        self.root.minsize(600, 540)

        menubar = tk.Menu(self.root, tearoff=0)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Exit", command=self._quit_app)
        menubar.add_cascade(label="CyberFinger", menu=app_menu)
        self.root.config(menu=menubar)

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

        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        self._config_dir = os.path.join(config_home, "cyberfinger-bridge")
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = VRMode()
        self.gamepad_mode = None         # created lazily on start
        self.vrchat_gamepad_mode = None  # created lazily on start
        self.active_mode = None

        self._build_ui()

        self._tray_icon = None
        if HAS_TRAY:
            self._setup_tray()

        self._poll_queues()

        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

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
            "CyberFinger Bridge - Idle",
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
            self._tray_icon.title = f"CyberFinger Bridge - {self._current_status}" + \
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
        radio_frame = ttk.Frame(ctrl_frame)
        radio_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(radio_frame, text="VR Mode (BLE→SteamVR)",
                        variable=self.mode_var, value="vr").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→uinput Xbox 360, Resonite)",
                        variable=self.mode_var, value="gamepad").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→uinput Xbox 360, VRChat)",
                        variable=self.mode_var, value="gamepad_vrc").pack(anchor=tk.W)

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
        if mode in ("gamepad", "gamepad_vrc"):
            if not HAS_EVDEV:
                self.log("ERROR: python-evdev not installed!")
                self.log("Run: pip install evdev")
                return
            gp = GamepadMode() if mode == "gamepad" else GamepadModeVRChat()
            if not gp.available:
                self.log("ERROR: Cannot create uinput device!")
                self.log("Run: sudo modprobe uinput")
                self.log("See file header for uinput permissions setup")
                return
            if mode == "gamepad":
                self.gamepad_mode = gp
                self.active_mode = self.gamepad_mode
            else:
                self.vrchat_gamepad_mode = gp
                self.active_mode = self.vrchat_gamepad_mode
        else:
            self.active_mode = self.vr_mode

        self._config["mode"] = mode
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

        self.log(f"Starting {mode.upper()} mode...")
        if mode == "gamepad_vrc":
            self.log(">>> VRChat: enable OSC via Action Menu → OSC → Enabled")

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
        self.gamepad_mode = None
        self.vrchat_gamepad_mode = None

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        self.left_panel.set_disconnected()
        self.right_panel.set_disconnected()
        self.set_status("Stopped")
        self.log("Bridge stopped")
        self._set_tray_running(False)

        self.ble = BLEManager(self)

    def on_input(self, hand, state):
        if self.active_mode:
            if isinstance(self.active_mode, (GamepadMode, GamepadModeVRChat)):
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

        self.root.after(33, self._poll_queues)  # ~30fps

    def run(self):
        self.log("CyberFinger Bridge (Linux) ready")
        self.log(f"BLE: {'bleak available' if HAS_BLEAK else 'NOT available (pip install bleak)'}")
        if HAS_EVDEV:
            if os.access('/dev/uinput', os.W_OK):
                self.log("Gamepad: evdev/uinput available")
            else:
                self.log("Gamepad: uinput permission denied — see file header for setup")
        else:
            self.log("Gamepad: NOT available (pip install evdev)")
        if not HAS_TRAY:
            self.log("System tray: not available (pip install pystray pillow)")
        if not HAS_PYNPUT:
            self.log("Keyboard (F12 screenshot): not available (pip install pynput)")
        self.root.mainloop()


# ── Hand visualization panel ─────────────────────────────────────────────

class HandPanel:
    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True, padx=(0, 4) if side == tk.LEFT else (4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, height=210)
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
        btn_y_start = 30
        btn_spacing = 17
        btn_names_bits = [
            ("TRIG",  BTN_TRIGGER),
            ("GRIP",  BTN_GRIP),
            ("C",     BTN_C),
            ("D",     BTN_D),
            ("E",     BTN_E),
            ("MENU",  BTN_MENU),
            ("JCLK",  BTN_JCLICK),
            ("ST/SE", BTN_STSEL),
        ]

        for i, (name, bit) in enumerate(btn_names_bits):
            by = btn_y_start + i * btn_spacing
            pressed = bool(state.buttons & bit)
            fill = COLOR_ACCENT if pressed else COLOR_BG
            outline = COLOR_ACCENT if pressed else COLOR_BG3
            c.create_oval(btn_x - 7, by - 7, btn_x + 7, by + 7,
                         fill=fill, outline=outline, width=2)
            c.create_text(btn_x + 14, by, text=name, fill=COLOR_FG if pressed else COLOR_FG_DIM,
                         font=("monospace", 8), anchor=tk.W)

        # Trigger bar
        trig_x = w // 2
        trig_y = 168
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
