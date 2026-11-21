# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/cyberfinger_report.py: every firmware revision of the VR input report.
python -m unittest discover -s tests"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cyberfinger_report as gr  # noqa: E402

Q1 = (0.70710677, 0.70710677, 0.0, 0.0)
Q2 = (0.0, 0.0, 1.0, 0.0)
QJ = (0.5, -0.5, 0.5, -0.5)
A1, A2, AJ = (10, -20, 2048), (1, 2, 3), (-7, 300, -2048)


def base():
    return gr.BASE.pack(1, 0x21, 100, -200, 128, 90, 4242)


def q(v):
    return gr.QUAT.pack(*v)


def a(v):
    return gr.ACCEL.pack(*v)


def fixed(present, accel):
    """July 2026 firmware: fixed tail, absent slots filled with identity / zero."""
    q2 = Q2 if present & gr.BODY2 else gr.IDENTITY
    qj = QJ if present & gr.JOINT else gr.IDENTITY
    pkt = base() + q(Q1) + bytes([present]) + q(q2) + q(qj)
    if accel:
        pkt += a(A1) + a(A2 if present & gr.BODY2 else gr.ZERO) + a(AJ if present & gr.JOINT else gr.ZERO)
    return pkt


def variable(present):
    """v1.3+ firmware: only the flagged slots, in slot order."""
    pkt = base() + q(Q1 if present & gr.BODY1 else gr.IDENTITY) + bytes([present])
    if present & gr.BODY1:
        pkt += a(A1)
    if present & gr.BODY2:
        pkt += q(Q2) + a(A2)
    if present & gr.JOINT:
        pkt += q(QJ) + a(AJ)
    return pkt


def extended(present, buttons2):
    """v1.3.3+ firmware: the variable tail, then the extension byte (flagged by imu_present bit 7)."""
    pkt = bytearray(variable(present))
    pkt[gr.PREFIX_SIZE] |= gr.EXT
    return bytes(pkt) + bytes([buttons2])


class CyberFingerReportTest(unittest.TestCase):
    def assertQuat(self, got, want):
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=6)

    def test_extension_byte(self):
        for present in (0, gr.BODY1, gr.BODY1 | gr.JOINT, gr.BODY1 | gr.BODY2 | gr.JOINT, gr.BODY2 | gr.JOINT):
            for b2 in (0, gr.PINK):
                pkt = extended(present, b2)
                self.assertNotIn(len(pkt), (gr.FIXED_SIZE, gr.FIXED_ACCEL_SIZE))   # never a legacy layout
                r = gr.decode(pkt)
                self.assertEqual((r.present, r.buttons2), (present, b2))
                if present & gr.JOINT:
                    self.assertQuat(r.quats[2], QJ)
                    self.assertEqual(r.accels[2], AJ)
        self.assertEqual(len(extended(gr.BODY1 | gr.JOINT, 0)), 58)
        self.assertEqual(gr.decode(variable(gr.BODY1 | gr.JOINT)).buttons2, 0)         # older firmware

    def test_base_fields(self):
        r = gr.decode(base())
        self.assertEqual((r.hand, r.buttons, r.joy_x, r.joy_y, r.trigger, r.battery, r.seq),
                         (1, 0x21, 100, -200, 128, 90, 4242))
        self.assertEqual((r.present, r.has_accel), (0, False))
        self.assertIsNone(gr.decode(b"\0" * 11))

    def test_single_quaternion(self):
        r = gr.decode(base() + q(Q1))
        self.assertEqual(r.present, gr.BODY1)
        self.assertQuat(r.quats[0], Q1)
        self.assertEqual(gr.decode(base() + q((0, 0, 0, 0))).present, 0)     # IMU didn't come up

    def test_fixed_tail(self):
        r = gr.decode(fixed(gr.BODY1 | gr.JOINT, accel=False))
        self.assertEqual((len(fixed(gr.BODY1 | gr.JOINT, False)), r.present, r.has_accel), (61, 0x5, False))
        self.assertQuat(r.quats[2], QJ)
        r = gr.decode(fixed(gr.BODY1 | gr.JOINT, accel=True))
        self.assertEqual((r.present, r.has_accel), (0x5, True))
        self.assertQuat(r.quats[2], QJ)
        self.assertEqual((r.accels[0], r.accels[2]), (A1, AJ))

    def test_fixed_tail_all_slots(self):
        """79 bytes with all three slots: the length of both tails; the old layout must still win."""
        r = gr.decode(fixed(0x7, accel=True))
        self.assertEqual((r.present, r.has_accel), (0x7, True))
        self.assertQuat(r.quats[1], Q2)
        self.assertQuat(r.quats[2], QJ)
        self.assertEqual(r.accels, (A1, A2, AJ))

    def test_variable_tail_body_and_joint(self):
        """v1.3 default (body 2 dropped): 57 bytes, which the fixed-tail reader took for the 28-byte report."""
        pkt = variable(gr.BODY1 | gr.JOINT)
        self.assertEqual(len(pkt), 57)
        r = gr.decode(pkt)
        self.assertEqual((r.present, r.has_accel), (0x5, True))
        self.assertQuat(r.quats[0], Q1)
        self.assertQuat(r.quats[1], gr.IDENTITY)
        self.assertQuat(r.quats[2], QJ)
        self.assertEqual(r.accels, (A1, gr.ZERO, AJ))

    def test_variable_tail_every_combination(self):
        for present in range(8):
            pkt = variable(present)
            self.assertEqual(len(pkt), gr.variable_size(present))
            r = gr.decode(pkt)
            self.assertEqual(r.present, present, f"present {present}")
            if present & gr.BODY2:
                self.assertQuat(r.quats[1], Q2)
                self.assertEqual(r.accels[1], A2)
            if present & gr.JOINT:
                self.assertQuat(r.quats[2], QJ)
                self.assertEqual(r.accels[2], AJ)

    def test_longer_and_truncated(self):
        r = gr.decode(variable(0x5) + b"\0\0")                              # a later revision appending more
        self.assertEqual(r.present, 0x5)
        self.assertQuat(r.quats[2], QJ)
        r = gr.decode(variable(0x5)[:45])                                   # cut short: only body 1 is certain
        self.assertEqual((r.present, r.has_accel), (gr.BODY1, False))
        self.assertQuat(r.quats[0], Q1)
        self.assertQuat(r.quats[2], gr.IDENTITY)


if __name__ == "__main__":
    unittest.main()
