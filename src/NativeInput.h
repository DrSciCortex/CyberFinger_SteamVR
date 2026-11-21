// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include "StudioLink.h"
#include "OpticalTap.h"
#include "SkeletonSynth.h"
#ifdef _WIN32
#include <windows.h>
#endif

namespace cf {
// ABI v1, little-endian, separately published for each physical hand. The
// writer owns the mapping; consumers only open it read-only. No UDP port used.
struct alignas(8) NativeInputFrame {
    uint32_t sequence = 0, magic = 0x314e4643, version = 1, bytes = 128;
    uint64_t tickMs = 0;
    uint32_t deviceIndex = 0xffffffff, flags = 0, mode = 0, buttons = 0;
    float fingerAgeMs = 1e9f, fingerConfidence = 0, poseConfidence = 0, trigger = 0;
    float curls[5] = {}, pinches[4] = {}, velocity[3] = {}, trust = 0;
    uint32_t processId = 0, reserved[4] = {};
};
static_assert(sizeof(NativeInputFrame) == 128, "Native input ABI size");
static_assert(offsetof(NativeInputFrame, curls) == 56 && offsetof(NativeInputFrame, trust) == 104, "Native ABI offsets");

inline NativeInputFrame MakeNativeInput(uint32_t device, uint8_t mode, bool live, bool poseValid,
    bool buttonsFresh, const CyberFingerState& buttons, const HandStateSample& fused,
    const TapHandSnapshot& tap, const HandGestures& gestures, double now, float trust) {
    NativeInputFrame out;
    out.deviceIndex = device; out.mode = mode;
    out.flags = (live ? 1u : 0u) | (poseValid ? 2u : 0u) | (buttonsFresh ? 8u : 0u);
    out.buttons = buttonsFresh ? buttons.buttons : 0;
    out.trigger = buttonsFresh ? std::max((buttons.buttons & kBtnTrigger) ? 1.f : 0.f,
                                          buttons.triggerAnalog ? buttons.trigger : 0.f) : 0.f;
    out.trust = trust;
    std::copy(gestures.curls, gestures.curls + 5, out.curls);
    std::copy(gestures.pinch, gestures.pinch + 4, out.pinches);
    if (mode == kModeFused) {
        const auto& p = fused.pkt;
        out.fingerAgeMs = float((now - fused.arrival) * 1000.0 + p.h.age_us * .001);
        out.fingerConfidence = *std::min_element(p.finger_conf, p.finger_conf + 5);
        out.poseConfidence = p.pose_conf;
        if (out.fingerConfidence >= .55f && out.fingerAgeMs <= 150 && out.fingerAgeMs >= 0) out.flags |= 4;
        if (p.h.flags & kHsCameraSees) out.flags |= 16;
        std::copy(p.lin_vel, p.lin_vel + 3, out.velocity);
    } else if (mode == kModePassthrough) {
        out.fingerAgeMs = float(tap.skeletonAge * 1000.0);
        const bool fresh = tap.skeletonValid && tap.boneCount == eBone_Count &&
                           tap.skeletonAge >= 0 && tap.skeletonAge <= .15;
        out.fingerConfidence = fresh ? trust : 0;
        out.poseConfidence = poseValid ? trust : 0;
        if (fresh) out.flags |= 4 | 16;
        out.velocity[0] = float(tap.linVel.x); out.velocity[1] = float(tap.linVel.y); out.velocity[2] = float(tap.linVel.z);
    }
    return out;
}

class NativeInputPublisher {
#ifdef _WIN32
    HANDLE mapping_ = nullptr;
    NativeInputFrame* view_ = nullptr;
    uint64_t retryAt_ = 0;
#endif
public:
    ~NativeInputPublisher() {
#ifdef _WIN32
        if (view_) UnmapViewOfFile(view_);
        if (mapping_) CloseHandle(mapping_);
#endif
    }
    void Publish(int hand, NativeInputFrame frame) {
#ifdef _WIN32
        const auto tick = GetTickCount64();
        if (!view_) {
            if (tick < retryAt_) return;
            retryAt_ = tick + 5000;
            mapping_ = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(frame),
                       hand == 0 ? L"Local\\CyberFinger.Native.v1.Left" : L"Local\\CyberFinger.Native.v1.Right");
            if (!mapping_) return;
            view_ = static_cast<NativeInputFrame*>(MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, sizeof(frame)));
            if (!view_) { CloseHandle(mapping_); mapping_ = nullptr; return; }
            // Reset an abandoned odd sequence after a driver crash/restart.
            InterlockedExchange(reinterpret_cast<volatile LONG*>(&view_->sequence), 0);
        }
        frame.tickMs = tick; frame.processId = GetCurrentProcessId();
        InterlockedIncrement(reinterpret_cast<volatile LONG*>(&view_->sequence));
        MemoryBarrier();
        std::memcpy(reinterpret_cast<uint8_t*>(view_) + 4, reinterpret_cast<const uint8_t*>(&frame) + 4, sizeof(frame) - 4);
        MemoryBarrier();
        InterlockedIncrement(reinterpret_cast<volatile LONG*>(&view_->sequence));
#endif
    }
};

inline void PublishNativeInput(int hand, NativeInputFrame frame) {
    static NativeInputPublisher publishers[2];
    if (hand >= 0 && hand < 2) publishers[hand].Publish(hand, frame);
}
}
