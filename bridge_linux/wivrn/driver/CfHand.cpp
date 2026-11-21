/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// CfHand.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "CfHand.h"
#include <algorithm>
#include <cmath>
#include "SkeletonSynth.h"
#include "Utils.h"

namespace cf {

namespace {

constexpr double kCyberFingerFresh = 0.25;   // s
constexpr double kSeenTimeout = 0.1;         // s without a tracked grip pose: the headset lost the hand
constexpr double kJointsTimeout = 0.25;      // s
constexpr double kGapAfter = 0.2;            // s without grip packets at all: tell the fusion the hand is unseen
constexpr double kHorizonSlack = 0.003;      // s
constexpr double kMaxPrediction = 0.05;      // s: how far the IMU's orientation is carried ahead
constexpr size_t kMaxPending = 256;

Quat FromRotationVector(const Vec3& r) {
    const double a = Length(r);
    if (a < 1e-12) return {};
    const double s = std::sin(a * 0.5) / a;
    return { std::cos(a * 0.5), r.x * s, r.y * s, r.z * s };
}

const char* HandName(int hand) { return hand ? "right" : "left"; }

} // namespace

void BonesFromJoints(const Xform& raw, const Xform& wrist, const Xform jointsInWrist[kNumJoints],
                     vr::VRBoneTransform_t bones[eBone_Count]) {
    bones[eBone_Root] = XformToBone({});
    bones[eBone_Wrist] = XformToBone(Inverse(raw) * wrist);
    for (int b = eBone_Thumb0; b <= eBone_PinkyFinger4; ++b) {
        const int parent = kBoneParent[b];
        bones[b] = XformToBone(parent == eBone_Wrist ? jointsInWrist[b]
                                                     : Inverse(jointsInWrist[parent]) * jointsInWrist[b]);
    }
    FillAuxBones(bones);
}

Hand::Hand(int hand) : m_hand(hand), m_grab(m_cfg.grabTapTime), m_aButton(m_cfg.blackHoldTime) {}

void Hand::SetConfig(const Config& cfg) {
    std::lock_guard<std::mutex> g(m_lock);
    if (cfg.blackHoldTime != m_cfg.blackHoldTime) m_aButton = LongPress(cfg.blackHoldTime);
    if (cfg.grabTapTime != m_cfg.grabTapTime) m_grabTapApplied = !cfg.grabTapToHold;   // applied in Tick
    m_cfg = cfg;
}

// ── the headset's tracking ─────────────────────────────────────────────────

// The headset sends each pose for several times ahead (what the applications ask for), each further prediction
// noisier than the last. The fusion and the status take the shortest horizon only, so they see one evenly
// predicted stream; each is observed when its time has come (Tick), not when it arrives.
void Hand::OnGrip(double target, double produced, double now, const TrackedPose& grip) {
    std::lock_guard<std::mutex> g(m_lock);
    m_lastGripPacket = now;
    const double horizon = target - produced;
    if (now >= m_horizonRoll) {
        m_horizonRoll = now + 1.0;
        m_minHorizonPrev = m_minHorizon;
        m_minHorizon = horizon;
    } else {
        m_minHorizon = std::min(m_minHorizon, horizon);
    }
    if (horizon > std::min(m_minHorizon, m_minHorizonPrev) + kHorizonSlack) return;

    const bool seen = grip.Valid() && grip.orientationTracked;
    if (seen) {
        m_lastSeen = now;
        m_grip = grip;
    } else if (!m_grip.orientationValid) {
        return;                                     // never seen yet: nothing to call lost
    }
    const Observation o{ target, seen ? grip.pose.q : m_grip.pose.q, seen };
    auto at = std::upper_bound(m_pending.begin(), m_pending.end(), o,
                               [](const Observation& a, const Observation& b) { return a.t < b.t; });
    if (at != m_pending.begin() && target - (at - 1)->t < 1e-3) {
        *(at - 1) = o;                              // the same moment again, predicted later: it replaces
    } else {
        m_pending.insert(at, o);
        if (m_pending.size() > kMaxPending) m_pending.erase(m_pending.begin());
    }
}

void Hand::OnHead(const TrackedPose& head) {
    std::lock_guard<std::mutex> g(m_lock);
    m_headValid = head.Valid();
    if (m_headValid) m_head = head.pose;
}

void Hand::OnJoints(double now, bool active, const Xform& wrist, const Xform* jointsInWrist) {
    std::lock_guard<std::mutex> g(m_lock);
    m_jointsTime = now;
    m_jointsActive = active && jointsInWrist;
    if (!m_jointsActive) return;
    m_wrist = wrist;
    std::copy(jointsInWrist, jointsInWrist + kNumJoints, m_joints);
}

void Hand::FlushObservations(double now, double trust) {
    size_t n = 0;
    for (; n < m_pending.size() && m_pending[n].t <= now; ++n)
        if (m_imu) m_imu->Observe(m_pending[n].t, m_pending[n].q, m_pending[n].seen, trust);
    m_pending.erase(m_pending.begin(), m_pending.begin() + std::ptrdiff_t(n));
    // No packets at all (the headset asleep, the stream stalled): the hand is unseen, as far as the fusion goes.
    if (m_imu && m_grip.orientationValid && now - m_lastGripPacket > kGapAfter && now >= m_nextGapNote) {
        m_nextGapNote = now + 0.1;
        m_imu->Observe(now, m_grip.pose.q, false, trust);
    }
}

Hand::Snapshot Hand::SnapLocked(double now) const {
    Snapshot s;
    s.grip = m_grip;
    s.gripValid = m_grip.Valid() && now - m_lastSeen < kSeenTimeout;
    s.headValid = m_headValid;
    s.head = m_head;
    s.jointsAge = now - m_jointsTime;
    s.jointsValid = m_jointsActive && s.jointsAge < kJointsTimeout;
    if (s.jointsValid) BonesFromJoints(s.gripValid ? m_grip.pose : m_wrist, m_wrist, m_joints, s.bones);
    return s;
}

Hand::Snapshot Hand::Snap(double now) const {
    std::lock_guard<std::mutex> g(m_lock);
    return SnapLocked(now);
}

uint8_t Hand::Mode(double now) const {
    std::lock_guard<std::mutex> g(m_lock);
    if (!m_cfg.active) return kModeNone;
    if (!Active()) return kModeReleased;
    return m_grip.Valid() && now - m_lastSeen < kSeenTimeout ? kModePassthrough : kModeNoPose;
}

// ── inputs ─────────────────────────────────────────────────────────────────

// The gestures as buttons, as src/CyberFingerController.cpp has them: the pinky pinch only when it's meant
// (PinkyPinchMeant) and with hysteresis, all three debounced (GestureClick.h).
void Hand::UpdateGestures(const Snapshot& snap, bool live, double now, bool& pinky, bool& point, bool& twoPoint) {
    const bool tracked = live && snap.gripValid && snap.jointsValid;
    HandGestures gestures;
    if (tracked) gestures = GesturesFromBones(snap.bones);

    // The two-finger point's start and, to tune it, its near misses at most once a second: those only with
    // gesture_log, a relaxed hand is one nearly all the time.
    const float* c = gestures.curls;
    const bool two = tracked && gestures.twoFingerPoint;
    const bool nearMiss = m_cfg.gestureLog && tracked && !two && gestures.twoFingerWhy &&
                          std::max(c[1], c[2]) < 0.5f && std::max(c[3], c[4]) > 0.35f &&
                          now - m_twoPointLogged > 1.0;
    if ((two && !m_twoPointWas) || nearMiss) {
        m_twoPointLogged = now;
        DriverLog("[%s] two-finger point %s: curls thumb %.2f index %.2f middle %.2f ring %.2f pinky %.2f\n",
                  HandName(m_hand), two ? "counts" : gestures.twoFingerWhy, c[0], c[1], c[2], c[3], c[4]);
    }
    m_twoPointWas = two;

    const bool wasClosed = m_pinkyClosed;
    m_pinkyClosed = tracked && gestures.pinch[3] >= (m_pinkyClosed ? 0.5f : 0.75f);
    bool meant = false;
    if (m_pinkyClosed) {
        const PinkyPinchShape shape = PinkyShape(Right(), snap.bones, snap.grip.pose,
                                                 snap.headValid ? &snap.head.p : nullptr);
        const char* why = nullptr;
        meant = PinkyPinchMeant(shape, &why);
        if (!wasClosed && now - m_pinkyLogged > 0.3) {
            m_pinkyLogged = now;
            DriverLog("[%s] pinky pinch %s: palm %.0f deg from the face, curls index %.2f middle %.2f ring %.2f, "
                      "thumb to middle %.1f ring %.1f pinky %.1f cm\n", HandName(m_hand),
                      meant ? "counts" : why, shape.palmToHeadDeg, shape.curls[1], shape.curls[2], shape.curls[3],
                      shape.thumbTo[1] * 100, shape.thumbTo[2] * 100, shape.thumbTo[3] * 100);
        }
    }
    m_pinkyPinched = m_pinkyClosed && (m_pinkyPinched || meant);
    pinky = m_pinkyClick.Update(m_pinkyPinched, now);
    point = m_pointClick.Update(tracked && gestures.indexPoint, now);
    twoPoint = m_twoPointClick.Update(two, now);
}

void Hand::Tick(const CyberFingerState& cyberFinger, double trust, double now, std::vector<FluxEvent>& flux) {
    std::lock_guard<std::mutex> g(m_lock);
    FlushObservations(now, trust);

    const bool live = Active();
    const bool fresh = live && cyberFinger.valid && now - cyberFinger.time < kCyberFingerFresh;
    const uint16_t b = !fresh ? 0
                              : uint16_t(cyberFinger.buttons | ((cyberFinger.buttons2 & kBtn2Pink) ? kMaskPink : 0));

    ControllerInputs in;
    in.trigger = (b & kBtnTrigger) ? 1.f : 0.f;
    if (fresh && cyberFinger.triggerAnalog) in.trigger = std::max(cyberFinger.trigger, in.trigger);
    in.triggerClick = (b & kBtnTrigger) != 0 || in.trigger > 0.95f;

    // The squeeze: the grip with tap to hold (TapHold.h), or the plain button.
    if (m_cfg.grabTapToHold != m_grabTapApplied) {
        m_grabTapApplied = m_cfg.grabTapToHold;
        m_grab.SetTapTime(m_cfg.grabTapToHold ? m_cfg.grabTapTime : 0.0);
        if (!m_cfg.grabTapToHold) m_grab.Reset();    // let go of a latched grab
    }
    bool grab = false;
    if (fresh) grab = m_grab.Update((b & kBtnGrip) != 0, now);
    else m_grab.Reset();                             // button state unknown: let go
    in.squeeze = grab ? 1.f : 0.f;

    in.stickX = fresh ? cyberFinger.joyX : 0.f;
    in.stickY = fresh ? cyberFinger.joyY : 0.f;
    in.stickClick = (b & kBtnStickClick) != 0;

    // A (the black button): a click, or held for blackHoldTime the long press instead, which the controller has
    // no input for: a FluxAction. With the long press, A reports on release (LongPress.h).
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
    in.a = a;
    in.b = (b & m_cfg.maskB) != 0;
    in.menu = (b & (Right() ? m_cfg.maskSystem : m_cfg.maskMenu)) != 0;
    m_inputs = in;

    // FluxActions: the long press, the buttons no controller input took, the gestures. Left hand n, right n + 1.
    const bool fluxOn = live && m_cfg.fluxPort > 0;
    const bool gesturesOn = fluxOn && (m_cfg.fluxTwoFingerPoint > 0 || m_cfg.fluxPinkyPinch > 0 ||
                                       m_cfg.fluxIndexPoint > 0);
    bool pinky = false, point = false, twoPoint = false;
    if (gesturesOn || m_pinkyClosed || m_twoPointWas)
        UpdateGestures(gesturesOn ? SnapLocked(now) : Snapshot{}, gesturesOn, now, pinky, point, twoPoint);
    const uint16_t free = uint16_t(b & ~(m_cfg.maskA | m_cfg.maskB | (Right() ? m_cfg.maskSystem : m_cfg.maskMenu)));
    const auto action = [&](int left, bool on) { return fluxOn && on && left > 0 ? left + m_hand : 0; };
    const int want[kNumFlux] = {
        action(m_cfg.fluxAHold, aHold),
        action(m_cfg.fluxC, (free & kBtnC) != 0),
        action(m_cfg.fluxD, (free & kBtnD) != 0),
        action(m_cfg.fluxE, (free & kBtnE) != 0),
        action(m_cfg.fluxTwoFingerPoint, twoPoint),
        action(m_cfg.fluxPinkyPinch, pinky),
        action(m_cfg.fluxIndexPoint, point),
        fluxOn && Right() && (free & kMaskPink) && m_cfg.fluxPinkRight > 0 ? m_cfg.fluxPinkRight : 0,
    };
    for (int i = 0; i < kNumFlux; ++i) {
        if (want[i] == m_fluxOn[i]) continue;
        if (m_fluxOn[i]) flux.push_back({ m_fluxOn[i], false });
        if (want[i]) flux.push_back({ want[i], true });
        m_fluxOn[i] = want[i];
    }
}

ControllerInputs Hand::Inputs() const {
    std::lock_guard<std::mutex> g(m_lock);
    return m_inputs;
}

// ── poses ──────────────────────────────────────────────────────────────────

bool Hand::Fused(double now, double target, Quat& q, Vec3& w) {
    const bool ok = m_imu && m_cfg.imuFusion && m_imu->Orientation(now, q, w);
    m_imuFused.store(ok, std::memory_order_relaxed);
    if (!ok) return false;
    w = w * m_cfg.rotationPrediction;
    q = Normalize(FromRotationVector(w * std::min(kMaxPrediction, std::max(0.0, target - now))) * q);
    return true;
}

// Every pose of the hand is rebuilt on the grip pose: grip · (grip⁻¹ · pose), with the grip's orientation the
// fused one. So the hand turns as one piece, about the grip, and the position stays the headset's. While the
// headset has lost the hand, the last position stands in and the IMU keeps turning it.
void Hand::AdjustPose(PoseKind kind, double target, double now, TrackedPose& pose, const TrackedPose& grip) {
    if (!Active()) return;
    std::lock_guard<std::mutex> g(m_lock);
    Quat qf;
    Vec3 w;
    const bool fused = Fused(now, target, qf, w);
    const int k = int(kind);
    // The controller's poses move with gripOffset; the pinch and poke poses belong to the hand.
    const bool onController = kind == PoseKind::Grip || kind == PoseKind::Aim || kind == PoseKind::Palm;
    const Xform offset = onController ? m_cfg.gripOffset : Xform{};

    if (grip.Valid() && pose.Valid()) {
        m_rel[k] = Inverse(grip.pose) * pose.pose;
        m_haveRel[k] = true;
        m_lastGrip = grip.pose;
        m_lastGripTime = now;
        Xform base = grip.pose;
        if (fused) base.q = qf;
        pose.pose = base * offset * m_rel[k];
        pose.pose.q = Normalize(pose.pose.q);
        if (fused) {
            pose.angular = w;
            pose.angularValid = true;
        }
    } else if (fused && m_haveRel[k] && now - m_lastGripTime < m_cfg.occlusionHold) {
        pose = TrackedPose{};
        pose.pose = Xform{ qf, m_lastGrip.p } * offset * m_rel[k];
        pose.pose.q = Normalize(pose.pose.q);
        pose.positionValid = pose.orientationValid = pose.orientationTracked = true;   // the position is a guess
        pose.linearValid = pose.angularValid = true;
        pose.angular = w;
    }
}

void Hand::AdjustJoints(double target, double now, TrackedPose& wrist, const TrackedPose& grip) {
    if (!Active()) return;
    std::lock_guard<std::mutex> g(m_lock);
    Quat qf;
    Vec3 w;
    if (!wrist.Valid() || !grip.Valid() || !Fused(now, target, qf, w)) return;
    wrist.pose = Xform{ qf, grip.pose.p } * (Inverse(grip.pose) * wrist.pose);
    wrist.pose.q = Normalize(wrist.pose.q);
}

} // namespace cf
