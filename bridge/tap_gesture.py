# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Triple tap on the CyberFinger's joint IMU (the module on the back of the hand), like SlimeVR's reset tap: the bridge
then asks the driver to resync its IMU fusion.

A tap is a spike in the accelerometer: the change between two consecutive samples (~8 ms apart) far beyond what the
hand's own motion gives. Worn normally the change stays under 0.9 g (captures 2026-09-26/27); vigorous throws reach
several g, but not three in a steady rhythm with the hand otherwise still, which is what a triple tap is: three
spikes 0.1-0.5 s apart, the samples between them calm. Over 6 minutes of those captures no threshold from 0.8 to
2 g found a single triple tap, even without the calm test.
"""

ACCEL_LSB_PER_G = 2048.0          # ±16 g (the firmware's VR_ACCEL_LSB_PER_G)


class TripleTap:
    """feed() each joint accelerometer sample (raw counts) with its time; True once per triple tap."""

    def __init__(self, threshold_g=1.0, min_gap=0.1, max_gap=0.5, ring=0.06, calm_g=0.15, cooldown=1.5):
        self.threshold_g = threshold_g   # a tap: this much change between two samples
        self.min_gap, self.max_gap = min_gap, max_gap   # s between taps
        self.ring = ring                 # s after a spike still part of the same tap (the module rings)
        self.calm_g = calm_g             # the samples between taps: median change below this (a steady hand)
        self.cooldown = cooldown         # s after a triple before another counts
        self._prev = None
        self._taps = []                  # spike times of the sequence so far
        self._between = []               # the changes since the first tap, outside the ringing
        self._quiet_until = 0.0
        self._sizes = []                 # the spikes' sizes (g), for the log
        self.sequence_g = ()             # the last triple tap's spike sizes (g), for tuning

    def reset(self):
        self._prev = None
        self._taps, self._between, self._sizes = [], [], []

    def feed(self, t, accel):
        a = tuple(c / ACCEL_LSB_PER_G for c in accel)
        prev, self._prev = self._prev, a
        if prev is None or t < self._quiet_until:
            return False
        change = sum((x - y) ** 2 for x, y in zip(a, prev)) ** 0.5
        if self._taps and t - self._taps[-1] > self.max_gap:
            self._taps, self._between, self._sizes = [], [], []      # too slow: not a sequence
        if self._taps and t - self._taps[-1] < self.ring:
            return False                                             # the same tap ringing on
        if change < self.threshold_g:
            if self._taps:
                self._between.append(change)
            return False
        if self._taps and t - self._taps[-1] < self.min_gap:
            self._taps, self._between, self._sizes = [], [], []      # too fast: a rattle, not taps
        self._taps.append(t)
        self._sizes.append(change)
        if len(self._taps) < 3:
            return False
        between = sorted(self._between)
        calm = not between or between[len(between) // 2] < self.calm_g
        sizes = tuple(self._sizes)
        self._taps, self._between, self._sizes = [], [], []
        if not calm:
            return False
        self.sequence_g = sizes
        self._quiet_until = t + self.cooldown
        return True
