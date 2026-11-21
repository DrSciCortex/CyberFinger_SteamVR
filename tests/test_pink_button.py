# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/pink_button.py (the right pink button: microphone mute or a FluxAction) and
bridge/flux_actions.py (the datagrams the MoreFluxActions mod listens for).
python -m unittest discover -s tests"""

import os
import socket
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import flux_actions  # noqa: E402
import mic_mute  # noqa: E402
import pink_button  # noqa: E402


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, action, pressed):
        self.sent.append((action, pressed))


class FakeHaptics:
    def __init__(self):
        self.requests = []

    def request(self, hand, seconds, hz, amplitude):
        self.requests.append((hand, seconds, hz))


class DatagramTest(unittest.TestCase):
    def test_names_the_impulse(self):
        # MoreFluxActions' FluxActionProtocol.TryParseDatagram reads exactly these
        self.assertEqual(flux_actions.datagram(42, True), b"FluxAction42.Pressed")
        self.assertEqual(flux_actions.datagram(1, False), b"FluxAction1.Released")
        for bad in (0, 43, -1):
            with self.assertRaises(ValueError):
                flux_actions.datagram(bad, True)

    def test_reaches_a_loopback_listener(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2.0)
        try:
            logs = []
            sender = flux_actions.FluxActionSender(logs.append, port=rx.getsockname()[1])
            sender.send(7, True)
            sender.send(7, False)
            sender.send(99, True)                # out of range: logged, not sent
            self.assertEqual(rx.recv(64), b"FluxAction7.Pressed")
            self.assertEqual(rx.recv(64), b"FluxAction7.Released")
            self.assertEqual(len(logs), 1)
        finally:
            rx.close()


class PinkButtonTest(unittest.TestCase):
    def setUp(self):
        self.sender, self.haptics, self.logs = FakeSender(), FakeHaptics(), []
        self.action = 0
        self.button = pink_button.PinkButton(self.logs.append, haptics=lambda: self.haptics,
                                             action=lambda: self.action, sender=self.sender)

    def press(self, hand=1):
        self.button.on_input(hand, pink_button.PINK)
        self.button.on_input(hand, 0)

    def test_flux_action_follows_the_button(self):
        self.action = 42
        self.button.on_input(1, pink_button.PINK)
        self.button.on_input(1, pink_button.PINK)     # still down: nothing new
        self.assertEqual(self.sender.sent, [(42, True)])
        self.button.on_input(1, 0)
        self.assertEqual(self.sender.sent, [(42, True), (42, False)])
        self.assertEqual(self.haptics.requests, [])          # no vibration for a FluxAction

    def test_release_goes_to_the_action_pressed(self):
        self.action = 5
        self.button.on_input(1, pink_button.PINK)
        self.action = 0                                # switched to the microphone while held
        self.button.on_input(1, 0)
        self.assertEqual(self.sender.sent, [(5, True), (5, False)])

    def test_steamvr_mode_leaves_it_alone(self):
        # -1: the driver hands the button to the app's binding; the bridge neither mutes nor fires anything
        self.action = -1
        original = mic_mute.toggle
        mic_mute.toggle = lambda: self.fail("the microphone was toggled in the SteamVR mode")
        try:
            self.press()
            threading.Event().wait(0.1)
        finally:
            mic_mute.toggle = original
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.haptics.requests, [])

    def test_left_hand_ignored(self):
        self.action = 42
        self.press(hand=0)
        self.assertEqual(self.sender.sent, [])

    def test_mic_by_default(self):
        toggled = threading.Event()
        original = mic_mute.toggle
        mic_mute.toggle = lambda: (toggled.set(), True)[1]
        try:
            self.press()
            self.assertTrue(toggled.wait(2.0))
        finally:
            mic_mute.toggle = original
        self.assertEqual(self.sender.sent, [])
        for _ in range(100):                                 # the confirmation follows on the worker thread
            if self.haptics.requests:
                break
            threading.Event().wait(0.02)
        self.assertEqual(self.haptics.requests, [(1, 0.35, 8.0)])   # muted: the pulse train, on the right CyberFinger


if __name__ == "__main__":
    unittest.main()
