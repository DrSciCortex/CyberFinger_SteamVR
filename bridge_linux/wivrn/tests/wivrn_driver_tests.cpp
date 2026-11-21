/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// wivrn_driver_tests.cpp — the WiVRn driver without WiVRn
//
// Everything below CfWivrn.cpp (which only converts WiVRn's types):
// 1. Settings: the JSON file, button names.
// 2. Inputs: the CyberFinger's buttons as the emulated controller's, tap to
//    hold, the long press, FluxActions.
// 3. Hand joints as bones, and the gestures read from them.
// 4. Poses: the IMU fusion on the headset's predicted grip poses, the hand
//    turning as one piece, a lost hand keeping its place.
// 5. The driver end to end over its UDP ports: CFG2 in, the role taken and
//    given back, CFOP, CFHP and FluxActions out, the settings file read again.
// ═══════════════════════════════════════════════════════════════════════════

#include <arpa/inet.h>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include "CfConfig.h"
#include "CfDriver.h"
#include "CfHand.h"
#include "SkeletonSynth.h"
#include "SyntheticHand.h"
#include "Utils.h"

namespace cf {
static bool g_verbose = false;
void DriverLog(const char* fmt, ...) {
    if (!g_verbose) return;
    va_list ap;
    va_start(ap, fmt);
    std::vprintf(fmt, ap);
    va_end(ap);
}
} // namespace cf

using namespace cf;

static int g_failures = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)
#define CHECK_NEAR(a, b, tol) CHECK(std::fabs(double(a) - double(b)) <= (tol))

static double AngleDeg(const Quat& a, const Quat& b) {
    const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
    return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
}

static TrackedPose Tracked(const Xform& x) {
    TrackedPose t;
    t.positionValid = t.orientationValid = t.positionTracked = t.orientationTracked = true;
    t.pose = x;
    return t;
}

// ── 1. settings ────────────────────────────────────────────────────────────

static void TestConfig() {
    CHECK(ButtonMask("STSEL") == kBtnStartSelect);
    CHECK(ButtonMask(" menu | C ") == (kBtnMenu | kBtnC));
    CHECK(ButtonMask("PINK") == kMaskPink);
    CHECK(ButtonMask("NONE") == 0);
    std::string unknown;
    CHECK(ButtonMask("MENU|bogus", &unknown) == kBtnMenu && unknown == "bogus");

    Config c;
    std::string warnings;
    CHECK(ParseConfig("{}", c, warnings) && warnings.empty());
    CHECK(c.active && c.profile == "touch" && c.listenPort == 27015 && c.contextPort == 27016);
    CHECK(c.maskA == kBtnStartSelect && c.maskB == kBtnMenu && c.maskMenu == kMaskPink && c.maskSystem == 0);
    CHECK(c.fluxAHold == 1 && c.fluxC == 3 && c.fluxPinkRight == 42);
    CHECK(c.statusLog && !c.gestureLog);

    CHECK(ParseConfig(R"({
        // comments are fine
        "active": false, "profile": "index", "handtracking_udp_port": 37015,
        "button_a": "C", "button_b": "MENU|D", "button_menu": "NONE", "button_system": "PINK",
        "black_hold_ms": 0, "grab_tap_to_hold": false, "grab_tap_ms": 350, "release_after_ms": 500,
        "imu_fusion": false, "pose_rotation_prediction": 3,
        "grip_offset_m": [0.01, -0.02, 0.03], "grip_angle_deg": [90, 0, 0],
        "flux_port": 0, "flux_actions": { "c": 0, "pink_right": 7 },
        "status_log": false, "gesture_log": true
    })", c, warnings));
    CHECK(warnings.empty());
    CHECK(!c.active && c.profile == "index" && c.listenPort == 37015 && c.contextPort == 27016);
    CHECK(c.maskA == kBtnC && c.maskB == (kBtnMenu | kBtnD) && c.maskMenu == 0 && c.maskSystem == kMaskPink);
    CHECK(c.blackHoldTime == 0 && !c.grabTapToHold && !c.imuFusion);
    CHECK_NEAR(c.grabTapTime, 0.35, 1e-9);
    CHECK_NEAR(c.releaseAfter, 0.5, 1e-9);
    CHECK_NEAR(c.rotationPrediction, 1.0, 1e-9);                      // clamped
    CHECK_NEAR(c.gripOffset.p.y, -0.02, 1e-9);
    CHECK_NEAR(AngleDeg(c.gripOffset.q, QuatFromEulerXYZDeg(90, 0, 0)), 0, 1e-6);
    CHECK(c.fluxPort == 0 && c.fluxC == 0 && c.fluxPinkRight == 7 && c.fluxD == 5);
    CHECK(!c.statusLog && c.gestureLog);

    // A wrong type or an unknown button is a warning, and the key keeps its value; not JSON leaves it all alone.
    Config d;
    warnings.clear();
    CHECK(ParseConfig(R"({ "active": "yes", "button_a": "FOO", "grip_offset_m": [1, 2], "flux_actions": 3 })", d,
                      warnings));
    CHECK(d.active && d.maskA == 0);
    CHECK(warnings.find("active") != std::string::npos && warnings.find("FOO") == std::string::npos &&
          warnings.find("foo") != std::string::npos && warnings.find("grip_offset_m") != std::string::npos &&
          warnings.find("flux_actions") != std::string::npos);
    Config e;
    e.profile = "kept";
    CHECK(!ParseConfig("not json", e, warnings) && e.profile == "kept");
    CHECK(!ParseConfig("[1, 2]", e, warnings));
}

