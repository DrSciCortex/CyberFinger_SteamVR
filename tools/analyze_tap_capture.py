# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Capture and analyse the headset hands' raw update stream as the CyberFinger driver sees it.

    python tools/analyze_tap_capture.py --capture 10     # ask the running driver for a 10 s capture, then analyse
    python tools/analyze_tap_capture.py [file.csv]       # analyse a capture (default: the newest)

Captures are a debugging tool: the driver only records them with 'Debug captures' (debug_captures) on in
SteamVR's settings for CyberFinger.

The driver records every pose (TrackedDevicePoseUpdated) and skeleton (UpdateSkeletonComponent, both motion
ranges) that the source hand devices submit, and what CyberFinger publishes at the same time, with microsecond
timestamps. This reports per hand:

  - rates, and how the calls bunch up: several calls within ~1 ms are one "burst" (one frame's worth);
  - whether the calls in a burst carry different data (e.g. a raw stage followed by a filtered one);
  - jitter of the fingertips (relative to the wrist) and of the wrist, using all calls, only the first
    call of each burst, and only the last: if the last is much smoother, apps (which read once per frame,
    after the burst) see filtered data while republishing every call passes on intermediate stages;
  - WithController vs WithoutController differences, and pose timing (poseTimeOffset);
  - the source hand vs CyberFinger's output, side by side: both as the world wrist (pose x wrist bone, the
    same physical frame for both) and a laser 2 m along the index finger, as SteamVR shows them now and
    extrapolated 40 ms ahead; jitter and error against the hand's path, for a still, slowly moving and fast
    moving hand; the delay of the output and how often it holds the hand (occlusion);
  - the CyberFinger IMUs, which the bridge (CyberFinger GUI) sends the driver during a capture:
    report rate, and against the optical rotation the timing offset and the rotation jitter while still.
"""

import argparse
import glob
import os
import sys
import time

import numpy as np

CAP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CyberFinger")
KIND = {7: "headset", 8: "body tracker (SlimeVR)",
        0: "pose", 1: "skeleton WithoutController", 2: "skeleton WithController",
        3: "CyberFinger pose", 4: "CyberFinger skel. Without", 5: "CyberFinger skel. With", 6: "CyberFinger IMU"}
IMU_SLOTS = ((0x1, "body 1"), (0x2, "body 2"), (0x4, "joint"))
LASER = 2.0                     # m: laser target distance along the index finger
CLASSES = (("still (<0.3 m/s)", 0, 0.3), ("slow (0.3-1.5)", 0.3, 1.5), ("fast (>1.5)", 1.5, 1e9))


def request_capture(seconds):
    os.makedirs(CAP_DIR, exist_ok=True)
    before = set(glob.glob(os.path.join(CAP_DIR, "captures", "tap_*.csv")))
    with open(os.path.join(CAP_DIR, "capture_request.txt"), "w") as f:
        f.write(str(seconds))
    print(f"capture requested ({seconds} s); hold your hands in view, still for part of it ...")
    deadline = time.time() + seconds + 15
    while time.time() < deadline:
        time.sleep(0.5)
        new = set(glob.glob(os.path.join(CAP_DIR, "captures", "tap_*.csv"))) - before
        if new:
            path = max(new, key=os.path.getmtime)
            size = -1                                         # let the writer finish: a long capture takes seconds
            while os.path.getsize(path) != size:
                size = os.path.getsize(path)
                time.sleep(1.5)
            return path
    sys.exit("no capture appeared: is SteamVR running with the CyberFinger driver, and 'Debug captures' on in "
             "SteamVR's settings for CyberFinger (advanced; setting debug_captures)?")


def bursts(t, gap=0.001):
    """Indices of the first call of each burst (calls closer than `gap` belong together)."""
    starts = np.r_[0, np.where(np.diff(t) > gap)[0] + 1]
    return starts


def residuals(t, x, window=0.1):
    """Per sample: distance (m) of x from its centred moving average over `window` s (high-pass jitter)."""
    flat = x.reshape(len(x), -1)
    c = np.vstack([np.zeros(flat.shape[1]), np.cumsum(flat, axis=0)])
    j0 = np.searchsorted(t, t - window / 2, side="left")
    j1 = np.searchsorted(t, t + window / 2, side="right")
    mean = (c[j1] - c[j0]) / (j1 - j0)[:, None]
    return np.linalg.norm((flat - mean).reshape(x.shape), axis=-1)


def jitter(t, x, window=0.1):
    """RMS distance (mm) of x from its centred moving average over `window` s (high-pass jitter)."""
    if len(t) < 10:
        return float("nan")
    d = residuals(t, x, window)
    return float(np.sqrt(np.mean(d ** 2)) * 1e3)


def centred_median(t, x, window=0.06):
    """The hand's path: a centred median over `window` s, per axis (short glitches don't move a median)."""
    j0 = np.searchsorted(t, t - window / 2, side="left")
    j1 = np.searchsorted(t, t + window / 2, side="right")
    return np.array([np.median(x[a:b], axis=0) for a, b in zip(j0, j1)])


# Quaternions as arrays (..., 4) = w x y z.
def qmul(a, b):
    aw, ax, ay, az = np.moveaxis(a, -1, 0)
    bw, bx, by, bz = np.moveaxis(b, -1, 0)
    return np.stack([aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw], axis=-1)


def qrot(q, v):
    u, w = q[..., 1:], q[..., :1]
    return v + 2 * np.cross(u, np.cross(u, v) + w * v)


def qexp(r):
    """Rotation by the rotation vector r (axis * angle)."""
    a = np.linalg.norm(r, axis=-1, keepdims=True)
    s = np.where(a > 1e-12, np.sin(a / 2) / np.maximum(a, 1e-12), 0.5)
    return np.concatenate([np.cos(a / 2), r * s], axis=-1)


def last_of_bursts(t, gap=0.0005):
    """Indices of the last call of each burst: what the driver's republishing (and an app) ends up with."""
    return np.r_[np.where(np.diff(t) > gap)[0], len(t) - 1]


