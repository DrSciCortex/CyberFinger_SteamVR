# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""CyberFinger UDP wire protocol v2 — Python side of src/Protocol.h.

  CFG2  bridge → driver  CyberFinger buttons / stick / trigger, one per BLE report   (pack_cyberfinger)
  CFHS  bridge → driver  fused hand state: /pose/raw + 31 bones               (pack_hand_state)
  CFOP  driver → bridge  HMD pose + headset hand tracking ("optical tap")     (unpack_context)
  CFHP  driver → bridge  haptic vibration request from an application         (unpack_haptic)
  CFIM  bridge → driver  raw CyberFinger IMU slots, one per BLE report: the driver's IMU fusion
                         and its captures                                       (pack_imu)

Little-endian, packed; the driver listens on 127.0.0.1:27015 and sends CFOP to 127.0.0.1:27016.
tests/ keep this module and the C++ header in sync:  python cf_protocol.py --write-vectors <file>
"""

import math
import struct
import sys
import time

VERSION = 1
DRIVER_PORT = 27015
CONTEXT_PORT = 27016
NUM_BONES = 31


def _magic(s):
    return struct.unpack("<I", s.encode("ascii"))[0]


MAGIC_CYBERFINGER = _magic("CFG2")
MAGIC_HAND_STATE = _magic("CFHS")
MAGIC_CONTEXT = _magic("CFOP")
MAGIC_HAPTIC = _magic("CFHP")
MAGIC_IMU = _magic("CFIM")
MAGIC_LEGACY = _magic("CFGP")

# Firmware button bits
BTN_TRIGGER, BTN_GRIP, BTN_C, BTN_D, BTN_E, BTN_MENU, BTN_STICK, BTN_STSEL = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)
# CFG2 buttons2: buttons beyond the first byte
CYBERFINGER_BTN2_PINK = 0x01        # the pink power key (left: SteamVR's system button)

# CFHS flags
HS_POSE_VALID, HS_HAS_BONES, HS_CAMERA_SEES, HS_CALIBRATED = 0x1, 0x2, 0x4, 0x8

# CFOP flags
CTX_CAPTURING = 0x1

# CFIM slot bits (the CyberFinger's own layout; body 2 is the same spot as body 1, other chip)
IMU_BODY1, IMU_BODY2, IMU_JOINT = 0x1, 0x2, 0x4

# Driver output modes (CFOP mode_left / mode_right)
MODE_NAMES = {0: "NONE", 1: "FUSED", 2: "PASSTHROUGH", 3: "NO_POSE", 4: "RELEASED"}

HEADER = struct.Struct("<IBBHIIQ")
BONE = struct.Struct("<8f")
CYBERFINGER_BODY = struct.Struct("<BBhhBBBx")
HAND_BODY = struct.Struct("<3f4f3f3f5f5ff5fIBBBB")
TAP_HEAD = struct.Struct("<BBBBBBxxI3f4f3f3f")
CONTEXT_BODY = struct.Struct("<BBBB3f4f3f3fII")
HAPTIC_BODY = struct.Struct("<3f")
IMU_BODY = struct.Struct("<BB2x12f9h2x")

CYBERFINGER_SIZE = HEADER.size + CYBERFINGER_BODY.size                                       # 34
HAND_STATE_SIZE = HEADER.size + HAND_BODY.size + NUM_BONES * BONE.size           # 1140
TAP_SIZE = TAP_HEAD.size + NUM_BONES * BONE.size                                 # 1056
CONTEXT_SIZE = HEADER.size + CONTEXT_BODY.size + 2 * TAP_SIZE                    # 2200
HAPTIC_SIZE = HEADER.size + HAPTIC_BODY.size                                     # 36
IMU_SIZE = HEADER.size + IMU_BODY.size                                           # 96
assert (CYBERFINGER_SIZE, HAND_STATE_SIZE, TAP_SIZE, CONTEXT_SIZE, HAPTIC_SIZE, IMU_SIZE) == \
    (34, 1140, 1056, 2200, 36, 96)


def now_us():
    return time.perf_counter_ns() // 1000


def _header(magic, hand, seq, flags=0, age_us=0):
    return HEADER.pack(magic, VERSION, hand, flags, seq & 0xFFFFFFFF, int(max(0, age_us)) & 0xFFFFFFFF,
                       now_us() & 0xFFFFFFFFFFFFFFFF)


def stick_to_int16(x, y, deadzone=0.0):
    """Stick in [-1, 1] (+y up) → int16 pair, with an optional radial deadzone."""
    m = math.hypot(x, y)
    if m <= deadzone:
        return 0, 0
    if deadzone > 0:
        s = min(1.0, (m - deadzone) / (1.0 - deadzone)) / m
        x, y = x * s, y * s
    clamp = lambda v: int(round(max(-1.0, min(1.0, v)) * 32767))
    return clamp(x), clamp(y)


def pack_cyberfinger(hand, seq, buttons, trigger, joy_x, joy_y, battery, age_us=0, buttons2=0, resync=0):
    """CFG2. joy_x/joy_y: int16, already centred, +y = up. buttons2: CYBERFINGER_BTN2_* (the CyberFinger's extension byte).
    resync: a count; each new value asks the driver to resync this hand's IMU fusion (Protocol.h
    CyberFingerPacket::resync)."""
    return _header(MAGIC_CYBERFINGER, hand, seq, age_us=age_us) + CYBERFINGER_BODY.pack(
        buttons & 0xFF, max(0, min(255, int(trigger))), int(joy_x), int(joy_y), max(0, min(100, int(battery))),
        buttons2 & 0xFF, resync & 0xFF)


def pack_hand_state(hand, seq, raw_pos, raw_rot, lin_vel=(0, 0, 0), ang_vel=(0, 0, 0),
                    curl=(0,) * 5, splay=(0,) * 5, bones=None, pose_valid=True, camera_sees=False,
                    calibrated=False, pose_conf=1.0, finger_conf=(1,) * 5, optical_seq=0,
                    pos_src=0, rot_src=0, pose_src=0, key_posture=0, age_us=0):
    """CFHS. raw_rot is (w, x, y, z). bones: None (driver synthesizes from curl/splay) or 31 items of
    (px, py, pz, qw, qx, qy, qz) in parent space."""
    flags = ((HS_POSE_VALID if pose_valid else 0) | (HS_HAS_BONES if bones is not None else 0)
             | (HS_CAMERA_SEES if camera_sees else 0) | (HS_CALIBRATED if calibrated else 0))
    body = HAND_BODY.pack(*raw_pos, *raw_rot, *lin_vel, *ang_vel, *curl, *splay, pose_conf, *finger_conf,
                          optical_seq & 0xFFFFFFFF, pos_src, rot_src, pose_src, key_posture)
    if bones is None:
        bone_bytes = BONE.pack(0, 0, 0, 1, 1, 0, 0, 0) * NUM_BONES
    else:
        if len(bones) != NUM_BONES:
            raise ValueError(f"need {NUM_BONES} bones, got {len(bones)}")
        bone_bytes = b"".join(BONE.pack(b[0], b[1], b[2], 1.0, b[3], b[4], b[5], b[6]) for b in bones)
    return _header(MAGIC_HAND_STATE, hand, seq, flags, age_us) + body + bone_bytes


def _unpack_bones(data, offset):
    out = []
    for i in range(NUM_BONES):
        px, py, pz, _pw, qw, qx, qy, qz = BONE.unpack_from(data, offset + i * BONE.size)
        out.append((px, py, pz, qw, qx, qy, qz))
    return out


def unpack_header(data):
    magic, version, hand, flags, seq, age_us, t_send_us = HEADER.unpack_from(data, 0)
    return {"magic": magic, "version": version, "hand": hand, "flags": flags, "seq": seq,
            "age_us": age_us, "t_send_us": t_send_us}


def unpack_context(data):
    """CFOP → dict, or None if the datagram is not a v1 context packet."""
    if len(data) < CONTEXT_SIZE:
        return None
    h = unpack_header(data)
    if h["magic"] != MAGIC_CONTEXT or h["version"] != VERSION:
        return None
    v = CONTEXT_BODY.unpack_from(data, HEADER.size)
    ctx = {"seq": h["seq"], "t_send_us": h["t_send_us"], "capturing": bool(h["flags"] & CTX_CAPTURING),
           "hmd_valid": bool(v[0]),
           "mode": [v[1], v[2]], "tap_hook_ok": bool(v[3]),
           "hmd_pos": v[4:7], "hmd_rot": v[7:11], "hmd_lin_vel": v[11:14], "hmd_ang_vel": v[14:17],
           "applied_hs_seq": [v[17], v[18]], "tap": []}
    off = HEADER.size + CONTEXT_BODY.size
    for _ in range(2):
        t = TAP_HEAD.unpack_from(data, off)
        ctx["tap"].append({
            "pose_valid": bool(t[0]), "skel_valid": bool(t[1]), "tracking_result": t[2], "bone_count": t[3],
            "system_click": bool(t[4]), "source_found": bool(t[5]), "skel_age_us": t[6],
            "raw_pos": t[7:10], "raw_rot": t[10:14], "lin_vel": t[14:17], "ang_vel": t[17:20],
            "bones": _unpack_bones(data, off + TAP_HEAD.size)})
        off += TAP_SIZE
    return ctx


def pack_haptic(hand, seq, duration_s, frequency_hz, amplitude):
    """CFHP (sent by the driver; packed here for tests and tools)."""
    return _header(MAGIC_HAPTIC, hand, seq) + HAPTIC_BODY.pack(duration_s, frequency_hz, amplitude)


def unpack_haptic(data):
    """CFHP → dict(hand, seq, duration_s, frequency_hz, amplitude), or None."""
    if len(data) < HAPTIC_SIZE:
        return None
    h = unpack_header(data)
    if h["magic"] != MAGIC_HAPTIC or h["version"] != VERSION or h["hand"] > 1:
        return None
    duration, frequency, amplitude = HAPTIC_BODY.unpack_from(data, HEADER.size)
    return {"hand": h["hand"], "seq": h["seq"], "duration_s": duration, "frequency_hz": frequency,
            "amplitude": amplitude}


def pack_imu(hand, seq, present, quats, accels=None, age_us=0):
    """CFIM. quats: body 1, body 2, joint as (w, x, y, z); accels: None or three raw (x, y, z) int16 triples."""
    q = [float(c) for quat in quats for c in quat]
    a = [int(c) for acc in accels for c in acc] if accels is not None else [0] * 9
    return _header(MAGIC_IMU, hand, seq, age_us=age_us) + IMU_BODY.pack(
        present & 0xFF, 1 if accels is not None else 0, *q, *a)


def unpack_imu(data):
    """CFIM → dict (tests and tools), or None."""
    if len(data) < IMU_SIZE:
        return None
    h = unpack_header(data)
    if h["magic"] != MAGIC_IMU or h["version"] != VERSION or h["hand"] > 1:
        return None
    v = IMU_BODY.unpack_from(data, HEADER.size)
    return {"header": h, "present": v[0], "has_accel": bool(v[1]),
            "quats": [v[2:6], v[6:10], v[10:14]], "accels": [v[14:17], v[17:20], v[20:23]]}


def unpack_hand_state(data):
    """CFHS → dict (tests and tools)."""
    if len(data) < HAND_STATE_SIZE:
        return None
    h = unpack_header(data)
    v = HAND_BODY.unpack_from(data, HEADER.size)
    return {"header": h, "raw_pos": v[0:3], "raw_rot": v[3:7], "lin_vel": v[7:10], "ang_vel": v[10:13],
            "curl": v[13:18], "splay": v[18:23], "pose_conf": v[23], "finger_conf": v[24:29],
            "optical_seq": v[29], "pos_src": v[30], "rot_src": v[31], "pose_src": v[32], "key_posture": v[33],
            "bones": _unpack_bones(data, HEADER.size + HAND_BODY.size)}


# ── golden vectors shared with tests/driver_tests.cpp ─────────────────────────
def golden_vectors():
    """Deterministic packets (t_send_us zeroed) whose field values the C++ test checks."""
    def fixed_time(pkt):
        return pkt[:16] + struct.pack("<Q", 0) + pkt[24:]
    cyberfinger = fixed_time(pack_cyberfinger(1, 7, BTN_MENU | BTN_TRIGGER, 200, -1000, 32767, 88, age_us=5))
    bones = [(0.01 * i, -0.02 * i, 0.03 * i, 1.0, 0.0, 0.0, 0.0) for i in range(NUM_BONES)]
    hand = fixed_time(pack_hand_state(0, 42, (0.1, 1.2, -0.3), (0.5, 0.5, -0.5, 0.5), lin_vel=(1, 2, 3),
                                      ang_vel=(-1, -2, -3), curl=(0.1, 0.2, 0.3, 0.4, 0.5),
                                      splay=(-0.5, 0, 0.25, 0, 0.5), bones=bones, camera_sees=True,
                                      optical_seq=99, key_posture=3, age_us=1234))
    ctx = bytearray(CONTEXT_SIZE)
    HEADER.pack_into(ctx, 0, MAGIC_CONTEXT, VERSION, 0xFF, CTX_CAPTURING, 5, 0, 0)
    CONTEXT_BODY.pack_into(ctx, HEADER.size, 1, 1, 2, 1, 0.1, 1.6, 0.2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 42, 0)
    off = HEADER.size + CONTEXT_BODY.size
    TAP_HEAD.pack_into(ctx, off + TAP_SIZE, 1, 1, 200, 31, 1, 1, 12345, -0.2, 1.1, -0.4, 1, 0, 0, 0,
                       0, 0, 0, 0, 0, 0)
    BONE.pack_into(ctx, off + TAP_SIZE + TAP_HEAD.size + 5 * BONE.size, 0.5, 0.25, 0.125, 1, 1, 0, 0, 0)
    haptic = fixed_time(pack_haptic(1, 3, 0.25, 160.0, 0.75))
    imu = fixed_time(pack_imu(0, 1000, IMU_BODY1 | IMU_JOINT,
                              [(1, 0, 0, 0), (1, 0, 0, 0), (0.5, -0.5, 0.5, -0.5)],
                              [(10, -20, 2048), (0, 0, 0), (-7, 300, -2048)], age_us=4321))
    return [cyberfinger, hand, bytes(ctx), haptic, imu]


def write_vectors(path):
    with open(path, "wb") as f:
        for pkt in golden_vectors():
            f.write(struct.pack("<I", len(pkt)) + pkt)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-vectors":
        write_vectors(sys.argv[2])
        print("wrote", sys.argv[2])
    else:
        print(__doc__)
