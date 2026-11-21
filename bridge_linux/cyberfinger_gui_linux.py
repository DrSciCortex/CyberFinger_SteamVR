# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
CyberFinger Bridge GUI — Linux version

Uses bleak (cross-platform BLE) and python-evdev/uinput for virtual gamepad.
Combines VR (BLE→UDP, to the CyberFinger driver in WiVRn) and Gamepad
(BLE→uinput Xbox 360) bridge modes.

The wire protocol, the report decoding, haptics, the pink button and the tap
gesture are the Windows bridge's own modules, imported from ../bridge.

Prerequisites:
    pip install bleak pystray pillow
    pip install evdev          # for gamepad mode
    pip install pynput         # optional, F12 screenshot in VRChat mode
    pip install python-osc     # optional, VRChat OSC (UseLeft, Grab, Voice)
    sudo modprobe uinput       # load uinput kernel module
    wpctl or pactl             # optional, the right pink button's microphone mute

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
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext
import struct
import socket
import time
import sys
import os
import queue
import json
import shutil
import subprocess

# The modules shared with the Windows bridge live in ../bridge, wherever this is started from.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _dir in (_HERE, os.path.join(os.path.dirname(_HERE), "bridge")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import cf_protocol  # driver wire protocol (CFG2 CyberFinger packets)
import cyberfinger_report  # the CyberFinger's BLE input report, every firmware revision
import cyberfinger_control  # bridge → CyberFinger commands (haptics)
import flux_actions   # FluxAction1..42 of the MoreFluxActions mod
import pink_button    # the right pink button: microphone mute or a FluxAction
import tap_gesture    # a triple tap on the joint IMU: resync the driver's IMU fusion
import bridge_version  # "v2.2.1 (<commit>)" for the header
from driver_link import DriverLink          # driver → bridge: haptic requests, driver status
from haptics_view import describe as describe_haptic, draw_haptic_meter
from hand_views import (COLOR_BG, COLOR_BG2, COLOR_BG3, COLOR_FG, COLOR_FG_DIM, COLOR_ACCENT, COLOR_ACCENT2,
                        COLOR_GREEN, COLOR_RED, COLOR_ORANGE,                 # the palette
                        quat_to_euler_deg, draw_triad, SkeletonRenderer, StreamView)  # drawn in both bridges' GUIs
from driver_skeleton import DriverSkeletonSource   # the Skeleton tab's hands, from the driver's status

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

# The GATT report grew with each firmware revision (12, 28, 61/79 bytes, then v1.3's variable-length IMU
# tail); cyberfinger_report.py decodes them all.
INPUT_REPORT_SIZE = cyberfinger_report.BASE.size   # shortest valid report

ZERO_ACCEL = (0, 0, 0)
ACCEL_LSB_PER_G = 2048.0  # VR_ACCEL_LSB_PER_G — ±16g on every sensor
GRAVITY_MS2 = 9.80665

# VrImuBit — which quaternion slots carry real data
IMU_BODY_PRIMARY   = 0x01
IMU_BODY_SECONDARY = 0x02
IMU_JOINT          = 0x04

IMU_SLOT_LABELS = (
    (IMU_BODY_PRIMARY,   "BODY 1"),
    (IMU_BODY_SECONDARY, "BODY 2"),
    (IMU_JOINT,          "JOINT"),
)

IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)

# ── SlimeVR tracker emulation ────────────────────────────────────────────
#
# Wire format taken from SlimeVR-Tracker-ESP (src/network/{packets.h,
# connection.cpp}). Everything multi-byte is BIG-endian — the opposite of the
# BLE report above, which is the easy mistake to make here.
#
# Outbound framing is a 4-byte packet type (three zero bytes then the type)
# followed by a big-endian u64 packet counter, then the payload. Handshake is
# the one exception: it always carries packet number 0.

SLIME_DEFAULT_HOST = "127.0.0.1"
SLIME_DEFAULT_PORT = 6969

SLIME_SEND_HEARTBEAT     = 0
SLIME_SEND_HANDSHAKE     = 3
SLIME_SEND_ACCEL         = 4
SLIME_SEND_BATTERY_LEVEL = 12
SLIME_SEND_SENSOR_INFO   = 15
SLIME_SEND_ROTATION_DATA = 17

SLIME_RECV_HEARTBEAT = 1
SLIME_RECV_HANDSHAKE = 3
SLIME_RECV_PING_PONG = 10

SLIME_HANDSHAKE_REPLY = b"Hey OVR =D 5"

SLIME_BOARD            = 4    # BOARD_CUSTOM
SLIME_MCU              = 2    # MCU_ESP32
SLIME_IMU_TYPE         = 16   # SensorTypeID::ICM45686 — what CyberFinger actually runs
SLIME_PROTOCOL_VERSION = 22
# TRACKER_TYPE_SVR_ROTATION. The GLOVE_LEFT/RIGHT types exist, but they make the
# server expect per-finger sensors we do not have, so we present as plain
# rotation trackers and let the user assign body parts in the SlimeVR GUI.
SLIME_TRACKER_TYPE_ROTATION = 0

SLIME_SENSOR_OFFLINE = 0
SLIME_SENSOR_OK      = 1
SLIME_DATA_TYPE_NORMAL     = 1  # DATA_TYPE_NORMAL
SLIME_SENSOR_DATA_ROTATION = 0  # SENSOR_DATATYPE_ROTATION

# SensorPosition, from sensors/sensorposition.h
SLIME_POS_LEFT_LOWER_ARM  = 13
SLIME_POS_RIGHT_LOWER_ARM = 14
SLIME_POS_LEFT_HAND       = 17
SLIME_POS_RIGHT_HAND      = 18

# Sensor ids within one emulated tracker
SLIME_SENSOR_BODY  = 0
SLIME_SENSOR_JOINT = 1

# The server drops a tracker after 3 s of silence, so the service thread has to
# keep answering heartbeats even when no CyberFinger data is flowing.
SLIME_TIMEOUT = 3.0

SLIME_FIRMWARE_VERSION = "CyberFinger"
SLIME_VENDOR_NAME      = "DrSciCortex"
SLIME_VENDOR_URL       = "https://github.com/DrSciCortex"
SLIME_PRODUCT_NAME     = "CyberFinger"

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


def _black_hold():
    """The driver's black-button long press, from its settings (~/.config/cyberfinger/wivrn.json): how long the
    button is held for it, in seconds (black_hold_ms, 800 ms by default; 0 = none), and the FluxAction it presses
    in Resonite for the left hand, the right hand's being the next one (flux_actions.a_hold, 1 by default;
    0 = none)."""
    seconds, flux = 0.8, 1
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config_home, "cyberfinger", "wivrn.json"), encoding="utf-8") as f:
            settings = json.load(f)
        seconds = max(0, int(settings.get("black_hold_ms", 800))) / 1000.0
        flux = max(0, int((settings.get("flux_actions") or {}).get("a_hold", 1)))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return seconds, flux


BLACK_HOLD = _black_hold()