def displayed(t, v, skel_t, skel_v, index_dir, ahead):
    """World wrist and laser target of a pose stream as SteamVR shows it `ahead` s after each update."""
    k = np.clip(np.searchsorted(skel_t, t, side="right") - 1, 0, len(skel_t) - 1)   # latest wrist bone
    wrist_p, wrist_q = skel_v[k, 0:3], skel_v[k, 3:7]
    dt = (ahead - v[:, 0])[:, None]                                  # from the pose's own time (poseTimeOffset)
    q = qmul(qexp(v[:, 11:14] * dt), v[:, 4:8])
    p = v[:, 1:4] + v[:, 8:11] * dt
    wp = p + qrot(q, wrist_p)
    wq = qmul(q, wrist_q)
    return wp, wp + LASER * qrot(wq, np.broadcast_to(index_dir, wp.shape))


def unit_quats(q):
    """Normalised, with signs made continuous (q and -q are the same rotation)."""
    q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-12)
    s = np.sign(np.sum(q[1:] * q[:-1], axis=1))
    s[s == 0] = 1
    q[1:] *= np.cumprod(s)[:, None]
    return q


def angle_between(a, b):
    return 2 * np.arccos(np.clip(np.abs(np.sum(a * b, axis=-1)), 0, 1))


def angular_speed(t, q, grid, span=0.04):
    """Rotation rate (rad/s) over ~`span` s, at the grid times: independent of the sensor's frame. Measured
    between the stream's own samples and placed at their midpoint, so its sample rate doesn't shift it."""
    j = np.clip(np.searchsorted(t, t + span, side="left"), 0, len(t) - 1)
    ok = t[j] > t
    i, j = np.nonzero(ok)[0], j[ok]
    return np.interp(grid, (t[i] + t[j]) / 2, angle_between(q[i], q[j]) / (t[j] - t[i]))


def rotation_residuals(t, q, window=0.1):
    """Per sample: angle (rad) from the centred mean orientation over `window` s."""
    c = np.vstack([np.zeros(4), np.cumsum(q, axis=0)])
    j0 = np.searchsorted(t, t - window / 2, side="left")
    j1 = np.searchsorted(t, t + window / 2, side="right")
    m = c[j1] - c[j0]
    return angle_between(q, m / np.linalg.norm(m, axis=1, keepdims=True))


