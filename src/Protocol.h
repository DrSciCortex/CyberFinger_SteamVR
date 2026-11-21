/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// Protocol.h — CyberFinger UDP wire protocol v2
//
// Little-endian, packed, loopback only. Mirrored in bridge/cf_protocol.py;
// tests/test_protocol.* keep the two in sync with golden vectors.
//
//   CFG2  bridge → driver  CyberFinger buttons / stick / trigger, one per BLE report
//   CFHS  bridge → driver  fused hand state (pose + 31 bones), per fusion tick
//   CFOP  driver → bridge  HMD pose + optical tap (headset hand tracking)
//   CFHP  driver → bridge  haptic vibration request from an application
//   CFIM  bridge → driver  raw CyberFinger IMU slots, one per BLE report (the
//                          driver's own IMU fusion, and its captures)
//   CFGP  legacy CyberFinger packet (12 bytes, no header), still accepted
// ═══════════════════════════════════════════════════════════════════════════

#include <cstdint>

namespace cf {

constexpr uint32_t Magic(char a, char b, char c, char d) {
    return uint32_t(uint8_t(a)) | (uint32_t(uint8_t(b)) << 8) |
           (uint32_t(uint8_t(c)) << 16) | (uint32_t(uint8_t(d)) << 24);
}

constexpr uint32_t kMagicCyberFinger     = Magic('C', 'F', 'G', '2');
constexpr uint32_t kMagicHandState = Magic('C', 'F', 'H', 'S');
constexpr uint32_t kMagicContext   = Magic('C', 'F', 'O', 'P');
constexpr uint32_t kMagicHaptic    = Magic('C', 'F', 'H', 'P');
constexpr uint32_t kMagicImu       = Magic('C', 'F', 'I', 'M');
constexpr uint32_t kMagicLegacy    = Magic('C', 'F', 'G', 'P');
constexpr uint8_t  kVersion        = 1;
constexpr int      kNumBones       = 31;

// Firmware button bits (current "VR mode" firmware layout).
enum CyberFingerButton : uint8_t {
    kBtnTrigger     = 0x01,
    kBtnGrip        = 0x02,
    kBtnC           = 0x04,
    kBtnD           = 0x08,
    kBtnE           = 0x10,
    kBtnMenu        = 0x20,
    kBtnStickClick  = 0x40,
    kBtnStartSelect = 0x80,
};

// CFG2 buttons2: buttons beyond the first byte (firmware 1.3.3+, the report's extension byte).
enum CyberFingerButton2 : uint8_t {
    kBtn2Pink       = 0x01,   // the pink power key, a short press as a click: left = SteamVR's system button
};

// CFHS flags
enum HandStateFlags : uint16_t {
    kHsPoseValid  = 0x0001,   // raw_pos/raw_rot are usable
    kHsHasBones   = 0x0002,   // bones[] are filled (else the driver synthesizes from curl/splay)
    kHsCameraSees = 0x0004,   // the headset camera saw the hand for this sample
    kHsCalibrated = 0x0008,   // IMU mounting calibrated
};

// CFOP flags
enum ContextFlags : uint16_t {
    kCtxCapturing = 0x0001,   // a capture is running (informational)
};

// CFIM slot bits (the CyberFinger's own layout)
enum ImuSlot : uint8_t {
    kImuBody1 = 0x01,
    kImuBody2 = 0x02,         // same spot as body 1, other chip
    kImuJoint = 0x04,
};

// Per-hand driver output mode, reported in CFOP.
enum OutputMode : uint8_t {
    kModeNone        = 0,
    kModeFused       = 1,     // pose + skeleton from CFHS
    kModePassthrough = 2,     // headset hand tracking, re-rooted onto CyberFinger
    kModeNoPose      = 3,     // no pose source: pose invalid, inputs still live
    kModeReleased    = 4,     // device reported disconnected
};

#pragma pack(push, 1)

struct Header {               // 24 bytes
    uint32_t magic;
    uint8_t  version;
    uint8_t  hand;            // 0 left, 1 right, 0xFF n/a
    uint16_t flags;
    uint32_t seq;             // per sender, per hand
    uint32_t age_us;          // age of the newest sensor sample behind this packet, at send time
    uint64_t t_send_us;       // sender's monotonic clock; diagnostics only
};

struct Bone {                 // same layout as vr::VRBoneTransform_t
    float px, py, pz, pw;
    float qw, qx, qy, qz;
};

struct CyberFingerPacket {          // 'CFG2'
    Header  h;
    uint8_t buttons;          // CyberFingerButton bits
    uint8_t trigger;          // 0..255 analog
    int16_t joy_x, joy_y;     // centred + deadzoned by the sender, ±32767, +y = up
    uint8_t battery_pct;
    uint8_t buttons2;         // CyberFingerButton2 bits (0 from older bridges)
    uint8_t resync;           // a count the bridge bumps to resync this hand's IMU fusion (its button, a triple tap)
    uint8_t reserved;
};

struct HandStatePacket {      // 'CFHS'
    Header   h;
    float    raw_pos[3];      // /pose/raw, SteamVR raw space, metres
    float    raw_rot[4];      // w, x, y, z
    float    lin_vel[3];      // m/s, raw space
    float    ang_vel[3];      // rad/s axis-angle, raw space
    float    curl[5];         // thumb..pinky, 0 open .. 1 closed
    float    splay[5];        // -1 .. 1
    float    pose_conf;
    float    finger_conf[5];
    uint32_t optical_seq;     // seq of the CFOP frame this state used, 0 if none
    uint8_t  pos_src, rot_src, pose_src, key_posture;
    Bone     bones[kNumBones];// parent space; used for both motion ranges
};

struct TapHand {
    uint8_t  pose_valid;      // headset hand-tracking device pose valid
    uint8_t  skel_valid;      // skeleton received recently
    uint8_t  tracking_result; // vr::ETrackingResult of the device pose
    uint8_t  bone_count;      // as submitted by the source
    uint8_t  system_click;    // forwarded system button (Quest palm pinch → ≡)
    uint8_t  source_found;    // a headset hand device was found for this hand
    uint8_t  reserved[2];
    uint32_t skel_age_us;
    float    raw_pos[3];
    float    raw_rot[4];      // w, x, y, z
    float    lin_vel[3];
    float    ang_vel[3];
    Bone     bones[kNumBones];// WithoutController, parent space, as submitted
};

struct ContextPacket {        // 'CFOP', hand = 0xFF
    Header   h;
    uint8_t  hmd_valid;
    uint8_t  mode_left, mode_right;   // OutputMode
    uint8_t  tap_hook_ok;             // skeleton hook installed
    float    hmd_pos[3];
    float    hmd_rot[4];              // w, x, y, z
    float    hmd_lin_vel[3];
    float    hmd_ang_vel[3];
    uint32_t applied_hs_seq[2];       // last CFHS applied per hand
    TapHand  tap[2];
};

struct HapticPacket {         // 'CFHP', one per SteamVR vibration event for our haptic component
    Header h;                 // hand
    float  duration_s;        // 0 = a single short pulse
    float  frequency_hz;
    float  amplitude;         // 0..1
};

struct ImuPacket {            // 'CFIM': seq = the CyberFinger report's; age_us = since that report arrived over BLE
    Header  h;                // hand
    uint8_t present;          // ImuSlot bits
    uint8_t has_accel;        // accel[] filled (newer firmware)
    uint8_t reserved0[2];
    float   quat[3][4];       // body 1, body 2, joint: w, x, y, z, as the CyberFinger fuses them
    int16_t accel[3][3];      // raw sensor-frame acceleration per slot, firmware counts
    uint8_t reserved1[2];
};

struct LegacyCyberFingerPacket {    // 'CFGP' (no header)
    uint32_t magic;
    uint8_t  hand;
    uint8_t  buttons;
    int16_t  joy_x, joy_y;    // raw, uncentred, +y = down
    uint8_t  trigger_analog;
    uint8_t  battery_pct;
};

#pragma pack(pop)

static_assert(sizeof(Header) == 24, "Header layout");
static_assert(sizeof(Bone) == 32, "Bone layout");
static_assert(sizeof(CyberFingerPacket) == 34, "CyberFingerPacket layout");
static_assert(sizeof(HandStatePacket) == 1140, "HandStatePacket layout");
static_assert(sizeof(TapHand) == 1056, "TapHand layout");
static_assert(sizeof(ContextPacket) == 2200, "ContextPacket layout");
static_assert(sizeof(HapticPacket) == 36, "HapticPacket layout");
static_assert(sizeof(ImuPacket) == 96, "ImuPacket layout");
static_assert(sizeof(LegacyCyberFingerPacket) == 12, "LegacyCyberFingerPacket layout");

} // namespace cf