def linear_accel_ms2(quat, accel_raw):
    """Gravity-corrected acceleration in the SENSOR frame, in m/s².

    Uses the quaternion from the same packet to rotate gravity into the sensor
    frame and subtract it. This is the derivation documented in vr_gatt.h, which
    is SlimeVR's own, so the result feeds straight into PACKET_ACCEL.
    """
    q0, q1, q2, q3 = quat
    gx = 2.0 * (q1 * q3 - q0 * q2)
    gy = 2.0 * (q0 * q1 + q2 * q3)
    gz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3
    scale = GRAVITY_MS2 / ACCEL_LSB_PER_G
    return (accel_raw[0] * scale - gx * GRAVITY_MS2,
            accel_raw[1] * scale - gy * GRAVITY_MS2,
            accel_raw[2] * scale - gz * GRAVITY_MS2)


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
        self.buttons2 = 0          # the extension byte's buttons (cyberfinger_report.PINK)
        self.joy_x = 0
        self.joy_y = 0
        self.trigger = 0
        self.battery = 100
        self.packet_count = 0
        self.timestamp = 0.0
        self.connected = False
        self.name = ""
        self.address = ""
        # Orientation — absent slots stay identity. imu_present is 0 on older
        # firmware and on units with no working IMU, which is what drives the
        # "no IMU installed" placeholder in the panel.
        self.imu_present = 0
        self.quat = IDENTITY_QUAT        # primary body IMU
        self.quat_body2 = IDENTITY_QUAT  # secondary body IMU
        self.quat_joint = IDENTITY_QUAT  # joint IMU
        # Raw sensor-frame acceleration per slot, ACCEL_LSB_PER_G counts. Only
        # meaningful when has_accel is set — firmware predating the 79-byte
        # report leaves these zero, which is NOT the same as "at rest".
        self.has_accel = False
        self.accel = ZERO_ACCEL
        self.accel_body2 = ZERO_ACCEL
        self.accel_joint = ZERO_ACCEL
        self.rx_perf = 0.0       # time.perf_counter() when the latest report arrived
        self.report_seq = 0      # the latest report's own sequence number

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

    def reset_link(self):
        """Clear per-connection capability flags before (re)attaching a CyberFinger."""
        self.imu_present = 0
        self.quat = IDENTITY_QUAT
        self.quat_body2 = IDENTITY_QUAT
        self.quat_joint = IDENTITY_QUAT
        self.has_accel = False
        self.accel = ZERO_ACCEL
        self.accel_body2 = ZERO_ACCEL
        self.accel_joint = ZERO_ACCEL

    @property
    def has_imu(self):
        return self.imu_present != 0

    def active_imus(self):
        """[(label, quat)] for slots this unit actually populates, in wire order."""
        quats = {
            IMU_BODY_PRIMARY:   self.quat,
            IMU_BODY_SECONDARY: self.quat_body2,
            IMU_JOINT:          self.quat_joint,
        }
        return [(label, quats[bit]) for bit, label in IMU_SLOT_LABELS
                if self.imu_present & bit]


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
        self.haptics = cyberfinger_control.HapticSender(log=app.log)   # vibration commands to the CyberFingers

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
            self.haptics.detach_all()
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
        state.reset_link()

        cf01_char = cf02_char = None
        for service in client.services:
            if "cf00" in str(service.uuid).lower():
                for char in service.characteristics:
                    if "cf01" in str(char.uuid).lower() and not cf01_char:
                        cf01_char = char
                    elif "cf02" in str(char.uuid).lower() and not cf02_char:
                        cf02_char = char

        if not cf01_char:
            self.app.log(f"{label}: CF01 characteristic not found!")
            self.app.log(f"{label}: Services: {[str(s.uuid) for s in client.services]}")
            return

        hand_idx = 0 if label == "LEFT" else 1

        # The control characteristic: haptics go there (cyberfinger_control.py)
        if cf02_char is not None:
            self.haptics.attach(hand_idx, cyberfinger_control.BleakControl(client, cf02_char))
        else:
            self.app.log(f"{label}: CF02 control characteristic not found (no haptics)")

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
        t_rx = time.perf_counter()

        # Every firmware revision's layout, including v1.3's variable-length IMU tail (cyberfinger_report.py).
        r = cyberfinger_report.decode(data)
        buttons, joy_x, joy_y, trigger, battery, seq = (
            r.buttons, r.joy_x, r.joy_y, r.trigger, r.battery, r.seq)
        present, quats, accels, has_accel = r.present, r.quats, r.accels, r.has_accel

        h = hand_override

        if present != state.imu_present:
            hn = "L" if h == 0 else "R"
            names = [label for bit, label in IMU_SLOT_LABELS if present & bit]
            self.app.log(f"{hn} IMU: {', '.join(names) if names else 'none detected'}")
        state.imu_present = present
        state.quat, state.quat_body2, state.quat_joint = quats
        state.has_accel = has_accel
        state.accel, state.accel_body2, state.accel_joint = accels
        state.rx_perf = t_rx
        state.report_seq = seq

        old_buttons = state.buttons
        state.buttons = buttons
        if r.buttons2 != state.buttons2 and r.buttons2:
            self.app.log(f"{'L' if h == 0 else 'R'} PINK")
        state.buttons2 = r.buttons2
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
    """CyberFinger → WiVRn driver: per BLE report one CFG2 packet (see cf_protocol.py) and, when the CyberFinger has
    IMUs, one CFIM packet with their raw slots (the driver's own IMU fusion)."""

    STICK_DEADZONE = 0.12

    def __init__(self, port=cf_protocol.DRIVER_PORT):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = ("127.0.0.1", port)
        self.seq = [0, 0]
        self.resync = [0, 0]     # CFG2's resync count per hand: a new value resyncs the driver's IMU fusion
        # The right pink button goes on to the driver only when the bridge leaves it to it; otherwise the bridge
        # acts on it (mic mute, a FluxAction) and the driver never sees it. The left one always goes on.
        self.right_pink_to_driver = False

    def resync_imu(self, hands=(0, 1)):
        """Ask the driver to resync the IMU fusion of these hands (with their next CFG2 packets)."""
        for hand in hands:
            self.resync[hand] = (self.resync[hand] + 1) & 0xFF

    def on_input(self, hand, state):
        # The firmware's stick +y is down; CFG2's is up.
        joy_x, joy_y = cf_protocol.stick_to_int16(state.joy_x_float, -state.joy_y_float,
                                                  self.STICK_DEADZONE)
        self.seq[hand] += 1
        buttons2 = state.buttons2
        if hand == 1 and not self.right_pink_to_driver:
            buttons2 &= ~cyberfinger_report.PINK
        pkt = cf_protocol.pack_cyberfinger(hand, self.seq[hand], state.buttons, state.trigger,
                                     joy_x, joy_y, state.battery, buttons2=buttons2,
                                     resync=self.resync[hand])
        try:
            self.sock.sendto(pkt, self.target)
            if state.imu_present:
                age_us = (time.perf_counter() - state.rx_perf) * 1e6 if state.rx_perf else 0
                self.sock.sendto(cf_protocol.pack_imu(
                    hand, state.report_seq, state.imu_present, (state.quat, state.quat_body2, state.quat_joint),
                    (state.accel, state.accel_body2, state.accel_joint) if state.has_accel else None,
                    age_us=age_us), self.target)
        except Exception:
            pass

    def stop(self):
        self.sock.close()


# ── SlimeVR forwarding (runs alongside whichever mode is active) ─────────