def compare_imu(data, v, hand):
    """The CyberFinger IMUs against the optical rotation (source and CyberFinger), on the same clock."""
    sel = (data["hand"] == hand) & (data["kind"] == 6)
    if sel.sum() < 20:
        print("  CyberFinger IMU: not recorded (bridge not running, or an older bridge)")
        return
    t_arr, vi = data["t"][sel], v[sel]
    delay = vi[:, 21]
    ti = t_arr - delay                                  # when the report reached the bridge over BLE
    present = int(np.bincount(data["changed"][sel].astype(int)).argmax())
    names = [n for bit, n in IMU_SLOTS if present & bit]
    dur = ti[-1] - ti[0]
    dt = np.diff(ti)
    print(f"  CyberFinger IMU: {len(ti) / dur:.0f} reports/s (interval median {np.median(dt) * 1e3:.1f} ms,"
          f" p99 {np.percentile(dt, 99) * 1e3:.0f}); slots {', '.join(names) or 'none'};"
          f" BLE arrival → driver median {np.median(delay) * 1e3:.2f} ms")
    imus = []
    if present & 0x3:                                   # body 1 and body 2 sit at the same spot: take one
        col = 0 if present & 0x1 else 4
        imus.append(("IMU body", unit_quats(vi[:, col:col + 4].copy())))
    if present & 0x4:
        imus.append(("IMU joint", unit_quats(vi[:, 8:12].copy())))
    optical = []
    for kind, name in ((0, "source"), (3, "CyberFinger")):
        s = (data["hand"] == hand) & (data["kind"] == kind)
        if s.sum() > 50:
            ts = data["t"][s]
            keep = last_of_bursts(ts)
            optical.append((name, ts[keep], unit_quats(v[s][keep][:, 4:8].copy())))
    if not imus or not optical:
        return
    lo = max(ti[0], max(o[1][0] for o in optical)) + 0.1
    hi = min(ti[-1], min(o[1][-1] for o in optical)) - 0.1
    if hi - lo < 2:
        print("  CyberFinger IMU and optical overlap < 2 s")
        return
    grid = np.arange(lo, hi, 0.001)
    speeds = {name: angular_speed(ti, q, grid) for name, q in imus}
    speeds.update({name: angular_speed(t, q, grid) for name, t, q in optical})
    lags = np.arange(-200, 201) * 0.001

    def best_lag(a, b):
        """The shift L that best matches a(t) with b(t - L): a trails b by L (s), and the correlation."""
        best = (0.0, -1.0)
        for L in lags:
            bs = np.interp(grid - L, grid, b)
            m = (grid - L >= grid[0]) & (grid - L <= grid[-1])
            if m.sum() > 100 and np.std(a[m]) > 0 and np.std(bs[m]) > 0:
                r = float(np.corrcoef(a[m], bs[m])[0, 1])
                if r > best[1]:
                    best = (float(L), r)
        return best

    # Still: neither the camera nor the CyberFinger sees the hand turning.
    still = np.all([s < 0.3 for s in speeds.values()], axis=0)
    print(f"  rotation vs the CyberFinger IMUs ({still.mean() * 100:.0f} % of the overlap still;"
          f" timing from the rotation rate, so no IMU calibration needed):")
    for name, t, q in optical:
        for iname, _ in imus:
            L, r = best_lag(speeds[name], speeds[iname])
            print(f"    {name:11s} trails {iname:9s} by {L * 1e3:+4.0f} ms (rate correlation {r:.2f})")
    if len(imus) == 2:                                  # the two CyberFinger IMUs against each other (firmware filters)
        L, r = best_lag(speeds["IMU body"], speeds["IMU joint"])
        print(f"    IMU body    trails IMU joint by {L * 1e3:+4.0f} ms (rate correlation {r:.2f}; same packets)")
    # Jitter at the same instants for all (the optical streams sampled at the IMU's report times), so a
    # different rate doesn't change the comparison.
    rows = [(name, q) for name, q in imus]
    rows += [(name, q[np.clip(np.searchsorted(t, ti, side="right") - 1, 0, len(t) - 1)]) for name, t, q in optical]
    m = np.interp(ti, grid, still.astype(float), left=0, right=0) > 0.5
    if m.sum() < 20:
        return
    for name, q in rows:
        j = np.sqrt(np.mean(rotation_residuals(ti, q)[m] ** 2))
        print(f"    {name:11s} rotation jitter while still {np.degrees(j):5.2f} deg"
              f" (= {j * LASER * 1e3:5.1f} mm at the {LASER:.0f} m laser target), at the IMU's report times")


