/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// SkeletonComposer.h — Merges hand tracking skeleton + gamepad button state
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <array>
#include "BoneData.h"
#include "HandTrackingReceiver.h"

namespace merged_ctrl {

class SkeletonComposer {
public:
    void Compose(
        int hand,
        const GamepadState& gamepad,
        const HandTrackingState& tracking,
        std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
        float outCurls[5]
    );

private:
    void SyntheticFromGamepad(
        const GamepadState& gamepad,
        std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
        float outCurls[5]
    );

    void BlendTrackedWithGamepad(
        int hand,
        const GamepadState& gamepad,
        const HandTrackingState& tracking,
        std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
        float outCurls[5]
    );

    std::array<vr::VRBoneTransform_t, kNumBones> m_restPose = MakeRestPose();
};

} // namespace merged_ctrl

