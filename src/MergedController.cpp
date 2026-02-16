/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// MergedController.cpp — Merged VR controller device driver
// ═══════════════════════════════════════════════════════════════════════════

#include "MergedController.h"
#include "Utils.h"
#include <cstring>

namespace merged_ctrl {

MergedController::MergedController(
    int hand, const std::string& serialNumber,
    HandTrackingReceiver* handTracking)
    : m_hand(hand)
    , m_serial(serialNumber)
    , m_handTracking(handTracking)
{
    m_pose = MakeDefaultPose();
    m_pose.deviceIsConnected = true;
    m_pose.poseIsValid = true;
    m_pose.result = vr::TrackingResult_Running_OK;
    // Start slightly in front of the user so they can see the controllers
    m_pose.vecPosition[0] = (hand == 0) ? -0.2 : 0.2;  // left/right offset
    m_pose.vecPosition[1] = 1.0;   // ~waist height
    m_pose.vecPosition[2] = -0.3;  // slightly in front
    m_boneTransforms = MakeRestPose();
}

MergedController::~MergedController() {}

// ═══════════════════════════════════════════════════════════════════════════
// ITrackedDeviceServerDriver
// ═══════════════════════════════════════════════════════════════════════════

vr::EVRInitError MergedController::Activate(uint32_t unObjectId) {
    m_objectId = unObjectId;

    DriverLog("MergedController[%s]: Activate (id=%u, hand=%d)\n",
              m_serial.c_str(), unObjectId, m_hand);

    auto props = vr::VRProperties();
    auto container = props->TrackedDeviceToPropertyContainer(m_objectId);

    // ── Device properties ──────────────────────────────────────────────
    props->SetStringProperty(container, vr::Prop_SerialNumber_String, m_serial.c_str());
    props->SetStringProperty(container, vr::Prop_ModelNumber_String, "CyberFinger");
    props->SetStringProperty(container, vr::Prop_ManufacturerName_String, "SciCortex");
    props->SetStringProperty(container, vr::Prop_TrackingSystemName_String, "cyberfinger");

    props->SetInt32Property(container, vr::Prop_DeviceClass_Int32,
                            vr::TrackedDeviceClass_Controller);
    props->SetInt32Property(container, vr::Prop_ControllerRoleHint_Int32,
                            m_hand == 0 ? vr::TrackedControllerRole_LeftHand
                                        : vr::TrackedControllerRole_RightHand);

    // Input profile
    props->SetStringProperty(container, vr::Prop_InputProfilePath_String,
        "{cyberfinger}/input/cyberfinger_profile.json");

    // Controller type (used for binding UI)
    props->SetStringProperty(container, vr::Prop_ControllerType_String,
        "cyberfinger");

    // Render model — use SteamVR's built-in generic controller models
    // (or use hand skeleton rendering which doesn't need a render model)
    props->SetStringProperty(container, vr::Prop_RenderModelName_String,
        m_hand == 0 ? "indexcontroller_left" : "indexcontroller_right");

    // Mark as a wireless device (prevents SteamVR from expecting USB)
    props->SetBoolProperty(container, vr::Prop_DeviceIsWireless_Bool, true);

    // Firmware / hardware version (avoids warnings)
    props->SetUint64Property(container, vr::Prop_HardwareRevision_Uint64, 1);
    props->SetUint64Property(container, vr::Prop_FirmwareVersion_Uint64, 1);
    props->SetStringProperty(container, vr::Prop_ResourceRoot_String, "cyberfinger");

    // ── Create input components ────────────────────────────────────────
    auto input = vr::VRDriverInput();

    // Face buttons
    if (m_hand == 0) {
        input->CreateBooleanComponent(container, "/input/x/click", &m_hBtnPrimary);
        input->CreateBooleanComponent(container, "/input/y/click", &m_hBtnSecondary);
    } else {
        input->CreateBooleanComponent(container, "/input/a/click", &m_hBtnPrimary);
        input->CreateBooleanComponent(container, "/input/b/click", &m_hBtnSecondary);
    }

    // Bumper
    input->CreateBooleanComponent(container, "/input/bumper/click", &m_hBtnBumper);

    // Joystick
    input->CreateScalarComponent(container, "/input/joystick/x",
        &m_hJoyX, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    input->CreateScalarComponent(container, "/input/joystick/y",
        &m_hJoyY, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    input->CreateBooleanComponent(container, "/input/joystick/click", &m_hJoyClick);

    // Trigger (from gamepad analog trigger)
    input->CreateScalarComponent(container, "/input/trigger/value",
        &m_hTrigger, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedOneSided);

    // Grip (mapped from bumper as analog)
    input->CreateScalarComponent(container, "/input/grip/value",
        &m_hGrip, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedOneSided);

    // ── Skeletal input ─────────────────────────────────────────────────
    const char* skeletonPath = (m_hand == 0)
        ? "/input/skeleton/left"
        : "/input/skeleton/right";

    const char* basePosePath = (m_hand == 0)
        ? "/pose/raw"
        : "/pose/raw";

    vr::EVRInputError skelErr = input->CreateSkeletonComponent(
        container,
        skeletonPath,
        (m_hand == 0) ? "/skeleton/hand/left" : "/skeleton/hand/right",
        basePosePath,
        vr::VRSkeletalTracking_Full,
        m_boneTransforms.data(),
        kNumBones,
        &m_hSkeleton
    );

    if (skelErr != vr::VRInputError_None) {
        DriverLog("MergedController[%s]: Failed to create skeleton component (err=%d)\n",
                  m_serial.c_str(), (int)skelErr);
    } else {
        DriverLog("MergedController[%s]: Skeleton component created OK\n", m_serial.c_str());
    }

    // Haptic output
    input->CreateHapticComponent(container, "/output/haptic", &m_hHaptic);

    return vr::VRInitError_None;
}

void MergedController::Deactivate() {
    DriverLog("MergedController[%s]: Deactivate\n", m_serial.c_str());
    m_objectId = vr::k_unTrackedDeviceIndexInvalid;
}

void MergedController::EnterStandby() {}

void* MergedController::GetComponent(const char* pchComponentNameAndVersion) {
    DriverLog("MergedController[%s]: GetComponent(%s)\n",
              m_serial.c_str(), pchComponentNameAndVersion);
    // No additional components — SteamVR handles controller input via
    // the IVRDriverInput interface which we use in Activate()
    return nullptr;
}

void MergedController::DebugRequest(
    const char* pchRequest, char* pchResponseBuffer, uint32_t unResponseBufferSize)
{
    if (unResponseBufferSize > 0) pchResponseBuffer[0] = '\0';
}

vr::DriverPose_t MergedController::GetPose() {
    return m_pose;
}

// ═══════════════════════════════════════════════════════════════════════════
// Update loop — called each frame by ServerProvider
// ═══════════════════════════════════════════════════════════════════════════

void MergedController::Update() {
    if (m_objectId == vr::k_unTrackedDeviceIndexInvalid) return;

    UpdateInputs();
    UpdatePose();
    UpdateSkeleton();
}

// ── Button & axis input updates ────────────────────────────────────────────

void MergedController::UpdateInputs() {
    auto input = vr::VRDriverInput();

    GamepadState gp = (m_hand == 0) ? m_handTracking->GetGamepadLeft()
                                    : m_handTracking->GetGamepadRight();

    bool hasGamepad = m_handTracking->HasRecentGamepad(m_hand, 0.25);

    // Debug: log gamepad state periodically (~every 2s at 90Hz)
    static int logCount[2] = {0, 0};
    if (++logCount[m_hand] % 180 == 0) {
        if (hasGamepad) {
            DriverLog("[%s] GP: btn=0x%02X trig=%d grip=%d B=%d jClk=%d A=%d jX=%d jY=%d trigA=%d bat=%d%%\n",
                      m_serial.c_str(), gp.buttons,
                      (gp.buttons & 0x01) != 0, (gp.buttons & 0x02) != 0,
                      (gp.buttons & 0x04) != 0, (gp.buttons & 0x08) != 0,
                      (gp.buttons & 0x10) != 0,
                      gp.joy_x, gp.joy_y, gp.trigger_analog, gp.battery_pct);
        } else {
            DriverLog("[%s] GP: no recent data\n", m_serial.c_str());
        }
    }

    if (hasGamepad) {
        // BLE GATT button bits (from firmware):
        //   bit0 = Trigger (digital)
        //   bit1 = Grip
        //   bit2 = B
        //   bit3 = Joy click
        //   bit4 = A
        bool btnTrigger    = (gp.buttons & 0x01) != 0;
        bool btnGrip       = (gp.buttons & 0x02) != 0;
        bool btnB          = (gp.buttons & 0x04) != 0;
        bool btnJoyClick   = (gp.buttons & 0x08) != 0;
        bool btnA          = (gp.buttons & 0x10) != 0;

        // SteamVR mapping:
        //   Primary   = A (right) / X (left)  <- A (bit4)
        //   Secondary = B (right) / Y (left)  <- B (bit2)
        input->UpdateBooleanComponent(m_hBtnPrimary,    btnA,        0.0);
        input->UpdateBooleanComponent(m_hBtnSecondary,  btnB,        0.0);
        input->UpdateBooleanComponent(m_hBtnBumper,     false,       0.0);
        input->UpdateBooleanComponent(m_hJoyClick,      btnJoyClick, 0.0);

        // Joystick: normalize from int16 (-32767..32767) to float (-1..1)
        // Y axis inverted: ESP32 sends +Y = up, SteamVR expects -Y = up
        float joyX =  gp.joy_x / 32767.f;
        float joyY = -gp.joy_y / 32767.f;
        input->UpdateScalarComponent(m_hJoyX, joyX, 0.0);
        input->UpdateScalarComponent(m_hJoyY, joyY, 0.0);

        // Trigger & grip
        float triggerVal = (gp.trigger_analog > 10)
                           ? (gp.trigger_analog / 255.f)
                           : (btnTrigger ? 1.0f : 0.0f);
        input->UpdateScalarComponent(m_hTrigger, triggerVal, 0.0);
        input->UpdateScalarComponent(m_hGrip,    btnGrip ? 1.0f : 0.0f, 0.0);
    } else {
        // No gamepad data — zero everything
        input->UpdateBooleanComponent(m_hBtnPrimary,   false, 0.0);
        input->UpdateBooleanComponent(m_hBtnSecondary,  false, 0.0);
        input->UpdateBooleanComponent(m_hBtnBumper,     false, 0.0);
        input->UpdateBooleanComponent(m_hJoyClick,      false, 0.0);
        input->UpdateScalarComponent(m_hJoyX,    0.f, 0.0);
        input->UpdateScalarComponent(m_hJoyY,    0.f, 0.0);
        input->UpdateScalarComponent(m_hTrigger, 0.f, 0.0);
        input->UpdateScalarComponent(m_hGrip,    0.f, 0.0);
    }
}

// ── Pose update from hand tracking ─────────────────────────────────────────

void MergedController::UpdatePose() {
    bool hasTracking = m_handTracking->HasRecentData(m_hand, 0.15);

    if (hasTracking) {
        HandTrackingState ht = (m_hand == 0) ? m_handTracking->GetLeft()
                                             : m_handTracking->GetRight();

        m_pose.poseIsValid = true;
        m_pose.deviceIsConnected = true;
        m_pose.result = vr::TrackingResult_Running_OK;

        // Hand tracking provides position in HMD-relative space
        // The driver needs to report in standing/raw tracking space.
        // We set the driver-from-head transform to identity and let SteamVR
        // handle the HMD-relative → world transform.
        m_pose.vecPosition[0] = ht.pos[0];
        m_pose.vecPosition[1] = ht.pos[1];
        m_pose.vecPosition[2] = ht.pos[2];

        m_pose.qRotation.w = ht.quat[0];
        m_pose.qRotation.x = ht.quat[1];
        m_pose.qRotation.y = ht.quat[2];
        m_pose.qRotation.z = ht.quat[3];

        // Zero velocity (hand tracking data is position-only for now)
        m_pose.vecVelocity[0] = 0;
        m_pose.vecVelocity[1] = 0;
        m_pose.vecVelocity[2] = 0;
        m_pose.vecAngularVelocity[0] = 0;
        m_pose.vecAngularVelocity[1] = 0;
        m_pose.vecAngularVelocity[2] = 0;
    } else {
        // No hand tracking → still show controllers at a fixed fallback position
        // so they remain visible and buttons still work
        m_pose.poseIsValid = true;
        m_pose.result = vr::TrackingResult_Running_OK;
        m_pose.deviceIsConnected = true;

        // Fixed position: waist height, slightly in front, offset left/right
        m_pose.vecPosition[0] = (m_hand == 0) ? -0.2 : 0.2;
        m_pose.vecPosition[1] = 1.0;
        m_pose.vecPosition[2] = -0.3;

        m_pose.qRotation.w = 1;
        m_pose.qRotation.x = 0;
        m_pose.qRotation.y = 0;
        m_pose.qRotation.z = 0;

        m_pose.vecVelocity[0] = 0;
        m_pose.vecVelocity[1] = 0;
        m_pose.vecVelocity[2] = 0;
        m_pose.vecAngularVelocity[0] = 0;
        m_pose.vecAngularVelocity[1] = 0;
        m_pose.vecAngularVelocity[2] = 0;
    }

    vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
        m_objectId, m_pose, sizeof(vr::DriverPose_t));
}

// ── Skeleton update ────────────────────────────────────────────────────────

void MergedController::UpdateSkeleton() {
    if (m_hSkeleton == vr::k_ulInvalidInputComponentHandle) return;

    GamepadState gp = (m_hand == 0) ? m_handTracking->GetGamepadLeft()
                                    : m_handTracking->GetGamepadRight();
    HandTrackingState ht = (m_hand == 0) ? m_handTracking->GetLeft()
                                         : m_handTracking->GetRight();

    // Check if hand tracking data is recent enough
    if (!m_handTracking->HasRecentData(m_hand, 0.15)) {
        ht.valid = false;
    }

    m_skeletonComposer.Compose(m_hand, gp, ht, m_boneTransforms, m_curls);

    // Determine skeletal tracking level
    vr::EVRSkeletalMotionRange motionRange = vr::VRSkeletalMotionRange_WithController;

    vr::EVRInputError err = vr::VRDriverInput()->UpdateSkeletonComponent(
        m_hSkeleton,
        motionRange,
        m_boneTransforms.data(),
        kNumBones
    );

    if (err != vr::VRInputError_None) {
        // Only log occasionally to avoid spam
        static int logCounter = 0;
        if (++logCounter % 1000 == 0) {
            DriverLog("MergedController[%s]: UpdateSkeletonComponent err=%d\n",
                      m_serial.c_str(), (int)err);
        }
    }
}

} // namespace merged_ctrl