def compare(data, v, hand):
    """The source hand vs CyberFinger's output over the same seconds."""
    sel = {k: (data["hand"] == hand) & (data["kind"] == k) for k in (0, 1, 3, 4)}
    if sel[3].sum() < 50:
        print("  CyberFinger's output: not recorded (older driver, or CyberFinger inactive)")
        return
    if min(sel[k].sum() for k in (0, 1, 4)) < 5:
        print("  CyberFinger vs source: missing source poses or skeletons")
        return
    ts, vs = data["t"][sel[0]], v[sel[0]]
    to, vo = data["t"][sel[3]], v[sel[3]]
    ks_t, ks_v = data["t"][sel[1]], v[sel[1]]
    ko_t, ko_v = data["t"][sel[4]], v[sel[4]]
    # Where each output update came from: the time since the latest source pose.
    j = np.searchsorted(ts, to, side="right") - 1
    delay = (to[j >= 0] - ts[j[j >= 0]]) * 1e3
    held = np.r_[False, np.all(vo[1:, 1:8] == vo[:-1, 1:8], axis=1)]
    print(f"  CyberFinger vs source ({len(to) / (to[-1] - to[0]):.0f} vs {len(ts) / (ts[-1] - ts[0]):.0f} poses/s):"
          f" output follows its source pose after median {np.median(delay):.2f} ms (p99 {np.percentile(delay, 99):.1f});"
          f" repeats the previous pose {held.mean() * 100:.1f} % of updates (occlusion hold);"
          f" valid {vo[:, 17].mean() * 100:.0f} %")
    ls, lo = last_of_bursts(ts), last_of_bursts(to)
    ts, vs, to, vo = ts[ls], vs[ls], to[lo], vo[lo]
    index_dir = ks_v[:, 10:13].mean(axis=0)                          # wrist -> index tip, in the wrist frame
    index_dir /= np.linalg.norm(index_dir)
    # The hand's path, from the source: centred 60 ms median of the wrist and laser target as streamed.
    src_w0, src_l0 = displayed(ts, vs, ks_t, ks_v, index_dir, 0.0)
    path_w, path_l = centred_median(ts, src_w0), centred_median(ts, src_l0)
    a = np.searchsorted(ts, ts - 0.015, side="left")
    b = np.clip(np.searchsorted(ts, ts + 0.015, side="right") - 1, 0, len(ts) - 1)
    speed = np.linalg.norm(path_l[b] - path_l[a], axis=1) / np.maximum(ts[b] - ts[a], 1e-6)

    def path_at(path, t):
        return np.stack([np.interp(t, ts, path[:, i]) for i in range(3)], axis=1)

    def cls_at(t):
        return np.interp(t, ts, speed)

    # Delay of the output: the shift that best aligns its (unextrapolated) wrist path with the source's.
    out_w0, _ = displayed(to, vo, ko_t, ko_v, index_dir, 0.0)
    moving = cls_at(to) > 0.3
    lag = 0.0
    if moving.sum() > 50:
        shifts = np.arange(-20, 61, 1) * 1e-3
        err = [np.sqrt(np.mean(np.sum((out_w0[moving] - path_at(path_w, to[moving] - s)) ** 2, axis=1)))
               for s in shifts]
        lag = shifts[int(np.argmin(err))]
        lag_text = f"{lag * 1e3:.0f} ms"
    else:
        lag_text = "n/a (no motion)"
    print(f"  output wrist trails the source's by {lag_text} while moving; mm below, source -> CyberFinger:"
          f" jitter (from a 100 ms mean) | error vs the hand's path, rms / p99")
    for ahead in (0.0, 0.04):
        print(f"    {ahead * 1e3:2.0f} ms ahead")
        rows = []
        # The output is classed by the source motion it shows (lag earlier), so a glitch falls in the same row.
        for t, vv, kt, kv, shift in ((ts, vs, ks_t, ks_v, 0.0), (to, vo, ko_t, ko_v, lag)):
            w, l = displayed(t, vv, kt, kv, index_dir, ahead)
            rows.append((t, cls_at(t - shift), residuals(t, w), residuals(t, l),
                         np.linalg.norm(w - path_at(path_w, t + ahead), axis=1),
                         np.linalg.norm(l - path_at(path_l, t + ahead), axis=1)))
        for name, lo_s, hi_s in CLASSES:
            cells = []
            for (t, c, jw, jl, ew, el) in rows:
                m = (c >= lo_s) & (c < hi_s) & (t + ahead <= ts[-1])
                if m.sum() < 10:
                    cells.append(None)
                    continue
                rms = lambda x: np.sqrt(np.mean(x[m] ** 2)) * 1e3      # noqa: E731
                cells.append((m.mean() * 100, rms(jw), rms(ew), np.percentile(ew[m], 99) * 1e3,
                              rms(jl), rms(el), np.percentile(el[m], 99) * 1e3))
            if cells[0] is None or cells[1] is None:
                continue
            s, o = cells
            print(f"      {name:17s} {s[0]:3.0f}%  wrist {s[1]:5.1f} -> {o[1]:5.1f} | {s[2]:5.1f} / {s[3]:4.0f} ->"
                  f" {o[2]:5.1f} / {o[3]:4.0f}    laser {s[4]:6.1f} -> {o[4]:6.1f} | {s[5]:6.1f} / {s[6]:5.0f} ->"
                  f" {o[5]:6.1f} / {o[6]:5.0f}")


