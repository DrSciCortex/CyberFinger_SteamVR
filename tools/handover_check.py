"""What does the headset's hand stream do when the hand leaves the view? A timeline from a capture (numpy only).

    python tools/handover_check.py <capture.csv> [--step 0.5]

Needs a capture with the CyberFinger IMUs and the headset (kind 7). Per hand, on a 100 Hz grid: whether the skeleton
still changes (the driver's "seen" today), the pose's time offset and validity, where the hand is relative to the
headset (angle off its forward direction, height below it), and how far the optical orientation is from the CyberFinger
IMU's (calibrated robustly: fitted, then refitted on the samples that agree). Printed per step: the story around
each exit and return.
"""
import argparse
import os

import numpy as np

from arm_chain_check import C_IMU, interp_quat, load, mount_fit, quat_to_mat, yaw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('capture')
    ap.add_argument('--step', type=float, default=0.5)
    a = ap.parse_args()
    path = a.capture if os.path.exists(a.capture) else os.path.join(
        os.environ.get('LOCALAPPDATA', ''), 'CyberFinger', 'captures', a.capture)
    rows = load(path)
    hmd = rows[rows[:, 2] == 7]
    dt = 0.01
    out = {}
    for hand in (0, 1):
        o = rows[(rows[:, 1] == hand) & (rows[:, 2] == 0)]
        sk = rows[(rows[:, 1] == hand) & (rows[:, 2] == 1)]
        im = rows[(rows[:, 1] == hand) & (rows[:, 2] == 6) & ((rows[:, 3].astype(int) & 4) == 4)]
        t0 = max(o[0, 0], im[0, 0], hmd[0, 0]) + 0.5
        t1 = min(o[-1, 0], im[-1, 0], hmd[-1, 0]) - 0.5
        tg = np.arange(t0, t1, dt)
        tch = sk[sk[:, 3] > 0, 0]
        stale = tg - tch[np.clip(np.searchsorted(tch, tg, side='right') - 1, 0, len(tch) - 1)]
        oi = np.clip(np.searchsorted(o[:, 0], tg), 0, len(o) - 1)
        pto, valid, result = o[oi, 4] * 1e3, o[oi, 21], o[oi, 22]
        P = np.stack([np.interp(tg, o[:, 0], o[:, 5 + i]) for i in range(3)], 1)
        Ro = quat_to_mat(interp_quat(tg, o[:, 0], o[:, 8:12]))
        H = np.stack([np.interp(tg, hmd[:, 0], hmd[:, 5 + i]) for i in range(3)], 1)
        Rh = quat_to_mat(interp_quat(tg, hmd[:, 0], hmd[:, 8:12]))
        ti = im[:, 0] - im[:, 25]
        order = np.argsort(ti)
        im, ti = im[order], ti[order]
        Rj = quat_to_mat(interp_quat(tg - 0.03, ti, im[:, 12:16]))
        seen = (stale < 0.08) & (valid > 0.5) & (pto > -50)
        use = seen.copy()
        for _ in range(3):                                     # robust: refit on the agreeing samples
            a_j, M_j, res = mount_fit(Rj[use][::5], Ro[use][::5])
            pred = np.einsum('ij,njk,kl->nil', yaw(a_j) @ C_IMU, Rj, M_j)
            dis = np.degrees(np.arccos(np.clip((np.einsum('nij,nij->n', pred, Ro) - 1) / 2, -1, 1)))
            use = seen & (dis < 15)
        # the hand relative to the headset: angle off its forward (-z), height below it
        fwd = -Rh[:, :, 2]
        d = P - H
        off = np.degrees(np.arccos(np.clip(np.einsum('ni,ni->n', d, fwd) / np.linalg.norm(d, axis=1), -1, 1)))
        below = (H[:, 1] - P[:, 1]) * 100
        # the optical hand moving while the IMU is quiet, or vice versa
        k = int(0.05 / dt)
        def rate(R):
            r = np.zeros(len(R))
            rel = np.einsum('nji,njk->nik', R[:-k], R[k:])
            r[k:] = np.degrees(np.arccos(np.clip((np.trace(rel, axis1=1, axis2=2) - 1) / 2, -1, 1))) / (k * dt)
            return r
        # direction in the headset's frame (x right, y up, -z forward): azimuth outward-positive (mirrored for the
        # left hand), elevation up-positive
        loc = np.einsum('nji,nj->ni', Rh, d)
        az = np.degrees(np.arctan2(loc[:, 0], -loc[:, 2])) * (1 if hand else -1)
        el = np.degrees(np.arctan2(loc[:, 1], np.hypot(loc[:, 0], loc[:, 2])))
        out[hand] = dict(tg=tg, stale=stale, pto=pto, valid=valid, result=result, dis=dis, off=off, below=below,
                         ro=rate(Ro), ri=rate(pred), fit=res, seen=seen, az=az, el=el, P=P, H=H, Ro=Ro)
        print(f"{'left' if hand == 0 else 'right'}: joint IMU fit {res:.1f} deg on {use.mean() * 100:.0f} % of the "
              f"samples; 'seen' {seen.mean() * 100:.0f} %; disagreement > 30 deg while 'seen': "
              f"{(seen & (dis > 30)).mean() * 100:.0f} % of the time")
        # losses: the skeleton unchanged > 80 ms
        lost = stale > 0.08
        starts = np.flatnonzero(lost[1:] & ~lost[:-1]) + 1
        ends = np.flatnonzero(~lost[1:] & lost[:-1]) + 1
        if len(starts) and len(ends):
            ends = ends[ends > starts[0]]
            n = min(len(starts), len(ends))
            dur = (ends[:n] - starts[:n]) * dt + 0.08
            print(f"  losses (skeleton unchanged > 80 ms): {n}, {dur.sum():.1f} s in all; duration median "
                  f"{np.median(dur):.2f} s, max {dur.max():.2f} s")
            # how soon would 'frozen while the IMU turns' have told? optical still (< 5 deg/s) for 30 ms while the
            # IMU turns > 60 deg/s
            ro, ri = out[hand]['ro'], out[hand]['ri']
            early = []
            for s0 in starts[:n]:
                s = s0 - int(0.08 / dt)                      # the last skeleton change
                for i in range(s, s0 + 1):
                    if i >= 3 and np.all(ro[i - 3:i + 1] < 5) and np.all(ri[i - 3:i + 1] > 60):
                        early.append((i - s) * dt)
                        break
            if early:
                print(f"    'frozen while the IMU turns' fires in {len(early)} of them, {np.median(early) * 1e3:.0f} ms "
                      f"after the last skeleton change (median; the staleness test: 80 ms)")
        # disagreement while 'seen', by where the hand is relative to the headset's forward direction
        print("  while 'seen', by the angle off the headset's forward: share of the time, disagreement with the IMU "
              "(median / p90 deg), > 30 deg")
        for lo, hi in ((0, 45), (45, 70), (70, 90), (90, 120), (120, 180)):
            m = seen & (off >= lo) & (off < hi)
            if m.sum() < 20:
                continue
            print(f"    {lo:3d}-{hi:3d} deg: {m.mean() * 100:4.0f} %   {np.median(dis[m]):5.1f} / "
                  f"{np.percentile(dis[m], 90):5.1f}   {(dis[m] > 30).mean() * 100:4.0f} %")
        # where the fusion takes corrections: both streams turning slowly (< 1 rad/s)
        slow = seen & (out[hand]['ro'] < 57) & (out[hand]['ri'] < 57)
        print(f"  slow turns only (< 1 rad/s, where the IMU is corrected; {slow.mean() * 100:.0f} % of the time): "
              "disagreement median / p75 / p90 / p99 deg, and the share over 10 / 15 / 25 deg")
        for lo, hi in ((0, 45), (45, 70), (70, 90), (90, 120), (120, 180)):
            m = slow & (off >= lo) & (off < hi)
            if m.sum() < 20:
                continue
            x = dis[m]
            print(f"    {lo:3d}-{hi:3d} deg: {np.median(x):5.1f} / {np.percentile(x, 75):5.1f} / {np.percentile(x, 90):5.1f}"
                  f" / {np.percentile(x, 99):5.1f}    {(x > 10).mean() * 100:3.0f} / {(x > 15).mean() * 100:3.0f} / "
                  f"{(x > 25).mean() * 100:3.0f} %   ({m.sum()} samples)")
    # Where the headset's hands go wrong, both hands together: share of the seen samples (turning < 2 rad/s, so
    # timing doesn't count) disagreeing with the IMU by > 20 deg, per direction from the headset. Rows: elevation
    # (up +); columns: azimuth (outward +, inward -).
    azb = [-90, -30, 0, 30, 60, 90, 180]
    elb = [-90, -60, -40, -20, 0, 20, 40, 90]
    print("\n  bad share (> 20 deg off the IMU) by direction from the headset, both hands; samples in brackets")
    print("  elevation \\ azimuth " + "".join(f"{f'{a0}..{a1}':>14s}" for a0, a1 in zip(azb[:-1], azb[1:])))
    for e0, e1 in reversed(list(zip(elb[:-1], elb[1:]))):
        line = f"  {f'{e0}..{e1}':>19s} "
        for a0, a1 in zip(azb[:-1], azb[1:]):
            bad = tot = 0
            for hand in (0, 1):
                d = out[hand]
                m = (d['seen'] & (d['ro'] < 115) & (d['ri'] < 115) & (d['az'] >= a0) & (d['az'] < a1) &
                     (d['el'] >= e0) & (d['el'] < e1))
                tot += m.sum()
                bad += (m & (d['dis'] > 20)).sum()
            line += f"{f'{bad / tot * 100:.0f}% ({tot})' if tot >= 30 else '-':>14s}"
        print(line)
    # Occlusion by the other hand: the angle between the two hands as seen from the headset, and which is behind.
    # And the hand's own orientation to the view ray (edge-on hands flip): |cos| of the ray with each hand axis.
    t_common = out[0]['tg']
    other = {}
    for hand in (0, 1):
        o, d = out[1 - hand], out[hand]
        Po = np.stack([np.interp(d['tg'], o['tg'], o['P'][:, i]) for i in range(3)], 1)
        seen_o = np.interp(d['tg'], o['tg'], o['seen'].astype(float)) > 0.5
        r1, r2 = d['P'] - d['H'], Po - d['H']
        sep = np.degrees(np.arccos(np.clip(np.einsum('ni,ni->n', r1, r2) /
                                           (np.linalg.norm(r1, axis=1) * np.linalg.norm(r2, axis=1)), -1, 1)))
        behind = np.linalg.norm(r1, axis=1) > np.linalg.norm(r2, axis=1)
        ray = r1 / np.linalg.norm(r1, axis=1, keepdims=True)
        axcos = np.abs(np.einsum('nij,ni->nj', d['Ro'], ray))       # |cos| of the ray with the hand's x, y, z axes
        dist = np.linalg.norm(r1, axis=1) * 100
        other[hand] = dict(sep=sep, behind=behind & seen_o, front=~behind & seen_o, axcos=axcos, dist=dist)

    def table(title, bins, key):
        print(f"\n  bad share (> 20 deg off the IMU) by {title}, both hands; samples in brackets")
        for label, sel in bins:
            bad = tot = 0
            for hand in (0, 1):
                d = out[hand]
                m = d['seen'] & (d['ro'] < 115) & (d['ri'] < 115) & sel(other[hand])
                tot += m.sum()
                bad += (m & (d['dis'] > 20)).sum()
            print(f"    {label:40s} {f'{bad / tot * 100:.0f}% ({tot})' if tot >= 30 else '-'}")

    table("the other hand (angle between the hands as seen from the headset)",
          [(f"this hand behind, {a0}-{a1} deg apart", lambda o, a0=a0, a1=a1: o['behind'] & (o['sep'] >= a0) & (o['sep'] < a1))
           for a0, a1 in ((0, 10), (10, 20), (20, 35), (35, 180))] +
          [(f"this hand in front, {a0}-{a1} deg apart", lambda o, a0=a0, a1=a1: o['front'] & (o['sep'] >= a0) & (o['sep'] < a1))
           for a0, a1 in ((0, 10), (10, 20), (20, 35), (35, 180))], 'sep')
    for ax, name in enumerate("xyz"):
        table(f"the hand's {name} axis to the view ray (|cos|: 0 = across the ray, 1 = along it)",
              [(f"|cos| {c0:.1f}-{c1:.1f}", lambda o, c0=c0, c1=c1, ax=ax: (o['axcos'][:, ax] >= c0) & (o['axcos'][:, ax] < c1))
               for c0, c1 in ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))], 'ax')
    table("distance from the headset",
          [(f"{d0}-{d1} cm", lambda o, d0=d0, d1=d1: (o['dist'] >= d0) & (o['dist'] < d1))
           for d0, d1 in ((0, 25), (25, 35), (35, 45), (45, 55), (55, 100))], 'dist')

    print("\n  t      | left:  stale  pto  res  off  below  dis  rate opt/imu | right: stale  pto  res  off  below  dis  "
          "rate opt/imu")
    tg = out[0]['tg']
    n = int(a.step / dt)
    for s in range(0, len(tg) - n, n):
        line = f"{tg[s] - tg[0]:7.1f} |"
        for hand in (0, 1):
            d = out[hand]
            sl = slice(s, s + n)
            if s + n > len(d['tg']):
                continue
            line += (f"  {np.max(d['stale'][sl]) * 1e3:5.0f} {np.min(d['pto'][sl]):5.0f} {np.median(d['result'][sl]):3.0f}"
                     f" {np.median(d['off'][sl]):4.0f} {np.median(d['below'][sl]):5.0f} {np.median(d['dis'][sl]):4.0f}"
                     f"  {np.median(d['ro'][sl]):4.0f}/{np.median(d['ri'][sl]):4.0f}   |")
        print(line)


if __name__ == '__main__':
    main()
