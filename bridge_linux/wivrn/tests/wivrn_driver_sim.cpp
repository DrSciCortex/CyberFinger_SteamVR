/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// wivrn_driver_sim.cpp — the WiVRn driver without WiVRn or a headset
//
//   wivrn_driver_sim [--haptics] [--seconds N] [settings.json]
//
// Runs the driver (CfDriver) on its usual UDP ports with two made-up hands in
// front of a made-up headset, waving and curling their fingers, so the bridge
// can be tried against it: start the bridge in VR mode and the hands appear in
// its Skeleton tab, the driver takes each hand a CyberFinger reports for, and
// what it makes of the buttons is printed here once a second. --haptics sends
// a vibration to each held hand every two seconds, as an application would.
// Stop WiVRn first: the ports are the same.
// ═══════════════════════════════════════════════════════════════════════════

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include "CfDriver.h"
#include "SyntheticHand.h"
#include "Utils.h"

namespace cf {
void DriverLog(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::vprintf(fmt, ap);
    va_end(ap);
    std::fflush(stdout);
}
} // namespace cf

using namespace cf;

static std::atomic<bool> g_run{ true };

int main(int argc, char** argv) {
    bool haptics = false;
    double seconds = 1e9;
    std::string config = DefaultConfigPath();
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--haptics") haptics = true;
        else if (a == "--seconds" && i + 1 < argc) seconds = std::atof(argv[++i]);
        else config = a;
    }
    std::signal(SIGINT, [](int) { g_run = false; });
    std::signal(SIGTERM, [](int) { g_run = false; });

    Driver driver(config, "");                           // no state folder: the real calibration stays untouched
    const double start = NowSeconds();
    double nextPrint = start + 1.0, nextHaptic = start + 2.0;
    while (g_run && NowSeconds() - start < seconds) {
        const double now = NowSeconds(), t = now - start;
        TrackedPose head;
        head.positionValid = head.orientationValid = head.positionTracked = head.orientationTracked = true;
        head.pose = { {}, { 0, 1.6, 0 } };
        for (int hand = 0; hand < 2; ++hand) {
            const double side = hand ? 1.0 : -1.0;
            // In front of the chest, palms down, drifting and turning a little; the fingers curl in a wave.
            TrackedPose grip = head;
            grip.pose = { QuatFromEulerXYZDeg(20 * std::sin(0.7 * t), side * 15 * std::sin(0.5 * t), 0),
                          { side * 0.2 + 0.03 * std::sin(0.9 * t), 1.25 + 0.03 * std::sin(1.3 * t), -0.4 } };
            const Xform wrist = grip.pose * Xform{ {}, { 0, 0.02, 0.08 } };
            float curl[5];
            for (int f = 0; f < 5; ++f) curl[f] = float(0.5 + 0.5 * std::sin(1.5 * t - 0.6 * f));
            Xform joints[kNumJoints];
            MakeJoints(hand == 1, curl, joints);
            Hand& h = driver.GetHand(hand);
            h.OnHead(head);
            h.OnGrip(now, now, now, grip);
            h.OnJoints(now, true, wrist, joints);
            const bool held = driver.HoldsRole(hand, true);
            if (held && haptics && now >= nextHaptic) driver.Haptic(hand, 0.2f, 160.f, 0.6f);
        }
        if (now >= nextHaptic) nextHaptic = now + 2.0;
        if (now >= nextPrint) {
            nextPrint = now + 1.0;
            for (int hand = 0; hand < 2; ++hand) {
                const Hand& h = driver.GetHand(hand);
                const ControllerInputs in = h.Inputs();
                std::printf("%-5s %-8s trigger %.2f%s squeeze %.0f stick %+.2f %+.2f%s a %d b %d menu %d\n",
                            hand ? "right" : "left", h.Active() ? "held" : "released", in.trigger,
                            in.triggerClick ? "*" : " ", in.squeeze, in.stickX, in.stickY, in.stickClick ? "*" : " ",
                            int(in.a), int(in.b), int(in.menu));
            }
            std::fflush(stdout);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(11));
    }
    return 0;
}
