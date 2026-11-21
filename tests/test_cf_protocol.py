# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/cf_protocol.py.   python -m unittest discover -s tests"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cf_protocol as cfp  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def test_sizes_match_protocol_h(self):
        self.assertEqual(cfp.HEADER.size, 24)
        self.assertEqual(cfp.BONE.size, 32)
        self.assertEqual(cfp.CYBERFINGER_SIZE, 34)
        self.assertEqual(cfp.HAND_STATE_SIZE, 1140)
        self.assertEqual(cfp.TAP_SIZE, 1056)
        self.assertEqual(cfp.CONTEXT_SIZE, 2200)
        self.assertEqual(cfp.HAPTIC_SIZE, 36)
        self.assertEqual(cfp.IMU_SIZE, 96)

    def test_magics(self):
        self.assertEqual(cfp.MAGIC_LEGACY, 0x50474643)       # the value the old driver used for 'CFGP'
        self.assertEqual(struct.pack("<I", cfp.MAGIC_CYBERFINGER), b"CFG2")

    def test_cyberfinger(self):
        pkt = cfp.pack_cyberfinger(1, 3, cfp.BTN_MENU, 300, 100, -200, 150)
        self.assertEqual(len(pkt), cfp.CYBERFINGER_SIZE)
        h = cfp.unpack_header(pkt)
        self.assertEqual((h["magic"], h["hand"], h["seq"]), (cfp.MAGIC_CYBERFINGER, 1, 3))
        buttons, trigger, jx, jy, battery, buttons2, resync = cfp.CYBERFINGER_BODY.unpack_from(pkt, cfp.HEADER.size)
        self.assertEqual((buttons, trigger, jx, jy, battery, buttons2, resync),
                         (cfp.BTN_MENU, 255, 100, -200, 100, 0, 0))
        # the IMU fusion resync count: the byte after buttons2 (CyberFingerPacket::resync), wrapping at 256
        pkt = cfp.pack_cyberfinger(0, 5, 0, 0, 0, 0, 50, resync=257)
        self.assertEqual(len(pkt), cfp.CYBERFINGER_SIZE)
        self.assertEqual(pkt[cfp.HEADER.size + 8], 1)
        # the pink button: the byte after battery_pct (Protocol.h CyberFingerPacket::buttons2)
        pkt = cfp.pack_cyberfinger(0, 4, 0, 0, 0, 0, 50, buttons2=cfp.CYBERFINGER_BTN2_PINK)
        self.assertEqual(pkt[cfp.HEADER.size + 7], cfp.CYBERFINGER_BTN2_PINK)

    def test_stick(self):
        self.assertEqual(cfp.stick_to_int16(0.05, 0.05, 0.12), (0, 0))
        self.assertEqual(cfp.stick_to_int16(1.0, 0.0, 0.12), (32767, 0))
        self.assertEqual(cfp.stick_to_int16(0.0, -1.0), (0, -32767))
        x, _ = cfp.stick_to_int16(0.56, 0.0, 0.12)
        self.assertAlmostEqual(x / 32767, 0.5, places=3)

    def test_hand_state_roundtrip(self):
        bones = [(0.01 * i, 0.0, -0.01 * i, 1.0, 0.0, 0.0, 0.0) for i in range(cfp.NUM_BONES)]
        pkt = cfp.pack_hand_state(0, 9, (1, 2, 3), (1, 0, 0, 0), curl=(0.1, 0.2, 0.3, 0.4, 0.5),
                                  bones=bones, camera_sees=True, optical_seq=77)
        self.assertEqual(len(pkt), cfp.HAND_STATE_SIZE)
        d = cfp.unpack_hand_state(pkt)
        self.assertEqual(d["header"]["flags"], cfp.HS_POSE_VALID | cfp.HS_HAS_BONES | cfp.HS_CAMERA_SEES)
        self.assertEqual(d["optical_seq"], 77)
        self.assertAlmostEqual(d["curl"][4], 0.5, places=6)
        self.assertAlmostEqual(d["bones"][30][2], -0.3, places=6)

    def test_hand_state_without_bones(self):
        d = cfp.unpack_hand_state(cfp.pack_hand_state(1, 1, (0, 0, 0), (1, 0, 0, 0)))
        self.assertEqual(d["header"]["flags"], cfp.HS_POSE_VALID)

    def test_context_unpack(self):
        ctx = cfp.unpack_context(cfp.golden_vectors()[2])
        self.assertTrue(ctx["hmd_valid"])
        self.assertTrue(ctx["capturing"])
        self.assertEqual(ctx["mode"], [1, 2])
        self.assertTrue(ctx["tap_hook_ok"])
        self.assertAlmostEqual(ctx["hmd_pos"][1], 1.6, places=6)
        right = ctx["tap"][1]
        self.assertTrue(right["pose_valid"] and right["system_click"] and right["source_found"])
        self.assertEqual((right["tracking_result"], right["bone_count"], right["skel_age_us"]), (200, 31, 12345))
        self.assertAlmostEqual(right["bones"][5][2], 0.125, places=6)
        self.assertIsNone(cfp.unpack_context(b"\0" * 10))

    def test_haptic(self):
        d = cfp.unpack_haptic(cfp.pack_haptic(0, 11, 0.02, 320.0, 0.5))
        self.assertEqual((d["hand"], d["seq"]), (0, 11))
        self.assertAlmostEqual(d["duration_s"], 0.02, places=6)
        self.assertAlmostEqual(d["frequency_hz"], 320.0, places=3)
        self.assertAlmostEqual(d["amplitude"], 0.5, places=6)
        self.assertIsNone(cfp.unpack_haptic(cfp.golden_vectors()[0]))    # a CFG2 packet is not a haptic

    def test_imu(self):
        quats = [(1, 0, 0, 0), (0, 1, 0, 0), (0.5, 0.5, 0.5, 0.5)]
        pkt = cfp.pack_imu(1, 77, cfp.IMU_BODY2 | cfp.IMU_JOINT, quats, [(1, 2, 3), (4, 5, 6), (-7, -8, -9)],
                           age_us=900)
        self.assertEqual(len(pkt), cfp.IMU_SIZE)
        d = cfp.unpack_imu(pkt)
        self.assertEqual((d["header"]["hand"], d["header"]["seq"], d["header"]["age_us"]), (1, 77, 900))
        self.assertEqual(d["present"], cfp.IMU_BODY2 | cfp.IMU_JOINT)
        self.assertTrue(d["has_accel"])
        self.assertAlmostEqual(d["quats"][2][3], 0.5, places=6)
        self.assertEqual(d["accels"][2], (-7, -8, -9))
        self.assertFalse(cfp.unpack_imu(cfp.pack_imu(0, 1, cfp.IMU_BODY1, quats))["has_accel"])
        self.assertIsNone(cfp.unpack_imu(cfp.golden_vectors()[0]))       # a CFG2 packet is not an IMU packet

    def test_golden_vectors_file_is_current(self):
        """tests/protocol_vectors.bin (read by driver_tests.cpp) must match cf_protocol.py."""
        with open(os.path.join(ROOT, "tests", "protocol_vectors.bin"), "rb") as f:
            data = f.read()
        expected = b"".join(struct.pack("<I", len(p)) + p for p in cfp.golden_vectors())
        self.assertEqual(data, expected, "regenerate: python bridge/cf_protocol.py --write-vectors "
                                         "tests/protocol_vectors.bin")


if __name__ == "__main__":
    unittest.main()