class SlimeVRTracker:
    """One emulated SlimeVR tracker — one CyberFinger, up to two sensors.

    The server keys trackers by the MAC in the handshake, so each CyberFinger gets a
    stable synthetic MAC and its own socket. Rotation packets are pushed from
    the BLE thread via send_rotation(); a service thread owns the handshake,
    heartbeat replies and periodic sensor-info re-announcements.
    """

    def __init__(self, hand, host, port, log=None):
        self.hand = hand  # 0 = left, 1 = right
        self.hand_name = "L" if hand == 0 else "R"
        self.target = (host, port)
        self._log = log or (lambda msg: None)

        # Locally-administered MAC (0x02 prefix) so it cannot collide with real
        # hardware, stable across restarts so SlimeVR keeps its assignment.
        self.mac = bytes((0x02, 0xCF, 0x00, 0x00, 0x00, hand + 1))

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", 0))  # ephemeral — the server owns 6969
        self.sock.settimeout(0.2)

        self._lock = threading.Lock()
        self._packet_number = 0
        self._connected = False
        self._last_inbound = 0.0
        self._last_handshake = 0.0
        self._last_sensor_info = 0.0
        self._sensors = ()  # ((sensor_id, position), ...) currently advertised
        self._running = False
        self._thread = None

    # ── framing ──

    def _send(self, ptype, payload, packet_number=None):
        with self._lock:
            if packet_number is None:
                packet_number = self._packet_number
                self._packet_number += 1
            pkt = struct.pack(">IQ", ptype, packet_number) + payload
            try:
                self.sock.sendto(pkt, self.target)
            except Exception:
                pass

    @staticmethod
    def _short_string(text):
        raw = text.encode("utf-8")[:255]
        return bytes((len(raw),)) + raw

    def _handshake_payload(self):
        sstr = self._short_string
        return (struct.pack(">IIIIIII",
                            SLIME_BOARD, SLIME_IMU_TYPE, SLIME_MCU,
                            0, 0, 0,  # legacy IMU fields, unused
                            SLIME_PROTOCOL_VERSION)
                + sstr(SLIME_FIRMWARE_VERSION)
                + self.mac
                + bytes((SLIME_TRACKER_TYPE_ROTATION,))
                + sstr(SLIME_VENDOR_NAME)
                + sstr(SLIME_VENDOR_URL)
                + sstr(SLIME_PRODUCT_NAME)
                + sstr("")   # UPDATE_ADDRESS
                + sstr(""))  # UPDATE_NAME

    # ── outbound data ──

    def set_sensors(self, sensors):
        """Declare which sensors this tracker exposes, as ((id, position), ...)."""
        if sensors == self._sensors:
            return
        # Retire anything that just disappeared so the server stops waiting on it.
        gone = [s for s in self._sensors if s not in sensors]
        self._sensors = tuple(sensors)
        if self._connected:
            for sid, pos in gone:
                self._send_sensor_info(sid, pos, SLIME_SENSOR_OFFLINE)
        self._last_sensor_info = 0.0  # re-announce on the next service tick

    def _send_sensor_info(self, sensor_id, position, state=SLIME_SENSOR_OK):
        self._send(SLIME_SEND_SENSOR_INFO,
                   struct.pack(">BBBHBBBff",
                               sensor_id, state, SLIME_IMU_TYPE,
                               0,      # sensorConfigData
                               0,      # hasCompletedRestCalibration
                               position, SLIME_SENSOR_DATA_ROTATION,
                               0.0, 0.0))  # TPS counters, debug only

    def send_rotation(self, sensor_id, quat):
        """quat is CyberFinger order (w, x, y, z); the wire wants x, y, z, w."""
        if not self._connected:
            return
        w, x, y, z = quat
        self._send(SLIME_SEND_ROTATION_DATA,
                   struct.pack(">BBffffB", sensor_id, SLIME_DATA_TYPE_NORMAL,
                               x, y, z, w, 0))

    def send_accel(self, sensor_id, accel):
        """Gravity-corrected sensor-frame acceleration, m/s². Note that unlike
        every other packet the sensor id comes last here."""
        if not self._connected:
            return
        x, y, z = accel
        self._send(SLIME_SEND_ACCEL, struct.pack(">fffB", x, y, z, sensor_id))

    def send_battery(self, percent):
        if not self._connected:
            return
        frac = max(0.0, min(1.0, percent / 100.0))
        # The server wants a voltage too; approximate a single li-ion cell.
        self._send(SLIME_SEND_BATTERY_LEVEL,
                   struct.pack(">ff", 3.3 + 0.9 * frac, frac))

    # ── service thread ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._service_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._connected:
            for sid, pos in self._sensors:
                self._send_sensor_info(sid, pos, SLIME_SENSOR_OFFLINE)
        self._connected = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.sock.close()
        except Exception:
            pass

    def _service_loop(self):
        while self._running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                data = None
            except Exception:
                data = None
            if data:
                self._handle_inbound(data, addr)

            now = time.time()
            if not self._connected:
                if now - self._last_handshake >= 1.0:
                    self._last_handshake = now
                    self._send(SLIME_SEND_HANDSHAKE, self._handshake_payload(),
                               packet_number=0)
            elif now - self._last_inbound > SLIME_TIMEOUT:
                self._connected = False
                self._log(f"SlimeVR: {self.hand_name} timed out, re-announcing")
            elif now - self._last_sensor_info >= 1.0:
                self._last_sensor_info = now
                for sid, pos in self._sensors:
                    self._send_sensor_info(sid, pos)

    def _handle_inbound(self, data, addr):
        # The handshake reply is unframed: type in byte 0, payload right after.
        # Framed packets always start with a zero byte, so there is no ambiguity.
        if data[0] == SLIME_RECV_HANDSHAKE:
            if data[1:13] != SLIME_HANDSHAKE_REPLY:
                return
            self.target = addr  # latch the server's real source port
            self._last_inbound = time.time()
            if not self._connected:
                self._connected = True
                self._last_sensor_info = 0.0
                self._log(f"SlimeVR: {self.hand_name} tracker connected")
            return

        if len(data) < 4:
            return
        self._last_inbound = time.time()

        ptype = data[3]
        if ptype == SLIME_RECV_HEARTBEAT:
            self._send(SLIME_SEND_HEARTBEAT, b"")
        elif ptype == SLIME_RECV_PING_PONG:
            with self._lock:
                try:
                    self.sock.sendto(data, self.target)  # echoed verbatim
                except Exception:
                    pass


class SlimeVRForwarder:
    """Feeds CyberFinger IMU quaternions to a SlimeVR server as two emulated trackers.

    Runs in parallel with the active mode rather than replacing it — VR/Gamepad
    still get buttons and sticks while SlimeVR gets orientation.
    """

    # (sensor id, left position, right position) per logical sensor
    _BODY_POS  = (SLIME_POS_LEFT_LOWER_ARM, SLIME_POS_RIGHT_LOWER_ARM)
    _JOINT_POS = (SLIME_POS_LEFT_HAND, SLIME_POS_RIGHT_HAND)

    def __init__(self, host=SLIME_DEFAULT_HOST, port=SLIME_DEFAULT_PORT,
                 body_slot="body1", log=None):
        self.body_slot = body_slot
        self._log = log or (lambda msg: None)
        self.trackers = {
            0: SlimeVRTracker(0, host, port, log),
            1: SlimeVRTracker(1, host, port, log),
        }
        self._last_battery = {0: 0.0, 1: 0.0}

    def start(self):
        for tracker in self.trackers.values():
            tracker.start()

    def stop(self):
        for tracker in self.trackers.values():
            tracker.stop()

    def set_body_slot(self, body_slot):
        self.body_slot = body_slot

    def _body_slot(self, state):
        """Pick between the two redundant body IMUs, honouring the user's choice.

        Body 1 and Body 2 are the same physical location (ICM at 0x69, QMI at
        0x6B), not two tracked points, so only one is ever forwarded.
        """
        order = ((IMU_BODY_PRIMARY, state.quat, state.accel),
                 (IMU_BODY_SECONDARY, state.quat_body2, state.accel_body2))
        if self.body_slot == "body2":
            order = tuple(reversed(order))
        for bit, quat, accel in order:
            if state.imu_present & bit:
                return quat, accel
        return None

    def on_input(self, hand, state):
        """Called from the BLE thread on each input report."""
        tracker = self.trackers.get(hand)
        if tracker is None:
            return

        body = self._body_slot(state)
        joint = ((state.quat_joint, state.accel_joint)
                 if state.imu_present & IMU_JOINT else None)

        sensors = []
        if body is not None:
            sensors.append((SLIME_SENSOR_BODY, self._BODY_POS[hand]))
        if joint is not None:
            sensors.append((SLIME_SENSOR_JOINT, self._JOINT_POS[hand]))
        tracker.set_sensors(tuple(sensors))

        for sensor_id, slot in ((SLIME_SENSOR_BODY, body),
                                (SLIME_SENSOR_JOINT, joint)):
            if slot is None:
                continue
            quat, accel_raw = slot
            tracker.send_rotation(sensor_id, quat)
            # Older firmware sends no accel at all; its zeroed vector would
            # decode as a constant 1g of linear acceleration, so skip it.
            if state.has_accel:
                tracker.send_accel(sensor_id, linear_accel_ms2(quat, accel_raw))

        now = time.time()
        if now - self._last_battery[hand] >= 10.0:
            self._last_battery[hand] = now
            tracker.send_battery(state.battery)


