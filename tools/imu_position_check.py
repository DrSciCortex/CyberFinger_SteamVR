"""Can the CyberFinger's accelerometers carry the hand position? A check against a capture (numpy only).

    python tools/imu_position_check.py <capture.csv> [--sensor joint|body]

Captures (tools/analyze_tap_capture.py --capture N, bridge running) hold the headset hand's poses and the CyberFinger
IMUs on the driver's clock. Per hand this compares the IMU's linear acceleration (the raw accelerometer rotated
into its own z-up world by its quaternion, gravity removed) with the optical wrist position's second derivative:

  * vertical first: the IMU world's z is SteamVR's y whatever the IMU's heading, so the vertical comparison needs
    no calibration. It gives the lag, the scale (should be 1) and the bias;
  * then all three axes, the heading fitted in the horizontal plane;
  * then dead reckoning: from the optical position and velocity at the start of a gap, the IMU's acceleration
    integrated for D seconds, against the optical position at the end; with and without the lever arm (the
    tracked point is not where the sensor is), next to holding the last position and to constant velocity.
"""
import argparse
import os
import sys

import numpy as np

G = 9.80665
LSB_PER_G = 2048.0                                           # every IMU slot, 16 g range (firmware)
SLOTS = {'body': (0, 12, 0x1), 'joint': (8, 18, 0x4)}        # quaternion column, accel column, present bit


def qmul(a, b):
    w1, x1, y1, z1 = np.moveaxis(a, -1, 0)
    w2, x2, y2, z2 = np.moveaxis(b, -1, 0)
    return np.stack([w1*w2 - x1*x2 - y1*y2 - z1*z2, w1*x2 + x1*w2 + y1*z2 - z1*y2,
                     w1*y2 - x1*z2 + y1*w2 + z1*x2, w1*z2 + x1*y2 - y1*x2 + z1*w2], -1)


def qconj(q):
    return q * np.array([1, -1, -1, -1])


def qrot(q, v):
    return qmul(qmul(q, np.concatenate([np.zeros(v.shape[:-1] + (1,)), v], -1)), qconj(q))[..., 1:]


def load(path):
    rows = []
    with open(path) as f:
        next(f)
        for line in f:
            c = line.split(',')
            if len(c) >= 26 and c[2] in ('0', '6'):
                rows.append([float(x) for x in c[:26]])
    return np.array(rows)


def smooth(x, dt, sigma):
    """Gaussian smoothing along axis 0 (sigma in seconds)."""
    r = int(3 * sigma / dt)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * dt / sigma) ** 2)
    k /= k.sum()
    return np.stack([np.convolve(x[:, i], k, mode='same') for i in range(x.shape[1])], 1)


def best_lag(a, b, dt, max_lag=0.15):
    """Shift (s) by which a trails b: max correlation of a(t) with b(t - s)."""
    best, bs = -2, 0
    n = len(a)
    for k in range(int(max_lag / dt) + 1):
        x, y = a[k:], b[:n - k]
        c = np.sum((x - x.mean(0)) * (y - y.mean(0))) / np.sqrt(np.sum((x - x.mean(0))**2) * np.sum((y - y.mean(0))**2))
        if c > best:
            best, bs = c, k
    return bs * dt, best


