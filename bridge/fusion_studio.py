"""CyberFinger Fusion Studio — a single-window application that fuses the headset's optical hand tracking (OpenXR),
the glove IMUs (BLE) and the MindRove forearm EMG into one live view of the body, the arm and the whole hand, with
per-source switches, hand-position prediction out of view, EMG finger posture and key-posture recognition.

Run:  python fusion_studio.py      (start the glove, the EMG armband and the headset camera preview from the top bar)
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
import csv
try:
    import key_postures                              # Key Postures tab: EMG + tilt recogniser for a few fixed postures
except Exception:                                    # noqa: BLE001
    key_postures = None
try:
    import mount_calib                               # Fusion Studio: camera-taught sensor mounting (live wrist bend out of view)
except Exception:                                    # noqa: BLE001
    mount_calib = None
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
try:
    import openvr
    HAS_OPENVR = True
except Exception:
    HAS_OPENVR = False
try:
    from armband_panel import ArmbandTab
    HAS_ARMBAND = True
except Exception:
    HAS_ARMBAND = False
try:
    from openxr_skeleton import OpenXRHandSkeletonSource, openxr_available
    HAS_OPENXR = openxr_available()
except Exception:
    HAS_OPENXR = False
    OpenXRHandSkeletonSource = None
try:
    import numpy as _np
    _FUSION_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fusion")
    if _FUSION_DIR not in sys.path:
        sys.path.insert(0, _FUSION_DIR)
    from gate import OcclusionGate as _OcclusionGate
    HAS_GATE = True
except Exception:
    _np = None
    _OcclusionGate = None
    HAS_GATE = False
try:
    from gesture_skeleton import GestureSkeleton, emg_features, HAS_GESTURE
except Exception:
    GestureSkeleton = None
    emg_features = None
    HAS_GESTURE = False
ENCODER_GUIDE_STEPS = [
    "PALM DOWN  ·  slowly OPEN and CLOSE your whole hand ~10 times",
    "PALM DOWN  ·  pause HALF-open, then QUARTER-closed — hold a few grades",
    "PALM DOWN  ·  curl each finger ALONE:  index → middle → ring → pinky → thumb",
    "PALM DOWN  ·  pinch · tripod · point · fist · flat hand — hold each ~2 s",
    "PALM UP  ·  slowly OPEN and CLOSE ~10 times",
    "PALM UP  ·  pinch · tripod · point · fist · flat hand",
    "PALM SIDEWAYS (thumb up)  ·  slowly OPEN and CLOSE ~10 times",
    "PALM SIDEWAYS  ·  pinch · tripod · point · fist · flat hand",
    "ARM RAISED in front  ·  open/close + a few grasps",
    "ARM LOW / relaxed  ·  open/close + a few grasps",
    "FAST  ·  rapid OPEN ↔ CLOSE as fast as you can (~15×)",
    "FAST  ·  quick individual finger TAPS — each finger, rapid",
    "FAST  ·  quick grasp ↔ release (fist ↔ open), rapidly",
    "FAST  ·  quick random finger wiggles + pinches",
    "FAST  ·  palm UP — rapid open ↔ close + quick taps",
]
ENC_RAW_WIN = 256    # raw EMG samples logged per frame (~0.5 s @500 Hz) — lets the TCN learn its
ENC_LABEL_CONF = {"CLEAR": 1.0, "SELF": 0.5, "OTHER": 0.4, "OBJECT": 0.3, "OUT_OF_VIEW": 0.2}
POSTURES = ["fist", "open", "point", "picking", "ok"]   # Posture-library tab: labelled gestures
GESTURE_REC_SECS = 6.0      # per-gesture recording duration (more samples = better fit)
GESTURE_REC_SETTLE = 1.5    # skip this much at the start — let the contraction settle to
try:
    from extrinsic import hand_frame_wrist as _fus_hand_frame
    from extrinsic import quat_to_R as _fus_quat_to_R
    from extrinsic import calibrate as _fus_calibrate
    from extrinsic import swing_twist as _fus_swing_twist
    from extrinsic import geodesic_deg as _fus_geo_R
    from extrinsic import _R_to_quat as _fus_R_to_quat
    from orient_fusion import OrientationFuser as _OrientationFuser
    from orient_fusion import geodesic_quat_deg as _fus_geo_quat
    from orient_fusion import qmul as _fus_qmul, qconj as _fus_qconj, qnorm as _fus_qnorm
    HAS_FUSION = HAS_GATE
except Exception:
    _fus_hand_frame = _fus_quat_to_R = _fus_calibrate = _OrientationFuser = None
    _fus_geo_quat = _fus_swing_twist = _fus_geo_R = _fus_R_to_quat = None
    _fus_qmul = _fus_qconj = _fus_qnorm = None
    HAS_FUSION = False
try:
    import occlusion as _occl
except Exception:
    _occl = None
try:
    import online_calib as _ocal      # T3: attention-based online calibration (optional)
except Exception:
    _ocal = None                      # a T3 failure disables ONLY the Live Calib tab, never core fusion
VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"
GAMEPAD_MAGIC    = 0x50474643
GAMEPAD_PACK_FMT = "<IBBhhBB"
INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)
INPUT_REPORT_IMU_FMT = "<BBhhBBI4f"
INPUT_REPORT_IMU_SIZE = struct.calcsize(INPUT_REPORT_IMU_FMT)
INPUT_REPORT_MULTI_FMT = "<BBhhBBI4fB4f4f"
INPUT_REPORT_MULTI_SIZE = struct.calcsize(INPUT_REPORT_MULTI_FMT)
ACCEL_TAIL_FMT = "<9h"
ACCEL_TAIL_SIZE = struct.calcsize(ACCEL_TAIL_FMT)
INPUT_REPORT_ACCEL_SIZE = INPUT_REPORT_MULTI_SIZE + ACCEL_TAIL_SIZE
ZERO_ACCEL = (0, 0, 0)
ACCEL_LSB_PER_G = 2048.0  # VR_ACCEL_LSB_PER_G — ±16g on every sensor
GRAVITY_MS2 = 9.80665
JOY_DEADZONE = 0.15
IMU_BODY_PRIMARY   = 0x01
IMU_BODY_SECONDARY = 0x02
IMU_JOINT          = 0x04
IMU_SLOT_LABELS = (
    (IMU_BODY_PRIMARY,   "BODY 1"),
    (IMU_BODY_SECONDARY, "BODY 2"),
    (IMU_JOINT,          "JOINT"),
)
IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)
_SYNC_IMU_COLS = ["gate_type", "imu_present",
                  "g_qw", "g_qx", "g_qy", "g_qz",       # glove BODY-1 quat [w,x,y,z]
                  "g2_qw", "g2_qx", "g2_qy", "g2_qz",   # glove BODY-2 quat
                  "gj_qw", "gj_qx", "gj_qy", "gj_qz",   # glove JOINT (back-of-hand) quat
                  "g_ax", "g_ay", "g_az"]               # glove BODY accel
_SYNC_EMG_COLS = [f"emg{c}" for c in range(8)] + \
                 ["arm_ax", "arm_ay", "arm_az", "arm_gx", "arm_gy", "arm_gz", "emg_present"]
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
SLIME_TRACKER_TYPE_ROTATION = 0
SLIME_SENSOR_OFFLINE = 0
SLIME_SENSOR_OK      = 1
SLIME_DATA_TYPE_NORMAL     = 1  # DATA_TYPE_NORMAL
SLIME_SENSOR_DATA_ROTATION = 0  # SENSOR_DATATYPE_ROTATION
SLIME_POS_LEFT_LOWER_ARM  = 13
SLIME_POS_RIGHT_LOWER_ARM = 14
SLIME_POS_LEFT_HAND       = 17
SLIME_POS_RIGHT_HAND      = 18
SLIME_SENSOR_BODY  = 0
SLIME_SENSOR_JOINT = 1
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
OCC_PHASES = [
    ("VISIBLE",          8, "Keep your RIGHT hand open, palm facing the headset, "
                            "fingers spread."),
    ("SELF OCCLUSION",   8, "Slowly rotate your RIGHT hand so the palm faces away "
                            "from the headset and the fingers are hidden behind the hand."),
    ("VISIBLE",          8, "Rotate your RIGHT hand back so the palm faces the headset."),
    ("HAND BEHIND HAND", 8, "Bring your LEFT hand in front of the RIGHT hand and "
                            "cover the right-hand fingers."),
    ("VISIBLE",          8, "Move the LEFT hand away and keep the RIGHT hand open "
                            "and clearly visible."),
]
OCC_PRECOUNT = 3   # s of "get ready" countdown before phase 1
def _occ_phase_color(label):
    """Green while the hand should be fully visible, orange during an occlusion
    stress phase — so a glance at the panel says which regime you're in."""
    return COLOR_GREEN if label.upper().startswith("VISIBLE") else COLOR_ORANGE


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


class HandState:
    def __init__(self):
        self.buttons = 0
        self.joy_x = 0
        self.joy_y = 0
        self.joy_cx = 0          # stick center offset (auto-captured at rest on connect)
        self.joy_cy = 0          # — a miswired/offset stick that reads e.g. -16000 at rest
        self._joy_cal = []       # is re-zeroed here so it centers correctly
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

    def _joy_deadzoned(self):
        """Raw stick as (x, y) in [-1, 1] with a RADIAL deadzone: magnitudes within
        JOY_DEADZONE of center → (0, 0); beyond it the vector is rescaled so the edge
        still reaches 1.0. Removes small analog drift (a stick wandering at rest) without
        clipping usable range. Large deflections (e.g. a stick physically pressed by a
        mount) are NOT masked — that's a hardware fix, not a deadzone one."""
        x = max(-1.0, min(1.0, (self.joy_x - self.joy_cx) / 32767.0))
        y = max(-1.0, min(1.0, (self.joy_y - self.joy_cy) / 32767.0))
        mag = math.hypot(x, y)
        if mag <= JOY_DEADZONE:
            return 0.0, 0.0
        s = (mag - JOY_DEADZONE) / (1.0 - JOY_DEADZONE) / mag
        return max(-1.0, min(1.0, x * s)), max(-1.0, min(1.0, y * s))

    @property
    def joy_x_float(self):
        return self._joy_deadzoned()[0]

    @property
    def joy_y_float(self):
        return self._joy_deadzoned()[1]

    @property
    def trigger_float(self):
        if self.trigger > 10:
            return self.trigger / 255.0
        return 1.0 if (self.buttons & BTN_TRIGGER) else 0.0

    def reset_link(self):
        """Clear per-connection capability flags before (re)attaching a glove."""
        self.imu_present = 0
        self._joy_cal = []          # re-capture the stick center on the next connect
        self.joy_cx = self.joy_cy = 0
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


class ImuLogger:
    """Per-packet CSV logger for both hands' IMU data (BODY 1/2 + JOINT
    quaternions and raw accel). log_packet() is called from the BLE thread and
    only enqueues; a daemon thread does the disk writes, so file I/O can never
    stall the BLE link (which would drop packets). One row per received packet,
    tagged by hand — filter by the `hand` column in analysis.
    """

    # IMU data only — one body + one joint IMU per hand (both hands via `hand`).
    COLUMNS = [
        "host_time", "seq", "hand", "present",
        "body_qw", "body_qx", "body_qy", "body_qz",       # active BODY IMU quaternion (w,x,y,z)
        "joint_qw", "joint_qx", "joint_qy", "joint_qz",   # JOINT IMU quaternion
        "body_ax", "body_ay", "body_az",                  # BODY IMU accel (raw int16)
        "joint_ax", "joint_ay", "joint_az",               # JOINT IMU accel (raw int16)
    ]

    def __init__(self, path, log=None):
        self.path = path
        self._log = log or (lambda _m: None)
        self._q = queue.Queue()
        self._f = None
        self._w = None
        self._thread = None
        self._running = False
        self._t0 = time.perf_counter()
        self.count = 0

    def start(self):
        self._f = open(self.path, "w", newline="")
        # provenance header so the units are never ambiguous offline
        self._f.write(f"# CyberFinger IMU log — one row per packet, per hand. "
                      f"body_* = the active body IMU (BODY 1 if present, else BODY 2); "
                      f"joint_* = the joint IMU. quats (w,x,y,z) unit; accel raw int16 "
                      f"at {ACCEL_LSB_PER_G:g} LSB/g (±16g). present bitmask: "
                      f"BODY1={IMU_BODY_PRIMARY} BODY2={IMU_BODY_SECONDARY} JOINT={IMU_JOINT}. "
                      f"host_time = monotonic seconds since logging started.\n")
        self._w = csv.writer(self._f)
        self._w.writerow(self.COLUMNS)
        self._running = True
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    def log_packet(self, hand, state, seq):
        """Called on the BLE thread per packet — non-blocking (just enqueues).
        Collapses the two redundant body slots into the single active body IMU."""
        if not self._running:
            return
        p = state.imu_present
        if p & IMU_BODY_PRIMARY:
            body_q, body_a = state.quat, state.accel
        elif p & IMU_BODY_SECONDARY:
            body_q, body_a = state.quat_body2, state.accel_body2
        else:
            body_q, body_a = IDENTITY_QUAT, ZERO_ACCEL
        self._q.put([
            round(time.perf_counter() - self._t0, 6), seq,
            "L" if hand == 0 else "R", p,
            *body_q, *state.quat_joint,
            *body_a, *state.accel_joint,
        ])

    def _writer(self):
        while self._running or not self._q.empty():
            try:
                row = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            self._w.writerow(row)
            self.count += 1

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._f is not None:
            try:
                self._f.flush()
                self._f.close()
            except Exception:
                pass
            self._f = None
        self._log(f"IMU log: wrote {self.count} rows to {self.path}")


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
        # Auto-center the stick from the first ~20 packets after connect (assumed at rest):
        # take the median as the zero point, so an offset/miswired stick (reads e.g.
        # -16000,-16000 at rest, like the RIGHT glove) is corrected to read center.
        if len(state._joy_cal) < 20:
            state._joy_cal.append((joy_x, joy_y))
            if len(state._joy_cal) == 20:
                state.joy_cx = sorted(v[0] for v in state._joy_cal)[10]
                state.joy_cy = sorted(v[1] for v in state._joy_cal)[10]
                if abs(state.joy_cx) > 1500 or abs(state.joy_cy) > 1500:
                    hn = "L" if h == 0 else "R"
                    self.app.log(f"{hn} stick auto-centered (offset "
                                 f"{state.joy_cx},{state.joy_cy})")
        state.trigger = trigger
        state.battery = battery
        state.timestamp = time.time()
        state.packet_count += 1

        if buttons != old_buttons:
            hn = "L" if h == 0 else "R"
            self.app.log(f"{hn} BTN: {fmt_buttons(buttons)}")

        self.app.on_input(h, state)

        logger = self.app.imu_logger
        if logger is not None:
            logger.log_packet(h, state, seq)

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

    async def _read_firmware_revision(self, label, services):
        """Best-effort read of the exact flashed firmware version from the
        standard BLE Device Information Service (0x180A) → Firmware Revision
        String (0x2A26). The glove's firmware publishes FW_VERSION_FULL_STR there
        (e.g. "1.3.1-beta+<git-hash>"). Never raises into the connect path."""
        try:
            from winrt.windows.devices.bluetooth.genericattributeprofile import \
                GattCommunicationStatus
            dis = next((s for s in services if "180a" in str(s.uuid).lower()), None)
            if dis is None:
                return
            cr = await dis.get_characteristics_async()
            if cr.status != GattCommunicationStatus.SUCCESS:
                return
            fw = next((c for c in cr.characteristics
                       if "2a26" in str(c.uuid).lower()), None)
            if fw is None:
                return
            rr = await fw.read_value_async()
            if rr.status != GattCommunicationStatus.SUCCESS:
                return
            ver = ibuffer_to_bytes(rr.value).decode("utf-8", "replace").strip("\x00").strip()
            if ver:
                self.app.log(f"{label}: firmware {ver}")
        except Exception as e:
            self.app.log(f"{label}: firmware version read failed: {e!r}")

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

        # Log the exact flashed firmware version (DIS 0x180A / 0x2A26).
        await self._read_firmware_revision(label, svc_result.services)

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


def create_skeleton_source(log=None, bisect=False, backend="auto"):
    """Pick the skeleton backend, or None if unavailable.

    backend ("skeleton_backend" in settings.json):
      "auto"   — OpenVR when present (the proven Windows path), else OpenXR.
                 This preserves the existing Windows behaviour exactly.
      "openvr" — force the SteamVR/OpenVR reader (31-bone + curl/splay).
      "openxr" — force the cross-platform OpenXR reader (XR_EXT_hand_tracking,
                 26 joints), which also runs on Linux via Monado.

    bisect=True ("skeleton_bisect" in settings.json) applies to the OpenVR
    backend only: it brings the session up in staged steps with 20 s holds, so
    if SteamVR falls over the last stage announced in the console names the culprit.
    """
    _log = log or (lambda _m: None)
    backend = (backend or "auto").lower()
    if backend == "auto":
        backend = "openvr" if HAS_OPENVR else ("openxr" if HAS_OPENXR else "none")

    if backend == "openxr":
        if HAS_OPENXR and OpenXRHandSkeletonSource is not None:
            return OpenXRHandSkeletonSource(log)
        _log("Skeleton: OpenXR backend requested but pyopenxr is not installed "
             "(pip install pyopenxr glfw PyOpenGL).")
        return None
    if backend == "openvr":
        if HAS_OPENVR:
            return OpenVRSkeletonSource(log, bisect=bisect)
        _log("Skeleton: OpenVR backend requested but pyopenvr/SteamVR is unavailable.")
        return None
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


class _OneEuroVec:
    """One-Euro filter (Casiez et al. 2012) for a vector signal — strong smoothing when the value
    is still (kills jitter), light smoothing when it moves fast (low lag). Beats a fixed EMA, which
    must trade one for the other. Used to smooth the per-finger curl."""

    def __init__(self, mincutoff=1.5, beta=0.8, dcutoff=1.0):
        self.mincutoff = mincutoff      # cutoff (Hz) at rest — lower = smoother/steadier
        self.beta = beta                # speed coupling — higher = snappier on fast motion
        self.dcutoff = dcutoff
        self._x = None
        self._dx = None

    def reset(self):
        self._x = self._dx = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def filter(self, x, dt):
        x = _np.asarray(x, float)
        dt = max(1e-3, float(dt))
        if self._x is None:
            self._x = x.copy()
            self._dx = _np.zeros_like(x)
            return self._x
        dx = (x - self._x) / dt
        a_d = self._alpha(self.dcutoff, dt)
        self._dx = a_d * dx + (1.0 - a_d) * self._dx
        cutoff = self.mincutoff + self.beta * _np.abs(self._dx)      # per-component
        a = 1.0 / (1.0 + (1.0 / (2.0 * math.pi * cutoff)) / dt)      # per-component alpha
        self._x = a * x + (1.0 - a) * self._x
        return self._x


