# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the Linux half of bridge/mic_mute.py: which tool it asks (wpctl, else pactl), how it reads their
answers, and what it runs to switch the mute. No audio server is touched: subprocess.run is replaced.
python -m unittest discover -s tests"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import mic_mute  # noqa: E402


class FakeTools:
    """Stands in for subprocess.run and shutil.which. installed: the tools on the PATH. answers: per command
    (tool, verb) the (returncode, stdout), or an exception to raise; anything else succeeds silently."""

    def __init__(self, installed, answers=None):
        self.installed, self.answers, self.ran = set(installed), answers or {}, []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.installed else None

    def run(self, args, **kw):
        self.ran.append(tuple(args))
        self.env = kw.get("env")
        if args[0] not in self.installed:
            raise FileNotFoundError(args[0])
        answer = self.answers.get((args[0], args[1]), (0, ""))
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(args, answer[0], stdout=answer[1], stderr="")


@unittest.skipIf(sys.platform == "win32", "the wpctl / pactl backend is the Linux one")
class LinuxMicMuteTest(unittest.TestCase):
    def use(self, tools):
        originals = (mic_mute.subprocess.run, mic_mute.shutil.which)
        mic_mute.subprocess.run, mic_mute.shutil.which = tools.run, tools.which

        def restore():
            mic_mute.subprocess.run, mic_mute.shutil.which = originals
        self.addCleanup(restore)
        return tools

    def test_wpctl_first(self):
        t = self.use(FakeTools({"wpctl", "pactl"}, {("wpctl", "get-volume"): (0, "Volume: 0.45\n")}))
        self.assertIs(mic_mute.is_muted(), False)
        self.assertEqual(t.ran, [("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@")])
        t.answers["wpctl", "get-volume"] = (0, "Volume: 1.00 [MUTED]\n")
        self.assertIs(mic_mute.is_muted(), True)

    def test_wpctl_sets_the_default_source(self):
        t = self.use(FakeTools({"wpctl", "pactl"}, {("wpctl", "get-volume"): (0, "Volume: 0.45\n")}))
        self.assertTrue(mic_mute.set_muted(True))
        self.assertEqual(t.ran[-1], ("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"))
        self.assertTrue(mic_mute.set_muted(False))
        self.assertEqual(t.ran[-1], ("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"))
        self.assertFalse(any(cmd[0] == "pactl" for cmd in t.ran))

    def test_toggle_flips_and_reports_the_new_state(self):
        t = self.use(FakeTools({"wpctl"}, {("wpctl", "get-volume"): (0, "Volume: 0.45\n")}))
        self.assertIs(mic_mute.toggle(), True)
        self.assertEqual(t.ran[-1], ("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"))
        t.answers["wpctl", "get-volume"] = (0, "Volume: 0.45 [MUTED]\n")
        self.assertIs(mic_mute.toggle(), False)
        self.assertEqual(t.ran[-1], ("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"))

    def test_pactl_without_wpctl(self):
        t = self.use(FakeTools({"pactl"}, {("pactl", "get-source-mute"): (0, "Mute: yes\n")}))
        self.assertIs(mic_mute.is_muted(), True)
        self.assertEqual(t.ran, [("pactl", "get-source-mute", "@DEFAULT_SOURCE@")])
        self.assertEqual(t.env.get("LC_ALL"), "C")           # "Mute: yes" is translated otherwise
        self.assertTrue(mic_mute.set_muted(False))
        self.assertEqual(t.ran[-1], ("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"))
        t.answers["pactl", "get-source-mute"] = (0, "Mute: no\n")
        self.assertIs(mic_mute.is_muted(), False)

    def test_pactl_when_wpctl_cannot_answer(self):
        # wpctl installed but without its server (a PulseAudio system), or hanging
        for failure in ((1, ""), subprocess.TimeoutExpired("wpctl", 2.0), OSError("exec failed")):
            t = self.use(FakeTools({"wpctl", "pactl"}, {("wpctl", "get-volume"): failure,
                                                        ("pactl", "get-source-mute"): (0, "Mute: no\n")}))
            self.assertIs(mic_mute.toggle(), True)
            self.assertEqual(t.ran[-1], ("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"))
            self.assertFalse(any(cmd[:2] == ("wpctl", "set-mute") for cmd in t.ran))

    def test_no_microphone(self):
        t = self.use(FakeTools({"wpctl", "pactl"}, {("wpctl", "get-volume"): (1, ""),
                                                    ("pactl", "get-source-mute"): (1, "")}))
        self.assertIsNone(mic_mute.is_muted())
        self.assertFalse(mic_mute.set_muted(True))
        self.assertIsNone(mic_mute.toggle())
        self.assertFalse(any("set" in cmd[1] for cmd in t.ran))          # nothing switched blindly

    def test_unreadable_answer_is_no_microphone(self):
        self.use(FakeTools({"wpctl"}, {("wpctl", "get-volume"): (0, "something else\n")}))
        self.assertIsNone(mic_mute.is_muted())

    def test_a_failed_switch_is_reported(self):
        self.use(FakeTools({"wpctl"}, {("wpctl", "get-volume"): (0, "Volume: 0.45\n"),
                                       ("wpctl", "set-mute"): (1, "")}))
        self.assertFalse(mic_mute.set_muted(True))

    def test_no_tool_is_an_oserror(self):
        # pink_button.py logs an OSError as "could not switch the microphone"
        self.use(FakeTools(set()))
        with self.assertRaises(OSError):
            mic_mute.toggle()


if __name__ == "__main__":
    unittest.main()
