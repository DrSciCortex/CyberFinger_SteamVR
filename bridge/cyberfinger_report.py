# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Decode the CyberFinger's VR input report (GATT characteristic 0xCF01), for every firmware revision.

Each revision keeps the frozen 28-byte prefix (CyberFingerFW_ESP32/src/vr_gatt.h):

  12 bytes   base report: hand, buttons, stick, trigger, battery, seq
  28 bytes   + one quaternion: the primary body IMU
  61 / 79    fixed tail (July 2026 firmware): imu_present, q_body2, q_joint [, a_body1, a_body2, a_joint];
             absent slots hold identity / zero
  29 .. 79   variable tail (v1.3.0 and later): imu_present, then only the slots it flags, in slot order:
             body 1 accel (its quaternion is the prefix's), body 2 quaternion + accel, joint quaternion + accel.
             A CyberFinger with body 1 + joint sends 57 bytes.
  + 1        extension byte (v1.3.3 and later, flagged by imu_present bit 7): buttons beyond the frozen 8 —
             bit 0 the pink power key (a short press, reported as a ~80 ms click). A CyberFinger with body 1 + joint
             sends 58 bytes.

Only 79 bytes fits both tails (all three slots, with accel); there the layout whose quaternions are unit wins.
"""

import math
import struct
from collections import namedtuple

BASE = struct.Struct("<BBhhBBI")
QUAT = struct.Struct("<4f")
ACCEL = struct.Struct("<3h")
PREFIX_SIZE = BASE.size + QUAT.size        # 28
HEADER_SIZE = PREFIX_SIZE + 1              # 29: + imu_present
FIXED_SIZE = HEADER_SIZE + 2 * QUAT.size   # 61
FIXED_ACCEL_SIZE = FIXED_SIZE + 3 * ACCEL.size   # 79

BODY1, BODY2, JOINT = 0x01, 0x02, 0x04     # imu_present bits (VrImuBit)
EXT = 0x80                                 # imu_present bit: the extension byte ends the report (VR_REPORT_EXT)
PINK = 0x01                                # extension byte: the pink power key (VR_BTN2_PWR)
IDENTITY = (1.0, 0.0, 0.0, 0.0)
ZERO = (0, 0, 0)

Report = namedtuple("Report", "hand buttons joy_x joy_y trigger battery seq present quats accels has_accel buttons2",
                    defaults=(0,))
Report.__doc__ = ("quats: body 1, body 2, joint (w, x, y, z), identity when absent. accels: raw sensor-frame "
                  "counts per slot, zero when absent; meaningful only with has_accel. buttons2: the extension "
                  "byte's buttons (PINK), 0 from older firmware.")


def variable_size(present):
    """Length of a v1.3+ report carrying the slots in `present`."""
    return (HEADER_SIZE + (ACCEL.size if present & BODY1 else 0)
            + (QUAT.size + ACCEL.size) * (bool(present & BODY2) + bool(present & JOINT)))


def _is_unit(q):
    n = math.sqrt(sum(c * c for c in q))
    return math.isfinite(n) and abs(n - 1.0) < 0.05


def _variable_tail(data, present, q1):
    quats, accels, off = [q1 if present & BODY1 else IDENTITY, IDENTITY, IDENTITY], [ZERO, ZERO, ZERO], HEADER_SIZE
    if present & BODY1:
        accels[0] = ACCEL.unpack_from(data, off)
        off += ACCEL.size
    for slot, bit in ((1, BODY2), (2, JOINT)):
        if present & bit:
            quats[slot] = QUAT.unpack_from(data, off)
            accels[slot] = ACCEL.unpack_from(data, off + QUAT.size)
            off += QUAT.size + ACCEL.size
    return quats, accels, bool(present)


def _fixed_tail(data, q1):
    quats = [q1, QUAT.unpack_from(data, HEADER_SIZE), QUAT.unpack_from(data, HEADER_SIZE + QUAT.size)]
    if len(data) >= FIXED_ACCEL_SIZE:
        return quats, [ACCEL.unpack_from(data, FIXED_SIZE + i * ACCEL.size) for i in range(3)], True
    return quats, [ZERO, ZERO, ZERO], False


def decode(data):
    """A Report, or None if `data` is shorter than the base report."""
    if len(data) < BASE.size:
        return None
    base = BASE.unpack_from(data)
    if len(data) < PREFIX_SIZE:
        return Report(*base, 0, (IDENTITY,) * 3, (ZERO,) * 3, False)
    q1 = QUAT.unpack_from(data, BASE.size)
    if len(data) < HEADER_SIZE:
        # One quaternion and no presence byte: an all-zero quaternion means the IMU didn't come up.
        present = BODY1 if any(abs(c) > 1e-6 for c in q1) else 0
        return Report(*base, present, (q1 if present else IDENTITY, IDENTITY, IDENTITY), (ZERO,) * 3, False)
    flags = data[PREFIX_SIZE]
    present = flags & (BODY1 | BODY2 | JOINT)
    ext = 1 if flags & EXT else 0                # v1.3.3+: one extension byte after the IMU blocks
    n, nv = len(data), variable_size(present) + ext
    variable = _variable_tail(data, present, q1) if n >= nv else None
    buttons2 = data[nv - 1] if ext and n >= nv else 0
    if n == nv and not (n == FIXED_ACCEL_SIZE and not all(_is_unit(q) for q in variable[0][1:])):
        quats, accels, has_accel = variable      # exactly the v1.3+ length (at 79: if its quaternions are unit)
    elif not ext and n in (FIXED_SIZE, FIXED_ACCEL_SIZE):
        quats, accels, has_accel = _fixed_tail(data, q1)
    elif variable:
        quats, accels, has_accel = variable      # longer than we know: a later revision appending more
    else:                                        # cut short: only the prefix's quaternion is certain
        present &= BODY1
        quats, accels, has_accel = [q1 if present else IDENTITY, IDENTITY, IDENTITY], [ZERO, ZERO, ZERO], False
    return Report(*base, present, tuple(quats), tuple(accels), has_accel, buttons2)
