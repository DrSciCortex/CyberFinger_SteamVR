# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Probe the CyberFinger SteamVR driver while SteamVR runs.

    python tools/cf_driver_probe.py                 # print what the driver sees, twice a second
    python tools/cf_driver_probe.py --fake-hand right   # also drive the right hand in FUSED mode

Reads the driver's context stream (CFOP, UDP 127.0.0.1:27016): HMD pose, the driver's output mode per
hand, and the optical tap — Steam Link's hand device pose, whether its skeleton is being captured, the
bone count and age, and the forwarded system button (Quest left palm pinch → ≡). Haptic requests (CFHP)
are printed as they arrive.

--fake-hand sends a synthetic fused hand (CFHS) that floats in front of the headset and slowly opens and
closes, to exercise the FUSED path. Stop it and the hand falls back to
PASSTHROUGH within fused_timeout_ms.

Only one program can own port 27016: stop the CyberFinger link in the CyberFinger GUI while probing.
"""

import argparse
import math
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cf_protocol as cfp  # noqa: E402

TRACKING = {1: "Uninit", 100: "Calib", 101: "CalibOOR", 200: "OK", 201: "OOR", 300: "RotOnly"}


def qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def qrot(q, v):
    w, x, y, z = qmul(qmul(q, (0.0, *v)), (q[0], -q[1], -q[2], -q[3]))
    return (x, y, z)


def fmt_tap(t):
    if not t["source_found"]:
        return "no headset hand device"
    parts = [f"pose {'ok' if t['pose_valid'] else '--'}({TRACKING.get(t['tracking_result'], t['tracking_result'])})"]
    if t["bone_count"]:
        parts.append(f"skeleton {'live' if t['skel_valid'] else 'stale'} {t['bone_count']} bones "
                     f"{t['skel_age_us'] / 1000:.0f} ms")
    else:
        parts.append("no skeleton")
    if t["system_click"]:
        parts.append("[SYSTEM pressed]")
    return ", ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="CyberFinger driver probe")
    ap.add_argument("--port", type=int, default=cfp.CONTEXT_PORT)
    ap.add_argument("--fake-hand", choices=("left", "right"))
    args = ap.parse_args(argv)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind(("127.0.0.1", args.port))
    except OSError as e:
        print(f"cannot bind 127.0.0.1:{args.port} ({e}) - is the CyberFinger link of the CyberFinger GUI running?")
        return 1
    rx.settimeout(0.5)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake = None if args.fake_hand is None else (0 if args.fake_hand == "left" else 1)

    print(f"listening for the driver on 127.0.0.1:{args.port} (Ctrl+C to stop)")
    last_print = 0.0
    frames = 0
    seq = 0
    t0 = time.perf_counter()
    try:
        while True:
            try:
                data, _ = rx.recvfrom(4096)
            except socket.timeout:
                print("... no context packets - is SteamVR running with the CyberFinger driver enabled?")
                continue
            hap = cfp.unpack_haptic(data)
            if hap is not None:
                dur = "pulse" if hap["duration_s"] <= 0 else f"{hap['duration_s'] * 1000:.0f} ms"
                print(f"   HAPTIC {'LR'[hap['hand']]}: {hap['frequency_hz']:.0f} Hz, {dur}, "
                      f"amplitude {hap['amplitude']:.2f}")
                continue
            ctx = cfp.unpack_context(data)
            if ctx is None:
                continue
            frames += 1
            now = time.perf_counter()

            if fake is not None and ctx["hmd_valid"]:
                # 35 cm ahead, 25 cm down and 20 cm to the side of the headset, palm-ish down.
                side = 0.2 if fake == 1 else -0.2
                offset = qrot(ctx["hmd_rot"], (side, -0.25, -0.35))
                pos = tuple(p + o for p, o in zip(ctx["hmd_pos"], offset))
                curl = 0.5 - 0.5 * math.cos(2 * math.pi * (now - t0) / 4.0)
                seq += 1
                pkt = cfp.pack_hand_state(fake, seq, pos, ctx["hmd_rot"], curl=(curl,) * 5,
                                          optical_seq=ctx["seq"])
                tx.sendto(pkt, ("127.0.0.1", cfp.DRIVER_PORT))

            if now - last_print >= 0.5:
                rate = frames / (now - last_print) if last_print else 0.0
                frames = 0
                last_print = now
                print(f"[{ctx['seq']:>7}] {rate:5.0f} Hz  HMD {'ok' if ctx['hmd_valid'] else '--'}  "
                      f"hook {'on' if ctx['tap_hook_ok'] else 'OFF'}")
                for h, name in ((0, "L"), (1, "R")):
                    mode = cfp.MODE_NAMES.get(ctx["mode"][h], ctx["mode"][h])
                    extra = f"  (applied CFHS #{ctx['applied_hs_seq'][h]})" if ctx["mode"][h] == 1 else ""
                    print(f"   {name}: {mode:<11} tap: {fmt_tap(ctx['tap'][h])}{extra}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
