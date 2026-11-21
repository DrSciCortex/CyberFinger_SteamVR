/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// CfWivrn.cpp — WiVRn's types to the driver's and back
// ═══════════════════════════════════════════════════════════════════════════

#include "CfWivrn.h"
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

#include "driver/clock_offset.h"
#include "util/u_logging.h"
#include "wivrn_packets.h"

#include "CfDriver.h"
#include "Utils.h"

namespace cf {

// The portable code's log (Utils.h), into WiVRn's.
void DriverLog(const char* fmt, ...) {
    char buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    size_t n = std::strlen(buf);
    while (n && buf[n - 1] == '\n') buf[--n] = 0;
    U_LOG_I("CyberFinger: %s", buf);
}

namespace {

Vec3 ToVec(const xrt_vec3& v) { return { v.x, v.y, v.z }; }
Vec3 ToVec(const XrVector3f& v) { return { v.x, v.y, v.z }; }
xrt_vec3 FromVec(const Vec3& v) { return { float(v.x), float(v.y), float(v.z) }; }
Xform ToXform(const xrt_pose& p) {
    return { Normalize({ p.orientation.w, p.orientation.x, p.orientation.y, p.orientation.z }), ToVec(p.position) };
}
Xform ToXform(const XrQuaternionf& q, const XrVector3f& p) { return { Normalize({ q.w, q.x, q.y, q.z }), ToVec(p) }; }

TrackedPose ToTracked(const xrt_space_relation& r) {
    TrackedPose t;
    t.positionValid = (r.relation_flags & XRT_SPACE_RELATION_POSITION_VALID_BIT) != 0;
    t.orientationValid = (r.relation_flags & XRT_SPACE_RELATION_ORIENTATION_VALID_BIT) != 0;
    t.positionTracked = (r.relation_flags & XRT_SPACE_RELATION_POSITION_TRACKED_BIT) != 0;
    t.orientationTracked = (r.relation_flags & XRT_SPACE_RELATION_ORIENTATION_TRACKED_BIT) != 0;
    t.linearValid = (r.relation_flags & XRT_SPACE_RELATION_LINEAR_VELOCITY_VALID_BIT) != 0;
    t.angularValid = (r.relation_flags & XRT_SPACE_RELATION_ANGULAR_VELOCITY_VALID_BIT) != 0;
    if (t.orientationValid) t.pose.q = ToXform(r.pose).q;
    if (t.positionValid) t.pose.p = ToVec(r.pose.position);
    if (t.linearValid) t.linear = ToVec(r.linear_velocity);
    if (t.angularValid) t.angular = ToVec(r.angular_velocity);
    return t;
}

void FromTracked(const TrackedPose& t, xrt_space_relation& r) {
    int flags = 0;
    if (t.positionValid) flags |= XRT_SPACE_RELATION_POSITION_VALID_BIT;
    if (t.orientationValid) flags |= XRT_SPACE_RELATION_ORIENTATION_VALID_BIT;
    if (t.positionTracked) flags |= XRT_SPACE_RELATION_POSITION_TRACKED_BIT;
    if (t.orientationTracked) flags |= XRT_SPACE_RELATION_ORIENTATION_TRACKED_BIT;
    if (t.linearValid) flags |= XRT_SPACE_RELATION_LINEAR_VELOCITY_VALID_BIT;
    if (t.angularValid) flags |= XRT_SPACE_RELATION_ANGULAR_VELOCITY_VALID_BIT;
    r.relation_flags = xrt_space_relation_flags(flags);
    r.pose.orientation = { float(t.pose.q.x), float(t.pose.q.y), float(t.pose.q.z), float(t.pose.q.w) };
    r.pose.position = FromVec(t.pose.p);
    r.linear_velocity = FromVec(t.linear);
    r.angular_velocity = FromVec(t.angular);
}

// A pose as the headset sends it.
template <class Pose> TrackedPose ToTracked(const Pose& p, const XrQuaternionf& q, const XrVector3f& position) {
    using namespace wivrn::from_headset;
    TrackedPose t;
    t.positionValid = (p.flags & position_valid) != 0;
    t.orientationValid = (p.flags & orientation_valid) != 0;
    t.positionTracked = (p.flags & position_tracked) != 0;
    t.orientationTracked = (p.flags & orientation_tracked) != 0;
    t.linearValid = (p.flags & linear_velocity_valid) != 0;
    t.angularValid = (p.flags & angular_velocity_valid) != 0;
    if (t.orientationValid) t.pose.q = ToXform(q, position).q;
    if (t.positionValid) t.pose.p = ToVec(position);
    if (t.linearValid) t.linear = ToVec(p.linear_velocity);
    if (t.angularValid) t.angular = ToVec(p.angular_velocity);
    return t;
}

bool KindOf(xrt_input_name name, PoseKind& kind) {
    switch (name) {
        case XRT_INPUT_TOUCH_GRIP_POSE:
        case XRT_INPUT_HAND_GRIP_POSE: kind = PoseKind::Grip; return true;
        case XRT_INPUT_TOUCH_AIM_POSE:
        case XRT_INPUT_HAND_AIM_POSE: kind = PoseKind::Aim; return true;
        case XRT_INPUT_GENERIC_PALM_POSE: kind = PoseKind::Palm; return true;
        case XRT_INPUT_HAND_PINCH_POSE: kind = PoseKind::Pinch; return true;
        case XRT_INPUT_HAND_POKE_POSE: kind = PoseKind::Poke; return true;
        default: return false;
    }
}

// The interaction profiles WiVRn's controller device can stand in for (its binding profiles).
xrt_device_name ProfileName(const std::string& profile) {
    static const struct { const char* name; xrt_device_name device; } kProfiles[] = {
        { "touch", XRT_DEVICE_TOUCH_CONTROLLER },
        { "touch_plus", XRT_DEVICE_TOUCH_PLUS_CONTROLLER },
        { "touch_pro", XRT_DEVICE_TOUCH_PRO_CONTROLLER },
        { "index", XRT_DEVICE_INDEX_CONTROLLER },
        { "vive_focus3", XRT_DEVICE_VIVE_FOCUS3_CONTROLLER },
        { "pico4", XRT_DEVICE_PICO4_CONTROLLER },
        { "pico_neo3", XRT_DEVICE_PICO_NEO3_CONTROLLER },
        { "simple", XRT_DEVICE_SIMPLE_CONTROLLER },
    };
    for (const auto& p : kProfiles)
        if (Lower(profile) == p.name) return p.device;
    DriverLog("settings: profile \"%s\" unknown (touch, touch_plus, touch_pro, index, vive_focus3, pico4, pico_neo3, "
              "simple): using touch\n", profile.c_str());
    return XRT_DEVICE_TOUCH_CONTROLLER;
}

} // namespace

// ── WivrnHand ──────────────────────────────────────────────────────────────

void WivrnHand::UpdateInputs(xrt_input* inputs, size_t count) {
    if (!m_hand->Active()) return;
    const ControllerInputs c = m_hand->Inputs();
    const int64_t now = int64_t(NowSeconds() * 1e9);
    if (m_last.size() != count) m_last.assign(count, {});

    for (size_t i = 0; i < count; ++i) {
        float x = 0, y = 0;
        bool isBool = true, isVec2 = false;
        // WiVRn's controller device names its inputs after the Touch controller, with a few from others where
        // Touch has none (trigger click, squeeze click and force); its binding profiles map the rest.
        switch (inputs[i].name) {
            case XRT_INPUT_TOUCH_TRIGGER_VALUE: x = c.trigger; isBool = false; break;
            case XRT_INPUT_INDEX_TRIGGER_CLICK: x = c.triggerClick; break;
            case XRT_INPUT_TOUCH_SQUEEZE_VALUE:
            case XRT_INPUT_INDEX_SQUEEZE_FORCE: x = c.squeeze; isBool = false; break;
            case XRT_INPUT_VIVE_FOCUS3_SQUEEZE_CLICK: x = c.squeeze > 0.5f; break;
            case XRT_INPUT_TOUCH_THUMBSTICK: x = c.stickX; y = c.stickY; isBool = false; isVec2 = true; break;
            case XRT_INPUT_TOUCH_THUMBSTICK_CLICK: x = c.stickClick; break;
            case XRT_INPUT_TOUCH_A_CLICK:
            case XRT_INPUT_TOUCH_X_CLICK: x = c.a; break;
            case XRT_INPUT_TOUCH_B_CLICK:
            case XRT_INPUT_TOUCH_Y_CLICK: x = c.b; break;
            case XRT_INPUT_TOUCH_MENU_CLICK:
            case XRT_INPUT_TOUCH_SYSTEM_CLICK: x = c.menu; break;
            // No capacitive sensors, as with the SteamVR driver: a touch says a controller is in the hand, and
            // ProximityGrab turns its hand gestures off for as long as that controller stays tracked.
            case XRT_INPUT_TOUCH_TRIGGER_TOUCH:
            case XRT_INPUT_TOUCH_THUMBSTICK_TOUCH:
            case XRT_INPUT_TOUCH_A_TOUCH:
            case XRT_INPUT_TOUCH_X_TOUCH:
            case XRT_INPUT_TOUCH_B_TOUCH:
            case XRT_INPUT_TOUCH_Y_TOUCH:
            case XRT_INPUT_TOUCH_THUMBREST_TOUCH: break;
            default: continue;                        // poses, hand tracking, sensors a CyberFinger hasn't got
        }
        Last& last = m_last[i];
        if (x != last.x || y != last.y || !last.changed) last = { x, y, now };
        inputs[i].active = true;
        inputs[i].timestamp = last.changed;
        if (isVec2) inputs[i].value.vec2 = { x, y };
        else if (isBool) inputs[i].value.boolean = x != 0;
        else inputs[i].value.vec1.x = x;
    }
}

void WivrnHand::AdjustPose(xrt_input_name name, int64_t targetNs, xrt_space_relation& pose, const GripPose& grip) {
    PoseKind kind;
    if (!m_hand->Active() || !KindOf(name, kind)) return;
    m_lastQuery.store(NowSeconds(), std::memory_order_relaxed);
    TrackedPose p = ToTracked(pose);
    const TrackedPose headsetGrip = kind == PoseKind::Grip ? p : ToTracked(grip());
    m_hand->AdjustPose(kind, targetNs * 1e-9, NowSeconds(), p, headsetGrip);
    FromTracked(p, pose);
}

void WivrnHand::AdjustJoints(int64_t targetNs, xrt_hand_joint_set& joints, const GripPose& grip) {
    if (!m_hand->Active()) return;
    m_lastQuery.store(NowSeconds(), std::memory_order_relaxed);
    if (!joints.is_active) return;
    TrackedPose wrist = ToTracked(joints.hand_pose);
    m_hand->AdjustJoints(targetNs * 1e-9, NowSeconds(), wrist, ToTracked(grip()));
    FromTracked(wrist, joints.hand_pose);
}

bool WivrnHand::Haptic(const xrt_output_value& value) {
    if (!m_hand->Active() || value.type != XRT_OUTPUT_VALUE_TYPE_VIBRATION) return false;
    const auto& v = value.vibration;
    // XRT_MIN_HAPTIC_DURATION (-1): the shortest pulse the device has, which is CFHP's 0.
    m_driver->Haptic(m_index, v.duration_ns > 0 ? float(v.duration_ns * 1e-9) : 0.f, v.frequency, v.amplitude);
    return true;
}

bool WivrnHand::TakeActivation() {
    const bool active = m_hand->Active(), took = active && !m_wasActive;
    m_wasActive = active;
    return took;
}

void WivrnHand::OnTracking(const wivrn::from_headset::tracking& tracking, const wivrn::clock_offset& offset) {
    if (!offset) return;
    const wivrn::device_id gripId = m_index ? wivrn::device_id::RIGHT_GRIP : wivrn::device_id::LEFT_GRIP;
    const double target = offset.from_headset(tracking.timestamp) * 1e-9;
    const double produced = offset.from_headset(tracking.production_timestamp) * 1e-9;
    for (const auto& pose : tracking.device_poses) {
        if (pose.device == wivrn::device_id::HEAD)
            m_hand->OnHead(ToTracked(pose, pose.pose.orientation, pose.pose.position));
        else if (pose.device == gripId)
            m_hand->OnGrip(target, produced, NowSeconds(), ToTracked(pose, pose.pose.orientation, pose.pose.position));
    }
}

void WivrnHand::OnHandTracking(const wivrn::from_headset::hand_tracking& tracking, const wivrn::clock_offset& offset) {
    if (!offset || int(tracking.hand) != m_index) return;
    const double now = NowSeconds();
    using namespace wivrn::from_headset;
    constexpr uint8_t kPose = position_valid | orientation_valid;
    if (!tracking.joints || ((*tracking.joints)[XR_HAND_JOINT_WRIST_EXT].flags & kPose) != kPose) {
        m_hand->OnJoints(now, false, {}, nullptr);
        return;
    }
    static_assert(XR_HAND_JOINT_COUNT_EXT == kNumJoints, "joint count");
    const auto& joints = *tracking.joints;
    const Xform wrist = ToXform(joints[XR_HAND_JOINT_WRIST_EXT].orientation, joints[XR_HAND_JOINT_WRIST_EXT].position);
    const Xform toWrist = Inverse(wrist);
    Xform rel[kNumJoints];
    for (int j = 0; j < kNumJoints; ++j)
        rel[j] = toWrist * ToXform(joints[size_t(j)].orientation, joints[size_t(j)].position);
    m_hand->OnJoints(now, true, wrist, rel);
}

// ── WivrnDriver ────────────────────────────────────────────────────────────

WivrnDriver::WivrnDriver() : m_driver(std::make_unique<Driver>(DefaultConfigPath(), DefaultStatePath())) {
    for (int hand = 0; hand < 2; ++hand) {
        m_hands[hand].m_driver = m_driver.get();
        m_hands[hand].m_hand = &m_driver->GetHand(hand);
        m_hands[hand].m_index = hand;
    }
}

WivrnDriver::~WivrnDriver() = default;

xrt_device_name WivrnDriver::Profile(int hand, xrt_device_name fromHeadset) {
    const bool handsFree = fromHeadset == XRT_DEVICE_EXT_HAND_INTERACTION || fromHeadset == XRT_DEVICE_INVALID;
    if (!m_driver->HoldsRole(hand, handsFree)) return fromHeadset;
    const uint32_t generation = m_driver->ConfigGeneration();
    if (generation != m_profileGeneration.load(std::memory_order_relaxed)) {
        m_profile = ProfileName(m_driver->GetConfig().profile);
        m_profileGeneration = generation;
    }
    return m_profile;
}

bool WivrnDriver::KeepTracking(int hand) const {
    const WivrnHand& h = m_hands[hand & 1];
    return h.m_hand->Active() && NowSeconds() - h.m_lastQuery.load(std::memory_order_relaxed) > 0.5;
}

} // namespace cf