// ── 2. inputs ──────────────────────────────────────────────────────────────

struct InputRig {
    Hand hand;
    CyberFingerState state;
    std::vector<FluxEvent> flux;
    double t = 100.0;

    explicit InputRig(int which, const Config& cfg = {}) : hand(which) {
        hand.SetConfig(cfg);
        hand.SetActive(true);
        state.valid = true;
        state.triggerAnalog = true;
    }
    // The CyberFinger reports `buttons` (pink: the extension byte) for `seconds`.
    ControllerInputs Run(uint16_t buttons, double seconds) {
        state.buttons = uint8_t(buttons);
        state.buttons2 = (buttons & kMaskPink) ? kBtn2Pink : 0;
        for (const double end = t + seconds; t < end; t += 0.005) {
            state.time = t;
            hand.Tick(state, 1.0, t, flux);
        }
        return hand.Inputs();
    }
    bool Took(int action, bool pressed) {
        for (size_t i = 0; i < flux.size(); ++i) {
            if (flux[i].action != action || flux[i].pressed != pressed) continue;
            flux.erase(flux.begin() + std::ptrdiff_t(i));
            return true;
        }
        return false;
    }
};

static void TestInputs() {
    {   // the plain buttons, left hand
        InputRig r(0);
        ControllerInputs in = r.Run(0, 0.05);
        CHECK(in.trigger == 0 && !in.triggerClick && in.squeeze == 0 && !in.a && !in.b && !in.menu && !in.stickClick);
        r.state.trigger = 0.4f;
        r.state.joyX = -0.5f;
        r.state.joyY = 0.25f;
        in = r.Run(0, 0.05);
        CHECK_NEAR(in.trigger, 0.4, 1e-6);
        CHECK(!in.triggerClick);
        CHECK_NEAR(in.stickX, -0.5, 1e-6);
        CHECK_NEAR(in.stickY, 0.25, 1e-6);
        r.state.trigger = 0;
        in = r.Run(kBtnTrigger | kBtnStickClick | kBtnMenu | kMaskPink, 0.05);
        CHECK(in.trigger == 1 && in.triggerClick && in.stickClick && in.b && in.menu && !in.a);
        CHECK(r.flux.empty());                         // the left pink button is the menu button, no FluxAction
    }
    {   // the right pink button: FluxAction42, not the system button, unless the settings say so
        InputRig r(1);
        ControllerInputs in = r.Run(kMaskPink, 0.05);
        CHECK(!in.menu && r.Took(42, true));
        r.Run(0, 0.05);
        CHECK(r.Took(42, false) && r.flux.empty());
        Config cfg;
        cfg.maskSystem = kMaskPink;
        InputRig s(1, cfg);
        in = s.Run(kMaskPink, 0.05);
        CHECK(in.menu && s.flux.empty());
    }
    {   // the grip: tap to hold
        InputRig r(0);
        CHECK(r.Run(kBtnGrip, 0.1).squeeze == 1);      // a tap …
        CHECK(r.Run(0, 0.5).squeeze == 1);             // … holds
        CHECK(r.Run(kBtnGrip, 0.1).squeeze == 1);      // the next press …
        CHECK(r.Run(0, 0.1).squeeze == 0);             // … lets go
        CHECK(r.Run(kBtnGrip, 0.6).squeeze == 1);      // a long press grabs while held
        CHECK(r.Run(0, 0.1).squeeze == 0);
        Config cfg;
        cfg.grabTapToHold = false;
        InputRig plain(0, cfg);
        CHECK(plain.Run(kBtnGrip, 0.1).squeeze == 1);
        CHECK(plain.Run(0, 0.1).squeeze == 0);
        // switched off while latched: let go
        r.Run(kBtnGrip, 0.1);
        CHECK(r.Run(0, 0.1).squeeze == 1);
        r.hand.SetConfig(cfg);
        CHECK(r.Run(0, 0.05).squeeze == 0);
    }
    {   // the black button: a click on release, or held, FluxAction 1 (left) / 2 (right) and no click
        InputRig r(0);
        CHECK(!r.Run(kBtnStartSelect, 0.2).a);         // held back while it could become a long press
        r.Run(0, 0.01);
        CHECK(r.hand.Inputs().a);                      // released early: the click, now
        CHECK(!r.Run(0, 0.2).a);
        CHECK(r.flux.empty());
        bool anyA = false;
        for (int i = 0; i < 30; ++i) anyA |= r.Run(kBtnStartSelect, 0.05).a;
        CHECK(!anyA && r.Took(1, true) && r.flux.empty());   // 1.5 s: the long press, held
        CHECK(!r.Run(0, 0.2).a && r.Took(1, false));
        InputRig right(1);
        right.Run(kBtnStartSelect, 1.0);
        CHECK(right.Took(2, true));
        Config cfg;
        cfg.blackHoldTime = 0;                         // no long press: A as pressed
        InputRig plain(0, cfg);
        CHECK(plain.Run(kBtnStartSelect, 0.05).a);
        plain.Run(kBtnStartSelect, 1.5);
        CHECK(plain.flux.empty());
    }
    {   // C, D, E: FluxActions 3, 5, 7 (+1 on the right), unless a controller button took them
        InputRig r(1);
        r.Run(kBtnC | kBtnE, 0.05);
        CHECK(r.Took(4, true) && r.Took(8, true) && r.flux.empty());
        r.Run(kBtnD, 0.05);
        CHECK(r.Took(4, false) && r.Took(8, false) && r.Took(6, true) && r.flux.empty());
        Config cfg;
        cfg.maskB = kBtnD;
        cfg.fluxPort = 0;
        r.hand.SetConfig(cfg);                         // FluxActions off: what was pressed is released
        CHECK(r.Run(kBtnD, 0.05).b && r.Took(6, false) && r.flux.empty());
        cfg.fluxPort = 42042;
        r.hand.SetConfig(cfg);
        r.Run(kBtnD | kBtnC, 0.05);
        CHECK(r.Took(4, true) && r.flux.empty());      // D is B now, not a FluxAction
    }
    {   // the CyberFinger falls silent: everything lets go
        InputRig r(0);
        r.state.trigger = 0.8f;
        r.Run(kBtnGrip | kBtnC, 0.1);
        CHECK(r.Run(kBtnC, 0.1).squeeze == 1 && r.Took(3, true));
        for (double end = r.t + 0.5; r.t < end; r.t += 0.005) r.hand.Tick(r.state, 1.0, r.t, r.flux);   // no new reports
        const ControllerInputs in = r.hand.Inputs();
        CHECK(in.trigger == 0 && in.squeeze == 0 && r.Took(3, false));
    }
    {   // not this hand's turn (a controller in it, or switched off): nothing is pressed
        InputRig r(0);
        r.hand.SetActive(false);
        const ControllerInputs in = r.Run(kBtnTrigger | kBtnC, 0.1);
        CHECK(in.trigger == 0 && !in.triggerClick && r.flux.empty());
    }
}

