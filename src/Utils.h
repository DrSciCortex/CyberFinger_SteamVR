/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// Utils.h — logging, time and settings helpers
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>
#include "VrTypes.h"

namespace cf {

constexpr const char* kSettingsSection = "driver_cyberfinger";

// ── Logging ────────────────────────────────────────────────────────────────
#ifndef CF_NO_OPENVR
void SetDriverLog(vr::IVRDriverLog* log);
#endif
void DriverLog(const char* fmt, ...);

// ── Time ───────────────────────────────────────────────────────────────────
inline double NowSeconds() {
    using namespace std::chrono;
    return duration_cast<duration<double>>(steady_clock::now().time_since_epoch()).count();
}
inline uint64_t NowMicros() {
    using namespace std::chrono;
    return uint64_t(duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count());
}

// ── Strings ────────────────────────────────────────────────────────────────
inline std::string Lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return char(std::tolower(c)); });
    return s;
}

// "a|b|c" → lower-case entries, blanks dropped.
inline std::vector<std::string> SplitList(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (const char ch : s + "|") {
        if (ch == '|') {
            if (!cur.empty()) out.push_back(Lower(cur));
            cur.clear();
        } else if (!std::isspace(static_cast<unsigned char>(ch))) {
            cur += ch;
        }
    }
    return out;
}

// ── Settings (section driver_cyberfinger) ──────────────────────────────────
std::string SettingString(const char* key, const char* def);
float SettingFloat(const char* key, float def);
int32_t SettingInt(const char* key, int32_t def);
bool SettingBool(const char* key, bool def);

} // namespace cf
