# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Driver → bridge link: what the CyberFinger SteamVR driver sends back.

The driver sends to UDP 127.0.0.1:27016:
  CFHP  haptic requests — an application asked a CyberFinger hand to vibrate (duration, frequency, amplitude)
  CFOP  its context stream — HMD pose, per-hand output mode, the headset's hand tracking (~120 Hz)

DriverLink listens there in a background thread. Haptic requests go to a HapticTracker (which the GUI draws)
and to an optional on_haptic callback — the place to forward them to the CyberFinger over GATT once the firmware
has an actuator. The latest context packet is kept for status displays.
"""

import socket
import threading
import time

import cf_protocol


class HapticTracker:
    """The latest haptic request per hand, readable from the UI thread."""

    PULSE_S = 0.08       # a zero-duration request ("single pulse") shows for this long
    AFTERGLOW_S = 0.15   # fade after the request ends, so short pulses stay visible

    def __init__(self):
        self._lock = threading.Lock()
        self._last = [None, None]    # (t, duration, frequency, amplitude)
        self._count = [0, 0]

    def add(self, hand, duration, frequency, amplitude, t=None):
        with self._lock:
            self._last[hand] = (time.perf_counter() if t is None else t, max(0.0, float(duration)),
                                max(0.0, float(frequency)), max(0.0, min(1.0, float(amplitude))))
            self._count[hand] += 1

    def state(self, hand, now=None):
        """on: a vibration is requested right now. level: 0..1 LED brightness (amplitude, fading after).
        remaining: 1 → 0 over the requested duration."""
        now = time.perf_counter() if now is None else now
        with self._lock:
            last, count = self._last[hand], self._count[hand]
        if last is None:
            return {"on": False, "level": 0.0, "remaining": 0.0, "duration": 0.0, "frequency": 0.0,
                    "amplitude": 0.0, "count": 0, "age": None}
        t0, duration, frequency, amplitude = last
        span = max(duration, self.PULSE_S)
        age = now - t0
        on = age < span
        level = amplitude if on else amplitude * max(0.0, 1.0 - (age - span) / self.AFTERGLOW_S)
        return {"on": on, "level": level, "remaining": max(0.0, 1.0 - age / span) if on else 0.0,
                "duration": duration, "frequency": frequency, "amplitude": amplitude, "count": count, "age": age}


class DriverLink:
    """Receives the driver's haptic requests and context stream on UDP 127.0.0.1:27016."""

    def __init__(self, port=cf_protocol.CONTEXT_PORT, on_haptic=None, log=None, name="SteamVR driver"):
        self.port = port
        self.name = name             # the driver in the log lines (the Linux bridge's is WiVRn's)
        self.on_haptic = on_haptic
        self.haptics = HapticTracker()
        self._log = log or (lambda _msg: None)
        self._sock = None
        self._thread = None
        self._running = False
        self._context_raw = None
        self._context_time = 0.0
        self._context_cache = (None, None)

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as e:
            sock.close()
            self._log(f"{self.name} link: UDP {self.port} is busy ({e}); haptics display off")
            return False
        sock.settimeout(0.25)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._run, name="cf-driver-link", daemon=True)
        self._thread.start()
        self._log(f"{self.name} link: listening on UDP {self.port} (haptics, driver status)")
        return True

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def context(self, max_age=0.5):
        """The latest CFOP as a dict (see cf_protocol.unpack_context), or None if stale."""
        raw = self._context_raw
        if raw is None or time.perf_counter() - self._context_time > max_age:
            return None
        cached_raw, cached = self._context_cache
        if cached_raw is not raw:
            cached = cf_protocol.unpack_context(raw)
            self._context_cache = (raw, cached)
        return cached

    def _run(self):
        while self._running:
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 4:
                continue
            magic = int.from_bytes(data[:4], "little")
            if magic == cf_protocol.MAGIC_HAPTIC:
                h = cf_protocol.unpack_haptic(data)
                if h is None:
                    continue
                self.haptics.add(h["hand"], h["duration_s"], h["frequency_hz"], h["amplitude"])
                if self.on_haptic is not None:
                    try:
                        self.on_haptic(h)
                    except Exception as e:  # a UI callback must not kill the link
                        self._log(f"{self.name} link: haptic handler failed: {e!r}")
            elif magic == cf_protocol.MAGIC_CONTEXT:
                # Parsed on demand (context()): unpacking 62 bones at 120 Hz nobody reads is waste.
                self._context_raw = data
                self._context_time = time.perf_counter()
