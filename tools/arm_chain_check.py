"""Can the arm chain carry the hand position through a gap? A check against a capture (numpy only).

    python tools/arm_chain_check.py <capture.csv>

Needs a capture with the CyberFinger IMUs (bridge running), the headset and SlimeVR's elbow trackers (kinds 7, 8). Per
hand, on the frames where the headset sees the hand (the skeleton changed within 80 ms, the pose not dated in the
past), it fits the chain

    hand point = elbow + R_forearm · f + R_hand · h + c

R_forearm: the CyberFinger's body IMU in SteamVR's frame (its heading fitted); R_hand: the CyberFinger's joint IMU calibrated
against the headset's hand (heading and mounting, as the driver's ImuFusion does); f, h, c: constant vectors (the
elbow → wrist in the forearm sensor's frame, the wrist → tracked point in the hand's, and a fixed offset for
SlimeVR's elbow). The fit's residual is how well the chain explains the hand. Then simulated gaps of D seconds
where the hand was in fact seen: the chain anchored at the gap's start (its change since then added to the last
seen position) against holding the last position. Without the Slimes: the same with the elbow held where it was
relative to the headset.
"""
import os
import sys

import numpy as np

C_IMU = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)   # IMU z-up world -> SteamVR y-up


def load(path):
    rows = []
    with open(path) as f:
        next(f)
        for line in f:
            c = line.split(',')
            if len(c) >= 26 and c[2] in ('0', '1', '6', '7', '8'):
                rows.append([float(x) for x in c[:26]])
    return np.array(rows)


def quat_to_mat(q):
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack([np.stack([1 - 2*(y*y + z*z), 2*(x*y - w*z), 2*(x*z + w*y)], -1),
                     np.stack([2*(x*y + w*z), 1 - 2*(x*x + z*z), 2*(y*z - w*x)], -1),
                     np.stack([2*(x*z - w*y), 2*(y*z + w*x), 1 - 2*(x*x + y*y)], -1)], -2)


def interp_quat(tg, t, q):
    q = q.copy()
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    Q = np.stack([np.interp(tg, t, q[:, i]) for i in range(4)], 1)
    return Q / np.linalg.norm(Q, axis=1, keepdims=True)


def yaw(a):
    return np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])


def mount_fit(Ri, Ro):
    """Joint IMU -> hand: R_hand = Y(a) C R_imu M. Grid over a, mean M (as ImuFusion::Solve). Returns a, M, residual deg."""
    best = None
    for a in np.radians(np.arange(0, 360, 2.0)):
        pre = yaw(a) @ C_IMU
        Ms = np.einsum('nji,jk,nkl->nil', Ri, pre.T, Ro)          # (pre R_imu)^T R_opt
        U, _, Vt = np.linalg.svd(Ms.sum(0))
        M = U @ np.diag([1, 1, np.linalg.det(U @ Vt)]) @ Vt
        err = np.linalg.norm(Ms - M, axis=(1, 2)).mean()
        if best is None or err < best[0]:
            best = (err, a, M)
    err, a, M = best
    pred = np.einsum('ij,njk,kl->nil', yaw(a) @ C_IMU, Ri, M)
    ang = np.degrees(np.arccos(np.clip((np.einsum('nij,nij->n', pred, Ro) - 1) / 2, -1, 1)))
    return a, M, np.sqrt(np.mean(ang ** 2))


