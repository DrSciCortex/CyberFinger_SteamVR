/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// SkeletonComposer.cpp — Skeleton blending: hand tracking + gamepad
// ═══════════════════════════════════════════════════════════════════════════

#include "SkeletonComposer.h"
#include "Utils.h"
#include <algorithm>
#include <cmath>

namespace merged_ctrl {

// GamepadState button bits (from vr_gatt.h / HandTrackingReceiver.h):
//   bit0 = AX (trigger)
//   bit1 = BY (grip)
//   bit2 = BP (menu/bumper)
//   bit3 = ST (joy click)
//   bit4 = STARTSELECT

void SkeletonComposer::Compose(
    int hand,
    const GamepadState& gamepad,
    const HandTrackingState& tracking,
    std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
    float outCurls[5])
{
    if (tracking.valid) {
        BlendTrackedWithGamepad(hand, gamepad, tracking, outBones, outCurls);
    } else {
        SyntheticFromGamepad(gamepad, outBones, outCurls);
    }
}

// ── Synthetic skeleton from gamepad only ───────────────────────────────────

void SkeletonComposer::SyntheticFromGamepad(
    const GamepadState& gamepad,
    std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
    float outCurls[5])
{
    outBones = m_restPose;

    bool btnTrigger  = (gamepad.buttons & 0x01) != 0;
    bool btnGrip     = (gamepad.buttons & 0x02) != 0;
    bool btnMenu     = (gamepad.buttons & 0x04) != 0;
    bool btnJoyClick = (gamepad.buttons & 0x08) != 0;

    // Trigger → index curl (use analog if available, else digital)
    float indexCurl  = (gamepad.trigger_analog > 10)
                       ? (gamepad.trigger_analog / 255.f)
                       : (btnTrigger ? 1.0f : 0.0f);
    float thumbCurl  = btnMenu ? 1.0f : 0.0f;
    float gripCurl   = btnGrip ? 1.0f : 0.0f;
    float middleCurl = gripCurl;
    float ringCurl   = gripCurl;
    float pinkyCurl  = gripCurl;

    // Joy click: tense all fingers slightly
    if (btnJoyClick) {
        thumbCurl  = std::max(thumbCurl, 0.3f);
        indexCurl  = std::max(indexCurl, 0.3f);
        middleCurl = std::max(middleCurl, 0.3f);
        ringCurl   = std::max(ringCurl, 0.3f);
        pinkyCurl  = std::max(pinkyCurl, 0.3f);
    }

    CurlFinger(outBones.data(), eBone_Thumb0, 4, thumbCurl);
    CurlFinger(outBones.data(), eBone_IndexFinger0, 5, indexCurl);
    CurlFinger(outBones.data(), eBone_MiddleFinger0, 5, middleCurl);
    CurlFinger(outBones.data(), eBone_RingFinger0, 5, ringCurl);
    CurlFinger(outBones.data(), eBone_PinkyFinger0, 5, pinkyCurl);

    outCurls[0] = thumbCurl;
    outCurls[1] = indexCurl;
    outCurls[2] = middleCurl;
    outCurls[3] = ringCurl;
    outCurls[4] = pinkyCurl;
}

// ── Blended skeleton: hand tracking + gamepad overrides ────────────────────

void SkeletonComposer::BlendTrackedWithGamepad(
    int hand,
    const GamepadState& gamepad,
    const HandTrackingState& tracking,
    std::array<vr::VRBoneTransform_t, kNumBones>& outBones,
    float outCurls[5])
{
    outBones = tracking.bones;

    for (int i = 0; i < 5; ++i)
        outCurls[i] = tracking.curls[i];

    bool btnTrigger = (gamepad.buttons & 0x01) != 0;
    bool btnGrip    = (gamepad.buttons & 0x02) != 0;
    bool btnMenu    = (gamepad.buttons & 0x04) != 0;

    // Trigger → index finger override
    float triggerCurl = (gamepad.trigger_analog > 10)
                        ? (gamepad.trigger_analog / 255.f)
                        : (btnTrigger ? 1.0f : 0.0f);
    if (triggerCurl > 0.1f && triggerCurl > outCurls[1]) {
        outCurls[1] = triggerCurl;
        CurlFinger(outBones.data(), eBone_IndexFinger0, 5, triggerCurl);
    }

    // Grip → middle + ring + pinky
    if (btnGrip) {
        float gripCurl = 1.0f;
        for (int f = 2; f <= 4; ++f) {
            if (gripCurl > outCurls[f])
                outCurls[f] = gripCurl;
        }
        CurlFinger(outBones.data(), eBone_MiddleFinger0, 5, std::max(outCurls[2], gripCurl));
        CurlFinger(outBones.data(), eBone_RingFinger0,   5, std::max(outCurls[3], gripCurl));
        CurlFinger(outBones.data(), eBone_PinkyFinger0,  5, std::max(outCurls[4], gripCurl));
    }

    // Menu → thumb
    if (btnMenu) {
        outCurls[0] = std::max(outCurls[0], 0.8f);
        CurlFinger(outBones.data(), eBone_Thumb0, 4, outCurls[0]);
    }
}

} // namespace merged_ctrl

