// SPDX-License-Identifier: GPL-3.0-only
// No SteamVR runtime or live named mappings are touched by this test.
#include "NativeInput.h"
#include <cstdio>
#include <fstream>
#include <limits>

int main(int argc, char** argv) {
    int checks = 0;
    auto check = [&](bool ok, const char* name) {
        if (!ok) { std::fprintf(stderr, "FAIL %s\n", name); std::exit(1); }
        ++checks; std::printf("PASS %s\n", name);
    };
    cf::CyberFingerState keys;
    keys.valid = true; keys.buttons = cf::kBtnGrip | cf::kBtnMenu;
    keys.triggerAnalog = true; keys.trigger = .4f;
    cf::HandStateSample fused;
    fused.valid = true; fused.arrival = 10;
    fused.pkt.h.flags = cf::kHsCameraSees | cf::kHsPoseValid;
    fused.pkt.h.age_us = 5000; fused.pkt.pose_conf = 1;
    for (auto& confidence : fused.pkt.finger_conf) confidence = 1;
    cf::TapHandSnapshot tap;
    tap.skeletonValid = true; tap.skeletonAge = .005; tap.boneCount = cf::eBone_Count;
    cf::HandGestures gestures;
    for (auto& curl : gestures.curls) curl = .8f;
    gestures.pinch[0] = .2f;
    auto make = [&](uint8_t mode, bool fresh = true) {
        return cf::MakeNativeInput(3, mode, true, true, fresh, keys, fused, tap, gestures, 10, 1);
    };
    auto frame = make(cf::kModeFused);
    check(frame.flags == 31 && frame.fingerAgeMs == 5, "fused age and optical evidence");
    check(frame.buttons == 34 && frame.trigger == .4f, "physical quick controls");
    check(frame.curls[4] == .8f && frame.pinches[0] == .2f, "gesture fields");
    check(make(cf::kModePassthrough).flags == 31, "fresh optical passthrough");
    tap.skeletonAge = .3;
    check((make(cf::kModePassthrough).flags & (4 | 16)) == 0, "stale optical skeleton cannot act");
    fused.pkt.finger_conf[2] = .2f;
    check((make(cf::kModeFused).flags & 4) == 0, "weak finger evidence cannot act");
    fused.pkt.finger_conf[2] = 1;
    auto staleKeys = make(cf::kModeFused, false);
    check(!(staleKeys.flags & 8) && !staleKeys.buttons && staleKeys.trigger == 0, "stale keys neutralized");
    check((make(cf::kModeNoPose).flags & 4) == 0, "inferred controller curls do not become measured fingers");
    // A real C++ layout fixture for the .NET ABI test, separate from mappings.
    frame.sequence = 2; frame.tickMs = 1000; frame.processId = 42;
    if (argc > 1) {
        std::ofstream out(argv[1], std::ios::binary);
        out.write(reinterpret_cast<const char*>(&frame), sizeof(frame));
        check(bool(out), "C++ native frame fixture written");
    }
    std::printf("%d native driver checks passed.\n", checks);
}
