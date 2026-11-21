/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// HandTrackingReceiver.h — Receives hand tracking skeleton data
//
// Supports multiple sources:
//   1. Steam Link (Quest hand tracking forwarded via SteamVR's own protocol)
//   2. Ultraleap (Leap Motion) via their OpenXR or UDP bridge
//   3. Any custom source sending our UDP packet format
//
// The receiver listens on a UDP port for hand skeleton packets.
// A companion bridge application translates from the source SDK to our
// simple wire format.
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <array>
#include <atomic>
#include <thread>
#include <mutex>
#include "BoneData.h"

namespace merged_ctrl {

// ── Wire protocol ──────────────────────────────────────────────────────────
// Sent over UDP, one packet per hand per frame.

#pragma pack(push, 1)
struct HandTrackingPacket {
    uint32_t magic;          // 'HTSK' = 0x4B535448
    uint8_t  version;        // 1
    uint8_t  hand;           // 0=left, 1=right
    uint8_t  confidence;     // 0-255 tracking confidence
    uint8_t  reserved;

    // Hand root pose (wrist position + orientation in HMD-relative space)
    float    pos[3];         // meters
    float    quat[4];        // wxyz orientation

    // Per-bone transforms: 31 bones × (pos xyz + quat wxyz) = 31 × 7 floats
    float    bones[kNumBones][7];

    // Finger curl values (computed by the bridge, 0=open, 1=closed)
    float    curls[5];       // thumb, index, middle, ring, pinky
};
#pragma pack(pop)

static constexpr uint32_t kHandTrackingMagic = 0x4B535448; // 'HTSK'
static constexpr uint8_t  kHandTrackingVersion = 1;

// ── Gamepad packet (from BLE bridge) ────────────────────────────────────
// Sent over the same UDP port, distinguished by magic.

#pragma pack(push, 1)
struct GamepadPacket {
    uint32_t magic;          // 'CFGP' = 0x50474643
    uint8_t  hand;           // 0=left, 1=right
    uint8_t  buttons;        // bit0=AX(trigger), bit1=BY(grip), bit2=BP(menu),
                             // bit3=ST(joy click), bit4=STARTSELECT
    int16_t  joy_x;          // -32767..32767
    int16_t  joy_y;          // -32767..32767
    uint8_t  trigger_analog; // 0-255
    uint8_t  battery_pct;    // 0-100
};
#pragma pack(pop)

static constexpr uint32_t kGamepadMagic = 0x50474643; // 'CFGP'

// ── Gamepad State ───────────────────────────────────────────────────────

struct GamepadState {
    bool valid = false;
    double timestamp = 0.0;
    uint8_t buttons = 0;
    int16_t joy_x = 0;
    int16_t joy_y = 0;
    uint8_t trigger_analog = 0;
    uint8_t battery_pct = 100;
};

// ── Receiver State ─────────────────────────────────────────────────────────

struct HandTrackingState {
    bool valid = false;
    double timestamp = 0.0;
    float confidence = 0.f;

    // Wrist pose in HMD-relative space
    float pos[3] = {};
    float quat[4] = {1, 0, 0, 0};

    // Full 31-bone skeleton
    std::array<vr::VRBoneTransform_t, kNumBones> bones;

    // Finger curls
    float curls[5] = {};

    HandTrackingState() {
        bones = MakeRestPose();
    }
};

class HandTrackingReceiver {
public:
    HandTrackingReceiver();
    ~HandTrackingReceiver();

    void Start(int udpPort);
    void Stop();

    HandTrackingState GetLeft() const;
    HandTrackingState GetRight() const;

    GamepadState GetGamepadLeft() const;
    GamepadState GetGamepadRight() const;

    bool HasRecentData(int hand, double maxAge = 0.1) const;
    bool HasRecentGamepad(int hand, double maxAge = 0.15) const;

private:
    void RecvThread();
    void ProcessPacket(const HandTrackingPacket& pkt);
    void ProcessGamepad(const GamepadPacket& pkt);

    std::thread        m_thread;
    std::atomic<bool>  m_running{false};
    int                m_port = 27015;

#ifdef _WIN32
    uintptr_t          m_socket = ~0ULL;
#else
    int                m_socket = -1;
#endif

    mutable std::mutex m_mutex;
    HandTrackingState  m_left;
    HandTrackingState  m_right;
    GamepadState       m_gamepadLeft;
    GamepadState       m_gamepadRight;
};

} // namespace merged_ctrl