class FusionStudioApp:
    """Single-window CyberFinger Fusion Studio: optical hand tracking (Quest / OpenXR) + glove IMUs + forearm EMG fused
    into one body-and-hand view, with per-source switches. Start the three sources from the top bar."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Fusion Studio")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("1700x960")
        self.root.minsize(760, 680)
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
        self.imu_logger = None           # ImuLogger while "Log IMU (CSV)" is on
        self.skeleton = (create_skeleton_source(
                             self.log, self._config.get("skeleton_bisect", False),
                             self._config.get("skeleton_backend", "auto"))
                         if self._config.get("skeleton_enabled", True) else None)
        self._skeleton_running = False
        self._skeleton_on_demand = (
            self.skeleton is not None and OpenXRHandSkeletonSource is not None
            and isinstance(self.skeleton, OpenXRHandSkeletonSource))
        self._occ_skeleton = None
        self._preview_skeleton = None
        self._preview_running = False
        self._occ_running = False
        self._occ_after = None            # pending .after() id
        self._occ_phase = -1              # -1 = pre-countdown, else index into OCC_PHASES
        self._occ_marks = []              # wall-clock start of each phase (+ final end)
        self._occ_deadline = 0.0          # monotonic deadline for the current step
        self._occ_precount_end = 0.0
        self._occ_phases_path = None      # where the phases CSV is written
        self._gate_prev_event = None
        self._gate_events_path = None
        self._gate = [_OcclusionGate(), _OcclusionGate()] if HAS_GATE else [None, None]
        self._gate_last_t = 0.0
        self._fov_az = float(self._config.get("gate_fov_az", 55.0))
        self._fov_el_lo = float(self._config.get("gate_fov_el_lo", -45.0))
        self._fov_el_hi = float(self._config.get("gate_fov_el_hi", 45.0))
        self._fov_samples = []
        self._gate_last_type = ["", ""]     # last per-hand gate verdict, for synced clips
        self._occl_vis = None               # smoothed per-joint visibility (Occlusion tab)
        self._calibrator = _ocal.OnlineFingerCalib() if _ocal is not None else None  # T3 online calib
        self._calib_curl = None             # smoothed calibrated curl (Live Calib tab)
        self._calib_curl_filt = _OneEuroVec(mincutoff=1.2, beta=0.5)   # de-jitter the calibrated fingers
        self._calib_base_filt = _OneEuroVec(mincutoff=1.5, beta=0.7)   # de-jitter the base fingers
        self._calib_feat_ema = None         # temporally-smoothed EMG feature for the lookup (held poses)
        self._calib_last_opt = None         # last camera-verified (curl, gtilt, t) → occlusion pseudo-label
        self._calib_pseudo_t = 0.0          # rate-limit occluded pseudo-label inserts
        self._calib_confw = None            # smoothed PER-FINGER optical weight (T1.2 per-finger fusion)
        self._calib_drift = None            # simulated encoder drift (per-finger bias) for the demo
        self._calib_err = [0.0, 0.0]        # EMA [base-vs-optical, calibrated-vs-optical] error
        self._gesture = GestureSkeleton() if (HAS_GESTURE and GestureSkeleton) else None
        self._gest_rec_until = 0.0
        self._gest_base_pose = None         # last hand seen by optical → base finger shape
        self._gest_hand = 1                 # glove/armband hand (right)
        self._fuser_wrist = (_OrientationFuser(right=(self._gest_hand == 1), slot="g2", tau=1.0)
                             if HAS_FUSION else None)
        self._fuser_knuckle = (_OrientationFuser(right=(self._gest_hand == 1), slot="g2", tau=1.0)
                               if HAS_FUSION else None)
        self._imu_calib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "imu_calib.npz")
        self._load_imu_calib()               # seed a SAVED extrinsic → IMU is calibrated even
        self._imu_calib_buf = []
        self._fuse_err_wrist = None          # live wrist-IMU vs optical error (deg), CLEAR
        self._fuse_err_knuckle = None        # live knuckle-IMU vs optical error (deg), CLEAR
        self._hand_use_knuckle = False       # confidence gate: does the knuckle drive the hand?
        self._prev_wq = None                 # prev-frame raw IMU quats, for motion gating
        self._prev_kq = None
        self._prev_clear = False             # was the hand optically tracked last frame?
        self._gate_enabled = {"OUT_OF_VIEW": True, "OTHER": True, "SELF": True, "OBJECT": True}
        self._disp_pose = None               # last DRAWN finger pose (for the handoff cross-fade)
        self._disp_R = None                  # last DRAWN hand orientation (for the cross-fade)
        self._prev_occluded_shape = True     # prev handoff state, to detect the transition
        self._pose_xfade_start = None        # finger pose to cross-fade FROM at a handoff
        self._R_xfade_start = None           # orientation to cross-fade FROM at a handoff
        self._pose_xfade_t0 = 0.0
        self._xfade_dur = 0.15               # adaptive: set per-handoff from the jump size
        self._enc_recording = False          # Encoder tab: capturing EMG↔optical pairs?
        self._enc_X = []                     # EMG feature vectors (encoder inputs)
        self._enc_Y = []                     # optical local 26-joint poses (encoder labels)
        self._enc_Wq = []                    # wrist IMU quat [w,x,y,z] per pair (IMU-conditioning)
        self._enc_Kq = []                    # knuckle IMU quat [w,x,y,z] per pair
        self._enc_E = []                     # RAW EMG window (n_ch, ENC_RAW_WIN) per pair (for the TCN)
        self._enc_C = []                     # per-frame LABEL confidence from the gate (0..1)
        self._enc_R = []                     # optical orientation R_opt (3x3) per pair — so the
        self._enc_t = []                     # wall_time per captured pair
        self._guide_step = None              # current step of the guided session (None = off)
        self._post_recording = False
        self._post_X, self._post_Y, self._post_Wq, self._post_Kq, self._post_t = [], [], [], [], []
        self._post_E, self._post_C, self._post_R = [], [], []
        self._post_file_list = []
        self._enc_W = self._enc_b = self._enc_mu = self._enc_sd = None
        self._encoder_model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "encoder_model.npz")
        self._fus_last_fingers = None        # last drawn fusion fingers (for the handoff cross-fade)
        self._fus_last_R = None
        self._fus_opt_last = None            # last OPTICAL pose (frozen on the camera-only panel)
        self._fus_prev_occ = True
        self._fus_xf_pose = None
        self._fus_xf_R = None
        self._fus_xf_t0 = 0.0
        self._fus_ema = None                 # EMA state for the predicted fingers (de-jitter)
        self._tcn = None                     # raw-EMG TCN (numpy inference) — beats the ridge
        self._tcn_model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "tcn_model.npz")
        self._hand_prior = None
        self._load_hand_prior()
        self._curl = None                    # option-1: EMG(+IMU) -> 5 per-finger curls
        self._vel = None                     # velocity decoder (Tracking tab): EMG+orient -> Δcurl/s
        self._penc = None                    # SEPARATE posture encoder (IMU+EMG → posture), own file
        self._penc_probs = None              # temporally-smoothed posture probabilities (de-jitter)
        self._penc_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.2)   # snappy on posture changes
        self._penc_prev_tl = None            # previous wrist tilt → freeze decision while rotating
        self._penc_cur = None                # current displayed posture idx (hysteresis, anti-flicker)
        self._penc_cand = None               # posture challenging the current one (must persist to win)
        self._penc_cand_t = 0.0              # seconds the challenger has led → switch only past HOLD_T
        self._penc_rec = False               # collecting camera-truth failure data (Posture Hand Record button)
        self._penc_rec_buf = None            # dict of capture lists while recording
        self._penc_rec_stat = None           # {total, correct, miss} → the live encoder-vs-camera failure readout
        self._ppose = None                   # SEPARATE continuous pose regressor (IMU+EMG → 5 curls), own file
        self._ppose_Wlive = None             # ONLINE-refined weights (NLMS from live optical ground truth)
        self._ppose_adapt_n = 0; self._ppose_err_ema = None
        self._ppose_adapt_n = 0; self._ppose_err_ema = None
        self._ppose_bufZ = self._ppose_bufT = self._ppose_bufW = None   # rehearsal buffer (anti-forgetting)
        self._ppose_bufn = 0
        self._ppose_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)   # smooth the continuous skeleton morph
        self._load_ppose()                   # loads the model + arms real-time refinement
        self._ppos_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._ppos_pivot = None              # forearm pivot (≈ elbow) in WORLD, learned while the camera sees the wrist
        self._ppos_last = None               # last head-local position shown (held if nothing better)
        self._ppos_last_w = None             # where the CAMERA last saw the wrist, in WORLD (what "held" holds)
        self._ppos_vel = (0.0, 0.0, 0.0)     # smoothed head-local velocity (radar whisker)
        self._ppos_src = None                # "camera" | "imu" | "held"
        self._ppos_lost_t = 0.0              # seconds since the camera last placed the hand
        self._pcap_rec = False               # recording a clip right now
        self._pcap_buf = None                # dict of per-frame lists while recording
        self._pcap_t0 = 0.0                  # wall time the current clip started
        self._pcap_fwd = None                # torso-forward (horizontal unit vector, world) — "Set forward"
        self._pcap_hmd = None                # latest (pos, R) of the headset, for the Set-forward button
        self._pcap_prevP = None; self._pcap_same = 0     # frozen-pose detector (Quest repeats a pose it can't see)
        self._pcap_prevP = None; self._pcap_same = 0     # frozen-pose detector (Quest repeats a pose it can't see)
        self._pcap_prev_w = None             # previous wrist (world) → speed read-out
        self._pcap_disk = None               # coverage already on disk: {"xyz": (N,3), "palm": (N,3)}
        self._pcap_files = []                # clip paths, same order as the tab's list
        self._plp_pm = None                  # the position_model module (features + inference shared with training)
        self._plp_model = None               # loaded model dict, or None until trained
        self._plp_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._plp_resid = None               # WORLD (camera − model) at the last camera fix → the fading anchor
        self._plp_last_w = None              # last camera-seen wrist, world (fallback hold when no model)
        self._plp_lost_t = 0.0               # seconds since the camera last placed the hand
        self._plp_err_ema = None             # running model error while the camera can check it (m)
        self._plp_last = None; self._plp_src = None; self._plp_vel = (0.0, 0.0, 0.0)
        self._plp_last = None; self._plp_src = None; self._plp_vel = (0.0, 0.0, 0.0)
        self._plp_last = None; self._plp_src = None; self._plp_vel = (0.0, 0.0, 0.0)
        self._plp_prevP = None; self._plp_same = 0       # frozen-pose detector (a repeated pose is not a camera fix)
        self._plp_prevP = None; self._plp_same = 0       # frozen-pose detector (a repeated pose is not a camera fix)
        self._pbv_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._pbv_fwd = None; self._pbv_hmd = None       # body 'forward' (horizontal, world) · last (hmd_p, hmd_R)
        self._pbv_fwd = None; self._pbv_hmd = None       # body 'forward' (horizontal, world) · last (hmd_p, hmd_R)
        self._pbv_resid = None; self._pbv_model_then = None; self._pbv_last_w = None
        self._pbv_resid = None; self._pbv_model_then = None; self._pbv_last_w = None
        self._pbv_resid = None; self._pbv_model_then = None; self._pbv_last_w = None
        self._pbv_lost_t = 0.0; self._pbv_err_ema = None; self._pbv_prevP = None; self._pbv_same = 0
        self._pbv_lost_t = 0.0; self._pbv_err_ema = None; self._pbv_prevP = None; self._pbv_same = 0
        self._pbv_lost_t = 0.0; self._pbv_err_ema = None; self._pbv_prevP = None; self._pbv_same = 0
        self._pbv_lost_t = 0.0; self._pbv_err_ema = None; self._pbv_prevP = None; self._pbv_same = 0
        self._pbv_msg = ("", 0.0)
        self._fk_mod = None; self._fk_cfg = {}; self._fk_cal_u = None; self._fk_cal_f = None
        self._fk_mod = None; self._fk_cfg = {}; self._fk_cal_u = None; self._fk_cal_f = None
        self._fk_mod = None; self._fk_cfg = {}; self._fk_cal_u = None; self._fk_cal_f = None
        self._fk_mod = None; self._fk_cfg = {}; self._fk_cal_u = None; self._fk_cal_f = None
        self._fk_fwd = None; self._fk_hmd = None; self._fk_u_held = None; self._fk_acc_hang = None
        self._fk_fwd = None; self._fk_hmd = None; self._fk_u_held = None; self._fk_acc_hang = None
        self._fk_fwd = None; self._fk_hmd = None; self._fk_u_held = None; self._fk_acc_hang = None
        self._fk_fwd = None; self._fk_hmd = None; self._fk_u_held = None; self._fk_acc_hang = None
        self._fk_L_held = None               # shoulder→elbow distance the camera implied at the last fix ('camera' source)
        self._fk_prevP = None; self._fk_same = 0; self._fk_pending = None; self._fk_msg = ("", 0.0)
        self._fk_prevP = None; self._fk_same = 0; self._fk_pending = None; self._fk_msg = ("", 0.0)
        self._fk_prevP = None; self._fk_same = 0; self._fk_pending = None; self._fk_msg = ("", 0.0)
        self._fk_prevP = None; self._fk_same = 0; self._fk_pending = None; self._fk_msg = ("", 0.0)
        self._fk_err_ema = None; self._fk_merr_ema = None; self._fk_ncmp = 0; self._fk_buf = []; self._fk_tickn = 0
        self._fk_err_ema = None; self._fk_merr_ema = None; self._fk_ncmp = 0; self._fk_buf = []; self._fk_tickn = 0
        self._fk_err_ema = None; self._fk_merr_ema = None; self._fk_ncmp = 0; self._fk_buf = []; self._fk_tickn = 0
        self._fk_err_ema = None; self._fk_merr_ema = None; self._fk_ncmp = 0; self._fk_buf = []; self._fk_tickn = 0
        self._fk_err_ema = None; self._fk_merr_ema = None; self._fk_ncmp = 0; self._fk_buf = []; self._fk_tickn = 0
        self._fk_log_f = None; self._fk_log_w = None; self._fk_log_n = 0; self._fk_log_path = None; self._fk_log_t0 = 0.0
        self._fk_log_f = None; self._fk_log_w = None; self._fk_log_n = 0; self._fk_log_path = None; self._fk_log_t0 = 0.0
        self._fk_log_f = None; self._fk_log_w = None; self._fk_log_n = 0; self._fk_log_path = None; self._fk_log_t0 = 0.0
        self._fk_log_f = None; self._fk_log_w = None; self._fk_log_n = 0; self._fk_log_path = None; self._fk_log_t0 = 0.0
        self._fk_log_f = None; self._fk_log_w = None; self._fk_log_n = 0; self._fk_log_path = None; self._fk_log_t0 = 0.0
        self._fk_last = {}
        self._pg = None; self._pg_Wlive = None; self._pg_shW = None
        self._pg = None; self._pg_Wlive = None; self._pg_shW = None
        self._pg = None; self._pg_Wlive = None; self._pg_shW = None
        self._pg_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._hyb_mod = None; self._hyb_nets = None; self._hyb_tracker = None; self._hyb_right = None
        self._hyb_mod = None; self._hyb_nets = None; self._hyb_tracker = None; self._hyb_right = None
        self._hyb_mod = None; self._hyb_nets = None; self._hyb_tracker = None; self._hyb_right = None
        self._hyb_mod = None; self._hyb_nets = None; self._hyb_tracker = None; self._hyb_right = None
        self._hyb_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._hyb_fwd = None; self._hyb_hmd = None; self._hyb_prevP = None; self._hyb_same = 0; self._hyb_msg = ("", 0.0)
        self._hyb_fwd = None; self._hyb_hmd = None; self._hyb_prevP = None; self._hyb_same = 0; self._hyb_msg = ("", 0.0)
        self._hyb_fwd = None; self._hyb_hmd = None; self._hyb_prevP = None; self._hyb_same = 0; self._hyb_msg = ("", 0.0)
        self._hyb_fwd = None; self._hyb_hmd = None; self._hyb_prevP = None; self._hyb_same = 0; self._hyb_msg = ("", 0.0)
        self._hyb_fwd = None; self._hyb_hmd = None; self._hyb_prevP = None; self._hyb_same = 0; self._hyb_msg = ("", 0.0)
        self._hyb_old_resid = None; self._hyb_old_then = None; self._hyb_freeze_w = None
        self._hyb_old_resid = None; self._hyb_old_then = None; self._hyb_freeze_w = None
        self._hyb_old_resid = None; self._hyb_old_then = None; self._hyb_freeze_w = None
        self._hyb_err = {}; self._hyb_nerr = 0
        self._hyb_err = {}; self._hyb_nerr = 0
        self._hyb_good_n = 0; self._hyb_handback = None; self._hyb_last_src = None; self._hyb_last_pos = None
        self._hyb_good_n = 0; self._hyb_handback = None; self._hyb_last_src = None; self._hyb_last_pos = None
        self._hyb_good_n = 0; self._hyb_handback = None; self._hyb_last_src = None; self._hyb_last_pos = None
        self._hyb_good_n = 0; self._hyb_handback = None; self._hyb_last_src = None; self._hyb_last_pos = None
        self._hyb_log_f = None; self._hyb_log_w = None; self._hyb_log_n = 0; self._hyb_log_path = None
        self._hyb_log_f = None; self._hyb_log_w = None; self._hyb_log_n = 0; self._hyb_log_path = None
        self._hyb_log_f = None; self._hyb_log_w = None; self._hyb_log_n = 0; self._hyb_log_path = None
        self._hyb_log_f = None; self._hyb_log_w = None; self._hyb_log_n = 0; self._hyb_log_path = None
        self._load_hyb()
        self._st_tracker = None; self._st_right = None; self._st_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._st_tracker = None; self._st_right = None; self._st_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._st_tracker = None; self._st_right = None; self._st_curl_filt = _OneEuroVec(mincutoff=2.0, beta=1.0)
        self._st_fwd = None; self._st_head_fwd = None; self._st_prevP = None; self._st_same = 0; self._st_msg = ("", 0.0)
        self._st_fwd = None; self._st_head_fwd = None; self._st_prevP = None; self._st_same = 0; self._st_msg = ("", 0.0)
        self._st_fwd = None; self._st_head_fwd = None; self._st_prevP = None; self._st_same = 0; self._st_msg = ("", 0.0)
        self._st_fwd = None; self._st_head_fwd = None; self._st_prevP = None; self._st_same = 0; self._st_msg = ("", 0.0)
        self._st_fwd = None; self._st_head_fwd = None; self._st_prevP = None; self._st_same = 0; self._st_msg = ("", 0.0)
        self._st_last_w = None; self._st_last_R = None; self._st_last_local = None
        self._st_last_w = None; self._st_last_R = None; self._st_last_local = None
        self._st_last_w = None; self._st_last_R = None; self._st_last_local = None
        self._st_mount_k = None; self._st_mount_w = None; self._st_bend_B = None; self._st_seen_t = None   # camera-taught mounts
        self._st_mount_k = None; self._st_mount_w = None; self._st_bend_B = None; self._st_seen_t = None   # camera-taught mounts
        self._st_mount_k = None; self._st_mount_w = None; self._st_bend_B = None; self._st_seen_t = None   # camera-taught mounts
        self._st_mount_k = None; self._st_mount_w = None; self._st_bend_B = None; self._st_seen_t = None   # camera-taught mounts
        self._kp_model = None; self._kp_gate = None; self._kp_templates = {}; self._kp_rec = None; self._kp_rec_t0 = 0.0
        self._kp_model = None; self._kp_gate = None; self._kp_templates = {}; self._kp_rec = None; self._kp_rec_t0 = 0.0
        self._kp_model = None; self._kp_gate = None; self._kp_templates = {}; self._kp_rec = None; self._kp_rec_t0 = 0.0
        self._kp_model = None; self._kp_gate = None; self._kp_templates = {}; self._kp_rec = None; self._kp_rec_t0 = 0.0
        self._kp_model = None; self._kp_gate = None; self._kp_templates = {}; self._kp_rec = None; self._kp_rec_t0 = 0.0
        self._kp_rec_label = ("fist", "front"); self._kp_clips_meta = []
        self._kp_rec_label = ("fist", "front"); self._kp_clips_meta = []
        if key_postures is not None:
            self._load_kp()
        self._vel_pose = None                # integrated held pose (5-curl), re-anchored by the camera
        self._vel_curl_filt = _OneEuroVec(mincutoff=1.5, beta=0.8)
        self._vel_prev_gt = None             # previous wrist gravity-tilt → rotation-rate gate
        self._vel_rec = False                # Tracking-tab session recorder (offline filter tuning)
        self._vel_anchor = None              # last camera-seen per-finger shape (for the openness rescue)
        self._vo_mu = self._vo_var = None    # running z-stats of the 8-ch log-RMS
        self._vo_open = self._vo_fist = None # running open/fist amplitude signatures (z-scored)
        self._vo_lo, self._vo_hi = 0.0, 1.0  # adaptive projection range (cancels limb-position bias)
        self._vo_g = 0.5                     # smoothed global openness (0=open, 1=fist)
        self._proc_hand = True               # render the anatomical hand when occluded
        self._fus_close = 0.0                 # smoothed global hand-closure 0..1 (option-2 fallback)
        self._fus_curl = None                 # smoothed per-finger curl 5-vector (option-1)
        self._fus_curl_filt = _OneEuroVec(mincutoff=1.5, beta=0.8)   # smooth+responsive curl
        self._diag_f = None                  # EMG-Hand diagnostic CSV handle (lazy)
        self._diag_last = 0.0                # throttle for the diagnostic log
        self._diag_rec_f = None              # per-gesture RECORDING diagnostic CSV handle
        self._last_poll_t = None             # perf_counter of the previous poll tick
        self._poll_dt = 1 / 30.0             # measured wall-clock tick period, fed to the fusers
        self._opt_R_filt = None              # speed-gated SLERP low-pass state (optical orient)
        self._opt_joints_filt = None         # matching EMA state for the 26 local joints
        self._opt_filt_t = None              # (reserved) last filtered-frame time
        self._anchor_R_opt_mat = None        # optical R_opt (matrix) at last CLEAR
        self._anchor_kq = None               # knuckle IMU quat at last CLEAR
        self._anchor_wq = None               # wrist IMU quat at last CLEAR
        self._W = None
        self._W_H = None                     # decaying accumulator Σ outer(ω_imu, ω_opt)
        self._W_prev_Ropt = None
        self._W_prev_kq = None
        self._gest_model_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "gesture_skel_model.npz")
        if self._gesture is not None and os.path.exists(self._gest_model_path):
            try:
                self._gesture.load(self._gest_model_path)
            except Exception:
                pass
        self._gate_last_bearing = [None, None]
        self._gate_prev_P = [None, None]
        self._gate_freeze = [0, 0]
        self._gloves_visible = True          # Gloves is the default (first) tab
        self._cap_gesture = None
        self._cap_file = None
        self._cap_writer = None
        self._cap_count = 0
        self._cap_target = None          # auto-stop seconds for a timed preset (rigid_hold)
        self._cap_start_wall = 0.0
        self._build_ui()
        if self.skeleton and self._config.get("skeleton_autostart",
                                              not self._skeleton_on_demand):
            self.skeleton.start()
            self._skeleton_running = True
        self._update_skeleton_btn()
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

    def _toggle_skeleton(self):
        """Start/stop the hand-skeleton source on demand (button)."""
        if self.skeleton is None:
            return
        if self._skeleton_running:
            self.skeleton.stop()
            self._skeleton_running = False
            self.log("Skeleton: stopped")
        else:
            # If "log joints" is on and this is the OpenXR backend, capture raw
            # Quest joints to a timestamped CSV (for fusion/run_gate_on_log.py).
            if (getattr(self, "skeleton_log_var", None) is not None
                    and self.skeleton_log_var.get()
                    and hasattr(self.skeleton, "log_path")):
                log_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "skeleton_logs")
                try:
                    os.makedirs(log_dir, exist_ok=True)
                    self.skeleton.log_path = os.path.join(
                        log_dir, f"quest_{time.strftime('%Y%m%d_%H%M%S')}.csv")
                except Exception as e:
                    self.log(f"Skeleton: cannot create log dir — {e!r}")
                    self.skeleton.log_path = None
            elif hasattr(self.skeleton, "log_path"):
                self.skeleton.log_path = None
            self.skeleton.start()
            self._skeleton_running = True
            self.log("Skeleton: started")
        self._update_skeleton_btn()

    def _update_skeleton_btn(self):
        btn = getattr(self, "skeleton_btn", None)
        if btn is None:
            return
        if self.skeleton is None:
            btn.configure(text="Skeleton: off", state=tk.DISABLED)
        elif self._skeleton_running:
            btn.configure(text="◼ Stop skeleton", state=tk.NORMAL)
        else:
            btn.configure(text="▶ Start skeleton", state=tk.NORMAL)

    def _toggle_preview(self):
        if self._preview_running:
            self._preview_stop()
        else:
            self._preview_start()

    def _preview_start(self):
        """Show the live Quest optical hand skeleton in the panels — no world, no
        logging. Its own OpenXR source, so the default backend is untouched."""
        if not HAS_OPENXR or OpenXRHandSkeletonSource is None:
            self.log("Live Preview needs OpenXR:  pip install pyopenxr glfw PyOpenGL")
            return
        if self._occ_running:
            self.log("Live Preview: stop the Occlusion Test first "
                     "(both need the headset).")
            return
        # A second OpenXR session can't run beside the default one; free it.
        if self._skeleton_running and isinstance(self.skeleton, OpenXRHandSkeletonSource):
            self.skeleton.stop()
            self._skeleton_running = False
            self._update_skeleton_btn()
        self._preview_skeleton = OpenXRHandSkeletonSource(log=self.log)
        self._preview_skeleton.start()          # no log_path → preview only
        self._preview_running = True
        self.log("Live Preview: started (OpenXR optical skeleton, no world)")
        self._update_preview_btn()

    def _preview_stop(self):
        if self._preview_skeleton is not None:
            self._preview_skeleton.stop()
            self._preview_skeleton = None
        self._preview_running = False
        self.log("Live Preview: stopped")
        self._update_preview_btn()

    def _update_preview_btn(self):
        btn = getattr(self, "preview_btn", None)
        if btn is None:
            return
        btn.configure(text="◼ Stop Preview" if self._preview_running else "◉ Live Preview")

    def _update_gate_tab(self, src):
        """Classify per-hand occlusion TYPE from the live source (real gate when
        world joints + HMD pose are available, else geometry-only blind-spot),
        log event changes (edge-triggered, wall_time-stamped), and repaint the
        gate view when it's visible."""
        gt = getattr(self, "gate_tab", None)
        if gt is None:
            return
        if src is None:
            self._gate_prev_event = None          # so the first real event logs
            self._gate_last_type = ["", ""]
            if gt.visible:
                idle = {"present": False, "az": 0.0, "el": 0.0, "dist": 0.0,
                        "type": "OUT_OF_VIEW", "conf": 0.0}
                gt.draw(idle, idle, "start ◉ Live Preview or ▶ Occlusion Test")
            return
        hmd = getattr(src, "hmd", None)
        if HAS_GATE and hmd and hmd.get("valid"):
            vsL, vsR = self._gate_v2(src, hmd)    # real per-joint occlusion types
        else:                                     # geometry-only fallback (FOV / blind-spot)
            vsL = _fov_status(src.pose_info[0])
            vsR = _fov_status(src.pose_info[1])
        # Remember the per-hand types so a synced clip row can label each frame
        # with the gate's verdict (the fusion switch signal, per hand).
        self._gate_last_type = [vsL["type"], vsR["type"]]
        # Edge-triggered logging on the per-hand (LEFT, RIGHT) state pair, so both
        # hands' states are captured even when they differ.
        key = (vsL["type"], vsR["type"])
        if key != self._gate_prev_event:
            self._gate_prev_event = key
            label = f"L:{vsL['type']} R:{vsR['type']}"
            self.log(f"[gate] {label}")
            self._gate_log_event(label)
        if gt.visible:
            gt.draw(vsL, vsR, src.status)

    def _well_inside_box(self, az, el, margin=8.0):    # GATE_BOX_MARGIN
        """True if a bearing is inside the calibrated FOV box by a margin — used to
        judge that a dropped hand vanished mid-view (object) vs at the edge."""
        return (abs(az) < self._fov_az - margin
                and el > self._fov_el_lo + margin
                and el < self._fov_el_hi - margin)

    def _gate_v2(self, src, hmd):
        """Classify each hand into TRACKED / OUT_OF_VIEW / SELF / OTHER / OBJECT
        from world joints + HMD pose. Returns per-hand view-status
        {present, az, el, dist, type, conf, joints_azel}."""
        cam_pos = _np.asarray(hmd["pos"], float)
        q = hmd["quat"]
        cam_R = _quat_to_R(q[0], q[1], q[2], q[3])
        now = time.perf_counter()
        dt = (now - self._gate_last_t) if self._gate_last_t else 1.0 / 30.0
        self._gate_last_t = now
        dt = min(max(dt, 1e-3), 0.2)

        # current positions + which hands are usably tracked (finite, sane range)
        P = [None, None]
        usable = [False, False]
        for h in (0, 1):
            wj = src.world_joints[h] if src.world_joints else None
            if wj is None:
                continue
            p = _np.asarray(wj, float)
            d = float(_np.linalg.norm(p[1] - cam_pos))     # wrist distance
            if bool(_np.all(_np.isfinite(p))) and 0.1 < d < 2.0:
                P[h], usable[h] = p, True

        vs = [None, None]
        for h in (0, 1):
            other = 1 - h
            active = bool(src.active[h]) if src.active else True
            if not usable[h]:
                # No position data at all. Decide from where it vanished: still
                # OCCLUDED-BY-OTHER if the other hand now covers that spot; else
                # OBJECT if it vanished well inside the FOV; else OUT OF VIEW.
                lb = self._gate_last_bearing[h]
                if lb is not None and usable[other] and _bearing_covered_by(
                        lb[0], lb[1], P[other], cam_pos, cam_R):
                    t = "OTHER"
                elif lb is not None and self._well_inside_box(lb[0], lb[1]):
                    t = "OBJECT"
                else:
                    t = "OUT_OF_VIEW"
                vs[h] = {"present": False, "az": 0.0, "el": 0.0, "dist": 0.0,
                         "type": t, "conf": 0.0}
                self._gate_freeze[h] = 0
                self._gate_prev_P[h] = None
                continue
            # We have positions (possibly inferred while is_active=0) — run the
            # occlusion cues regardless of is_active.
            az, el, dist = _bearing_cam(P[h][1], cam_pos, cam_R)
            fwd = -float(((P[h][1] - cam_pos) @ cam_R)[2])
            out_of_box = ((fwd <= 0.0) or (abs(az) > self._fov_az)
                          or (el < self._fov_el_lo) or (el > self._fov_el_hi))
            occ_other = (_interhand_occ(P[h], P[other], cam_pos, cam_R)
                         if usable[other] else 0.0)
            occ_self = _self_occlusion(P[h])               # fraction of fingers curled
            # freeze: Quest repeats the EXACT world pose when it can't see the hand.
            pv = self._gate_prev_P[h]
            if pv is not None and _np.allclose(P[h], pv, atol=1e-6):
                self._gate_freeze[h] += 1
            else:
                self._gate_freeze[h] = 0
            self._gate_prev_P[h] = P[h]
            frozen = self._gate_freeze[h] >= GATE_FREEZE_N
            type_ = classify_occlusion(active, out_of_box, occ_self, occ_other,
                                       frozen, self._well_inside_box(az, el))
            if active and not out_of_box:
                # remember where a confidently-tracked in-box hand is, to tell a
                # later full drop apart (object mid-view vs out-of-view at edge).
                self._gate_last_bearing[h] = (az, el)
                self._fov_samples.append((az, el))         # tracking envelope for calibration
                if len(self._fov_samples) > 6000:
                    self._fov_samples = self._fov_samples[-4000:]
            # all 26 joint bearings → the Gate view draws the full hand skeleton
            qj = (P[h] - cam_pos) @ cam_R
            fwdj = _np.maximum(-qj[:, 2], 1e-6)
            azj = _np.degrees(_np.arctan2(qj[:, 0], fwdj))
            elj = _np.degrees(_np.arctan2(qj[:, 1], fwdj))
            vs[h] = {"present": True, "az": az, "el": el, "dist": dist, "type": type_,
                     "conf": float(max(0.0, 1.0 - max(occ_self, occ_other))),
                     "joints_azel": list(zip(azj.tolist(), elj.tolist()))}
        return vs[0], vs[1]

    def _emg_cols(self):
        """Armband columns: 8-ch EMG RMS (over the dashboard window) + armband
        accel/gyro (latest) + present. Zeros when the armband isn't streaming."""
        arm = getattr(self, "armband", None)
        dash = getattr(arm, "dash", None) if arm is not None else None
        if dash is None or not getattr(arm, "running", False):
            return [0.0] * 8 + [0.0, 0.0, 0.0, 0.0, 0.0, 0.0] + [0]
        try:
            be = dash.buf_emg
            rms = [round(float((be[c] ** 2).mean() ** 0.5), 4)
                   for c in range(min(8, be.shape[0]))]
            rms += [0.0] * (8 - len(rms))
            acc = [round(float(dash.buf_acc[k][-1]), 4) for k in range(3)]
            gyr = [round(float(dash.buf_gyr[k][-1]), 4) for k in range(3)]
            return rms + acc + gyr + [1]
        except Exception:
            return [0.0] * 8 + [0.0, 0.0, 0.0, 0.0, 0.0, 0.0] + [0]

    def _glove_slot_quat(self, state, slot):
        """Glove quaternion for a slot name (body1|body2|joint), or None if that
        IMU isn't present on the hand."""
        m = {"body1": (IMU_BODY_PRIMARY, "quat"),
             "body2": (IMU_BODY_SECONDARY, "quat_body2"),
             "joint": (IMU_JOINT, "quat_joint")}.get(slot)
        if state is None or m is None:
            return None
        bit, attr = m
        if not (getattr(state, "imu_present", 0) & bit):
            return None
        return getattr(state, attr, None)

    def _optical_local(self, wj, right):
        """26-joint optical hand in ITS OWN frame R_opt (wrist-relative, de-rotated),
        plus R_opt. R_off maps the IMU into R_opt, so R_off·R_imu·P_local reproduces the
        true world orientation. Returns (list-of-(x,y,z), R_opt) or (None, None)."""
        if not HAS_FUSION or wj is None:
            return None, None
        try:
            P = _np.asarray([[float(a), float(b), float(c)] for a, b, c in wj], float)
            R_opt = _fus_hand_frame(P, right)
            if _np.linalg.det(R_opt) < 0.99:
                return None, None
            wrist = P[1]
            local = [tuple(float(x) for x in (R_opt.T @ (P[j] - wrist))) for j in range(26)]
            return local, R_opt
        except Exception:
            return None, None

    def _ppose_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "posture_pose_model.npz")

    def _load_ppose(self):
        try:
            p = self._ppose_path()
            self._ppose = dict(_np.load(p, allow_pickle=True)) if os.path.exists(p) else None
            if self._ppose is not None:
                self.log("Pose Hand: continuous pose regressor loaded (posture_pose_model.npz)")
        except Exception as e:
            self._ppose = None
            self.log(f"Pose regressor load failed — {e!r}")
        self._ppose_reset_online()

    def _ppose_reset_online(self):
        """(Re)start online refinement from the loaded model as the prior (↺ Reset / after a batch retrain)."""
        M = getattr(self, "_ppose", None)
        self._ppose_Wlive = (_np.asarray(M["W"], float).copy() if M is not None else None)
        self._ppose_adapt_n = 0; self._ppose_err_ema = None
        d = self._ppose_Wlive.shape[0] if self._ppose_Wlive is not None else 44
        C = 300                                                   # rehearsal-buffer capacity
        self._ppose_bufZ = _np.zeros((C, d)); self._ppose_bufT = _np.zeros((C, 5)); self._ppose_bufW = _np.zeros((C, 5))
        self._ppose_bufn = 0
        pt = getattr(self, "ppose_tab", None)
        if pt is not None:
            pt.set_learn_status("online learning armed — show your hand to the camera to refine")

    def _ppose_buf_add(self, z, t, w):
        """Rehearsal buffer with DIVERSITY eviction: a new sample replaces its NEAREST neighbour, so eviction
        happens inside the dense (common-posture) region and RARE postures (e.g. a point shown briefly) are kept."""
        n = self._ppose_bufn; C = len(self._ppose_bufZ)
        if n < C:
            i = n; self._ppose_bufn = n + 1
        else:
            i = int(((self._ppose_bufZ - z) ** 2).sum(1).argmin())
        self._ppose_bufZ[i] = z; self._ppose_bufT[i] = t; self._ppose_bufW[i] = w

    def _ppose_replay_step(self, z, t, w, K=8):
        """One masked-NLMS step over a mini-batch = current sample + K replayed buffer samples. Old postures are
        replayed every frame, so the model keeps fitting them → no catastrophic forgetting of e.g. a point."""
        G = _np.outer(z, w * (t - z @ self._ppose_Wlive)) / (float(z @ z) + 1e-3); cnt = 1
        k = min(K, self._ppose_bufn)
        if k > 0:
            for i in _np.random.randint(0, self._ppose_bufn, k):
                zi = self._ppose_bufZ[i]; ei = self._ppose_bufW[i] * (self._ppose_bufT[i] - zi @ self._ppose_Wlive)
                G = G + _np.outer(zi, ei) / (float(zi @ zi) + 1e-3)
            cnt += k
        self._ppose_Wlive = self._ppose_Wlive + (0.4 / cnt) * G
        _np.clip(self._ppose_Wlive, -60, 60, out=self._ppose_Wlive)

    def _ppose_save_refined(self):
        """Bake the live-refined weights into the model file so the refinement persists across restarts."""
        M = getattr(self, "_ppose", None); pt = getattr(self, "ppose_tab", None)
        if M is None or self._ppose_Wlive is None:
            return
        try:
            _np.savez(self._ppose_path(), W=self._ppose_Wlive.astype(_np.float32), mu=M["mu"], sd=M["sd"],
                      names=M["names"], fist_prior=M["fist_prior"], kind=M["kind"])
            n = self._ppose_adapt_n; self._load_ppose()
            if pt: pt.set_learn_status(f"saved — {n} in-view frames of refinement baked into the model")
            self.log(f"Pose regressor: refined weights saved ({n} frames)")
        except Exception as e:
            if pt: pt.set_learn_status(f"save failed — {e!r}")

    def _ppose_finger_vis(self, src, wj, gate):
        """Per-finger camera confidence (T1.2 ray-cast): which fingers the Quest actually SEES right now. A point
        self-occludes its curled fingers but its extended index stays fully visible → lets each finger learn only
        when it is genuinely seen. Returns a 5-vector [thumb,index,mid,ring,pinky] in [0,1], or None."""
        if _occl is None or wj is None:
            return None
        hmd = getattr(src, "hmd", None)
        if not hmd or not hmd.get("valid"):
            return None
        try:
            P = _np.asarray([[float(a), float(b), float(c)] for a, b, c in wj], float)
            cam = _np.asarray(hmd["pos"], float)
            vis = _occl.per_joint_visibility(P, cam)               # geometric self-occlusion
            q = hmd["quat"]; cam_R = _quat_to_R(q[0], q[1], q[2], q[3]); m = 10.0
            for j in range(2, 26):                                 # field-of-view: off-frame joints aren't seen
                qc = (P[j] - cam) @ cam_R; fwd = -float(qc[2])
                if fwd <= 0.02:
                    vis[j] = 0.0; continue
                az = math.degrees(math.atan2(float(qc[0]), fwd)); el = math.degrees(math.atan2(float(qc[1]), fwd))
                fx = max(0.0, min(1.0, (self._fov_az - abs(az)) / m))
                fy = max(0.0, min(1.0, (el - self._fov_el_lo) / m)) * max(0.0, min(1.0, (self._fov_el_hi - el) / m))
                vis[j] *= fx * fy
            if gate == "OUT_OF_VIEW":                              # no live optical at all → trust nothing
                vis[:] = 0.0
            elif gate in ("OBJECT", "OTHER"):
                vis *= 0.35
            return _np.asarray(_occl.finger_confidence(vis), float)
        except Exception:
            return None

    def _load_hyb(self):
        try:
            import hybrid_position as _hp
            self._hyb_mod = _hp
            p = _hp.default_weights_path()
            self._hyb_nets = _hp.load_weights(p) if os.path.exists(p) else None
            if self._hyb_nets is not None:
                self.log("Hybrid Position: weights loaded (hybrid_position_weights.npz)")
        except Exception as e:
            self._hyb_nets = None
            self.log(f"Hybrid Position: load failed — {e!r}")
        self._hyb_tracker = None

    def _hyb_model_info(self):
        N = self._hyb_nets
        if N is None:
            return "no weights — run  python train_hybrid_position.py  (needs PyTorch) to create hybrid_position_weights.npz"
        try:
            me = N.get("meta", {}); a = _np.asarray(me.get("lab_score_cm", []), float); b = _np.asarray(me.get("lab_current_cm", []), float)
            txt = f"nets trained on {int(me.get('n_frames', 0))} camera frames · {int(me.get('n_clips', 0))} clips"
            if len(a) == 5 and len(b) == 5:
                txt += (f"\nlab test on unseen clips, head turned away — error 1 s / 4 s after the loss:\n"
                        f"  this method {a[1]:.0f} / {a[3]:.0f} cm   ·   current method {b[1]:.0f} / {b[3]:.0f} cm")
            return txt
        except Exception:
            return "weights loaded"

    def _st_set_forward(self):
        pt = getattr(self, "st_tab", None)
        if self._st_head_fwd is None:
            self._st_msg = ("no headset pose yet — press ◉ Start preview (top bar)", time.time() + 4.0); return
        self._st_fwd = self._st_head_fwd.copy()
        if pt is not None:
            pt.set_follow(False)
        self._st_msg = ("body heading locked to the way you face now — tick 'follows the headset' to release", time.time() + 4.0)

    def _st_reset_learning(self):
        self._ppose_reset_online()                            # the online-refined pose model is shared with Pose Hand
        pt = getattr(self, "st_tab", None)
        if pt is not None:
            pt.set_learn_status("learning reset to the trained model — show your hand to the camera to refine")

    def _st_save_refined(self):
        n = self._ppose_adapt_n
        self._ppose_save_refined()                            # bakes the refinement into posture_pose_model.npz (shared)
        pt = getattr(self, "st_tab", None)
        if pt is not None:
            pt.set_learn_status(f"saved — {n} in-view frames of refinement baked into the Pose Hand model")

    @staticmethod
    def _st_feasible(hand_R, f_fore, trust_hand, max_deg=70.0, reject_deg=110.0):
        """A wrist cannot bend the hand more than ~70° away from the forearm. Returns (hand_R, f_fore, note).
        trust_hand=True (camera): the FOREARM axis is swung toward the hand. Otherwise (IMU hand): the hand is swung
        toward the forearm, keeping its twist; beyond reject_deg the hand sensor is not believed at all."""
        if hand_R is None or f_fore is None:
            return hand_R, f_fore, ""
        hx = hand_R[:, 0]; f = f_fore / (float(_np.linalg.norm(f_fore)) or 1.0)
        th = math.degrees(math.acos(max(-1.0, min(1.0, float(hx @ f)))))
        if th <= max_deg:
            return hand_R, f, ""
        n = _np.cross(hx, f); nn = float(_np.linalg.norm(n))
        if nn < 1e-6:
            return hand_R, f, ""
        n /= nn
        def rot(axis, deg):
            a = math.radians(deg); K = _np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
            return _np.eye(3) + math.sin(a) * K + (1.0 - math.cos(a)) * K @ K
        if trust_hand:                                        # camera hand is right → the forearm drawing follows it
            return hand_R, rot(-n, th - max_deg) @ f, f"forearm axis moved {th - max_deg:.0f}° to respect the wrist limit"
        if th > reject_deg:                                   # a hand sensor pointing backwards is not a wrist bend
            R = hand_R.copy(); R = rot(n, th) @ R             # swing the hand fully onto the forearm (twist kept)
            return R, f, f"hand sensor disagreed with the forearm by {th:.0f}° → ignored (hand aligned to the forearm)"
        return rot(n, th - max_deg) @ hand_R, f, f"wrist bend limited to {max_deg:.0f}° (sensor said {th:.0f}°)"

    def _st_tick(self, src):
        pt = getattr(self, "st_tab", None)
        if pt is None or not pt.visible:
            return
        now = time.time(); h = self._gest_hand; right = (h == 1); F = pt.flags(); hp_ = self._hyb_mod
        msg = self._st_msg[0] if now < self._st_msg[1] else ""
        hmd = getattr(src, "hmd", None) if src is not None else None
        hp = hR = None
        if hmd and hmd.get("valid"):
            try:
                q = [float(v) for v in hmd["quat"]][:4]; hp = _np.asarray(hmd["pos"], float)
                hR = _np.asarray(_quat_to_R(q[0], q[1], q[2], q[3]), float)
            except Exception:
                hp = hR = None
        chips = {}
        if hR is None:
            chips["headset"] = (COLOR_RED, "no pose")
            pt.draw(None, msg or "needs the headset pose — press ◉ Start preview (top bar)", chips)
            return
        chips["headset"] = (COLOR_GREEN, "live")
        head_f = -hR[:, 2]; hf = _np.array([head_f[0], 0.0, head_f[2]]); n = float(_np.linalg.norm(hf))
        if n > 0.2:
            hf = hf / n; self._st_head_fwd = hf
            if self._st_fwd is None:
                self._st_fwd = hf.copy()
            elif F["follow"]:                                       # the body follows the head slowly (τ 2.5 s)
                a = 1.0 - math.exp(-max(self._poll_dt, 1e-3) / 2.5)
                v = self._st_fwd + a * (hf - self._st_fwd); nn = float(_np.linalg.norm(v))
                self._st_fwd = v / nn if nn > 1e-6 else hf.copy()
        if self._st_fwd is None:
            pt.draw(None, "look straight ahead once so the body heading can be set", chips); return
        fwd = self._st_fwd
        f_b = _np.array([fwd[0], 0.0, fwd[2]]); f_b /= (float(_np.linalg.norm(f_b)) or 1.0)
        up = _np.array([0.0, 1.0, 0.0]); r_b = _np.cross(f_b, up); r_b /= (float(_np.linalg.norm(r_b)) or 1.0)
        neck = hp + hR @ _np.array([0.0, -0.10, 0.08])
        # ── camera ──
        wj = (src.world_joints[h] if (F["optical"] and src is not None and getattr(src, "world_joints", None)) else None)
        local, R_opt = self._optical_local(wj, right) if wj is not None else (None, None)
        P_world = None
        if wj is not None:
            try:
                P_world = _np.asarray([[float(a), float(b), float(c_)] for a, b, c_ in wj], float)
            except Exception:
                P_world = None
        gate_raw = self._gate_last_type[h] if self._gate_last_type else ""
        gate = gate_raw if F["gate"] else ("CLEAR" if P_world is not None else "OUT_OF_VIEW")
        gate_clear = (gate == "CLEAR") or (F["gate"] and gate in self._gate_enabled and not self._gate_enabled[gate])
        if P_world is not None and self._st_prevP is not None and float(_np.abs(P_world - self._st_prevP).max()) < 1e-7:
            self._st_same += 1
        else:
            self._st_same = 0
        self._st_prevP = P_world
        frozen = self._st_same >= 2
        cam_ok = bool(P_world is not None and (not F["gate"] or not frozen) and gate not in ("OUT_OF_VIEW", "OBJECT", "OTHER"))
        chips["optical"] = ((COLOR_FG_DIM, "off") if not F["optical"] else (COLOR_GREEN, "live") if P_world is not None else (COLOR_RED, "no hand"))
        # ── IMUs through the shared fusers (same calls the other tabs make) ──
        st = (self.ble.right if h == 1 else self.ble.left) if getattr(self, "ble", None) else None
        wq = self._glove_slot_quat(st, self._config.get("imu_wrist_slot") or "body2") if F["wrist"] else None
        kq = self._glove_slot_quat(st, self._config.get("imu_hand_slot") or "joint") if F["knuckle"] else None
        wq = _np.asarray([float(x) for x in wq], float) if wq is not None else None
        kq = _np.asarray([float(x) for x in kq], float) if kq is not None else None
        seen_f = bool(cam_ok and gate_clear and wj is not None)
        forearm_R = qoff = wrist_raw_R = knuckle_R = None
        if self._fuser_wrist is not None and wq is not None and _fus_quat_to_R is not None:
            try:
                qw_ = self._fuser_wrist.update("CLEAR" if seen_f else "HOLD", P_world if seen_f else None, wq, dt=self._poll_dt)
                wrist_raw_R = _np.asarray(_fus_quat_to_R(list(wq)), float)
                if self._fuser_wrist._have_off:
                    forearm_R = _np.asarray(_fus_quat_to_R(list(qw_)), float); qoff = _np.asarray(self._fuser_wrist.q_off, float).copy()
            except Exception:
                pass
        if self._fuser_knuckle is not None and kq is not None and _fus_quat_to_R is not None:
            try:
                qk_ = self._fuser_knuckle.update("CLEAR" if seen_f else "HOLD", P_world if seen_f else None, kq, dt=self._poll_dt)
                if self._fuser_knuckle._have_off:
                    knuckle_R = _np.asarray(_fus_quat_to_R(list(qk_)), float)
            except Exception:
                pass
        chips["wrist"] = ((COLOR_FG_DIM, "off") if not F["wrist"] else (COLOR_RED, "missing") if wq is None else (COLOR_GREEN, "calibrated") if qoff is not None else (COLOR_ORANGE, "uncalibrated"))
        chips["knuckle"] = ((COLOR_FG_DIM, "off") if not F["knuckle"] else (COLOR_RED, "missing") if kq is None else (COLOR_GREEN, "calibrated") if knuckle_R is not None else (COLOR_ORANGE, "uncalibrated"))
        # ── armband ──
        ec = self._emg_cols(); band = bool(ec[14]); emg_on = bool(F["emg"] and band)
        rms = [float(v) for v in ec[0:8]] if emg_on else None
        acc = _np.asarray(ec[8:11], float) if (band and float(_np.linalg.norm(ec[8:11])) > 0.3) else None
        arm = getattr(self, "armband", None); dash = getattr(arm, "dash", None) if arm is not None else None
        feat_emg = (emg_features(getattr(dash, "buf_emg", None))
                    if (emg_on and dash is not None and getattr(arm, "running", False) and emg_features is not None) else None)
        chips["emg"] = ((COLOR_FG_DIM, "off") if not F["emg"] else (COLOR_GREEN, "live") if band else (COLOR_RED, "not streaming"))
        # ── position: own hybrid tracker (shares only the trained nets) ──
        if hp_ is not None and self._hyb_nets is not None and (self._st_tracker is None or self._st_right != right):
            self._st_tracker = hp_.HybridTracker(self._hyb_nets, right=right); self._st_right = right
        out = None
        if self._st_tracker is not None and wq is not None:
            self._st_tracker.smooth = bool(F["smooth"])
            try:
                out = self._st_tracker.update(now, wq, qoff if cam_ok else None, hp, hR, fwd, P_world[1] if cam_ok else None, acc)
            except Exception as e:
                out = None; self._st_msg = (f"tracker error — {e!r}", now + 3.0)
        chips["pos"] = ((COLOR_FG_DIM, "off") if not F["pos"] else (COLOR_GREEN, "ready") if self._hyb_nets is not None else (COLOR_RED, "no weights"))
        why = ""; wrist = None; src_pos = None
        lost_reason = ("optical off" if not F["optical"] else
                       {"OUT_OF_VIEW": "hand out of view", "OBJECT": "hand behind an object", "OTHER": "hand behind something",
                        "SELF": "self-occluded"}.get(gate, "frozen camera pose" if frozen else "no hand from the camera"))
        if cam_ok:
            wrist = P_world[1].copy(); src_pos = "camera"; self._st_last_w = wrist.copy()
        elif F["pos"] and out is not None and out.get("src") == "hybrid":
            wrist = _np.asarray(out["pos"], float); src_pos = "hybrid"; why = lost_reason
        elif self._st_last_w is not None:
            wrist = self._st_last_w; src_pos = "hold"
            why = lost_reason + " · " + ("position model switched off" if not F["pos"] else "wrist IMU off — nothing can predict" if wq is None
                                         else "no position weights" if self._hyb_nets is None else "waiting for the model")
        # ── sensor mountings taught by the camera (mount_calib): two-sided  R = Ryaw(α)·C·R_imu·M  per sensor ──
        # The bridge's fuser holds a ONE-sided offset, exact only at the pose where it was set: on the recordings the
        # knuckle sensor then wanders 30–50° within 3 s of losing the camera (the 'hand folded back' picture). With the
        # fixed mounting M learned while the camera sees the hand, the same sensor stays within ~9° (median) of the
        # camera hand frame for 30 s and more — so the wrist BEND can stay live out of view. The wrist sensor fitted
        # the same way gives the forearm frame (column 0 = forearm axis), which also lets the bend be drawn at all.
        mk = mw = None
        if mount_calib is not None:
            if self._st_mount_k is None:
                self._st_mount_k = mount_calib.MountCalib(); self._st_mount_w = mount_calib.MountCalib()
            mk, mw = self._st_mount_k, self._st_mount_w
        Rk_raw = _np.asarray(mount_calib.quat_wxyz_to_R(kq), float) if (mk is not None and kq is not None) else None
        Rw_raw = _np.asarray(mount_calib.quat_wxyz_to_R(wq), float) if (mw is not None and wq is not None) else None
        if cam_ok and R_opt is not None:
            Ro_ = _np.asarray(R_opt, float); self._st_seen_t = now; g_ = 1.0 - math.exp(-max(self._poll_dt, 1e-3) / 2.0)
            for m_, Rr_ in ((mk, Rk_raw), (mw, Rw_raw)):
                if m_ is not None and Rr_ is not None:
                    m_.add(now, Rr_, Ro_); m_.solve(now); m_.nudge(Rr_, Ro_, g_)
        hand_k = mk.estimate(Rk_raw) if (mk is not None and mk.ready and Rk_raw is not None) else None
        fore_F = mw.estimate(Rw_raw) if (mw is not None and mw.ready and Rw_raw is not None) else None
        k_good = bool(hand_k is not None and mk.resid is not None and mk.resid < 25.0)
        if cam_ok and R_opt is not None and fore_F is not None:
            self._st_bend_B = fore_F.T @ _np.asarray(R_opt, float)        # wrist bend at the last camera fix, forearm frame
        # ── orientation of the hand: camera > knuckle IMU (live bend, if its mount is trusted) > wrist IMU (bend held) ──
        bend_note = ""; hand_R = None; src_R = "—"
        if cam_ok and R_opt is not None:
            hand_R = _np.asarray(R_opt, float); src_R = "camera"
        k_bend = (math.degrees(math.acos(max(-1.0, min(1.0, float(hand_k[:, 0] @ fore_F[:, 0])))))
                  if (hand_k is not None and fore_F is not None) else None)
        if cam_ok and R_opt is not None:
            pass
        elif F["oov"] == "knuckle" and k_good and (k_bend is None or k_bend <= 100.0):
            hand_R = hand_k; src_R = "knuckle IMU"; bend_note = f"live (mount {mk.fit_deg:.0f}°, residual {mk.resid:.0f}°)"
        elif fore_F is not None and self._st_bend_B is not None:
            hand_R = fore_F @ self._st_bend_B; src_R = "wrist IMU"
            bend_note = "held" + ((" — knuckle " + (f"sensor disagreed with the forearm by {k_bend:.0f}°" if (k_good and k_bend is not None and k_bend > 100.0)
                                                  else "mount not trusted (residual %.0f°)" % mk.resid if (mk is not None and mk.ready and mk.resid is not None)
                                                  else "mount not learned yet")) if F["oov"] == "knuckle" else "")
        elif forearm_R is not None:
            hand_R = forearm_R; src_R = "wrist IMU"
        elif knuckle_R is not None:
            hand_R = knuckle_R; src_R = "knuckle IMU"
        elif self._st_last_R is not None:
            hand_R = self._st_last_R; src_R = "held"
        elif wrist_raw_R is not None:
            hand_R = wrist_raw_R; src_R = "wrist IMU (uncalibrated)"
        else:
            hand_R = None; src_R = "—"
        f_fore = (fore_F[:, 0] if fore_F is not None else forearm_R[:, 0] if forearm_R is not None
                  else (hand_R[:, 0] if hand_R is not None else None))
        hand_R, f_fore, feas_note = self._st_feasible(hand_R, f_fore, trust_hand=(src_R == "camera"), max_deg=(80.0 if fore_F is not None else 70.0))
        if hand_R is not None and f_fore is not None and fore_F is not None:
            bend_deg = math.degrees(math.acos(max(-1.0, min(1.0, float(hand_R[:, 0] @ f_fore)))))
            src_R += f", wrist bend {bend_deg:.0f}°" + (" " + bend_note if bend_note else "")
        elif mw is not None and src_R != "—":
            src_R += ", bend: " + (mw.status() if wq is not None else "wrist IMU missing")
        if feas_note:
            src_R = src_R + " · " + feas_note
        if hand_R is not None and not src_R.startswith("held"):
            self._st_last_R = hand_R
        LF = hp_.LF if hp_ is not None else 0.26
        elbow = (wrist - LF * f_fore) if (wrist is not None and f_fore is not None) else None
        # ── posture: camera per finger where seen, EMG regressor (Pose Hand model, read-only) elsewhere ──
        curls = None; emg_local = None; feat43 = None
        fc = self._ppose_finger_vis(src, wj, gate) if (wj is not None and F["optical"]) else None
        learn_state = ("off", COLOR_FG_DIM) if not F["learn"] else ("paused", COLOR_ORANGE)
        if (feat_emg is not None and wq is not None and self._ppose is not None and self._ppose_Wlive is not None
                and _fus_quat_to_R is not None and self._hand_prior is not None):
            try:
                tl = _np.asarray(_fus_quat_to_R(list(wq)), float).T @ _np.array([0., -1., 0.])   # the feature that model was trained with
                feat43 = _np.concatenate([_np.asarray(feat_emg, float), tl])
                z = _np.concatenate([((feat43 - self._ppose["mu"]) / self._ppose["sd"]), [1.0]])
                # ── CONTINUAL LEARNING (identical to Pose Hand, shared model + rehearsal memory): while the camera sees a
                #    finger, that finger's curl is the free label; hidden fingers are not taught; old postures are replayed ──
                if F["learn"] and cam_ok and local is not None and fc is not None:
                    target = _np.asarray(self._finger_flex(local), float)
                    wfing = _np.clip((_np.asarray(fc, float) - 0.5) / 0.4, 0.0, 1.0)
                    if float(wfing.sum()) > 0.05:
                        self._ppose_buf_add(z, target, wfing)
                        self._ppose_replay_step(z, target, wfing)
                        self._ppose_adapt_n += 1
                        e = float((wfing * _np.abs(target - _np.clip(z @ self._ppose_Wlive, 0, 1))).sum() / (float(wfing.sum()) + 1e-6))
                        self._ppose_err_ema = e if self._ppose_err_ema is None else 0.94 * self._ppose_err_ema + 0.06 * e
                        seen_f_ = "".join(ch for ch, wv in zip("TIMRP", wfing) if wv > 0.5) or "—"
                        learn_state = ("learning", COLOR_GREEN)
                        if self._ppose_adapt_n % 6 == 0:
                            pt.set_learn_status(f"● learning [{seen_f_}] · remembers {self._ppose_bufn} exemplars · "
                                                f"refined on {self._ppose_adapt_n} frames · err {self._ppose_err_ema:.3f}")
                    else:
                        pt.set_learn_status("paused — no finger is clearly visible to the camera", COLOR_ORANGE)
                elif F["learn"]:
                    pt.set_learn_status(("paused — camera does not see the hand; predicting from EMG + IMU"
                                         + (f" (refined on {self._ppose_adapt_n} frames)" if self._ppose_adapt_n else "")), COLOR_ORANGE)
                else:
                    pt.set_learn_status("continual learning is switched off" + (f" (model refined on {self._ppose_adapt_n} frames so far)" if self._ppose_adapt_n else ""), COLOR_FG_DIM)
                raw = _np.clip(z @ self._ppose_Wlive, 0.0, 1.0)
                curls = _np.asarray(self._st_curl_filt.filter(raw, self._poll_dt), float)
                cb = _np.clip(curls + 1.8 * (curls ** 3) * (1.0 - curls), 0.0, 1.0)
                emg_local = _np.asarray(self._procedural_hand(cb, opp_mult=1.25), float)
            except Exception:
                curls = None; emg_local = None
        elif F["learn"]:
            pt.set_learn_status("paused — needs the EMG armband + wrist IMU + the trained pose model", COLOR_ORANGE)
        else:
            pt.set_learn_status("continual learning is switched off", COLOR_FG_DIM)
        chips["learn"] = ((COLOR_GREEN, "live") if learn_state[0] == "learning" else (COLOR_ORANGE, "paused") if learn_state[0] == "paused" else (COLOR_FG_DIM, "off"))
        # ── key-posture recogniser (Key Postures tab, read-only): a stable decision + its template hand ──
        kp_probs, kp_active, kp_top, kp_p = (self._kp_classify2(None, wq, (local if cam_ok else None), (float(_np.mean(fc)) if (cam_ok and fc is not None) else 0.0), self._poll_dt)
                                             if (key_postures is not None and wq is not None) else (None, None, None, 0.0))
        kp_local = self._kp_template_hand(kp_active) if (kp_active and F["snap"]) else None
        chips["posture"] = ((COLOR_FG_DIM, "no recogniser") if getattr(self, "_kp_model", None) is None else (COLOR_FG_DIM, "off") if not F["snap"]
                            else (key_postures.COLORS.get(kp_active, COLOR_GREEN), kp_active) if kp_active else (COLOR_ORANGE, f"{kp_top or '—'} {kp_p * 100:.0f} %"))
        hand_local = None; src_pose = None; fusion_note = ""; finger_cols = None
        if cam_ok and local is not None:
            L = _np.asarray(local, float); src_pose = "camera"
            if fc is not None:
                fc = _np.asarray(fc, float); wf = _np.clip((fc - 0.5) / 0.4, 0.0, 1.0)
                finger_cols = [(COLOR_GREEN if fc[k] >= 0.7 else COLOR_ORANGE if fc[k] >= 0.4 else "#ff5a5a") for k in range(5)]
                fill_src = kp_local if kp_local is not None else emg_local
                if fill_src is not None:
                    E = fill_src - fill_src[1]; nfill = 0
                    for ci, chain in enumerate(SKELETON_CHAINS):
                        if wf[ci] < 0.999:
                            for j in chain[1:]:
                                L[j] = wf[ci] * L[j] + (1.0 - wf[ci]) * E[j]
                            if wf[ci] < 0.5:
                                nfill += 1
                    if nfill:
                        src_pose = f"camera + {'KEY POSTURE ' + kp_active if kp_local is not None else 'EMG'} ({nfill} hidden finger{'s' if nfill > 1 else ''})"
                        fusion_note = "hidden fingers are filled in from " + ("the recognised key posture" if kp_local is not None else "the EMG")
            else:
                finger_cols = [COLOR_GREEN] * 5
            hand_local = L
            if curls is None:
                try: curls = _np.asarray(self._finger_flex(local), float)
                except Exception: curls = None
        elif kp_local is not None:
            hand_local = kp_local; src_pose = f"KEY POSTURE: {kp_active}"; finger_cols = [key_postures.COLORS.get(kp_active, pt.TEAL)] * 5
            curls = _np.asarray(self._kp_model["templates"][self._kp_model["classes"].index(kp_active)], float)   # bars match the drawn hand
        elif emg_local is not None:
            hand_local = emg_local - emg_local[1]; src_pose = "EMG"; finger_cols = [pt.TEAL] * 5
        elif self._st_last_local is not None:
            hand_local = self._st_last_local; src_pose = "held"; finger_cols = [COLOR_FG_DIM] * 5
        elif self._hand_prior is not None:
            try:
                hand_local = _np.asarray(self._procedural_hand(0.0), float); hand_local = hand_local - hand_local[1]
                src_pose = "neutral (no data)"; finger_cols = [COLOR_FG_DIM] * 5
            except Exception:
                hand_local = None
        if hand_local is not None and src_pose != "held":
            self._st_last_local = hand_local
        HW = (wrist + (hand_R @ hand_local.T).T) if (wrist is not None and hand_local is not None and hand_R is not None) else None
        # ── signals ──
        def seg(Rm, cal, qv):
            f = Rm[:, 0]; el = math.degrees(math.asin(max(-1.0, min(1.0, float(f[1]))))); hz = _np.array([f[0], 0.0, f[2]]); nn = float(_np.linalg.norm(hz))
            hd = math.degrees(math.atan2(float(hz @ r_b), float(hz @ f_b))) if nn > 0.15 else 0.0
            return {"elev": el, "heading": hd, "cal": cal, "q": ("q " + " ".join(f"{v:+.2f}" for v in qv)) if qv is not None else ""}
        imu = {}
        if forearm_R is not None: imu["wrist"] = seg(forearm_R, True, wq)
        elif wrist_raw_R is not None: imu["wrist"] = seg(wrist_raw_R, False, wq)
        if knuckle_R is not None: imu["knuckle"] = seg(knuckle_R, True, kq)
        elif kq is not None and _fus_quat_to_R is not None: imu["knuckle"] = seg(_np.asarray(_fus_quat_to_R(list(kq)), float), False, kq)
        bend = None
        if forearm_R is not None and knuckle_R is not None:
            bend = math.degrees(math.acos(max(-1.0, min(1.0, float(forearm_R[:, 0] @ knuckle_R[:, 0])))))
        head_yaw_rel = math.degrees(math.atan2(float(hf @ r_b), float(hf @ f_b))) if n > 0.2 else 0.0
        head_pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(head_f[1])))))
        xyz = None; depth = None; cam_depth = None
        if wrist is not None:
            d = wrist - neck; xyz = (float(d @ r_b), float(d @ up), float(d @ f_b)); depth = float(-(hR.T @ (wrist - hp))[2])
        if cam_ok:
            cam_depth = float(-(hR.T @ (P_world[1] - hp))[2])
        shoulder = neck + (0.165 if right else -0.165) * r_b - 0.19 * up + 0.05 * f_b
        gtxt, gcol = {
            "CLEAR": ("gate: ✓ CLEAR — the camera sees your hand", COLOR_GREEN),
            "SELF": ("gate: ◐ SELF-OCCLUSION (fist / point)", COLOR_ORANGE),
            "OTHER": ("gate: ✕ OCCLUDED — blocked by something in front", COLOR_ORANGE),
            "OBJECT": ("gate: ✕ OCCLUDED — blocked by an object", COLOR_ORANGE),
            "OUT_OF_VIEW": ("gate: ⦸ OUT OF VIEW — hand not in the camera frame", "#ff5a5a"),
        }.get(gate_raw, ("gate: — no optical —", COLOR_FG_DIM))
        chips["gate"] = ((COLOR_FG_DIM, "off") if not F["gate"] else (gcol, gate_raw.replace("_", " ").lower() if gate_raw else "—"))
        S = {"flags": F, "right": right, "hmd_p": hp, "hmd_R": hR, "head_fwd": head_f, "neck": neck, "axes": (r_b, up, f_b),
             "fov_az": float(getattr(self, "_fov_az", 55.0)), "shoulder": shoulder, "elbow": elbow, "wrist": wrist,
             "src_pos": src_pos, "why": why, "lost_t": float((out or {}).get("lost_t", 0.0)) if src_pos == "hybrid" else 0.0,
             "n_anchor": int((out or {}).get("n_anchor", 0)), "g": float((out or {}).get("g", 0.0)),
             "hand": HW, "hand_local": hand_local, "hand_R": hand_R, "src_R": src_R, "src_pose": src_pose,
             "finger_cols": finger_cols, "fc": fc, "curls": curls, "fusion_note": fusion_note,
             "kp": {"active": kp_active, "top": kp_top, "p": kp_p, "probs": kp_probs, "classes": (self._kp_model["classes"] if getattr(self, "_kp_model", None) else [])},
             "imu": imu, "bend": bend, "emg_rms": rms, "acc": acc, "xyz": xyz, "depth": depth, "cam_depth": cam_depth,
             "head_yaw_rel": head_yaw_rel, "head_pitch": head_pitch, "gate_txt": gtxt if F["gate"] else "gate: switched off", "gate_col": gcol,
             "chips": chips}
        pt.draw(S, msg)

    def _load_kp(self):
        self._kp_teacher = None
        try:
            p2 = key_postures.default_model2_path() if hasattr(key_postures, "default_model2_path") else ""
            p = key_postures.default_model_path()
            if p2 and os.path.exists(p2):                                    # v2: EMG covariance + MLP + camera self-teaching
                self._kp_model = key_postures.load2(p2); self._kp_teacher = key_postures.OnlineTeacher(self._kp_model)
            else:
                self._kp_model = key_postures.load(p) if os.path.exists(p) else None
            self._kp_gate = key_postures.PostureGate(self._kp_model["classes"]) if self._kp_model is not None else None
            if self._kp_model is not None:
                self.log(f"Key Postures: recogniser loaded ({', '.join(self._kp_model['classes'])}; "
                         + ("v2 with camera self-teaching" if self._kp_teacher is not None else "v1 linear") + ")")
        except Exception as e:
            self._kp_model = None; self._kp_gate = None; self._kp_teacher = None
            self.log(f"Key Postures: load failed — {e!r}")
        self._kp_templates = {}

    def _kp_template_hand(self, name):
        """26-joint local hand (wrist at the origin, R_opt frame) for a recognised posture, from its template curls."""
        M = self._kp_model
        if M is None or name not in M["classes"] or self._hand_prior is None:
            return None
        if name not in self._kp_templates:
            try:
                k = M["classes"].index(name)
                if bool(_np.asarray(M.get("template_has_joints", []))[k]) if len(_np.asarray(M.get("template_has_joints", []))) > k else False:
                    H = _np.asarray(M["template_joints"][k], float)                     # the camera's median shape of THIS posture
                else:
                    cur = _np.asarray(M["templates"][k], float)
                    cb = _np.clip(cur + 1.8 * (cur ** 3) * (1.0 - cur), 0.0, 1.0)     # same render boost as Pose Hand
                    H = _np.asarray(self._procedural_hand(cb, opp_mult=1.25), float)
                self._kp_templates[name] = H - H[1]
            except Exception:
                return None
        return self._kp_templates[name]

    def _kp_classify(self, feat43, dt):
        """→ (probs, active name or None, top name, top prob) — one shared gate, whichever tab calls it this frame."""
        M = self._kp_model
        if M is None or feat43 is None or self._kp_gate is None:
            return None, None, None, 0.0
        try:
            p = key_postures.predict(M, _np.asarray(feat43, float)[None])[0]
            active, top_p, top = self._kp_gate.update(p, max(dt, 1e-3))
            return p, active, top, top_p
        except Exception:
            return None, None, None, 0.0

    def _kp_teach_enabled(self):
        pt = getattr(self, "kp_tab", None)
        try:
            return bool(pt.teach_var.get()) if (pt is not None and hasattr(pt, "teach_var")) else True
        except Exception:
            return True

    def _kp_teach_status(self):
        T = getattr(self, "_kp_teacher", None)
        if T is None:
            return ("v1 recogniser — press ⚙ Train for the self-teaching one" if getattr(self, "_kp_model", None) is not None else "")
        return ("self-teaching " + ("ON — " if self._kp_teach_enabled() else "OFF — ") + T.status())

    def _kp_reset_teacher(self):
        T = getattr(self, "_kp_teacher", None)
        if T is not None:
            T.reset()
            if self._kp_gate is not None:
                self._kp_gate.reset()
            self.log("Key Postures: camera self-teaching reset to the trained model")

    def _kp_classify2(self, raw, wq, local, cam_conf, dt, allow_teach=True):
        """v2 path: features from the raw EMG window + wrist tilt; while the camera sees the hand clearly the geometric
        label teaches the live copy of the model (OnlineTeacher); then the same stable gate. Falls back to the v1 path
        (43-d) when only the v1 recogniser is loaded. → (probs, active, top, top_p)."""
        M = getattr(self, "_kp_model", None); T = getattr(self, "_kp_teacher", None)
        if M is None or self._kp_gate is None or wq is None:
            return None, None, None, 0.0
        if T is None or M.get("kind") != "mlp":
            try:
                feat = emg_features(getattr(getattr(self.armband, "dash", None), "buf_emg", None)) if getattr(self, "armband", None) is not None else None
                feat43 = _np.concatenate([_np.asarray(feat, float), key_postures.tilt(_np.asarray(wq, float))]) if feat is not None else None
            except Exception:
                feat43 = None
            return self._kp_classify(feat43, dt)
        try:
            if raw is None:
                dash = getattr(getattr(self, "armband", None), "dash", None); be = getattr(dash, "buf_emg", None)
                if be is not None:
                    bem = _np.asarray(be, float)
                    if bem.shape[1] >= key_postures.EMG_WIN:
                        raw = bem[:, -key_postures.EMG_WIN:]
            if raw is None or not _np.isfinite(_np.asarray(raw, float)).all():
                return None, None, None, 0.0
            win = int(M.get("win", key_postures.EMG_WIN)); raw = _np.asarray(raw, float)[:, -win:]     # the window the model was trained with
            F = key_postures.feat_raw(raw[None], key_postures.tilt(_np.asarray(wq, float))[None])[0]
            if allow_teach and self._kp_teach_enabled() and local is not None and float(cam_conf or 0.0) >= key_postures.TEACH_CONF:
                if T.observe(F, key_postures.cam_label(local), time.time()):
                    T.step(key_postures.TEACH_STEPS if hasattr(key_postures, "TEACH_STEPS") else 3)
            elif allow_teach:
                T.observe(F, None, time.time())
            p = key_postures.predict2(T.M if self._kp_teach_enabled() else M, F[None])[0]
            active, top_p, top = self._kp_gate.update(p, max(dt, 1e-3))
            return p, active, top, top_p
        except Exception as e:
            self._kp_last_err = repr(e)
            return None, None, None, 0.0

    def _load_hand_prior(self):
        """Load the anatomical hand prior (hand_prior.npz): a clean OPEN rest skeleton plus a
        per-finger flexion axis, so we can procedurally curl a guaranteed-plausible hand. This is
        how we render a real fist — the optical labels can't (a closed fist self-occludes)."""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_prior.npz")
            if not os.path.exists(path):
                self._hand_prior = None
                return
            d = _np.load(path, allow_pickle=True)
            self._hand_prior = {"rest": _np.asarray(d["rest"], float),
                                "axes": _np.asarray(d["axes"], float),
                                "maxa": _np.asarray(d["maxa"], float),
                                "nflex": _np.asarray(d["nflex"], int),
                                "open_ref": float(d["open_ref"]) if "open_ref" in d.files else None,
                                "fist_ref": float(d["fist_ref"]) if "fist_ref" in d.files else None,
                                "opp_axis": (_np.asarray(d["opp_axis"], float)
                                             if "opp_axis" in d.files else None),
                                "opp_deg": float(d["opp_deg"]) if "opp_deg" in d.files else 0.0}
            self.log("Fusion: anatomical hand prior loaded (hand_prior.npz)")
        except Exception as e:
            self._hand_prior = None
            self.log(f"hand prior load failed — {e!r}")

    @staticmethod
    def _axis_rot(k, ang):
        """Rodrigues rotation matrix about unit axis k by angle (rad)."""
        k = _np.asarray(k, float); k = k / (_np.linalg.norm(k) + 1e-12)
        c, s = math.cos(ang), math.sin(ang)
        K = _np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        return _np.eye(3) * c + s * K + (1 - c) * _np.outer(k, k)

    def _procedural_hand(self, closure, opp_mult=1.0):
        """Curl the clean rest hand by `closure` (scalar 0..1, or per-finger array of 5) about each
        finger's flexion axis → a guaranteed-plausible 26x3 de-rotated pose (fingers can't splay,
        cross, or deform). `opp_mult` scales the thumb-opposition wrap (>1 = thumb reaches further,
        e.g. so a pinch's tips meet). Returns None if the prior isn't loaded."""
        hp = self._hand_prior
        if hp is None:
            return None
        rest, axes, maxa = hp["rest"], hp["axes"], hp["maxa"]
        P = [rest[j].copy() for j in range(26)]
        # THUMB opposition: swing the whole thumb about the palm-normal axis toward the palm as it
        # curls, so a fist wraps the thumb over the fingers (instead of it sticking straight out).
        thumb_c = float(closure if _np.isscalar(closure) else closure[0])
        thumb_c = max(0.0, min(1.0, thumb_c))
        if hp.get("opp_axis") is not None and hp.get("opp_deg", 0.0):
            piv = P[2]
            R = self._axis_rot(hp["opp_axis"], math.radians(thumb_c * hp["opp_deg"] * opp_mult))
            for j in (3, 4, 5):
                P[j] = piv + R @ (P[j] - piv)
        for ci, chain in enumerate(SKELETON_CHAINS):
            cf = float(closure if _np.isscalar(closure) else closure[ci])
            cf = max(0.0, min(1.0, cf))
            for p in range(2, len(chain) - 1):        # flex at proximal/intermediate/distal joints
                ang = math.radians(cf * float(maxa[ci][min(p - 2, maxa.shape[1] - 1)]))
                piv = P[chain[p]]
                R = self._axis_rot(axes[ci], ang)
                for j in range(p + 1, len(chain)):
                    P[chain[j]] = piv + R @ (P[chain[j]] - piv)
        return [tuple(float(x) for x in v) for v in P]

    @staticmethod
    def _finger_flex(local):
        """Per-finger flexion 0..1 (0=straight, 1=curled) from the 26-joint local pose, using the
        SUM OF INTERIOR JOINT BEND ANGLES — honest for the thumb (which curls across the palm),
        unlike a tip-to-palm distance ratio."""
        P = [_np.asarray(p, float) for p in local]
        out = []
        for ci, chain in enumerate(SKELETON_CHAINS):
            a = 0.0
            for i in range(1, len(chain) - 1):
                v1 = P[chain[i]] - P[chain[i - 1]]
                v2 = P[chain[i + 1]] - P[chain[i]]
                c = float(_np.dot(v1, v2) / (_np.linalg.norm(v1) * _np.linalg.norm(v2) + 1e-9))
                a += math.degrees(math.acos(max(-1.0, min(1.0, c))))
            out.append(max(0.0, min(1.0, (a - 15.0) / (FusionStudioApp._FLEX_MAX[ci] - 15.0))))
        return out

    def _save_imu_calib(self, calw, calk):
        """Persist the IMU→optical extrinsic (R_off per sensor) so the IMU stays calibrated when
        optical is off or before the first optical frame. NOTE: includes the per-power-on yaw, so
        after a GLOVE power-cycle (IMU yaw resets) re-run Calibrate IMU once to refresh the yaw."""
        try:
            d = {}
            if calw and calw.get("R_off") is not None:
                d["R_off_wrist"] = _np.asarray(calw["R_off"], float)
            if calk and calk.get("R_off") is not None:
                d["R_off_knuckle"] = _np.asarray(calk["R_off"], float)
            if d:
                _np.savez(self._imu_calib_path, **d)
        except Exception as e:
            self.log(f"IMU calib save failed — {e!r}")

    def _load_imu_calib(self):
        """Seed the fusers from a saved extrinsic at startup → the IMU hand is oriented correctly
        even with optical OFF. Live optical (a CLEAR frame) re-snaps/refines it when available."""
        try:
            if not HAS_FUSION or not os.path.exists(self._imu_calib_path):
                return
            d = _np.load(self._imu_calib_path, allow_pickle=True)
            if "R_off_wrist" in d.files and self._fuser_wrist is not None:
                self._fuser_wrist.set_R_off(_np.asarray(d["R_off_wrist"], float))
            if "R_off_knuckle" in d.files and self._fuser_knuckle is not None:
                self._fuser_knuckle.set_R_off(_np.asarray(d["R_off_knuckle"], float))
            self.log("IMU calibration loaded from disk (works without optical; re-Calibrate "
                     "after a glove power-cycle to refresh the yaw)")
        except Exception as e:
            self.log(f"IMU calib load failed — {e!r}")

    def _gate_log_event(self, label):
        """Append one edge-triggered event to a per-session CSV, timestamped with
        wall_time so it lines up with the occlusion-capture CSV's wall_time."""
        try:
            if self._gate_events_path is None:
                log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "skeleton_logs")
                os.makedirs(log_dir, exist_ok=True)
                self._gate_events_path = os.path.join(
                    log_dir, f"gate_events_{time.strftime('%Y%m%d_%H%M%S')}.csv")
                with open(self._gate_events_path, "w", newline="") as f:
                    csv.writer(f).writerow(["wall_time", "event"])
            with open(self._gate_events_path, "a", newline="") as f:
                csv.writer(f).writerow([f"{time.time():.6f}", label])
        except Exception as e:
            self.log(f"[gate] cannot log event — {e!r}")

    def _push_hand_imu(self):
        """Forward one glove hand's IMU orientation to the armband web-hand /pose,
        so the browser 3D hand rotates and bends with the real hand. Which glove
        IMU is the dorsal (hand) one vs the wrist one is configurable — watch the
        BODY 1 / BODY 2 / JOINT triads to see which tracks what, then set
        "imu_hand_slot" / "imu_wrist_slot" (body1|body2|joint) in settings.json.
        No-op unless the armband tab is running and the hand IMU is live."""
        arm = self.armband
        if arm is None or not getattr(arm, "running", False):
            return
        side = self._config.get("imu_hand", "right")
        state = self.ble.left if side == "left" else self.ble.right
        slot_bit = {"body1": IMU_BODY_PRIMARY, "body2": IMU_BODY_SECONDARY, "joint": IMU_JOINT}
        slot_q = {"body1": state.quat, "body2": state.quat_body2, "joint": state.quat_joint}
        present = state.imu_present if state.connected else 0

        def ok(slot):
            return bool(present & slot_bit.get(slot, 0))

        # Hand (dorsal) slot: honour the config if that IMU is actually present,
        # else auto-pick the first available BODY IMU (the dorsal one), else any
        # present slot. Wrist slot: config if present, else JOINT. This means a
        # glove that only exposes e.g. BODY 2 + JOINT (no BODY 1) just works.
        hand_slot = self._config.get("imu_hand_slot", "")
        if not ok(hand_slot):
            hand_slot = next((s for s in ("body1", "body2", "joint") if ok(s)), None)
        wrist_slot = self._config.get("imu_wrist_slot", "")
        if not ok(wrist_slot):
            wrist_slot = "joint" if ok("joint") else None
        if wrist_slot == hand_slot:
            wrist_slot = None   # never use one sensor as both hand and wrist

        if hand_slot is None:
            arm.push_hand_orientation(None, None, False)   # no hand IMU → browser uses drag-orbit
            return
        hand_q = list(slot_q[hand_slot])
        wrist_q = list(slot_q[wrist_slot]) if wrist_slot else None
        arm.push_hand_orientation(hand_q, wrist_q, True)

    def _on_autostart_changed(self):
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

    def _toggle_imu_log(self):
        """Start/stop the per-packet IMU CSV logger (both hands)."""
        if self.imu_log_var.get():
            # Default: an "imu_logs" folder next to the script (or the .exe when
            # frozen). Override with "imu_log_dir" in settings.json.
            if getattr(sys, "frozen", False):
                base = os.path.dirname(sys.executable)
            else:
                base = os.path.dirname(os.path.abspath(__file__))
            log_dir = self._config.get("imu_log_dir") or os.path.join(base, "imu_logs")
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                self.log(f"IMU log: cannot create {log_dir} — {e!r}")
                self.imu_log_var.set(False)
                return
            path = os.path.join(log_dir, f"imu_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            logger = ImuLogger(path, self.log)
            try:
                logger.start()
            except Exception as e:
                self.log(f"IMU log: could not start — {e!r}")
                self.imu_log_var.set(False)
                return
            self.imu_logger = logger      # published last so the BLE thread only
            self.log(f"IMU log: recording → {path}")   # sees a fully-started logger
        else:
            logger, self.imu_logger = self.imu_logger, None
            if logger is not None:
                logger.stop()

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


    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(".", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT, font=("Consolas", 14, "bold"))
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_FG_DIM, font=("Consolas", 9))
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 9))
        style.map("TCheckbutton", background=[("active", COLOR_BG)], foreground=[("active", COLOR_FG)])
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 9))
        style.map("TRadiobutton", background=[("active", COLOR_BG)], foreground=[("active", COLOR_FG)])
        style.configure("Small.TRadiobutton", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 9))
        style.configure("FK.TRadiobutton", background=COLOR_BG, foreground=COLOR_FG, font=("Consolas", 9))
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="white", font=("Consolas", 10, "bold"), padding=(14, 4))
        style.map("Accent.TButton", background=[("active", "#3a8be0"), ("disabled", COLOR_BG3)])
        style.configure("Stop.TButton", background="#c62828", foreground="white", font=("Consolas", 10, "bold"), padding=(14, 4))
        style.map("Stop.TButton", background=[("active", "#ff4444"), ("disabled", COLOR_BG3)])
        style.configure("Console.TButton", background=COLOR_BG3, foreground=COLOR_FG, font=("Consolas", 9), padding=(10, 2))
        style.map("Console.TButton", background=[("active", COLOR_BG2)], foreground=[("active", COLOR_ACCENT)])
        style.configure("TScale", background=COLOR_BG, troughcolor=COLOR_BG3)
        style.configure("Horizontal.TScale", background=COLOR_BG, troughcolor=COLOR_BG3)
        # settings carried in the config (no widgets for them in this single-window build)
        self.mode_var = tk.StringVar(value="vr")
        self.autostart_var = tk.BooleanVar(value=bool(self._config.get("autostart", False)))
        self.slimevr_var = tk.BooleanVar(value=bool(self._config.get("slimevr_enabled", False)))
        self.slimevr_body_var = tk.StringVar(value=self._config.get("slimevr_body_imu", "body1"))
        self.imu_log_var = tk.BooleanVar(value=False)
        self.skeleton_log_var = tk.BooleanVar(value=False)
        self.skeleton_btn = None
        # ── top bar: the three sources + console ──
        bar = ttk.Frame(self.root); bar.pack(fill=tk.X, padx=12, pady=(8, 2))
        ttk.Label(bar, text="⬡ CyberFinger Fusion Studio", style="Title.TLabel").pack(side=tk.LEFT)
        self.console_btn = ttk.Button(bar, text="▲ Console", style="Console.TButton", command=self._toggle_console)
        self.console_btn.pack(side=tk.RIGHT)
        self.status_label = ttk.Label(bar, text="Idle", style="Status.TLabel"); self.status_label.pack(side=tk.RIGHT, padx=(0, 12))
        src_bar = ttk.Frame(self.root); src_bar.pack(fill=tk.X, padx=12, pady=(2, 4))
        g = ttk.Frame(src_bar); g.pack(side=tk.LEFT, padx=(0, 18))
        ttk.Label(g, text="GLOVE (BLE → SteamVR)", style="Status.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self.start_btn = ttk.Button(g, text="▶ Start glove", style="Accent.TButton", command=self._start_bridge); self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(g, text="■ Stop", style="Stop.TButton", command=self._stop_bridge, state=tk.DISABLED); self.stop_btn.pack(side=tk.LEFT, padx=(4, 0))
        e = ttk.Frame(src_bar); e.pack(side=tk.LEFT, padx=(0, 18))
        ttk.Label(e, text="EMG ARMBAND (MindRove)", style="Status.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self.emg_btn = ttk.Button(e, text="▶ Start EMG", style="Accent.TButton", command=self._toggle_armband); self.emg_btn.pack(side=tk.LEFT)
        self.emg_src_var = tk.StringVar(value="armband")
        for value, label in (("armband", "device"), ("sim", "simulator")):
            ttk.Radiobutton(e, text=label, value=value, variable=self.emg_src_var, style="Small.TRadiobutton").pack(side=tk.LEFT, padx=(6, 0))
        c = ttk.Frame(src_bar); c.pack(side=tk.LEFT)
        ttk.Label(c, text="HEADSET CAMERA (OpenXR)", style="Status.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self.preview_btn = ttk.Button(c, text="◉ Start preview", style="Accent.TButton", command=self._toggle_preview); self.preview_btn.pack(side=tk.LEFT)
        # ── console (bottom, hidden until toggled) ──
        self.log_frame = ttk.Frame(self.root)
        self.log_text = scrolledtext.ScrolledText(self.log_frame, height=8, bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
                                                  font=("Consolas", 9), relief=tk.FLAT, borderwidth=0, selectbackground=COLOR_ACCENT,
                                                  selectforeground="white", state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.console_visible = False
        # ── the Studio fills the rest ──
        self.st_frame = ttk.Frame(self.root); self.st_frame.pack(fill=tk.BOTH, expand=True)
        self.st_tab = FusionStudioTab(self.st_frame, on_forward=self._st_set_forward, on_reset=self._st_reset_learning,
                                      on_save=self._st_save_refined)
        self.st_tab.visible = True
        # ── hidden helpers: the embedded EMG dashboard (streams; never painted) and the occlusion gate ──
        self._hidden = ttk.Frame(self.root)
        self.armband_frame = ttk.Frame(self._hidden)
        self.armband = ArmbandTab(self.armband_frame, log=self.log) if HAS_ARMBAND else None
        if self.armband is not None:
            self.armband.visible = False
        self.gate_frame = ttk.Frame(self._hidden)
        self.gate_tab = GateTab(self.gate_frame, on_calibrate=lambda: None, box=(self._fov_az, self._fov_el_lo, self._fov_el_hi))
        self.gate_tab.visible = False
        hands = ttk.Frame(self._hidden)
        self.left_panel = HandPanel(hands, "LEFT", side=tk.LEFT)
        self.right_panel = HandPanel(hands, "RIGHT", side=tk.RIGHT)

    def _toggle_armband(self):
        arm = getattr(self, "armband", None)
        if arm is None:
            self.log("EMG armband unavailable — armband_panel.py did not import (pip install numpy matplotlib mindrove)")
            return
        if getattr(arm, "running", False):
            arm.stop()
        else:
            try:
                arm.source_var.set(self.emg_src_var.get())
            except Exception:
                pass
            arm.start()
        self._update_emg_btn()

    def _update_emg_btn(self):
        btn = getattr(self, "emg_btn", None); arm = getattr(self, "armband", None)
        if btn is None:
            return
        btn.configure(text=("■ Stop EMG" if (arm is not None and getattr(arm, "running", False)) else "▶ Start EMG"),
                      style=("Stop.TButton" if (arm is not None and getattr(arm, "running", False)) else "Accent.TButton"))

    def _update_preview_btn(self):
        btn = getattr(self, "preview_btn", None)
        if btn is None:
            return
        btn.configure(text="■ Stop preview" if self._preview_running else "◉ Start preview",
                      style=("Stop.TButton" if self._preview_running else "Accent.TButton"))

    def _toggle_console(self):
        self.console_visible = not self.console_visible
        self.console_btn.configure(text="▼ Console" if self.console_visible else "▲ Console")
        if self.console_visible:
            self.log_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 8), before=self.st_frame)
            self.log_text.see(tk.END)
        else:
            self.log_frame.pack_forget()

    def _on_tab_changed(self, _event=None):
        if getattr(self, "st_tab", None) is not None:
            self.st_tab.visible = True

    def _poll_queues(self):
        """Main-thread tick: drain the log/status queues, pick the live optical source, run the gate and the Studio."""
        _t0 = time.perf_counter()
        if self._last_poll_t is not None:
            self._poll_dt = min(0.1, max(1 / 120.0, _t0 - self._last_poll_t))
        self._last_poll_t = _t0
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
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
        src = self._preview_skeleton
        if src is None and self.skeleton and self._skeleton_running:
            src = self.skeleton
        self._update_gate_tab(src)
        self._st_tick(src)
        if getattr(self, "armband", None) is not None:
            self.armband.tick()
            self._push_hand_imu()
            self._update_emg_btn()
        _busy_ms = (time.perf_counter() - _t0) * 1000.0
        self.root.after(max(2, int(round(16.0 - _busy_ms))), self._poll_queues)

    def _quit_app(self):
        """Full application shutdown."""
        self._config["mode"] = "vr"
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()
        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self._stop_slimevr()
        if self.imu_logger is not None:
            self.imu_logger.stop()
            self.imu_logger = None
        if self._preview_running:
            self._preview_stop()
        if self.skeleton and self._skeleton_running:
            self.skeleton.stop()
        if getattr(self, "armband", None) is not None:
            self.armband.shutdown()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.log("Fusion Studio ready — start the glove, the EMG armband and the headset camera preview from the top bar")
        if not HAS_OPENXR:
            self.log("Headset camera: OpenXR not available (pip install pyopenxr glfw PyOpenGL)")
        if not HAS_ARMBAND:
            self.log("EMG armband: armband_panel did not import (pip install numpy matplotlib mindrove)")
        self.root.mainloop()

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
        # raw stick counts (diagnostic): what the glove actually sends, pre-deadzone
        c.create_text(joy_cx, joy_cy + joy_r + 10, anchor=tk.N,
                      text=f"raw {state.joy_x},{state.joy_y}",
                      fill=COLOR_FG_DIM, font=("Consolas", 7))

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


GATE_FOV_H = 55.0     # deg, horizontal half-angle
GATE_FOV_V = 45.0     # deg, vertical half-angle
GATE_FOV_SOFT = 6.0   # deg, soft edge band
GATE_TAU_LO = 0.35     # confidence-bar colour thresholds (red / yellow / green)
GATE_TAU_HI = 0.70
GATE_OCC_FRAC = 0.15   # inter-hand: fraction of a hand's joints behind the other → OTHER
GATE_CURL_RATIO = 0.65 # finger CURLED when straight-line/extended-length < this.
GATE_SELF_FRAC = 0.30  # self-occ: fraction of curled fingers → SELF (open 0%,
GATE_FREEZE_N = 5      # consecutive IDENTICAL poses ⇒ Quest froze a stale pose (it
GATE_BOX_MARGIN = 8.0  # deg inside the FOV box that still counts as "well inside"
POS_VALID = 0x2        # XR_SPACE_LOCATION_POSITION_VALID_BIT
COLOR_PURPLE = "#b388ff"   # object-occlusion colour (distinct from red/orange)
_FLEX_CHAINS = ((3, 4, 5), (7, 8, 9, 10), (12, 13, 14, 15),
                (17, 18, 19, 20), (22, 23, 24, 25))
_TYPE_SEV = {"OUT_OF_VIEW": 4, "OBJECT": 3, "OTHER": 2, "SELF": 1, "CLEAR": 0}
def _type_color(t):
    if t == "OUT_OF_VIEW":
        return COLOR_RED
    if t == "OBJECT":
        return COLOR_PURPLE
    if t in ("OTHER", "SELF"):
        return COLOR_ORANGE
    return COLOR_GREEN


def _quat_to_R(x, y, z, w):
    """Unit quaternion → 3x3 (columns = camera basis in world); camera looks -z.
    Matches fusion/run_gate_on_log.quat_to_R so g_fov reads correctly."""
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return _np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def _bearing_cam(p_world, cam_pos, cam_R):
    """World point → (az_deg, el_deg, dist) in the camera frame (camera looks -z)."""
    q = (p_world - cam_pos) @ cam_R
    fwd = -float(q[2])
    dist = float(_np.linalg.norm(p_world - cam_pos))
    az = math.degrees(math.atan2(float(q[0]), max(fwd, 1e-6)))
    el = math.degrees(math.atan2(float(q[1]), max(fwd, 1e-6)))
    return az, el, dist


def _interhand_occ(P_self, P_other, cam_pos, cam_R, ang_margin=3.0, depth_margin=0.02):
    """Inter-hand occlusion, the simple robust way: the fraction of THIS hand's
    joints that fall inside the OTHER hand's angular footprint (as seen by the
    camera) AND are deeper than it — i.e. this hand is behind the other in view.
    A silhouette+depth overlap, far more forgiving than per-joint sphere ray-cast
    (which needs joints to line up within ~1 cm and misses real hand-behind-hand)."""
    def bearings(P):
        q = (P - cam_pos) @ cam_R
        fwd = _np.maximum(-q[:, 2], 1e-6)
        return (_np.degrees(_np.arctan2(q[:, 0], fwd)),
                _np.degrees(_np.arctan2(q[:, 1], fwd)),
                _np.linalg.norm(P - cam_pos, axis=1))
    saz, sel, sd = bearings(P_self)
    oaz, oel, od = bearings(P_other)
    az_lo, az_hi = oaz.min() - ang_margin, oaz.max() + ang_margin
    el_lo, el_hi = oel.min() - ang_margin, oel.max() + ang_margin
    od_med = float(_np.median(od))
    inside = ((saz >= az_lo) & (saz <= az_hi) & (sel >= el_lo) & (sel <= el_hi)
              & (sd > od_med + depth_margin))
    return float(inside.mean())


def _self_occlusion(P):
    """Self-occlusion = the fraction of fingers that are CURLED — measured by how
    much the finger is folded: straight-line (knuckle→tip) / extended-length. An
    open finger is ~1.0; a curled one is small. Curled fingers hide their tips
    behind the hand (a fist), and the same measure catches a bent hand where the
    fingers fold. Verified on real clips to cleanly separate open / behind-object
    (0% curled) from fist (75%) and wrist-bend (39%). View-independent — needs no
    camera pose. This is what Quest actually reports reliably; a depth/orientation
    test does NOT (Quest reports a plausible pose regardless of real occlusion)."""
    occ = 0
    for chain in _FLEX_CHAINS:                         # (knuckle .. tip) per finger
        length = sum(float(_np.linalg.norm(P[chain[i + 1]] - P[chain[i]]))
                     for i in range(len(chain) - 1))   # extended finger length
        if length < 1e-6:
            continue
        if float(_np.linalg.norm(P[chain[-1]] - P[chain[0]])) / length < GATE_CURL_RATIO:
            occ += 1
    return occ / len(_FLEX_CHAINS)


def _bearing_covered_by(az, el, P_other, cam_pos, cam_R, margin=5.0):
    """Is the bearing (az,el) inside the OTHER hand's angular footprint? Used to
    call a fully-dropped hand OCCLUDED-BY-OTHER when the other hand now sits where
    it vanished."""
    q = (P_other - cam_pos) @ cam_R
    fwd = _np.maximum(-q[:, 2], 1e-6)
    oaz = _np.degrees(_np.arctan2(q[:, 0], fwd))
    oel = _np.degrees(_np.arctan2(q[:, 1], fwd))
    return (oaz.min() - margin <= az <= oaz.max() + margin
            and oel.min() - margin <= el <= oel.max() + margin)


def classify_occlusion(is_active, out_of_box, self_occ, other_occ, frozen, well_inside):
    """Occlusion TYPE for a hand that still has (possibly inferred) positions.
    Occlusion cues are checked REGARDLESS of is_active — Quest drops is_active to 0
    (or, worse, keeps it 1 with a FROZEN stale pose) exactly when a hand is lost,
    so trusting is_active hides the very thing we want. `frozen` (a stale repeated
    pose) or is_active off ⇒ the hand is not really being tracked → OBJECT if it
    stalled well inside the view (something blocking it) else OUT_OF_VIEW (it left
    at the edge). Priority: out-of-box > inter-hand > self > stale/lost > clear."""
    if out_of_box:
        return "OUT_OF_VIEW"
    if other_occ >= GATE_OCC_FRAC:
        return "OTHER"
    if self_occ >= GATE_SELF_FRAC:
        return "SELF"
    if frozen or not is_active:                 # stale frozen pose / Quest lost it
        return "OBJECT" if well_inside else "OUT_OF_VIEW"
    return "CLEAR"


def _fov_status(pose):
    """Geometry-only fallback (no gate / no world data): blind-spot from one
    pose_info tuple (rot, local, dist, vel) or None. Returns the same view-status
    dict shape the gate path produces."""
    if pose is None:                               # not tracked → avatar gone
        return {"present": False, "az": 0.0, "el": 0.0, "dist": 0.0,
                "type": "OUT_OF_VIEW", "conf": 0.0}
    _, local, dist, _v = pose
    x, y, z = local
    fwd = -z
    az = math.degrees(math.atan2(x, fwd if fwd else 1e-6))
    el = math.degrees(math.atan2(y, fwd if fwd else 1e-6))
    return {"present": True, "az": az, "el": el, "dist": float(dist),
            "type": "CLEAR", "conf": 1.0}          # pose present = tracked (no occ w/o gate)


class GateTab:
    """Live 'Gate Radar' — HMD camera-view of both hands vs the optical sensor
    FOV, with a plain-language occlusion-TYPE banner and per-hand status. Fed
    per-hand view-status dicts {present, az, el, dist, type, conf} from the App."""

    _TYPE_TXT = {"CLEAR": "tracked", "OUT_OF_VIEW": "OUT OF VIEW",
                 "SELF": "SELF-OCC", "OTHER": "OCC-BY-OTHER", "OBJECT": "BEHIND OBJECT"}

    def __init__(self, parent, on_calibrate=None, box=(55.0, -45.0, 45.0)):
        self.visible = False
        self._last_px = [None, None]      # last on-screen pos per hand (ghost when lost)
        # Sensor-FOV box bounds (deg): az half-angle + asymmetric elevation
        # low/high. Calibrated to Quest's real tracking envelope, so the drawn box
        # is where the cameras actually track — not a guessed cone.
        self.az, self.el_lo, self.el_hi = box
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text="⬡ Gate Radar — optical sensor view",
                  style="Title.TLabel").pack(anchor=tk.W, padx=16, pady=(12, 2))
        ttk.Label(outer, text="Green box = where the Quest cameras actually track "
                  "your hands. Outside it = OUT OF VIEW. Inside, each hand is "
                  "labelled: tracked, self-occluded, or occluded by the other hand.",
                  style="Status.TLabel").pack(anchor=tk.W, padx=16)
        row = ttk.Frame(outer)
        row.pack(fill=tk.X, padx=16, pady=(6, 0))
        self.calib_btn = ttk.Button(row, text="⊹ Calibrate box", style="Console.TButton",
                                    command=(on_calibrate or (lambda: None)))
        self.calib_btn.pack(side=tk.LEFT)
        self.calib_lbl = tk.Label(row, text="", bg=COLOR_BG, fg=COLOR_FG_DIM,
                                  font=("Consolas", 9))
        self.calib_lbl.pack(side=tk.LEFT, padx=(10, 0))
        self.banner = tk.Canvas(outer, height=46, bg=COLOR_BG2, highlightthickness=0)
        self.banner.pack(fill=tk.X, padx=16, pady=(6, 6))
        self.view = tk.Canvas(outer, bg=COLOR_BG2, highlightthickness=0)
        self.view.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

    def set_box(self, az, el_lo, el_hi):
        self.az, self.el_lo, self.el_hi = az, el_lo, el_hi

    def _proj(self, w, h, az, el):
        """Map a bearing to a pixel so the calibrated box always fills ~82% of the
        canvas (box centred on its own mid-elevation, which shows the headset tilt)."""
        bx = 0.82 * (w / 2 - 12)
        by = 0.82 * (h / 2 - 12)
        el_mid = (self.el_lo + self.el_hi) / 2.0
        el_half = max(5.0, (self.el_hi - self.el_lo) / 2.0)
        sx = w / 2 + (az / max(self.az, 5.0)) * bx
        sy = h / 2 - ((el - el_mid) / el_half) * by
        return max(6, min(w - 6, sx)), max(6, min(h - 6, sy))

    _PHRASE = {"CLEAR": "TRACKED", "OUT_OF_VIEW": "OUT OF VIEW",
               "SELF": "SELF-OCCLUDED", "OTHER": "OCCLUDED BY {other}",
               "OBJECT": "BEHIND OBJECT"}

    def draw(self, vsL, vsR, status):
        self._draw_banner(vsL, vsR)
        self._draw_view(vsL, vsR, status)

    def _draw_banner(self, vsL, vsR):
        """Split banner: LEFT | RIGHT, each half coloured by that hand's state, so
        both hands' status show at once (e.g. one OUT OF VIEW, the other OCCLUDED)."""
        c = self.banner
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            return
        mid = w / 2
        for vs, hand, other, x0, x1 in ((vsL, "LEFT", "RIGHT", 0, mid),
                                        (vsR, "RIGHT", "LEFT", mid, w)):
            c.create_rectangle(x0, 0, x1, h, fill=_type_color(vs["type"]), outline="")
            phrase = self._PHRASE.get(vs["type"], vs["type"]).format(other=other)
            c.create_text((x0 + x1) / 2, h / 2, text=f"{hand}: {phrase}",
                          fill=COLOR_BG, font=("Consolas", 12, "bold"))
        c.create_line(mid, 0, mid, h, fill=COLOR_BG, width=2)

    def _draw_view(self, vsL, vsR, status):
        c = self.view
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        # sensor-FOV box at the calibrated bounds (az half-angle, el low..high)
        x0, y0 = self._proj(w, h, -self.az, self.el_hi)
        x1, y1 = self._proj(w, h, self.az, self.el_lo)
        c.create_rectangle(x0, y0, x1, y1, outline=COLOR_GREEN, width=2)
        c.create_text((x0 + x1) / 2, y0 - 8,
                      text=f"tracking box  az±{self.az:.0f}° el[{self.el_lo:.0f},{self.el_hi:.0f}]",
                      fill=COLOR_GREEN, font=("Consolas", 7))
        gx, gy = self._proj(w, h, 0.0, 0.0)                        # gaze-forward crosshair
        c.create_line(gx - 6, gy, gx + 6, gy, fill=COLOR_FG_DIM)
        c.create_line(gx, gy - 6, gx, gy + 6, fill=COLOR_FG_DIM)

        pxL = self._draw_hand(c, w, h, vsL, 0, "L", COLOR_BLUE)
        pxR = self._draw_hand(c, w, h, vsR, 1, "R", COLOR_ACCENT)
        occ_pair = (vsL["type"] == "OTHER" or vsR["type"] == "OTHER")
        if pxL and pxR:
            c.create_line(pxL[0], pxL[1], pxR[0], pxR[1],
                          fill=COLOR_RED if occ_pair else COLOR_FG_DIM,
                          width=2 if occ_pair else 1)
        self._draw_chips(c, w, h, vsL, "LEFT", COLOR_BLUE, left=True)
        self._draw_chips(c, w, h, vsR, "RIGHT", COLOR_ACCENT, left=False)
        if not (vsL["present"] or vsR["present"]):
            c.create_text(w / 2, h / 2, text=status, fill=COLOR_FG_DIM,
                          font=("Consolas", 8))

    def _draw_hand(self, c, w, h, vs, idx, tag, color):
        if not vs["present"]:
            px = self._last_px[idx]
            if px:                                        # ghost at last-known spot
                gc = _type_color(vs["type"])              # purple = behind object, red = out of view
                c.create_oval(px[0] - 5, px[1] - 5, px[0] + 5, px[1] + 5,
                              outline=gc, width=1)
                c.create_text(px[0], px[1], text="×", fill=gc,
                              font=("Consolas", 9, "bold"))
            return None
        # occluded/blind → typed colour (red/orange); tracked → the hand's colour
        tcol = color if vs["type"] == "CLEAR" else _type_color(vs["type"])
        jz = vs.get("joints_azel")
        if jz:                                            # full skeleton, as the sensor sees it
            pts = [self._proj(w, h, az, el) for az, el in jz]
            for chain in SKELETON_CHAINS:
                for a, b in zip(chain, chain[1:]):
                    c.create_line(pts[a][0], pts[a][1], pts[b][0], pts[b][1],
                                  fill=tcol, width=1)
            for chain in SKELETON_CHAINS:
                for j in chain[1:]:
                    x, y = pts[j]
                    r = 3 if j in SKELETON_TIPS else 2
                    c.create_oval(x - r, y - r, x + r, y + r, fill=tcol, outline="")
            wx, wy = pts[1]                               # wrist
            c.create_rectangle(wx - 3, wy - 3, wx + 3, wy + 3, fill=tcol, outline="")
            c.create_text(wx, wy - 12, text=tag, fill=color, font=("Consolas", 8, "bold"))
            self._last_px[idx] = (wx, wy)
            return (wx, wy)
        # fallback (no per-joint data, e.g. OpenVR backend): single dot at the wrist
        px = self._proj(w, h, vs["az"], vs["el"])
        self._last_px[idx] = px
        size = max(3.0, 10.0 - vs["dist"] * 6.0)          # closer → bigger
        c.create_oval(px[0] - size, px[1] - size, px[0] + size, px[1] + size,
                      fill=tcol if vs["type"] == "CLEAR" else "",
                      outline="" if vs["type"] == "CLEAR" else tcol, width=2)
        c.create_text(px[0], px[1] - size - 6, text=tag, fill=color,
                      font=("Consolas", 8, "bold"))
        return px

    def _draw_chips(self, c, w, h, vs, name, color, left):
        x = 12 if left else w - 12
        anchor = tk.W if left else tk.E
        tt = self._TYPE_TXT.get(vs["type"], vs["type"])
        txt = f"{name}  {tt}"
        if vs["present"]:
            txt += f"   {vs['dist']:.2f}m  az{vs['az']:+.0f}° el{vs['el']:+.0f}°"
        c.create_text(x, h - 8, text=txt, anchor=anchor,
                      fill=color if vs["type"] == "CLEAR" else _type_color(vs["type"]),
                      font=("Consolas", 8))
        conf = vs["conf"]
        bar_w = 110
        bx = x if left else x - bar_w
        by = h - 26
        col = (COLOR_RED if conf < GATE_TAU_LO else
               COLOR_ORANGE if conf < GATE_TAU_HI else COLOR_GREEN)
        c.create_rectangle(bx, by, bx + bar_w, by + 5, outline=COLOR_BG3)
        if conf > 0:
            c.create_rectangle(bx, by, bx + bar_w * conf, by + 5, fill=col, outline="")


CAPTURE_GESTURES = [
    # key, description, expected-gate label, auto-stop seconds (None = manual Stop)
    ("open",             "Open hand, palm to headset", "→ TRACKED", None),
    ("fist",             "Make a fist / curl fingers", "→ SELF-OCCLUDED", None),
    ("wrist_bend",       "Bend the wrist, fingers hidden", "→ SELF-OCCLUDED", None),
    ("hand_behind_hand", "This hand behind the other", "→ OCCLUDED BY OTHER", None),
    ("out_of_view",      "Move the hand out to the side", "→ OUT OF VIEW", None),
    ("behind_object",    "Slide the hand behind an object", "→ BEHIND OBJECT", None),
    ("rigid_hold",       "Hold a FIXED hand shape, swing the whole forearm",
     "→ FUSION DRIFT · 20 s", 20.0),
]
CAPTURE_TARGET = {g[0]: g[3] for g in CAPTURE_GESTURES}
class FusionStudioTab:
    """Fusion Studio — ONE professional view of everything: your body (placed and turned by the headset), the arm and the
    whole 26-joint hand at its estimated POSITION with its estimated POSTURE, from whichever sensors are switched on,
    plus live signal panels (hand, IMUs, EMG, position, headset) and a per-source enable switch. Standalone tab: it reads
    the shared pose model, the wrist/knuckle fusers and the hybrid nets read-only and keeps its own state. No other tab
    is modified."""

    TEAL = "#2dd4bf"; VIOLET = "#b388ff"; GOLD = "#ffd166"; BODY = "#5c6b7a"
    SRC = {"camera": (COLOR_ACCENT, "CAMERA"), "hybrid": ("#2dd4bf", "PREDICTED"), "hold": (COLOR_FG_DIM, "HELD")}
    SOURCES = (("optical", "Optical (camera)", COLOR_ACCENT), ("wrist", "Wrist IMU", COLOR_BLUE),
               ("knuckle", "Knuckle IMU", "#b388ff"), ("emg", "EMG (armband)", COLOR_GREEN),
               ("pos", "Position model", "#2dd4bf"), ("gate", "Occlusion gate", COLOR_ORANGE))
    FINGERS = ("thumb", "index", "middle", "ring", "pinky")
    TRAIL_S = 3.0

    def __init__(self, parent, on_forward=None, on_reset=None, on_save=None):
        self.visible = False
        self._on_forward = on_forward or (lambda: None)
        self._on_reset = on_reset or (lambda: None); self._on_save = on_save or (lambda: None)
        self._trail = []; self._dhist = []; self._ref_f = None; self._ref_t = None
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.BOTH, expand=True)
        head = ttk.Frame(outer); head.pack(fill=tk.X, padx=16, pady=(10, 0))
        ttk.Label(head, text="⬡ Fusion Studio — the whole hand on your body, every sensor in one view",
                  style="Title.TLabel").pack(side=tk.LEFT)
        self.msg = tk.Label(head, text="", bg=COLOR_BG, fg=COLOR_ORANGE, font=("Consolas", 10, "bold"), anchor=tk.E)
        self.msg.pack(side=tk.RIGHT)
        self.chips = tk.Canvas(outer, bg=COLOR_BG, highlightthickness=0, height=28)
        self.chips.pack(fill=tk.X, padx=16, pady=(4, 2))
        main = ttk.Frame(outer); main.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 10))
        # ── left column: switches ──
        left = ttk.Frame(main); left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        tk.Label(left, text="SOURCES", bg=COLOR_BG, fg=COLOR_FG, font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
        self.src_vars = {}
        for key, label, col in self.SOURCES:
            row = ttk.Frame(left); row.pack(fill=tk.X, pady=1)
            tk.Canvas(row, width=10, height=10, bg=COLOR_BG, highlightthickness=0).pack(side=tk.LEFT, padx=(0, 4))
            row.winfo_children()[0].create_oval(1, 1, 9, 9, fill=col, outline="")
            v = tk.BooleanVar(value=True); self.src_vars[key] = v
            ttk.Checkbutton(row, text=label, style="TCheckbutton", variable=v).pack(side=tk.LEFT)
        tk.Label(left, text="\nVIEW", bg=COLOR_BG, fg=COLOR_FG, font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
        self.view_vars = {}
        for key, label, default in (("body", "body outline", True), ("fov", "camera field of view", True),
                                    ("trail", "hand trail (3 s)", True), ("smooth", "smooth the prediction", True),
                                    ("follow", "body heading follows the headset", True), ("names", "labels", True),
                                    ("snap", "snap to KEY POSTURES when hidden", True)):
            v = tk.BooleanVar(value=default); self.view_vars[key] = v
            ttk.Checkbutton(left, text=label, style="TCheckbutton", variable=v).pack(anchor=tk.W)
        ttk.Button(left, text="⌖ Set forward (lock heading)", style="Console.TButton",
                   command=lambda: self._on_forward()).pack(anchor=tk.W, pady=(6, 2))
        tk.Label(left, text="\nHAND OUT OF VIEW", bg=COLOR_BG, fg=COLOR_FG, font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
        self.oov_var = tk.StringVar(value="knuckle")
        for val, label in (("knuckle", "knuckle IMU: live wrist bend\n(mount taught by the camera)"),
                           ("wrist", "wrist IMU: bend held from\nthe last camera fix")):
            ttk.Radiobutton(left, text=label, value=val, variable=self.oov_var, style="TRadiobutton").pack(anchor=tk.W)
        tk.Label(left, text="\nLEARNING", bg=COLOR_BG, fg=COLOR_FG, font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
        self.learn_var = tk.BooleanVar(value=True)          # same continual learning as Pose Hand (shared model + memory)
        ttk.Checkbutton(left, text="continual learning: the camera\nteaches the EMG posture, live", style="TCheckbutton",
                        variable=self.learn_var).pack(anchor=tk.W)
        lb = ttk.Frame(left); lb.pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(lb, text="↺ Reset", style="Console.TButton", command=lambda: self._on_reset()).pack(side=tk.LEFT)
        ttk.Button(lb, text="⇊ Save refined", style="Console.TButton", command=lambda: self._on_save()).pack(side=tk.LEFT, padx=(6, 0))
        self.learn_status = tk.Label(left, text="", bg=COLOR_BG, fg=COLOR_GREEN, font=("Consolas", 8), anchor=tk.W,
                                     justify=tk.LEFT, wraplength=205)
        self.learn_status.pack(fill=tk.X, pady=(2, 0))
        tk.Label(left, text="hand size in the views", bg=COLOR_BG, fg=COLOR_FG_DIM, font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X, pady=(8, 0))
        self.zoom_var = tk.DoubleVar(value=2.0)
        ttk.Scale(left, from_=1.0, to=3.0, orient=tk.HORIZONTAL, variable=self.zoom_var, length=170).pack(anchor=tk.W)
        self.legend = tk.Canvas(left, bg=COLOR_BG, highlightthickness=0, width=200, height=118)
        self.legend.pack(anchor=tk.W, pady=(10, 0))
        L = self.legend
        for i, (col, txt) in enumerate(((COLOR_ACCENT, "camera places the hand"), (self.TEAL, "position predicted"),
                                        (COLOR_FG_DIM, "held (nothing better)"), (COLOR_GREEN, "finger seen by the camera"),
                                        (COLOR_ORANGE, "finger partly hidden"), ("#ff5a5a", "finger hidden → EMG"),
                                        (self.GOLD, "camera field of view"))):
            L.create_oval(4, 6 + i * 16, 12, 14 + i * 16, fill=col, outline="")
            L.create_text(18, 10 + i * 16, anchor="w", text=txt, fill=COLOR_FG_DIM, font=("Consolas", 8))
        # ── right column: signal panels ──
        right = ttk.Frame(main, width=352); right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0)); right.pack_propagate(False)
        self.p_hand = tk.Canvas(right, bg=COLOR_BG2, highlightthickness=0, height=232); self.p_hand.pack(fill=tk.X, pady=(0, 6))
        self.p_imu = tk.Canvas(right, bg=COLOR_BG2, highlightthickness=0, height=126); self.p_imu.pack(fill=tk.X, pady=(0, 6))
        self.p_emg = tk.Canvas(right, bg=COLOR_BG2, highlightthickness=0, height=112); self.p_emg.pack(fill=tk.X, pady=(0, 6))
        self.p_pos = tk.Canvas(right, bg=COLOR_BG2, highlightthickness=0, height=190); self.p_pos.pack(fill=tk.X, pady=(0, 6))
        self.p_head = tk.Canvas(right, bg=COLOR_BG2, highlightthickness=0); self.p_head.pack(fill=tk.BOTH, expand=True)
        # ── centre: the two stages ──
        centre = ttk.Frame(main); centre.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.stages = []
        for title in ("FRONT — mirror view, you face the screen", "TOP — room map, forward is up"):
            f = ttk.Frame(centre); f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
            tk.Label(f, text=title, bg=COLOR_BG, fg=COLOR_FG, font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
            c = tk.Canvas(f, bg="#1e2126", highlightthickness=0); c.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
            self.stages.append(c)

    # ── small API ──
    def flags(self):
        d = {k: bool(v.get()) for k, v in self.src_vars.items()}
        d.update({k: bool(v.get()) for k, v in self.view_vars.items()})
        d["zoom"] = float(self.zoom_var.get()); d["learn"] = bool(self.learn_var.get()); d["oov"] = str(self.oov_var.get())
        return d

    def set_learn_status(self, text, color=None):
        self.learn_status.configure(text=text, fg=(color or COLOR_GREEN))

    def set_follow(self, on):
        self.view_vars["follow"].set(bool(on))

    def set_msg(self, text):
        self.msg.configure(text=text)

    # ── drawing ──
    def draw(self, S, msg="", chips=None):
        now = time.time()
        self.set_msg(msg)
        self._chips(chips if chips is not None else (S.get("chips", {}) if S else {}))
        if S is None:
            for c in self.stages:
                c.delete("all"); c._bg = None
                w, h = c.winfo_width(), c.winfo_height()
                if w > 100:
                    c.create_text(w / 2, h / 2, fill=COLOR_FG_DIM, font=("Consolas", 11),
                                  text="needs the headset pose — press ◉ Start preview (top bar)")
            for p in (self.p_hand, self.p_imu, self.p_emg, self.p_pos, self.p_head):
                p.delete("all")
            self._trail = []
            return
        if S.get("wrist") is not None:
            self._trail.append((now, _np.asarray(S["wrist"], float), self.SRC.get(S.get("src_pos"), (COLOR_FG_DIM, ""))[0]))
        while self._trail and self._trail[0][0] < now - self.TRAIL_S:
            self._trail.pop(0)
        for c, axis in ((self.stages[0], 1), (self.stages[1], 2)):
            self._stage(c, axis, S, now)
        self._panel_hand(S); self._panel_imu(S); self._panel_emg(S); self._panel_pos(S, now); self._panel_head(S)

    def _chips(self, st):
        c = self.chips; c.delete("all"); x = 2
        for key, label, _ in self.SOURCES + (("headset", "Headset", COLOR_FG), ("learn", "Learning", COLOR_GREEN), ("posture", "Key posture", COLOR_FG)):
            col, txt = st.get(key, (COLOR_FG_DIM, "—"))
            text = f"{label.upper()}  {txt}"; wpx = int(7.2 * len(text)) + 30
            c.create_rectangle(x, 3, x + wpx, 25, fill=COLOR_BG2, outline=COLOR_BG3)
            c.create_oval(x + 8, 10, x + 16, 18, fill=col, outline="")
            c.create_text(x + 22, 14, anchor="w", text=text, fill=(COLOR_FG_DIM if col == COLOR_FG_DIM else COLOR_FG),
                          font=("Consolas", 8, "bold"))
            x += wpx + 6

    def _stage(self, c, axis, S, now):
        w, h = c.winfo_width(), c.winfo_height()
        if w < 160 or h < 120:
            c.delete("all"); c._bg = None
            return
        neck = S.get("neck"); r_b, up, f_b = S["axes"]
        hmd_p = S["hmd_p"]; head_f = S["head_fwd"]
        if axis == 1:                                        # FRONT: horizontal = the body's right, vertical = world up
            s = min((w - 40) / 2.0, (h - 34) / 2.3); cx = w / 2.0; cy = h * 0.42
            if self._ref_f is None or abs(float((neck - self._ref_f) @ r_b)) > 0.9 or abs(float(neck[1] - self._ref_f[1])) > 0.5:
                self._ref_f = neck.copy()                    # the view re-centres when you walk / crouch out of it
            ref = self._ref_f
            P2 = lambda p: (cx + float((_np.asarray(p, float) - ref) @ r_b) * s, cy - (float(p[1]) - ref[1]) * s)
            key = (w, h, 1, round(float(ref[1]), 3))
        else:                                                # TOP: room map (world x right, forward = −z = up)
            s = min((w - 40) / 2.0, (h - 40) / 2.0); cx = w / 2.0; cy = h / 2.0
            if self._ref_t is None or math.hypot(neck[0] - self._ref_t[0], neck[2] - self._ref_t[2]) > 1.05:
                self._ref_t = neck.copy()
            ref = self._ref_t
            P2 = lambda p: (cx + (float(p[0]) - ref[0]) * s, cy + (float(p[2]) - ref[2]) * s)
            key = (w, h, 2, round(float(ref[0]), 3), round(float(ref[2]), 3))
        if getattr(c, "_bg", None) != key:                   # static layer: grid, floor, scale bar
            c.delete("all"); c._bg = key; T = ("bg",)
            grid = "#262a31"; grid2 = "#2f343c"
            if axis == 1:
                k = -5
                while k <= 5:
                    x = cx + k * 0.2 * s; c.create_line(x, 0, x, h, fill=(grid2 if k % 5 == 0 else grid), tags=T); k += 1
                y0 = math.floor((ref[1] - 1.6) / 0.2) * 0.2               # world-height lines: crouching / standing up shows
                while y0 <= ref[1] + 1.0:
                    y = cy - (y0 - ref[1]) * s
                    if 0 < y < h:
                        c.create_line(0, y, w, y, fill=(grid2 if abs(y0 - round(y0)) < 1e-6 else grid), tags=T)
                        if abs(y0 / 0.5 - round(y0 / 0.5)) < 1e-6:
                            c.create_text(4, y - 6, anchor="w", text=f"y {y0:+.1f} m", fill=COLOR_FG_DIM, font=("Consolas", 7), tags=T)
                    y0 = round(y0 + 0.2, 6)
                c.create_line(cx - 0.5 * s, h - 8, cx + 0.5 * s, h - 8, fill=COLOR_FG_DIM, width=2, tags=T)
                c.create_text(cx + 0.5 * s + 4, h - 8, anchor="w", text="1 m", fill=COLOR_FG_DIM, font=("Consolas", 7), tags=T)
            else:
                x0 = math.floor((ref[0] - 1.2) / 0.2) * 0.2
                while x0 <= ref[0] + 1.2:
                    x = cx + (x0 - ref[0]) * s
                    if 0 < x < w: c.create_line(x, 0, x, h, fill=(grid2 if abs(x0 - round(x0)) < 1e-6 else grid), tags=T)
                    x0 = round(x0 + 0.2, 6)
                z0 = math.floor((ref[2] - 1.2) / 0.2) * 0.2
                while z0 <= ref[2] + 1.2:
                    y = cy + (z0 - ref[2]) * s
                    if 0 < y < h: c.create_line(0, y, w, y, fill=(grid2 if abs(z0 - round(z0)) < 1e-6 else grid), tags=T)
                    z0 = round(z0 + 0.2, 6)
                c.create_line(20, h - 10, 20 + 0.5 * s, h - 10, fill=COLOR_FG_DIM, width=2, tags=T)
                c.create_text(24 + 0.5 * s, h - 10, anchor="w", text="50 cm", fill=COLOR_FG_DIM, font=("Consolas", 7), tags=T)
                c.create_text(w - 6, 10, anchor="e", text="room  x →   forward ↑", fill=COLOR_FG_DIM, font=("Consolas", 7), tags=T)
        c.delete("dyn"); D = ("dyn",)
        F = S["flags"]; right = S["right"]; sgn = 1.0 if right else -1.0
        # ── camera field of view (top view) ──
        if axis == 2 and F["fov"] and head_f is not None:
            hf = _np.array([head_f[0], 0.0, head_f[2]]); n = float(_np.linalg.norm(hf))
            if n > 0.2:
                hf /= n; a0 = math.atan2(hf[0], hf[2]); L = 0.75; pts = list(P2(hmd_p))
                for k in range(-8, 9):
                    a = a0 + math.radians(S["fov_az"]) * k / 8.0
                    pts += list(P2(hmd_p + L * _np.array([math.sin(a), 0.0, math.cos(a)])))
                c.create_polygon(pts, fill="#3a3a26", outline="#6b6538", stipple="gray25", tags=D)
        # ── body ──
        col_body = self.BODY
        if F["body"]:
            if axis == 1:
                B = lambda a, b: P2(neck + a * r_b + b * up)          # body-frame (right, up) metres → screen
                sh_y = P2(neck - 1.52 * up)[1]; sx0, _ = P2(neck)      # soft ground shadow under the feet
                c.create_oval(sx0 - 0.42 * s, sh_y - 0.05 * s, sx0 + 0.42 * s, sh_y + 0.05 * s, fill="#161a1e", outline="", tags=D)
                for sg in (-1.0, 1.0):                               # legs (no floor reference in this headset space)
                    self._limb(c, B(sg * 0.10, -0.64), B(sg * 0.10, -1.08), 0.13 * s, self.SKIN, D)
                    self._limb(c, B(sg * 0.10, -1.08), B(sg * 0.10, -1.48), 0.11 * s, self.SKIN, D)
                    fx, fy = B(sg * 0.10, -1.50); c.create_oval(fx - 0.07 * s, fy - 0.03 * s, fx + 0.07 * s, fy + 0.035 * s, fill=self.SKIN, outline=self.SKIN_EDGE, tags=D)
                other = -sgn                                          # the untracked arm hangs beside the body
                self._limb(c, B(other * 0.24, -0.17), B(other * 0.27, -0.45), 0.09 * s, self.SKIN, D)
                self._limb(c, B(other * 0.27, -0.45), B(other * 0.27, -0.72), 0.075 * s, self.SKIN, D)
                ox, oy = B(other * 0.27, -0.75); c.create_oval(ox - 0.045 * s, oy - 0.06 * s, ox + 0.045 * s, oy + 0.06 * s, fill=self.SKIN, outline="", tags=D)
                torso = [(0.06, -0.06), (0.22, -0.13), (0.24, -0.20), (0.20, -0.34), (0.16, -0.48), (0.19, -0.62), (0.17, -0.72),
                         (0.04, -0.74), (-0.04, -0.74), (-0.17, -0.72), (-0.19, -0.62), (-0.16, -0.48), (-0.20, -0.34),
                         (-0.24, -0.20), (-0.22, -0.13), (-0.06, -0.06)]
                c.create_polygon([v for a, b in torso for v in B(a, b)], fill=self.SKIN, outline=self.SKIN_EDGE, width=1,
                                 smooth=True, splinesteps=10, tags=D)
                hc = hmd_p + 0.02 * up; x, y = P2(hc); r = 0.105 * s
                self._limb(c, P2(hc - 0.08 * up), B(0.0, -0.08), 0.09 * s, self.SKIN, D)
                c.create_oval(x - r, y - r * 1.12, x + r, y + r * 1.0, fill="#323b46", outline=self.SKIN_EDGE, width=1, tags=D)
            else:
                cc = neck - 0.02 * f_b; pts = []
                for k in range(28):
                    a = 2 * math.pi * k / 28.0; pts += list(P2(cc + 0.23 * math.cos(a) * r_b + 0.12 * math.sin(a) * f_b))
                c.create_polygon(pts, fill=self.SKIN, outline=self.SKIN_EDGE, width=1, smooth=True, tags=D)
                other = -sgn; oa = neck + other * 0.24 * r_b - 0.02 * f_b   # the untracked arm, seen from above
                self._limb(c, P2(oa), P2(oa + other * 0.04 * r_b + 0.05 * f_b), 0.085 * s, self.SKIN, D)
                x, y = P2(hmd_p); r = 0.095 * s
                c.create_oval(x - r, y - r, x + r, y + r, fill="#323b46", outline=self.SKIN_EDGE, width=1, tags=D)
                if head_f is not None:
                    hf = _np.array([head_f[0], 0.0, head_f[2]]); n = float(_np.linalg.norm(hf))
                    if n > 0.2: c.create_line(x, y, *P2(hmd_p + 0.15 * hf / n), fill=COLOR_FG, width=3, tags=D)
                bx, by = P2(neck + 0.30 * f_b); c.create_line(*P2(neck), bx, by, fill="#6b7f92", width=1, dash=(3, 3), tags=D)
                c.create_text(bx, by - 8, text="body forward", fill="#6b7f92", font=("Consolas", 7), tags=D)
        # ── trail ──
        if F["trail"] and len(self._trail) > 1:
            pts = [(P2(q), t_, col_) for t_, q, col_ in self._trail]; n = len(pts); step = max(2, n // 14)
            for i0 in range(0, n - 1, step):
                i1 = min(n - 1, i0 + step); flat = [v for p_, _, _ in pts[i0:i1 + 1] for v in p_]
                c.create_line(*flat, width=2, tags=D, fill=_blend(pts[i1][2], "#1e2126", (now - pts[i1][1]) / self.TRAIL_S))
        # ── arm + hand ──
        wrist = S.get("wrist")
        if wrist is None:
            c.create_text(w / 2, (h / 2 if axis == 2 else h - 40), fill=COLOR_FG_DIM, font=("Consolas", 10), tags=D,
                          text="show your hand to the camera once")
            return
        col = self.SRC.get(S.get("src_pos"), (COLOR_FG_DIM, ""))[0]; arm_col = _blend(col, self.SKIN, 0.75)
        sh = S["shoulder"]; el = S.get("elbow")
        if el is not None:                                    # filled limbs, then the bone inside them
            self._limb(c, P2(sh), P2(el), 0.095 * s, arm_col, D); self._limb(c, P2(el), P2(wrist), 0.075 * s, arm_col, D)
            c.create_line(*P2(sh), *P2(el), *P2(wrist), fill=col, width=1, tags=D)
            ex, ey = P2(el); c.create_oval(ex - 3, ey - 3, ex + 3, ey + 3, fill=col, outline="", tags=D)
        else:
            c.create_line(*P2(sh), *P2(wrist), fill=arm_col, width=5, dash=(6, 4), tags=D)
        sx, sy = P2(sh); c.create_oval(sx - 3, sy - 3, sx + 3, sy + 3, fill=col, outline="", tags=D)
        HW = S.get("hand")                                    # (26,3) world joints, or None
        wx, wy = P2(wrist)
        if HW is not None:
            z = F["zoom"]; Q = wrist + (HW - wrist) * z            # magnified about the wrist (posture stays readable)
            pts2 = [P2(Q[j]) for j in range(26)]
            fcol = S.get("finger_cols") or [col] * 5
            self._hand_mesh(c, pts2, s * z, fcol, D, mesh_col=_blend(self.MESH, col, 0.12))
            if z > 1.05 and F["names"]:
                c.create_text(wx - 12, wy + 12, anchor="ne", text=f"hand ×{z:.1f}", fill=COLOR_FG_DIM, font=("Consolas", 7), tags=D)
        c.create_oval(wx - 9, wy - 9, wx + 9, wy + 9, outline=col, width=2, tags=D)
        c.create_oval(wx - 4, wy - 4, wx + 4, wy + 4, fill=col, outline="", tags=D)
        if F["names"]:
            lab = self.SRC.get(S.get("src_pos"), (COLOR_FG_DIM, "?"))[1]
            if S.get("src_pos") == "hybrid": lab += f"  {S.get('lost_t', 0.0):.1f} s"
            c.create_text(wx + 12, wy - 12, anchor="sw", text=lab, fill=col, font=("Consolas", 8, "bold"), tags=D)

    # ── mesh-style rendering (a filled body silhouette + a Meta-style hand shell with the skeleton inside) ──
    MESH = "#a7b6c6"; MESH_DARK = "#3a4756"; SKIN = "#2b3440"; SKIN_EDGE = "#46525f"

    def _hand_mesh(self, c, pts2, spx, fcol, tags, mesh_col=None, edge=True):
        """pts2: 26 screen points · spx: screen px per metre (incl. zoom). Draws a solid hand shell (palm + tapered finger
        capsules) and the skeleton inside it, coloured per finger."""
        mesh = mesh_col or self.MESH
        cxp = sum(pts2[j][0] for j in (1, 6, 11, 16, 21)) / 5.0; cyp = sum(pts2[j][1] for j in (1, 6, 11, 16, 21)) / 5.0
        palm = []
        for j in (1, 2, 6, 11, 16, 21):                        # palm outline pushed ~1 cm outwards from its centre
            dx, dy = pts2[j][0] - cxp, pts2[j][1] - cyp; n = math.hypot(dx, dy) or 1.0; e = 0.011 * spx
            palm += [pts2[j][0] + dx / n * e, pts2[j][1] + dy / n * e]
        c.create_polygon(palm, fill=mesh, outline=(self.MESH_DARK if edge else ""), width=1, smooth=True, splinesteps=8, tags=tags)
        for ci, chain in enumerate(SKELETON_CHAINS):
            for k, (a, b) in enumerate(zip(chain[1:], chain[2:])):
                wpx = max(3.0, (0.021 if ci == 0 else 0.019) * spx * (1.0 - 0.13 * k))
                c.create_line(pts2[a][0], pts2[a][1], pts2[b][0], pts2[b][1], fill=mesh, width=wpx, capstyle=tk.ROUND, tags=tags)
            a, b = chain[0], chain[1]                            # metacarpal, under the palm shell
            c.create_line(pts2[a][0], pts2[a][1], pts2[b][0], pts2[b][1], fill=mesh, width=max(3.0, 0.02 * spx), capstyle=tk.ROUND, tags=tags)
        for ci, chain in enumerate(SKELETON_CHAINS):            # the skeleton inside
            col = fcol[ci]
            for a, b in zip(chain, chain[1:]):
                c.create_line(pts2[a][0], pts2[a][1], pts2[b][0], pts2[b][1], fill=col, width=1, tags=tags)
            for j in chain[1:]:
                x, y = pts2[j]; rr = 2.2 if j in SKELETON_TIPS else 1.6
                c.create_oval(x - rr, y - rr, x + rr, y + rr, fill=(COLOR_ACCENT if j in SKELETON_TIPS else col), outline="", tags=tags)

    def _limb(self, c, p0, p1, wpx, col, tags):
        c.create_line(p0[0], p0[1], p1[0], p1[1], fill=col, width=max(2.0, wpx), capstyle=tk.ROUND, tags=tags)

    # ── signal panels ──
    def _title(self, c, text, w):
        c.create_rectangle(0, 0, w, 18, fill="#2a2f36", outline="")
        c.create_text(8, 9, anchor="w", text=text, fill=COLOR_FG, font=("Consolas", 9, "bold"))

    def _panel_hand(self, S):
        c = self.p_hand; c.delete("all"); w, h = c.winfo_width(), c.winfo_height()
        if w < 100: return
        self._title(c, "HAND — posture " + (f"from {S.get('src_pose', '—')}" if S.get("src_pose") else ""), w)
        HL = S.get("hand_local"); R = S.get("hand_R")
        cx, cy = w * 0.70, 24 + (h - 60) / 2.0; sc = max(40.0, min((h - 60) / 0.24, (w * 0.56) / 0.24))
        if HL is not None:
            fcol = S.get("finger_cols") or [COLOR_FG] * 5
            HLw = HL - HL[1]
            if R is not None: HLw = (R @ HLw.T).T
            HLw = HLw - HLw.mean(0)                           # centred on the hand, so it always fits the panel
            def px(j):
                sxx, syy, _ = project(tuple(HLw[j]), cx, cy, sc); return sxx, syy
            P = [px(j) for j in range(26)]
            self._hand_mesh(c, P, sc, fcol, ())
        else:
            c.create_text(cx, cy, text="no hand yet", fill=COLOR_FG_DIM, font=("Consolas", 9))
        curls = S.get("curls"); fc = S.get("fc")
        for i, name in enumerate(self.FINGERS):                # 5 curl bars, coloured by camera visibility
            y = 26 + i * 15; bw = w * 0.30
            c.create_text(8, y + 5, anchor="w", text=name[:5], fill=COLOR_FG_DIM, font=("Consolas", 8))
            c.create_rectangle(52, y, 52 + bw, y + 10, outline=COLOR_BG3, fill="")
            if curls is not None:
                col = (S.get("finger_cols") or [self.TEAL] * 5)[i]
                c.create_rectangle(52, y, 52 + bw * float(curls[i]), y + 10, fill=col, outline="")
                c.create_text(56 + bw, y + 5, anchor="w", text=f"{float(curls[i]) * 100:3.0f}%", fill=COLOR_FG, font=("Consolas", 8))
        kp = S.get("kp") or {}
        if kp.get("classes"):
            if kp.get("active"):
                c.create_text(8, h - 36, anchor="w", text=f"KEY POSTURE  {str(kp['active']).upper()}  {kp.get('p', 0) * 100:.0f} %", fill=key_postures.COLORS.get(kp["active"], COLOR_FG) if key_postures else COLOR_FG, font=("Consolas", 9, "bold"))
            else:
                c.create_text(8, h - 36, anchor="w", text=f"key posture: none  (leading {kp.get('top') or '—'} {kp.get('p', 0) * 100:.0f} %)", fill=COLOR_FG_DIM, font=("Consolas", 8))
        c.create_text(8, h - 22, anchor="w", text=("orientation: " + (S.get("src_R") or "—")), fill=(COLOR_ORANGE if "·" in (S.get("src_R") or "") else COLOR_FG_DIM), font=("Consolas", 8), width=w - 12)
        c.create_text(8, h - 9, anchor="w", text=(S.get("fusion_note") or ""), fill=COLOR_FG_DIM, font=("Consolas", 8))

    def _panel_imu(self, S):
        c = self.p_imu; c.delete("all"); w, h = c.winfo_width(), c.winfo_height()
        if w < 100: return
        self._title(c, "IMUs — forearm & hand orientation", w)
        for k, (name, key, col) in enumerate((("WRIST", "wrist", COLOR_BLUE), ("KNUCKLE", "knuckle", self.VIOLET))):
            x0 = 10 + k * (w / 2.0); d = S.get("imu", {}).get(key)
            c.create_text(x0, 30, anchor="w", text=name, fill=col, font=("Consolas", 9, "bold"))
            if d is None:
                c.create_text(x0, 48, anchor="w", text="off / not streaming", fill=COLOR_FG_DIM, font=("Consolas", 8)); continue
            cx, cy, r = x0 + 20, 70, 18                          # compass: heading of the segment vs body forward
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=COLOR_BG3)
            a = math.radians(d["heading"]); c.create_line(cx, cy, cx + r * math.sin(a), cy - r * math.cos(a), fill=col, width=3)
            c.create_text(cx, cy + r + 8, text="heading", fill=COLOR_FG_DIM, font=("Consolas", 7))
            bx = x0 + 50; c.create_rectangle(bx, 52, bx + 8, 88, outline=COLOR_BG3)         # elevation bar −90..+90
            ey = 70 - 18 * max(-1.0, min(1.0, d["elev"] / 90.0)); c.create_rectangle(bx, min(70, ey), bx + 8, max(70, ey), fill=col, outline="")
            c.create_text(bx + 4, 96, text="tilt", fill=COLOR_FG_DIM, font=("Consolas", 7))
            c.create_text(bx + 16, 50, anchor="w", text=f"tilt {d['elev']:+4.0f}°", fill=COLOR_FG, font=("Consolas", 8))
            c.create_text(bx + 16, 64, anchor="w", text=f"heading {d['heading']:+4.0f}°", fill=COLOR_FG, font=("Consolas", 8))
            c.create_text(bx + 16, 78, anchor="w", text=("calibrated ✓" if d.get("cal") else "uncalibrated"), fill=(COLOR_GREEN if d.get("cal") else COLOR_ORANGE), font=("Consolas", 8))
            c.create_text(x0, 114, anchor="w", text=d.get("q", ""), fill=COLOR_FG_DIM, font=("Consolas", 7))
        b = S.get("bend")
        if b is not None:
            c.create_text(w - 8, 30, anchor="e", text=f"wrist bend {b:.0f}°", fill=COLOR_FG, font=("Consolas", 8))

    def _panel_emg(self, S):
        c = self.p_emg; c.delete("all"); w, h = c.winfo_width(), c.winfo_height()
        if w < 100: return
        rms = S.get("emg_rms"); self._title(c, "EMG — 8 channels (RMS µV)" + ("" if rms is not None else "  · off / not streaming"), w)
        if rms is None: return
        mx = max(60.0, float(max(rms)) * 1.15); bw = (w - 60) / 8.0
        for i, v in enumerate(rms):
            x = 10 + i * bw; hh = (h - 44) * min(1.0, float(v) / mx)
            c.create_rectangle(x, h - 22 - hh, x + bw - 6, h - 22, fill=_blend(COLOR_GREEN, COLOR_BG2, 0.25 + 0.5 * (1 - min(1.0, float(v) / mx))), outline="")
            c.create_text(x + (bw - 6) / 2, h - 12, text=str(i + 1), fill=COLOR_FG_DIM, font=("Consolas", 7))
        c.create_text(w - 8, 30, anchor="e", text=f"max {mx:.0f} µV", fill=COLOR_FG_DIM, font=("Consolas", 7))
        acc = S.get("acc")
        if acc is not None:
            c.create_text(w - 8, 44, anchor="e", text=f"band accel {acc[0]:+.2f} {acc[1]:+.2f} {acc[2]:+.2f} g", fill=COLOR_FG_DIM, font=("Consolas", 7))

    def _panel_pos(self, S, now):
        c = self.p_pos; c.delete("all"); w, h = c.winfo_width(), c.winfo_height()
        if w < 100: return
        src = S.get("src_pos"); col, lab = self.SRC.get(src, (COLOR_FG_DIM, "—"))
        self._title(c, "POSITION — wrist", w)
        c.create_rectangle(w - 96, 3, w - 4, 15, fill=col, outline=""); c.create_text(w - 50, 9, text=lab, fill="#111", font=("Consolas", 8, "bold"))
        xyz = S.get("xyz")
        if xyz is not None:
            x, y, f = xyz
            c.create_text(8, 30, anchor="w", text=f"{abs(x) * 100:3.0f} cm {'right' if x >= 0 else 'left '}   {abs(y) * 100:3.0f} cm {'above' if y >= 0 else 'below'} the neck", fill=COLOR_FG, font=("Consolas", 8))
            c.create_text(8, 44, anchor="w", text=f"{abs(f) * 100:3.0f} cm {'in front' if f >= 0 else 'BEHIND  '}    depth from the eyes {S.get('depth', 0.0) * 100:3.0f} cm", fill=COLOR_FG, font=("Consolas", 8))
        if src == "hybrid":
            c.create_text(8, 60, anchor="w", text=f"{S.get('why', '')} · lost {S.get('lost_t', 0.0):.1f} s · {S.get('n_anchor', 0)} anchors · motion gate {S.get('g', 0.0) * 100:.0f}%", fill=self.TEAL, font=("Consolas", 7))
        elif src == "hold":
            c.create_text(8, 60, anchor="w", text=S.get("why", "holding the last camera position"), fill=COLOR_FG_DIM, font=("Consolas", 7))
        d = S.get("depth"); cd = S.get("cam_depth")
        self._dhist.append((now, cd, d if src != "camera" else None))
        while self._dhist and self._dhist[0][0] < now - 10.0: self._dhist.pop(0)
        L_, R_, T_, B_ = 34.0, w - 10.0, 84.0, h - 16.0
        vals = [v for smp in self._dhist for v in smp[1:] if v is not None]
        lo, hi = 0.0, max(0.6, (max(vals) + 0.05) if vals else 0.6)
        X = lambda t_: L_ + (R_ - L_) * (1.0 - (now - t_) / 10.0); Y = lambda v: B_ - (B_ - T_) * (v - lo) / (hi - lo)
        for g in (0.0, 0.2, 0.4, 0.6, 0.8):
            if g <= hi:
                c.create_line(L_, Y(g), R_, Y(g), fill=COLOR_BG3); c.create_text(L_ - 4, Y(g), anchor="e", text=f"{g * 100:.0f}", fill=COLOR_FG_DIM, font=("Consolas", 7))
        c.create_text(L_, T_ - 7, anchor="w", text="depth from the eyes, cm · last 10 s · pink = camera, teal = predicted", fill=COLOR_FG_DIM, font=("Consolas", 7))
        for idx, col_ in ((1, COLOR_ACCENT), (2, self.TEAL)):
            seg = []; tprev = None
            for smp in self._dhist + [None]:
                v = smp[idx] if smp is not None else None
                if v is None or (seg and smp[0] - tprev > 0.5):
                    if len(seg) >= 4: c.create_line(*seg, fill=col_, width=2)
                    seg = []
                if v is not None: seg += [X(smp[0]), Y(v)]; tprev = smp[0]
        c.create_text(R_, B_ + 8, anchor="e", text="now", fill=COLOR_FG_DIM, font=("Consolas", 7))

    def _panel_head(self, S):
        c = self.p_head; c.delete("all"); w, h = c.winfo_width(), c.winfo_height()
        if w < 100 or h < 40: return
        self._title(c, "HEADSET / BODY", w); hp = S["hmd_p"]
        c.create_text(8, 30, anchor="w", text=f"head position  x {hp[0]:+.2f}   y {hp[1]:+.2f}   z {hp[2]:+.2f} m", fill=COLOR_FG, font=("Consolas", 8))
        c.create_text(8, 44, anchor="w", text=f"head turned {S.get('head_yaw_rel', 0.0):+.0f}° from the body · pitch {S.get('head_pitch', 0.0):+.0f}°", fill=COLOR_FG, font=("Consolas", 8))
        c.create_text(8, 58, anchor="w", text=("body heading: " + ("follows the headset (slow)" if S["flags"]["follow"] else "LOCKED by ⌖ Set forward")), fill=COLOR_FG_DIM, font=("Consolas", 8))
        g = S.get("gate_txt")
        if g and h > 72: c.create_text(8, 72, anchor="w", text=g, fill=S.get("gate_col", COLOR_FG_DIM), font=("Consolas", 8))




def main():
    app = FusionStudioApp()
    app.run()


if __name__ == "__main__":
    main()
