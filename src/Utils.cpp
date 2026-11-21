/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// Utils.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "Utils.h"
#include <cstdarg>
#include <cstdio>

namespace cf {

static vr::IVRDriverLog* g_pLog = nullptr;

void SetDriverLog(vr::IVRDriverLog* log) { g_pLog = log; }

void DriverLog(const char* fmt, ...) {
    char buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (g_pLog) g_pLog->Log(buf);
}

std::string SettingString(const char* key, const char* def) {
    char buf[512]{};
    vr::EVRSettingsError err = vr::VRSettingsError_None;
    vr::VRSettings()->GetString(kSettingsSection, key, buf, sizeof(buf), &err);
    return (err == vr::VRSettingsError_None) ? std::string(buf) : std::string(def);
}

float SettingFloat(const char* key, float def) {
    vr::EVRSettingsError err = vr::VRSettingsError_None;
    const float v = vr::VRSettings()->GetFloat(kSettingsSection, key, &err);
    return (err == vr::VRSettingsError_None) ? v : def;
}

int32_t SettingInt(const char* key, int32_t def) {
    vr::EVRSettingsError err = vr::VRSettingsError_None;
    const int32_t v = vr::VRSettings()->GetInt32(kSettingsSection, key, &err);
    return (err == vr::VRSettingsError_None) ? v : def;
}

bool SettingBool(const char* key, bool def) {
    vr::EVRSettingsError err = vr::VRSettingsError_None;
    const bool v = vr::VRSettings()->GetBool(kSettingsSection, key, &err);
    return (err == vr::VRSettingsError_None) ? v : def;
}

} // namespace cf
