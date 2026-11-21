/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// ServerProvider.h — IServerTrackedDeviceProvider implementation
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>
#include "CyberFingerController.h"
#include "OpticalTap.h"
#include "ImuFusion.h"
#include "StudioLink.h"

namespace cf {

class ServerProvider : public vr::IServerTrackedDeviceProvider, private TapListener {
public:
    vr::EVRInitError Init(vr::IVRDriverContext* pDriverContext) override;
    void Cleanup() override;
    const char* const* GetInterfaceVersions() override { return vr::k_InterfaceVersions; }
    void RunFrame() override;
    bool ShouldBlockStandbyMode() override { return false; }
    void EnterStandby() override {}
    void LeaveStandby() override {}

private:
    // TapListener: the headset hands' updates, on their driver's thread and possibly inside other drivers'
    // hooks (Virtual Desktop hooks the same functions). They are only queued here; RepublishThread passes
    // them on, so we never call back into SteamVR from inside another driver's call.
    void OnTapPose(int hand, const vr::DriverPose_t& pose) override;
    void OnTapSkeleton(int hand, vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones,
                       uint32_t count) override;
    void RepublishThread();

    void PollCaptureRequest();
    void PollPoseFilter();
    bool FilteredSource(const std::string& controllerType) const;   // listed in pose_filter_types
    void ScanOtherControllers(double now);
    void UpdateHandoff(const vr::TrackedDevicePose_t* poses, const TapHandSnapshot tap[2], double now);
    void UpdateHiddenControllers(const bool hide[2], bool hideHandSources);
    void RestoreHiddenControllers();
    void SendContext(const vr::TrackedDevicePose_t* poses, const TapHandSnapshot tap[2], double now);
    void LogStatus(const TapHandSnapshot tap[2], double now);
    void OnHaptic(const vr::VREvent_HapticVibration_t& hv);
    void LoadImuCalibration();
    void UpdateImuCalibration(const TapHandSnapshot tap[2], double now);
    void ColdStartImu(int hand);
    void SaveImuCalibration();

    StudioLink m_link;
    ImuFusion m_imuFusion[2];         // the CyberFinger's joint IMU + optical orientation, per hand (fed by m_link)
    bool m_imuFusionEnabled = true;   // setting imu_fusion
    bool m_imuFusionKnown = false;
    bool m_grabTapToHold = true;      // setting grab_tap_to_hold
    bool m_grabTapKnown = false;

    // The IMU calibration carried across sessions (%LOCALAPPDATA%\CyberFinger\imu_calibration.txt), per hand and
    // hand-tracking source (their hand frames may differ): the mounting and lag the fusion starts from.
    std::map<std::string, ImuFusion::Calibration> m_imuSaved[2];   // by source controller type, lower case
    ImuFusion::Calibration m_imuBase[2];          // this session's saved start, which new solves are averaged into
    bool   m_imuHaveBase[2] = {};
    std::string m_imuSource[2];                   // the source the fusion started for
    size_t m_imuSavedSolves[2] = {};              // full solves already taken
    int m_resyncSeen[2] = { -1, -1 };             // the bridge's last resync count (CFG2), -1 before its first
    size_t m_imuResyncsLogged[2] = {};            // resyncs already logged
    bool   m_imuCalDirty = false;
    double m_nextImuCalCheck = 0, m_nextImuCalSave = 0;
    std::unique_ptr<OpticalTap> m_tap;
    std::unique_ptr<CyberFingerController> m_controller[2];
    bool m_tapEnabled = true;
    uint32_t m_contextSeq = 0;
    double m_nextContext = 0;
    double m_nextStatus = 0;
    double m_lastStatus = 0;
    bool m_loggedHaptic[2] = {};
    bool m_active = true;             // setting "active": live on/off
    double m_nextSettingsPoll = 0;
    PoseFilter::Params m_filter;      // last applied pose filter settings
    bool m_filterKnown = false;
    std::string m_filterTypesSetting;             // pose_filter_types as read
    std::vector<std::string> m_filterTypes;       // … split, lower case

    // Other drivers' hand controllers (the Touch controllers Steam Link and Virtual Desktop emulate or connect),
    // rescanned every second.
    struct OtherController {
        vr::PropertyContainerHandle_t container;
        uint32_t index;                           // tracked device index
        int hand;                                 // from its role hint
    };
    std::vector<OtherController> m_others;
    double m_nextScan = 0;
    uint32_t m_deviceCount = 0;                   // a change means a device was just added…
    double m_fastScanUntil = 0;                   // … so scan every frame until then
    std::vector<std::string> m_handSourceTypes;   // tap_controller_types: hand-tracking sources, never others
    std::vector<vr::PropertyContainerHandle_t> m_handSources;  // the devices of those types, rescanned with the others
    // SlimeVR's body trackers (elbows, chest …): device index and role code (BodyTrackerCode), rescanned with the
    // others; captured (kind 8) for the arm model.
    std::vector<std::pair<uint32_t, uint8_t>> m_bodyTrackers;

    // Marked Prop_NeverTracked: the other controllers while CyberFinger holds their hand, the hand-tracking sources
    // while CyberFinger is active.
    std::set<vr::PropertyContainerHandle_t> m_hidden;
    std::set<vr::PropertyContainerHandle_t> m_hideRefused;   // SteamVR refused the write: don't retry
    bool m_hideSetting = true;                    // hide_other_hand_controllers

    // Handoff: a hand whose hand tracking stopped while a controller for it is tracked (the user picked the
    // controllers up) is released to the controller, and taken back when hand tracking returns.
    bool   m_yieldSetting = true;                 // yield_to_controllers
    bool   m_yield[2] = {};
    double m_yieldSince[2] = { -1, -1 };
    double m_returnSince[2] = { -1, -1 };

    // Headset hand updates waiting for RepublishThread (latest wins).
    struct Pending {
        bool hasPose = false, hasSkeleton = false;
        vr::DriverPose_t pose{};
        double poseArrival = 0;       // NowSeconds() when the source submitted it
        vr::VRBoneTransform_t bones[eBone_Count]{};
    };
    std::mutex m_pubLock;
    std::condition_variable m_pubWake;
    Pending m_pending[2];
    bool m_pubRun = false;
    std::thread m_pubThread;
};

} // namespace cf
