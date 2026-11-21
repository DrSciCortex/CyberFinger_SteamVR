/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// CyberFingerController.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "CyberFingerController.h"
#include "SkeletonSynth.h"
#include "NativeInput.h"
#include "Utils.h"
#include <algorithm>
#include <cstring>

namespace cf {

namespace {

constexpr double kBlendTime = 0.1;        // s, FUSED <-> PASSTHROUGH cross-fade
constexpr double kCyberFingerFresh = 0.25;      // s
constexpr double kTapSkeletonHold = 1.0;  // s to hold the last tap skeleton when it stalls
constexpr double kEventTimeout = 0.1;     // s without headset hand updates before the frame loop takes over
constexpr double kOcclusion = 0.08;       // s of an unchanged source skeleton: the headset lost the hand (normal pauses reach ~60 ms)

const char* ModeName(uint8_t m) {
    switch (m) {
        case kModeFused: return "FUSED";
        case kModePassthrough: return "PASSTHROUGH";
        case kModeNoPose: return "NO_POSE";
        case kModeReleased: return "RELEASED";
        default: return "NONE";
    }
}

} // namespace

CyberFingerController::CyberFingerController(int hand, const Config& cfg)
    : m_hand(hand), m_cfg(cfg), m_synthWrist(SynthWristBone(hand == 1)), m_grab(cfg.grabTapTime),
      m_aButton(cfg.blackHoldTime) {
    m_pose.qWorldFromDriverRotation.w = 1;
    m_pose.qDriverFromHeadRotation.w = 1;
    m_pose.qRotation.w = 1;
    m_pose.deviceIsConnected = true;
    m_pose.poseIsValid = false;
    m_pose.result = vr::TrackingResult_Running_OutOfRange;

    const float zero[5] = {};
    SynthesizeSkeleton(Right(), zero, zero, m_lastBones);
}

vr::EVRInitError CyberFingerController::Activate(uint32_t objectId) {
    m_objectId = objectId;
    auto* props = vr::VRProperties();
    m_container = props->TrackedDeviceToPropertyContainer(objectId);
    const auto c = m_container;
    const bool right = Right();

    props->SetStringProperty(c, vr::Prop_SerialNumber_String, m_cfg.serial.c_str());
    props->SetStringProperty(c, vr::Prop_ModelNumber_String, "CyberFinger");
    props->SetStringProperty(c, vr::Prop_ManufacturerName_String, "SciCortex");
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String, "cyberfinger");
    props->SetStringProperty(c, vr::Prop_ControllerType_String, "cyberfinger");
    props->SetStringProperty(c, vr::Prop_ResourceRoot_String, "cyberfinger");
    props->SetStringProperty(c, vr::Prop_InputProfilePath_String, "{cyberfinger}/input/cyberfinger_profile.json");
    props->SetStringProperty(c, vr::Prop_RenderModelName_String,
                             right ? "{cyberfinger}cyberfinger_right" : "{cyberfinger}cyberfinger_left");
    props->SetStringProperty(c, vr::Prop_RegisteredDeviceType_String, ("cyberfinger/" + m_cfg.serial).c_str());
    props->SetInt32Property(c, vr::Prop_ControllerRoleHint_Int32,
                            right ? vr::TrackedControllerRole_RightHand : vr::TrackedControllerRole_LeftHand);
    // Steam Link's hand devices declare -1: any positive priority takes the hand roles.
    props->SetInt32Property(c, vr::Prop_ControllerHandSelectionPriority_Int32, m_cfg.handPriority);
    props->SetBoolProperty(c, vr::Prop_DeviceIsWireless_Bool, true);
    props->SetBoolProperty(c, vr::Prop_DeviceProvidesBatteryStatus_Bool, true);
    props->SetFloatProperty(c, vr::Prop_DeviceBatteryPercentage_Float, 1.f);
    props->SetUint64Property(c, vr::Prop_HardwareRevision_Uint64, 2);
    props->SetUint64Property(c, vr::Prop_FirmwareVersion_Uint64, 2);
    // Status icons (tools/make_status_icons.py): grey until the CyberFinger tracks, blue while it does.
    const std::string icon = std::string("{cyberfinger}/icons/cyberfinger_") + (right ? "right" : "left");
    const std::string iconOff = icon + "_off.png", iconReady = icon + "_ready.png";
    for (const auto prop : { vr::Prop_NamedIconPathDeviceOff_String, vr::Prop_NamedIconPathDeviceSearching_String,
                             vr::Prop_NamedIconPathDeviceSearchingAlert_String, vr::Prop_NamedIconPathDeviceNotReady_String,
                             vr::Prop_NamedIconPathDeviceStandby_String, vr::Prop_NamedIconPathDeviceStandbyAlert_String })
        props->SetStringProperty(c, prop, iconOff.c_str());
    for (const auto prop : { vr::Prop_NamedIconPathDeviceReady_String, vr::Prop_NamedIconPathDeviceReadyAlert_String,
                             vr::Prop_NamedIconPathDeviceAlertLow_String })
        props->SetStringProperty(c, prop, iconReady.c_str());