def analyse(rows, hand, sensor):
    qcol, acol, bit = SLOTS[sensor]
    o = rows[(rows[:, 1] == hand) & (rows[:, 2] == 0) & (rows[:, 21] > 0.5)]
    m = rows[(rows[:, 1] == hand) & (rows[:, 2] == 6) & ((rows[:, 3].astype(int) & bit) != 0)]
    if len(o) < 100 or len(m) < 100:
        print(f"  {'LR'[hand]}: not enough data")
        return
    to, po = o[:, 0], o[:, 5:8]                               # t, position (v1..v3)
    ti = m[:, 0] - m[:, 4 + 21]                               # IMU sample time: arrival - age
    qi = m[:, 4 + qcol:4 + qcol + 4]
    qi /= np.linalg.norm(qi, axis=1, keepdims=True)
    ai = m[:, 4 + acol:4 + acol + 3] / LSB_PER_G * G          # m/s², sensor frame
    order = np.argsort(ti)
    ti, qi, ai = ti[order], qi[order], ai[order]
    keep = np.r_[True, np.diff(ti) > 1e-4]
    ti, qi, ai = ti[keep], qi[keep], ai[keep]
    # linear acceleration in the IMU's z-up world
    aw = qrot(qi, ai) - np.array([0, 0, G])
    print(f"  {'LR'[hand]} {sensor}: |accel| at rest-ish median {np.median(np.linalg.norm(ai, axis=1)):.3f} m/s² "
          f"(g = {G:.3f}); linear accel in its world: mean {np.round(aw.mean(0), 3)} m/s²")

    dt = 0.005
    t0, t1 = max(to[0], ti[0]) + 0.2, min(to[-1], ti[-1]) - 0.2
    tg = np.arange(t0, t1, dt)
    P = np.stack([np.interp(tg, to, po[:, i]) for i in range(3)], 1)
    A = np.stack([np.interp(tg, ti, aw[:, i]) for i in range(3)], 1)
    sig = 0.025
    Ps = smooth(P, dt, sig)
    Ao = np.gradient(np.gradient(Ps, dt, axis=0), dt, axis=0)  # optical acceleration, SteamVR world
    Ai = smooth(A, dt, sig)                                    # same band for the IMU
    cut = int(0.3 / dt)
    Ao, Ai, Ps, tgc = Ao[cut:-cut], Ai[cut:-cut], Ps[cut:-cut], tg[cut:-cut]

    # vertical: optical y vs IMU world z
    lag, c = best_lag(Ao[:, 1:2], Ai[:, 2:3], dt)
    k = int(round(lag / dt))
    y, x = Ao[k:, 1], Ai[:len(Ai) - k, 2]
    X = np.stack([x, np.ones_like(x)], 1)
    (scale, bias), *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ [scale, bias]
    print(f"    vertical: optical trails by {lag*1e3:.0f} ms (corr {c:.2f}); optical = {scale:.2f} × IMU "
          f"{bias:+.2f} m/s²; residual {res.std():.2f} m/s² rms of optical {y.std():.2f}")

    # all axes: heading about the vertical fitted in the horizontal plane (2D Procrustes), after the lag
    Ci = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])        # IMU z-up world -> SteamVR y-up: (x, y, z) -> (x, z, -y)
    Iv = Ai[:len(Ai) - k] @ Ci.T
    Ov = Ao[k:]
    h = np.sum(Iv[:, 0] * Ov[:, 0] + Iv[:, 2] * Ov[:, 2]), np.sum(Iv[:, 2] * Ov[:, 0] - Iv[:, 0] * Ov[:, 2])
    alpha = np.arctan2(h[1], h[0])
    Ry = np.array([[np.cos(alpha), 0, np.sin(alpha)], [0, 1, 0], [-np.sin(alpha), 0, np.cos(alpha)]])
    Iw = Iv @ Ry.T
    r = Ov - Iw
    print(f"    3D: heading {np.degrees(alpha) % 360:.1f} deg; residual {np.sqrt((r**2).sum(1).mean()):.2f} m/s² rms "
          f"of optical {np.sqrt(((Ov - Ov.mean(0))**2).sum(1).mean()):.2f}; per axis corr "
          f"{[round(float(np.corrcoef(Ov[:, i], Iw[:, i])[0, 1]), 2) for i in range(3)]}")
    return dict(ti=ti, qi=qi, ai=ai, to=to, po=po, qo=o[:, 8:12], alpha=alpha, lag=lag, bias_v=bias)


def quat_to_mat(q):
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack([np.stack([1 - 2*(y*y + z*z), 2*(x*y - w*z), 2*(x*z + w*y)], -1),
                     np.stack([2*(x*y + w*z), 1 - 2*(x*x + z*z), 2*(y*z - w*x)], -1),
                     np.stack([2*(x*z - w*y), 2*(y*z + w*x), 1 - 2*(x*x + y*y)], -1)], -2)