# ── Hand skeleton (display only) ─────────────────────────────────────────
#
# The Skeleton tab draws the hands the headset tracks, as the driver reports
# them back (driver_skeleton.py), with the renderer the Windows bridge uses
# (hand_views.SkeletonRenderer).


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
        self.root.geometry("680x720")
        self.root.minsize(600, 640)

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
        self._stream_mode = False        # the stream view in place of the setup options and the tabs

        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        self._config_dir = os.path.join(config_home, "cyberfinger-bridge")
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = VRMode()
        self.gamepad_mode = None         # created lazily on start
        self.vrchat_gamepad_mode = None  # created lazily on start
        self.active_mode = None
        self.slimevr = None              # created lazily while forwarding is on
        self.driver_link = None          # listens for the driver's haptic requests and status while in VR mode
        # VR mode: the right CyberFinger's pink button is left to the driver, mutes and unmutes the default
        # microphone, or fires a FluxAction (_pink_flux: the FluxAction's number, 0 for the microphone, -1 for
        # the driver; set on the Tk thread from the options, read on the BLE one)
        self._pink_flux = -1
        self.pink_button = pink_button.PinkButton(self.log, haptics=lambda: getattr(self.ble, "haptics", None),
                                                  action=lambda: self._pink_flux)
        # VR mode: a triple tap on a CyberFinger's joint IMU resyncs the driver's IMU fusion, like the Resync IMU button.
        # settings.json "tap_threshold_g" tunes how hard a tap must be.
        self.triple_tap = [tap_gesture.TripleTap(threshold_g=float(self._config.get("tap_threshold_g", 1.0)))
                           for _ in range(2)]
        self._haptic_logged = [False, False]
        # The Skeleton tab: the hands the headset tracks, as the driver reports them back while in VR mode
        # (driver_skeleton.py). "skeleton_enabled": false in settings.json turns it off.
        self.skeleton = (DriverSkeletonSource(lambda: self.driver_link)
                         if self._config.get("skeleton_enabled", True) else None)

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
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self._stop_slimevr()
        self._stop_driver_link()
        if self.skeleton:
            self.skeleton.stop()

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
        style.configure("Small.TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("monospace", 9), focuscolor=COLOR_BG)
        style.map("Small.TRadiobutton",
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
        style.configure("Console.TButton", background=COLOR_BG3, foreground=COLOR_FG,
                        font=("monospace", 9), padding=(10, 2))
        style.map("Console.TButton",
                  background=[("active", COLOR_BG2)],
                  foreground=[("active", COLOR_ACCENT)])

        # Header
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(12, 4))
        self.title_label = ttk.Label(header, text="⬡ CyberFinger Bridge (Linux)", style="Title.TLabel")
        self.title_label.pack(side=tk.LEFT)
        ttk.Label(header, text="ver: " + bridge_version.get(), style="Status.TLabel").pack(side=tk.RIGHT, padx=(16, 0))
        self.status_label = ttk.Label(header, text="Idle", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)
        # Stream mode's way out, in the middle of this line (placed by _place_stream_stop while in stream mode).
        self.stream_stop_btn = ttk.Button(header, text=STREAM_STOP_TEXTS[0], style="Console.TButton", width=0,
                                          command=self._stop_stream)     # width 0: no wider than its text
        self._stream_stop_font = tkfont.Font(root=self.root, font=("monospace", 9))
        self._stream_stop_chrome = (self.stream_stop_btn.winfo_reqwidth()
                                    - self._stream_stop_font.measure(STREAM_STOP_TEXTS[0]))
        Tooltip(self.stream_stop_btn, TIPS["stream_stop"])
        header.bind("<Configure>", self._place_stream_stop)
        self.status_label.bind("<Configure>", self._place_stream_stop)

        # Everything below the header but the stream view: the setup options and the tabs. Stream mode hides it.
        self.setup_frame = ttk.Frame(self.root)
        self.setup_frame.pack(fill=tk.BOTH, expand=True)

        # Mode selection + Start/Stop
        ctrl_frame = ttk.Frame(self.setup_frame)
        ctrl_frame.pack(fill=tk.X, padx=16, pady=(4, 4))

        self.mode_var = tk.StringVar(value=self._config.get("mode", "vr"))
        radio_frame = ttk.Frame(ctrl_frame)
        radio_frame.pack(side=tk.LEFT)
        for text, value in (("VR Mode (BLE→WiVRn driver)", "vr"),
                            ("Gamepad Mode (BLE→uinput Xbox 360, Resonite)", "gamepad"),
                            ("Gamepad Mode (BLE→uinput Xbox 360, VRChat)", "gamepad_vrc")):
            radio = ttk.Radiobutton(radio_frame, text=text, variable=self.mode_var, value=value)
            radio.pack(anchor=tk.W)
            Tooltip(radio, TIPS[value])

        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", style="Stop.TButton",
                                   command=self._stop_bridge, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.start_btn = ttk.Button(ctrl_frame, text="Start", style="Accent.TButton",
                                    command=self._start_bridge)
        self.start_btn.pack(side=tk.RIGHT)
        Tooltip(self.start_btn, TIPS["start"])
        Tooltip(self.stop_btn, TIPS["stop"])

        # Options row
        opts_frame = ttk.Frame(self.setup_frame)
        opts_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.autostart_var = tk.BooleanVar(value=self._config.get("autostart", False))
        autostart = ttk.Checkbutton(opts_frame, text="Auto-start on launch", variable=self.autostart_var,
                                    command=self._on_autostart_changed)
        autostart.pack(side=tk.LEFT)
        Tooltip(autostart, TIPS["autostart"])

        if HAS_TRAY:
            ttk.Label(opts_frame, text="(close button minimizes to tray)",
                     style="Status.TLabel").pack(side=tk.RIGHT)

        # Right pink button row (VR mode): left to the driver, the default microphone's mute, or a FluxAction
        # of the MoreFluxActions mod in Resonite
        style.configure("TSpinbox", fieldbackground=COLOR_BG2, background=COLOR_BG3, foreground=COLOR_FG,
                        arrowcolor=COLOR_FG, insertcolor=COLOR_FG, bordercolor=COLOR_BG3,
                        lightcolor=COLOR_BG2, darkcolor=COLOR_BG2)
        style.map("TSpinbox", fieldbackground=[("disabled", COLOR_BG)], foreground=[("disabled", COLOR_FG_DIM)],
                  arrowcolor=[("disabled", COLOR_FG_DIM)])
        pink_frame = ttk.Frame(self.setup_frame)
        pink_frame.pack(fill=tk.X, padx=16, pady=(0, 8))
        pink_label = ttk.Label(pink_frame, text="Right pink button:", style="Status.TLabel")
        pink_label.pack(side=tk.LEFT)
        Tooltip(pink_label, TIPS["pink"])
        saved = self._config.get("right_pink", "driver")
        self.pink_mode_var = tk.StringVar(value=saved if saved in ("mic", "flux") else "driver")
        for label, value in (("driver", "driver"), ("mic mute", "mic"), ("FluxAction", "flux")):
            radio = ttk.Radiobutton(pink_frame, text=label, style="Small.TRadiobutton",
                                    variable=self.pink_mode_var, value=value, command=self._on_pink_changed)
            radio.pack(side=tk.LEFT)
            Tooltip(radio, TIPS["pink"])
        self.pink_flux_var = tk.StringVar(value=str(self._saved_pink_flux()))
        self.pink_flux_box = ttk.Spinbox(pink_frame, from_=1, to=flux_actions.COUNT, width=3,
                                         textvariable=self.pink_flux_var, font=("monospace", 9))
        self.pink_flux_box.pack(side=tk.LEFT)
        Tooltip(self.pink_flux_box, TIPS["pink_flux"])
        self.pink_flux_var.trace_add("write", lambda *_: self._on_pink_changed())
        # Left mid-edit (empty, 0, 50...): back to the number in use.
        self.pink_flux_box.bind("<FocusOut>", lambda _e: self.pink_flux_var.set(str(self._saved_pink_flux())))
        self.pink_flux_box.bind("<Return>", lambda _e: self.root.focus_set())

        # SlimeVR row
        slime_frame = ttk.Frame(self.setup_frame)
        slime_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.slimevr_var = tk.BooleanVar(value=self._config.get("slimevr_enabled", False))
        slime_check = ttk.Checkbutton(slime_frame, text="Forward IMU to SlimeVR", variable=self.slimevr_var,
                                      command=self._on_slimevr_changed)
        slime_check.pack(side=tk.LEFT)
        Tooltip(slime_check, TIPS["slimevr"])

        # Body 1 and Body 2 are redundant IMUs at the same location, so only one
        # is forwarded — this picks which, falling back to the other if absent.
        body_label = ttk.Label(slime_frame, text="  body IMU:", style="Status.TLabel")
        body_label.pack(side=tk.LEFT)
        Tooltip(body_label, TIPS["body_imu"])
        self.slimevr_body_var = tk.StringVar(
            value=self._config.get("slimevr_body_imu", "body1"))
        for label, value in (("1", "body1"), ("2", "body2")):
            radio = ttk.Radiobutton(slime_frame, text=label, style="Small.TRadiobutton",
                                    variable=self.slimevr_body_var, value=value, command=self._on_slimevr_changed)
            radio.pack(side=tk.LEFT)
            Tooltip(radio, TIPS["body_imu"])

        # The driver's IMU fusion starts its heading over (a CyberFinger switched off and on, or put back differently).
        # A triple tap on a CyberFinger's joint IMU does the same.
        resync = ttk.Button(slime_frame, text="Resync IMU", style="Console.TButton",
                            command=lambda: self._resync_imu("button"))
        resync.pack(side=tk.RIGHT)
        Tooltip(resync, TIPS["resync"])

        # Tabs: the CyberFingers (buttons, stick, IMU, haptics), the skeletons the driver sees, the log
        style.configure("TNotebook", background=COLOR_BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=COLOR_BG3, foreground=COLOR_FG_DIM,
                        font=("monospace", 10), padding=(14, 4), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_BG2), ("active", COLOR_BG2)],
                  foreground=[("selected", COLOR_ACCENT), ("active", COLOR_FG)])
        self.tabs = ttk.Notebook(self.setup_frame)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        hands_tab = ttk.Frame(self.tabs)
        self.tabs.add(hands_tab, text="Buttons + IMU")
        hands_frame = ttk.Frame(hands_tab)
        hands_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.left_panel = HandPanel(hands_frame, "LEFT", side=tk.LEFT, on_test_haptic=lambda: self._test_haptic(0))
        self.right_panel = HandPanel(hands_frame, "RIGHT", side=tk.RIGHT, on_test_haptic=lambda: self._test_haptic(1))

        skeleton_tab = ttk.Frame(self.tabs)
        self.tabs.add(skeleton_tab, text="Skeleton")
        self.skeleton_area = ttk.Frame(skeleton_tab)
        self.skeleton_area.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.left_skeleton = SkeletonPanel(self.skeleton_area, "LEFT", side=tk.LEFT)
        self.right_skeleton = SkeletonPanel(self.skeleton_area, "RIGHT", side=tk.RIGHT)

        console_tab = ttk.Frame(self.tabs)
        self.tabs.add(console_tab, text="Console")
        self.log_text = scrolledtext.ScrolledText(
            console_tab, height=8,
            bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
            font=("monospace", 9), relief=tk.FLAT, borderwidth=0,
            selectbackground=COLOR_ACCENT, selectforeground="white",
            state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # Reopens on the tab last used.
        self._tab_names = ("hands", "skeleton", "console")
        last = self._config.get("tab", "hands")
        if last in self._tab_names:
            self.tabs.select(self._tab_names.index(last))
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Stream mode: under Resync IMU, in the free end of the tab row.
        self.stream_btn = ttk.Button(self.setup_frame, text="◉ Stream mode", style="Console.TButton",
                                     command=self._start_stream)
        self.stream_btn.place(in_=self.tabs, relx=1.0, y=0, anchor=tk.NE)
        Tooltip(self.stream_btn, TIPS["stream"])
        self.stream_view = StreamView(self.root, "monospace", BLACK_HOLD[0])

        self._on_pink_changed()

    def _saved_pink_flux(self):
        n = self._config.get("right_pink_flux", flux_actions.COUNT)
        return n if isinstance(n, int) and 1 <= n <= flux_actions.COUNT else flux_actions.COUNT

    def _on_pink_changed(self):
        """The right pink button's job: saved, and in effect at the next press."""
        try:
            n = int(self.pink_flux_var.get())
        except ValueError:
            n = 0
        if 1 <= n <= flux_actions.COUNT:
            self._config["right_pink_flux"] = n
        mode = self.pink_mode_var.get()
        mode = mode if mode in ("mic", "flux", "driver") else "driver"
        flux = mode == "flux"
        self._config["right_pink"] = mode
        self._save_config()
        self.pink_flux_box.state(["!disabled"] if flux else ["disabled"])
        n = self._saved_pink_flux()
        self._pink_flux = n if flux else (-1 if mode == "driver" else 0)
        self.vr_mode.right_pink_to_driver = mode == "driver"
        self.right_panel.pink_label = f"PINK→FA{n}" if flux else ("PINK→mic" if mode == "mic" else None)

    def _start_stream(self):
        """Stream mode: the setup options and the tabs make way for one view of both CyberFingers that fills the
        window (its size stays). The header line stays, with Stop stream in its middle."""
        if self._stream_mode:
            return
        self._stream_mode = True
        self.setup_frame.pack_forget()
        self.stream_view.frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        self._place_stream_stop()

    def _stop_stream(self):
        if not self._stream_mode:
            return
        self._stream_mode = False
        self.stream_stop_btn.place_forget()
        self.stream_view.frame.pack_forget()
        self.setup_frame.pack(fill=tk.BOTH, expand=True)

    def _place_stream_stop(self, _event=None):
        """Stream mode's Stop button: in the middle of the header line, or as near to it as the title and the status
        leave room; with too little room for its text (a narrow window, a long status), the short one."""
        if not self._stream_mode:
            return
        left = self.title_label.winfo_x() + self.title_label.winfo_width() + 8
        right = self.status_label.winfo_x() - 8
        widths = [self._stream_stop_font.measure(t) + self._stream_stop_chrome for t in STREAM_STOP_TEXTS]
        fits = [i for i, width in enumerate(widths) if width <= right - left]
        i = fits[0] if fits else len(widths) - 1
        if self.stream_stop_btn.cget("text") != STREAM_STOP_TEXTS[i]:
            self.stream_stop_btn.configure(text=STREAM_STOP_TEXTS[i])
        lo, hi = left + widths[i] / 2, right - widths[i] / 2
        x = max(lo, min(hi, self.stream_stop_btn.master.winfo_width() / 2)) if lo <= hi else (lo + hi) / 2
        self.stream_stop_btn.place(x=x, rely=0.5, anchor=tk.CENTER)

    def _draw_stream(self):
        sk = self.skeleton
        skeletons = None
        if sk is not None:
            sk.update()
            if sk.connected:
                poses, modes = sk.pose_info, sk.modes
                skeletons = ((sk.hands[0], sk.status, poses[0], poses[1], modes[0]),
                             (sk.hands[1], sk.status, poses[1], poses[0], modes[1]))
        self.stream_view.draw((self.ble.left, self.ble.right), (self._haptic_state(0), self._haptic_state(1)),
                              (self.left_panel.pink_text, self.right_panel.pink_text), skeletons)

    def _current_tab(self):
        try:
            return self._tab_names[self.tabs.index(self.tabs.select())]
        except (tk.TclError, IndexError):
            return "hands"

    def _on_tab_changed(self, _event=None):
        tab = self._current_tab()
        self._config["tab"] = tab
        self._save_config()
        if tab == "console":
            self.log_text.see(tk.END)

    def _on_autostart_changed(self):
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

    def _on_slimevr_changed(self):
        """Persist the SlimeVR options, applying them live if already running."""
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        if self.slimevr:
            self.slimevr.set_body_slot(self.slimevr_body_var.get())

        # Only churn the forwarder while the bridge is actually running;
        # otherwise _start_bridge will pick the new setting up.
        if not self.active_mode:
            return
        if self.slimevr_var.get():
            self._start_slimevr()
        else:
            self._stop_slimevr()

    def _start_slimevr(self):
        if self.slimevr:
            return
        host = self._config.get("slimevr_host", SLIME_DEFAULT_HOST)
        port = int(self._config.get("slimevr_port", SLIME_DEFAULT_PORT))
        self.slimevr = SlimeVRForwarder(host, port,
                                        self.slimevr_body_var.get(), self.log)
        self.slimevr.start()
        self.log(f"SlimeVR: announcing trackers to {host}:{port}")

    def _stop_slimevr(self):
        if not self.slimevr:
            return
        self.slimevr.stop()
        self.slimevr = None
        self.log("SlimeVR: forwarding stopped")

    def _start_failed(self, why, *hints):
        """Start could not start: said where it is seen (the status line, and the Console tab brought forward), not
        only in a log nobody is looking at."""
        self.log(f"ERROR: {why}!")
        for hint in hints:
            self.log(hint)
        self.set_status(f"Start failed: {why}")
        self.tabs.select(self._tab_names.index("console"))

    def _start_bridge(self):
        if self.active_mode:
            return

        if not HAS_BLEAK:
            self._start_failed("bleak not installed", "Run the bridge from its Python environment (README: "
                               "uv venv, uv pip install -r requirements.txt), or: pip install bleak")
            return

        mode = self.mode_var.get()
        if mode in ("gamepad", "gamepad_vrc"):
            if not HAS_EVDEV:
                self._start_failed("python-evdev not installed", "Run: pip install evdev")
                return
            gp = GamepadMode() if mode == "gamepad" else GamepadModeVRChat()
            if not gp.available:
                self._start_failed("cannot create the uinput device", "Run: sudo modprobe uinput",
                                   "See file header for uinput permissions setup")
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
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        self.log(f"Starting {mode.upper()} mode...")
        if mode == "gamepad_vrc":
            self.log(">>> VRChat: enable OSC via Action Menu → OSC → Enabled")

        if self.slimevr_var.get():
            self._start_slimevr()
        if self.active_mode is self.vr_mode:
            self._start_driver_link()

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
        if self.active_mode is self.vr_mode:
            self.vr_mode = VRMode()   # stop() closed its socket
            self.vr_mode.right_pink_to_driver = self._pink_flux < 0
        self.active_mode = None
        self.gamepad_mode = None
        self.vrchat_gamepad_mode = None
        self._stop_slimevr()
        self._stop_driver_link()

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        self.left_panel.set_disconnected()
        self.right_panel.set_disconnected()
        self.set_status("Stopped")
        self.log("Bridge stopped")
        self._set_tray_running(False)

        self.ble = BLEManager(self)

    def _start_driver_link(self):
        if self.driver_link is None:
            self.driver_link = DriverLink(on_haptic=self._on_haptic, log=self.log, name="WiVRn driver")
        self.driver_link.start()

    def _stop_driver_link(self):
        if self.driver_link is not None:
            self.driver_link.stop()
            self.driver_link = None

    def _on_haptic(self, h):
        """Driver-link thread: an app asked a hand to vibrate. The hand panel shows it, and the CyberFinger gets it over
        GATT (cyberfinger_control.py; firmware 1.3.3+ with the motor)."""
        hand = h["hand"]
        if not self._haptic_logged[hand]:
            self._haptic_logged[hand] = True
            st = {"count": 1, "duration": h["duration_s"], "frequency": h["frequency_hz"], "amplitude": h["amplitude"]}
            self.log(f"Haptics: first request for the {'right' if hand else 'left'} hand ({describe_haptic(st)})")
        self.ble.haptics.request(hand, h["duration_s"], h["frequency_hz"], h["amplitude"])

    def _resync_imu(self, why, confirm_hand=None):
        """Any thread: ask the driver to resync both hands' IMU fusion (the heading is fitted again from the next
        views). A triple tap is confirmed on that CyberFinger: two short pulses."""
        if self.active_mode is not self.vr_mode:
            self.log("Resync IMU: only in VR mode (it's the WiVRn driver's fusion)")
            return
        # Only some hardware revisions have the joint IMU (the fusion's sensor); without it the headset alone turns
        # the hand and there's nothing to resync.
        hands = [h for h, st in enumerate((self.ble.left, self.ble.right)) if st.imu_present & IMU_JOINT]
        if not hands:
            self.log("Resync IMU: no CyberFinger with a joint IMU is connected (only some revisions have one; without it "
                     "the headset alone turns the hands)")
            return
        self.vr_mode.resync_imu(hands)
        names = " and ".join("right" if h else "left" for h in hands)
        self.log(f"Resync IMU fusion, {names} ({why})")
        haptics = getattr(self.ble, "haptics", None)
        if confirm_hand is not None and haptics is not None:
            haptics.request(confirm_hand, 0.25, 8.0, 1.0)

    def _test_haptic(self, hand):
        """UI thread: the hand panel's haptics strip was clicked (VR mode): a short pulse to that CyberFinger."""
        name = "right" if hand else "left"
        if not self.ble.haptics.available(hand):
            self.log(f"Haptics: the {name} CyberFinger isn't connected (or has no control characteristic)")
            return
        duration, frequency, amplitude = 0.25, 0.0, 0.8
        self.ble.haptics.request(hand, duration, frequency, amplitude)
        if self.driver_link is not None:
            self.driver_link.haptics.add(hand, duration, frequency, amplitude)   # shown on the meter too
        self.log(f"Haptics: test pulse to the {name} CyberFinger")

    def _haptic_state(self, hand):
        return self.driver_link.haptics.state(hand) if self.driver_link is not None else None

    def on_input(self, hand, state):
        """Called from BLE thread on each input report."""
        if self.active_mode:
            if isinstance(self.active_mode, (GamepadMode, GamepadModeVRChat)):
                self.active_mode.update_gamepad(self.ble.left, self.ble.right)
            else:
                self.active_mode.on_input(hand, state)
            if self.active_mode is self.vr_mode:
                self.pink_button.on_input(hand, state.buttons2)
                if state.has_accel and state.imu_present & IMU_JOINT:
                    tap = self.triple_tap[hand]
                    if tap.feed(state.rx_perf or time.perf_counter(), state.accel_joint):
                        self._resync_imu(f"{'right' if hand else 'left'} CyberFinger triple tap "
                                         f"({', '.join(f'{g:.1f}' for g in tap.sequence_g)} g)", hand)

        # Runs alongside the active mode, not instead of it — SlimeVR takes the
        # orientation none of the other modes forward.
        if self.slimevr:
            self.slimevr.on_input(hand, state)

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

        # Only what's showing is redrawn: the stream view, or the visible tab.
        tab = "stream" if self._stream_mode else self._current_tab()
        if tab == "stream":
            self._draw_stream()
        if self.ble and tab == "hands":
            self.left_panel.update_state(self.ble.left, self._haptic_state(0))
            self.right_panel.update_state(self.ble.right, self._haptic_state(1))

        if tab == "skeleton":
            if self.skeleton:
                self.skeleton.update()
                poses, modes = self.skeleton.pose_info, self.skeleton.modes
                self.left_skeleton.draw(self.skeleton.hands[0],
                                        self.skeleton.status,
                                        poses[0], poses[1], mode=modes[0])
                self.right_skeleton.draw(self.skeleton.hands[1],
                                         self.skeleton.status,
                                         poses[1], poses[0], mode=modes[1])
            else:
                self.left_skeleton.draw(None, "disabled in settings")
                self.right_skeleton.draw(None, "disabled in settings")

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
        if not (shutil.which("wpctl") or shutil.which("pactl")):
            self.log("Mic mute (right pink button): not available (needs wpctl or pactl)")
        self.root.mainloop()


# ── Hand visualization panel ─────────────────────────────────────────────

class Tooltip:
    """A hover tip for a widget: why you'd use it, and what it implies. Shown after a short pause, beside the
    pointer's widget and kept on screen; gone on leaving or clicking."""

    DELAY_MS = 450
    WRAP_PX = 380

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        self._after = None
        if self._tip is not None or not self.widget.winfo_viewable():
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self.text, justify=tk.LEFT, wraplength=self.WRAP_PX, bg=COLOR_BG3, fg=COLOR_FG,
                 font=("monospace", 9), padx=9, pady=7, relief=tk.SOLID, borderwidth=1).pack()
        tip.update_idletasks()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        x = max(0, min(x, self.widget.winfo_screenwidth() - tip.winfo_width() - 8))
        if y + tip.winfo_height() > self.widget.winfo_screenheight() - 8:       # no room below: above
            y = self.widget.winfo_rooty() - tip.winfo_height() - 4
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# Stream mode's Stop button: the text it has, and the one for a header line without room for it.
STREAM_STOP_TEXTS = ("■ Stop stream", "■ Stop")

