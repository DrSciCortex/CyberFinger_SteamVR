/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// SyntheticHand.h — a made-up tracked hand, for the tests and the driver simulator

#include <cmath>
#include "BoneData.h"
#include "CfHand.h"
#include "MathUtil.h"

namespace cf {

// A hand in OpenXR's joint layout, in its wrist's frame: fingers along -z, the palm facing -y, each finger
// curled by curl[f] (0 straight .. 1: 80° at each of its three joints). Right hand: the thumb at +x.
inline void MakeJoints(bool right, const float curl[5], Xform joints[kNumJoints]) {
    const double side = right ? 1.0 : -1.0;
    joints[0] = { {}, { 0, 0, -0.05 } };                // palm
    joints[1] = {};                                     // wrist
    const double baseX[5] = { 0.03, 0.025, 0.005, -0.012, -0.028 };
    const double length[5][4] = { { 0.03, 0.035, 0.03, 0.025 }, { 0.07, 0.04, 0.025, 0.02 }, { 0.07, 0.045, 0.028, 0.02 },
                                  { 0.065, 0.04, 0.026, 0.02 }, { 0.06, 0.03, 0.02, 0.018 } };
    for (int f = 0; f < 5; ++f) {
        const int first = kFingerFirstBone[f], n = f == 0 ? 4 : 5;
        Vec3 p{ side * baseX[f], 0, -0.02 };
        double bend = 0;                                // about x: fingers fold toward the palm (-y)
        for (int k = 0; k < n; ++k) {
            if (k >= 1) bend += DegToRad(80.0) * curl[f];   // every joint past the metacarpal bends
            const Quat q{ std::cos(bend / 2), -std::sin(bend / 2), 0, 0 };
            joints[first + k] = { q, p };
            if (k + 1 < n) p = p + Rotate(q, Vec3{ 0, 0, -length[f][k] });
        }
    }
}

} // namespace cf
