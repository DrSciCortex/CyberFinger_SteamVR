# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/hand_views.py, the hand drawing the Windows and the Linux bridge GUIs share, on a recording
canvas (no window opens): the stream view's layout with and without a skeleton source, a CyberFinger that isn't
connected, the black button's long press, and the skeleton renderer in a box.
python -m unittest discover -s tests"""

import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cf_protocol as cfp  # noqa: E402
import cyberfinger_report as gr  # noqa: E402
from test_linux_bridge import FakeCanvas, FakeWidget  # noqa: E402

try:
    import hand_views
except ImportError:                          # no tkinter in this Python
    hand_views = None

IDLE_HAPTIC = {"on": False, "level": 0.0, "remaining": 0.0, "duration": 0.0, "frequency": 0.0, "amplitude": 0.0,
               "count": 0, "age": None}
TURNED = (0.70710677, 0.70710677, 0.0, 0.0)


class Hand:
    """What the views read of a GUI's HandState."""

    def __init__(self, connected=False, buttons=0, buttons2=0, trigger=0.0, imus=()):
        self.connected, self.buttons, self.buttons2 = connected, buttons, buttons2
        self.joy_x_float, self.joy_y_float, self.trigger_float = 0.5, -0.25, trigger
        self.battery, self.packet_count = 90, 100
        self._imus = list(imus)

    def active_imus(self):
        return self._imus


def flat_hand():
    """26 joints: the wrist at the origin, each finger straight ahead of it."""
    joints = [(0.0, 0.0, 0.0)] * 2
    for finger, count in enumerate((4, 5, 5, 5, 5)):
        joints += [(0.02 * (finger - 2), 0.0, -0.03 * (k + 1)) for k in range(count)]
    return tuple(joints)


@unittest.skipIf(hand_views is None, "the GUIs' hand views: a Python with tkinter")
class HandViewsTest(unittest.TestCase):
    def setUp(self):
        for target, name, fake in ((hand_views.tk, "Canvas", FakeCanvas), (hand_views.ttk, "Frame", FakeWidget)):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_stream_view_without_a_skeleton_source(self):
        view = hand_views.StreamView(None, "monospace", 0.05)
        left, right = (hand.canvas for hand in view.hands)
        held = Hand(True, cfp.BTN_STSEL | cfp.BTN_GRIP, gr.PINK, 0.5, [("BODY 1", TURNED), ("JOINT", TURNED)])
        view.draw((Hand(), held), (None, None), ("PINK→dash", "PINK→mic"))
        self.assertIn("offline", left.texts())                 # no left CyberFinger: its controls, at rest
        self.assertIn("GRIP", left.texts())
        self.assertNotIn("BODY 1", left.texts())
        texts = right.texts()
        for want in ("RIGHT", "90%", "50%", "STICK", "TRIG", "GRIP", "MENU", "C", "D", "E", "JCLK", "ST/SE",
                     "PINK→mic", "BODY 1", "JOINT"):
            self.assertIn(want, texts)
        self.assertNotIn("HAPTIC", texts)                      # no haptics strip outside VR mode
        self.assertFalse(any("skeleton" in t for t in texts))  # nor room kept for a skeleton nobody sends
        lit = [kw for kind, kw in right.calls if kind == "polygon" and kw.get("fill") == hand_views.COLOR_ACCENT]
        self.assertEqual(len(lit), 3)                          # GRIP, ST/SE and the pink click
        time.sleep(0.06)                                       # the black button, held past the long press
        view.draw((Hand(), held), (None, None), ("PINK→dash", "PINK→mic"))
        self.assertIn("ST/SE · HOLD", right.texts())
        view.hands[1].black_hold_s = 0                         # the driver's long press switched off
        view.draw((Hand(), held), (None, None), ("PINK→dash", "PINK→mic"))
        self.assertNotIn("ST/SE · HOLD", right.texts())

    def test_stream_view_with_skeletons_and_haptics(self):
        view = hand_views.StreamView(None, "Consolas", 0.8)
        left, right = (hand.canvas for hand in view.hands)
        pose = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.2, -0.3, -0.4), 0.54, (0.5, 0.0, 0.0))
        hands = (Hand(True), Hand(True, imus=[("BODY 1", TURNED)]))
        view.draw(hands, (IDLE_HAPTIC, IDLE_HAPTIC), ("PINK→dash", "PINK→VR"),
                  ((None, "hands not in view", None, None, None), (flat_hand(), "connected", pose, None, "FUSED")))
        self.assertIn("LEFT skeleton\n(hands not in view)", left.texts())
        self.assertIn("no IMU installed", left.texts())
        texts = right.texts()
        for want in ("driver: FUSED", "0.54m", "HAPTIC", "PINK→VR", "BODY 1"):
            self.assertIn(want, texts)
        self.assertNotIn("RIGHT SKELETON", texts)              # the hand's name is in the view's header already
        bones = [kw for kind, kw in right.calls if kind == "line" and kw.get("capstyle") == hand_views.tk.ROUND
                 and kw.get("fill") == hand_views.COLOR_FG_DIM]
        self.assertEqual(len(bones), 4 + 4 * 5)                # thumb 4 segments, the others 5

    def test_too_small_a_canvas_is_left_empty(self):
        view = hand_views.StreamView(None, "Consolas", 0.8)
        canvas = view.hands[0].canvas
        canvas.size = (100, 150)
        view.draw((Hand(True), Hand()), (None, None), ("PINK", "PINK"))
        self.assertEqual(canvas.calls, [])

    def test_skeleton_renderer_draws_inside_its_box(self):
        canvas = FakeCanvas()
        seen = []
        canvas._rec = lambda kind, *coords, **kw: seen.append((kind, coords))
        renderer = hand_views.SkeletonRenderer("LEFT", "Consolas")
        renderer.draw_in(canvas, 400, 100, 300, 260, flat_hand(), "connected")      # no pose: the auto-fit view
        points = [c for kind, coords in seen if kind in ("line", "oval", "rectangle") for c in zip(*[iter(coords)] * 2)]
        self.assertEqual(len([1 for kind, _ in seen if kind == "line"]), 24)
        for x, y in points:
            self.assertTrue(400 <= x <= 700 and 100 <= y <= 360, (x, y))


if __name__ == "__main__":
    unittest.main()
