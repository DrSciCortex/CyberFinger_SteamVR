# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge_linux/cyberfinger_gui_linux.py without a CyberFinger, a driver or a display: the report
decoding into the hand state, VR mode's CFG2 / CFIM packets, the SlimeVR forwarder, the panels' drawing (on a
recording canvas) and the app's wiring of the pink button, the triple tap, the haptics and stream mode (Tk
replaced by mocks: no window opens).
python -m unittest discover -s tests"""

import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, os.path.join(ROOT, "bridge_linux"))
import cf_protocol as cfp  # noqa: E402
import cyberfinger_report as gr  # noqa: E402
import mic_mute  # noqa: E402
import pink_button  # noqa: E402
from test_driver_skeleton import context_packet  # noqa: E402

gui = None
if sys.platform.startswith("linux"):
    try:
        import hand_views  # noqa: E402     (what the GUI draws with; its Tk is mocked along with the GUI's)
        import cyberfinger_gui_linux as gui  # noqa: E402
    except Exception:                        # no tkinter in this Python, or pynput without a display
        pass

Q1 = (0.70710677, 0.70710677, 0.0, 0.0)
QJ = (0.5, -0.5, 0.5, -0.5)
A1, AJ = (10, -20, 2048), (-7, 300, -2048)


def report(buttons=0, joy=(0, 0), trigger=0, battery=90, seq=4242, buttons2=None, imu=True, hand=0):
    """A CyberFinger report: body 1 + joint IMU (57 bytes, v1.3) or no IMU (the first firmware's 12 bytes); with
    buttons2, v1.3.3's: the extension byte after the IMU blocks."""
    pkt = gr.BASE.pack(hand, buttons, joy[0], joy[1], trigger, battery, seq)
    ext = gr.EXT if buttons2 is not None else 0
    if imu:
        pkt += (gr.QUAT.pack(*Q1) + bytes([gr.BODY1 | gr.JOINT | ext]) + gr.ACCEL.pack(*A1) + gr.QUAT.pack(*QJ)
                + gr.ACCEL.pack(*AJ))
    elif ext:
        pkt += gr.QUAT.pack(*gr.IDENTITY) + bytes([ext])
    return pkt + (bytes([buttons2]) if ext else b"")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listener():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    return rx, rx.getsockname()[1]


def cfg2(data):
    """A CFG2 datagram → (hand, seq, buttons, trigger, joy_x, joy_y, battery, buttons2, resync)."""
    h = cfp.unpack_header(data)
    assert h["magic"] == cfp.MAGIC_CYBERFINGER and len(data) == cfp.CYBERFINGER_SIZE
    return (h["hand"], h["seq"]) + cfp.CYBERFINGER_BODY.unpack_from(data, cfp.HEADER.size)


class FakeApp:
    """What the BLE manager calls back into."""

    def __init__(self):
        self.logs, self.inputs = [], []

    def log(self, msg):
        self.logs.append(msg)

    def set_status(self, status):
        pass

    def on_input(self, hand, state):
        self.inputs.append((hand, state.buttons, state.buttons2))


class FakeWidget:
    def __init__(self, *args, **kw):
        pass

    def pack(self, **kw):
        pass

    def bind(self, *args, **kw):
        pass


class FakeCanvas(FakeWidget):
    """Records draw calls; checks coordinates are numbers, as Tk requires."""

    size = (320, 380)

    def __init__(self, *args, **kw):
        self.calls = []

    def winfo_width(self):
        return self.size[0]

    def winfo_height(self):
        return self.size[1]

    def delete(self, _what):
        self.calls = []

    def _rec(self, kind, *coords, **kw):
        if len(coords) == 1 and isinstance(coords[0], (list, tuple)):
            coords = tuple(coords[0])
        for v in coords:
            assert isinstance(v, (int, float)), (kind, v)
        self.calls.append((kind, kw))

    def create_oval(self, *a, **kw): self._rec("oval", *a, **kw)
    def create_line(self, *a, **kw): self._rec("line", *a, **kw)
    def create_text(self, *a, **kw): self._rec("text", *a, **kw)
    def create_rectangle(self, *a, **kw): self._rec("rectangle", *a, **kw)
    def create_polygon(self, *a, **kw): self._rec("polygon", *a, **kw)

    def texts(self):
        return [kw["text"] for kind, kw in self.calls if kind == "text"]


@unittest.skipIf(gui is None, "the Linux bridge GUI: Linux with tkinter")
class LinuxBridgeTest(unittest.TestCase):
    pass


class ReportTest(LinuxBridgeTest):
    def test_report_into_hand_state(self):
        app = FakeApp()
        ble = gui.BLEManager(app)
        # bleak hands over a bytearray; the hand is the one the device was paired as, not the report's byte
        ble._handle_data(bytearray(report(buttons=0x21, joy=(100, -200), trigger=128, buttons2=gr.PINK)), 1, ble.right)
        st = ble.right
        self.assertEqual((st.buttons, st.buttons2, st.joy_x, st.joy_y, st.trigger, st.battery, st.report_seq),
                         (0x21, gr.PINK, 100, -200, 128, 90, 4242))
        self.assertEqual(st.imu_present, gui.IMU_BODY_PRIMARY | gui.IMU_JOINT)
        self.assertTrue(st.has_accel)
        self.assertEqual((st.accel, st.accel_joint), (A1, AJ))
        for got, want in zip(st.quat_joint, QJ):
            self.assertAlmostEqual(got, want, places=6)
        self.assertEqual([label for label, _q in st.active_imus()], ["BODY 1", "JOINT"])
        self.assertEqual(app.inputs, [(1, 0x21, gr.PINK)])
        self.assertEqual(app.logs, ["R IMU: BODY 1, JOINT", "R PINK", "R BTN: TRIG+MENU"])
        self.assertGreater(st.rx_perf, 0.0)

    def test_oldest_firmware_and_runts(self):
        app = FakeApp()
        ble = gui.BLEManager(app)
        ble._handle_data(report(buttons=0x02, imu=False), 0, ble.left)       # the 12-byte report: no IMU, no pink
        self.assertEqual((ble.left.buttons, ble.left.buttons2, ble.left.imu_present), (0x02, 0, 0))
        self.assertEqual(ble.left.active_imus(), [])
        ble._handle_data(b"\x00" * 5, 0, ble.left)                           # too short: ignored
        self.assertEqual(len(app.inputs), 1)

    def test_reset_link_forgets_the_imus(self):
        ble = gui.BLEManager(FakeApp())
        ble._handle_data(report(), 0, ble.left)
        self.assertTrue(ble.left.has_imu)
        ble.left.reset_link()
        self.assertFalse(ble.left.has_imu or ble.left.has_accel)


class VRModeTest(LinuxBridgeTest):
    def setUp(self):
        self.rx, port = listener()
        self.mode = gui.VRMode(port=port)
        self.addCleanup(self.rx.close)
        self.addCleanup(self.mode.stop)

    def state(self, **kw):
        ble = gui.BLEManager(FakeApp())
        ble._handle_data(report(**kw), 1, ble.right)
        return ble.right

    def test_cfg2_and_cfim_per_report(self):
        st = self.state(buttons=0x21, joy=(32767, 16384), trigger=200, buttons2=0)
        self.mode.on_input(1, st)
        hand, seq, buttons, trigger, joy_x, joy_y, battery, buttons2, resync = cfg2(self.rx.recv(4096))
        self.assertEqual((hand, seq, buttons, trigger, battery, buttons2, resync), (1, 1, 0x21, 200, 90, 0, 0))
        # centred and with the deadzone, +y up (the firmware's is down)
        self.assertEqual((joy_x, joy_y), cfp.stick_to_int16(1.0, -16384 / 32767.0, gui.VRMode.STICK_DEADZONE))
        self.assertGreater(joy_x, 0)
        self.assertLess(joy_y, 0)
        imu = cfp.unpack_imu(self.rx.recv(4096))
        self.assertEqual((imu["header"]["hand"], imu["header"]["seq"]), (1, 4242))     # the report's own sequence
        self.assertEqual(imu["present"], cfp.IMU_BODY1 | cfp.IMU_JOINT)
        self.assertTrue(imu["has_accel"])
        self.assertEqual([tuple(a) for a in imu["accels"]], [A1, (0, 0, 0), AJ])
        for got, want in zip(imu["quats"][2], QJ):
            self.assertAlmostEqual(got, want, places=6)

    def test_stick_deadzone(self):
        self.mode.on_input(0, self.state(joy=(2000, -2000), imu=False))
        self.assertEqual(cfg2(self.rx.recv(4096))[4:6], (0, 0))

    def test_no_cfim_without_an_imu(self):
        st = self.state(imu=False)
        self.mode.on_input(0, st)
        self.mode.on_input(0, st)
        self.assertEqual([cfg2(self.rx.recv(4096))[1] for _ in range(2)], [1, 2])      # two CFG2, nothing between

    def test_right_pink_reaches_the_driver_only_when_left_to_it(self):
        st = self.state(buttons2=gr.PINK, imu=False)
        self.mode.on_input(1, st)                              # the bridge's (mic, FluxAction): kept from the driver
        self.assertEqual(cfg2(self.rx.recv(4096))[7], 0)
        self.mode.on_input(0, st)                              # the left one always goes on
        self.assertEqual(cfg2(self.rx.recv(4096))[7], cfp.CYBERFINGER_BTN2_PINK)
        self.mode.right_pink_to_driver = True
        self.mode.on_input(1, st)
        self.assertEqual(cfg2(self.rx.recv(4096))[7], cfp.CYBERFINGER_BTN2_PINK)

    def test_resync_count(self):
        st = self.state(imu=False)
        self.mode.resync_imu([1])
        self.mode.on_input(1, st)
        self.mode.on_input(0, st)
        self.assertEqual([cfg2(self.rx.recv(4096))[8] for _ in range(2)], [1, 0])
        self.mode.resync = [255, 0]
        self.mode.resync_imu()
        self.assertEqual(self.mode.resync, [0, 1])            # wraps: one byte on the wire


class SlimeVRTest(LinuxBridgeTest):
    def test_announces_then_forwards_rotation(self):
        server, port = listener()
        logs = []
        fwd = gui.SlimeVRForwarder("127.0.0.1", port, "body1", logs.append)
        ble = gui.BLEManager(FakeApp())
        ble._handle_data(report(), 0, ble.left)
        try:
            fwd.trackers[1].sock.close()                       # one tracker is enough here
            tracker = fwd.trackers[0]
            tracker.start()
            data, addr = server.recvfrom(2048)
            ptype, number = struct.unpack_from(">IQ", data)
            self.assertEqual((ptype, number), (gui.SLIME_SEND_HANDSHAKE, 0))
            self.assertIn(tracker.mac, data)
            server.sendto(bytes([gui.SLIME_RECV_HANDSHAKE]) + gui.SLIME_HANDSHAKE_REPLY, addr)
            deadline = time.time() + 2.0
            while not tracker._connected and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(tracker._connected)
            fwd.on_input(0, ble.left)
            seen = {}
            deadline = time.time() + 2.0
            while not {gui.SLIME_SEND_ROTATION_DATA, gui.SLIME_SEND_ACCEL} <= set(seen) and time.time() < deadline:
                data, _ = server.recvfrom(2048)
                seen.setdefault(struct.unpack_from(">I", data)[0], data)
            sensor, _dtype, x, y, z, w, _acc = struct.unpack_from(">BBffffB", seen[gui.SLIME_SEND_ROTATION_DATA], 12)
            self.assertEqual(sensor, gui.SLIME_SENSOR_BODY)
            for got, want in zip((w, x, y, z), Q1):            # the wire wants x, y, z, w
                self.assertAlmostEqual(got, want, places=6)
        finally:
            fwd.trackers[0].stop()
            server.close()


class PanelTest(LinuxBridgeTest):
    def setUp(self):
        for target, fake in ((gui.tk, "Canvas"), (gui.ttk, "Frame")):
            patcher = mock.patch.object(target, fake, FakeCanvas if fake == "Canvas" else FakeWidget)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hand_panel(self):
        ble = gui.BLEManager(FakeApp())
        st = ble.right
        panel = gui.HandPanel(None, "RIGHT", gui.tk.RIGHT, black_hold=(0.05, 1))
        c = panel.canvas
        panel.update_state(st)
        self.assertEqual(c.texts(), ["RIGHT (disconnected)"])
        st.connected = True
        ble._handle_data(report(buttons=gui.BTN_STSEL, trigger=128, buttons2=gr.PINK), 1, st)
        panel.update_state(st)
        texts = c.texts()
        for want in ("RIGHT", "90%", "PINK→VR", "HOLD·FA2", "BODY 1", "JOINT", "Trigger: 50%"):
            self.assertIn(want, texts)
        self.assertNotIn("HAPTIC", texts)                      # no haptics strip outside VR mode
        hold = [kw for kind, kw in c.calls if kind == "rectangle" and kw.get("fill") == gui.COLOR_ACCENT]
        time.sleep(0.06)                                       # the black button, held past the long press
        panel.update_state(st)
        lit = [kw for kind, kw in c.calls if kind == "rectangle" and kw.get("fill") == gui.COLOR_ACCENT]
        self.assertGreater(len(lit), len(hold))
        # VR mode: the haptics strip, and what the bridge does with the right pink button
        panel.pink_label = "PINK→FA42"
        haptic = {"on": True, "level": 0.8, "remaining": 0.5, "duration": 0.2, "frequency": 160.0,
                  "amplitude": 0.8, "count": 3, "age": 0.1}
        panel.update_state(st, haptic)
        self.assertIn("PINK→FA42", c.texts())
        self.assertIn("HAPTIC", c.texts())
        panel.black_hold_s = 0                                 # a driver without the long press: no pill
        panel.update_state(st, haptic)
        self.assertFalse(any(t.startswith("HOLD") for t in c.texts()))
        st.imu_present = 0
        panel.update_state(st, haptic)
        self.assertIn("no IMU installed", c.texts())
        panel.set_disconnected()

    def test_black_hold_follows_the_driver_settings(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            self.assertEqual(gui._black_hold(), (0.8, 1))      # no settings file: the driver's defaults
            os.makedirs(os.path.join(home, "cyberfinger"))
            path = os.path.join(home, "cyberfinger", "wivrn.json")
            for text, want in (('{"black_hold_ms": 500, "flux_actions": {"a_hold": 9}}', (0.5, 9)),
                               ('{"black_hold_ms": 0, "flux_actions": {"a_hold": 0}}', (0.0, 0)),
                               ('{"profile": "touch"}', (0.8, 1)),
                               ('{"black_hold_ms": 500, // a comment: the driver reads it, json does not\n}', (0.8, 1)),
                               ('{"black_hold_ms": "soon"}', (0.8, 1))):
                with open(path, "w") as f:
                    f.write(text)
                self.assertEqual(gui._black_hold(), want)
        panel = gui.HandPanel(None, "LEFT", gui.tk.LEFT, black_hold=(0.5, 0))
        st = gui.HandState()
        st.connected = True
        panel.update_state(st)
        self.assertIn("HOLD", panel.canvas.texts())            # a long press that isn't a FluxAction

    def test_test_haptic_click_only_on_the_strip(self):
        clicks = []
        panel = gui.HandPanel(None, "LEFT", gui.tk.LEFT, on_test_haptic=lambda: clicks.append(1))
        st = gui.HandState()
        event = mock.Mock(y=370)
        panel.update_state(st)
        panel._on_click(event)                                 # no strip shown: nothing
        panel.update_state(st, {"on": False, "level": 0.0, "remaining": 0.0, "duration": 0.0, "frequency": 0.0,
                                "amplitude": 0.0, "count": 0, "age": None})
        panel._on_click(mock.Mock(y=100))                      # above the strip
        panel._on_click(event)
        self.assertEqual(clicks, [1])

    def test_skeleton_panel(self):
        import driver_skeleton
        bones = [(0.01 * (i % 5), 0.02, -0.01 * (i % 4), 1.0, 0.0, 0.0, 0.0) for i in range(cfp.NUM_BONES)]
        joints = driver_skeleton.model_joints(bones)
        pose = driver_skeleton.relative_pose((0.0, 1.6, 0.0), (1.0, 0.0, 0.0, 0.0), (0.2, 1.3, -0.4),
                                             (1.0, 0.0, 0.0, 0.0), vel=(0.5, 0.0, 0.0))
        other = driver_skeleton.relative_pose((0.0, 1.6, 0.0), (1.0, 0.0, 0.0, 0.0), (-0.2, 1.3, 0.4),
                                              (1.0, 0.0, 0.0, 0.0))
        panel = gui.SkeletonPanel(None, "LEFT", gui.tk.LEFT)
        c = panel.canvas
        panel.draw(None, "WiVRn driver not running")
        self.assertEqual(c.texts(), ["LEFT skeleton\n(WiVRn driver not running)"])
        panel.draw(None, "hands not in view", mode="NO_POSE")
        self.assertIn("driver: NO_POSE", c.texts())
        panel.draw(joints, "connected", pose, other, mode="FUSED")        # oriented, with the dome
        self.assertIn("LEFT SKELETON", c.texts())
        self.assertIn("driver: FUSED", c.texts())
        self.assertIn("0.54m", c.texts())                                 # the hand's distance from the head
        bones_drawn = sum(1 for kind, kw in c.calls if kind == "line" and kw.get("width") == 2)
        self.assertEqual(bones_drawn, 4 + 4 * 5)                          # thumb 4 segments, the others 5
        panel.draw(joints, "connected")                                   # no pose: the auto-fit view
        self.assertEqual(sum(1 for kind, kw in c.calls if kind == "line" and kw.get("width") == 2), 24)
        panel.draw(joints, "connected", other, pose)                      # a hand behind the head

    def test_skeleton_is_drawn_as_its_owner_sees_it(self):
        # A flat hand before the user, fingers away from them, whichever way the user faces in the tracking space:
        # the fingertip is drawn above the wrist, and what is to the user's right, to the right.
        import driver_skeleton
        joints = [(0.0, 0.0, 0.0)] * 26
        joints[10] = (0.0, 0.0, -0.10)                                    # index tip: ahead of the wrist
        joints[25] = (0.05, 0.0, 0.0)                                     # pinky tip: to its right
        panel = gui.SkeletonPanel(None, "RIGHT", gui.tk.RIGHT)
        seen = []
        for deg in (0.0, 90.0, 200.0):
            half = math.radians(deg) / 2
            facing = (math.cos(half), 0.0, math.sin(half), 0.0)           # head and hand turned alike
            pose = driver_skeleton.relative_pose((0.0, 1.6, 0.0), facing, (0.0, 1.3, 0.0), facing)
            to_px = panel._orient_to_px(joints, pose[0], 320, 380, dist=pose[2])
            wrist, tip, right = to_px(1), to_px(10), to_px(25)
            self.assertLess(tip[1], wrist[1] - 5)
            self.assertAlmostEqual(tip[0], wrist[0], places=3)
            self.assertGreater(right[0], wrist[0] + 5)
            seen.append((tip, right))
        for other in seen[1:]:
            for a, b in zip(sum(other, ()), sum(seen[0], ())):
                self.assertAlmostEqual(a, b, places=3)


class FakeVar:
    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value

    def trace_add(self, *args):
        pass


class FakeBLE:
    """The BLE manager without Bluetooth: the hand states and a recording HapticSender."""

    def __init__(self, app):
        self.left, self.right = gui.HandState(), gui.HandState()
        self.haptics = mock.Mock()
        self.haptics.available.return_value = True

    def start(self):
        pass

    def stop(self):
        pass


class AppTest(LinuxBridgeTest):
    """CyberFingerApp with Tk mocked out, its settings in a temporary directory and its sockets on spare ports."""

    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        self.rx, self.driver_port = listener()
        self.addCleanup(self.rx.close)
        self.link_port = free_port()
        self.flux = mock.Mock()
        self.handle_data = gui.BLEManager._handle_data         # the real one: BLEManager is replaced below
        fake_tk = mock.MagicMock()
        fake_tk.TclError = type("TclError", (Exception,), {})
        fake_tk.StringVar, fake_tk.BooleanVar, fake_tk.Canvas = FakeVar, FakeVar, FakeCanvas
        real_link, real_vr = gui.DriverLink, gui.VRMode
        for patcher in (
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config.name}),
                mock.patch.object(gui, "tk", fake_tk), mock.patch.object(gui, "ttk", mock.MagicMock()),
                mock.patch.object(hand_views, "tk", fake_tk), mock.patch.object(hand_views, "ttk", mock.MagicMock()),
                mock.patch.object(gui, "scrolledtext", mock.MagicMock()),
                mock.patch.object(gui, "HAS_TRAY", False), mock.patch.object(gui, "HAS_BLEAK", True),
                mock.patch.object(gui, "BLEManager", FakeBLE),
                mock.patch.object(gui, "VRMode", lambda: real_vr(port=self.driver_port)),
                mock.patch.object(gui, "DriverLink", lambda **kw: real_link(port=self.link_port, **kw)),
                mock.patch.object(pink_button, "FluxActionSender", lambda log: self.flux),
                mock.patch.object(gui.bridge_version, "get", lambda: "v0 (test)")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def app(self, **settings):
        if settings:
            os.makedirs(os.path.join(self.config.name, "cyberfinger-bridge"), exist_ok=True)
            with open(os.path.join(self.config.name, "cyberfinger-bridge", "settings.json"), "w") as f:
                gui.json.dump(settings, f)
        app = gui.CyberFingerApp()
        app.tabs.index.return_value = 0                        # the Buttons + IMU tab

        def close():
            app._stop_bridge()
            app.vr_mode.stop()
        self.addCleanup(close)
        return app

    def logs(self, app):
        out = []
        while not app.log_queue.empty():
            out.append(app.log_queue.get_nowait())
        return out

    def feed(self, app, hand, **kw):
        state = app.ble.right if hand else app.ble.left
        self.handle_data(mock.Mock(app=app), report(**kw), hand, state)

    def test_defaults_and_saved_settings(self):
        app = self.app()
        self.assertEqual(app.pink_mode_var.get(), "driver")
        self.assertEqual(app._pink_flux, -1)
        self.assertTrue(app.vr_mode.right_pink_to_driver)
        self.assertIsNone(app.right_panel.pink_label)
        with open(app._config_path) as f:
            self.assertEqual(gui.json.load(f)["right_pink"], "driver")
        app = self.app(right_pink="flux", right_pink_flux=7, slimevr_body_imu="body2", tab="console",
                       skeleton_enabled=False)
        self.assertEqual((app._pink_flux, app.right_panel.pink_label), (7, "PINK→FA7"))
        self.assertFalse(app.vr_mode.right_pink_to_driver)
        self.assertEqual(app.slimevr_body_var.get(), "body2")
        self.assertIsNone(app.skeleton)
        app._poll_queues()
        app = self.app(right_pink="steamvr")                   # a Windows settings file: the driver's
        self.assertEqual((app.pink_mode_var.get(), app._pink_flux), ("driver", -1))

    def test_pink_setting_takes_effect_and_survives_a_restart_of_the_bridge(self):
        app = self.app()
        app._start_bridge()
        self.assertIs(app.active_mode, app.vr_mode)
        self.feed(app, 1, buttons2=gr.PINK, imu=False)
        self.assertEqual(cfg2(self.rx.recv(4096))[7], cfp.CYBERFINGER_BTN2_PINK)       # left to the driver
        self.feed(app, 1, buttons2=0, imu=False)
        self.rx.recv(4096)
        self.flux.send.assert_not_called()

        app.pink_mode_var.set("flux")
        app.pink_flux_var.set("5")
        app._on_pink_changed()
        self.feed(app, 1, buttons2=gr.PINK, imu=False)
        self.assertEqual(cfg2(self.rx.recv(4096))[7], 0)                               # the bridge's now
        self.feed(app, 1, buttons2=0, imu=False)
        self.rx.recv(4096)
        self.assertEqual(self.flux.send.call_args_list, [mock.call(5, True), mock.call(5, False)])
        self.feed(app, 0, buttons2=gr.PINK, imu=False)                                 # the left one: the driver's
        self.assertEqual(cfg2(self.rx.recv(4096))[7], cfp.CYBERFINGER_BTN2_PINK)
        self.assertEqual(self.flux.send.call_count, 2)

        app.pink_mode_var.set("driver")
        app._on_pink_changed()
        app._stop_bridge()
        app._start_bridge()                                                            # a new VRMode
        self.feed(app, 1, buttons2=gr.PINK, imu=False)
        self.assertEqual(cfg2(self.rx.recv(4096))[7], cfp.CYBERFINGER_BTN2_PINK)

    def test_mic_mute_and_its_confirmation(self):
        app = self.app(right_pink="mic")
        self.assertEqual((app._pink_flux, app.right_panel.pink_label), (0, "PINK→mic"))
        app._start_bridge()
        toggled = threading.Event()
        with mock.patch.object(mic_mute, "toggle", lambda: (toggled.set(), True)[1]):   # not the real microphone
            self.feed(app, 1, buttons2=gr.PINK, imu=False)
            self.assertTrue(toggled.wait(2.0))
            deadline = time.time() + 2.0
            while not app.ble.haptics.request.called and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(cfg2(self.rx.recv(4096))[7], 0)
        app.ble.haptics.request.assert_called_once_with(1, 0.35, 8.0, 1.0)            # muted: the pulse train
        self.assertIn("Mic: muted (right pink button)", self.logs(app))

    def test_triple_tap_and_button_resync(self):
        app = self.app()
        app._resync_imu("button")
        self.assertIn("only in VR mode", self.logs(app)[-1])
        app._start_bridge()
        app._resync_imu("button")
        self.assertIn("no CyberFinger with a joint IMU", self.logs(app)[-1])
        self.assertEqual(app.vr_mode.resync, [0, 0])
        self.feed(app, 0)                                      # the left CyberFinger has the joint IMU
        app._resync_imu("button")
        self.assertEqual(app.vr_mode.resync, [1, 0])
        app.ble.haptics.request.assert_not_called()
        # three taps on the right joint IMU, 0.25 s apart, the hand still between them
        self.feed(app, 1)
        app.triple_tap[1].reset()
        st, t = app.ble.right, 100.0
        for i in range(120):
            st.accel_joint, st.rx_perf = ((0, 0, 2048 * 4) if i in (20, 51, 82) else (0, 0, 2048)), t
            app.on_input(1, st)
            t += 0.008
        self.assertEqual(app.vr_mode.resync, [2, 1])           # both hands have a joint IMU now
        app.ble.haptics.request.assert_called_once_with(1, 0.25, 8.0, 1.0)            # confirmed on the tapped one
        self.assertTrue(any("right CyberFinger triple tap" in m for m in self.logs(app)))

    def test_haptics_from_the_driver_and_the_test_click(self):
        app = self.app()
        self.assertIsNone(app._haptic_state(0))                # not in VR mode: no strip
        app._start_bridge()
        self.assertTrue(app.driver_link.running)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.sendto(cfp.pack_haptic(1, 1, 0.05, 200.0, 0.5), ("127.0.0.1", self.link_port))
            deadline = time.time() + 2.0
            while not app.ble.haptics.request.called and time.time() < deadline:
                time.sleep(0.01)
        finally:
            tx.close()
        app.ble.haptics.request.assert_called_once()
        hand, duration, frequency, amplitude = app.ble.haptics.request.call_args.args
        self.assertEqual((hand, frequency, amplitude), (1, 200.0, 0.5))
        self.assertAlmostEqual(duration, 0.05, places=6)
        self.assertEqual(app._haptic_state(1)["count"], 1)
        logs = self.logs(app)
        self.assertTrue(any(m.startswith("WiVRn driver link: listening") for m in logs))
        self.assertTrue(any("first request for the right hand" in m for m in logs))
        app._test_haptic(0)
        self.assertEqual(app.ble.haptics.request.call_args, mock.call(0, 0.25, 0.0, 0.8))
        self.assertEqual(app._haptic_state(0)["count"], 1)     # shown on the meter too
        app._poll_queues()                                     # draws both hand panels with their strips
        self.assertIn("HAPTIC", app.left_panel.canvas.texts())
        app._stop_bridge()
        self.assertIsNone(app.driver_link)

    def test_skeleton_tab_draws_from_the_driver(self):
        app = self.app()
        app.tabs.index.return_value = 1                        # the Skeleton tab
        app._poll_queues()
        self.assertEqual(app.left_skeleton.canvas.texts(), ["LEFT skeleton\n(start the bridge in VR mode)"])
        app._start_bridge()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.sendto(context_packet(modes=(2, 3), hands=({"pos": (0.0, 1.2, -0.4)},)), ("127.0.0.1", self.link_port))
            deadline = time.time() + 2.0
            while app.driver_link.context() is None and time.time() < deadline:
                time.sleep(0.01)
        finally:
            tx.close()
        app._poll_queues()
        self.assertIn("LEFT SKELETON", app.left_skeleton.canvas.texts())
        self.assertIn("driver: PASSTHROUGH", app.left_skeleton.canvas.texts())
        self.assertIn("RIGHT skeleton\n(connected)", app.right_skeleton.canvas.texts())
        self.assertIn("driver: NO_POSE", app.right_skeleton.canvas.texts())

    def test_stream_mode(self):
        app = self.app()
        app._start_bridge()
        app.ble.left.connected = True
        self.feed(app, 0, buttons=gui.BTN_GRIP)
        left, right = (hand.canvas for hand in app.stream_view.hands)
        app._poll_queues()
        self.assertEqual(left.calls, [])                       # not in stream mode: its view isn't drawn
        app.title_label = mock.Mock(**{"winfo_x.return_value": 0, "winfo_width.return_value": 300})
        app.status_label = mock.Mock(**{"winfo_x.return_value": 500})
        app._stream_stop_font, app._stream_stop_chrome = mock.Mock(measure=lambda text: 8 * len(text)), 20
        app.stream_stop_btn.master.winfo_width.return_value = 648
        app._start_stream()
        app.stream_view.frame.pack.assert_called_once()
        # Stop stream: 124 wide, as near the line's middle (324) as the title (to 300) lets it
        self.assertEqual(app.stream_stop_btn.place.call_args.kwargs["x"], 370)
        tabs_drawn = len(app.left_panel.canvas.calls)
        app._poll_queues()
        self.assertEqual(len(app.left_panel.canvas.calls), tabs_drawn)    # the hidden tab isn't redrawn
        for want in ("LEFT", "90%", "GRIP", "PINK→VR", "BODY 1", "HAPTIC"):
            self.assertIn(want, left.texts())
        self.assertIn("offline", right.texts())
        self.assertNotIn("LEFT skeleton\n(WiVRn driver not running)", left.texts())     # no driver: no space kept
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.sendto(context_packet(modes=(2, 3), hands=({"pos": (0.0, 1.2, -0.4)},)), ("127.0.0.1", self.link_port))
            deadline = time.time() + 2.0
            while app.driver_link.context() is None and time.time() < deadline:
                time.sleep(0.01)
        finally:
            tx.close()
        app._poll_queues()                                     # the driver's hands, above the controls
        self.assertIn("driver: PASSTHROUGH", left.texts())
        self.assertIn("RIGHT skeleton\n(connected)", right.texts())
        app.status_label.winfo_x.return_value = 380            # a long status: room for the short text only
        app._place_stream_stop()
        app.stream_stop_btn.configure.assert_called_with(text="■ Stop")
        app._stop_stream()
        app.stream_view.frame.pack_forget.assert_called_once()
        app.stream_stop_btn.place_forget.assert_called_once()
        app._poll_queues()
        self.assertGreater(len(app.left_panel.canvas.calls), 0)

    def test_gamepad_mode_gets_no_driver_link_or_pink(self):
        device = mock.Mock()
        with mock.patch.object(gui, "HAS_EVDEV", True), mock.patch.object(gui, "_make_uinput", lambda: (device, True)), \
                mock.patch.object(gui, "ecodes", mock.MagicMock(), create=True):
            app = self.app(mode="gamepad", right_pink="flux")
            app._start_bridge()
            self.assertIsInstance(app.active_mode, gui.GamepadMode)
            self.assertIsNone(app.driver_link)
            self.feed(app, 1, buttons2=gr.PINK)
            self.assertTrue(device.syn.called)                 # the uinput gamepad got the report
            app._stop_bridge()
        self.flux.send.assert_not_called()
        self.rx.settimeout(0.1)
        with self.assertRaises(socket.timeout):                # and the driver nothing
            self.rx.recv(4096)


if __name__ == "__main__":
    unittest.main()
