# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Haptics indicator for the bridge GUIs (Tk canvas).

Shows what a vibration request from SteamVR asks of the CyberFinger:
  LED        lit while the vibration is requested; brightness = amplitude, short fade afterwards
  waveform   drawn at the requested frequency (more cycles = higher frequency), height = amplitude
  bar        time left of the requested duration
  readout    frequency · duration · amplitude, and the number of requests so far
"""

import math

# Waveform: one visible cycle per this many Hz, clamped to a readable range.
HZ_PER_CYCLE = 40.0
MIN_CYCLES, MAX_CYCLES = 1.0, 10.0


def _blend(c1, c2, t):
    t = max(0.0, min(1.0, t))
    a, b = int(c1[1:], 16), int(c2[1:], 16)
    parts = []
    for shift in (16, 8, 0):
        va, vb = (a >> shift) & 255, (b >> shift) & 255
        parts.append(int(va + (vb - va) * t))
    return "#%02x%02x%02x" % tuple(parts)


def describe(st):
    """Short text for a HapticTracker state, e.g. '160 Hz  25 ms  75%'."""
    if not st["count"]:
        return "no haptics yet"
    dur = "pulse" if st["duration"] <= 0 else f"{st['duration'] * 1000:.0f} ms"
    freq = f"{st['frequency']:.0f} Hz" if st["frequency"] > 0 else "default"
    return f"{freq}  {dur}  {st['amplitude'] * 100:.0f}%"


def draw_haptic_meter(c, x, y, w, h, st, now, label="", accent="#e6007e", bg="#1a1a1a",
                      dim="#888888", fg="#e0e0e0", line="#2e2e2e", text_w=150, font=("Consolas", 8)):
    """Draw one hand's haptic state into canvas c, within the box (x, y, w, h)."""
    cy = y + h / 2.0
    r = max(3.0, h / 2.0 - 2.0)
    x0 = x
    if label:
        c.create_text(x0, cy, text=label, fill=fg if st["on"] else dim, font=font, anchor="w")
        x0 += 8 * len(label) + 4

    # LED
    lit = _blend(bg, accent, 0.25 + 0.75 * st["level"]) if st["level"] > 0.01 else bg
    c.create_oval(x0, cy - r, x0 + 2 * r, cy + r, fill=lit, outline=accent if st["on"] else line, width=2)
    x0 += 2 * r + 6

    # Waveform at the requested frequency, scrolling while active
    wave_w = max(20.0, w - (x0 - x) - text_w)
    if st["on"] or st["level"] > 0.01:
        cycles = max(MIN_CYCLES, min(MAX_CYCLES, (st["frequency"] or 3 * HZ_PER_CYCLE) / HZ_PER_CYCLE))
        amp = (h / 2.0 - 3.0) * max(0.15, st["level"])
        phase = now * 2.0
        n = 48
        pts = []
        for i in range(n + 1):
            u = i / n
            pts.extend((x0 + u * wave_w, cy - amp * math.sin(2 * math.pi * (cycles * u + phase))))
        c.create_line(*pts, fill=accent if st["on"] else _blend(bg, accent, st["level"]), width=2, smooth=True)
    else:
        c.create_line(x0, cy, x0 + wave_w, cy, fill=line, width=1)

    # Time left of the requested duration
    if st["on"] and st["remaining"] > 0:
        c.create_line(x0, y + h - 1, x0 + wave_w * st["remaining"], y + h - 1, fill=accent, width=2)

    # Readout
    text = describe(st) + (f"  #{st['count']}" if st["count"] else "")
    c.create_text(x0 + wave_w + 8, cy, text=text, fill=fg if st["on"] else dim, font=font, anchor="w")
