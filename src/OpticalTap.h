/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// OpticalTap.h — the headset's own hand tracking, captured inside vrserver
//
// Streamers expose Quest hand tracking as controller devices: Steam Link as
// "VRLINKQ_Hand_Left" (type svl_hand_interaction_augmented), Virtual Desktop as
// "HANDL" (type vd_hand_controller). CyberFinger takes the hand roles
// from them, so apps never see those devices — but their data is what we build on:
//
//   pose      captured by hooking IVRServerDriverHost::TrackedDevicePoseUpdated,
//             and read per frame with GetRawTrackedDevicePoses
//   skeleton  captured by hooking IVRDriverInput::UpdateSkeletonComponent
//   gestures  captured by hooking Update{Boolean,Scalar}Component: the
//             standard hand-tracking set (pinches, grip, index point) and
//             the ≡ button (Quest left palm pinch → /input/system/click)
//
// The hooks only observe; every call is passed through unchanged. The source
// hand's pose and skeleton updates also go to a TapListener the moment they
// are submitted, so they can be republished with the source's own timing.
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>
#include "BoneData.h"
#include "MathUtil.h"
#include "Protocol.h"
#include "SpreadMeter.h"

namespace cf {

// Inputs of a headset hand device that CyberFinger mirrors.
enum TapInput : int {
    kTapSystem = 0,      // /input/system/click
    kTapIndexPoint,      // /input/index_point/touch
    kTapIndexPinch,      // /input/index_pinch/value
    kTapMiddlePinch,     // /input/middle_pinch/value
    kTapRingPinch,       // /input/ring_pinch/value
    kTapPinkyPinch,      // /input/pinky_pinch/value
    kTapGrip,            // /input/grip/value (whole-hand grasp)
    kTapInputCount
};

struct TapHandSnapshot {
    bool     sourceFound = false;     // a headset hand device is known for this hand
    bool     poseValid = false;
    uint8_t  trackingResult = 0;      // vr::ETrackingResult
    Xform    rawPose;                 // device /pose/raw, raw tracking space
    Vec3     linVel, angVel;
    bool     skeletonValid = false;   // bones received within the last second
    double   skeletonAge = 1e9;       // s
    uint32_t boneCount = 0;           // as submitted by the source
    bool     baseIsRaw = true;        // the source skeleton is rooted at /pose/raw
    vr::VRBoneTransform_t bones[eBone_Count]{};
    bool     systemClick = false;     // source device's /input/system/click
    bool     gesturesPresent = false; // the source device reports pinch/grip values
    float    input[kTapInputCount] = {};
    std::string serial;
    std::string controllerType;       // the source device's Prop_ControllerType (e.g. svl_hand_interaction_augmented)
};

// Receives the selected source hands' updates on the submitting driver's thread, possibly inside other
// drivers' hooks on the same functions: record them and return, never call back into SteamVR from here.
class TapListener {
public:
    virtual void OnTapPose(int hand, const vr::DriverPose_t& pose) = 0;
    virtual void OnTapSkeleton(int hand, vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones,
                               uint32_t count) = 0;

protected:
    ~TapListener() = default;
};

class OpticalTap {
public:
    OpticalTap();
    ~OpticalTap();

    // What identifies the headset hand devices: serial substrings per hand and
    // hand-tracking controller types ("a|b" lists). Our own serials are never
    // tapped; ownPriority is our hand-selection priority, to warn when a source
    // declares one as high.
    void Configure(const std::string& patternLeft, const std::string& patternRight, const std::string& handTypes,
                   const std::string& ownLeft, const std::string& ownRight, int32_t ownPriority);

    // Install the observation hooks. Must run in ServerProvider::Init, before
    // any device is added. Returns true when the hooks are active.
    bool InstallHooks(vr::IVRDriverContext* ctx);
    void RemoveHooks();
    bool HooksActive() const { return m_hooksActive; }
    bool PoseHookActive() const { return m_poseHookActive; }

    void SetListener(TapListener* listener) { m_listener.store(listener, std::memory_order_release); }

    // Once per frame, with that frame's raw poses.
    void Update(const vr::TrackedDevicePose_t* poses, uint32_t count);

    TapHandSnapshot Get(int hand) const;

