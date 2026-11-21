/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CfHand.h — one CyberFinger hand inside WiVRn
//
// What src/CyberFingerController does for SteamVR, for an OpenXR runtime: while
// a CyberFinger is worn and the headset tracks the bare hand, WiVRn's controller
// device for that hand reports
//
//   inputs   the CyberFinger's stick, trigger and buttons, as the emulated
//            controller's (tap to hold on the grip, the black button's long
//            press); the inputs a controller has no place for, and three
//            hand gestures, as FluxActions for Resonite
//   poses    the headset's hand poses, the orientation taken from the
//            CyberFinger's joint IMU once the fusion is calibrated
//            (src/ImuFusion.h): the whole hand turns with the IMU about the
//            grip, joints included, and keeps its place through short losses
//   haptics  to the CyberFinger's motor, through the bridge
//
// No WiVRn or OpenXR types here (CfWivrn.cpp converts), so tests/ runs it
// without a runtime.
// ═══════════════════════════════════════════════════════════════════════════

#include <atomic>
#include <cstdint>
#include <mutex>
#include <vector>
#include "BoneData.h"
#include "CfConfig.h"
#include "GestureClick.h"
#include "ImuFusion.h"
#include "LongPress.h"
#include "MathUtil.h"
#include "Protocol.h"
#include "StudioLink.h"
#include "TapHold.h"
#include "VrTypes.h"

namespace cf {

constexpr int kNumJoints = 26;            // XR_HAND_JOINT_COUNT_EXT: palm, wrist, thumb 4, four fingers 5 each

// A pose in tracking space with what the runtime knows about it.
struct TrackedPose {
    bool  positionValid = false, orientationValid = false;
    bool  positionTracked = false, orientationTracked = false;
    bool  linearValid = false, angularValid = false;
    Xform pose;
    Vec3  linear, angular;                // m/s, rad/s

    bool Valid() const { return positionValid && orientationValid; }
};

// Which of a hand's poses (the hand joints go through AdjustJoints).
enum class PoseKind { Grip, Aim, Palm, Pinch, Poke, Count };

// The emulated controller's state, per hand.
struct ControllerInputs {
    float trigger = 0.f;
    bool  triggerClick = false;
    float squeeze = 0.f;                  // the grip button, or its tap-to-hold latch
    float stickX = 0.f, stickY = 0.f;     // -1..1, +y up
    bool  stickClick = false;
    bool  a = false;                      // lower face button (A right, X left)
    bool  b = false;                      // upper face button (B right, Y left)
    bool  menu = false;                   // the left controller's menu button (the right one's system button)
};

// A FluxAction turning on or off (MoreFluxActions: "FluxAction<n>.Pressed" / ".Released").
struct FluxEvent {
    int  action;
    bool pressed;
};

class Hand {
public:
    explicit Hand(int hand);

    void SetImuFusion(ImuFusion* fusion) { m_imu = fusion; }
    void SetConfig(const Config& cfg);

    // Whether the CyberFinger holds this hand (Driver::HoldsRole decides, per tracking packet).
    void SetActive(bool active) { m_active.store(active, std::memory_order_release); }
    bool Active() const { return m_active.load(std::memory_order_acquire); }

    // ── from the runtime's network thread ──
    // The headset's grip pose for time `target`, predicted at `produced` (the runtime's clock, s), as a tracking
    // packet brings it.
    void OnGrip(double target, double produced, double now, const TrackedPose& grip);
    void OnHead(const TrackedPose& head);
    // The hand joints: the wrist in tracking space and every joint relative to the wrist. active false: the
    // headset doesn't track the hand.
    void OnJoints(double now, bool active, const Xform& wrist, const Xform* jointsInWrist);

    // ── from the driver's own thread, every few ms ──
    // The buttons through their state machines, the gestures, and the optical observations whose time has come
    // into the IMU fusion. trust: TrackingTrust for this hand now. FluxActions that changed are appended to flux.
    void Tick(const CyberFingerState& cyberFinger, double trust, double now, std::vector<FluxEvent>& flux);