// ── 3. joints, bones, gestures ─────────────────────────────────────────────

static void TestBones() {
    const float curl[5] = { 0.2f, 0.9f, 0.1f, 0.5f, 0.7f };
    Xform joints[kNumJoints];
    MakeJoints(true, curl, joints);
    const Xform raw{ QuatFromEulerXYZDeg(10, 40, -25), { 0.3, 1.2, -0.4 } };
    const Xform wrist = raw * Xform{ QuatFromEulerXYZDeg(-20, 5, 60), { 0.01, 0.02, 0.09 } };
    vr::VRBoneTransform_t bones[eBone_Count];
    BonesFromJoints(raw, wrist, joints, bones);

    // Forward kinematics over the bones puts every joint back where it was.
    Xform model[eBone_Count];
    ModelSpace(bones, model);
    double worst = 0;
    for (int j = 1; j < kNumJoints; ++j) {
        const Vec3 want = (wrist * joints[j]).p, got = (raw * model[j]).p;
        worst = std::fmax(worst, Length(want - got));
    }
    CHECK(worst < 1e-5);
    for (int f = 0; f < 5; ++f)                         // the aux bones: each finger's last knuckle, on the root
        CHECK(Length(BoneToXform(bones[eBone_Aux_Thumb + f]).p - model[kAuxSource[f]].p) < 1e-6);

    float curls[5];
    CurlsFromBones(bones, curls);
    for (int f = 1; f < 5; ++f) CHECK_NEAR(curls[f], curl[f] * 240.0 / 250.0, 0.02);
}

