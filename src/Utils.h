/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// Utils.h — Shared utilities (logging, quaternion math, time)
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <string>
#include <chrono>
#include <cmath>

namespace merged_ctrl {

// ── Logging ────────────────────────────────────────────────────────────────
void DriverLog(const char* fmt, ...);
void SetDriverLog(vr::IVRDriverLog* log);

// ── Time ───────────────────────────────────────────────────────────────────
inline double NowSeconds() {
    using namespace std::chrono;
    return duration_cast<duration<double>>(
        steady_clock::now().time_since_epoch()).count();
}

// ── Quaternion helpers ─────────────────────────────────────────────────────
struct Quat {
    float w = 1, x = 0, y = 0, z = 0;
};

inline Quat Slerp(const Quat& a, const Quat& b, float t) {
    float dot = a.w*b.w + a.x*b.x + a.y*b.y + a.z*b.z;
    Quat b2 = b;
    if (dot < 0.f) { dot = -dot; b2 = {-b.w, -b.x, -b.y, -b.z}; }
    if (dot > 0.9995f) {
        return { a.w + t*(b2.w-a.w), a.x + t*(b2.x-a.x),
                 a.y + t*(b2.y-a.y), a.z + t*(b2.z-a.z) };
    }
    float theta = std::acos(dot);
    float sinT = std::sin(theta);
    float wa = std::sin((1.f-t)*theta) / sinT;
    float wb = std::sin(t*theta) / sinT;
    return { wa*a.w + wb*b2.w, wa*a.x + wb*b2.x,
             wa*a.y + wb*b2.y, wa*a.z + wb*b2.z };
}

// ── VR Pose helpers ────────────────────────────────────────────────────────
inline vr::DriverPose_t MakeDefaultPose() {
    vr::DriverPose_t pose{};
    pose.poseIsValid = false;
    pose.result = vr::TrackingResult_Uninitialized;
    pose.deviceIsConnected = true;

    pose.qWorldFromDriverRotation.w = 1;
    pose.qDriverFromHeadRotation.w = 1;
    pose.qRotation.w = 1;

    pose.vecWorldFromDriverTranslation[0] = 0;
    pose.vecWorldFromDriverTranslation[1] = 0;
    pose.vecWorldFromDriverTranslation[2] = 0;

    pose.vecPosition[0] = 0;
    pose.vecPosition[1] = 0;
    pose.vecPosition[2] = 0;

    return pose;
}

// ── String helpers ─────────────────────────────────────────────────────────
std::string GetDriverSettingString(const char* section, const char* key,
                                   const char* defaultVal);
float GetDriverSettingFloat(const char* section, const char* key, float def);
int32_t GetDriverSettingInt(const char* section, const char* key, int32_t def);
bool GetDriverSettingBool(const char* section, const char* key, bool def);

} // namespace merged_ctrl

