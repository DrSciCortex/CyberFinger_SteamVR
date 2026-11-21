# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/driver_link.py and bridge/haptics_view.py (no Tk, no SteamVR)."""

import os
import socket
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cf_protocol as cfp  # noqa: E402
from driver_link import DriverLink, HapticTracker  # noqa: E402
from haptics_view import describe, draw_haptic_meter  # noqa: E402


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeCanvas:
    """Records draw calls; checks coordinates are numbers, as Tk requires."""

    def __init__(self):
        self.calls = []

    def _rec(self, kind, *coords, **kw):
        for v in coords:
            assert isinstance(v, (int, float)), (kind, v)
        self.calls.append((kind, kw))

    def create_oval(self, *a, **kw): self._rec("oval", *a, **kw)
    def create_line(self, *a, **kw): self._rec("line", *a, **kw)
    def create_text(self, *a, **kw): self._rec("text", *a, **kw)


class HapticTrackerTest(unittest.TestCase):
    def test_lifecycle(self):
        t = HapticTracker()
        self.assertFalse(t.state(0)["on"])
        t.add(1, 0.2, 160.0, 0.8, t=100.0)
        st = t.state(1, now=100.1)
        self.assertTrue(st["on"])
        self.assertAlmostEqual(st["level"], 0.8)
        self.assertAlmostEqual(st["remaining"], 0.5, places=6)
        self.assertEqual(st["count"], 1)
        fading = t.state(1, now=100.2 + HapticTracker.AFTERGLOW_S / 2)
        self.assertFalse(fading["on"])
        self.assertTrue(0 < fading["level"] < 0.8)
        self.assertEqual(t.state(1, now=101.0)["level"], 0.0)
        self.assertFalse(t.state(0, now=100.1)["on"])        # other hand untouched

    def test_zero_duration_pulse_is_visible(self):
        t = HapticTracker()
        t.add(0, 0.0, 0.0, 1.0, t=5.0)
        self.assertTrue(t.state(0, now=5.0 + HapticTracker.PULSE_S / 2)["on"])

    def test_describe(self):
        self.assertEqual(describe({"count": 0}), "no haptics yet")
        self.assertEqual(describe({"count": 2, "duration": 0.025, "frequency": 160.0, "amplitude": 0.75}),
                         "160 Hz  25 ms  75%")
        self.assertIn("pulse", describe({"count": 1, "duration": 0.0, "frequency": 0.0, "amplitude": 1.0}))


class MeterTest(unittest.TestCase):
    def test_draws_every_state(self):
        t = HapticTracker()
        for st in (t.state(0),):                           # nothing received yet
            c = FakeCanvas()
            draw_haptic_meter(c, 0, 0, 260, 22, st, 1.0, label="L")
            self.assertTrue(any(k == "text" for k, _ in c.calls))
        t.add(0, 0.1, 320.0, 0.6, t=10.0)
        for now in (10.05, 10.1 + HapticTracker.AFTERGLOW_S / 2, 20.0):   # active, fading, idle
            c = FakeCanvas()
            draw_haptic_meter(c, 5, 5, 260, 22, t.state(0, now=now), now)
            self.assertGreaterEqual(len(c.calls), 3)


class DriverLinkTest(unittest.TestCase):
    def test_receives_haptics_and_context(self):
        port = free_port()
        got = []
        link = DriverLink(port=port, on_haptic=got.append)
        self.assertTrue(link.start())
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.sendto(cfp.pack_haptic(1, 1, 0.05, 200.0, 0.5), ("127.0.0.1", port))
            tx.sendto(cfp.golden_vectors()[2], ("127.0.0.1", port))
            deadline = time.time() + 2.0
            while (not got or link.context() is None) and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["hand"], 1)
            self.assertEqual(link.haptics.state(1)["count"], 1)
            ctx = link.context()
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["mode"], [1, 2])
            self.assertIs(link.context(), ctx)             # parsed once, then cached
        finally:
            tx.close()
            link.stop()

    def test_busy_port_is_reported_not_raised(self):
        port = free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))
        msgs = []
        try:
            link = DriverLink(port=port, log=msgs.append)
            self.assertFalse(link.start())
            self.assertTrue(msgs and "busy" in msgs[0])
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