BODY = ["left elbow", "right elbow", "chest", "waist", "left knee", "right knee", "left foot", "right foot",
        "left hand", "right hand", "left shoulder", "right shoulder", "left upper arm", "right upper arm"]


def body_summary(data, v):
    """The headset (kind 7) and SlimeVR's body trackers (kind 8, by role code) in the capture: rates, validity, and
    for an elbow its distance to the same side's headset-tracked hand (roughly the forearm: ~25-30 cm)."""
    for kind, code, name in [(7, 0, "headset")] + [(8, c, n) for c, n in enumerate(BODY)]:
        sel = (data["kind"] == kind) & (data["hand"] == code)
        if sel.sum() < 5:
            continue
        t, vv = data["t"][sel], v[sel]
        line = f"  {name:15s} {len(t) / (t[-1] - t[0]):5.0f} poses/s, valid {vv[:, 17].mean() * 100:3.0f} %"
        if kind == 8 and code in (0, 1):
            h = (data["kind"] == 0) & (data["hand"] == code) & (v[:, 17] > 0.5)
            if h.sum() > 10:
                th, ph = data["t"][h], v[h][:, 1:4]
                ok = (t >= th[0]) & (t <= th[-1]) & (vv[:, 17] > 0.5)
                hand = np.stack([np.interp(t[ok], th, ph[:, i]) for i in range(3)], 1)
                d = np.linalg.norm(vv[ok][:, 1:4] - hand, axis=1) * 100
                if len(d):
                    line += (f"; to the tracked {name.split()[0]} hand {np.median(d):.1f} cm median "
                             f"({np.percentile(d, 10):.1f}-{np.percentile(d, 90):.1f})")
        print(line)
    print()