static void TestGestures() {
    // An index point (the index out, the others folded) turns FluxAction 40 on (left; 41 right), debounced; a
    // two-finger point 36.
    for (int which = 0; which < 2; ++which) {
        Hand hand(which);
        hand.SetConfig({});
        hand.SetActive(true);
        CyberFingerState state;
        state.valid = true;
        std::vector<FluxEvent> flux;
        const Xform grip{ QuatFromEulerXYZDeg(0, 30, 0), { 0.2, 1.1, -0.3 } };
        const Xform wrist = grip * Xform{ {}, { 0, 0, 0.08 } };
        double t = 50.0;
        auto run = [&](const float curl[5], double seconds) {
            Xform joints[kNumJoints];
            MakeJoints(which == 1, curl, joints);
            for (const double end = t + seconds; t < end; t += 0.005) {
                hand.OnGrip(t, t, t, Tracked(grip));
                hand.OnJoints(t, true, wrist, joints);
                state.time = t;
                hand.Tick(state, 1.0, t, flux);
            }
        };
        const float open[5] = { 0, 0, 0, 0, 0 }, point[5] = { 0.8f, 0, 1, 1, 1 }, two[5] = { 0.8f, 0, 0, 1, 1 };
        run(open, 0.3);
        CHECK(flux.empty());
        run(point, 0.03);
        CHECK(flux.empty());                            // not yet: it must hold for 60 ms
        run(point, 0.2);
        CHECK(flux.size() == 1 && flux[0].action == 40 + which && flux[0].pressed);
        flux.clear();
        run(two, 0.4);
        CHECK(flux.size() == 2);
        bool pointOff = false, twoOn = false;
        for (const FluxEvent& e : flux) {
            pointOff |= e.action == 40 + which && !e.pressed;
            twoOn |= e.action == 36 + which && e.pressed;
        }
        CHECK(pointOff && twoOn);
        flux.clear();
        // The hand is lost: the gesture ends.
        for (const double end = t + 0.5; t < end; t += 0.005) {
            hand.OnJoints(t, false, {}, nullptr);
            state.time = t;
            hand.Tick(state, 1.0, t, flux);
        }
        CHECK(flux.size() == 1 && flux[0].action == 36 + which && !flux[0].pressed);
    }
}

// ── 4. poses ───────────────────────────────────────────────────────────────