# What each control is for (Tooltip texts).
TIPS = {
    "vr": "CyberFingers → the CyberFinger driver in WiVRn, for the VR apps run through it (Resonite, VRChat).\n\n"
          "The CyberFingers become two hand controllers: their buttons, stick and trigger, with the hand pose and fingers "
          "from the headset's hand tracking, the CyberFinger's IMUs (where fitted), app vibration (where fitted), and "
          "the pink buttons. Needs WiVRn with the CyberFinger driver, running.",
    "gamepad": "CyberFingers → a virtual Xbox 360 controller (uinput) laid out for Resonite, without the VR driver: "
               "desktop mode, or alongside other controllers.\n\nOnly buttons and sticks: no hand tracking, no "
               "vibration from apps, no pink-button functions. Needs python-evdev and write access to /dev/uinput.",
    "gamepad_vrc": "CyberFingers → a virtual Xbox 360 controller (uinput) laid out for VRChat, with Use, Grab and "
                   "Voice over OSC, without the VR driver: desktop mode, or alongside other controllers.\n\n"
                   "Only buttons and sticks: no hand tracking, no vibration from apps, no pink-button functions. "
                   "Needs python-evdev, write access to /dev/uinput, and OSC enabled in VRChat.",
    "start": "Connect to both CyberFingers over Bluetooth and start forwarding them in the chosen mode. The CyberFingers must be "
             "paired (bluetoothctl) and switched on.",
    "stop": "Stop forwarding. The CyberFingers stay connected to the PC as Bluetooth devices. In VR mode the WiVRn "
            "driver then gets no input from them.",
    "autostart": "Start in the last used mode as soon as the bridge opens: handy when it starts with the desktop "
                 "session. The CyberFingers must be on by then (otherwise: Stop, then Start).",
    "pink": "What the right CyberFinger's pink button does in VR mode.\n\n"
            "driver (the default): leaves the button to the WiVRn driver: in Resonite FluxAction42, which mutes "
            "with the MoreFluxActions mod (its MuteToggleAction, 42 by default; the driver's flux_actions.pink_right "
            "setting). The bridge does nothing with it.\n\n"
            "mic mute: mutes and unmutes the default microphone (PipeWire's or PulseAudio's default source), so "
            "every app goes quiet at once (Resonite, VRChat, Discord). The CyberFinger confirms: pulses when muted, "
            "one buzz when live (CyberFingers with a motor).\n\n"
            "FluxAction: presses a FluxAction in Resonite instead, for your own ProtoFlux (needs the MoreFluxActions "
            "mod). The microphone is left alone, and nothing vibrates.\n\n"
            "Remembered across restarts.\n\n"
            "(The left pink button always goes to the driver: the left controller's menu button, by its default.)",
    "pink_flux": "Which FluxAction (1-42) the right pink button presses, with FluxAction chosen. 42 by default.\n\n"
                 "In Resonite, a Dynamic Impulse Receiver tagged "
                 "FluxAction42.Pressed (or .Released) under your avatar reacts to it. Sent straight to the mod, "
                 "not through the driver.",
    "slimevr": "Also send the CyberFingers' IMUs to a SlimeVR server on this PC, as trackers: the wrist IMU as the lower "
               "arm, the joint IMU (where fitted) as the hand.\n\nFor full-body setups with SlimeVR: it gives "
               "SlimeVR the forearms. Runs alongside any mode. Needs the SlimeVR server running.",
    "body_imu": "Which wrist IMU SlimeVR gets. Every CyberFinger has body IMU 1; some revisions add a second chip at the "
                "same spot (2). Falls back to the other when the chosen one is missing.",
    "resync": "Start the WiVRn driver's IMU fusion over, for both hands: it fits the IMU's heading again from the "
              "next views of the hand.\n\nUse it when a hand's rotation is off: after taking a CyberFinger "
              "off or re-seating it, or switching it off and on. A triple tap on the module on the back of the "
              "hand does the same.\n\n"
              "VR mode only, and only for CyberFingers with the joint IMU (some revisions): without it the headset "
              "alone turns the hand, and there's nothing to resync.",
    "stream": "For streaming or recording: show the CyberFingers at work. The options and tabs make way for one view "
              "of both CyberFingers that fills this window: the hand skeletons the driver sees, stick, trigger and "
              "buttons, the motion sensors, and vibration. Capture the window in OBS, beside the game.\n\n"
              "The bridge keeps running as it is; Stop stream, in the top line, brings the options back.",
    "stream_stop": "Leave stream mode: back to the options and the tabs.",
}