def chain_fit(P, E, Rb_imu, Rh, seen):
    """Grid over the forearm sensor's heading; least squares for f, h, c. Returns (a, f, h, c, residual cm)."""
    best = None
    n = seen.sum()
    for a in np.radians(np.arange(0, 360, 2.0)):
        Rb = np.einsum('ij,njk->nik', yaw(a) @ C_IMU, Rb_imu[seen])
        X = np.concatenate([Rb, Rh[seen], np.broadcast_to(np.eye(3), (n, 3, 3))], 2).reshape(-1, 9)
        y = (P[seen] - E[seen]).reshape(-1)
        sol, *_ = np.linalg.lstsq(X, y, rcond=None)
        r = np.sqrt(np.mean(((X @ sol - y).reshape(-1, 3) ** 2).sum(1)))
        if best is None or r < best[0]:
            best = (r, a, sol)
    r, a, sol = best
    return a, sol[0:3], sol[3:6], sol[6:9], r * 100


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ''
    if not os.path.exists(path):
        path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'CyberFinger', 'captures', path)
    rows = load(path)
    print(os.path.basename(path))
    hmd = rows[rows[:, 2] == 7]
    for hand in (0, 1):
        name = 'left' if hand == 0 else 'right'
        o = rows[(rows[:, 1] == hand) & (rows[:, 2] == 0)]
        sk = rows[(rows[:, 1] == hand) & (rows[:, 2] == 1)]
        el = rows[(rows[:, 1] == hand) & (rows[:, 2] == 8)]
        im = rows[(rows[:, 1] == hand) & (rows[:, 2] == 6) & ((rows[:, 3].astype(int) & 5) == 5)]
        if len(el) < 100 or len(im) < 100 or len(o) < 100:
            print(f"  {name}: needs the elbow tracker, both CyberFinger IMUs and the hand")
            continue
        dt = 0.01
        t0 = max(o[0, 0], el[0, 0], im[0, 0], hmd[0, 0]) + 0.5
        t1 = min(o[-1, 0], el[-1, 0], im[-1, 0], hmd[-1, 0]) - 0.5
        tg = np.arange(t0, t1, dt)
        # seen: skeleton changed within 80 ms, pose valid, not dated > 50 ms in the past (the driver's criterion)
        tch = sk[sk[:, 3] > 0, 0]
        last = tch[np.clip(np.searchsorted(tch, tg, side='right') - 1, 0, len(tch) - 1)]
        oi = np.clip(np.searchsorted(o[:, 0], tg), 0, len(o) - 1)
        seen = (tg - last < 0.08) & (o[oi, 21] > 0.5) & (o[oi, 4] > -0.05)
        P = np.stack([np.interp(tg, o[:, 0], o[:, 5 + i]) for i in range(3)], 1)
        Ro = quat_to_mat(interp_quat(tg, o[:, 0], o[:, 8:12]))
        E = np.stack([np.interp(tg, el[:, 0], el[:, 5 + i]) for i in range(3)], 1)
        H = np.stack([np.interp(tg, hmd[:, 0], hmd[:, 5 + i]) for i in range(3)], 1)
        Rhmd = quat_to_mat(interp_quat(tg, hmd[:, 0], hmd[:, 8:12]))
        ti = im[:, 0] - im[:, 25]
        order = np.argsort(ti)
        im, ti = im[order], ti[order]
        lag = 0.03                                               # the optical trails the IMU (roughly)
        Rb_imu = quat_to_mat(interp_quat(tg - lag, ti, im[:, 4:8]))       # body 1
        Rj_imu = quat_to_mat(interp_quat(tg - lag, ti, im[:, 12:16]))     # joint
        # seen, and not in the first 150 ms after a gap (reacquisition is rough)
        gap_end = np.r_[False, seen[1:] & ~seen[:-1]]
        fresh = np.zeros_like(seen)
        for i in np.flatnonzero(gap_end):
            fresh[i:i + int(0.15 / dt)] = True
        good = seen & ~fresh
        print(f"  {name}: {len(tg) * dt:.0f} s, hand seen {seen.mean() * 100:.0f} %, "
              f"{int(gap_end.sum())} returns after a gap")
        d = np.linalg.norm(P - E, axis=1)[good] * 100
        print(f"    elbow -> tracked hand, seen: {np.median(d):.1f} cm median ({np.percentile(d, 10):.1f}-"
              f"{np.percentile(d, 90):.1f})")
        a_j, M_j, res_j = mount_fit(Rj_imu[good][::5], Ro[good][::5])
        Rh = np.einsum('ij,njk,kl->nil', yaw(a_j) @ C_IMU, Rj_imu, M_j)          # the hand from its IMU
        print(f"    joint IMU -> hand: heading {np.degrees(a_j):.0f} deg, fit {res_j:.1f} deg rms")
        a_b, f, h, c, r = chain_fit(P, E, Rb_imu, Rh, good)
        Rb = np.einsum('ij,njk->nik', yaw(a_b) @ C_IMU, Rb_imu)
        model = E + np.einsum('nij,j->ni', Rb, f) + np.einsum('nij,j->ni', Rh, h) + c
        print(f"    chain with the Slime elbow: residual {r:.1f} cm rms; elbow->wrist |f| {np.linalg.norm(f)*100:.1f} cm,"
              f" wrist->point |h| {np.linalg.norm(h)*100:.1f} cm, elbow offset |c| {np.linalg.norm(c)*100:.1f} cm")
        # without the Slime: the elbow held relative to the headset (in its yaw frame) from the gap's start
        yaw_h = np.arctan2(Rhmd[:, 0, 2], Rhmd[:, 2, 2])
        Yh = np.stack([yaw(a) for a in yaw_h])
        # gaps
        print("    simulated gaps (hand seen throughout, hidden): error at the end, cm median / p90")
        for D in (1.0, 2.0, 5.0, 8.0):
            n = int(D / dt)
            eh, ec, en = [], [], []
            for s in range(0, len(tg) - n, int(1.0 / dt)):
                if not good[s] or good[s:s + n + 1].mean() < 0.95 or not good[s + n]:
                    continue
                truth = P[s + n]
                eh.append(np.linalg.norm(truth - P[s]))
                ec.append(np.linalg.norm(truth - (P[s] + model[s + n] - model[s])))
                # elbow fixed in the headset's yaw frame from the gap's start
                e_rel = Yh[s].T @ (E[s] - H[s])
                e_n = H[s + n] + Yh[s + n] @ e_rel
                m0 = E[s] + Rb[s] @ f + Rh[s] @ h
                m1 = e_n + Rb[s + n] @ f + Rh[s + n] @ h
                en.append(np.linalg.norm(truth - (P[s] + m1 - m0)))
            if not eh:
                continue
            fmt = lambda e: f"{np.median(e)*100:5.1f} / {np.percentile(e, 90)*100:5.1f}"
            print(f"      {D:3.0f} s: hold {fmt(eh)}   chain + Slime elbow {fmt(ec)}   chain, elbow held to the "
                  f"headset {fmt(en)}   ({len(eh)} gaps)")


if __name__ == '__main__':
    main()
