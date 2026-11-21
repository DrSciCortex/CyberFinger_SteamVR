/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CfConfig.h — the WiVRn driver's settings
//
// A flat JSON file, ~/.config/cyberfinger/wivrn.json, with the SteamVR driver's
// key names (resources/settings/default.vrsettings) where the setting is the
// same. Every key is optional; the file is read again whenever it changes, so
// all but the UDP ports apply while WiVRn runs.
// ═══════════════════════════════════════════════════════════════════════════

#include <cstdint>
#include <string>
#include "MathUtil.h"
#include "Protocol.h"

namespace cf {

// The CyberFinger's pink button in a button mask (it arrives in CFG2's buttons2, not among the eight).
constexpr uint16_t kMaskPink = 0x100;

struct Config {
    bool        active = true;              // false: WiVRn behaves as without CyberFinger
    std::string profile = "touch";          // the controller applications see (CfWivrn.cpp: ProfileName)

    int  listenPort = 27015;                // handtracking_udp_port: bridge → driver
    int  contextPort = 27016;               // context_udp_port: driver → bridge
    bool loopbackOnly = true;               // bind_loopback_only
    bool legacy5bit = false;                // legacy_5bit_buttons

    // CyberFinger buttons → the emulated controller's buttons. A: the lower face button (A right, X left),
    // B: the upper one (B, Y), menu: the left controller's menu button, system: the right controller's system
    // button (which few applications can bind).
    uint16_t maskA = kBtnStartSelect;       // button_a
    uint16_t maskB = kBtnMenu;              // button_b
    uint16_t maskMenu = kMaskPink;          // button_menu (left hand)
    uint16_t maskSystem = 0;                // button_system (right hand)

    double blackHoldTime = 0.8;             // black_hold_ms: A held this long is the long press instead; 0: off
    bool   grabTapToHold = true;            // grab_tap_to_hold: the squeeze latches on a short grip press
    double grabTapTime = 0.2;               // grab_tap_ms

    double releaseAfter = 2.0;              // release_after_ms: without CyberFinger data, the hand goes back to WiVRn
    double occlusionHold = 2.0;             // occlusion_hold_ms: a hand the headset lost keeps its place this long,
                                            // turning with the IMU (needs imu_fusion); 0: it is lost at once

    bool   imuFusion = true;                // imu_fusion
    double rotationPrediction = 0.0;        // pose_rotation_prediction: how much of the IMU's angular velocity
                                            // applications get to extrapolate with (0..1)

    // The controller's poses against the hand's (grip_offset_m, grip_angle_deg: intrinsic X-Y-Z, as the SteamVR
    // driver's grip_angle_*): moves the grip, aim and palm poses together. The hand joints stay where the hand is.
    Xform  gripOffset;

    // The inputs an OpenXR controller has no place for go to Resonite as the MoreFluxActions mod's FluxActions
    // (UDP, as the bridge's right pink button does): the left hand's action number, the right hand gets the next
    // one. 0: off. The numbers are those of the SteamVR driver's Resonite binding.
    int    fluxPort = 42042;                // flux_port; 0: no FluxActions at all
    int    fluxAHold = 1;                   // flux_actions.a_hold
    int    fluxC = 3, fluxD = 5, fluxE = 7; // flux_actions.c, .d, .e
    int    fluxTwoFingerPoint = 36;         // flux_actions.two_finger_point
    int    fluxPinkyPinch = 38;             // flux_actions.pinky_pinch
    int    fluxIndexPoint = 40;             // flux_actions.index_point
    int    fluxPinkRight = 42;              // flux_actions.pink_right (when the bridge leaves the button to the driver)

    bool   statusLog = true;                // status_log: a status line per hand every 10 s
    bool   gestureLog = false;              // gesture_log: the two-finger point's near misses, to tune it (a relaxed
                                            // hand is one most of the time: a line per hand every second)
};

// "STSEL|MENU" → a button mask. Names: TRIG GRIP C D E MENU JCLK STSEL PINK NONE; unknown names are ignored
// (reported through unknown, if given).
uint16_t ButtonMask(const std::string& names, std::string* unknown = nullptr);

// ~/.config/cyberfinger/wivrn.json (XDG_CONFIG_HOME honoured).
std::string DefaultConfigPath();

// Reads the JSON text into cfg, keys it doesn't have keep their value. False (cfg untouched) when the text isn't
// a JSON object; problems with single keys go to warnings, one per line.
bool ParseConfig(const std::string& json, Config& cfg, std::string& warnings);

} // namespace cf