def lever_fit(d, hand, sensor):
    """The sensor sits at p_opt + R_opt·r (r: fixed, in the optical hand frame), so its acceleration is
    a_opt + R̈·r. Fit r, the heading and the lag together; report how much of the optical acceleration the IMU
    then explains."""
    ti, qi, ai, to, po, qo = d['ti'], d['qi'], d['ai'], d['to'], d['po'], d['qo']
    aw = qrot(qi, ai) - np.array([0, 0, G])                   # IMU world (z-up), gravity removed
    Ci = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    dt, sig = 0.005, 0.025
    tg = np.arange(max(to[0], ti[0]) + 0.4, min(to[-1], ti[-1]) - 0.2, dt)
    P = np.stack([np.interp(tg, to, po[:, i]) for i in range(3)], 1)
    # optical rotation on the grid: sign-continuous quaternions interpolated per component, then matrices
    q = qo.copy()
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    Q = np.stack([np.interp(tg, to, q[:, i]) for i in range(4)], 1)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    M = quat_to_mat(Q).reshape(-1, 9)
    Ms = smooth(M, dt, sig)
    Mdd = np.gradient(np.gradient(Ms, dt, axis=0), dt, axis=0).reshape(-1, 3, 3)
    Ao = np.gradient(np.gradient(smooth(P, dt, sig), dt, axis=0), dt, axis=0)
    cut = int(0.3 / dt)
    best = None
    for lag in np.arange(0, 0.121, 0.005):
        Ai = smooth(np.stack([np.interp(tg[cut:-cut] - lag, ti, aw[:, i]) for i in range(3)], 1), dt, sig) @ Ci.T
        ao, mdd = Ao[cut:-cut], Mdd[cut:-cut]
        r = np.zeros(3)
        for _ in range(4):                                     # alternate: heading given r, r given heading
            target = ao + mdd @ r
            h = np.sum(Ai[:, 0] * target[:, 0] + Ai[:, 2] * target[:, 2]), \
                np.sum(Ai[:, 2] * target[:, 0] - Ai[:, 0] * target[:, 2])
            alpha = np.arctan2(h[1], h[0])
            Ry = np.array([[np.cos(alpha), 0, np.sin(alpha)], [0, 1, 0], [-np.sin(alpha), 0, np.cos(alpha)]])
            Iw = Ai @ Ry.T
            r, *_ = np.linalg.lstsq(mdd.reshape(-1, 3), (Iw - ao).reshape(-1), rcond=None)
        res = Iw - (ao + mdd @ r)
        e = np.sqrt((res**2).sum(1).mean())
        if best is None or e < best[0]:
            best = (e, lag, alpha, r, Iw, ao, mdd)
    e, lag, alpha, r, Iw, ao, mdd = best
    base = np.sqrt(((Iw - ao)**2).sum(1).mean())
    sig_o = np.sqrt(((ao + mdd @ r - (ao + mdd @ r).mean(0))**2).sum(1).mean())
    print(f"    lever arm: r = {np.round(r * 100, 1)} cm in the hand frame (|r| {np.linalg.norm(r)*100:.1f} cm); lag "
          f"{lag*1e3:.0f} ms, heading {np.degrees(alpha) % 360:.1f} deg; residual {base:.2f} -> {e:.2f} m/s² rms "
          f"(signal {sig_o:.2f}); per axis corr "
          f"{[round(float(np.corrcoef((ao + mdd @ r)[:, i], Iw[:, i])[0, 1]), 2) for i in range(3)]}")
    d.update(alpha=alpha, lag=lag, r=r)


