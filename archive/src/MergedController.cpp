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
#include <cmath>



namespace merged_ctrl {

static inline void RotateVecByQuat(const vr::HmdQuaternion_t& q,
    float x, float y, float z,
    float& ox, float& oy, float& oz)
{
    // v' = v + 2*cross(q.xyz, cross(q.xyz, v) + q.w * v)
    const float qw = (float)q.w, qx = (float)q.x, qy = (float)q.y, qz = (float)q.z;

    // t = 2 * cross(q.xyz, v)
    const float tx = 2.f * (qy * z - qz * y);
    const float ty = 2.f * (qz * x - qx * z);
    const float tz = 2.f * (qx * y - qy * x);

    // v' = v + qw*t + cross(q.xyz, t)
    ox = x + qw * tx + (qy * tz - qz * ty);
    oy = y + qw * ty + (qz * tx - qx * tz);
    oz = z + qw * tz + (qx * ty - qy * tx);
}

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
    m_lastSettingsPoll = std::chrono::steady_clock::now();
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

    // Face buttons — A and B on both hands (same as Quest)
    input->CreateBooleanComponent(container, "/input/a/click", &m_hBtnA);
    input->CreateBooleanComponent(container, "/input/b/click", &m_hBtnB);

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

        // SteamVR mapping: A/B on both hands
        input->UpdateBooleanComponent(m_hBtnA,          btnA,        0.0);
        input->UpdateBooleanComponent(m_hBtnB,          btnB,        0.0);
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
        input->UpdateBooleanComponent(m_hBtnA,      false, 0.0);
        input->UpdateBooleanComponent(m_hBtnB,      false, 0.0);
        input->UpdateBooleanComponent(m_hJoyClick,  false, 0.0);
        input->UpdateScalarComponent(m_hJoyX,    0.f, 0.0);
        input->UpdateScalarComponent(m_hJoyY,    0.f, 0.0);
        input->UpdateScalarComponent(m_hTrigger, 0.f, 0.0);
        input->UpdateScalarComponent(m_hGrip,    0.f, 0.0);
    }
}

// ── Pose update — reads VRLink hand tracker pose directly from server ─────

