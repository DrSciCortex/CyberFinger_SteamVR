/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// CfConfig.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "CfConfig.h"
#include <cstdlib>
#include <nlohmann/json.hpp>
#include "Utils.h"

namespace cf {

uint16_t ButtonMask(const std::string& names, std::string* unknown) {
    static const struct { const char* name; uint16_t mask; } kNames[] = {
        { "trig", kBtnTrigger }, { "grip", kBtnGrip }, { "c", kBtnC }, { "d", kBtnD }, { "e", kBtnE },
        { "menu", kBtnMenu }, { "jclk", kBtnStickClick }, { "stsel", kBtnStartSelect }, { "pink", kMaskPink },
        { "none", 0 },
    };
    uint16_t mask = 0;
    for (const std::string& name : SplitList(names)) {
        bool found = false;
        for (const auto& n : kNames) {
            if (name != n.name) continue;
            mask |= n.mask;
            found = true;
        }
        if (!found && unknown) *unknown += (unknown->empty() ? "" : ", ") + name;
    }
    return mask;
}

std::string DefaultConfigPath() {
    const char* xdg = std::getenv("XDG_CONFIG_HOME");
    if (xdg && *xdg) return std::string(xdg) + "/cyberfinger/wivrn.json";
    const char* home = std::getenv("HOME");
    return home && *home ? std::string(home) + "/.config/cyberfinger/wivrn.json" : std::string();
}

namespace {

using Json = nlohmann::json;

// j[key] into out if present and of the right type; a wrong type is a warning and out keeps its value.
template <class T> void Read(const Json& j, const char* key, T& out, std::string& warnings) {
    const auto it = j.find(key);
    if (it == j.end()) return;
    try {
        out = it->get<T>();
    } catch (const Json::exception&) {
        warnings += std::string(key) + ": wrong type, ignored\n";
    }
}

void ReadMs(const Json& j, const char* key, double& seconds, std::string& warnings) {
    double ms = seconds * 1e3;
    Read(j, key, ms, warnings);
    seconds = ms < 0 ? 0 : ms * 1e-3;
}

void ReadButtons(const Json& j, const char* key, uint16_t& mask, std::string& warnings) {
    const auto it = j.find(key);
    if (it == j.end()) return;
    if (!it->is_string()) {
        warnings += std::string(key) + ": expected button names like \"STSEL|MENU\", ignored\n";
        return;
    }
    std::string unknown;
    mask = ButtonMask(it->get<std::string>(), &unknown);
    if (!unknown.empty()) warnings += std::string(key) + ": unknown button " + unknown + "\n";
}

bool ReadVec3(const Json& j, const char* key, double v[3], std::string& warnings) {
    const auto it = j.find(key);
    if (it == j.end()) return false;
    if (!it->is_array() || it->size() != 3 || !(*it)[0].is_number() || !(*it)[1].is_number() ||
        !(*it)[2].is_number()) {
        warnings += std::string(key) + ": expected three numbers, ignored\n";
        return false;
    }
    for (int i = 0; i < 3; ++i) v[i] = (*it)[size_t(i)].get<double>();
    return true;
}

} // namespace

bool ParseConfig(const std::string& text, Config& cfg, std::string& warnings) {
    const Json j = Json::parse(text, nullptr, false, true);   // no exceptions; comments allowed
    if (!j.is_object()) return false;
    Config c = cfg;

    Read(j, "active", c.active, warnings);
    Read(j, "profile", c.profile, warnings);
    Read(j, "handtracking_udp_port", c.listenPort, warnings);
    Read(j, "context_udp_port", c.contextPort, warnings);
    Read(j, "bind_loopback_only", c.loopbackOnly, warnings);
    Read(j, "legacy_5bit_buttons", c.legacy5bit, warnings);
    ReadButtons(j, "button_a", c.maskA, warnings);
    ReadButtons(j, "button_b", c.maskB, warnings);
    ReadButtons(j, "button_menu", c.maskMenu, warnings);
    ReadButtons(j, "button_system", c.maskSystem, warnings);
    ReadMs(j, "black_hold_ms", c.blackHoldTime, warnings);
    Read(j, "grab_tap_to_hold", c.grabTapToHold, warnings);
    ReadMs(j, "grab_tap_ms", c.grabTapTime, warnings);
    ReadMs(j, "release_after_ms", c.releaseAfter, warnings);
    ReadMs(j, "occlusion_hold_ms", c.occlusionHold, warnings);
    Read(j, "imu_fusion", c.imuFusion, warnings);
    Read(j, "pose_rotation_prediction", c.rotationPrediction, warnings);
    c.rotationPrediction = std::min(1.0, std::max(0.0, c.rotationPrediction));
    Read(j, "status_log", c.statusLog, warnings);
    Read(j, "gesture_log", c.gestureLog, warnings);

    double v[3];
    if (ReadVec3(j, "grip_offset_m", v, warnings)) c.gripOffset.p = { v[0], v[1], v[2] };
    if (ReadVec3(j, "grip_angle_deg", v, warnings)) c.gripOffset.q = QuatFromEulerXYZDeg(v[0], v[1], v[2]);

    Read(j, "flux_port", c.fluxPort, warnings);
    if (const auto it = j.find("flux_actions"); it != j.end()) {
        if (it->is_object()) {
            Read(*it, "a_hold", c.fluxAHold, warnings);
            Read(*it, "c", c.fluxC, warnings);
            Read(*it, "d", c.fluxD, warnings);
            Read(*it, "e", c.fluxE, warnings);
            Read(*it, "two_finger_point", c.fluxTwoFingerPoint, warnings);
            Read(*it, "pinky_pinch", c.fluxPinkyPinch, warnings);
            Read(*it, "index_point", c.fluxIndexPoint, warnings);
            Read(*it, "pink_right", c.fluxPinkRight, warnings);
        } else {
            warnings += "flux_actions: expected an object, ignored\n";
        }
    }
    cfg = c;
    return true;
}

} // namespace cf