    auto* in = vr::VRDriverInput();
    in->CreateBooleanComponent(c, "/input/system/click", &m_bool[kSystem]);
    // The pink buttons, a plain input for bindings: the left one toggles the dashboard through the dashboard's binding
    // (vrcompositor_cyberfinger.json, so it can be rebound), the right one is apps' (mute), which the bridge passes on
    // only in its SteamVR mode (otherwise it mutes the microphone or fires a FluxAction itself).
    in->CreateBooleanComponent(c, "/input/pink/click", &m_bool[kPink]);
    in->CreateBooleanComponent(c, "/input/a/click", &m_bool[kA]);
    in->CreateBooleanComponent(c, "/input/a_hold/click", &m_bool[kAHold]);   // A's long press (LongPress.h)
    in->CreateBooleanComponent(c, "/input/b/click", &m_bool[kB]);
    in->CreateBooleanComponent(c, "/input/c/click", &m_bool[kC]);
    in->CreateBooleanComponent(c, "/input/d/click", &m_bool[kD]);
    in->CreateBooleanComponent(c, "/input/e/click", &m_bool[kE]);
    in->CreateBooleanComponent(c, "/input/trigger/click", &m_bool[kTriggerClick]);
    in->CreateBooleanComponent(c, "/input/grip/click", &m_bool[kGripClick]);
    in->CreateBooleanComponent(c, "/input/grab/click", &m_bool[kGrab]);   // grip, tap to hold (TapHold.h)
    in->CreateBooleanComponent(c, "/input/thumbstick/click", &m_bool[kStickClick]);
    const auto one = vr::VRScalarUnits_NormalizedOneSided, two = vr::VRScalarUnits_NormalizedTwoSided;
    in->CreateScalarComponent(c, "/input/trigger/value", &m_scalar[kTrigger], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/grip/value", &m_scalar[kGrip], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/thumbstick/x", &m_scalar[kStickX], vr::VRScalarType_Absolute, two);
    in->CreateScalarComponent(c, "/input/thumbstick/y", &m_scalar[kStickY], vr::VRScalarType_Absolute, two);
    in->CreateScalarComponent(c, "/input/finger/index", &m_scalar[kFingerIndex], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/finger/middle", &m_scalar[kFingerMiddle], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/finger/ring", &m_scalar[kFingerRing], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/finger/pinky", &m_scalar[kFingerPinky], vr::VRScalarType_Absolute, one);
    // The standard hand-tracking gestures (no app action bound by default), and three of them as buttons, for Flux
    // Actions in Resonite: the pinky pinch, the index point, and the two-finger point (index and middle).
    in->CreateScalarComponent(c, "/input/index_pinch/value", &m_scalar[kIndexPinch], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/middle_pinch/value", &m_scalar[kMiddlePinch], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/ring_pinch/value", &m_scalar[kRingPinch], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/pinky_pinch/value", &m_scalar[kPinkyPinch], vr::VRScalarType_Absolute, one);
    in->CreateScalarComponent(c, "/input/grasp/value", &m_scalar[kGrasp], vr::VRScalarType_Absolute, one);
    in->CreateBooleanComponent(c, "/input/index_point/touch", &m_bool[kIndexPoint]);
    in->CreateBooleanComponent(c, "/input/pinky_pinch/click", &m_bool[kPinkyPinchClick]);
    in->CreateBooleanComponent(c, "/input/index_point/click", &m_bool[kIndexPointClick]);
    in->CreateBooleanComponent(c, "/input/two_finger_point/click", &m_bool[kTwoFingerPoint]);
    in->CreateHapticComponent(c, "/output/haptic", &m_haptic);

    const vr::EVRInputError err = in->CreateSkeletonComponent(
        c, right ? "/input/skeleton/right" : "/input/skeleton/left",
        right ? "/skeleton/hand/right" : "/skeleton/hand/left",
        "/pose/raw", m_cfg.trackingLevel, nullptr, 0, &m_skeleton);
    if (err != vr::VRInputError_None) {
        DriverLog("[%s] CreateSkeletonComponent failed (err %d)\n", m_cfg.serial.c_str(), int(err));
        m_skeleton = vr::k_ulInvalidInputComponentHandle;
    }
    SubmitSkeleton(m_lastBones);   // SteamVR expects skeleton data right away

    DriverLog("[%s] activated as device %u (%s hand, skeletal tracking level %d, hand priority %d)\n",
              m_cfg.serial.c_str(), objectId, right ? "right" : "left", int(m_cfg.trackingLevel), m_cfg.handPriority);
    return vr::VRInitError_None;
}

void CyberFingerController::Deactivate() {
    DriverLog("[%s] deactivated\n", m_cfg.serial.c_str());
    m_follow.store(false, std::memory_order_release);
    m_objectId = vr::k_unTrackedDeviceIndexInvalid;
}

// ── fallback skeleton from the CyberFinger's buttons ─────────────────────────────

void CyberFingerController::CyberFingerCurls(const CyberFingerState& g, bool fresh, float curls[5]) const {
    std::fill(curls, curls + 5, 0.f);
    if (!fresh) return;
    const bool trigger = (g.buttons & kBtnTrigger) != 0;
    curls[1] = g.triggerAnalog ? std::max(g.trigger, trigger ? 1.f : 0.f) : (trigger ? 1.f : 0.f);
    const float grip = (g.buttons & kBtnGrip) ? 1.f : 0.f;
    curls[2] = curls[3] = curls[4] = grip;
    const bool thumbBusy = (g.buttons & (kBtnC | kBtnD | kBtnE | kBtnMenu | kBtnStickClick | kBtnStartSelect)) != 0 ||
                           std::fabs(g.joyX) > 0.05f || std::fabs(g.joyY) > 0.05f;
    curls[0] = thumbBusy ? 0.7f : 0.f;
}

// ── headset hand tracking, re-rooted onto our /pose/raw convention ─────────

// raw_ours · wrist_ours = raw_link · wrist_link: the wrist stays where the headset put it.
Xform CyberFingerController::LinkToOurs() {
    std::lock_guard<std::mutex> g(m_wristLock);
    return m_haveLinkWrist ? m_linkWrist * Inverse(m_synthWrist) : m_cfg.noSkeletonOffset;
}

// Republished as submitted, with the source's timing (poseTimeOffset) and velocities: apps see the
// headset hand's own pose stream, not a per-frame resampling of it.
void CyberFingerController::OnTapPose(const vr::DriverPose_t& pose) {
    const double now = NowSeconds();
    m_lastTapPose.store(now, std::memory_order_relaxed);
    // Occlusion: the source resends its last skeleton unchanged (Steam Link) or stops sending it (Virtual
    // Desktop), or dates the pose well in the past, while it extrapolates a hand it no longer sees.
    const bool unseen = now - m_lastSkeletonChange.load(std::memory_order_relaxed) > kOcclusion ||
                        pose.poseTimeOffset < -0.05;
    if (m_imu) m_imu->Observe(now, FromHmdQuat(pose.qRotation), pose.poseIsValid && !unseen,
                              m_trust.load(std::memory_order_relaxed));
    if (!m_follow.load(std::memory_order_acquire)) return;
    const uint32_t id = m_objectId;
    if (id == vr::k_unTrackedDeviceIndexInvalid) return;
    vr::DriverPose_t src = pose;
    double rotPrediction;
    {
        // Steam Link's velocities are noisy and its poses already predicted: filter the pose, publish our
        // own velocities for SteamVR's extrapolation, and date it now (both hands alike). Sources that don't
        // need it (Virtual Desktop) pass through untouched.
        std::lock_guard<std::mutex> g(m_filterLock);
        rotPrediction = m_filterParams.rotPrediction;
        if (m_filterParams.enabled && m_sourceFiltered.load(std::memory_order_acquire) && pose.poseIsValid) {
            Vec3 pos = FromArray(pose.vecPosition), v, w;
            Quat q = FromHmdQuat(pose.qRotation);
            // While the hand is unseen, hold the last good pose, motionless.
            if (!(unseen && m_filter.Current(pos, q))) m_filter.Filter(pos, q, v, w, now, m_filterParams);
            ToArray(pos, src.vecPosition);
            src.qRotation = ToHmdQuat(q);
            ToArray(v, src.vecVelocity);
            ToArray(w, src.vecAngularVelocity);
            src.vecAcceleration[0] = src.vecAcceleration[1] = src.vecAcceleration[2] = 0;
            src.vecAngularAcceleration[0] = src.vecAngularAcceleration[1] = src.vecAngularAcceleration[2] = 0;
            src.poseTimeOffset = 0;
        }
    }
    // The orientation from the CyberFinger's joint IMU (the headset's absolute orientation, the IMU's motion and
    // timing): also through occlusions, where the pose filter holds the position.
    Quat qf;
    Vec3 wf;
    if (pose.poseIsValid && FusedOrientation(now, qf, wf)) {
        src.qRotation = ToHmdQuat(qf);
        ToArray(wf * rotPrediction, src.vecAngularVelocity);
        src.vecAngularAcceleration[0] = src.vecAngularAcceleration[1] = src.vecAngularAcceleration[2] = 0;
    }
    vr::DriverPose_t p = OffsetDriverPose(src, LinkToOurs());
    p.shouldApplyHeadModel = false;
    p.poseIsValid = pose.poseIsValid && pose.deviceIsConnected;
    p.deviceIsConnected = true;
    vr::VRServerDriverHost()->TrackedDevicePoseUpdated(id, p, sizeof(p));
    m_eventPoses.fetch_add(1, std::memory_order_relaxed);
}

void CyberFingerController::OnTapSkeleton(vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones,
                                          uint32_t count) {
    // Both of our motion ranges follow the source's WithoutController pose: nothing is held.
    if (range != vr::VRSkeletalMotionRange_WithoutController || !bones || count < uint32_t(eBone_Count)) return;
    const Xform linkWrist = SourceWrist(bones);
    {
        std::lock_guard<std::mutex> g(m_wristLock);
        m_linkWrist = linkWrist;
        m_haveLinkWrist = true;
    }
    const double now = NowSeconds();
    m_lastTapSkeleton.store(now, std::memory_order_relaxed);
    // Real tracking never repeats bit for bit: an identical skeleton means the source lost the hand.
    if (std::memcmp(m_prevSourceBones, bones, sizeof(m_prevSourceBones)) != 0) {
        std::memcpy(m_prevSourceBones, bones, sizeof(m_prevSourceBones));
        m_lastSkeletonChange.store(now, std::memory_order_relaxed);
    }
    if (!m_follow.load(std::memory_order_acquire)) return;
    vr::VRBoneTransform_t out[eBone_Count];
    ReRootBones(bones, linkWrist, m_synthWrist, out);
    SubmitSkeleton(out);
    m_eventSkeletons.fetch_add(1, std::memory_order_relaxed);
}

bool CyberFingerController::FusedOrientation(double now, Quat& q, Vec3& w) {
    const bool ok = m_imu && m_imuEnabled.load(std::memory_order_relaxed) && m_imu->Orientation(now, q, w);
    m_imuFused.store(ok, std::memory_order_relaxed);
    return ok;
}

void CyberFingerController::SetPoseFilter(const PoseFilter::Params& params) {
    std::lock_guard<std::mutex> g(m_filterLock);
    if (params.enabled != m_filterParams.enabled) m_filter.Reset();
    m_filterParams = params;
}

void CyberFingerController::SetSourceFiltered(bool filtered, const std::string& sourceType) {
    if (sourceType.empty()) return;                  // not resolved yet: keep the current choice
    if (m_sourceFilterKnown && filtered == m_sourceFiltered.load(std::memory_order_relaxed)) return;
    m_sourceFilterKnown = true;
    if (filtered) {
        std::lock_guard<std::mutex> g(m_filterLock);
        m_filter.Reset();                            // start fresh on the new source
    }
    m_sourceFiltered.store(filtered, std::memory_order_release);
    DriverLog("[%s] headset hand source %s: pose filter %s\n", m_cfg.serial.c_str(), sourceType.c_str(),
              filtered ? "applies (pose_filter_types)" : "off, poses pass through as streamed");
}

void CyberFingerController::TakeEventRates(double seconds, double& poseRate, double& skeletonRate) {
    const double s = seconds > 0 ? seconds : 1.0;
    poseRate = m_eventPoses.exchange(0, std::memory_order_relaxed) / s;
    skeletonRate = m_eventSkeletons.exchange(0, std::memory_order_relaxed) / s;
}

// The frame loop's version, from the per-frame snapshot: used during cross-fades, when the headset
// hand's updates stop arriving, and to keep m_lastRaw / m_lastBones current for the next cross-fade.
void CyberFingerController::PassthroughPose(const TapHandSnapshot& tap, const CyberFingerState& cyberFinger, double now,
                                            Xform& raw, Vec3& lin, Vec3& ang,
                                            vr::VRBoneTransform_t* bones, float* curls) {
    const bool tapSkeleton = tap.skeletonValid && tap.boneCount >= uint32_t(eBone_Count);
    const Xform linkWrist = tapSkeleton ? SourceWrist(tap.bones) : Xform{};
    if (tapSkeleton) {
        m_lastTapBones = now;
        if (now - m_lastTapSkeleton.load(std::memory_order_relaxed) > kEventTimeout) {
            std::lock_guard<std::mutex> g(m_wristLock);   // the event path keeps it fresher when it runs
            m_linkWrist = linkWrist;
            m_haveLinkWrist = true;
        }
    }

    raw = tap.rawPose * LinkToOurs();
    ang = tap.angVel;
    lin = tap.linVel + Cross(tap.angVel, raw.p - tap.rawPose.p);

    // The IMU fusion, as in OnTapPose: fed here while the source's events don't arrive (e.g. Virtual Desktop
    // keeping a lost hand's pose), and used whenever calibrated.
    if (m_imu && now - m_lastTapPose.load(std::memory_order_relaxed) > kEventTimeout)
        m_imu->Observe(now, tap.rawPose.q,
                       tap.poseValid && now - m_lastSkeletonChange.load(std::memory_order_relaxed) < kOcclusion,
                       m_trust.load(std::memory_order_relaxed));
    Quat qf;
    Vec3 wf;
    if (FusedOrientation(now, qf, wf)) {
        raw = Xform{ qf, tap.rawPose.p } * LinkToOurs();
        std::lock_guard<std::mutex> g(m_filterLock);
        ang = wf * m_filterParams.rotPrediction;
    }

    if (tapSkeleton) {
        ReRootBones(tap.bones, linkWrist, m_synthWrist, bones);
        CurlsFromBones(bones, curls);
    } else if (now - m_lastTapBones < kTapSkeletonHold) {
        std::memcpy(bones, m_lastBones, sizeof(vr::VRBoneTransform_t) * eBone_Count);
        CurlsFromBones(bones, curls);
    } else {
        const float zero[5] = {};
        CyberFingerCurls(cyberFinger, now - cyberFinger.time < kCyberFingerFresh && cyberFinger.valid, curls);
        SynthesizeSkeleton(Right(), curls, zero, bones);
    }
}

// ── per-frame update ───────────────────────────────────────────────────────

void CyberFingerController::Update(const CyberFingerState& cyberFinger, const HandStateSample& fused,
                                   const TapHandSnapshot& tap, double now, bool active) {
    if (m_objectId == vr::k_unTrackedDeviceIndexInvalid) return;

    const bool cyberFingerFresh = cyberFinger.valid && now - cyberFinger.time < kCyberFingerFresh;
    const bool fusedFresh = fused.valid && now - fused.arrival < m_cfg.fusedTimeout &&
                            (fused.pkt.h.flags & kHsPoseValid) != 0;

    uint8_t mode = fusedFresh ? kModeFused : (tap.poseValid ? kModePassthrough : kModeNoPose);
    if (fusedFresh || tap.poseValid || cyberFingerFresh) m_lastData = now;
    if (m_cfg.disconnectAfter > 0 && now - m_lastData > m_cfg.disconnectAfter) mode = kModeReleased;
    if (!active) mode = kModeReleased;

    Xform raw = m_lastRaw;
    Vec3 lin, ang;
    double timeOffset = 0;
    bool poseValid = true;
    vr::VRBoneTransform_t bones[eBone_Count];
    float curls[5] = {};

    if (mode == kModeFused) {
        const HandStatePacket& p = fused.pkt;
        raw = Xform{ Normalize({ p.raw_rot[0], p.raw_rot[1], p.raw_rot[2], p.raw_rot[3] }),
                     { p.raw_pos[0], p.raw_pos[1], p.raw_pos[2] } };
        lin = { p.lin_vel[0], p.lin_vel[1], p.lin_vel[2] };
        ang = { p.ang_vel[0], p.ang_vel[1], p.ang_vel[2] };
        timeOffset = -(p.h.age_us * 1e-6 + (now - fused.arrival));
        std::copy(p.curl, p.curl + 5, curls);
        if (p.h.flags & kHsHasBones) {
            static_assert(sizeof(Bone) == sizeof(vr::VRBoneTransform_t), "bone layout");
            std::memcpy(bones, p.bones, sizeof(bones));
        } else {
            SynthesizeSkeleton(Right(), p.curl, p.splay, bones);
        }
        m_appliedSeq = p.h.seq;
    } else if (mode == kModePassthrough) {
        PassthroughPose(tap, cyberFinger, now, raw, lin, ang, bones, curls);
    } else {
        poseValid = false;
        const float zero[5] = {};
        CyberFingerCurls(cyberFinger, cyberFingerFresh, curls);
        SynthesizeSkeleton(Right(), curls, zero, bones);
    }

    // Cross-fade when the source changes between two tracked modes.
    if (mode != m_mode) {
        const bool tracked = (mode == kModeFused || mode == kModePassthrough);
        const bool wasTracked = (m_mode == kModeFused || m_mode == kModePassthrough);
        if (tracked && wasTracked) {
            m_blendStart = now;
            m_blendFromRaw = m_lastRaw;
            std::memcpy(m_blendFromBones, m_lastBones, sizeof(m_blendFromBones));
        }
        DriverLog("[%s] mode %s -> %s\n", m_cfg.serial.c_str(), ModeName(m_mode), ModeName(mode));
        m_mode = mode;
    }
    const double t = (now - m_blendStart) / kBlendTime;
    const bool blending = poseValid && t >= 0 && t < 1;
    if (blending) {
        raw = Blend(m_blendFromRaw, raw, t);
        for (int b = 0; b < eBone_Count; ++b)
            bones[b] = XformToBone(Blend(BoneToXform(m_blendFromBones[b]), BoneToXform(bones[b]), t));
    }
    if (poseValid) m_lastRaw = raw;
    std::memcpy(m_lastBones, bones, sizeof(bones));

    // PASSTHROUGH goes out event by event (OnTapPose / OnTapSkeleton) while the headset hand's
    // updates keep coming; this loop submits pose and skeleton otherwise.
    const bool eventsLive = now - m_lastTapPose.load(std::memory_order_relaxed) < kEventTimeout &&
                            now - m_lastTapSkeleton.load(std::memory_order_relaxed) < kEventTimeout;
    const bool follow = mode == kModePassthrough && !blending && eventsLive;
    if (follow != m_follow.load(std::memory_order_relaxed))
        DriverLog("[%s] %s\n", m_cfg.serial.c_str(),
                  follow ? "republishing the headset hand's updates as they arrive"
                         : "pose and skeleton from the frame loop");
    m_follow.store(follow, std::memory_order_release);

    // ── pose ──
    m_pose.poseTimeOffset = timeOffset;
    m_pose.vecPosition[0] = raw.p.x;
    m_pose.vecPosition[1] = raw.p.y;
    m_pose.vecPosition[2] = raw.p.z;
    m_pose.qRotation = ToHmdQuat(raw.q);
    m_pose.vecVelocity[0] = poseValid ? lin.x : 0;
    m_pose.vecVelocity[1] = poseValid ? lin.y : 0;
    m_pose.vecVelocity[2] = poseValid ? lin.z : 0;
    m_pose.vecAngularVelocity[0] = poseValid ? ang.x : 0;
    m_pose.vecAngularVelocity[1] = poseValid ? ang.y : 0;
    m_pose.vecAngularVelocity[2] = poseValid ? ang.z : 0;
    m_pose.poseIsValid = poseValid;
    m_pose.result = poseValid ? vr::TrackingResult_Running_OK : vr::TrackingResult_Running_OutOfRange;
    m_pose.deviceIsConnected = (mode != kModeReleased);
    if (!follow) vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_objectId, m_pose, sizeof(vr::DriverPose_t));

    // Gestures: the headset's own pinches while it tracks the hand, else derived from our skeleton. The points come
    // from the skeleton as well: Steam Link declares an index point but wasn't seen setting it (2026-09-27), and has
    // no two-finger one.
    HandGestures gestures;
    const bool live = (mode != kModeReleased);
    if (!live) {
        // released: everything reads idle
    } else if (mode == kModePassthrough && tap.gesturesPresent) {
        for (int f = 0; f < 4; ++f) gestures.pinch[f] = tap.input[kTapIndexPinch + f];
        gestures.grasp = tap.input[kTapGrip];
        gestures.indexPoint = tap.input[kTapIndexPoint] > 0.5f;
        if (poseValid) {
            const HandGestures fromBones = GesturesFromBones(bones);
            gestures.indexPoint = gestures.indexPoint || fromBones.indexPoint;
            gestures.twoFingerPoint = fromBones.twoFingerPoint;
            gestures.twoFingerWhy = fromBones.twoFingerWhy;
            std::copy(fromBones.curls, fromBones.curls + 5, gestures.curls);
        }
    } else if (poseValid) {
        gestures = GesturesFromBones(bones);
    }

    // The two-finger point's start, and its near misses (index and middle fairly straight, the ring or pinky bending,
    // yet no point: TwoFingerPointShape says why), at most once a second, to tune it.
    const float* c = gestures.curls;
    const bool twoPoint = live && poseValid && gestures.twoFingerPoint;
    const bool nearMiss = live && poseValid && !twoPoint && gestures.twoFingerWhy && std::max(c[1], c[2]) < 0.5f &&
                          std::max(c[3], c[4]) > 0.35f && now - m_twoPointLogged > 1.0;
    if ((twoPoint && !m_twoPointWas) || nearMiss) {
        m_twoPointLogged = now;
        DriverLog("[%s] two-finger point %s: curls thumb %.2f index %.2f middle %.2f ring %.2f pinky %.2f\n",
                  m_cfg.serial.c_str(), twoPoint ? "counts" : gestures.twoFingerWhy, c[0], c[1], c[2], c[3], c[4]);
    }
    m_twoPointWas = twoPoint;

    // The pinky pinch as a button only when it's meant (PinkyPinchMeant): the palm toward the face, the other fingers
    // relaxed, the pinky alone at the thumb. Checked while the pinch is closed and not yet a click; each closing is
    // logged with what the hand looked like, to tune the thresholds.
    const bool wasClosed = m_pinkyClosed;
    m_pinkyClosed = live && gestures.pinch[3] >= (m_pinkyClosed ? 0.5f : 0.75f);
    m_pinkyMeant = false;
    if (m_pinkyClosed && poseValid) {
        const PinkyPinchShape shape = PinkyShape(Right(), bones, raw, m_headValid ? &m_head : nullptr);
        const char* why = nullptr;
        m_pinkyMeant = PinkyPinchMeant(shape, &why);
        if (!wasClosed && now - m_pinkyLogged > 0.3) {
            m_pinkyLogged = now;
            DriverLog("[%s] pinky pinch %s: palm %.0f deg from the face, curls index %.2f middle %.2f ring %.2f, "
                      "thumb to middle %.1f ring %.1f pinky %.1f cm\n", m_cfg.serial.c_str(),
                      m_pinkyMeant ? "counts" : why, shape.palmToHeadDeg, shape.curls[1], shape.curls[2],
                      shape.curls[3], shape.thumbTo[1] * 100, shape.thumbTo[2] * 100, shape.thumbTo[3] * 100);
        }
    }

    if (!follow) SubmitSkeleton(bones);
    SubmitInputs(cyberFinger, cyberFingerFresh, live, tap, curls, gestures, now);
    PublishNativeInput(m_hand, MakeNativeInput(m_objectId, mode, live, poseValid, cyberFingerFresh && live,
        cyberFinger, fused, tap, gestures, now, float(m_trust.load(std::memory_order_relaxed))));
}

void CyberFingerController::SubmitSkeleton(const vr::VRBoneTransform_t* bones) {
    if (m_skeleton == vr::k_ulInvalidInputComponentHandle) return;
    // Nothing is held in the hand, so both motion ranges get the same pose.
    auto* in = vr::VRDriverInput();
    in->UpdateSkeletonComponent(m_skeleton, vr::VRSkeletalMotionRange_WithoutController, bones, eBone_Count);
    in->UpdateSkeletonComponent(m_skeleton, vr::VRSkeletalMotionRange_WithController, bones, eBone_Count);
}

void CyberFingerController::SubmitInputs(const CyberFingerState& g, bool fresh, bool live, const TapHandSnapshot& tap,
                                         const float curls[5], const HandGestures& gestures, double now) {
    auto* in = vr::VRDriverInput();
    fresh = fresh && live;
    const uint8_t b = fresh ? g.buttons : 0;

    float trigger = (b & kBtnTrigger) ? 1.f : 0.f;
    if (fresh && g.triggerAnalog) trigger = std::max(g.trigger, trigger);

    in->UpdateScalarComponent(m_scalar[kTrigger], trigger, 0);
    in->UpdateBooleanComponent(m_bool[kTriggerClick], (b & kBtnTrigger) != 0 || trigger > 0.95f, 0);
    in->UpdateScalarComponent(m_scalar[kGrip], (b & kBtnGrip) ? 1.f : 0.f, 0);
    in->UpdateBooleanComponent(m_bool[kGripClick], (b & kBtnGrip) != 0, 0);
    bool grab = false;
    const bool tapToHold = m_grabTapToHold.load(std::memory_order_relaxed);
    if (tapToHold != m_grabTapApplied) {
        m_grabTapApplied = tapToHold;
        m_grab.SetTapTime(tapToHold ? m_cfg.grabTapTime : 0.0);
        if (!tapToHold) m_grab.Reset();                  // let go of a latched grab
    }
    if (fresh) grab = m_grab.Update((b & kBtnGrip) != 0, now);
    else m_grab.Reset();                     // button state unknown: let go
    in->UpdateBooleanComponent(m_bool[kGrab], grab, 0);
    in->UpdateScalarComponent(m_scalar[kStickX], fresh ? g.joyX : 0.f, 0);
    in->UpdateScalarComponent(m_scalar[kStickY], fresh ? g.joyY : 0.f, 0);
    in->UpdateBooleanComponent(m_bool[kStickClick], (b & kBtnStickClick) != 0, 0);
    // A (black button): a click, or held for blackHoldTime, /input/a_hold instead, for as long as it stays held
    // (Resonite: FluxAction1 left, FluxAction2 right). With the long press, A reports on release (LongPress.h).
    bool a = (b & m_cfg.maskA) != 0, aHold = false;
    if (m_cfg.blackHoldTime > 0) {
        bool fired = false;
        if (fresh) {
            a = m_aButton.Update(a, now, fired);
            aHold = m_aButton.Held();
        } else {
            m_aButton.Reset();
        }
    }
    in->UpdateBooleanComponent(m_bool[kA], a, 0);
    in->UpdateBooleanComponent(m_bool[kAHold], aHold, 0);
    in->UpdateBooleanComponent(m_bool[kB], (b & m_cfg.maskB) != 0, 0);
    in->UpdateBooleanComponent(m_bool[kC], (b & kBtnC) != 0, 0);
    in->UpdateBooleanComponent(m_bool[kD], (b & kBtnD) != 0, 0);
    in->UpdateBooleanComponent(m_bool[kE], (b & kBtnE) != 0, 0);
    // System: a CyberFinger button mapped to it (system_button), and the headset's palm-pinch gesture if forwarded (off by
    // default). The left pink button opens the dashboard as /input/pink, through the dashboard's binding.
    const bool system = (b & m_cfg.maskSystem) != 0 || (live && m_cfg.forwardTapSystem && tap.systemClick);
    in->UpdateBooleanComponent(m_bool[kSystem], system, 0);
    in->UpdateBooleanComponent(m_bool[kPink], fresh && (g.buttons2 & kBtn2Pink) != 0, 0);
    for (int f = 0; f < 4; ++f) {
        in->UpdateScalarComponent(m_scalar[kFingerIndex + f], curls[f + 1], 0);
        in->UpdateScalarComponent(m_scalar[kIndexPinch + f], gestures.pinch[f], 0);
    }
    in->UpdateScalarComponent(m_scalar[kGrasp], gestures.grasp, 0);
    in->UpdateBooleanComponent(m_bool[kIndexPoint], gestures.indexPoint, 0);
    m_pinkyPinched = m_pinkyClosed && (m_pinkyPinched || m_pinkyMeant);
    in->UpdateBooleanComponent(m_bool[kPinkyPinchClick], m_pinkyClick.Update(m_pinkyPinched, now), 0);
    in->UpdateBooleanComponent(m_bool[kIndexPointClick], m_pointClick.Update(live && gestures.indexPoint, now), 0);
    in->UpdateBooleanComponent(m_bool[kTwoFingerPoint], m_twoPointClick.Update(live && gestures.twoFingerPoint, now),
                               0);

    if (fresh && g.battery != m_lastBattery && now - m_lastBatteryUpdate > 10.0) {
        m_lastBattery = g.battery;
        m_lastBatteryUpdate = now;
        vr::VRProperties()->SetFloatProperty(m_container, vr::Prop_DeviceBatteryPercentage_Float,
                                             std::min(1.f, g.battery / 100.f));
    }
}

} // namespace cf