def analyse(path):
    data = np.genfromtxt(path, delimiter=",", names=True)
    print(f"{os.path.basename(path)}: {len(data)} calls over {data['t'][-1] - data['t'][0]:.1f} s\n")
    v = np.stack([data[f"v{i}"] for i in range(22)], axis=1)
    body_summary(data, v)
    for hand in (0, 1):
        print(f"══ {'left' if hand == 0 else 'right'} hand ══")
        for kind in (0, 1, 2, 3, 4, 5):
            sel = (data["hand"] == hand) & (data["kind"] == kind)
            if sel.sum() < 5:
                if kind < 3:
                    print(f"  {KIND[kind]:27s} no data")
                continue
            t = data["t"][sel]
            vv = v[sel]
            dur = t[-1] - t[0]
            b = bursts(t)
            sizes = np.diff(np.r_[b, len(t)])
            changed = data["changed"][sel]
            frame_iv = np.diff(t[b]) * 1e3
            print(f"  {KIND[kind]:27s} {len(t) / dur:6.1f} calls/s, {changed.mean() * len(t) / dur:6.1f} with new data/s,"
                  f" {len(b) / dur:6.1f} bursts/s (sizes {np.bincount(sizes).tolist()[1:]}),"
                  f" burst interval median {np.median(frame_iv):.1f} ms")
            if kind in (0, 3):
                pos = vv[:, 1:4]
                off = vv[:, 0] * 1e3
                print(f"      poseTimeOffset ms: median {np.median(off):.1f}, range {off.min():.1f} .. {off.max():.1f};"
                      f" |velocity| median {np.median(np.linalg.norm(vv[:, 8:11], axis=1)):.3f} m/s,"
                      f" |accel| median {np.median(np.linalg.norm(vv[:, 14:17], axis=1)):.2f} m/s²")
                if sizes.max() > 1:
                    within = [np.linalg.norm(pos[s + k] - pos[s]) * 1e3 for s, n in zip(b, sizes) for k in range(1, n)]
                    offs = [off[s:s + n] for s, n in zip(b, sizes) if n > 1][:3]
                    print(f"      within a burst the position moves median {np.median(within):.2f} mm (max {np.max(within):.2f});"
                          f" poseTimeOffsets in a burst e.g. {[np.round(o, 1).tolist() for o in offs]}")
                x = pos
                label = "wrist position"
            else:
                tips = vv[:, 7:22].reshape(-1, 5, 3)
                if sizes.max() > 1:
                    within = [np.linalg.norm(tips[s + k] - tips[s], axis=1).max() * 1e3
                              for s, n in zip(b, sizes) for k in range(1, n)]
                    print(f"      within a burst the fingertips change median {np.median(within):.2f} mm"
                          f" (max {np.max(within):.2f})")
                x = tips
                label = "fingertips (rel. wrist)"
            last = np.r_[b[1:] - 1, len(t) - 1]
            j_all = jitter(t, x)
            j_first = jitter(t[b], x[b])
            j_last = jitter(t[last], x[last])
            print(f"      jitter of {label}: all calls {j_all:.2f} mm, first of burst {j_first:.2f} mm,"
                  f" last of burst {j_last:.2f} mm")
        # The two skeleton streams side by side.
        s1 = (data["hand"] == hand) & (data["kind"] == 1)
        s2 = (data["hand"] == hand) & (data["kind"] == 2)
        if s1.sum() > 5 and s2.sum() > 5:
            t1, t2 = data["t"][s1], data["t"][s2]
            a = v[s1][:, 7:22].reshape(-1, 5, 3)
            c = v[s2][:, 7:22].reshape(-1, 5, 3)
            idx = np.clip(np.searchsorted(t2, t1), 0, len(t2) - 1)
            d = np.linalg.norm(a - c[idx], axis=2).max(axis=1) * 1e3
            print(f"  WithController vs WithoutController: fingertips differ median {np.median(d):.2f} mm,"
                  f" p95 {np.percentile(d, 95):.2f} mm")
        compare(data, v, hand)
        compare_imu(data, v, hand)
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", help="capture CSV (default: newest)")
    ap.add_argument("--capture", type=float, metavar="SECONDS", help="request a new capture from the driver first")
    args = ap.parse_args()
    if args.capture:
        path = request_capture(args.capture)
    elif args.file:
        path = args.file
    else:
        files = glob.glob(os.path.join(CAP_DIR, "captures", "tap_*.csv"))
        if not files:
            sys.exit(f"no captures in {os.path.join(CAP_DIR, 'captures')}")
        path = max(files, key=os.path.getmtime)
    analyse(path)


if __name__ == "__main__":
    main()
