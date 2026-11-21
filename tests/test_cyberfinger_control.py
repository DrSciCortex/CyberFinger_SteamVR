# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/cyberfinger_control.py: the haptic command (firmware src/vr_gatt.h VrGattHapticCommand), the
per-hand coalescing writer, and the bleak write path of the Linux bridge.
python -m unittest discover -s tests"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cyberfinger_control as gc  # noqa: E402


class PackTest(unittest.TestCase):
    def test_layout(self):
        # cmd, amplitude, duration_ms LE, frequency_hz LE: 6 bytes, as the firmware's packed struct
        self.assertEqual(gc.pack_haptic(0.25, 160.0, 1.0), bytes([0x10, 255, 250, 0, 160, 0]))
        self.assertEqual(len(gc.pack_haptic(0, 0, 0.5)), 6)
        self.assertEqual(gc.pack_haptic_stop(), bytes([0x10, 0, 0, 0, 0, 0]))

    def test_ranges(self):
        self.assertEqual(gc.pack_haptic(0.001, 0, 0.001)[1], 1)       # faint but not 0 (0 stops the motor)
        self.assertEqual(gc.pack_haptic(0.1, 0, 0.0)[1], 0)
        self.assertEqual(gc.pack_haptic(0.1, 0, 3.0)[1], 255)
        self.assertEqual(gc.HAPTIC.unpack(gc.pack_haptic(100.0, 1e6, 0.5))[2:], (65535, 65535))
        self.assertEqual(gc.HAPTIC.unpack(gc.pack_haptic(-1.0, -5, 0.5))[2:], (0, 0))


class SenderTest(unittest.TestCase):
    def test_coalesces_while_a_write_is_in_flight(self):
        written = []
        release = None

        async def fake_write(char, payload):
            written.append((char, gc.HAPTIC.unpack(payload)))
            await release.wait()
            return True

        async def run():
            nonlocal release
            release = asyncio.Event()
            s = gc.HapticSender()
            s.attach(1, "right-char")
            self.assertTrue(s.available(1))
            self.assertFalse(s.available(0))
            self.assertFalse(s.request(0, 0.1, 0, 1.0))                  # the left CyberFinger isn't attached
            s.request(1, 0.010, 0, 0.5)                                  # goes out at once
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            s.request(1, 0.300, 0, 0.2)                                  # these two, while the first is in flight,
            s.request(1, 0.050, 90, 0.9)                                 # merge: latest amplitude, longest duration
            await asyncio.sleep(0.01)
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            return written

        orig = gc.write_without_response
        gc.write_without_response = fake_write
        try:
            w = asyncio.run(run())
        finally:
            gc.write_without_response = orig
        self.assertEqual([c for c, _ in w], ["right-char", "right-char"])
        self.assertEqual(w[0][1], (0x10, 128, 10, 0))
        self.assertEqual(w[1][1], (0x10, 230, 300, 90))


class FakeBleakClient:
    """bleak's BleakClient.write_gatt_char, recorded."""

    def __init__(self, fail=None):
        self.writes, self.fail = [], fail

    async def write_gatt_char(self, char_specifier, data, response=None):
        if self.fail:
            raise self.fail
        self.writes.append((char_specifier, bytes(data), response))


class FakeBleakChar:
    def __init__(self, properties):
        self.uuid, self.properties = gc.CONTROL_UUID, properties


class BleakTest(unittest.TestCase):
    """The Linux bridge's path: the control characteristic of a bleak client (BleakControl)."""

    def test_writes_without_response_to_the_control_characteristic(self):
        client = FakeBleakClient()
        payload = gc.pack_haptic(0.25, 160.0, 1.0)
        self.assertTrue(asyncio.run(gc.write_without_response(gc.BleakControl(client), payload)))
        self.assertEqual(client.writes, [(gc.CONTROL_UUID, payload, False)])

    def test_acknowledged_write_only_when_the_characteristic_takes_no_other(self):
        for properties, response in ((["write-without-response", "write"], False), (["write-without-response"], False),
                                     (["write"], True), ([], False)):
            client, char = FakeBleakClient(), FakeBleakChar(properties)
            asyncio.run(gc.BleakControl(client, char).write(b"\x10\x00"))
            self.assertEqual(client.writes, [(char, b"\x10\x00", response)])

    def test_sender_reaches_the_cyberfinger(self):
        client, logs = FakeBleakClient(), []

        async def run():
            s = gc.HapticSender(log=logs.append)
            s.attach(0, gc.BleakControl(client))
            self.assertTrue(s.request(0, 0.1, 80, 0.5))
            for _ in range(10):
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual([(c, gc.HAPTIC.unpack(p), r) for c, p, r in client.writes],
                         [(gc.CONTROL_UUID, (0x10, 128, 100, 80), False)])
        self.assertEqual(logs, ["Haptics: forwarding to the left CyberFinger"])

    def test_a_failed_write_is_logged_once_and_the_sender_goes_on(self):
        client, logs = FakeBleakClient(fail=RuntimeError("Not connected")), []

        async def run():
            s = gc.HapticSender(log=logs.append)
            s.attach(1, gc.BleakControl(client))
            for _ in range(2):
                s.request(1, 0.1, 0, 1.0)
                for _ in range(10):
                    await asyncio.sleep(0)
            client.fail = None
            s.request(1, 0.1, 0, 1.0)
            for _ in range(10):
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(len([m for m in logs if "failed" in m]), 1)
        self.assertEqual(len(client.writes), 1)


if __name__ == "__main__":
    unittest.main()
