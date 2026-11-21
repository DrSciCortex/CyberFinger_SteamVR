# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge_linux/driver_skeleton.py: the Linux bridge's Skeleton tab, fed from the driver's context
stream (CFOP) instead of OpenVR. Synthetic packets, laid out as src/Protocol.h's ContextPacket.
python -m unittest discover -s tests"""

import math
import os
import socket
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, os.path.join(ROOT, "bridge_linux"))
import cf_protocol as cfp  # noqa: E402
import driver_skeleton as ds  # noqa: E402
from driver_link import DriverLink  # noqa: E402

S = math.sqrt(0.5)
IDENT = (1.0, 0.0, 0.0, 0.0)
Z90 = (S, 0.0, 0.0, S)            # 90° about +z: x → y
Y90 = (S, 0.0, S, 0.0)            # 90° about +y (a left turn): -z → -x


def hand_bones():
    """31 parent-space bones (px, py, pz, qw, qx, qy, qz): the wrist turned 90° about z, 3 cm behind the pose;
    an index finger that curls 90° at its second joint; the other fingers straight along the wrist's x."""
    bones = [(0.0, 0.0, 0.0) + IDENT for _ in range(cfp.NUM_BONES)]
    bones[1] = (0.0, 0.0, 0.03) + Z90
    for first in (2, 6, 11, 16, 21):
        for j in range(first, first + (4 if first == 2 else 5)):
            bones[j] = (0.02, 0.0, 0.0) + IDENT
    bones[6] = (0.10, 0.0, 0.0) + IDENT
    bones[7] = (0.05, 0.0, 0.0) + Z90
    bones[8] = (0.03, 0.0, 0.0) + IDENT
    return bones


def context_packet(seq=1, hmd_valid=1, hmd_pos=(0.0, 1.6, 0.0), hmd_rot=Y90, hmd_vel=(0.0, 0.0, 0.0),
                   modes=(1, 2), hands=()):
    """A CFOP datagram. hands: per hand None, or a dict with pos, rot, vel, bones, skel_valid, pose_valid."""
    pkt = bytearray(cfp.CONTEXT_SIZE)
    cfp.HEADER.pack_into(pkt, 0, cfp.MAGIC_CONTEXT, cfp.VERSION, 0xFF, 0, seq, 0, 0)
    cfp.CONTEXT_BODY.pack_into(pkt, cfp.HEADER.size, hmd_valid, modes[0], modes[1], 1, *hmd_pos, *hmd_rot,
                               *hmd_vel, 0, 0, 0, 0, 0)
    off = cfp.HEADER.size + cfp.CONTEXT_BODY.size
    for hand in range(2):
        h = hands[hand] if hand < len(hands) else None
        if h is not None:
            cfp.TAP_HEAD.pack_into(pkt, off, h.get("pose_valid", 1), h.get("skel_valid", 1), 200, cfp.NUM_BONES, 0, 1,
                                   0, *h["pos"], *h.get("rot", IDENT), *h.get("vel", (0.0, 0.0, 0.0)), 0, 0, 0)
            for i, b in enumerate(h.get("bones") or hand_bones()):
                cfp.BONE.pack_into(pkt, off + cfp.TAP_HEAD.size + i * cfp.BONE.size, b[0], b[1], b[2], 1.0, *b[3:7])
        off += cfp.TAP_SIZE
    return bytes(pkt)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class VecTest(unittest.TestCase):
    def assertVec(self, got, want, places=5):
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=places)