class HandPanel:
    """Canvas-based hand state visualization."""

    def __init__(self, parent, label, side, on_test_haptic=None, black_hold=None):
        self.label = label
        # The driver's long press of the black button: seconds (0 = none), the left hand's FluxAction (0 = none)
        self.black_hold_s, self.black_hold_flux = black_hold or BLACK_HOLD
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True, padx=(0, 4) if side == tk.LEFT else (4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, width=1, height=380)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._last_state = None
        self._haptic_shown = False
        self._on_test_haptic = on_test_haptic
        self._pink_until = 0.0       # the pink click, shown lit until then
        self._black_since = None     # the black button held since then (its long press)
        self.pink_label = None       # the pink button's row, when the bridge acts on it (PINK→mic, PINK→FA42)
        self.canvas.bind("<Button-1>", self._on_click)

    @property
    def pink_text(self):
        return self.pink_label or "PINK→VR"     # left to the driver unless set otherwise

    def _on_click(self, event):
        """A click on the haptics strip (VR mode) sends a test pulse to this CyberFinger."""
        if self._haptic_shown and self._on_test_haptic and event.y > self.canvas.winfo_height() - 36:
            self._on_test_haptic()

    def _draw_haptics(self, c, w, h, haptic):
        """Bottom strip: what the WiVRn driver asks this hand's CyberFinger to vibrate (None: not in VR mode). Click it
        for a test pulse."""
        self._haptic_shown = haptic is not None
        if haptic is None:
            return
        c.create_line(20, h - 34, w - 20, h - 34, fill=COLOR_BG3, width=1)
        draw_haptic_meter(c, 10, h - 28, w - 20, 22, haptic, time.perf_counter(), label="HAPTIC",
                          accent=COLOR_ACCENT, bg=COLOR_BG, dim=COLOR_FG_DIM, fg=COLOR_FG, line=COLOR_BG3,
                          font=("monospace", 8))
        c.create_text(w - 12, h - 40, text="click: test", fill=COLOR_FG_DIM, font=("monospace", 7), anchor=tk.E)

    def update_state(self, state: HandState, haptic=None):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        is_left = self.label == "LEFT"

        # Title
        if state.connected:
            c.create_text(w // 2, 14, text=f"{self.label}", fill=COLOR_ACCENT,
                         font=("monospace", 11, "bold"))
        else:
            c.create_text(w // 2, 14, text=f"{self.label} (disconnected)",
                         fill=COLOR_FG_DIM, font=("monospace", 10))
            self._draw_haptics(c, w, h, haptic)
            return

        # Battery
        bat = state.battery
        bat_color = COLOR_GREEN if bat > 50 else COLOR_ORANGE if bat > 20 else COLOR_RED
        c.create_text(w - 10, 14, text=f"{bat}%", fill=bat_color,
                     font=("monospace", 9), anchor=tk.E)

        # Packet counter
        c.create_text(10, 14, text=f"#{state.packet_count}", fill=COLOR_FG_DIM,
                     font=("monospace", 8), anchor=tk.W)

        # ── Joystick visualization ──
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

        # ── Button indicators ──
        now = time.perf_counter()
        btn_x = 3 * w // 4 if is_left else w // 4
        btn_y_start = 30
        btn_spacing = 15
        # The pink button's press arrives as an ~80 ms click: shown lit a little longer, so it can be seen.
        if state.buttons2 & cyberfinger_report.PINK:
            self._pink_until = now + 0.3
        btn_names_bits = [
            ("TRIG", state.buttons & BTN_TRIGGER),
            ("GRIP", state.buttons & BTN_GRIP),
            ("C",    state.buttons & BTN_C),
            ("D",    state.buttons & BTN_D),
            ("E",    state.buttons & BTN_E),
            ("MENU", state.buttons & BTN_MENU),
            ("JCLK", state.buttons & BTN_JCLICK),
            ("ST/SE", state.buttons & BTN_STSEL),
            (self.pink_text, now < self._pink_until),
        ]

        for i, (name, pressed) in enumerate(btn_names_bits):
            by = btn_y_start + i * btn_spacing
            pressed = bool(pressed)
            fill = COLOR_ACCENT if pressed else COLOR_BG
            outline = COLOR_ACCENT if pressed else COLOR_BG3
            c.create_oval(btn_x - 6, by - 6, btn_x + 6, by + 6,
                         fill=fill, outline=outline, width=2)
            c.create_text(btn_x + 13, by, text=name, fill=COLOR_FG if pressed else COLOR_FG_DIM,
                         font=("monospace", 8), anchor=tk.W)

        # Beside ST/SE: the black button's long press, as the driver sees it (in Resonite FluxAction1 left,
        # FluxAction2 right, by its defaults). The pill fills while the button is held and lights at the
        # threshold. On the side with room for it: the stick's.
        black = bool(state.buttons & BTN_STSEL)
        if black and self._black_since is None:
            self._black_since = now
        elif not black:
            self._black_since = None
        hold_s = self.black_hold_s
        if hold_s > 0:
            held = now - self._black_since if self._black_since is not None else 0.0
            by = btn_y_start + 7 * btn_spacing
            px0, px1 = (btn_x - 74, btn_x - 14) if is_left else (btn_x + 52, btn_x + 112)
            on = held >= hold_s
            c.create_rectangle(px0, by - 6, px1, by + 6, fill=COLOR_BG, outline=COLOR_ACCENT if on else COLOR_BG3)
            if on:
                c.create_rectangle(px0, by - 6, px1, by + 6, fill=COLOR_ACCENT, outline=COLOR_ACCENT)
            elif held > 0:
                c.create_rectangle(px0, by - 6, px0 + int((px1 - px0) * held / hold_s), by + 6,
                                   fill=COLOR_BG3, outline="")
            flux = self.black_hold_flux and self.black_hold_flux + (0 if is_left else 1)
            c.create_text((px0 + px1) // 2, by, text=f"HOLD·FA{flux}" if flux else "HOLD",
                         fill="white" if on else COLOR_FG_DIM, font=("monospace", 7))

        # ── Trigger bar ──
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

        # ── IMU orientation ──
        self._draw_imu(c, w, h, state)
        self._draw_haptics(c, w, h, haptic)

    def _draw_imu(self, c, w, h, state):
        """Draw a 3D triad per populated IMU slot, or a placeholder if there are none."""
        c.create_line(20, 192, w - 20, 192, fill=COLOR_BG3, width=1)

        imus = state.active_imus()
        if not imus:
            c.create_text(w // 2, 258, text="no IMU installed",
                         fill=COLOR_FG_DIM, font=("monospace", 9))
            return

        # Share the panel width between however many slots are live, shrinking
        # the triads rather than letting them collide.
        col_w = w / len(imus)
        scale = max(18.0, min(46.0, col_w * 0.30))
        cy = 262

        for i, (label, quat) in enumerate(imus):
            cx = col_w * (i + 0.5)
            c.create_text(cx, 206, text=label, fill=COLOR_FG_DIM,
                         font=("monospace", 8, "bold"))
            draw_triad(c, cx, cy, scale, quat, "monospace")

            roll, pitch, yaw = quat_to_euler_deg(quat)
            if len(imus) == 1:
                readout = f"R{roll:+6.1f}  P{pitch:+6.1f}  Y{yaw:+6.1f}"
            else:
                readout = f"{roll:+.0f} {pitch:+.0f} {yaw:+.0f}"
            c.create_text(cx, 322, text=readout,
                         fill=COLOR_FG_DIM, font=("monospace", 8))

    def set_disconnected(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w > 10:
            c.create_text(w // 2, h // 2, text=f"{self.label}\n(disconnected)",
                         fill=COLOR_FG_DIM, font=("monospace", 10), justify=tk.CENTER)


class SkeletonPanel(SkeletonRenderer):
    """One hand's skeleton on a canvas of its own (the Skeleton tab)."""

    def __init__(self, parent, label, side):
        super().__init__(label, "monospace")
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True,
                        padx=(0, 4) if side == tk.LEFT else (4, 0))
        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0,
                                height=150)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def draw(self, joints, status, pose=None, other_pose=None, mode=None):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return
        self.draw_in(c, 0, 0, w, h, joints, status, pose, other_pose, mode)


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