def dead_reckon(d, hand, sensor, use_lever=False):
    """Gaps from every 0.5 s: start from the optical state (lag-aligned), integrate the IMU. With the lever arm,
    the sensor's own point is integrated (from the optical point + R·r) and mapped back with the end's R."""
    ti, qi, ai, to, po, qo, alpha, lag = d['ti'], d['qi'], d['ai'], d['to'], d['po'], d['qo'], d['alpha'], d['lag']
    r = d['r'] if use_lever else np.zeros(3)
    Ci = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    Ry = np.array([[np.cos(alpha), 0, np.sin(alpha)], [0, 1, 0], [-np.sin(alpha), 0, np.cos(alpha)]])
    R = Ry @ Ci
    aw = (qrot(qi, ai) - np.array([0, 0, G])) @ R.T            # SteamVR world, at the IMU's times
    dt = 0.005
    tg = np.arange(max(to[0], ti[0] + lag) + 0.3, min(to[-1], ti[-1] + lag) - 0.3, dt)
    q = qo.copy()
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    Q = np.stack([np.interp(tg, to, q[:, i]) for i in range(4)], 1)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    Rh = quat_to_mat(Q)
    P = np.stack([np.interp(tg, to, po[:, i]) for i in range(3)], 1)
    Pi = P + Rh @ r                                            # the sensor's point
    Pis = smooth(Pi, dt, 0.02)
    V = np.gradient(Pis, dt, axis=0)
    Ps = smooth(P, dt, 0.02)
    Vo = np.gradient(Ps, dt, axis=0)
    A = np.stack([np.interp(tg - lag, ti, aw[:, i]) for i in range(3)], 1)   # IMU aligned to the optical timing
    # Still: over the last 100 ms the IMU turned slower than 0.3 rad/s and its linear acceleration stayed under
    # 0.5 m/s² (what the IMU alone can tell, as a driver would while the hand is unseen).
    qs = qi.copy()
    for i in range(1, len(qs)):
        if np.dot(qs[i], qs[i - 1]) < 0:
            qs[i] = -qs[i]
    Qi = np.stack([np.interp(tg - lag, ti, qs[:, i]) for i in range(4)], 1)
    Qi /= np.linalg.norm(Qi, axis=1, keepdims=True)
    k = int(0.04 / dt)
    rate = np.zeros(len(tg))
    rate[k:] = 2 * np.arccos(np.clip(np.abs(np.sum(Qi[k:] * Qi[:-k], 1)), 0, 1)) / (k * dt)
    w = int(0.1 / dt)
    quiet = (rate < 0.3) & (np.linalg.norm(A, axis=1) < 0.5)
    still = np.array([quiet[max(0, i - w):i + 1].all() for i in range(len(tg))])
    # A filter running while the hand is seen: position and velocity per axis, predicted with the IMU's
    # acceleration, corrected by the optical position (a Kalman filter; the IMU aligned to the optical timing, so
    # no delay to handle here). Its state at the start of a gap, rather than the optical velocity, starts the
    # dead reckoning: causal, only data up to then.
    def kalman(sa, sz):
        F = np.array([[1, dt], [0, 1]])
        Bm = np.array([0.5 * dt * dt, dt])
        Qm = sa**2 * np.outer(Bm, Bm)
        Hm = np.array([1.0, 0.0])
        xs = np.zeros((len(tg), 3, 2))
        for ax in range(3):
            x = np.array([Pi[0, ax], 0.0])
            Pc = np.diag([sz**2, 1.0])
            for i in range(len(tg)):
                x = F @ x + Bm * A[i, ax]
                Pc = F @ Pc @ F.T + Qm
                S = Hm @ Pc @ Hm + sz**2
                K = Pc @ Hm / S
                x = x + K * (Pi[i, ax] - x[0])
                Pc = Pc - np.outer(K, Hm) @ Pc
                xs[i, ax] = x
        return xs[:, :, 0], xs[:, :, 1]
    KF = {f"filter σa {sa:g}": kalman(sa, 0.002) for sa in (1.0, 3.0)}
    print(f"    dead reckoning ({sensor}{', lever arm' if use_lever else ''}), error at the end of a gap, "
          f"mm median / p90; still {still.mean()*100:.0f}% of the time:")
    for D in (0.1, 0.25, 0.5, 1.0):
        n = int(D / dt)
        eh, ev, ei, ed, ez, ek = [], [], [], [], [], {}
        for s0 in range(int(0.1 / dt), len(tg) - n, int(0.5 / dt)):
            truth = P[s0 + n]
            back = Rh[s0 + n] @ r                              # sensor point -> tracked point, with the end's R
            eh.append(np.linalg.norm(truth - Ps[s0]))
            ev.append(np.linalg.norm(truth - (Ps[s0] + Vo[s0] * D)))
            p, v = Pis[s0].copy(), V[s0].copy()
            pd, vd = p.copy(), v.copy()
            pz, vz = p.copy(), (np.zeros(3) if still[s0] else v.copy())
            for i in range(s0, s0 + n):
                v += A[i] * dt
                p += v * dt
                vd += (A[i] - vd / 0.3) * dt                  # damped: velocity leaks away (τ 0.3 s)
                pd += vd * dt
                vz = np.zeros(3) if still[i] else vz + (A[i] - vz / 0.3) * dt   # damped, stopped when still
                pz += vz * dt
            ei.append(np.linalg.norm(truth - (p - back)))
            ed.append(np.linalg.norm(truth - (pd - back)))
            ez.append(np.linalg.norm(truth - (pz - back)))
            for name, (kp, kv) in KF.items():
                pk, vk = kp[s0].copy(), (np.zeros(3) if still[s0] else kv[s0].copy())
                for i in range(s0, s0 + n):
                    vk = np.zeros(3) if still[i] else vk + (A[i] - vk / 0.3) * dt
                    pk += vk * dt
                ek.setdefault(name, []).append(np.linalg.norm(truth - (pk - back)))
        f = lambda e: f"{np.median(e)*1e3:5.0f} / {np.percentile(e, 90)*1e3:4.0f}"
        print(f"      {D:4.2f} s: hold {f(eh)}   IMU damped {f(ed)}   + still stop {f(ez)}   "
              + "   ".join(f"{name} {f(e)}" for name, e in ek.items()) + f"   ({len(eh)} gaps)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('capture')
    ap.add_argument('--sensor', default='joint', choices=SLOTS)
    a = ap.parse_args()
    path = a.capture if os.path.exists(a.capture) else os.path.join(
        os.environ.get('LOCALAPPDATA', ''), 'CyberFinger', 'captures', a.capture)
    rows = load(path)
    print(os.path.basename(path))
    for hand in (0, 1):
        d = analyse(rows, hand, a.sensor)
        if d:
            lever_fit(d, hand, a.sensor)
            dead_reckon(d, hand, a.sensor)


if __name__ == '__main__':
    sys.exit(main())
