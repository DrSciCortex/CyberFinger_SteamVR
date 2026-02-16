/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// BoneData.h — SteamVR Hand Skeleton bone definitions (31 bones per hand)
// ═══════════════════════════════════════════════════════════════════════════
//
// SteamVR expects exactly 31 bones per hand in this order.
// Reference: openvr.h  HandSkeletonBone enum
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <array>
#include <cmath>

namespace merged_ctrl {

// Number of bones in the SteamVR hand skeleton
static constexpr int kNumBones = 31;

// Bone indices matching SteamVR's HandSkeletonBone enum
enum BoneIndex : int {
    eBone_Root = 0,
    eBone_Wrist,
    eBone_Thumb0,
    eBone_Thumb1,
    eBone_Thumb2,
    eBone_Thumb3,           // tip
    eBone_IndexFinger0,
    eBone_IndexFinger1,
    eBone_IndexFinger2,
    eBone_IndexFinger3,
    eBone_IndexFinger4,     // tip
    eBone_MiddleFinger0,
    eBone_MiddleFinger1,
    eBone_MiddleFinger2,
    eBone_MiddleFinger3,
    eBone_MiddleFinger4,    // tip
    eBone_RingFinger0,
    eBone_RingFinger1,
    eBone_RingFinger2,
    eBone_RingFinger3,
    eBone_RingFinger4,      // tip
    eBone_PinkyFinger0,
    eBone_PinkyFinger1,
    eBone_PinkyFinger2,
    eBone_PinkyFinger3,
    eBone_PinkyFinger4,     // tip
    eBone_Aux_Thumb,        // auxiliary / metacarpal markers
    eBone_Aux_IndexFinger,
    eBone_Aux_MiddleFinger,
    eBone_Aux_RingFinger,
    eBone_Aux_PinkyFinger,
};

// Parent bone lookup (root has no parent, set to -1)
static constexpr int kBoneParent[kNumBones] = {
    -1,                     // Root
    eBone_Root,             // Wrist
    eBone_Wrist,            // Thumb0
    eBone_Thumb0,           // Thumb1
    eBone_Thumb1,           // Thumb2
    eBone_Thumb2,           // Thumb3 (tip)
    eBone_Wrist,            // IndexFinger0
    eBone_IndexFinger0,     // IndexFinger1
    eBone_IndexFinger1,     // IndexFinger2
    eBone_IndexFinger2,     // IndexFinger3
    eBone_IndexFinger3,     // IndexFinger4 (tip)
    eBone_Wrist,            // MiddleFinger0
    eBone_MiddleFinger0,    // MiddleFinger1
    eBone_MiddleFinger1,    // MiddleFinger2
    eBone_MiddleFinger2,    // MiddleFinger3
    eBone_MiddleFinger3,    // MiddleFinger4 (tip)
    eBone_Wrist,            // RingFinger0
    eBone_RingFinger0,      // RingFinger1
    eBone_RingFinger1,      // RingFinger2
    eBone_RingFinger2,      // RingFinger3
    eBone_RingFinger3,      // RingFinger4 (tip)
    eBone_Wrist,            // PinkyFinger0
    eBone_PinkyFinger0,     // PinkyFinger1
    eBone_PinkyFinger1,     // PinkyFinger2
    eBone_PinkyFinger2,     // PinkyFinger3
    eBone_PinkyFinger3,     // PinkyFinger4 (tip)
    eBone_Wrist,            // Aux_Thumb
    eBone_Wrist,            // Aux_IndexFinger
    eBone_Wrist,            // Aux_MiddleFinger
    eBone_Wrist,            // Aux_RingFinger
    eBone_Wrist,            // Aux_PinkyFinger
};

// Identity quaternion / zero-position rest pose (open hand)
inline vr::VRBoneTransform_t MakeIdentityBone() {
    vr::VRBoneTransform_t b{};
    b.position.v[0] = b.position.v[1] = b.position.v[2] = b.position.v[3] = 0.f;
    b.orientation.w = 1.f;
    b.orientation.x = b.orientation.y = b.orientation.z = 0.f;
    return b;
}

// Build a default open-hand rest pose.
// In production you would load exact Valve reference poses; this gives a
// reasonable starting skeleton that the hand-tracking data overrides.
inline std::array<vr::VRBoneTransform_t, kNumBones> MakeRestPose() {
    std::array<vr::VRBoneTransform_t, kNumBones> pose;
    for (auto& b : pose) b = MakeIdentityBone();

    // Slight wrist offset from root
    pose[eBone_Wrist].position.v[1] = 0.05f;

    // Finger metacarpal offsets (spread across hand width)
    auto setPos = [&](int idx, float x, float y, float z) {
        pose[idx].position.v[0] = x;
        pose[idx].position.v[1] = y;
        pose[idx].position.v[2] = z;
    };

    // Thumb
    setPos(eBone_Thumb0, 0.02f, 0.01f, -0.015f);
    setPos(eBone_Thumb1, 0.0f, 0.035f, 0.0f);
    setPos(eBone_Thumb2, 0.0f, 0.03f, 0.0f);
    setPos(eBone_Thumb3, 0.0f, 0.025f, 0.0f);

    // Index
    setPos(eBone_IndexFinger0, 0.01f, 0.065f, 0.0f);
    setPos(eBone_IndexFinger1, 0.0f, 0.04f, 0.0f);
    setPos(eBone_IndexFinger2, 0.0f, 0.028f, 0.0f);
    setPos(eBone_IndexFinger3, 0.0f, 0.022f, 0.0f);
    setPos(eBone_IndexFinger4, 0.0f, 0.018f, 0.0f);

    // Middle
    setPos(eBone_MiddleFinger0, 0.0f, 0.07f, 0.0f);
    setPos(eBone_MiddleFinger1, 0.0f, 0.043f, 0.0f);
    setPos(eBone_MiddleFinger2, 0.0f, 0.03f, 0.0f);
    setPos(eBone_MiddleFinger3, 0.0f, 0.024f, 0.0f);
    setPos(eBone_MiddleFinger4, 0.0f, 0.018f, 0.0f);

    // Ring
    setPos(eBone_RingFinger0, -0.01f, 0.065f, 0.0f);
    setPos(eBone_RingFinger1, 0.0f, 0.04f, 0.0f);
    setPos(eBone_RingFinger2, 0.0f, 0.028f, 0.0f);
    setPos(eBone_RingFinger3, 0.0f, 0.022f, 0.0f);
    setPos(eBone_RingFinger4, 0.0f, 0.016f, 0.0f);

    // Pinky
    setPos(eBone_PinkyFinger0, -0.02f, 0.058f, 0.0f);
    setPos(eBone_PinkyFinger1, 0.0f, 0.032f, 0.0f);
    setPos(eBone_PinkyFinger2, 0.0f, 0.022f, 0.0f);
    setPos(eBone_PinkyFinger3, 0.0f, 0.018f, 0.0f);
    setPos(eBone_PinkyFinger4, 0.0f, 0.014f, 0.0f);

    return pose;
}

// Curl a single finger by rotating its joints.
// curl = 0 → open, curl = 1 → fully closed (~90° per joint)
inline void CurlFinger(vr::VRBoneTransform_t* bones, int startBone, int numJoints, float curl) {
    float angle = curl * 1.5708f; // 0..π/2
    float sinA = std::sin(angle * 0.5f);
    float cosA = std::cos(angle * 0.5f);
    // Rotate around local X axis (flex)
    for (int i = 1; i < numJoints; ++i) {  // skip metacarpal
        auto& b = bones[startBone + i];
        b.orientation.w = cosA;
        b.orientation.x = sinA;
        b.orientation.y = 0.f;
        b.orientation.z = 0.f;
    }
}

} // namespace merged_ctrl