// A synthetic hand as in tests/driver_tests.cpp's TestImuFusion, through the Hand: the joint IMU at 100 Hz, the
// headset's grip pose at 90 Hz, each sent for two times ahead (20 and 45 ms, as applications ask) and trailing
// the true hand by `lag`. The further prediction is made useless on purpose: the driver must go by the nearer.
static void TestPoses() {
    const double alpha = 40.0 * kPi / 180.0, lag = 0.03;
    const Quat yaw{ std::cos(alpha / 2), 0, std::sin(alpha / 2), 0 };
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    auto truth = [](double t) {
        return QuatFromEulerXYZDeg(30 * std::sin(2 * kPi * 0.13 * t) + 12 * std::sin(2 * kPi * 0.7 * t),
                                   25 * std::sin(2 * kPi * 0.17 * t + 1.0),
                                   35 * std::sin(2 * kPi * 0.11 * t + 2.0) + 10 * std::sin(2 * kPi * 0.9 * t));
    };
    auto place = [](double t) { return Vec3{ 0.2 + 0.1 * std::sin(t), 1.1, -0.3 + 0.05 * std::cos(0.7 * t) }; };
    auto optical = [&](double t) { return Xform{ truth(t - lag), place(t) }; };   // what the headset says for t
    const Xform gripToAim{ QuatFromEulerXYZDeg(-35, 0, 0), { 0, 0.02, -0.05 } };
    const Xform gripToWrist{ QuatFromEulerXYZDeg(5, 10, 80), { 0.01, 0.03, 0.09 } };

    ImuFusion fusion;
    fusion.Reset();
    Hand hand(1);
    Config cfg;
    cfg.occlusionHold = 2.0;
    hand.SetConfig(cfg);
    hand.SetImuFusion(&fusion);
    hand.SetActive(true);
    CyberFingerState state;
    std::vector<FluxEvent> flux;

    const double lostAt = 30.0, end = 34.0;
    double tImu = 0, tOpt = 0, tTick = 0;
    double worstSeen = 0, worstOptical = 0, worstHidden = 0, worstRigid = 0, worstPlace = 0;
    bool heldAfter = false, untouchedEarly = true;
    Vec3 lastPlace;
    for (double t = 0; t < end; t += 0.001) {
        while (tImu <= t) {
            fusion.AddImu(tImu, Normalize(Conj(yaw * toVr) * truth(tImu) * Conj(mount)));
            tImu += 0.01;
        }
        if (tOpt <= t) {
            tOpt += 1.0 / 90;
            if (t < lostAt) {
                hand.OnGrip(t + 0.020, t, t, Tracked(optical(t + 0.020)));
                TrackedPose far = Tracked(optical(t + 0.045));
                far.pose.q = QuatFromEulerXYZDeg(170 * std::sin(40 * t), 90, 0);
                hand.OnGrip(t + 0.045, t, t, far);
            } else {
                hand.OnGrip(t + 0.020, t, t, TrackedPose{});              // the headset lost the hand
            }
        }
        if (tTick <= t) {
            tTick += 0.005;
            hand.Tick(state, 1.0, t, flux);

            // An application asks for the grip and aim poses and the joints, for now.
            const bool seen = t < lostAt;
            const TrackedPose gripIn = seen ? Tracked(optical(t)) : TrackedPose{};
            TrackedPose grip = gripIn;
            TrackedPose aim = seen ? Tracked(optical(t) * gripToAim) : TrackedPose{};
            TrackedPose wrist = seen ? Tracked(optical(t) * gripToWrist) : TrackedPose{};
            hand.AdjustPose(PoseKind::Grip, t, t, grip, gripIn);
            hand.AdjustPose(PoseKind::Aim, t, t, aim, gripIn);
            hand.AdjustJoints(t, t, wrist, gripIn);
            if (t < 1.0 && AngleDeg(grip.pose.q, gripIn.pose.q) > 1e-4) untouchedEarly = false;
            if (seen && t > 20.0) {
                CHECK(hand.ImuFused());
                worstSeen = std::fmax(worstSeen, AngleDeg(grip.pose.q, truth(t)));
                worstOptical = std::fmax(worstOptical, AngleDeg(gripIn.pose.q, truth(t)));
                worstPlace = std::fmax(worstPlace, Length(grip.pose.p - place(t)));
                // The aim pose and the wrist keep their place on the grip: the hand turns as one piece.
                const Xform aimRel = Inverse(grip.pose) * aim.pose, wristRel = Inverse(grip.pose) * wrist.pose;
                worstRigid = std::fmax(worstRigid, Length(aimRel.p - gripToAim.p) + Length(wristRel.p - gripToWrist.p));
                worstRigid = std::fmax(worstRigid, (AngleDeg(aimRel.q, gripToAim.q) +
                                                    AngleDeg(wristRel.q, gripToWrist.q)) * kPi / 180.0);
                lastPlace = grip.pose.p;
            } else if (!seen && t < lostAt + cfg.occlusionHold - 0.05) {
                // Lost: the grip keeps its last place, turning with the IMU; guessed, so not "tracked".
                CHECK(grip.Valid() && grip.orientationTracked && !grip.positionTracked);
                CHECK(aim.Valid());
                CHECK(!wrist.Valid());                                    // the joints are the headset's alone
                worstHidden = std::fmax(worstHidden, AngleDeg(grip.pose.q, truth(t)));
                worstPlace = std::fmax(worstPlace, Length(grip.pose.p - lastPlace));
            } else if (!seen && t > lostAt + cfg.occlusionHold + 0.05) {
                heldAfter |= grip.Valid() || aim.Valid();
            }
        }
    }
    const ImuFusion::Status st = fusion.GetStatus();
    CHECK(st.calibrated);
    CHECK_NEAR(st.alphaDeg, 40.0, 3.0);
    CHECK(untouchedEarly);                               // before the calibration the poses are the headset's
    CHECK(worstOptical > 2.0);                           // the headset's own orientation trails the hand …
    CHECK(worstSeen < 1.0);                              // … the fused one doesn't
    CHECK(worstHidden < 2.0);
    CHECK(worstPlace < 1e-6);                            // the position stays the headset's
    CHECK(worstRigid < 1e-5);
    CHECK(!heldAfter);                                   // after occlusion_hold_ms the hand is lost
    std::printf("  poses: heading %.1f deg, lag %.1f ms; off the true hand: headset %.1f, fused %.2f, hidden %.2f deg\n",
                st.alphaDeg, st.lag * 1e3, worstOptical, worstSeen, worstHidden);

    // The grip offset moves the controller's poses, not the hand's.
    {
        Hand h(0);
        Config c;
        c.imuFusion = false;
        c.gripOffset = { QuatFromEulerXYZDeg(0, 0, 90), { 0, 0, 0.1 } };
        h.SetConfig(c);
        h.SetActive(true);
        const Xform g{ QuatFromEulerXYZDeg(0, 90, 0), { 1, 1, 1 } };
        TrackedPose grip = Tracked(g), aim = Tracked(g * gripToAim), poke = Tracked(g * gripToAim);
        h.AdjustPose(PoseKind::Grip, 0, 0, grip, Tracked(g));
        h.AdjustPose(PoseKind::Aim, 0, 0, aim, Tracked(g));
        h.AdjustPose(PoseKind::Poke, 0, 0, poke, Tracked(g));
        CHECK(Length(grip.pose.p - (g * c.gripOffset).p) < 1e-9);
        CHECK(Length(aim.pose.p - (g * c.gripOffset * gripToAim).p) < 1e-9);
        CHECK(Length(poke.pose.p - (g * gripToAim).p) < 1e-9);
        TrackedPose wrist = Tracked(g * gripToWrist);
        h.AdjustJoints(0, 0, wrist, Tracked(g));
        CHECK(Length(wrist.pose.p - (g * gripToWrist).p) < 1e-9);
        // Without the IMU a lost hand is simply lost.
        TrackedPose lost;
        h.AdjustPose(PoseKind::Grip, 0.1, 0.1, lost, TrackedPose{});
        CHECK(!lost.Valid());
    }
    // A hand the CyberFinger doesn't hold is left alone.
    {
        Hand h(0);
        Config c;
        c.gripOffset.p = { 0, 0, 0.1 };
        h.SetConfig(c);
        const Xform g{ {}, { 1, 1, 1 } };
        TrackedPose grip = Tracked(g);
        h.AdjustPose(PoseKind::Grip, 0, 0, grip, Tracked(g));
        CHECK(Length(grip.pose.p - g.p) < 1e-12);
    }
}

