# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
OpenXR hand-skeleton source for the CyberFinger GUI.

A cross-platform (Windows + Linux) alternative to OpenVRSkeletonSource, reading
the 26-joint hand skeleton through OpenXR's XR_EXT_hand_tracking. It exposes the
SAME duck-typed contract the GUI already consumes, so it drops straight into
create_skeleton_source() and needs no changes to SkeletonPanel:

    .start() / .stop()          non-blocking
    .hands       [left, right]  each None, or a 26-tuple of (x,y,z) model-space joints
    .pose_info   [left, right]  each None, or (rot_3x3_rows, head_local_pos, dist, head_local_vel)
    .status      short string for the panel placeholder

The actual OpenXR session runs in a helper subprocess (openxr_skeleton_provider.py)
because an OpenXR session needs a GL/GLFW binding that is not safe to create on a
background thread; this class just manages that process and parses its UDP stream.
OpenVRSkeletonSource is untouched and remains the default on Windows.
"""

import json
import os
import socket
import subprocess
import sys
import threading


def openxr_available():
    """True if pyopenxr imports (the loader is present). The provider needs it;
    this lets create_skeleton_source pick a backend without spawning anything."""
    try:
        import xr  # noqa: F401
        return True
    except Exception:
        return False


_PROVIDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "openxr_skeleton_provider.py")


class OpenXRHandSkeletonSource:
    """Runs the OpenXR provider subprocess and turns its UDP stream into the
    same .hands/.pose_info/.status a SkeletonPanel expects."""

    RECV_TIMEOUT = 0.5   # s — also how often provider liveness is checked

    def __init__(self, log=None):
        self._log = log or (lambda _m: None)
        self.hands = [None, None]        # 0 = left, 1 = right
        self.pose_info = [None, None]
        # V2 live-gate feed: world-frame joints + flags/radius/activity per hand,
        # and the HMD world pose. Present when the provider is the V2 build; the
        # Gate tab uses these to run the real OcclusionGate per hand.
        self.world_joints = [None, None]   # each None or 26-tuple of (x,y,z) world
        self.jflags = [None, None]         # each None or 26-tuple of OpenXR flags
        self.jradius = [None, None]        # each None or 26-tuple of joint radii (m)
        self.active = [0, 0]               # per-hand is_active
        self.wrist_valid = [0, 0]          # per-hand wrist pos+orient valid
        self.hmd = None                    # {"pos":[x,y,z], "quat":[x,y,z,w], "valid":0/1}
        self.status = "starting..."
        self.log_path = None             # set before start() to also capture raw joints to CSV
        self._running = False
        self._thread = None
        self._sock = None
        self._proc = None
        self._errlog = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))            # ephemeral; provider sends here
        port = self._sock.getsockname()[1]
        self._sock.settimeout(self.RECV_TIMEOUT)

        # stderr -> a file (not a PIPE) so a chatty provider can't deadlock on a
        # full pipe buffer; we tail it if the process dies.
        cfg_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                               "CyberFingerBridge")
        try:
            os.makedirs(cfg_dir, exist_ok=True)
            self._errlog = open(os.path.join(cfg_dir, "openxr_provider.log"), "w")
        except Exception:
            self._errlog = None
        cmd = [sys.executable, _PROVIDER, "--port", str(port)]
        if self.log_path:
            cmd += ["--log", self.log_path]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL,
                stderr=(self._errlog or subprocess.DEVNULL))
            self._log(f"Skeleton(OpenXR): provider started (pid {self._proc.pid}, udp {port})"
                      + (f" — logging joints to {self.log_path}" if self.log_path else ""))
        except Exception as e:
            self.status = "OpenXR provider failed to launch"
            self._log(f"Skeleton(OpenXR): could not launch provider — {e!r}")
            self._proc = None

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._errlog is not None:
            try:
                self._errlog.close()
            except Exception:
                pass
            self._errlog = None
        self.hands = [None, None]
        self.pose_info = [None, None]
        self.world_joints = [None, None]
        self.jflags = [None, None]
        self.jradius = [None, None]
        self.active = [0, 0]
        self.wrist_valid = [0, 0]
        self.hmd = None

    # ── receive loop ─────────────────────────────────────────────────────────
    def _run(self):
        while self._running:
            # provider died? surface why (last line of its stderr log) and stop.
            if self._proc is not None and self._proc.poll() is not None:
                self.status = self._exit_reason()
                self.hands = [None, None]
                self.pose_info = [None, None]
                break
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._apply(json.loads(data.decode("utf-8")))
            except Exception:
                pass  # a malformed datagram is not worth killing the loop

    def _exit_reason(self):
        tail = ""
        try:
            if self._errlog is not None:
                self._errlog.flush()
            path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                "CyberFingerBridge", "openxr_provider.log")
            with open(path, "r") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            tail = lines[-1] if lines else ""
        except Exception:
            pass
        code = self._proc.returncode if self._proc is not None else "?"
        self._log(f"Skeleton(OpenXR): provider exited ({code}) {tail}")
        return "OpenXR provider stopped — see console"

    def _apply(self, pkt):
        """Parse one provider datagram into the GUI-facing attributes. Builds new
        lists and swaps them in atomically (the GUI reads on the main thread)."""
        hands = pkt.get("hands") or [None, None]
        new_hands = [None, None]
        new_pose = [None, None]
        new_wj = [None, None]
        new_flags = [None, None]
        new_rad = [None, None]
        new_active = [0, 0]
        new_wrist = [0, 0]
        for h in (0, 1):
            hp = hands[h] if h < len(hands) else None
            if not hp:
                continue
            joints = hp.get("joints")
            if joints and len(joints) >= 26:
                new_hands[h] = tuple((float(j[0]), float(j[1]), float(j[2])) for j in joints)
            pose = hp.get("pose")
            if pose:
                r = pose["rot"]
                rot = ((r[0], r[1], r[2]), (r[3], r[4], r[5]), (r[6], r[7], r[8]))
                new_pose[h] = (rot, tuple(pose["local"]), float(pose["dist"]), tuple(pose["vel"]))
            wj = hp.get("wjoints")
            if wj and len(wj) >= 26:
                new_wj[h] = tuple((float(j[0]), float(j[1]), float(j[2])) for j in wj)
            fl = hp.get("flags")
            if fl and len(fl) >= 26:
                new_flags[h] = tuple(int(x) for x in fl)
            rd = hp.get("radius")
            if rd and len(rd) >= 26:
                new_rad[h] = tuple(float(x) for x in rd)
            new_active[h] = int(hp.get("active", 0))
            new_wrist[h] = int(hp.get("wrist_valid", 0))
        self.hands = new_hands
        self.pose_info = new_pose
        self.world_joints = new_wj
        self.jflags = new_flags
        self.jradius = new_rad
        self.active = new_active
        self.wrist_valid = new_wrist
        self.hmd = pkt.get("hmd")
        self.status = pkt.get("status", self.status)