    // ── from application threads ──
    ControllerInputs Inputs() const;
    // A pose of this hand the runtime is about to hand out for time `target`, and the headset's grip pose for the
    // same time. Left alone unless the CyberFinger holds the hand.
    void AdjustPose(PoseKind kind, double target, double now, TrackedPose& pose, const TrackedPose& grip);
    // The same for the hand joints' root (the wrist; the joints are relative to it).
    void AdjustJoints(double target, double now, TrackedPose& wrist, const TrackedPose& grip);

    // ── status, for the context stream and the log ──
    struct Snapshot {
        bool   gripValid = false;         // a tracked grip pose arrived within the last 100 ms
        TrackedPose grip;
        bool   headValid = false;
        Xform  head;
        bool   jointsValid = false;
        double jointsAge = 1e9;           // s
        vr::VRBoneTransform_t bones[eBone_Count]{};   // parent space, root at the grip (BonesFromJoints)
    };
    Snapshot Snap(double now) const;
    bool ImuFused() const { return m_imuFused.load(std::memory_order_relaxed); }
    uint8_t Mode(double now) const;       // cf::OutputMode

private:
    bool Right() const { return m_hand == 1; }
    bool Fused(double now, double target, Quat& q, Vec3& w);
    void FlushObservations(double now, double trust);
    Snapshot SnapLocked(double now) const;
    void UpdateGestures(const Snapshot& snap, bool live, double now, bool& pinky, bool& point, bool& twoPoint);

    const int m_hand;
    ImuFusion* m_imu = nullptr;
    std::atomic<bool> m_active{ false };
    std::atomic<bool> m_imuFused{ false };

    mutable std::mutex m_lock;            // everything below
    Config m_cfg;

    // inputs
    ControllerInputs m_inputs;
    TapHold   m_grab;
    LongPress m_aButton;
    bool      m_grabTapApplied = true;
    GestureClick m_pinkyClick, m_pointClick, m_twoPointClick;
    bool      m_pinkyPinched = false, m_pinkyClosed = false;
    double    m_pinkyLogged = -1e9, m_twoPointLogged = -1e9;
    bool      m_twoPointWas = false;
    enum Flux { kFluxAHold, kFluxC, kFluxD, kFluxE, kFluxTwoPoint, kFluxPinky, kFluxPoint, kFluxPink, kNumFlux };
    int       m_fluxOn[kNumFlux] = {};    // the action each input holds pressed (0: none)

    // the headset's tracking, as it arrives
    struct Observation { double t; Quat q; bool seen; };
    std::vector<Observation> m_pending;   // by time; those up to `now` go to the fusion
    double    m_lastGripPacket = -1e9;
    double    m_lastSeen = -1e9;
    double    m_nextGapNote = 0;
    double    m_minHorizon = 1e9, m_minHorizonPrev = 1e9;   // the shortest prediction this second, and the last
    double    m_horizonRoll = 0;
    TrackedPose m_grip;                   // the latest tracked one
    bool      m_headValid = false;
    Xform     m_head;
    double    m_jointsTime = -1e9;
    bool      m_jointsActive = false;
    Xform     m_wrist;
    Xform     m_joints[kNumJoints];

    // what the poses are rebuilt from
    Xform     m_rel[int(PoseKind::Count)];        // grip → pose, as last seen together
    bool      m_haveRel[int(PoseKind::Count)] = {};
    Xform     m_lastGrip;                 // the last tracked grip pose handed out
    double    m_lastGripTime = -1e9;
};

// The hand joints as a SteamVR-style bone array, so the skeleton code of src/ applies to them: bones 1..25 are
// joints 1..25 (wrist, thumb, index, middle, ring, little), each in its parent's space, the root at `raw` (the
// grip pose, tracking space). Positions are exact; orientations are OpenXR's joint axes, not SteamVR's, which the
// gesture code doesn't look at.
void BonesFromJoints(const Xform& raw, const Xform& wrist, const Xform jointsInWrist[kNumJoints],
                     vr::VRBoneTransform_t bones[eBone_Count]);

} // namespace cf