// ── 5. the driver over UDP ─────────────────────────────────────────────────

struct Udp {
    int fd = -1;
    explicit Udp(int port = 0) {
        fd = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        timeval tv{ 0, 20000 };
        ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        if (port) {
            sockaddr_in a{};
            a.sin_family = AF_INET;
            a.sin_port = htons(uint16_t(port));
            a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            if (::bind(fd, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0) {
                ::close(fd);
                fd = -1;
            }
        }
    }
    ~Udp() { if (fd >= 0) ::close(fd); }
    void Send(int port, const void* data, size_t size) const {
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons(uint16_t(port));
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        ::sendto(fd, data, size, 0, reinterpret_cast<sockaddr*>(&a), sizeof(a));
    }
    // Every datagram that arrives within `seconds`.
    std::vector<std::vector<uint8_t>> Drain(double seconds) const {
        std::vector<std::vector<uint8_t>> out;
        const double end = NowSeconds() + seconds;
        uint8_t buf[4096];
        while (NowSeconds() < end) {
            const ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
            if (n > 0) out.emplace_back(buf, buf + n);
        }
        return out;
    }
};

static void TestDriver(const std::string& dir) {
    const int listen = 37015, context = 37016, fluxPort = 37042;
    const std::string configPath = dir + "/wivrn.json";
    auto writeConfig = [&](bool active) {
        std::ofstream f(configPath, std::ios::trunc);
        f << "{ \"active\": " << (active ? "true" : "false") << ", \"handtracking_udp_port\": " << listen
          << ", \"context_udp_port\": " << context << ", \"flux_port\": " << fluxPort
          << ", \"release_after_ms\": 300, \"status_log\": false }\n";
    };
    writeConfig(true);
    Udp fromDriver(context), fluxIn(fluxPort), bridge;
    CHECK(fromDriver.fd >= 0 && fluxIn.fd >= 0);
    if (fromDriver.fd < 0 || fluxIn.fd < 0) return;      // the test ports are taken

    Driver driver(configPath, dir);
    CHECK(driver.GetConfig().listenPort == listen && driver.ConfigGeneration() == 1);
    std::this_thread::sleep_for(std::chrono::milliseconds(150));   // the link's thread binds its port
    CHECK(!driver.HoldsRole(0, true));                   // no CyberFinger yet

    uint32_t seq = 0;
    auto report = [&](int hand, uint8_t buttons) {
        CyberFingerPacket p{};
        p.h.magic = kMagicCyberFinger;
        p.h.version = kVersion;
        p.h.hand = uint8_t(hand);
        p.h.seq = ++seq;
        p.h.t_send_us = NowMicros();
        p.buttons = buttons;
        p.trigger = 128;
        p.joy_x = 16384;
        p.battery_pct = 80;
        bridge.Send(listen, &p, sizeof(p));
    };
    // The left CyberFinger reports; `hold`: the headset sees the bare hand.
    auto run = [&](uint8_t buttons, double seconds, bool handsFree = true) {
        bool held = false;
        for (const double end = NowSeconds() + seconds; NowSeconds() < end;) {
            report(0, buttons);
            std::this_thread::sleep_for(std::chrono::milliseconds(8));
            held = driver.HoldsRole(0, handsFree);
        }
        return held;
    };

    CHECK(run(0, 0.1));                                  // the CyberFinger takes the hand
    CHECK(!driver.HoldsRole(1, true));                   // the other hand has none
    CHECK(!run(0, 0.05, false));                         // a controller picked up: back to it
    CHECK(run(kBtnC, 0.1));
    const ControllerInputs in = driver.GetHand(0).Inputs();
    CHECK_NEAR(in.trigger, 128 / 255.0, 1e-3);
    CHECK_NEAR(in.stickX, 0.5, 1e-3);

    // C pressed and released: FluxAction3 to the mod's port.
    run(0, 0.1);
    std::vector<std::string> texts;
    for (const auto& d : fluxIn.Drain(0.1)) texts.emplace_back(d.begin(), d.end());
    CHECK(texts.size() == 2 && texts[0] == "FluxAction3.Pressed" && texts[1] == "FluxAction3.Released");

    // The context stream and a haptic request reach the bridge's port.
    fromDriver.Drain(0.05);
    driver.Haptic(0, 0.25f, 160.f, 0.5f);
    int contexts = 0, haptics = 0;
    ContextPacket ctx{};
    HapticPacket hap{};
    for (double end = NowSeconds() + 0.3; NowSeconds() < end;) {
        report(0, 0);
        driver.HoldsRole(0, true);
        for (const auto& d : fromDriver.Drain(0.02)) {
            uint32_t magic = 0;
            std::memcpy(&magic, d.data(), 4);
            if (magic == kMagicContext && d.size() == sizeof(ContextPacket)) {
                std::memcpy(&ctx, d.data(), sizeof(ctx));
                ++contexts;
            } else if (magic == kMagicHaptic && d.size() == sizeof(HapticPacket)) {
                std::memcpy(&hap, d.data(), sizeof(hap));
                ++haptics;
            }
        }
    }
    CHECK(contexts > 15 && contexts < 45);               // ~100 Hz for 0.3 s
    CHECK(ctx.h.version == kVersion && ctx.h.hand == 0xFF);
    CHECK(ctx.mode_left == kModeNoPose && ctx.mode_right == kModeReleased);   // held, but no tracking here
    CHECK(!ctx.tap[0].pose_valid && !ctx.tap[0].skel_valid);
    CHECK(haptics == 1 && hap.h.hand == 0);
    CHECK_NEAR(hap.duration_s, 0.25, 1e-6);
    CHECK_NEAR(hap.frequency_hz, 160, 1e-6);
    CHECK_NEAR(hap.amplitude, 0.5, 1e-6);

    // With tracking, the context stream carries the hand: FK over its bones gives the joints back.
    const Xform grip{ QuatFromEulerXYZDeg(0, 30, 0), { 0.2, 1.1, -0.3 } };
    const Xform wrist = grip * Xform{ {}, { 0, 0, 0.08 } };
    const float curl[5] = { 0.1f, 0.2f, 0.3f, 0.4f, 0.5f };
    Xform joints[kNumJoints];
    MakeJoints(false, curl, joints);
    fromDriver.Drain(0.02);
    for (double end = NowSeconds() + 0.1; NowSeconds() < end;) {
        report(0, 0);
        const double now = NowSeconds();
        driver.GetHand(0).OnGrip(now, now, now, Tracked(grip));
        driver.GetHand(0).OnHead(Tracked({ {}, { 0, 1.6, 0 } }));
        driver.GetHand(0).OnJoints(now, true, wrist, joints);
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    bool gotHand = false;
    for (const auto& d : fromDriver.Drain(0.05)) {
        if (d.size() != sizeof(ContextPacket)) continue;
        std::memcpy(&ctx, d.data(), sizeof(ctx));
        gotHand |= ctx.tap[0].skel_valid != 0;
        if (ctx.tap[0].skel_valid) break;
    }
    CHECK(gotHand);
    if (gotHand) {
        CHECK(ctx.mode_left == kModePassthrough && ctx.tap[0].pose_valid && ctx.tap[0].bone_count == eBone_Count);
        CHECK(ctx.hmd_valid);
        CHECK_NEAR(ctx.hmd_pos[1], 1.6, 1e-6);
        CHECK_NEAR(ctx.tap[0].raw_pos[0], 0.2, 1e-6);
        vr::VRBoneTransform_t bones[eBone_Count];
        std::memcpy(bones, ctx.tap[0].bones, sizeof(bones));
        Xform model[eBone_Count];
        ModelSpace(bones, model);
        const Xform raw{ { ctx.tap[0].raw_rot[0], ctx.tap[0].raw_rot[1], ctx.tap[0].raw_rot[2], ctx.tap[0].raw_rot[3] },
                         { ctx.tap[0].raw_pos[0], ctx.tap[0].raw_pos[1], ctx.tap[0].raw_pos[2] } };
        double worst = 0;
        for (int j = 1; j < kNumJoints; ++j) worst = std::fmax(worst, Length((raw * model[j]).p - (wrist * joints[j]).p));
        CHECK(worst < 1e-4);
    }

    // The CyberFinger falls silent: after release_after_ms the hand goes back to WiVRn.
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    CHECK(driver.HoldsRole(0, true));
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    CHECK(!driver.HoldsRole(0, true));

    // Switched off in the settings while running: read again within a second or so.
    CHECK(run(0, 0.05));
    writeConfig(false);
    bool held = true;
    for (double end = NowSeconds() + 2.5; held && NowSeconds() < end;) held = run(0, 0.1);
    CHECK(!held && driver.ConfigGeneration() == 2 && !driver.GetConfig().active);
    fromDriver.Drain(0.05);
    report(0, 0);
    bool sawNone = false;
    for (const auto& d : fromDriver.Drain(0.1)) {
        if (d.size() != sizeof(ContextPacket)) continue;
        std::memcpy(&ctx, d.data(), sizeof(ctx));
        sawNone |= ctx.mode_left == kModeNone;
    }
    CHECK(sawNone);
}

int main(int argc, char** argv) {
    std::string dir = "/tmp";
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "-v") g_verbose = true;
        else dir = argv[i];
    }
    TestConfig();
    TestInputs();
    TestBones();
    TestGestures();
    TestPoses();
    TestDriver(dir);
    if (g_failures) {
        std::printf("%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("all WiVRn driver tests passed\n");
    return 0;
}
