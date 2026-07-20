# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
CyberFinger Bridge GUI

Combines VR (BLE→UDP) and Gamepad (BLE→ViGEm Xbox 360) bridge modes
into a single application with visual feedback, system tray icon,
and optional auto-start.
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
import base64
import queue
import json
import math

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    from pynput.keyboard import Key, Controller as KeyboardController
    _keyboard = KeyboardController()
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

# pyopenvr — used to read the hand skeleton SteamVR itself is tracking (e.g.
# Steam Link camera hand tracking). Broad except: the import can also fail on
# a missing openvr_api.dll, not just an absent package.
try:
    import openvr
    HAS_OPENVR = True
except Exception:
    HAS_OPENVR = False

# ── BLE protocol ─────────────────────────────────────────────────────────

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"

GAMEPAD_MAGIC    = 0x50474643
GAMEPAD_PACK_FMT = "<IBBhhBB"
INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)

# The GATT report grew twice, and each revision is a strict prefix of the next
# (see CyberFingerFW_ESP32/src/vr_gatt.h — the first 28 bytes are frozen), so
# the widest layout the payload can support is the correct one to apply:
#
#   12 bytes — base report, no IMU at all
#   28 bytes — one appended quaternion (primary body IMU)
#   61 bytes — presence bitmask + two further quaternions
#
INPUT_REPORT_IMU_FMT = "<BBhhBBI4f"
INPUT_REPORT_IMU_SIZE = struct.calcsize(INPUT_REPORT_IMU_FMT)

INPUT_REPORT_MULTI_FMT = "<BBhhBBI4fB4f4f"
INPUT_REPORT_MULTI_SIZE = struct.calcsize(INPUT_REPORT_MULTI_FMT)

# 79 bytes — three raw body-frame accel vectors appended, one per IMU slot in
# the same order as the quaternions. Still a strict suffix, so it is unpacked
# separately from the 61-byte prefix above rather than duplicating that layout.
ACCEL_TAIL_FMT = "<9h"
ACCEL_TAIL_SIZE = struct.calcsize(ACCEL_TAIL_FMT)
INPUT_REPORT_ACCEL_SIZE = INPUT_REPORT_MULTI_SIZE + ACCEL_TAIL_SIZE

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
# keep answering heartbeats even when no glove data is flowing.
SLIME_TIMEOUT = 3.0

SLIME_FIRMWARE_VERSION = "CyberFinger"
SLIME_VENDOR_NAME      = "DrSciCortex"
SLIME_VENDOR_URL       = "https://github.com/DrSciCortex"
SLIME_PRODUCT_NAME     = "CyberFinger"

BTN_TRIGGER = 0x01  # bit0 — AX  (trigger)
BTN_GRIP    = 0x02  # bit1 — BY  (grip)
BTN_C       = 0x04  # bit2 — CZ
BTN_D       = 0x08  # bit3 — DD
BTN_E       = 0x10  # bit4 — EE
BTN_MENU    = 0x20  # bit5 — BP  (bumper/menu)
BTN_JCLICK  = 0x40  # bit6 — ST  (stick click)
BTN_STSEL   = 0x80  # bit7 — STARTSELECT

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
COLOR_ACCENT   = "#e6007e"  # CyberFinger pink
COLOR_ACCENT2  = "#ff2d9b"
COLOR_GREEN    = "#00e676"
COLOR_RED      = "#ff1744"
COLOR_ORANGE   = "#ff9100"
COLOR_BLUE     = "#448aff"


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


def _blend(c1, c2, t):
    """Blend two #rrggbb colors; t=0 → c1, t=1 → c2."""
    t = max(0.0, min(1.0, t))
    a, b = int(c1[1:], 16), int(c2[1:], 16)
    parts = []
    for shift in (16, 8, 0):
        va = (a >> shift) & 255
        vb = (b >> shift) & 255
        parts.append(int(va + (vb - va) * t))
    return "#%02x%02x%02x" % tuple(parts)


def fmt_buttons(btn):
    parts = [name for bit, name in BUTTON_NAMES.items() if btn & bit]
    return "+".join(parts) if parts else "none"


def ibuffer_to_bytes(ibuffer):
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(ibuffer)
    length = dr.unconsumed_buffer_length
    result = bytearray()
    for _ in range(length):
        result.append(dr.read_byte())
    return bytes(result)