    // Pose and skeleton (WithoutController) updates per second from a source hand since the last call, and how
    // many of them carried new data (a streamer may repeat its last sample between real tracking frames).
    struct Rates {
        double pose = 0, poseNew = 0, skeleton = 0, skeletonNew = 0;
    };
    Rates TakeRates(double seconds, int hand);

    // Tracking noise of each source hand since the last call, measured while the hand was held still
    // (SpreadMeter): wrist and fingertips in the world, fingertips relative to the wrist. Metres.
    struct Noise {
        double wrist = 0, tips = 0, fingers = 0;
        int    windows = 0;              // 1 s windows that counted
    };
    Noise TakeNoise(int hand);

    // Record every update of the source hands (poses, both skeleton motion ranges), and of our own devices
    // alongside, for `seconds`, then write them as CSV to `path` (tools/analyze_tap_capture.py). Frame loop only.
    void StartCapture(double seconds, const std::string& path);
    void PollCapture(double now);

    // Our device of each hand (its poses are captured as ours). Frame loop, every frame.
    void SetOwnDevice(int hand, uint32_t device) { m_ownDevice[hand].store(device, std::memory_order_relaxed); }

    bool Capturing() const { return m_capturing.load(std::memory_order_relaxed); }
    // A CyberFinger IMU packet (CFIM) that arrived at `arrival`: captured alongside, while capturing. Any thread.
    void CaptureImu(const ImuPacket& p, double arrival);
    // Another device's pose this frame (kind 7 = the headset, 8 = a body tracker, `id` its role code), captured
    // alongside while capturing, in the columns of a hand pose (kind 0). Frame loop.
    void CaptureDevicePose(uint8_t kind, uint8_t id, const vr::TrackedDevicePose_t& pose, double now);

    // While on, the hand-tracking sources' system button (the Quest palm gesture) is held back from SteamVR, which
    // would open the dashboard through the source's own binding; it's still read here (forward_tap_system_button).
    // Frame loop: on while CyberFinger is active.
    void SetBlockHandSystem(bool on) { m_blockHandSystem.store(on, std::memory_order_relaxed); }

    // ── called from the hook detours (any thread) ──────────────────────────
    void OnCreateInput(vr::PropertyContainerHandle_t c, const char* name, vr::VRInputComponentHandle_t h);
    bool OnUpdateInput(vr::VRInputComponentHandle_t h, float value);   // true: hold the update back from SteamVR
    void OnCreateSkeleton(vr::PropertyContainerHandle_t c, const char* name, const char* skeletonPath,
                          const char* basePosePath, vr::EVRSkeletalTrackingLevel level,
                          vr::VRInputComponentHandle_t h);
    void OnUpdateSkeleton(vr::VRInputComponentHandle_t h, vr::EVRSkeletalMotionRange range,
                          const vr::VRBoneTransform_t* bones, uint32_t count);
    void OnPoseUpdated(uint32_t device, const vr::DriverPose_t& pose, uint32_t size);

private:
    static constexpr int kMaxBones = 64;
    static constexpr int kMaxSkeletons = 32;
    static constexpr int kMaxInputs = 128;

    struct BoneBuffer {
        vr::VRBoneTransform_t bones[kMaxBones]{};
        uint32_t count = 0;
        double   time = -1e9;
    };

    struct SkeletonEntry {
        vr::VRInputComponentHandle_t handle = vr::k_ulInvalidInputComponentHandle;
        vr::PropertyContainerHandle_t container = vr::k_ulInvalidPropertyContainer;
        int  hand = -1;
        bool orphan = false;          // created before our hooks: no container known
        bool baseIsRaw = true;
        int  level = 0;               // vr::EVRSkeletalTrackingLevel
        std::mutex lock;              // guards everything below
        std::string serial;           // resolved lazily, with the two below
        std::string controllerType;
        bool handType = false;        // a hand-tracking controller type
        int32_t handPriority = 0;
        bool own = false;
        bool serialResolved = false;
        BoneBuffer without, with;     // per motion range
        double lastUpdate = -1e9;
        vr::TrackedDeviceIndex_t device = vr::k_unTrackedDeviceIndexInvalid;
    };

    struct InputEntry {
        vr::VRInputComponentHandle_t handle = vr::k_ulInvalidInputComponentHandle;
        vr::PropertyContainerHandle_t container = vr::k_ulInvalidPropertyContainer;
        int id = -1;                  // TapInput
        std::atomic<float> value{ 0.f };
        std::atomic<int> handSource{ -1 };   // its device is a hand-tracking source: -1 not known yet, 0 no, 1 yes
    };

