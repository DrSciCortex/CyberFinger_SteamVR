# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

#!/usr/bin/env python3
"""
OpenXR hand-skeleton provider  (feeds OpenXRHandSkeletonSource over UDP)
=======================================================================

A standalone helper that opens an OpenXR session, reads the 26-joint hand
skeleton via XR_EXT_hand_tracking, and streams it to the CyberFinger GUI over
localhost UDP as JSON. It is the OpenXR analogue of the existing OpenVR skeleton
reader, and is deliberately a SEPARATE PROCESS:

  * an OpenXR session needs a GL/GLFW graphics binding, and GLFW window creation
    is not safe on a background thread — so the session lives on this process's
    OWN main thread, isolated from the GUI's Tk main loop;
  * a native runtime crash here can't take the GUI down;
  * it can be run standalone for debugging, exactly like openxr_handflag_probe.py.

Wire format (one JSON UDP datagram per frame, to 127.0.0.1:<port>):
  {
    "status": "connected" | "hands not in view" | "waiting for headset" | ...,
    "hands": [ left, right ]           # each null, or:
      { "joints": [[x,y,z], ...26],    # MODEL space: wrist-local, metres
        "pose":  {                     # null if head pose unavailable
          "rot":  [r00,r01,r02, r10,r11,r12, r20,r21,r22],  # wrist WORLD rotation
          "local":[x,y,z],             # hand position in HEAD-local frame (+x R,+y up,-z fwd)
          "dist": float,               # metres from head
          "vel":  [x,y,z] } }          # head-local hand velocity (finite-diff)
  }

This matches OpenVRSkeletonSource's contract exactly: joints in model space,
pose_info = (rot_rows, head_local_pos, dist, head_local_vel). Joint order is
OpenXR's (0 PALM, 1 WRIST, thumb 2-5, index 6-10, ...), which already lines up
1:1 with SKELETON_CHAINS/SKELETON_TIPS in the GUI.

Requirements: pip install pyopenxr glfw PyOpenGL  + an OpenXR runtime with hand
tracking (SteamVR+Steam Link, Meta via Quest Link, or Monado+WiVRn on Linux).

Run standalone (prints packets it would send):
  python openxr_skeleton_provider.py --port 0 --debug
"""

import argparse
import csv
import json
import math
import socket
import sys
import time


# ── pure math (no OpenXR needed — unit-testable) ─────────────────────────────
def quat_to_rows(x, y, z, w):
    """Unit quaternion -> 3x3 row-major rotation matrix (maps LOCAL -> WORLD)."""
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        (1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)),
        (2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)),
        (2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)),
    )


def matT_vec(R, v):
    """R^T . v  (world -> local when R maps local -> world)."""
    return (R[0][0] * v[0] + R[1][0] * v[1] + R[2][0] * v[2],
            R[0][1] * v[0] + R[1][1] * v[1] + R[2][1] * v[2],
            R[0][2] * v[0] + R[1][2] * v[1] + R[2][2] * v[2])