def resource_path(relative):
    """Get path to resource, works for dev and PyInstaller."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


# ── Tray icon image helpers ──────────────────────────────────────────────

def _load_tray_icon_running():
    """Load the color (running) tray icon."""
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32.png")))
    except Exception:
        return _generate_fallback_icon((230, 0, 126))


def _load_tray_icon_idle():
    """Load the B&W (idle/stopped) tray icon."""
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32_bw.png")))
    except Exception:
        return _generate_fallback_icon((128, 128, 128))


def _generate_fallback_icon(color):
    """Generate a simple 32x32 circle icon as fallback."""
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
        """Clear per-connection capability flags before (re)attaching a glove."""
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


# ── BLE discovery + subscription (runs in asyncio thread) ────────────────

class BLEManager:
    """Manages BLE connections in a background asyncio thread."""

    def __init__(self, app):
        self.app = app
        self.left = HandState()
        self.right = HandState()
        self._thread = None
        self._loop = None
        self._running = False
        self._subscriptions = []
        self._polling_chars = []
        self._ble_devices = []     # track opened BLE device handles
        self._gatt_services = []   # track opened GATT service handles

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
            if str(e) != "Event loop stopped before Future completed.":
                self.app.log(f"BLE thread error: {e}")
        finally:
            # Clean up: unsubscribe notifications
            for _, char, token in self._subscriptions:
                try:
                    char.remove_value_changed(token)
                except Exception:
                    pass
            self._subscriptions = []
            self._polling_chars = []
            # Close GATT service handles FIRST (they hold exclusive locks)
            for svc in self._gatt_services:
                try:
                    svc.close()
                except Exception:
                    pass
            self._gatt_services = []
            # Then close BLE device handles
            for ble_dev in self._ble_devices:
                try:
                    ble_dev.close()
                except Exception:
                    pass
            self._ble_devices = []
            # Give Windows time to release BLE handles
            import time
            time.sleep(0.5)
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self):
        self.app.log("Scanning for CyberFinger devices...")
        self.app.set_status("Scanning...")

        left_dev, right_dev = await self._find_devices()

        if not left_dev and not right_dev:
            self.app.log("No CyberFinger devices found!")
            self.app.set_status("No devices found")
            return

        self._subscriptions = []
        self._polling_chars = []

        if left_dev:
            mac, name, ble_dev = left_dev
            self._ble_devices.append(ble_dev)
            self.left.name = name
            self.left.connected = True
            self.left.reset_link()
            result = await self._setup_device("LEFT", ble_dev)
            if result:
                mode, char, token = result
                if mode == "notify":
                    self._subscriptions.append(("LEFT", char, token))
                else:
                    self._polling_chars.append(("LEFT", char))

        if right_dev:
            mac, name, ble_dev = right_dev
            self._ble_devices.append(ble_dev)
            self.right.name = name
            self.right.connected = True
            self.right.reset_link()
            result = await self._setup_device("RIGHT", ble_dev)
            if result:
                mode, char, token = result
                if mode == "notify":
                    self._subscriptions.append(("RIGHT", char, token))
                else:
                    self._polling_chars.append(("RIGHT", char))

        if not self._subscriptions and not self._polling_chars:
            self.app.log("Failed to establish data channels!")
            self.app.set_status("Connection failed")
            return

        count = len(self._subscriptions) + len(self._polling_chars)
        self.app.log(f"Connected! {count} channel(s) active")
        self.app.set_status("Connected")

        while self._running:
            for _, char in self._polling_chars:
                await self._poll_char(char)
            if self._polling_chars:
                await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.1)

    def _handle_data(self, data):
        if len(data) < INPUT_REPORT_SIZE:
            return

        # Each revision is a strict prefix of the next, so decode with the
        # widest layout this payload can satisfy and leave the rest at defaults.
        present = 0
        quats = (IDENTITY_QUAT, IDENTITY_QUAT, IDENTITY_QUAT)
        accels = (ZERO_ACCEL, ZERO_ACCEL, ZERO_ACCEL)
        has_accel = False

        if len(data) >= INPUT_REPORT_MULTI_SIZE:
            (hand, buttons, joy_x, joy_y, trigger, battery, seq,
             q1w, q1x, q1y, q1z,
             present,
             q2w, q2x, q2y, q2z,
             q3w, q3x, q3y, q3z) = struct.unpack(
                INPUT_REPORT_MULTI_FMT, data[:INPUT_REPORT_MULTI_SIZE])
            quats = ((q1w, q1x, q1y, q1z),
                     (q2w, q2x, q2y, q2z),
                     (q3w, q3x, q3y, q3z))

            if len(data) >= INPUT_REPORT_ACCEL_SIZE:
                a = struct.unpack(ACCEL_TAIL_FMT,
                                  data[INPUT_REPORT_MULTI_SIZE:INPUT_REPORT_ACCEL_SIZE])
                accels = (a[0:3], a[3:6], a[6:9])
                has_accel = True

        elif len(data) >= INPUT_REPORT_IMU_SIZE:
            hand, buttons, joy_x, joy_y, trigger, battery, seq, qw, qx, qy, qz = \
                struct.unpack(INPUT_REPORT_IMU_FMT, data[:INPUT_REPORT_IMU_SIZE])
            # This revision has no presence bitmask. An all-zero quaternion is
            # the only signal that the IMU failed to come up.
            if any(abs(v) > 1e-6 for v in (qw, qx, qy, qz)):
                present = IMU_BODY_PRIMARY
                quats = ((qw, qx, qy, qz), IDENTITY_QUAT, IDENTITY_QUAT)

        else:
            hand, buttons, joy_x, joy_y, trigger, battery, seq = \
                struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])

        h = min(hand, 1)
        state = self.left if h == 0 else self.right

        if present != state.imu_present:
            hn = "L" if h == 0 else "R"
            names = [label for bit, label in IMU_SLOT_LABELS if present & bit]
            self.app.log(f"{hn} IMU: {', '.join(names) if names else 'none detected'}")
        state.imu_present = present
        state.quat, state.quat_body2, state.quat_joint = quats
        state.has_accel = has_accel
        state.accel, state.accel_body2, state.accel_joint = accels

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

    async def _find_devices(self):
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus

        all_devices = await DeviceInformation.find_all_async()
        self.app.log(f"System devices: {len(all_devices)}")

        cf_ble_entries = []
        for dev in all_devices:
            name = dev.name or ""
            dev_id = dev.id or ""
            if "cyberfinger" in name.lower() and "bthledevice" in dev_id.lower():
                cf_ble_entries.append((name, dev_id))

        self.app.log(f"CyberFinger BLE entries: {len(cf_ble_entries)}")
        if not cf_ble_entries:
            return None, None

        seen = {}
        for enum_name, dev_id in cf_ble_entries:
            try:
                ble_dev = await BluetoothLEDevice.from_id_async(dev_id)
                if not ble_dev:
                    continue
                raw = ble_dev.bluetooth_address
                mac = ":".join(f"{(raw >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
                connected = (ble_dev.connection_status == BluetoothConnectionStatus.CONNECTED)
                if mac not in seen or (connected and not seen[mac][1]):
                    seen[mac] = (enum_name, connected, ble_dev)
            except Exception:
                pass

        if not seen:
            return None, None

        for mac, (enum_name, connected, _) in sorted(seen.items()):
            status = "CONNECTED" if connected else "disconnected"
            self.app.log(f"  {status}: \"{enum_name}\" {mac}")

        left_dev = right_dev = None
        for mac, (enum_name, connected, ble_dev) in seen.items():
            if not connected:
                continue
            nl = enum_name.lower()
            if not left_dev and "left" in nl:
                left_dev = (mac, enum_name, ble_dev)
                self.app.log(f"  LEFT  ← \"{enum_name}\"")
            elif not right_dev and "right" in nl:
                right_dev = (mac, enum_name, ble_dev)
                self.app.log(f"  RIGHT ← \"{enum_name}\"")

        return left_dev, right_dev

    async def _setup_device(self, label, ble_dev):
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
            GattClientCharacteristicConfigurationDescriptorValue,
        )

        # Retry GATT service discovery (important for reconnect after stop)
        svc_result = None
        for attempt in range(3):
            try:
                svc_result = await ble_dev.get_gatt_services_async()
                if svc_result.status == GattCommunicationStatus.SUCCESS:
                    break
            except Exception as e:
                self.app.log(f"{label}: GATT attempt {attempt+1}/3 error: {e}")
            self.app.log(f"{label}: GATT services attempt {attempt+1}/3 failed, retrying...")
            await asyncio.sleep(0.5)

        if not svc_result or svc_result.status != GattCommunicationStatus.SUCCESS:
            self.app.log(f"{label}: Failed to get GATT services after 3 attempts")
            return None

        vr_svc = None
        for svc in svc_result.services:
            if "cf00" in str(svc.uuid).lower():
                vr_svc = svc
                break
        if not vr_svc:
            self.app.log(f"{label}: 0xCF00 service not found!")
            return None

        # Track service handle for cleanup (CRITICAL for reconnect)
        self._gatt_services.append(vr_svc)

        # Retry characteristics discovery
        char_result = None
        for attempt in range(3):
            try:
                char_result = await vr_svc.get_characteristics_async()
                if char_result.status == GattCommunicationStatus.SUCCESS:
                    break
            except Exception as e:
                self.app.log(f"{label}: Characteristics attempt {attempt+1}/3 error: {e}")
            await asyncio.sleep(0.3)

        if not char_result or char_result.status != GattCommunicationStatus.SUCCESS:
            self.app.log(f"{label}: Failed to get characteristics (status: {char_result.status if char_result else 'None'})")
            return None

        vr_input = None
        for char in char_result.characteristics:
            if "cf01" in str(char.uuid).lower():
                vr_input = char
                break
        if not vr_input:
            self.app.log(f"{label}: CF01 characteristic not found")
            return None

        # Clear any stale CCCD from previous session
        try:
            await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
        except Exception:
            pass
        await asyncio.sleep(0.1)

        mgr = self

        def on_notify(sender, args):
            try:
                data = ibuffer_to_bytes(args.characteristic_value)
                mgr._handle_data(data)
            except Exception:
                pass

        try:
            cccd_result = await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            if cccd_result == GattCommunicationStatus.SUCCESS:
                token = vr_input.add_value_changed(on_notify)
                self.app.log(f"{label}: Notifications active")
                return ("notify", vr_input, token)
            else:
                self.app.log(f"{label}: Using polling mode")
                return ("poll", vr_input, None)
        except Exception as e:
            self.app.log(f"{label}: Notify failed, polling")
            return ("poll", vr_input, None)

    async def _poll_char(self, char):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
        try:
            result = await char.read_value_async()
            if result.status == GattCommunicationStatus.SUCCESS:
                data = ibuffer_to_bytes(result.value)
                self._handle_data(data)
        except Exception:
            pass


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


# ── SlimeVR forwarding (runs alongside whichever mode is active) ─────────

class SlimeVRTracker:
    """One emulated SlimeVR tracker — one glove, up to two sensors.

    The server keys trackers by the MAC in the handshake, so each glove gets a
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
    """Feeds glove IMU quaternions to a SlimeVR server as two emulated trackers.

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


# ── Runtime hand skeleton (display only) ─────────────────────────────────
#
# Reads the 31-bone hand skeleton the VR runtime is tracking — e.g. Steam
# Link's camera-based hand tracking — for display under each hand panel.
#
# Backend contract (duck-typed, like the bridge modes): .start(), .stop(),
# .status (short string for the panel placeholder), and .hands — a 2-list
# indexed by hand (0=left, 1=right) holding either None or a tuple of
# (x, y, z) joint positions in a wrist-origin space. Backends swap whole
# tuples in atomically, so readers need no lock.
#
# Windows backend is OpenVR skeletal input: its Background app type attaches
# to a running SteamVR without a graphics session and without contending with
# the focused game — something OpenXR on SteamVR cannot do yet (no headless,
# XR_EXTX_overlay still provisional). An OpenXR backend for bridge_linux /
# Monado can slot into create_skeleton_source() when that lands.

# Hand skeleton indices: 0 root/palm, 1 wrist, then five finger chains off
# the wrist, tips at 5/10/15/20/25. This layout is shared by SteamVR's native
# 31-bone skeleton (26-30 are aux bones, ignored) and the 26-bone OpenXR-style
# set Steam Link reports; bone count itself is queried from the runtime.
SKELETON_CHAINS = (
    (1, 2, 3, 4, 5),          # thumb
    (1, 6, 7, 8, 9, 10),      # index
    (1, 11, 12, 13, 14, 15),  # middle
    (1, 16, 17, 18, 19, 20),  # ring
    (1, 21, 22, 23, 24, 25),  # pinky
)
SKELETON_TIPS = frozenset((5, 10, 15, 20, 25))

SKELETON_ACTION_SET = "/actions/cyberfinger"
SKELETON_ACTIONS = ("/actions/cyberfinger/in/skeleton_left",
                    "/actions/cyberfinger/in/skeleton_right")

SKELETON_APP_KEY = "drscicortex.cyberfinger.bridge"

# VR events worth narrating in the console — resolved by name at runtime so a
# pyopenvr build lacking one just skips it.
_SKELETON_EVENTS = (
    "VREvent_TrackedDeviceActivated",
    "VREvent_TrackedDeviceDeactivated",
    "VREvent_TrackedDeviceRoleChanged",
    "VREvent_TrackedDeviceUserInteractionStarted",   # headset put on
    "VREvent_TrackedDeviceUserInteractionEnded",     # headset taken off
    "VREvent_EnterStandbyMode",
    "VREvent_LeaveStandbyMode",
    "VREvent_Input_BindingLoadFailed",
    "VREvent_Input_BindingLoadSuccessful",
    "VREvent_Input_ActionManifestReloaded",
    "VREvent_SceneApplicationChanged",
)

_HMD_ACTIVITY_LEVELS = {
    "k_EDeviceActivityLevel_Unknown": "activity unknown",
    "k_EDeviceActivityLevel_Idle": "idle (not worn)",
    "k_EDeviceActivityLevel_UserInteraction": "active (worn)",
    "k_EDeviceActivityLevel_UserInteraction_Timeout": "recently active",
    "k_EDeviceActivityLevel_Standby": "standby",
    "k_EDeviceActivityLevel_Idle_Timeout": "idle timeout",
}


STEAMVR_SETTINGS_PATH = os.path.join(
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "Steam", "config", "steamvr.vrsettings")


def _clean_pinned_bindings_offline(log):
    """Drop workshop binding pins for our app key from steamvr.vrsettings.

    SteamVR's binding UI can autosave a legacy workshop binding as this app's
    pinned selection, which silently disables our skeleton actions (see
    _check_pinned_binding). Editing the file is only safe while vrserver is
    down — it rewrites the file on exit — so this runs from the retry path
    after openvr.init fails. Only vr-input-workshop:// pins are dropped; a
    deliberately hand-picked local binding survives. Returns True if the file
    was changed.
    """
    try:
        with open(STEAMVR_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    section = data.get(SKELETON_APP_KEY)
    if not isinstance(section, dict):
        return False
    removed = []
    for key in list(section.keys()):
        if not key.endswith("_steamvrinput"):
            continue
        val = section[key]
        if isinstance(val, str) and not val.startswith("vr-input-workshop://"):
            continue  # a non-workshop pin was chosen on purpose; keep it
        removed.append(key)
        del section[key]
    if not removed:
        return False
    if not section:
        del data[SKELETON_APP_KEY]
    try:
        tmp = STEAMVR_SETTINGS_PATH + ".cyberfinger.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=3)
        os.replace(tmp, STEAMVR_SETTINGS_PATH)
    except Exception as e:
        log(f"Skeleton: could not clean steamvr.vrsettings: {e!r}")
        return False
    log(f"Skeleton: removed stale binding pin(s) from steamvr.vrsettings: "
        + ", ".join(removed))
    return True


def _write_app_manifest():
    """Write a .vrmanifest reflecting how this process was actually launched.

    Registering it (plus identifyApplication) is what makes SteamVR show
    "CyberFinger Bridge" in Manage Controller Bindings instead of filing us
    under an auto-generated "python.exe" key. Generated at runtime because the
    truthful binary path differs between `python cyberfinger_gui.py` and the
    PyInstaller exe. Returns the manifest path.
    """
    if getattr(sys, "frozen", False):
        binary, arguments = sys.executable, ""
    else:
        binary = sys.executable
        arguments = f'"{os.path.abspath(sys.argv[0])}"'
    manifest = {
        "applications": [{
            "app_key": SKELETON_APP_KEY,
            "launch_type": "binary",
            "binary_path_windows": binary,
            "arguments": arguments,
            "is_dashboard_overlay": False,
            "strings": {
                "en_us": {
                    "name": "CyberFinger Bridge",
                    "description": "CyberFinger glove bridge — hand skeleton display",
                },
            },
        }],
    }
    cfg_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           "CyberFingerBridge")
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "cyberfinger.vrmanifest")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return path


def create_skeleton_source(log=None, bisect=False):
    """Pick the skeleton backend for this platform, or None if unavailable.

    bisect=True ("skeleton_bisect" in settings.json) brings the OpenVR
    session up in staged steps with 20 s holds, so if SteamVR falls over the
    last stage announced in the console names the culprit.
    """
    if HAS_OPENVR:
        return OpenVRSkeletonSource(log, bisect=bisect)
    return None


class OpenVRSkeletonSource:
    """Polls SteamVR for hand skeletons in a background thread.

    Connects as a Background app so it never launches SteamVR itself; while
    SteamVR is down it just retries quietly.
    """

    RETRY_S = 5.0
    HOLD_S = 20.0  # per-stage hold in bisect mode

    def __init__(self, log=None, bisect=False):
        self._log = log or (lambda msg: None)
        self._bisect = bisect
        self._poll_actions = True
        self._ready_at = 0.0
        # Per hand: (rot_3x3_rows, head_local_pos_xyz, distance_m) or None.
        # World pose of the hand device relative to the HMD, for the 6DOF
        # display. Swapped atomically like .hands.
        self.pose_info = [None, None]
        self.hands = [None, None]   # 0 = left, 1 = right
        self.status = "starting..."
        self._running = False
        self._thread = None
        self._ready = False
        self._logged_waiting = False
        self._offline_cleaned = False
        self._reset_requested = False
        self._reset_count = 0
        self._vrin = None
        self._system = None
        self._actions = [None, None]
        self._action_set = None
        self._event_names = {getattr(openvr, n): n[8:] for n in _SKELETON_EVENTS
                             if hasattr(openvr, n)}
        self._activity_names = {getattr(openvr, k): v
                                for k, v in _HMD_ACTIVITY_LEVELS.items()
                                if hasattr(openvr, k)}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._teardown()

    # ── OpenVR session ──

    def _init_openvr(self):
        self._stage_connect()
        self._hold("1/5 client connected (idle)")
        self._stage_identity()
        self._hold("2/5 app identity registered")
        self._stage_actions()
        self._hold("3/5 action manifest + handles loaded")

        # In bisect mode stage 4 is passive polling (events + HMD activity
        # only); _loop promotes to stage 5 (action polling) after the hold.
        self._poll_actions = not self._bisect
        self._ready = True
        self._ready_at = time.time()
        self.status = "connected"
        self._connected_at = time.time()
        self._hand_active = [None, None]   # tri-state: unknown / False / True
        self._ever_active = False
        self._bone_count = [0, 0]
        self._err_logged = [False, False]
        self._hmd_activity = None
        self._last_diag = time.time()
        self._inactive_since = None
        self._ctype_logged = {}
        self._logged_waiting = False
        self._reset_requested = False
        self._log("Skeleton: connected to SteamVR")
        # Binding attachment completes on the device's first delivered input
        # event (observed: a thumb-index pinch attaches instantly after
        # standby). A haptic pulse is the one output we can push without
        # bound actions; on some stacks it nudges that same path awake.
        for role in (openvr.TrackedControllerRole_LeftHand,
                     openvr.TrackedControllerRole_RightHand):
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx != openvr.k_unTrackedDeviceIndexInvalid:
                    self._system.triggerHapticPulse(idx, 0, 1000)
            except Exception:
                pass
        if self._bisect:
            self._log("Skeleton BISECT: stage 4/5 passive polling "
                      f"(events + HMD activity) — holding {int(self.HOLD_S)} s")
        self._log_controller_types()

    def _stage_connect(self):
        # Probe as Background first: that type never auto-launches SteamVR,
        # so the bridge stays passive while VR is down. Once the server is
        # known to be up, reconnect as Overlay — overlay apps' action sets
        # keep getting pumped even in the void (no scene app; this machine
        # runs with SteamVR Home disabled), where a Background app's skeleton
        # bindings may never attach origins.
        openvr.init(openvr.VRApplication_Background)
        openvr.shutdown()
        openvr.init(openvr.VRApplication_Overlay)
        self._system = openvr.VRSystem()
        self._vrin = openvr.VRInput()
        # Hold a real (hidden) overlay handle, not just the app type: after
        # the vrlink HMD cycles through standby in the void, vrserver stops
        # attaching binding origins for clients without one.
        try:
            self._overlay = openvr.VROverlay().createOverlay(
                "drscicortex.cyberfinger.bridge.anchor", "CyberFinger Bridge")
        except Exception as e:
            self._overlay = None
            self._log(f"Skeleton: overlay anchor failed: {type(e).__name__}")

    def _stage_identity(self):
        # Identify as our own app key so SteamVR's binding UI lists us as
        # "CyberFinger Bridge" rather than an auto-generated python.exe entry.
        # Best-effort: skeleton reading works without it, rebinding does not.
        try:
            vrapps = openvr.VRApplications()
            vrapps.addApplicationManifest(_write_app_manifest(), True)  # temporary
            vrapps.identifyApplication(os.getpid(), SKELETON_APP_KEY)
        except Exception as e:
            self._log(f"Skeleton: app identity registration failed: {e!r}")

    def _stage_actions(self):
        self._manifest_path = resource_path(
            os.path.join("assets", "cyberfinger_actions.json"))
        self._vrin.setActionManifestPath(self._manifest_path)
        self._action_set = self._vrin.getActionSetHandle(SKELETON_ACTION_SET)
        self._actions = [self._vrin.getActionHandle(a) for a in SKELETON_ACTIONS]

    def _hold(self, label):
        """In bisect mode, announce the stage and idle through its window so a
        SteamVR-side death lands unambiguously inside one stage."""
        if not self._bisect:
            return
        self._log(f"Skeleton BISECT: stage {label} — holding {int(self.HOLD_S)} s")
        deadline = time.time() + self.HOLD_S
        while self._running and time.time() < deadline:
            time.sleep(0.2)
        if not self._running:
            raise RuntimeError("stopped during bisect hold")

    def _log_controller_types(self):
        """Log each hand's controller type — this is the string a binding file
        must name, so it is the first thing to check when nothing draws."""
        for role, name in ((openvr.TrackedControllerRole_LeftHand, "L"),
                           (openvr.TrackedControllerRole_RightHand, "R")):
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx == openvr.k_unTrackedDeviceIndexInvalid:
                    continue
                ctype = self._system.getStringTrackedDeviceProperty(
                    idx, openvr.Prop_ControllerType_String)
            except Exception:
                continue
            if ctype and ctype != self._ctype_logged.get(name):
                self._ctype_logged[name] = ctype
                self._log(f"Skeleton: {name} controller type '{ctype}'")
                self._check_pinned_binding(ctype)

    def _check_pinned_binding(self, ctype):
        """Warn if a saved workshop binding pins this controller type.

        Opening SteamVR's binding UI on an app can autosave a legacy workshop
        binding as the app's "current" selection (steamvr.vrsettings, key
        <ctype>_250820_CurrentURL_steamvrinput). A pin overrides our
        default_bindings entirely, and a legacy binding carries no skeleton
        actions — so the skeleton goes permanently inactive with no error
        anywhere.

        Reads the settings FILE, never the IVRSettings API: every vrserver
        c0000005 today followed an IVRSettings call from this client within
        seconds (getString included), while runs without any settings IPC were
        crash-free — so this client does not speak IVRSettings at all. Repair
        also happens on the file, offline — see
        _clean_pinned_bindings_offline, run while SteamVR is down.
        """
        try:
            with open(STEAMVR_SETTINGS_PATH, "r", encoding="utf-8") as f:
                section = json.load(f).get(SKELETON_APP_KEY, {})
            val = section.get(f"{ctype}_250820_CurrentURL_steamvrinput")
        except Exception:
            return
        if val and str(val).startswith("vr-input-workshop://"):
            self._log(f"Skeleton: WARNING — saved binding {val} overrides the "
                      f"defaults for '{ctype}'; skeleton will stay inactive. "
                      "Fix: close SteamVR and relaunch this bridge (auto-clean), "
                      "or pick the CyberFinger default binding in SteamVR.")

    def _teardown(self):
        self._ready = False
        self.hands = [None, None]
        self.pose_info = [None, None]
        self._vrin = None
        self._system = None
        try:
            openvr.shutdown()
        except Exception:
            pass

    def _loop(self):
        while self._running:
            if not self._ready:
                try:
                    self._init_openvr()
                except Exception:
                    self._teardown()
                    self.status = "SteamVR not running"
                    if not self._logged_waiting:
                        self._logged_waiting = True
                        self._log("Skeleton: SteamVR not running, will retry")
                    # With vrserver down it is safe to sweep out any stale
                    # workshop binding pin that would mute the skeleton.
                    if not self._offline_cleaned:
                        self._offline_cleaned = True
                        try:
                            _clean_pinned_bindings_offline(self._log)
                        except Exception:
                            pass
                    # Sleep in short slices so stop() stays responsive.
                    deadline = time.time() + self.RETRY_S
                    while self._running and time.time() < deadline:
                        time.sleep(0.2)
                    continue
            try:
                self._poll()
            except Exception:
                self._teardown()
                self.status = "SteamVR lost, retrying"
                self._log("Skeleton: lost SteamVR connection")
                continue
            if self._reset_requested:
                # Bindings never attached (input context built while the HMD
                # was asleep). A fresh client connect attaches immediately —
                # the manual-reload observation, automated.
                self._reset_requested = False
                self._log("Skeleton: bindings never attached — reconnecting")
                self._teardown()
                self.status = "reconnecting..."
                continue
            if (self._bisect and not self._poll_actions
                    and time.time() - self._ready_at >= self.HOLD_S):
                self._poll_actions = True
                self._log("Skeleton BISECT: stage 5/5 full action polling "
                          "(updateActionState + skeletal reads)")
            time.sleep(1.0 / 30.0)

    def _poll(self):
        # A quit event means SteamVR is going down — raise into the retry path
        # so the session is torn down promptly instead of erroring out call by
        # call while SteamVR waits on us to exit.
        ev = openvr.VREvent_t()
        while self._system.pollNextEvent(ev):
            if ev.eventType == openvr.VREvent_Quit:
                self._system.acknowledgeQuit_Exiting()
                raise RuntimeError("SteamVR quit")
            name = self._event_names.get(ev.eventType)
            if name:
                self._log(f"Skeleton: event {name} (device {ev.trackedDeviceIndex})")
            if ev.eventType in (openvr.VREvent_TrackedDeviceActivated,
                                openvr.VREvent_TrackedDeviceRoleChanged):
                self._log_controller_types()
                # Devices returning from standby may accept a different bone
                # count, and any earlier read failure is stale news — reset so
                # recovery is attempted and new failures get logged again.
                self._bone_count = [0, 0]
                self._err_logged = [False, False]
            # A scene app starting is the one event known to un-wedge
            # vrserver's binding attachment, so it re-arms fast reconnects.
            # Device churn does NOT — it's constant with camera hand tracking.
            if ev.eventType == getattr(openvr,
                                       "VREvent_SceneApplicationChanged", -1):
                self._reset_count = 0

        # HMD activity explains most "why is nothing tracking" confusion —
        # Steam Link only streams hand skeletons while the headset is worn.
        try:
            lvl = self._system.getTrackedDeviceActivityLevel(
                openvr.k_unTrackedDeviceIndex_Hmd)
        except Exception:
            lvl = None
        if lvl != self._hmd_activity:
            self._hmd_activity = lvl
            self._log("Skeleton: HMD "
                      + self._activity_names.get(lvl, f"activity {lvl}"))

        # World poses for the 6DOF display. Device poses come from IVRSystem,
        # not the skeletal actions, so this works even while the skeleton is
        # still warming up.
        try:
            poses = (openvr.TrackedDevicePose_t
                     * openvr.k_unMaxTrackedDeviceCount)()
            self._system.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseStanding, 0.0, poses)
            hmd = self._extract_pose(poses[openvr.k_unTrackedDeviceIndex_Hmd])
            for hand, role in ((0, openvr.TrackedControllerRole_LeftHand),
                               (1, openvr.TrackedControllerRole_RightHand)):
                info = None
                if hmd is not None:
                    idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                    if idx != openvr.k_unTrackedDeviceIndexInvalid:
                        dev = self._extract_pose(poses[idx])
                        if dev is not None:
                            info = self._relative_pose(hmd, dev)
                self.pose_info[hand] = info
        except Exception:
            self.pose_info = [None, None]

        if not self._poll_actions:
            return  # bisect stage 4: passive only

        active = (openvr.VRActiveActionSet_t * 1)()
        active[0].ulActionSet = self._action_set
        self._vrin.updateActionState(active)

        for hand, action in enumerate(self._actions):
            hn = "L" if hand == 0 else "R"
            joints = None
            try:
                data = self._vrin.getSkeletalActionData(action)
                if bool(data.bActive) != self._hand_active[hand]:
                    # Don't log the initial unknown→False transition: hands
                    # simply not being tracked yet at startup is the normal
                    # case, not an event.
                    if data.bActive or self._hand_active[hand] is not None:
                        self._log(f"Skeleton: {hn} hand "
                                  + ("tracking" if data.bActive else "lost"))
                    self._hand_active[hand] = bool(data.bActive)
                if data.bActive:
                    self._ever_active = True
                    self._err_logged[hand] = False  # re-arm error reporting
                    self._reset_count = 0
                    bones = self._get_bones(action, hand)
                    if bones is not None:
                        joints = tuple(
                            (t.position.v[0], t.position.v[1], t.position.v[2])
                            for t in bones)
            except Exception as e:
                if not self._err_logged[hand]:
                    self._err_logged[hand] = True
                    self._log(f"Skeleton: {hn} read error: {e!r}")
            self.hands[hand] = joints

        # "connected" alone is misleading when the actions never go active —
        # surface the most likely cause right in the panel placeholder. Hands
        # leaving camera view is the everyday case; a hand that has never once
        # tracked long after connect suggests a binding problem instead.
        if any(self._hand_active):
            self.status = "connected"
        elif self._ever_active:
            self.status = "hands not in view"
        elif time.time() - self._connected_at > 30.0:
            self.status = "no data — try a finger pinch"

        # While nothing is tracking, narrate the state so the console answers
        # "why" instead of leaving a frozen status — including when tracking
        # worked earlier and then got stuck after a standby/wake cycle. Fast
        # cadence for the first minute of an inactive stretch, then slow, so
        # an idle bridge doesn't flood the console overnight.
        now = time.time()
        if any(self._hand_active):
            self._inactive_since = None
        else:
            if self._inactive_since is None:
                self._inactive_since = now
            cadence = 5.0 if now - self._inactive_since < 60.0 else 60.0
            if now - self._last_diag >= cadence:
                self._last_diag = now
                roles_held, total_origins = self._diag()
                # Stuck-state self-heal: devices hold hand roles and the HMD
                # is worn, yet after a grace period no origins ever attached.
                worn = getattr(openvr, "k_EDeviceActivityLevel_UserInteraction", 1)
                # Two quick reconnect attempts, then slow periodic retries
                # forever — the wedge clears on SteamVR's schedule (settling
                # after boot, or a scene app starting), so give up never,
                # just quietly.
                grace = 20.0 if self._reset_count < 2 else 120.0
                if (roles_held and total_origins == 0
                        and self._hmd_activity == worn
                        and now - self._connected_at > grace):
                    if self._reset_count == 0:
                        self._log("Skeleton: tip — a thumb-index pinch "
                                  "usually completes attachment instantly")
                    elif self._reset_count == 2:
                        self._log(
                            "Skeleton: bindings still not attaching — "
                            "dropping to slow retries (every 2 min). "
                            "A finger pinch or starting any VR app "
                            "usually fixes it instantly")
                    self._reset_count += 1
                    self._reset_requested = True

    def _diag(self):
        roles_held = 0
        total_origins = 0
        for hand, action in enumerate(self._actions):
            hn = "L" if hand == 0 else "R"
            role = (openvr.TrackedControllerRole_LeftHand if hand == 0
                    else openvr.TrackedControllerRole_RightHand)
            parts = []
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx == openvr.k_unTrackedDeviceIndexInvalid:
                    parts.append("no device holds this hand role")
                else:
                    roles_held += 1
                    conn = self._system.isTrackedDeviceConnected(idx)
                    parts.append(f"device #{idx}"
                                 + ("" if conn else " (disconnected)"))
            except Exception as e:
                parts.append(f"role query failed: {e!r}")
            try:
                data = self._vrin.getSkeletalActionData(action)
                parts.append("action ACTIVE" if data.bActive else "action inactive")
            except Exception as e:
                parts.append(f"skeletal data error: {e!r}")
            # pyopenvr's getActionOrigins wrapper is broken (2.12 ends with
            # `originsOut.value` on a ctypes array) — call the C function
            # table directly instead.
            try:
                count = getattr(openvr, "k_unMaxActionOriginCount", 16)
                origins = (openvr.VRInputValueHandle_t * count)()
                # Pass the array itself: ctypes converts it to the pointer the
                # prototype wants. byref(origins[0]) is a TypeError, because
                # indexing a simple-type ctypes array yields a plain int —
                # the exact bug inside pyopenvr's own wrapper.
                err = self._vrin.function_table.getActionOrigins(
                    self._action_set, action, origins, count)
                if err == 0:
                    n = sum(1 for o in origins if o)
                    total_origins += n
                    parts.append(f"{n} binding origin(s)")
                else:
                    parts.append(f"origins error {err}")
            except Exception as e:
                parts.append(f"origins query failed: {type(e).__name__}")
            try:
                parts.append(
                    f"tracking level {int(self._vrin.getSkeletalTrackingLevel(action))}")
            except Exception:
                pass
            self._log(f"Skeleton: {hn} diag — " + ", ".join(parts))
        return roles_held, total_origins

    def _get_bones(self, action, hand):
        """Fetch bone transforms, discovering the count the runtime accepts.

        getBoneCount cannot be trusted: with Steam Link hand tracking it
        reports the standard 31-bone skeleton while GetSkeletalBoneData
        demands the count the driver actually submits (rejecting everything
        else as InvalidBoneCount). So probe — reported count first, then the
        two known skeleton sizes, then the rest — and cache what works.
        """
        n = self._bone_count[hand]
        if n < 0:
            return None  # probing already failed for this hand; stay quiet
        if n > 0:
            try:
                return self._fetch_bones(action, n)
            except Exception as e:
                if type(e).__name__ != "InputError_InvalidBoneCount":
                    raise
                self._bone_count[hand] = 0  # skeleton changed; re-probe

        hn = "L" if hand == 0 else "R"
        try:
            reported = self._vrin.getBoneCount(action)
        except Exception:
            reported = 0
        candidates = []
        for c in [reported, 26, 31] + list(range(1, 65)):
            if c > 0 and c not in candidates:
                candidates.append(c)
        for c in candidates:
            try:
                bones = self._fetch_bones(action, c)
            except Exception as e:
                if type(e).__name__ == "InputError_InvalidBoneCount":
                    continue
                raise
            self._bone_count[hand] = c
            extra = f" (runtime claims {reported})" if reported != c else ""
            self._log(f"Skeleton: {hn} using {c} bones{extra}")
            return bones
        self._bone_count[hand] = -1
        self._log(f"Skeleton: {hn} rejected every bone count 1-64 "
                  f"(runtime claims {reported})")
        return None

    @staticmethod
    def _extract_pose(pose):
        """TrackedDevicePose_t → (position, rotation rows, velocity), or None."""
        if not pose.bPoseIsValid:
            return None
        m = pose.mDeviceToAbsoluteTracking.m
        rot = tuple(tuple(float(m[r][c]) for c in range(3)) for r in range(3))
        pos = tuple(float(m[r][3]) for r in range(3))
        vel = tuple(float(pose.vVelocity.v[i]) for i in range(3))
        return pos, rot, vel

    @staticmethod
    def _relative_pose(hmd, dev):
        """(hand world rotation, head-local position, distance, head-local
        velocity relative to the head).

        Head frame follows OpenVR device convention: +x right, +y up,
        -z forward — what the dome inset projects.
        """
        (hpos, hrot, hvel), (dpos, drot, dvel) = hmd, dev
        rel = tuple(dpos[i] - hpos[i] for i in range(3))
        relv = tuple(dvel[i] - hvel[i] for i in range(3))
        # Rows of hrot are the head axes in world space, so head-local is
        # R^T · v.
        local = tuple(sum(hrot[r][i] * rel[r] for r in range(3))
                      for i in range(3))
        local_v = tuple(sum(hrot[r][i] * relv[r] for r in range(3))
                        for i in range(3))
        dist = math.sqrt(sum(v * v for v in rel))
        return drot, local, dist, local_v

    def _fetch_bones(self, action, n):
        # Must pass a caller-allocated ctypes array: pyopenvr's wrapper
        # quietly substitutes a 1-element array for any non-array argument
        # and calls the C API with count=1, which the runtime rejects as
        # InvalidBoneCount no matter what count we intended.
        arr = (openvr.VRBoneTransform_t * n)()
        self._vrin.getSkeletalBoneData(
            action, openvr.VRSkeletalTransformSpace_Model,
            openvr.VRSkeletalMotionRange_WithoutController, arr)
        return arr


# ── Gamepad Mode (ViGEm Xbox 360) ────────────────────────────────────────

class GamepadMode:
    def __init__(self):
        self.gamepad = None
        self.available = False
        try:
            import vgamepad as vg
            self.vg = vg
            self.gamepad = vg.VX360Gamepad()
            self.available = True
        except ImportError:
            pass
        except Exception:
            pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available:
            return
        vg = self.vg
        gp = self.gamepad

        gp.reset()

        # Sticks (Y inverted)
        gp.left_joystick_float(x_value_float=left.joy_x_float, y_value_float=-left.joy_y_float)
        gp.right_joystick_float(x_value_float=right.joy_x_float, y_value_float=-right.joy_y_float)

        # Triggers (analog)
        gp.left_trigger_float(value_float=left.trigger_float)
        gp.right_trigger_float(value_float=right.trigger_float)

        # ── Right hand (original assignments preserved) ──
        if right.buttons & BTN_TRIGGER:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)           # btn 1
        if right.buttons & BTN_GRIP:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_B)           # btn 2
        if right.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER) # btn 6
        if right.buttons & BTN_JCLICK:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB) # btn 10
        if right.buttons & BTN_STSEL:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_START)       # btn 8

        # ── Left hand (original assignments preserved) ──
        if left.buttons & BTN_TRIGGER:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)           # btn 3
        if left.buttons & BTN_GRIP:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)           # btn 4
        if left.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER) # btn 5
        if left.buttons & BTN_JCLICK:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)  # btn 9
        if left.buttons & BTN_STSEL:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)        # btn 7

        # ── New C/D/E buttons — raw wButtons bits (11-16) ──
        # Xbox 360 wButtons is a 16-bit field; bits 11-15 are unused by XInput
        # and pass through ViGEm, appearing as buttons 11-16 in DirectInput.
        # bit 11 = 0x0800 (reserved, unused by XInput)
        # bit 12 = 0x1000 ... already XUSB_GAMEPAD_A — so we use D-pad bits
        # instead, which are free in this mapping (no d-pad inputs assigned):
        # DPAD_UP=0x0001(btn11), DPAD_DOWN=0x0002(btn12), DPAD_LEFT=0x0004(btn13)
        # DPAD_RIGHT=0x0008(btn14), GUIDE=0x0400(btn15), reserved=0x0800(btn16)
        if right.buttons & BTN_C:
            gp.report.wButtons |= 0x0001  # DPAD_UP   → btn 11 (R-C)
        if right.buttons & BTN_D:
            gp.report.wButtons |= 0x0002  # DPAD_DOWN → btn 12 (R-D)
        if right.buttons & BTN_E:
            gp.report.wButtons |= 0x0004  # DPAD_LEFT → btn 13 (R-E)
        if left.buttons & BTN_C:
            gp.report.wButtons |= 0x0008  # DPAD_RIGHT → btn 14 (L-C)
        if left.buttons & BTN_D:
            gp.report.wButtons |= 0x0400  # GUIDE      → btn 15 (L-D)
        if left.buttons & BTN_E:
            gp.report.wButtons |= 0x0800  # reserved   → btn 16 (L-E)

        gp.update()

    def stop(self):
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()


# ── Gamepad Mode — VRChat optimised ──────────────────────────────────────

class GamepadModeVRChat:
    """
    ViGEm Xbox 360 gamepad with VRChat-optimal button mapping.

    VRChat gamepad layout (Xbox reference):
      Left  stick          → Move (head-relative in VR)
      Right stick X        → Smooth turn
      Right stick Y        → Look up/down
      RT (right trigger)   → Use / Interact  (right hand)
      LT (left trigger)    → Use / Interact  (left hand)
      A                    → Jump
      B / Y                → Quick Menu
      R3 (right stick click) → Action Menu
      L3 (left  stick click) → Action Menu (left)
      X                    → Mute toggle

    CyberFinger → Xbox mapping:
      Right TRIGGER  → RT  (use/interact right)
      Left  TRIGGER  → LT  (use/interact left)
      Right GRIP     → A   (jump)
      Left  GRIP     → B   (quick menu)
      Right MENU     → Y   (quick menu right)
      Left  MENU     → X   (mute)
      Right JCLICK   → R3  (action menu right)
      Left  JCLICK   → L3  (action menu left)
      Right C        → RB  (extra / world-specific)
      Left  C        → LB  (extra / world-specific)
      Right D        → DPAD_RIGHT
      Left  D        → DPAD_LEFT
      Right E / Left E → DPAD_UP / DPAD_DOWN
      ST/SE (either) → Start
    """

    def __init__(self):
        self.gamepad = None
        self.available = False
        try:
            import vgamepad as vg
            self.vg = vg
            self.gamepad = vg.VX360Gamepad()
            self.available = True
        except ImportError:
            pass
        except Exception:
            pass

        # OSC client for per-hand UseLeft/UseRight — VRChat gamepad mode has
        # no separate left hand interact, but OSC UseLeft/UseRight works in VR
        # and can run simultaneously alongside the gamepad input.
        self._osc = None
        try:
            from pythonosc import udp_client
            self._osc = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        except ImportError:
            pass

        self._prev_use_r = False
        self._prev_use_l = False
        self._prev_grab_r = False
        self._prev_grab_l = False
        self._grab_right_toggled = False
        self._grab_left_toggled  = False
        self._grab_right_press_time = 0.0
        self._grab_left_press_time  = 0.0
        self._prev_jclick_r = False
        self._prev_jclick_l = False
        self._prev_stsel_l  = False
        self._prev_stsel_r  = False
        self._prev_c_r      = False

    def _osc_send(self, address, value):
        if self._osc:
            try:
                self._osc.send_message(address, value)
            except Exception:
                pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available:
            return
        vg = self.vg
        gp = self.gamepad

        gp.reset()

        # ── Sticks ────────────────────────────────────────────────────
        gp.left_joystick_float(x_value_float=left.joy_x_float,
                               y_value_float=-left.joy_y_float)
        gp.right_joystick_float(x_value_float=right.joy_x_float,
                                y_value_float=-right.joy_y_float)

        # ── Triggers ───────────────────────────────────────────────────
        # Right trigger → RT (gamepad, right hand interact)
        # Left  trigger → OSC /input/UseLeft only (no LT gamepad — avoids
        #                 duplicate/conflicting events with right hand)
        trig_r = max(right.trigger_float, 1.0 if (right.buttons & BTN_TRIGGER) else 0.0)
        trig_l = max(left.trigger_float,  1.0 if (left.buttons  & BTN_TRIGGER) else 0.0)
        gp.right_trigger_float(value_float=trig_r)
        gp.left_trigger_float(value_float=0.0)      # suppressed — UseLeft via OSC

        # ── OSC: UseLeft (left trigger) — no gamepad equivalent ────────
        use_l = trig_l > 0.1
        if use_l != self._prev_use_l:
            self._osc_send("/input/UseLeft", int(use_l))
            self._prev_use_l = use_l

        # ── OSC: GrabRight / GrabLeft (button 2 = GRIP) ────────────────
        # Tap  (<200ms): toggle grab state
        # Hold (≥200ms): release on finger-up
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

        # ── Stick clicks ───────────────────────────────────────────────
        # Right jclick → Jump (gamepad A)
        # Left  jclick → Jump (gamepad A, same button — either hand jumps)
        jclick_r = bool(right.buttons & BTN_JCLICK)
        jclick_l = bool(left.buttons  & BTN_JCLICK)
        if jclick_r or jclick_l:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
        self._prev_jclick_r = jclick_r
        self._prev_jclick_l = jclick_l

        # ── Right hand (gamepad) ────────────────────────────────────────
        if right.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)   # Action menu R
        # ── Right C → F12 screenshot (rising edge) ─────────────────────
        c_r = bool(right.buttons & BTN_C)
        if c_r and not self._prev_c_r:
            if HAS_PYNPUT:
                try:
                    _keyboard.press(Key.f12)
                    _keyboard.release(Key.f12)
                except Exception:
                    pass
        self._prev_c_r = c_r
        if right.buttons & BTN_D:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT)
        if right.buttons & BTN_E:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP)
        # Right ST/SE → open VRChat chatbox keyboard (rising edge)
        stsel_r = bool(right.buttons & BTN_STSEL)
        if stsel_r and not self._prev_stsel_r:
            # b=False opens the keyboard, n=False suppresses notification SFX
            self._osc_send("/chatbox/input", ["", False, False])
        self._prev_stsel_r = stsel_r

        # ── Left hand (gamepad + OSC) ────────────────────────────────────
        # Left MENU → Start (Quick Menu, gamepad)
        if left.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
        # BTN_GRIP → OSC GrabLeft (no gamepad event)
        if left.buttons & BTN_C:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)             # Mute
        if left.buttons & BTN_D:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT)
        if left.buttons & BTN_E:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN)
        # ST/SE → /input/Voice mute toggle: 1 on press, 0 on release
        stsel_l = bool(left.buttons & BTN_STSEL)
        if stsel_l != self._prev_stsel_l:
            self._osc_send("/input/Voice", int(stsel_l))
        self._prev_stsel_l = stsel_l

        gp.update()

    def stop(self):
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()
        for addr in ("/input/UseLeft", "/input/GrabRight", "/input/GrabLeft",
                     "/input/QuickMenuToggleRight", "/input/Voice"):
            self._osc_send(addr, 0)


# ── GUI Application ──────────────────────────────────────────────────────

class CyberFingerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Bridge")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("680x790")
        self.root.minsize(600, 660)

        # Set window icon (color version)
        try:
            icon_path = resource_path(os.path.join("assets", "icon_32x32.png"))
            icon_img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon_img)
            self._icon_ref = icon_img  # prevent GC
        except Exception:
            pass

        self.log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self._current_status = "Idle"
        self._window_visible = True

        # Config persistence
        self._config_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                        "CyberFingerBridge")
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = VRMode()
        self.gamepad_mode = None         # created lazily on first use
        self.vrchat_gamepad_mode = None  # created lazily on first use
        self.active_mode = None
        self.slimevr = None              # created lazily while forwarding is on
        # Config gate ("skeleton_enabled": false in settings.json) exists so
        # the OpenVR client can be ruled in/out when debugging SteamVR-side
        # trouble without touching code.
        self.skeleton = (create_skeleton_source(
                             self.log, self._config.get("skeleton_bisect", False))
                         if self._config.get("skeleton_enabled", True) else None)

        self._build_ui()

        if self.skeleton:
            self.skeleton.start()

        # System tray icon
        self._tray_icon = None
        if HAS_TRAY:
            self._setup_tray()

        self._poll_queues()

        # X button minimizes to tray (if available), otherwise saves and quits
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Auto-start if enabled
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
        """Switch tray icon between running (color) and idle (B&W)."""
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
        """Left-click on tray icon: toggle window visibility."""
        if self._window_visible:
            self.root.after(0, self._hide_window)
        else:
            self.root.after(0, self._show_window)

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self._start_bridge)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self._stop_bridge)

    def _tray_exit(self, icon=None, item=None):
        """Exit from tray context menu — full shutdown."""
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
        """X button pressed — minimize to tray if available, else quit."""
        if HAS_TRAY and self._tray_icon:
            self._hide_window()
            self.log("Minimized to system tray")
        else:
            self._quit_app()

    def _quit_app(self):
        """Full application shutdown."""
        self._config["mode"] = self.mode_var.get()
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self._stop_slimevr()
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
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT,
                        font=("Consolas", 14, "bold"))
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_FG_DIM,
                        font=("Consolas", 9))
        style.configure("Hand.TLabel", background=COLOR_BG2, foreground=COLOR_FG,
                        font=("Consolas", 10))
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("Consolas", 10), focuscolor=COLOR_BG)
        style.map("TRadiobutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("Small.TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("Consolas", 9), focuscolor=COLOR_BG)
        style.map("Small.TRadiobutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=("Consolas", 9), focuscolor=COLOR_BG)
        style.map("TCheckbutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="white",
                        font=("Consolas", 11, "bold"), padding=(20, 8))
        style.map("Accent.TButton",
                  background=[("active", COLOR_ACCENT2), ("disabled", COLOR_BG3)])
        style.configure("Stop.TButton", background=COLOR_RED, foreground="white",
                        font=("Consolas", 11, "bold"), padding=(20, 8))
        style.map("Stop.TButton",
                  background=[("active", "#ff4444"), ("disabled", COLOR_BG3)])
        style.configure("Console.TButton", background=COLOR_BG3, foreground=COLOR_FG,
                        font=("Consolas", 9), padding=(10, 2))
        style.map("Console.TButton",
                  background=[("active", COLOR_BG2)],
                  foreground=[("active", COLOR_ACCENT)])

        # ── Header ──
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(12, 4))
        ttk.Label(header, text="⬡ CyberFinger Bridge", style="Title.TLabel").pack(side=tk.LEFT)
        self.status_label = ttk.Label(header, text="Idle", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # ── Mode selection + Start/Stop ──
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, padx=16, pady=(4, 4))

        # Radio buttons stacked vertically on the left
        self.mode_var = tk.StringVar(value=self._config.get("mode", "vr"))
        radio_frame = ttk.Frame(ctrl_frame)
        radio_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(radio_frame, text="VR Mode (BLE→SteamVR)",
                        variable=self.mode_var, value="vr").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→Xbox 360, Resonite)",
                        variable=self.mode_var, value="gamepad").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→Xbox 360, VRChat)",
                        variable=self.mode_var, value="gamepad_vrc").pack(anchor=tk.W)

        # Start/Stop buttons on the right
        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", style="Stop.TButton",
                                   command=self._stop_bridge, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.start_btn = ttk.Button(ctrl_frame, text="Start", style="Accent.TButton",
                                    command=self._start_bridge)
        self.start_btn.pack(side=tk.RIGHT)

        # ── Options row ──
        opts_frame = ttk.Frame(self.root)
        opts_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.autostart_var = tk.BooleanVar(value=self._config.get("autostart", False))
        ttk.Checkbutton(opts_frame, text="Auto-start on launch",
                        variable=self.autostart_var,
                        command=self._on_autostart_changed).pack(side=tk.LEFT)

        if HAS_TRAY:
            ttk.Label(opts_frame, text="(close button minimizes to tray)",
                     style="Status.TLabel").pack(side=tk.RIGHT)

        # ── SlimeVR row ──
        slime_frame = ttk.Frame(self.root)
        slime_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.slimevr_var = tk.BooleanVar(value=self._config.get("slimevr_enabled", False))
        ttk.Checkbutton(slime_frame, text="Forward IMU to SlimeVR",
                        variable=self.slimevr_var,
                        command=self._on_slimevr_changed).pack(side=tk.LEFT)

        # Body 1 and Body 2 are redundant IMUs at the same location, so only one
        # is forwarded — this picks which, falling back to the other if absent.
        ttk.Label(slime_frame, text="  body IMU:",
                  style="Status.TLabel").pack(side=tk.LEFT)
        self.slimevr_body_var = tk.StringVar(
            value=self._config.get("slimevr_body_imu", "body1"))
        for label, value in (("1", "body1"), ("2", "body2")):
            ttk.Radiobutton(slime_frame, text=label, style="Small.TRadiobutton",
                            variable=self.slimevr_body_var, value=value,
                            command=self._on_slimevr_changed).pack(side=tk.LEFT)

        # ── Hands visualization ──
        hands_frame = ttk.Frame(self.root)
        hands_frame.pack(fill=tk.X, padx=16, pady=4)

        self.left_panel = HandPanel(hands_frame, "LEFT", side=tk.LEFT)
        self.right_panel = HandPanel(hands_frame, "RIGHT", side=tk.RIGHT)

        # ── Bottom bar: console toggle ──
        # Packed before the skeleton row so pack gives it its slice at the
        # bottom and the skeleton area expands into whatever is left.
        bottom = ttk.Frame(self.root)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 8))
        self.console_visible = self._config.get("console_visible", False)
        self.console_btn = ttk.Button(
            bottom, text="▼ Console" if self.console_visible else "▲ Console",
            style="Console.TButton", command=self._toggle_console)
        self.console_btn.pack(side=tk.RIGHT)

        # ── Hand skeleton row (what the VR runtime is tracking) ──
        self.skeleton_area = ttk.Frame(self.root)
        self.skeleton_area.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 4))
        self.left_skeleton = SkeletonPanel(self.skeleton_area, "LEFT", side=tk.LEFT)
        self.right_skeleton = SkeletonPanel(self.skeleton_area, "RIGHT", side=tk.RIGHT)

        # ── Log console — hidden by default, slides up over the skeletons ──
        self.log_frame = ttk.Frame(self.root)
        self.log_text = scrolledtext.ScrolledText(
            self.log_frame, height=8,
            bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
            font=("Consolas", 9), relief=tk.FLAT, borderwidth=0,
            selectbackground=COLOR_ACCENT, selectforeground="white",
            state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("accent", foreground=COLOR_ACCENT)
        self.log_text.tag_configure("green", foreground=COLOR_GREEN)
        self.log_text.tag_configure("red", foreground=COLOR_RED)

        self._console_frac = 1.0 if self.console_visible else 0.0
        self._console_anim = None
        if self.console_visible:
            self._place_console(1.0)

    def _place_console(self, frac):
        """Overlay the console over the bottom `frac` of the skeleton area."""
        self.log_frame.place(in_=self.skeleton_area, relx=0.0, rely=1.0,
                             anchor="sw", relwidth=1.0,
                             relheight=max(0.02, frac))

    def _toggle_console(self):
        self.console_visible = not self.console_visible
        self._config["console_visible"] = self.console_visible
        self._save_config()
        self.console_btn.configure(
            text="▼ Console" if self.console_visible else "▲ Console")
        if self._console_anim is not None:
            self.root.after_cancel(self._console_anim)
        self._animate_console()

    def _animate_console(self):
        self._console_anim = None
        target = 1.0 if self.console_visible else 0.0
        delta = target - self._console_frac
        if abs(delta) < 0.02:
            self._console_frac = target
            if target > 0.0:
                self._place_console(1.0)
                self.log_text.see(tk.END)
            else:
                self.log_frame.place_forget()
            return
        self._console_frac += max(-0.2, min(0.2, delta))
        self._place_console(self._console_frac)
        self._console_anim = self.root.after(16, self._animate_console)

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

    def _start_bridge(self):
        if self.active_mode:
            return  # Already running

        mode = self.mode_var.get()
        if mode in ("gamepad", "gamepad_vrc"):
            try:
                import vgamepad  # noqa — just check it's importable
            except ImportError:
                self.log("ERROR: vgamepad not available!")
                self.log("Install: pip install vgamepad")
                self.log("Also need ViGEmBus driver")
                return

        self._config["mode"] = mode
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        if mode == "vr":
            self.active_mode = self.vr_mode
        elif mode == "gamepad":
            self.gamepad_mode = GamepadMode()
            self.active_mode = self.gamepad_mode
        elif mode == "gamepad_vrc":
            self.vrchat_gamepad_mode = GamepadModeVRChat()
            self.active_mode = self.vrchat_gamepad_mode
        else:
            self.active_mode = self.vr_mode  # fallback
        self.log(f"Starting {mode.upper()} mode...")
        if mode == "vrchat":
            self.log(">>> VRChat: enable OSC via Action Menu → OSC → Enabled")
            self.log(">>> VRChat window must be focused for Use/Grab to work")

        if self.slimevr_var.get():
            self._start_slimevr()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_tray_running(True)

        self.ble.start()

    def _stop_bridge(self):
        if not self.active_mode:
            return  # Not running

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self.active_mode = None
        self._stop_slimevr()

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        self.left_panel.set_disconnected()
        self.right_panel.set_disconnected()
        self.set_status("Stopped")
        self.log("Bridge stopped")
        self._set_tray_running(False)

        # Recreate for next start
        self.ble = BLEManager(self)
        self.gamepad_mode = None         # recreated lazily on next start
        self.vrchat_gamepad_mode = None  # recreated lazily on next start

    def on_input(self, hand, state):
        """Called from BLE thread on each input report."""
        if self.active_mode:
            if isinstance(self.active_mode, (GamepadMode, GamepadModeVRChat)):
                self.active_mode.update_gamepad(self.ble.left, self.ble.right)
            else:
                self.active_mode.on_input(hand, state)

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
        """Process log/status messages on the main thread."""
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

        # Skip the skeleton redraw while the console fully covers it.
        if self._console_frac < 1.0:
            if self.skeleton:
                poses = self.skeleton.pose_info
                self.left_skeleton.draw(self.skeleton.hands[0],
                                        self.skeleton.status,
                                        poses[0], poses[1])
                self.right_skeleton.draw(self.skeleton.hands[1],
                                         self.skeleton.status,
                                         poses[1], poses[0])
            else:
                why = ("disabled in settings"
                       if not self._config.get("skeleton_enabled", True)
                       else "pip install openvr")
                self.left_skeleton.draw(None, why)
                self.right_skeleton.draw(None, why)

        self.root.after(33, self._poll_queues)  # ~30fps

    def run(self):
        self.log("CyberFinger Bridge ready")
        try:
            import vgamepad  # noqa
            self.log("Gamepad mode: available")
        except ImportError:
            self.log("Gamepad mode: not available (install vgamepad + ViGEmBus)")
        if not HAS_TRAY:
            self.log("System tray: not available (install pystray pillow)")
        if not HAS_PYNPUT:
            self.log("Keyboard (F12 screenshot): not available (install pynput)")
        if not HAS_OPENVR:
            self.log("Hand skeleton: not available (pip install openvr)")
        self.root.mainloop()


# ── Minimal 3D helpers (orthographic, no external deps) ──────────────────

# Fixed camera angles — a three-quarter view so all three axes stay distinct.
_VIEW_YAW   = math.radians(35.0)
_VIEW_PITCH = math.radians(20.0)


def quat_to_matrix(q):
    """Unit quaternion (w, x, y, z) → 3x3 rotation matrix as row tuples."""
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = w / n, x / n, y / n, z / n
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z),       2.0 * (x * z + w * y)),
        (2.0 * (x * y + w * z),       1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
        (2.0 * (x * z - w * y),       2.0 * (y * z + w * x),       1.0 - 2.0 * (x * x + y * y)),
    )


def quat_to_euler_deg(q):
    """Unit quaternion (w, x, y, z) → (roll, pitch, yaw) in degrees."""
    w, x, y, z = q

    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Clamp guards against gimbal-lock inputs drifting just past ±1.
    sinp = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sinp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def rotate_vec(m, v):
    """Apply a 3x3 row-major matrix to a 3-vector."""
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def project(v, cx, cy, scale):
    """World point → (screen_x, screen_y, depth). Larger depth is nearer."""
    x, y, z = v

    # Yaw about world Y, then pitch about the camera's X.
    cyaw, syaw = math.cos(_VIEW_YAW), math.sin(_VIEW_YAW)
    xe = x * cyaw - z * syaw
    ze = x * syaw + z * cyaw

    cp, sp = math.cos(_VIEW_PITCH), math.sin(_VIEW_PITCH)
    ye = y * cp - ze * sp
    depth = y * sp + ze * cp

    # Screen y is inverted so +Y points up on the canvas.
    return (cx + xe * scale, cy - ye * scale, depth)


# ── Hand visualization panel ─────────────────────────────────────────────

class HandPanel:
    """Canvas-based hand state visualization."""

    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True, padx=(0, 4) if side == tk.LEFT else (4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, height=350)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._last_state = None

    def update_state(self, state: HandState):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        is_left = self.label == "LEFT"
        lr = "L" if is_left else "R"

        # Title
        if state.connected:
            c.create_text(w // 2, 14, text=f"{self.label}", fill=COLOR_ACCENT,
                         font=("Consolas", 11, "bold"))
        else:
            c.create_text(w // 2, 14, text=f"{self.label} (disconnected)",
                         fill=COLOR_FG_DIM, font=("Consolas", 10))
            return

        # Battery
        bat = state.battery
        bat_color = COLOR_GREEN if bat > 50 else COLOR_ORANGE if bat > 20 else COLOR_RED
        c.create_text(w - 10, 14, text=f"{bat}%", fill=bat_color,
                     font=("Consolas", 9), anchor=tk.E)

        # Packet counter
        c.create_text(10, 14, text=f"#{state.packet_count}", fill=COLOR_FG_DIM,
                     font=("Consolas", 8), anchor=tk.W)

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
        btn_x = 3 * w // 4 if is_left else w // 4
        btn_y_start = 30
        btn_spacing = 17
        btn_names_bits = [
            ("TRIG", BTN_TRIGGER),
            ("GRIP", BTN_GRIP),
            ("C",    BTN_C),
            ("D",    BTN_D),
            ("E",    BTN_E),
            ("MENU", BTN_MENU),
            ("JCLK", BTN_JCLICK),
            ("ST/SE",BTN_STSEL),
        ]

        for i, (name, bit) in enumerate(btn_names_bits):
            by = btn_y_start + i * btn_spacing
            pressed = bool(state.buttons & bit)
            fill = COLOR_ACCENT if pressed else COLOR_BG
            outline = COLOR_ACCENT if pressed else COLOR_BG3
            c.create_oval(btn_x - 7, by - 7, btn_x + 7, by + 7,
                         fill=fill, outline=outline, width=2)
            c.create_text(btn_x + 14, by, text=name, fill=COLOR_FG if pressed else COLOR_FG_DIM,
                         font=("Consolas", 8), anchor=tk.W)

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
                     fill=COLOR_FG_DIM, font=("Consolas", 8))

        # ── IMU orientation ──
        self._draw_imu(c, w, h, state)

    def _draw_imu(self, c, w, h, state):
        """Draw a 3D triad per populated IMU slot, or a placeholder if there are none."""
        c.create_line(20, 192, w - 20, 192, fill=COLOR_BG3, width=1)

        imus = state.active_imus()
        if not imus:
            c.create_text(w // 2, 258, text="no IMU installed",
                         fill=COLOR_FG_DIM, font=("Consolas", 9))
            return

        # Share the panel width between however many slots are live, shrinking
        # the triads rather than letting them collide.
        col_w = w / len(imus)
        scale = max(18.0, min(46.0, col_w * 0.30))
        cy = 262

        for i, (label, quat) in enumerate(imus):
            cx = col_w * (i + 0.5)
            c.create_text(cx, 206, text=label, fill=COLOR_FG_DIM,
                         font=("Consolas", 8, "bold"))
            self._draw_triad(c, cx, cy, scale, quat)

            roll, pitch, yaw = quat_to_euler_deg(quat)
            if len(imus) == 1:
                readout = f"R{roll:+6.1f}  P{pitch:+6.1f}  Y{yaw:+6.1f}"
            else:
                readout = f"{roll:+.0f} {pitch:+.0f} {yaw:+.0f}"
            c.create_text(cx, 322, text=readout,
                         fill=COLOR_FG_DIM, font=("Consolas", 8))

    def _draw_triad(self, c, cx, cy, scale, quat):
        """Render one orientation as an XYZ axis triad against a horizon ring."""
        m = quat_to_matrix(quat)

        # Reference ground ring so rotation reads against a fixed horizon.
        ring = []
        for i in range(32):
            a = 2.0 * math.pi * i / 32
            px, py, _ = project((math.cos(a), 0.0, math.sin(a)), cx, cy, scale)
            ring.extend((px, py))
        c.create_polygon(ring, outline=COLOR_BG3, fill="", width=1)

        axes = [
            ((1.0, 0.0, 0.0), COLOR_RED,   "X"),
            ((0.0, 1.0, 0.0), COLOR_GREEN, "Y"),
            ((0.0, 0.0, 1.0), COLOR_BLUE,  "Z"),
        ]

        # Paint far-to-near so nearer arms overlap correctly.
        drawn = []
        for vec, color, name in axes:
            px, py, depth = project(rotate_vec(m, vec), cx, cy, scale)
            drawn.append((depth, px, py, color, name))
        drawn.sort(key=lambda t: t[0])

        ox, oy, _ = project((0.0, 0.0, 0.0), cx, cy, scale)
        for depth, px, py, color, name in drawn:
            # Nearer arms draw thicker — a cheap depth cue without shading.
            width = 3 if depth >= 0 else 2
            c.create_line(ox, oy, px, py, fill=color, width=width)
            c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=color, outline="")
            c.create_text(px + 9, py - 7, text=name, fill=color,
                         font=("Consolas", 8, "bold"))

    def set_disconnected(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w > 10:
            c.create_text(w // 2, h // 2, text=f"{self.label}\n(disconnected)",
                         fill=COLOR_FG_DIM, font=("Consolas", 10), justify=tk.CENTER)


class SkeletonPanel:
    """Canvas rendering of the runtime-tracked hand skeleton for one hand."""

    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True,
                        padx=(0, 4) if side == tk.LEFT else (4, 0))
        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0,
                                height=150)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # Projection axes, chosen from the first tracked frame and then kept
        # fixed — re-deriving per frame makes the view twitch between axes
        # whenever the hand tilts past 45°.
        self._axes = None
        # Dome trail: recent (time, head-local unit direction) samples for
        # this panel's own hand, redrawn with fading color each frame.
        self._trail = []

    def _pick_axes(self, joints):
        """Choose the two model-space axes to project onto: the widest-spread
        axis is drawn vertically (finger direction), the runner-up across.
        Sign puts fingertips at the top of the canvas."""
        pts = joints[1:26]  # skip root and aux bones
        ext = []
        for a in range(3):
            vals = [p[a] for p in pts]
            ext.append((max(vals) - min(vals), a))
        ext.sort(reverse=True)
        v_axis, h_axis = ext[0][1], ext[1][1]
        tips = [joints[t][v_axis] for t in SKELETON_TIPS]
        v_sign = -1.0 if (sum(tips) / len(tips)) >= joints[1][v_axis] else 1.0
        self._axes = (h_axis, v_axis, v_sign)

    def draw(self, joints, status, pose=None, other_pose=None):
        """pose/other_pose: (rot_3x3, head_local_pos, dist) from pose_info."""
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        if not joints or len(joints) < 26:
            self._axes = None
            c.create_text(w // 2, h // 2,
                          text=f"{self.label} skeleton\n({status})",
                          fill=COLOR_FG_DIM, font=("Consolas", 9),
                          justify=tk.CENTER)
        else:
            c.create_text(w // 2, 12, text=f"{self.label} SKELETON",
                          fill=COLOR_BLUE, font=("Consolas", 8, "bold"))
            if pose is not None:
                self._draw_bones(c, w, h,
                                 self._orient_to_px(joints, pose[0], w, h,
                                                    dist=pose[2]))
            else:
                self._draw_bones(c, w, h, self._autofit_to_px(joints, w, h))

        if pose is not None:
            self._draw_dome(c, w, h, pose, other_pose)

    # ── skeleton projections ──

    def _orient_to_px(self, joints, rot, w, h, dist=None):
        """World-oriented view: wrist-relative joints rotated by the hand
        device's world rotation, then through the same fixed three-quarter
        camera the IMU triads use. Base scale is physical (no zooming as the
        hand turns); on top of that, distance from the head scales the whole
        render like the dome dot — closer hand draws bigger."""
        scale = min(w - 24, h - 36) / 0.28  # px per metre, hand span ~0.25 m
        if dist is not None:
            scale *= max(0.6, min(1.5, 0.55 / max(dist, 0.2)))
        wx, wy, wz = joints[1]
        # Straight-on camera, unlike the IMU triads' three-quarter view: its
        # 35° yaw reads as "the hand is rotated wrong", not as perspective.
        # A touch of pitch keeps some depth without skewing the heading.
        cy_, sy_ = 1.0, 0.0
        cp_, sp_ = math.cos(_VIEW_PITCH), math.sin(_VIEW_PITCH)

        def to_px(j):
            lx, ly, lz = joints[j]
            lx, ly, lz = lx - wx, ly - wy, lz - wz
            x = rot[0][0] * lx + rot[0][1] * ly + rot[0][2] * lz
            y = rot[1][0] * lx + rot[1][1] * ly + rot[1][2] * lz
            z = rot[2][0] * lx + rot[2][1] * ly + rot[2][2] * lz
            # 180° about vertical: without it the render is left/right
            # mirrored relative to the user's own view of their hand.
            x, z = -x, -z
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            y1 = y * cp_ - z1 * sp_
            return w / 2 + x1 * scale, h / 2 + 6 - y1 * scale

        return to_px

    def _autofit_to_px(self, joints, w, h):
        """Fallback when no device pose is available: original auto-fit."""
        if self._axes is None:
            self._pick_axes(joints)
        h_axis, v_axis, v_sign = self._axes

        pts = joints[1:26]
        hs = [p[h_axis] for p in pts]
        vs = [p[v_axis] * v_sign for p in pts]
        cx_m = (max(hs) + min(hs)) / 2.0
        cy_m = (max(vs) + min(vs)) / 2.0
        span_h = max(max(hs) - min(hs), 0.05)
        span_v = max(max(vs) - min(vs), 0.05)
        scale = min((w - 24) / span_h, (h - 32) / span_v)

        def to_px(j):
            p = joints[j]
            return (w / 2 + (p[h_axis] - cx_m) * scale,
                    h / 2 + 4 + (p[v_axis] * v_sign - cy_m) * scale)

        return to_px

    @staticmethod
    def _draw_bones(c, w, h, to_px):
        for chain in SKELETON_CHAINS:
            px = [to_px(j) for j in chain]
            for (x0, y0), (x1, y1) in zip(px, px[1:]):
                c.create_line(x0, y0, x1, y1, fill=COLOR_FG_DIM, width=2)

        for chain in SKELETON_CHAINS:
            for j in chain[1:]:
                x, y = to_px(j)
                if j in SKELETON_TIPS:
                    c.create_oval(x - 3, y - 3, x + 3, y + 3,
                                  fill=COLOR_ACCENT, outline="")
                else:
                    c.create_oval(x - 2, y - 2, x + 2, y + 2,
                                  fill=COLOR_BLUE, outline="")

        wx, wy = to_px(1)
        c.create_rectangle(wx - 3, wy - 3, wx + 3, wy + 3,
                           fill=COLOR_GREEN, outline="")

    # ── position inset: 180° dome as seen from the head ──

    def _draw_dome(self, c, w, h, pose, other_pose):
        """Azimuthal-equidistant projection of the front hemisphere: centre is
        straight ahead of the gaze, rings at 30°/60°/90° off-forward, the rim
        is beside/behind the head. Dot size encodes distance."""
        r = max(24, min(44, h // 3))
        margin = 8
        cx = (margin + r) if self.label == "LEFT" else (w - margin - r)
        cy = h - margin - r

        for k in (1 / 3, 2 / 3, 1.0):
            rr = r * k
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                          outline=COLOR_BG3, width=1)
        for deg in range(0, 360, 45):
            a = math.radians(deg)
            c.create_line(cx + (r / 3) * math.cos(a), cy + (r / 3) * math.sin(a),
                          cx + r * math.cos(a), cy + r * math.sin(a),
                          fill=COLOR_BG3, width=1)
        c.create_line(cx - 3, cy, cx + 3, cy, fill=COLOR_FG_DIM)
        c.create_line(cx, cy - 3, cx, cy + 3, fill=COLOR_FG_DIM)

        # Fading trail of the own hand's last second of motion.
        now = time.time()
        self._trail.append((now, pose[1]))
        while self._trail and self._trail[0][0] < now - 1.0:
            self._trail.pop(0)
        pts = [self._dome_project(cx, cy, r, p)[:2] for _, p in self._trail]
        for i in range(1, len(pts)):
            age = (now - self._trail[i][0])  # 0 = fresh, 1 = oldest
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=_blend(COLOR_ACCENT, COLOR_BG2, age), width=1)

        if other_pose is not None:
            self._dome_dot(c, cx, cy, r, other_pose, COLOR_FG_DIM)
        self._dome_dot(c, cx, cy, r, pose, COLOR_ACCENT)

        c.create_text(cx, cy - r - 7, text=f"{pose[2]:.2f}m",
                      fill=COLOR_FG_DIM, font=("Consolas", 7))

    @staticmethod
    def _dome_project(cx, cy, r, local):
        """Head-local point → dome pixel position (+ whether behind 90°)."""
        x, y, z = local
        n = math.sqrt(x * x + y * y + z * z)
        if n < 1e-6:
            return cx, cy, False
        ux, uy, uz = x / n, y / n, z / n
        # Head frame: +x right, +y up, -z forward. Angle off forward-gaze:
        theta = math.acos(max(-1.0, min(1.0, -uz)))
        rr = min(theta / (math.pi / 2), 1.0) * r
        phi = math.atan2(uy, ux)
        return (cx + rr * math.cos(phi), cy - rr * math.sin(phi),
                theta > math.pi / 2)

    def _dome_dot(self, c, cx, cy, r, pose, color):
        _, local, dist, vel = pose
        px, py, behind = self._dome_project(cx, cy, r, local)

        # Velocity whisker: where the hand will be in 0.15 s, projected the
        # same way, so the whisker curves with the dome rather than lying.
        speed = math.sqrt(sum(v * v for v in vel))
        if speed > 0.05:
            ahead = tuple(local[i] + vel[i] * 0.15 for i in range(3))
            qx, qy, _ = self._dome_project(cx, cy, r, ahead)
            c.create_line(px, py, qx, qy, fill=color, width=1)

        size = max(2.5, 7.0 - dist * 6.0)  # closer → bigger
        if behind:
            c.create_oval(px - size, py - size, px + size, py + size,
                          outline=color, width=1)
        else:
            c.create_oval(px - size, py - size, px + size, py + size,
                          fill=color, outline="")


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