class ForwardKinematicsTest(VecTest):
    def test_joint_positions_follow_the_hierarchy(self):
        joints = ds.model_joints(hand_bones())
        self.assertEqual(len(joints), 26)                 # root, wrist, 24 finger joints: the aux bones are left out
        self.assertVec(joints[0], (0.0, 0.0, 0.0))
        self.assertVec(joints[1], (0.0, 0.0, 0.03))
        self.assertVec(joints[6], (0.0, 0.10, 0.03))      # the wrist's x is the pose's y
        self.assertVec(joints[7], (0.0, 0.15, 0.03))
        self.assertVec(joints[8], (-0.03, 0.15, 0.03))    # past the curl: 180° from the pose's x
        self.assertVec(joints[10], (-0.07, 0.15, 0.03))
        self.assertVec(joints[5], (0.0, 0.08, 0.03))      # thumb: four joints off the wrist
        self.assertVec(joints[25], (0.0, 0.10, 0.03))     # pinky tip hangs off the wrist, not off the ring finger

    def test_unset_rotations_count_as_identity(self):
        bones = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)] * cfp.NUM_BONES     # all zero: no rotation given
        bones[6] = (0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.assertVec(ds.model_joints(bones)[6], (0.1, 0.0, 0.0))

    def test_needs_the_26_joints(self):
        self.assertIsNone(ds.model_joints(None))
        self.assertIsNone(ds.model_joints(hand_bones()[:20]))


class SourceTest(VecTest):
    def source(self):
        return ds.DriverSkeletonSource(lambda: None)

    def test_hand_in_front_of_a_turned_head(self):
        # The head looks along -x (turned 90° left); the right hand is half a metre along that, moving away.
        pkt = context_packet(hands=(None, {"pos": (-0.5, 1.6, 0.0), "rot": Z90, "vel": (-1.0, 0.0, 0.0)}))
        src = self.source()
        src.feed(cfp.unpack_context(pkt))
        self.assertIsNone(src.hands[0])
        self.assertIsNone(src.pose_info[0])
        self.assertEqual(len(src.hands[1]), 26)
        self.assertVec(src.hands[1][6], (0.0, 0.10, 0.03))
        rot, local, dist, local_v = src.pose_info[1]
        self.assertVec(local, (0.0, 0.0, -0.5))           # straight ahead: the head's -z
        self.assertAlmostEqual(dist, 0.5, places=5)
        self.assertVec(local_v, (0.0, 0.0, -1.0))
        # The hand's rotation as matrix rows, in the panel's world: up stays up, the user's heading (-x here) is
        # +z, the user's left +x. The hand's x axis points up, its y axis to the user's left (-z here).
        for row, want in zip(rot, ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))):
            self.assertVec(row, want)
        self.assertEqual(src.modes, ["FUSED", "PASSTHROUGH"])

    def test_hand_rotation_is_relative_to_the_users_heading(self):
        # The same hand before the same head, the pair turned to any heading and the head nodding: one rotation.
        def yaw(deg, pitch_deg=0.0):
            y, p = math.radians(deg) / 2, math.radians(pitch_deg) / 2
            return ds._quat_mul((math.cos(y), 0.0, math.sin(y), 0.0), (math.cos(p), math.sin(p), 0.0, 0.0))
        want = ds.relative_pose((0.0, 1.6, 0.0), IDENT, (0.0, 1.3, -0.4), Z90)[0]
        for row, expect in zip(want, ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))):
            self.assertVec(row, expect)                   # facing -z: a half turn about the vertical from tracking space
        for deg in (30.0, 90.0, 180.0, -135.0):
            for pitch in (0.0, -60.0, 40.0):
                got = ds.relative_pose((0.0, 1.6, 0.0), yaw(deg, pitch), (0.0, 1.3, -0.4),
                                       ds._quat_mul(yaw(deg), Z90))[0]
                for g, w in zip(got, want):
                    self.assertVec(g, w)
        on_its_side = (S, 0.0, 0.0, S)                    # no heading to take: the tracking space's
        for g, w in zip(ds.relative_pose((0.0, 1.6, 0.0), on_its_side, (0.0, 1.3, -0.4), Z90)[0], want):
            self.assertVec(g, w)

    def test_hand_to_the_right_and_above(self):
        pkt = context_packet(hmd_rot=IDENT, hmd_vel=(0.0, 0.0, -1.0),
                             hands=({"pos": (0.3, 2.0, 0.0), "vel": (0.0, 0.0, -1.0)},))
        src = self.source()
        src.feed(cfp.unpack_context(pkt))
        _rot, local, dist, local_v = src.pose_info[0]
        self.assertVec(local, (0.3, 0.4, 0.0))
        self.assertAlmostEqual(dist, 0.5, places=5)
        self.assertVec(local_v, (0.0, 0.0, 0.0))          # moving with the head

    def test_validity(self):
        src = self.source()
        hand = {"pos": (0.0, 1.0, -0.3)}
        # no skeleton, but a pose: the dome still shows the hand
        src.feed(cfp.unpack_context(context_packet(hands=(dict(hand, skel_valid=0),))))
        self.assertIsNone(src.hands[0])
        self.assertIsNotNone(src.pose_info[0])
        # a skeleton implies its pose, flagged or not
        src.feed(cfp.unpack_context(context_packet(hands=(dict(hand, pose_valid=0),))))
        self.assertIsNotNone(src.hands[0])
        self.assertIsNotNone(src.pose_info[0])
        # neither
        src.feed(cfp.unpack_context(context_packet(hands=(dict(hand, pose_valid=0, skel_valid=0),))))
        self.assertEqual((src.hands[0], src.pose_info[0]), (None, None))
        # without the headset's pose there's no head-relative position: the panel falls back to its auto-fit
        for ctx in (context_packet(hmd_valid=0, hands=(hand,)), context_packet(hmd_rot=(0.0,) * 4, hands=(hand,))):
            src.feed(cfp.unpack_context(ctx))
            self.assertIsNotNone(src.hands[0])
            self.assertIsNone(src.pose_info[0])
        # an unset hand rotation is no pose either
        src.feed(cfp.unpack_context(context_packet(hands=(dict(hand, rot=(0.0,) * 4),))))
        self.assertIsNone(src.pose_info[0])

    def test_unknown_mode_and_no_packet(self):
        src = self.source()
        src.feed(cfp.unpack_context(context_packet(modes=(4, 9))))
        self.assertEqual(src.modes, ["RELEASED", "9"])
        src.feed(None)
        self.assertEqual((src.hands, src.pose_info, src.modes), ([None, None],) * 3)

    def test_the_golden_context_vector(self):
        # tests/protocol_vectors.bin's CFOP, as the C++ driver packs it: the right hand, skeleton and pose valid
        src = self.source()
        src.feed(cfp.unpack_context(cfp.golden_vectors()[2]))
        self.assertIsNone(src.hands[0])
        self.assertEqual(len(src.hands[1]), 26)
        self.assertVec(src.pose_info[1][1], (-0.3, -0.5, -0.6))
        self.assertEqual(src.modes, ["FUSED", "PASSTHROUGH"])


