/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CyberFingerController.h — one CyberFinger hand as a SteamVR controller
//
// Holds the /user/hand/left|right role (high hand-selection priority) and
// exposes the CyberFinger's physical inputs plus a 31-bone skeleton. Per frame it
// picks one pose/skeleton source:
//
//   FUSED        CFHS from the bridge (an externally fused hand)
//   PASSTHROUGH  the headset's own hand tracking (OpticalTap), re-rooted.
//                Each pose and skeleton update of the headset hand is
//                republished as it arrives (OnTapPose / OnTapSkeleton), with
//                its own timing; the frame loop only takes over when those
//                updates stop or during a cross-fade.
//   NO_POSE      nothing tracks the hand: pose invalid, inputs stay live
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <atomic>
#include <mutex>
#include <string>
#include "BoneData.h"
#include "MathUtil.h"
#include "ImuFusion.h"
#include "OpticalTap.h"
#include "PoseFilter.h"
#include "Protocol.h"
#include "SkeletonSynth.h"
#include "StudioLink.h"
#include "LongPress.h"
#include "GestureClick.h"
#include "TapHold.h"

namespace cf {

class CyberFingerController final : public vr::ITrackedDeviceServerDriver {
public:
    struct Config {
        std::string serial;
        vr::EVRSkeletalTrackingLevel trackingLevel = vr::VRSkeletalTracking_Full;
        int32_t handPriority = 1000;
        double  grabTapTime = 0.2;          // s: a shorter grip press latches /input/grab (0 = never)
        uint8_t maskA = kBtnStartSelect;    // CyberFinger buttons driving /input/a/click
        uint8_t maskB = kBtnMenu;           // … /input/b/click (context menu, like Touch B/Y)
        uint8_t maskSystem = 0;             // … /input/system/click (dashboard)
        bool    forwardTapSystem = false;   // headset hand's system button → ours (the left pink button does it)
        double  blackHoldTime = 0.8;        // s: the A button (black) held this long is /input/a_hold instead of A
                                            // (Resonite: FluxAction1/2); 0 = no long press (A reports as pressed)
        double  fusedTimeout = 0.15;        // s
        double  disconnectAfter = 0.0;      // s without any data → report disconnected; 0 = never
        Xform   noSkeletonOffset;           // tap raw pose → our raw pose, when no tap skeleton was ever seen
    };

    CyberFingerController(int hand, const Config& cfg);

    // ── ITrackedDeviceServerDriver ─────────────────────────────────────
    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void Deactivate() override;
    void EnterStandby() override {}
    void* GetComponent(const char*) override { return nullptr; }
    void DebugRequest(const char*, char* buf, uint32_t size) override { if (size) buf[0] = 0; }
    vr::DriverPose_t GetPose() override { return m_pose; }

    // active = false reports the device disconnected (the setting "active"), releasing the hand role.
    void Update(const CyberFingerState& cyberFinger, const HandStateSample& fused, const TapHandSnapshot& tap, double now,
                bool active = true);

    // The headset hand's own updates, as they arrive (ServerProvider's republish thread).
    void OnTapPose(const vr::DriverPose_t& pose);
    void OnTapSkeleton(vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones, uint32_t count);

    // Poses and skeletons republished per second since the last call.
    void TakeEventRates(double seconds, double& poseRate, double& skeletonRate);

    // Filter for the headset hand's pose in PASSTHROUGH (settings, changeable live).
    void SetPoseFilter(const PoseFilter::Params& params);
    // Whether the current source's poses need it (Steam Link's do; Virtual Desktop's are clean). Frame loop.
    void SetSourceFiltered(bool filtered, const std::string& sourceType);

    // The CyberFinger's joint IMU fused with the headset's orientation (owned by ServerProvider); used in PASSTHROUGH
    // once calibrated. The FUSED mode (the sender's own fusion) never uses it.
    void SetImuFusion(ImuFusion* fusion) { m_imu = fusion; }
    void SetImuFusionEnabled(bool on) { m_imuEnabled.store(on, std::memory_order_relaxed); }
    // Tap to hold on the grab (setting grab_tap_to_hold): off, the grab simply follows the grip button. Live.
    void SetGrabTapToHold(bool on) { m_grabTapToHold.store(on, std::memory_order_relaxed); }
    // How far the headset's tracking of this hand is trusted now (TrackingTrust.h), set every frame.
    void SetTrackingTrust(double trust) { m_trust.store(trust, std::memory_order_relaxed); }
    // The headset's position (raw tracking space), each frame before Update: the pinky pinch wants the palm toward
    // the face. valid false: unknown, and the pinky pinch stays off.
    void SetHeadPosition(bool valid, const Vec3& p) { m_headValid = valid; m_head = p; }

    bool ImuFused() const { return m_imuFused.load(std::memory_order_relaxed); }

