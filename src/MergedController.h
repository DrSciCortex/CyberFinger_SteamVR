/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// MergedController.h — One merged VR controller (left or right hand)
//
// Appears to SteamVR as a full VR controller with:
//   - Button/axis inputs from gamepad
//   - 6DOF pose from hand tracking
//   - Full 31-bone hand skeleton
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <string>
#include <array>
#include "BoneData.h"
#include "HandTrackingReceiver.h"
#include "SkeletonComposer.h"

namespace merged_ctrl {

class MergedController : public vr::ITrackedDeviceServerDriver {
public:
    MergedController(int hand, const std::string& serialNumber,
                     HandTrackingReceiver* handTracking);
    ~MergedController();

    // ── ITrackedDeviceServerDriver ─────────────────────────────────────
    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void Deactivate() override;
    void EnterStandby() override;
    void* GetComponent(const char* pchComponentNameAndVersion) override;
    void DebugRequest(const char* pchRequest, char* pchResponseBuffer,
                      uint32_t unResponseBufferSize) override;
    vr::DriverPose_t GetPose() override;

    // ── Update loop (called by ServerProvider) ─────────────────────────
    void Update();

    uint32_t GetObjectId() const { return m_objectId; }
    int GetHand() const { return m_hand; }

private:
    void UpdateInputs();
    void UpdatePose();
    void UpdateSkeleton();

    int                    m_hand;          // 0=left, 1=right
    std::string            m_serial;
    uint32_t               m_objectId = vr::k_unTrackedDeviceIndexInvalid;

    HandTrackingReceiver*  m_handTracking = nullptr;
    SkeletonComposer       m_skeletonComposer;

    // Input component handles
    vr::VRInputComponentHandle_t m_hBtnPrimary   = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hBtnSecondary  = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hBtnBumper     = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hJoyX          = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hJoyY          = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hJoyClick      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hTrigger       = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hGrip          = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hSkeleton      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_hHaptic        = vr::k_ulInvalidInputComponentHandle;

    // Cached skeleton
    std::array<vr::VRBoneTransform_t, kNumBones> m_boneTransforms;
    float m_curls[5] = {};

    // Cached pose
    vr::DriverPose_t m_pose;
};

} // namespace merged_ctrl