class LinkTest(unittest.TestCase):
    def test_status_follows_the_driver_link(self):
        port = free_port()
        holder = [None]
        src = ds.DriverSkeletonSource(lambda: holder[0])
        src.update()
        self.assertEqual(src.status, "start the bridge in VR mode")
        self.assertEqual(src.modes, [None, None])

        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))
        try:
            holder[0] = DriverLink(port=port)
            self.assertFalse(holder[0].start())
            src.update()
            self.assertEqual(src.status, f"UDP {port} is busy")
        finally:
            blocker.close()

        link = holder[0] = DriverLink(port=port)
        self.assertTrue(link.start())
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def send(pkt):
            before = link._context_raw
            tx.sendto(pkt, ("127.0.0.1", port))
            deadline = time.time() + 2.0
            while link._context_raw is before and time.time() < deadline:
                time.sleep(0.005)
            src.update()

        try:
            src.update()
            self.assertEqual(src.status, "WiVRn driver not running")
            send(context_packet(seq=1))
            self.assertEqual(src.status, "hands not in view")
            self.assertEqual(src.modes, ["FUSED", "PASSTHROUGH"])
            send(context_packet(seq=2, hands=({"pos": (0.0, 1.2, -0.4)},)))
            self.assertEqual(src.status, "connected")
            self.assertEqual(len(src.hands[0]), 26)
            self.assertIsNone(src.hands[1])
            hands = src.hands
            src.update()                                   # the same packet: nothing recomputed
            self.assertIs(src.hands, hands)
            link._context_time -= 1.0                      # the driver went quiet
            src.update()
            self.assertEqual(src.status, "WiVRn driver not running")
            self.assertEqual(src.hands, [None, None])
        finally:
            tx.close()
            link.stop()
        holder[0] = None
        src.update()
        self.assertEqual(src.status, "start the bridge in VR mode")


if __name__ == "__main__":
    unittest.main()