    const std::string& Serial() const { return m_cfg.serial; }
    uint32_t ObjectId() const { return m_objectId; }
    uint8_t Mode() const { return m_mode; }
    bool Following() const { return m_follow.load(std::memory_order_relaxed); }
    uint32_t AppliedSeq() const { return m_appliedSeq; }
    vr::VRInputComponentHandle_t HapticHandle() const { return m_haptic; }

private:
    bool Right() const { return m_hand == 1; }
    Xform LinkToOurs();                   // tap source's /pose/raw → ours
    void PassthroughPose(const TapHandSnapshot& tap, const CyberFingerState& cyberFinger, double now,
                         Xform& raw, Vec3& lin, Vec3& ang, vr::VRBoneTransform_t* bones, float* curls);
    void CyberFingerCurls(const CyberFingerState& cyberFinger, bool fresh, float curls[5]) const;
    void SubmitInputs(const CyberFingerState& cyberFinger, bool cyberFingerFresh, bool live, const TapHandSnapshot& tap,
                      const float curls[5], const HandGestures& gestures, double now);
    void SubmitSkeleton(const vr::VRBoneTransform_t* bones);

    int       m_hand;
    Config    m_cfg;
    uint32_t  m_objectId = vr::k_unTrackedDeviceIndexInvalid;
    vr::PropertyContainerHandle_t m_container = vr::k_ulInvalidPropertyContainer;
    vr::DriverPose_t m_pose{};

    Xform     m_synthWrist;               // our raw → wrist bone (Index convention)
    std::mutex m_wristLock;               // m_linkWrist, m_haveLinkWrist: also used on the republish thread
    Xform     m_linkWrist;                // tap source's raw → wrist, last seen
    bool      m_haveLinkWrist = false;

    // Event-driven PASSTHROUGH (see OnTapPose / OnTapSkeleton)
    std::atomic<bool>     m_follow{ false };
    std::atomic<double>   m_lastTapPose{ -1e9 };
    std::atomic<double>   m_lastTapSkeleton{ -1e9 };
    std::atomic<uint32_t> m_eventPoses{ 0 };
    std::atomic<uint32_t> m_eventSkeletons{ 0 };

    TapHold   m_grab;
    LongPress m_aButton;                  // A: a click, or held long, /input/a_hold
    std::atomic<bool> m_grabTapToHold{ true };
    bool      m_grabTapApplied = true;
    // The gestures as buttons: /input/pinky_pinch/click (hysteresis on the pinch), index_point/click,
    // two_finger_point/click
    bool      m_pinkyPinched = false;       // the click: a meant pinky pinch, held until the pinch opens
    bool      m_pinkyClosed = false;        // the raw pinky pinch (hysteresis), whatever the hand's shape
    bool      m_pinkyMeant = false;         // PinkyPinchMeant, this frame
    double    m_pinkyLogged = -1e9;
    double    m_twoPointLogged = -1e9;
    bool      m_twoPointWas = false;         // the two-finger point, as last logged
    bool      m_headValid = false;
    Vec3      m_head;
    GestureClick m_pinkyClick, m_pointClick, m_twoPointClick;

    std::mutex         m_filterLock;      // republish thread vs settings updates
    PoseFilter         m_filter;
    PoseFilter::Params m_filterParams;
    std::atomic<bool>  m_sourceFiltered{ false };   // SetSourceFiltered; off until the source is known
    bool               m_sourceFilterKnown = false; // frame loop only

    ImuFusion*         m_imu = nullptr;
    std::atomic<bool>  m_imuEnabled{ true };
    std::atomic<bool>  m_imuFused{ false };          // the last pose's orientation came from the IMU fusion
    std::atomic<double> m_trust{ 1.0 };              // SetTrackingTrust
    bool FusedOrientation(double now, Quat& q, Vec3& w);
    vr::VRBoneTransform_t m_prevSourceBones[eBone_Count]{};   // republish thread only
    std::atomic<double>   m_lastSkeletonChange{ -1e9 };

    uint8_t   m_mode = kModeNone;
    uint32_t  m_appliedSeq = 0;
    double    m_lastData = -1e9;
    double    m_lastTapBones = -1e9;      // frame loop: last time the snapshot had a live skeleton

    Xform     m_lastRaw;
    vr::VRBoneTransform_t m_lastBones[eBone_Count]{};
    double    m_blendStart = -1e9;
    Xform     m_blendFromRaw;
    vr::VRBoneTransform_t m_blendFromBones[eBone_Count]{};

    double    m_lastBatteryUpdate = -1e9;
    uint8_t   m_lastBattery = 255;

    enum Bool { kSystem, kA, kAHold, kB, kC, kD, kE, kTriggerClick, kGripClick, kGrab, kStickClick, kIndexPoint,
                kPinkyPinchClick, kIndexPointClick, kTwoFingerPoint, kPink, kNumBool };
    enum Scalar { kTrigger, kGrip, kStickX, kStickY, kFingerIndex, kFingerMiddle, kFingerRing, kFingerPinky,
                  kIndexPinch, kMiddlePinch, kRingPinch, kPinkyPinch, kGrasp, kNumScalar };
    vr::VRInputComponentHandle_t m_bool[kNumBool] = {};
    vr::VRInputComponentHandle_t m_scalar[kNumScalar] = {};
    vr::VRInputComponentHandle_t m_skeleton = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_haptic = vr::k_ulInvalidInputComponentHandle;
};

} // namespace cf
