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
        self.root.geometry("680x620")
        self.root.minsize(600, 540)

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

        self._build_ui()

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

        # ── Log console ──
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8,
            bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
            font=("Consolas", 9), relief=tk.FLAT, borderwidth=0,
            selectbackground=COLOR_ACCENT, selectforeground="white",
            state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("accent", foreground=COLOR_ACCENT)
        self.log_text.tag_configure("green", foreground=COLOR_GREEN)
        self.log_text.tag_configure("red", foreground=COLOR_RED)

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


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
