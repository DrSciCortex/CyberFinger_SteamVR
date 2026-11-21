# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/tap_gesture.py: the triple tap on the joint IMU that resyncs the driver's IMU fusion.
python -m unittest discover -s tests"""

import math
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import tap_gesture  # noqa: E402

G = tap_gesture.ACCEL_LSB_PER_G
DT = 0.0077                                    # the CyberFinger's report interval


def signal(seconds, taps=(), noise_g=0.01, sway_g=0.0, seed=1):
    """(t, counts) samples: gravity on z, noise, an optional slow sway, and a tap (a 2.5 g jolt, ringing for two
    samples) at each time in taps."""
    rng = random.Random(seed)
    out, n = [], int(seconds / DT)
    for i in range(n):
        t = i * DT
        a = [rng.gauss(0, noise_g), rng.gauss(0, noise_g), 1.0 + rng.gauss(0, noise_g)]
        a[0] += sway_g * math.sin(2 * math.pi * 3 * t)
        for tap in taps:
            k = round((t - tap) / DT)
            if k in (0, 1, 2):
                a[2] += (2.5, -1.5, 0.6)[k]
        out.append((t, [int(v * G) for v in a]))
    return out


def fires(samples, **kw):
    det = tap_gesture.TripleTap(**kw)
    return [round(t, 2) for t, a in samples if det.feed(t, a)], det


class TripleTapTest(unittest.TestCase):
    def test_triple_tap(self):
        times, det = fires(signal(3.0, taps=(1.0, 1.25, 1.5)))
        self.assertEqual(len(times), 1)
        self.assertAlmostEqual(times[0], 1.5, delta=0.02)
        self.assertEqual(len(det.sequence_g), 3)
        self.assertTrue(all(g > 2.0 for g in det.sequence_g))

    def test_still_hand_never(self):
        self.assertEqual(fires(signal(30.0, noise_g=0.03))[0], [])

    def test_two_taps_not_enough(self):
        self.assertEqual(fires(signal(3.0, taps=(1.0, 1.25)))[0], [])

    def test_rhythm(self):
        self.assertEqual(fires(signal(3.0, taps=(1.0, 1.05, 1.1)))[0], [])      # a rattle: too fast
        self.assertEqual(fires(signal(4.0, taps=(1.0, 1.8, 2.6)))[0], [])       # too slow

    def test_restless_hand_between_taps(self):
        # jolts in the middle of vigorous motion (large changes between samples): not taps on a steady hand
        self.assertEqual(fires(signal(3.0, taps=(1.0, 1.25, 1.5), sway_g=5.0))[0], [])

    def test_cooldown(self):
        taps = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 4.0, 4.25, 4.5)
        self.assertEqual(len(fires(signal(5.0, taps=taps))[0]), 2)            # the second triple is in the cooldown


if __name__ == "__main__":
    unittest.main()