void MergedController::UpdatePose() {
    // Find the VRLink hand tracker device by serial name
    // Cache the device index so we don't search every frame
    if (m_sourceDeviceIdx == vr::k_unTrackedDeviceIndexInvalid) {
        m_sourceDeviceIdx = FindSourceDevice();
        if (m_sourceDeviceIdx != vr::k_unTrackedDeviceIndexInvalid) {
            DriverLog("[%s] Found source hand tracker at device index %d\n",
                      m_serial.c_str(), m_sourceDeviceIdx);
        }
    }

    bool gotPose = false;

    if (m_sourceDeviceIdx != vr::k_unTrackedDeviceIndexInvalid) {
        // Read the raw pose from the VRLink hand tracker
        vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
        vr::VRServerDriverHost()->GetRawTrackedDevicePoses(0.f, poses, vr::k_unMaxTrackedDeviceCount);

        const auto& srcPose = poses[m_sourceDeviceIdx];
        if (srcPose.bPoseIsValid && srcPose.bDeviceIsConnected) {
            const auto& mat = srcPose.mDeviceToAbsoluteTracking.m;

            m_pose.poseIsValid = true;
            m_pose.deviceIsConnected = true;
            m_pose.result = vr::TrackingResult_Running_OK;

            m_pose.vecPosition[0] = mat[0][3];
            m_pose.vecPosition[1] = mat[1][3];
            m_pose.vecPosition[2] = mat[2][3];

            // Extract quaternion from 3x3 rotation
            float trace = mat[0][0] + mat[1][1] + mat[2][2];
            if (trace > 0) {
                float s = 0.5f / std::sqrt(trace + 1.0f);
                m_pose.qRotation.w = 0.25f / s;
                m_pose.qRotation.x = (mat[2][1] - mat[1][2]) * s;
                m_pose.qRotation.y = (mat[0][2] - mat[2][0]) * s;
                m_pose.qRotation.z = (mat[1][0] - mat[0][1]) * s;
            } else if (mat[0][0] > mat[1][1] && mat[0][0] > mat[2][2]) {
                float s = 2.0f * std::sqrt(1.0f + mat[0][0] - mat[1][1] - mat[2][2]);
                m_pose.qRotation.w = (mat[2][1] - mat[1][2]) / s;
                m_pose.qRotation.x = 0.25f * s;
                m_pose.qRotation.y = (mat[0][1] + mat[1][0]) / s;
                m_pose.qRotation.z = (mat[0][2] + mat[2][0]) / s;
            } else if (mat[1][1] > mat[2][2]) {
                float s = 2.0f * std::sqrt(1.0f + mat[1][1] - mat[0][0] - mat[2][2]);
                m_pose.qRotation.w = (mat[0][2] - mat[2][0]) / s;
                m_pose.qRotation.x = (mat[0][1] + mat[1][0]) / s;
                m_pose.qRotation.y = 0.25f * s;
                m_pose.qRotation.z = (mat[1][2] + mat[2][1]) / s;
            } else {
                float s = 2.0f * std::sqrt(1.0f + mat[2][2] - mat[0][0] - mat[1][1]);
                m_pose.qRotation.w = (mat[1][0] - mat[0][1]) / s;
                m_pose.qRotation.x = (mat[0][2] + mat[2][0]) / s;
                m_pose.qRotation.y = (mat[1][2] + mat[2][1]) / s;
                m_pose.qRotation.z = 0.25f * s;
            }

            // Apply grip correction rotation (3-axis Euler XYZ).
            // Configured via SteamVR Settings > CyberFinger sliders.
            // Read live so slider changes apply immediately.
            {
                vr::EVRSettingsError serr;
                PollSettingsIfNeeded();
                /*
                float ax = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_x", &serr);
                if (serr != vr::VRSettingsError_None) ax = -60.f;
                float ay = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_y", &serr);
                if (serr != vr::VRSettingsError_None) ay = 0.f;
                float az = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_z", &serr);
                if (serr != vr::VRSettingsError_None) az = 0.f;
                */

                // Convert degrees to radians (half-angles for quaternion)
                float hx = (m_gripAx * 3.14159265f / 180.f) * 0.5f;
                float hy = (m_hand == 0) 
                    ? -(m_gripAy * 3.14159265f / 180.f) * 0.5f 
                    :  (m_gripAy * 3.14159265f / 180.f) * 0.5f;
                float hz = (m_gripAz * 3.14159265f / 180.f) * 0.5f;

                // Euler XYZ to quaternion
                float cx = std::cos(hx), sx = std::sin(hx);
                float cy = std::cos(hy), sy = std::sin(hy);
                float cz = std::cos(hz), sz = std::sin(hz);

                float cw = cx*cy*cz + sx*sy*sz;
                float cqx = sx*cy*cz - cx*sy*sz;
                float cqy = cx*sy*cz + sx*cy*sz;
                float cqz = cx*cy*sz - sx*sy*cz;

                // result = deviceQuat * correctionQuat
                float rw = m_pose.qRotation.w;
                float rx = m_pose.qRotation.x;
                float ry = m_pose.qRotation.y;
                float rz = m_pose.qRotation.z;

                m_pose.qRotation.w = rw*cw - rx*cqx - ry*cqy - rz*cqz;
                m_pose.qRotation.x = rw*cqx + rx*cw + ry*cqz - rz*cqy;
                m_pose.qRotation.y = rw*cqy - rx*cqz + ry*cw + rz*cqx;
                m_pose.qRotation.z = rw*cqz + rx*cqy - ry*cqx + rz*cw;

                /*
                // Apply position offset in controller-local space
                float wx, wy, wz;
                RotateVecByQuat(m_pose.qRotation, m_offX, m_offY, m_offZ, wx, wy, wz);
                if (m_hand==0) m_pose.vecPosition[0] -= wx;
                else m_pose.vecPosition[0] += wx;
                m_pose.vecPosition[1] += wy;
                m_pose.vecPosition[2] += wz;
                */
            }

            m_pose.vecVelocity[0] = srcPose.vVelocity.v[0];
            m_pose.vecVelocity[1] = srcPose.vVelocity.v[1];
            m_pose.vecVelocity[2] = srcPose.vVelocity.v[2];
            m_pose.vecAngularVelocity[0] = srcPose.vAngularVelocity.v[0];
            m_pose.vecAngularVelocity[1] = srcPose.vAngularVelocity.v[1];
            m_pose.vecAngularVelocity[2] = srcPose.vAngularVelocity.v[2];

            gotPose = true;
        } else if (!srcPose.bDeviceIsConnected) {
            // Device disconnected, re-search next time
            m_sourceDeviceIdx = vr::k_unTrackedDeviceIndexInvalid;
        }
    }

    // Debug: log tracking state periodically
    static int poseLogCount[2] = {0, 0};
    if (++poseLogCount[m_hand] % 180 == 0) {
        if (gotPose) {
            DriverLog("[%s] POSE: direct dev=%d pos=(%.3f,%.3f,%.3f)\n",
                      m_serial.c_str(), m_sourceDeviceIdx,
                      m_pose.vecPosition[0], m_pose.vecPosition[1], m_pose.vecPosition[2]);
        } else {
            DriverLog("[%s] POSE: no source device (fallback)\n", m_serial.c_str());
        }
    }

    if (!gotPose) {
        // Fallback — fixed position so controllers remain visible
        m_pose.poseIsValid = true;
        m_pose.result = vr::TrackingResult_Running_OK;
        m_pose.deviceIsConnected = true;

        m_pose.vecPosition[0] = (m_hand == 0) ? -0.2 : 0.2;
        m_pose.vecPosition[1] = 1.0;
        m_pose.vecPosition[2] = -0.3;
        m_pose.qRotation = {1, 0, 0, 0};

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

// ── Find the VRLink hand tracker for this hand ────────────────────────────

vr::TrackedDeviceIndex_t MergedController::FindSourceDevice() {
    const char* leftPatterns[] = {"Hand_Left", "hand_left", nullptr};
    const char* rightPatterns[] = {"Hand_Right", "hand_right", nullptr};
    const char** patterns = (m_hand == 0) ? leftPatterns : rightPatterns;

    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    vr::VRServerDriverHost()->GetRawTrackedDevicePoses(0.f, poses, vr::k_unMaxTrackedDeviceCount);

    for (vr::TrackedDeviceIndex_t i = 0; i < vr::k_unMaxTrackedDeviceCount; ++i) {
        if (!poses[i].bDeviceIsConnected) continue;

        auto props = vr::VRProperties();
        auto container = props->TrackedDeviceToPropertyContainer(i);

        char serial[256] = {};
        vr::ETrackedPropertyError err;
        props->GetStringProperty(container, vr::Prop_SerialNumber_String, serial, sizeof(serial), &err);
        if (err != vr::TrackedProp_Success) continue;

        // Skip our own devices
        if (strstr(serial, "CYBERFINGER") != nullptr) continue;

        for (const char** p = patterns; *p; ++p) {
            if (strstr(serial, *p) != nullptr) {
                DriverLog("[%s] Matched source device [%d] serial=%s\n",
                          m_serial.c_str(), i, serial);
                return i;
            }
        }
    }
    return vr::k_unTrackedDeviceIndexInvalid;
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

void MergedController::PollSettingsIfNeeded()
{
    using namespace std::chrono;

    auto now = steady_clock::now();
    auto elapsed = duration_cast<milliseconds>(now - m_lastSettingsPoll).count();

    if (elapsed < 100)  // 100 ms = 10 Hz polling
        return;

    m_lastSettingsPoll = now;

    vr::EVRSettingsError serr;

    float ax = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_x", &serr);
    if (serr == vr::VRSettingsError_None)
        m_gripAx = ax;

    float ay = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_y", &serr);
    if (serr == vr::VRSettingsError_None)
        m_gripAy = ay;

    float az = vr::VRSettings()->GetFloat("driver_cyberfinger", "grip_angle_z", &serr);
    if (serr == vr::VRSettingsError_None)
        m_gripAz = az;

    m_offX = vr::VRSettings()->GetFloat("driver_cyberfinger", "pose_offset_x", &serr);
    if (serr != vr::VRSettingsError_None) m_offX = 0.f;

    m_offY = vr::VRSettings()->GetFloat("driver_cyberfinger", "pose_offset_y", &serr);
    if (serr != vr::VRSettingsError_None) m_offY = 0.f;

    m_offZ = vr::VRSettings()->GetFloat("driver_cyberfinger", "pose_offset_z", &serr);
    if (serr != vr::VRSettingsError_None) m_offZ = 0.f;

}



} // namespace merged_ctrl