def vsub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vnorm(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


class _FiniteDiff:
    """Per-key world-space velocity by finite difference between frames."""

    def __init__(self):
        self._prev = {}  # key -> (pos, t_seconds)

    def velocity(self, key, pos, t_s):
        prev = self._prev.get(key)
        self._prev[key] = (pos, t_s)
        if prev is None:
            return (0.0, 0.0, 0.0)
        p0, t0 = prev
        dt = t_s - t0
        if dt <= 1e-4 or dt > 0.5:      # skip huge/zero gaps (stalls, first frame)
            return (0.0, 0.0, 0.0)
        return ((pos[0] - p0[0]) / dt, (pos[1] - p0[1]) / dt, (pos[2] - p0[2]) / dt)


def _run(port, host, debug, log_path=None):
    import ctypes

    # Socket + send() first, using only stdlib, so we can report even an
    # import failure (missing pyopenxr) back to the GUI as a status packet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (host, port)

    def send(pkt):
        try:
            sock.sendto(json.dumps(pkt).encode("utf-8"), addr)
        except OSError:
            pass
        if debug:
            print(pkt.get("status"),
                  [None if h is None else "hand" for h in pkt.get("hands", [])])

    # Optional raw-capture CSV (one row per hand per frame, incl. inactive frames)
    # for the offline reliability-gate evaluation.
    logf = logw = None
    if log_path:
        jhdr = []
        for j in range(26):
            jhdr += [f"p{j}x", f"p{j}y", f"p{j}z", f"f{j}", f"r{j}"]
        try:
            logf = open(log_path, "w", newline="")
            logw = csv.writer(logf)
            logw.writerow(["host_time", "wall_time", "xr_time", "hand", "is_active",
                           "hmd_valid", "hmd_px", "hmd_py", "hmd_pz",
                           "hmd_qx", "hmd_qy", "hmd_qz", "hmd_qw"] + jhdr)
            print(f"[provider] logging raw joints -> {log_path}", file=sys.stderr)
        except Exception as e:
            print(f"[provider] could not open log {log_path}: {e!r}", file=sys.stderr)
            logf = logw = None

    try:
        import xr
        from xr.utils.gl import ContextObject
        from xr.utils.gl.glfw_util import GLFWOffscreenContextProvider
    except Exception as e:
        send({"status": "pip install pyopenxr glfw PyOpenGL", "hands": [None, None]})
        print(f"[provider] pyopenxr/GL import failed: {e!r}", file=sys.stderr)
        return 3

    N = xr.HAND_JOINT_COUNT_EXT  # 26
    POS_VALID = int(xr.SPACE_LOCATION_POSITION_VALID_BIT)
    ORI_VALID = int(xr.SPACE_LOCATION_ORIENTATION_VALID_BIT)

    def read_joint(arr, i):
        p = arr[i].pose.position
        return (float(p.x), float(p.y), float(p.z))

    try:
        with ContextObject(
            context_provider=GLFWOffscreenContextProvider(),
            instance_create_info=xr.InstanceCreateInfo(
                enabled_extension_names=[
                    xr.KHR_OPENGL_ENABLE_EXTENSION_NAME,     # graphics binding (mandatory)
                    xr.EXT_HAND_TRACKING_EXTENSION_NAME,
                ],
            ),
        ) as context:
            # LOCAL base for everything (universally supported); head-relative
            # math cancels the base out, so LOCAL vs STAGE does not matter.
            base_space = xr.create_reference_space(
                context.session,
                xr.ReferenceSpaceCreateInfo(reference_space_type=xr.ReferenceSpaceType.LOCAL))
            view_space = xr.create_reference_space(
                context.session,
                xr.ReferenceSpaceCreateInfo(reference_space_type=xr.ReferenceSpaceType.VIEW))

            try:
                trackers = {
                    0: xr.create_hand_tracker_ext(context.session, xr.HandTrackerCreateInfoEXT(
                        hand=xr.HandEXT.LEFT, hand_joint_set=xr.HandJointSetEXT.DEFAULT)),
                    1: xr.create_hand_tracker_ext(context.session, xr.HandTrackerCreateInfoEXT(
                        hand=xr.HandEXT.RIGHT, hand_joint_set=xr.HandJointSetEXT.DEFAULT)),
                }
            except Exception as e:
                send({"status": "OpenXR hand tracking not available", "hands": [None, None]})
                print(f"[provider] create_hand_tracker_ext failed: {e!r}", file=sys.stderr)
                return 2

            locate_fn = ctypes.cast(
                xr.get_instance_proc_addr(context.instance, "xrLocateHandJointsEXT"),
                xr.PFN_xrLocateHandJointsEXT)

            # reusable per-hand output buffers (runtime overwrites each frame)
            bufs = {}
            for hand in (0, 1):
                loc = xr.HandJointLocationsEXT()
                arr = (xr.HandJointLocationEXT * N)()
                loc.joint_count = N
                loc.joint_locations = arr
                bufs[hand] = (loc, arr)

            fd = _FiniteDiff()
            last_send = 0.0

            for _frame_index, frame_state in enumerate(context.frame_loop()):
                t_xr = frame_state.predicted_display_time
                t_s = float(int(t_xr)) / 1e9       # XrTime is nanoseconds
                now = time.perf_counter()          # monotonic, precise dt for motion
                t_wall = time.time()               # wall clock, to align GUI phase marks

                # head (VIEW) pose in the base frame
                head = None
                hmd_log = None
                try:
                    hl = xr.locate_space(view_space, base_space, t_xr)
                    hf = int(hl.location_flags)
                    hp = hl.pose.position
                    hq = hl.pose.orientation
                    hmd_log = (hf, (float(hp.x), float(hp.y), float(hp.z)),
                               (float(hq.x), float(hq.y), float(hq.z), float(hq.w)))
                    if (hf & POS_VALID) and (hf & ORI_VALID):
                        p_h = hmd_log[1]
                        R_h = quat_to_rows(*hmd_log[2])
                        v_h = fd.velocity("head", p_h, t_s)
                        head = (p_h, R_h, v_h)
                except Exception:
                    head = None

                hands_out = [None, None]
                active_any = False
                for hand in (0, 1):
                    loc, arr = bufs[hand]
                    info = xr.HandJointsLocateInfoEXT(base_space=base_space, time=t_xr)
                    try:
                        res = xr.check_result(locate_fn(trackers[hand], info, ctypes.byref(loc)))
                        if res.is_exception():
                            raise res
                    except Exception:
                        continue

                    # raw capture (every frame, incl. inactive/invalid — that IS the
                    # occlusion signal we want to study offline)
                    if logw is not None:
                        row = [f"{now:.6f}", f"{t_wall:.6f}", int(t_xr), hand,
                               1 if loc.is_active else 0]
                        if hmd_log is None:
                            row += [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
                        else:
                            hok = 1 if (hmd_log[0] & POS_VALID and hmd_log[0] & ORI_VALID) else 0
                            row += [hok, *hmd_log[1], *hmd_log[2]]
                        for j in range(N):
                            pj = arr[j].pose.position
                            row += [round(float(pj.x), 5), round(float(pj.y), 5),
                                    round(float(pj.z), 5), int(arr[j].location_flags),
                                    round(float(arr[j].radius), 5)]
                        logw.writerow(row)

                    # World-frame joints + per-joint flags/radius + activity are
                    # emitted EVERY frame (incl. occluded/inactive) so the live gate
                    # can observe exactly the frames it must classify. Model joints +
                    # head-relative pose are added only when tracked, for the
                    # existing skeleton render.
                    wjoints = [[round(float(c), 5) for c in read_joint(arr, j)]
                               for j in range(N)]
                    jflags = [int(arr[j].location_flags) for j in range(N)]
                    jrad = [round(float(arr[j].radius), 5) for j in range(N)]
                    active = 1 if bool(loc.is_active) else 0
                    wf = int(arr[1].location_flags)
                    wrist_ok = bool((wf & POS_VALID) and (wf & ORI_VALID))

                    model = pose = None
                    if active and wrist_ok:
                        active_any = True
                        p_w = read_joint(arr, 1)
                        wq = arr[1].pose.orientation
                        R_w = quat_to_rows(float(wq.x), float(wq.y), float(wq.z), float(wq.w))
                        # MODEL space: each joint expressed in the wrist's local frame
                        model = [list(matT_vec(R_w, vsub(read_joint(arr, j), p_w)))
                                 for j in range(N)]
                        if head is not None:
                            p_h, R_h, v_h = head
                            rel = vsub(p_w, p_h)
                            local = matT_vec(R_h, rel)
                            v_w = fd.velocity(f"wrist{hand}", p_w, t_s)
                            local_v = matT_vec(R_h, vsub(v_w, v_h))
                            pose = {
                                "rot": [R_w[0][0], R_w[0][1], R_w[0][2],
                                        R_w[1][0], R_w[1][1], R_w[1][2],
                                        R_w[2][0], R_w[2][1], R_w[2][2]],
                                "local": [local[0], local[1], local[2]],
                                "dist": vnorm(rel),
                                "vel": [local_v[0], local_v[1], local_v[2]],
                            }
                    hands_out[hand] = {"joints": model, "pose": pose,
                                       "wjoints": wjoints, "flags": jflags,
                                       "radius": jrad, "active": active,
                                       "wrist_valid": 1 if wrist_ok else 0}

                if active_any:
                    status = "connected"
                elif context.session_state == xr.SessionState.FOCUSED:
                    status = "hands not in view"
                else:
                    status = "waiting for headset"

                # HMD world pose (for the live gate's camera frame): sent once per
                # packet, regardless of hand tracking.
                hmd_pkt = None
                if hmd_log is not None:
                    hf, hp, hq = hmd_log
                    hmd_pkt = {"pos": [hp[0], hp[1], hp[2]],
                               "quat": [hq[0], hq[1], hq[2], hq[3]],
                               "valid": 1 if (hf & POS_VALID and hf & ORI_VALID) else 0}

                # cap to ~60 Hz — the GUI only redraws the skeleton at ~30 fps
                if now - last_send >= 1.0 / 60.0:
                    last_send = now
                    send({"status": status, "hands": hands_out, "hmd": hmd_pkt})

    except Exception as e:
        # instance/session/runtime failure — tell the GUI something useful
        try:
            sock.sendto(json.dumps(
                {"status": "OpenXR runtime not available", "hands": [None, None]}
            ).encode("utf-8"), addr)
        except OSError:
            pass
        print(f"[provider] session error: {e!r}", file=sys.stderr)
        return 1
    finally:
        if logf is not None:
            try:
                logf.flush()
                logf.close()
            except Exception:
                pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenXR hand-skeleton UDP provider")
    ap.add_argument("--port", type=int, required=True,
                    help="UDP port to send packets to on --host (0 just prints in --debug)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true", help="also print status to stdout")
    ap.add_argument("--log", default=None, metavar="CSV",
                    help="also record raw WORLD joints + flags + radius + HMD pose "
                         "per frame (every frame, incl. inactive) to this CSV — for the "
                         "offline reliability-gate evaluation (fusion/run_gate_on_log.py)")
    args = ap.parse_args(argv)
    return _run(args.port, args.host, args.debug, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
