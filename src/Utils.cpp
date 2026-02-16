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
#include <cstring>

namespace merged_ctrl {

static vr::IVRDriverLog* g_pLog = nullptr;

void SetDriverLog(vr::IVRDriverLog* log) { g_pLog = log; }

void DriverLog(const char* fmt, ...) {
    char buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    if (g_pLog)
        g_pLog->Log(buf);
}

std::string GetDriverSettingString(const char* section, const char* key,
                                    const char* defaultVal) {
    char buf[512]{};
    vr::EVRSettingsError err;
    vr::VRSettings()->GetString(section, key, buf, sizeof(buf), &err);
    if (err != vr::VRSettingsError_None) return defaultVal;
    return buf;
}

float GetDriverSettingFloat(const char* section, const char* key, float def) {
    vr::EVRSettingsError err;
    float val = vr::VRSettings()->GetFloat(section, key, &err);
    return (err == vr::VRSettingsError_None) ? val : def;
}

int32_t GetDriverSettingInt(const char* section, const char* key, int32_t def) {
    vr::EVRSettingsError err;
    int32_t val = vr::VRSettings()->GetInt32(section, key, &err);
    return (err == vr::VRSettingsError_None) ? val : def;
}

bool GetDriverSettingBool(const char* section, const char* key, bool def) {
    vr::EVRSettingsError err;
    bool val = vr::VRSettings()->GetBool(section, key, &err);
    return (err == vr::VRSettingsError_None) ? val : def;
}

} // namespace merged_ctrl

