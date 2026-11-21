/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// TrackingTrust.h — how far the headset's hand tracking is trusted, from where
// the hands are seen
//
// Measured against the CyberFinger's IMU (tools/handover_check.py; capture 2026-09-27
// 02:21, Quest 3 over Steam Link): the share of samples whose optical
// orientation is > 20° off the IMU's, 7 % overall —
//   view:        in front and below 0-12 %; beside and behind the head (> 90°
//                outward) 17-43 %; overhead the hand is lost outright
//   other hand:  this one behind it, within 20° of it as seen from the headset,
//                22-68 %; in front, overlapping within 10°, 37 %; apart 1-7 %
//   distance:    25-35 cm 19 %, 35-55 cm 5 %, beyond 55 cm 15 %
// Other headsets see differently (their maps: TODO). Trust is a product of the
// three, 1 = fully trusted; ImuFusion slows its corrections by it, calibrates
// only from trusted views and tightens its gate.
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include "MathUtil.h"

namespace cf {

namespace trust_detail {
// 1 inside [lo, hi], falling linearly to 0 over `width` outside.
inline double Band(double x, double lo, double hi, double width) {
    if (x < lo) return std::max(0.0, 1.0 - (lo - x) / width);
    if (x > hi) return std::max(0.0, 1.0 - (x - hi) / width);
    return 1.0;
}
inline double Ramp(double x, double x0, double x1, double y0, double y1) {   // y0 at x0 … y1 at x1, clamped
    const double f = std::min(1.0, std::max(0.0, (x - x0) / (x1 - x0)));
    return y0 + f * (y1 - y0);
}
} // namespace trust_detail

// hand: 0 left, 1 right; head: the headset's pose; handPos, otherPos: the tracked hand points (tracking space).
inline double TrackingTrust(int hand, const Xform& head, const Vec3& handPos, const Vec3* otherPos) {
    using namespace trust_detail;
    constexpr double kDeg = 180.0 / 3.14159265358979323846;
    const Vec3 r = handPos - head.p;
    const double dist = Length(r);
    if (dist < 1e-3) return 1.0;
    // View: the direction in the headset's frame (x right, y up, -z forward), azimuth outward-positive.
    const Vec3 local = Rotate(Conj(head.q), r);
    const double az = std::atan2(local.x, -local.z) * kDeg * (hand ? 1.0 : -1.0);
    const double el = std::atan2(local.y, std::hypot(local.x, local.z)) * kDeg;
    const double view = 0.3 + 0.7 * std::min(Band(az, -40.0, 75.0, 30.0), Band(el, -75.0, 10.0, 30.0));
    // The other hand in front of this one, or the two overlapping.
    double occlusion = 1.0;
    if (otherPos) {
        const Vec3 ro = *otherPos - head.p;
        const double lo = Length(ro);
        if (lo > 1e-3) {
            const double sep = std::acos(std::min(1.0, std::max(-1.0, Dot(r, ro) / (dist * lo)))) * kDeg;
            occlusion = dist > lo ? Ramp(sep, 22.0, 35.0, 0.2, 1.0)        // behind it
                                  : Ramp(sep, 10.0, 15.0, 0.5, 1.0);       // in front, overlapping
        }
    }
    // Distance: too close, or at arm's length.
    const double reach = dist < 0.45 ? Ramp(dist, 0.28, 0.38, 0.5, 1.0) : Ramp(dist, 0.55, 0.70, 1.0, 0.6);
    return view * occlusion * reach;
}

} // namespace cf