    SkeletonEntry* FindSkeleton(vr::VRInputComponentHandle_t h);
    SkeletonEntry* AddSkeleton(vr::VRInputComponentHandle_t h, vr::PropertyContainerHandle_t c,
                               int hand, bool orphan, bool baseIsRaw, int level);
    void ResolveSerial(SkeletonEntry& e);
    bool MatchesPattern(const std::string& serial, int hand) const;
    bool IsHandType(const std::string& controllerType) const;
    bool IsOwn(const std::string& serial) const;
    vr::TrackedDeviceIndex_t DeviceForContainer(vr::PropertyContainerHandle_t c, uint32_t count) const;
    vr::TrackedDeviceIndex_t DeviceForPattern(int hand, uint32_t count) const;
    std::string StringOf(vr::PropertyContainerHandle_t c, vr::ETrackedDeviceProperty prop) const;
    std::string SerialOf(vr::PropertyContainerHandle_t c) const { return StringOf(c, vr::Prop_SerialNumber_String); }
    void MeasureNoise(int hand, const TapHandSnapshot& s, double now);

    std::vector<std::string> m_patterns[2];
    std::vector<std::string> m_handTypes;
    std::string m_own[2];
    int32_t m_ownPriority = 1000;
    struct NoiseMeters { SpreadMeter wrist, tips[5], fingers[5]; } m_noise[2];   // frame loop only
    bool m_hooksActive = false;
    bool m_poseHookActive = false;
    std::atomic<TapListener*> m_listener{ nullptr };

    SkeletonEntry m_skeletons[kMaxSkeletons];
    std::atomic<int> m_numSkeletons{ 0 };
    InputEntry m_inputs[kMaxInputs];
    std::atomic<int> m_numInputs{ 0 };
    std::mutex m_registerLock;        // serialises registry appends

    mutable std::mutex m_snapLock;
    TapHandSnapshot m_snap[2];
    // Chosen per hand in Update(), read by the hook threads.
    std::atomic<int> m_selected[2] = { -1, -1 };                   // skeleton entry
    std::atomic<uint32_t> m_sourceDevice[2] = { vr::k_unTrackedDeviceIndexInvalid,
                                                vr::k_unTrackedDeviceIndexInvalid };
    std::atomic<uint32_t> m_poseEvents[2] = { 0, 0 };
    std::atomic<uint32_t> m_skeletonEvents[2] = { 0, 0 };
    std::atomic<uint32_t> m_poseNew[2] = { 0, 0 };
    std::atomic<uint32_t> m_skeletonNew[2] = { 0, 0 };
    struct LastPose {
        std::mutex lock;
        double position[3] = {};
        vr::HmdQuaternion_t rotation{};
    } m_lastPose[2];

    // Capture (StartCapture): kind 0 = pose, 1 = skeleton WithoutController, 2 = skeleton WithController of the
    // source hand; 3, 4, 5 = the same of our device; 6 = CyberFinger IMU (CaptureImu); 7 = the headset, 8 = a body
    // tracker (CaptureDevicePose).
    struct CaptureRecord {
        double  t;
        uint8_t hand, kind, changed;
        float   v[22];
    };
    void Capture(const CaptureRecord& r);
    void CapturePose(int hand, uint8_t kind, bool changed, const vr::DriverPose_t& pose);
    void CaptureSkeleton(int hand, uint8_t kind, bool changed, const vr::VRBoneTransform_t* bones);
    std::atomic<uint32_t> m_ownDevice[2] = { vr::k_unTrackedDeviceIndexInvalid, vr::k_unTrackedDeviceIndexInvalid };
    std::atomic<bool> m_capturing{ false };
    std::atomic<bool> m_blockHandSystem{ false };   // SetBlockHandSystem
    double m_captureEnd = 0;
    std::string m_capturePath;
    std::mutex m_captureLock;
    std::vector<CaptureRecord> m_capture;
    vr::TrackedDeviceIndex_t m_patternDevice[2] = { vr::k_unTrackedDeviceIndexInvalid, vr::k_unTrackedDeviceIndexInvalid };
    double m_nextPatternSearch[2] = { 0, 0 };
};

} // namespace cf
