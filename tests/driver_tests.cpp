/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// driver_tests.cpp — checks that need no SteamVR runtime
//
//   driver_tests <tests/protocol_vectors.bin>
//
// 1. Wire protocol: parse the golden vectors written by bridge/cf_protocol.py.
// 2. Skeleton synthesis (Valve sample port): unit quaternions, aux bones,
//    handedness, mirror symmetry, curl estimates.
// 3. PASSTHROUGH re-rooting keeps every joint where the headset put it, and
//    the republished pose moves like the headset hand's.
// 4. Grab, tap to hold.
// ═══════════════════════════════════════════════════════════════════════════

#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <vector>
#include "BoneData.h"
#include "MathUtil.h"
#include "Protocol.h"
#include "ImuFusion.h"
#include "PoseFilter.h"
#include "SkeletonSynth.h"
#include "SpreadMeter.h"
#include "TapHold.h"
#include "LongPress.h"
#include "GestureClick.h"
#include "Utils.h"

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

static void TestProtocol(const char* path) {
    std::ifstream f(path, std::ios::binary);
    CHECK(bool(f));
    if (!f) return;
    std::vector<std::vector<uint8_t>> pkts;
    uint32_t len;
    while (f.read(reinterpret_cast<char*>(&len), 4)) {
        std::vector<uint8_t> p(len);
        f.read(reinterpret_cast<char*>(p.data()), len);
        pkts.push_back(std::move(p));
    }
    CHECK(pkts.size() == 5);
    if (pkts.size() != 5) return;

    CHECK(pkts[0].size() == sizeof(CyberFingerPacket));
    CyberFingerPacket g;
    std::memcpy(&g, pkts[0].data(), sizeof(g));
    CHECK(g.h.magic == kMagicCyberFinger);
    CHECK(g.h.version == kVersion);
    CHECK(g.h.hand == 1);
    CHECK(g.h.seq == 7);
    CHECK(g.h.age_us == 5);
    CHECK(g.buttons == (kBtnMenu | kBtnTrigger));
    CHECK(g.trigger == 200);
    CHECK(g.joy_x == -1000);
    CHECK(g.joy_y == 32767);
    CHECK(g.battery_pct == 88);

    CHECK(pkts[1].size() == sizeof(HandStatePacket));
    HandStatePacket hs;
    std::memcpy(&hs, pkts[1].data(), sizeof(hs));
    CHECK(hs.h.magic == kMagicHandState);
    CHECK(hs.h.hand == 0);
    CHECK(hs.h.seq == 42);
    CHECK(hs.h.age_us == 1234);
    CHECK(hs.h.flags == (kHsPoseValid | kHsHasBones | kHsCameraSees));
    CHECK_NEAR(hs.raw_pos[1], 1.2, 1e-6);
    CHECK_NEAR(hs.raw_rot[3], 0.5, 1e-6);
    CHECK_NEAR(hs.ang_vel[2], -3, 1e-6);
    CHECK_NEAR(hs.curl[4], 0.5, 1e-6);
    CHECK_NEAR(hs.splay[0], -0.5, 1e-6);
    CHECK(hs.optical_seq == 99);
    CHECK(hs.key_posture == 3);
    CHECK_NEAR(hs.bones[30].px, 0.3, 1e-6);
    CHECK_NEAR(hs.bones[30].py, -0.6, 1e-6);
    CHECK_NEAR(hs.bones[30].pw, 1.0, 1e-6);
    CHECK_NEAR(hs.bones[30].qw, 1.0, 1e-6);

    CHECK(pkts[2].size() == sizeof(ContextPacket));
    ContextPacket c;
    std::memcpy(&c, pkts[2].data(), sizeof(c));
    CHECK(c.h.magic == kMagicContext);
    CHECK(c.h.hand == 0xFF);
    CHECK(c.h.seq == 5);
    CHECK(c.h.flags == kCtxCapturing);
    CHECK(c.hmd_valid == 1);
    CHECK(c.mode_left == kModeFused);
    CHECK(c.mode_right == kModePassthrough);
    CHECK(c.tap_hook_ok == 1);
    CHECK_NEAR(c.hmd_pos[1], 1.6, 1e-6);
    CHECK_NEAR(c.hmd_rot[0], 1.0, 1e-6);
    CHECK(c.applied_hs_seq[0] == 42);
    CHECK(c.tap[1].pose_valid == 1);
    CHECK(c.tap[1].tracking_result == 200);
    CHECK(c.tap[1].bone_count == 31);
    CHECK(c.tap[1].system_click == 1);
    CHECK(c.tap[1].skel_age_us == 12345);
    CHECK_NEAR(c.tap[1].raw_pos[2], -0.4, 1e-6);
    CHECK_NEAR(c.tap[1].bones[5].pz, 0.125, 1e-6);

    CHECK(pkts[3].size() == sizeof(HapticPacket));
    HapticPacket hp;
    std::memcpy(&hp, pkts[3].data(), sizeof(hp));
    CHECK(hp.h.magic == kMagicHaptic);
    CHECK(hp.h.hand == 1);
    CHECK(hp.h.seq == 3);
    CHECK_NEAR(hp.duration_s, 0.25, 1e-6);
    CHECK_NEAR(hp.frequency_hz, 160.0, 1e-6);
    CHECK_NEAR(hp.amplitude, 0.75, 1e-6);

    CHECK(pkts[4].size() == sizeof(ImuPacket));
    ImuPacket im;
    std::memcpy(&im, pkts[4].data(), sizeof(im));
    CHECK(im.h.magic == kMagicImu);
    CHECK(im.h.hand == 0);
    CHECK(im.h.seq == 1000);
    CHECK(im.h.age_us == 4321);
    CHECK(im.present == (kImuBody1 | kImuJoint));
    CHECK(im.has_accel == 1);
    CHECK_NEAR(im.quat[0][0], 1.0, 1e-6);
    CHECK_NEAR(im.quat[2][1], -0.5, 1e-6);
    CHECK_NEAR(im.quat[2][3], -0.5, 1e-6);
    CHECK(im.accel[0][2] == 2048);
    CHECK(im.accel[2][1] == 300);
    CHECK(im.accel[2][2] == -2048);
}

static void TestGestures() {
    CHECK_NEAR(PinchFromDistance(0.01), 1.0, 1e-6);
    CHECK_NEAR(PinchFromDistance(0.04), 0.5, 1e-6);
    CHECK_NEAR(PinchFromDistance(0.10), 0.0, 1e-6);

    const float zero[5] = {}, fist[5] = { 1, 1, 1, 1, 1 }, point[5] = { 0.5f, 0, 1, 1, 1 };
    vr::VRBoneTransform_t b[eBone_Count];
    SynthesizeSkeleton(true, zero, zero, b);
    const HandGestures open = GesturesFromBones(b);
    CHECK(open.grasp < 0.2f);
    CHECK(!open.indexPoint);
    for (float p : open.pinch) CHECK(p < 0.5f);
    SynthesizeSkeleton(true, fist, zero, b);
    CHECK(GesturesFromBones(b).grasp > 0.8f);
    SynthesizeSkeleton(true, point, zero, b);
    CHECK(GesturesFromBones(b).indexPoint);
    CHECK(!GesturesFromBones(b).twoFingerPoint);
    // The two-finger point: index and middle out, ring and pinky curled, the thumb across them. With the thumb out
    // (a V), or the middle curled (the index point), it isn't one.
    const float two[5] = { 0.7f, 0, 0, 1, 1 }, vee[5] = { 0, 0, 0, 1, 1 };
    SynthesizeSkeleton(true, two, zero, b);
    CHECK(GesturesFromBones(b).twoFingerPoint);
    CHECK(!GesturesFromBones(b).indexPoint);
    SynthesizeSkeleton(false, two, zero, b);
    CHECK(GesturesFromBones(b).twoFingerPoint);
    SynthesizeSkeleton(true, vee, zero, b);
    CHECK(!GesturesFromBones(b).twoFingerPoint);
    SynthesizeSkeleton(true, fist, zero, b);
    CHECK(!GesturesFromBones(b).twoFingerPoint);
    // Real attempts (2026-09-27 log, thumb and pinky assumed across and folded): the ring folded only to 0.51-0.59,
    // the middle at 0.36. The index point (middle curled) and an open hand stay out.
    auto curled = [](float index, float middle, float ring) {
        const float c[5] = { 0.5f, index, middle, ring, 0.7f };
        return TwoFingerPointShape(c);
    };
    CHECK(curled(0.22f, 0.22f, 0.51f));
    CHECK(curled(0.28f, 0.30f, 0.57f));
    CHECK(curled(0.30f, 0.36f, 0.53f));
    CHECK(!curled(0.20f, 0.70f, 0.70f));               // the index point
    CHECK(!curled(0.24f, 0.22f, 0.24f));               // an open hand
    const float thumbOut[5] = { 0.1f, 0.2f, 0.2f, 0.7f, 0.7f };
    const char* why = nullptr;
    CHECK(!TwoFingerPointShape(thumbOut, &why) && why != nullptr);

    // The palm's normal points the way the fingers curl, on either hand (the sign is chirality's, so check it on the
    // synthetic skeleton rather than trust a convention): curling moves the middle fingertip along the normal.
    for (int right = 0; right < 2; ++right) {
        const float half[5] = { 0, 0.5f, 0.5f, 0.5f, 0.5f };
        vr::VRBoneTransform_t open[eBone_Count], curled[eBone_Count];
        SynthesizeSkeleton(right == 1, zero, zero, open);
        SynthesizeSkeleton(right == 1, half, zero, curled);
        Vec3 c, n;
        PalmFrame(right == 1, open, c, n);
        Xform mo[eBone_Count], mc[eBone_Count];
        ModelSpace(open, mo);
        ModelSpace(curled, mc);
        CHECK(Dot(mc[eBone_MiddleFinger4].p - mo[eBone_MiddleFinger4].p, n) > 0.01);
        // A head straight out of the palm: 0°; behind the back of the hand: 180°.
        const Xform pose{ QuatFromEulerXYZDeg(30, -20, 50), { 0.1, 1.2, -0.3 } };
        const Vec3 front = pose.p + Rotate(pose.q, c + n * 0.4), back = pose.p + Rotate(pose.q, c - n * 0.4);
        CHECK(PinkyShape(right == 1, open, pose, &front).palmToHeadDeg < 1.0);
        CHECK(PinkyShape(right == 1, open, pose, &back).palmToHeadDeg > 179.0);
        CHECK(PinkyShape(right == 1, open, pose, nullptr).palmToHeadDeg == 180.0);
    }
    // A deliberate pinky pinch, and the ones it must refuse.
    PinkyPinchShape meant;
    meant.palmToHeadDeg = 25;
    const float relaxed[5] = { 0.4f, 0.2f, 0.25f, 0.45f, 0.9f };
    std::copy(relaxed, relaxed + 5, meant.curls);
    const double tips[4] = { 0.07, 0.06, 0.04, 0.012 };
    std::copy(tips, tips + 4, meant.thumbTo);
    CHECK(PinkyPinchMeant(meant));
    PinkyPinchShape s = meant;
    s.palmToHeadDeg = 120;                              // palm away from the face
    CHECK(!PinkyPinchMeant(s));
    s = meant;
    s.thumbTo[2] = 0.018;                               // the thumb on the ring and the pinky together
    CHECK(!PinkyPinchMeant(s));
    s = meant;
    s.thumbTo[1] = 0.02;                                // ... or the last three
    CHECK(!PinkyPinchMeant(s));
    s = meant;
    s.curls[1] = s.curls[2] = 0.85f;                    // a fist closing on the pinky
    CHECK(!PinkyPinchMeant(s));
    // A two-finger point, as the headset saw one (2026-09-27): the thumb across the ring and pinky is no pinky pinch.
    s = meant;
    s.palmToHeadDeg = 49;
    s.curls[1] = 0.12f, s.curls[2] = 0.11f, s.curls[3] = 0.64f;
    s.thumbTo[1] = 0.113, s.thumbTo[2] = 0.013, s.thumbTo[3] = 0.013;
    CHECK(!PinkyPinchMeant(s));

    // As a button: on after 60 ms held, off after 120 ms gone; a flicker shorter than that changes nothing.
    GestureClick click;
    CHECK(!click.Update(true, 0.00));
    CHECK(!click.Update(false, 0.03));                  // a 30 ms flicker
    CHECK(!click.Update(true, 0.10));
    CHECK(click.Update(true, 0.17));                    // held 70 ms
    CHECK(click.Update(false, 0.20));
    CHECK(click.Update(true, 0.25));                    // back within 120 ms: still held
    CHECK(click.Update(false, 0.30));
    CHECK(!click.Update(false, 0.43));                  // gone 130 ms
}

static double QuatNorm(const vr::HmdQuaternionf_t& q) {
    return std::sqrt(double(q.w) * q.w + double(q.x) * q.x + double(q.y) * q.y + double(q.z) * q.z);
}

static void TestSynth() {
    const float open[5] = {}, closed[5] = { 1, 1, 1, 1, 1 }, zero[5] = {};
    vr::VRBoneTransform_t L[eBone_Count], R[eBone_Count], F[eBone_Count];
    SynthesizeSkeleton(false, open, zero, L);
    SynthesizeSkeleton(true, open, zero, R);
    SynthesizeSkeleton(false, closed, zero, F);

    for (int b = 0; b < eBone_Count; ++b) {
        CHECK_NEAR(QuatNorm(L[b].orientation), 1.0, 1e-4);
        CHECK_NEAR(QuatNorm(R[b].orientation), 1.0, 1e-4);
        CHECK_NEAR(L[b].position.v[3], 1.0, 1e-6);
    }
    // Root is identity; the wrist is the Index-derived offset.
    CHECK_NEAR(L[eBone_Root].orientation.w, 1.0, 1e-6);
    CHECK_NEAR(L[eBone_Wrist].position.v[2], 0.164722, 1e-6);
    CHECK_NEAR(R[eBone_Wrist].position.v[0], 0.034038, 1e-6);

    // Aux bones mirror the last knuckle of each finger in model space.
    Xform model[eBone_Count];
    ModelSpace(L, model);
    for (int f = 0; f < 5; ++f) {
        const Xform aux = BoneToXform(L[eBone_Aux_Thumb + f]);
        CHECK(Length(aux.p - model[kAuxSource[f]].p) < 1e-5);
    }

    // Handedness from bone layout.
    CHECK(SkeletonHandedness(L, eBone_Count) == -1);
    CHECK(SkeletonHandedness(R, eBone_Count) == +1);

    // Mirror symmetry: right-hand model positions are the left's with X negated.
    Xform ml[eBone_Count], mr[eBone_Count];
    ModelSpace(L, ml);
    ModelSpace(R, mr);
    double worst = 0;
    for (int b = 0; b <= eBone_PinkyFinger4; ++b) {
        const Vec3 d = mr[b].p - Vec3{ -ml[b].p.x, ml[b].p.y, ml[b].p.z };
        worst = std::fmax(worst, Length(d));
    }
    CHECK(worst < 1e-5);
    if (worst >= 1e-5) std::printf("  mirror error %.6f m\n", worst);

    // Curl estimates: open hand low, fist high.
    float co[5], cc[5];
    CurlsFromBones(L, co);
    CurlsFromBones(F, cc);
    for (int f = 1; f < 5; ++f) {
        CHECK(co[f] < 0.2f);
        CHECK(cc[f] > 0.8f);
    }
    CHECK(cc[0] > co[0]);
    std::printf("  curls open  %.2f %.2f %.2f %.2f %.2f\n", co[0], co[1], co[2], co[3], co[4]);
    std::printf("  curls fist  %.2f %.2f %.2f %.2f %.2f\n", cc[0], cc[1], cc[2], cc[3], cc[4]);
}

// PASSTHROUGH re-rooting (ReRootBones + the matching raw pose) keeps every joint in place, also
// when the source skeleton's root bone isn't the identity.
static void TestReroot() {
    const float curls[5] = { 0.3f, 0.6f, 0.2f, 0.9f, 0.1f }, splay[5] = { 0.2f, -0.3f, 0, 0.4f, -0.2f };
    vr::VRBoneTransform_t link[eBone_Count];
    SynthesizeSkeleton(true, curls, splay, link);
    // Give the "Steam Link" skeleton a different root → wrist offset, and a non-identity root.
    link[eBone_Wrist] = XformToBone({ Normalize(Quat{ 0.9, 0.1, -0.3, 0.2 }), { 0.02, -0.05, 0.09 } });
    FillAuxBones(link);                                   // aux bones for an identity root …
    const Xform root{ Normalize(Quat{ 0.95, 0.05, 0.2, -0.1 }), { 0.01, 0.02, -0.03 } };
    link[eBone_Root] = XformToBone(root);
    for (int b = eBone_Aux_Thumb; b <= eBone_Aux_PinkyFinger; ++b)   // … kept in place under the new root
        link[b] = XformToBone(Inverse(root) * BoneToXform(link[b]));
    const Xform rawLink{ Normalize(Quat{ 0.7, -0.2, 0.5, 0.1 }), { 0.3, 1.1, -0.4 } };

    const Xform ourWrist = SynthWristBone(true);
    const Xform linkWrist = SourceWrist(link);
    const Xform rawOurs = rawLink * (linkWrist * Inverse(ourWrist));
    vr::VRBoneTransform_t ours[eBone_Count];
    ReRootBones(link, linkWrist, ourWrist, ours);

    Xform ml[eBone_Count], mo[eBone_Count];
    ModelSpace(link, ml);
    ModelSpace(ours, mo);
    double worst = 0;
    for (int b = 1; b < eBone_Count; ++b) {
        const Xform wl = rawLink * ml[b], wo = rawOurs * mo[b];
        worst = std::fmax(worst, Length(wl.p - wo.p));
        const Quat dq = Conj(wl.q) * wo.q;
        worst = std::fmax(worst, 1.0 - std::fabs(dq.w));
    }
    CHECK(worst < 1e-5);
    CHECK_NEAR(BoneToXform(ours[eBone_Root]).q.w, 1.0, 1e-6);
    std::printf("  re-root worst error %.2e\n", worst);
}

// OffsetDriverPose: the offset point's pose, and velocities that match rigid-body motion.
static void TestOffsetPose() {
    vr::DriverPose_t src{};
    src.poseTimeOffset = -0.021;
    src.qWorldFromDriverRotation.w = 1;
    src.qDriverFromHeadRotation.w = 1;
    const Quat q = Normalize(Quat{ 0.8, 0.3, -0.4, 0.2 });
    src.qRotation = ToHmdQuat(q);
    const Vec3 p{ 0.2, 1.3, -0.5 }, v{ 0.4, -0.1, 0.25 }, w{ 1.5, -2.0, 0.7 };
    ToArray(p, src.vecPosition);
    ToArray(v, src.vecVelocity);
    ToArray(w, src.vecAngularVelocity);
    src.poseIsValid = true;
    src.result = vr::TrackingResult_Running_OK;
    const Xform offset{ Normalize(Quat{ 0.9, -0.1, 0.3, 0.1 }), { 0.03, -0.04, 0.16 } };

    const vr::DriverPose_t out = OffsetDriverPose(src, offset);
    const Xform expect = Xform{ q, p } * offset;
    CHECK(Length(FromArray(out.vecPosition) - expect.p) < 1e-9);
    CHECK(std::fabs(std::fabs((Conj(FromHmdQuat(out.qRotation)) * expect.q).w) - 1.0) < 1e-9);
    CHECK_NEAR(out.poseTimeOffset, src.poseTimeOffset, 0);
    CHECK(out.poseIsValid && out.result == vr::TrackingResult_Running_OK);

    // Move the source rigidly for dt (world-frame angular velocity) and differentiate the offset point.
    const double dt = 1e-6, angle = Length(w) * dt;
    const Vec3 axis = w * (1.0 / Length(w));
    const Quat dq{ std::cos(angle / 2), axis.x * std::sin(angle / 2), axis.y * std::sin(angle / 2),
                   axis.z * std::sin(angle / 2) };
    const Xform later = Xform{ dq * q, p + v * dt } * offset;
    const Vec3 numeric = (later.p - expect.p) * (1.0 / dt);
    const double err = Length(numeric - FromArray(out.vecVelocity));
    CHECK(err < 1e-5);
    std::printf("  offset pose velocity error %.2e m/s\n", err);
}

// Still-hand noise: known noise on a still point reads back; a moving point doesn't count.
static void TestSpread() {
    SpreadMeter still, moving;
    unsigned seed = 12345;
    auto noise = [&]() {                         // uniform in ±1 mm per axis: std 1/√3 mm
        seed = seed * 1664525u + 1013904223u;
        return ((seed >> 8) / double(1u << 24) * 2.0 - 1.0) * 1e-3;
    };
    for (int i = 0; i < 90 * 5; ++i) {          // 5 s at 90 Hz
        const double t = i / 90.0;
        still.Add({ 0.3 + noise(), 1.2 + noise(), -0.4 + noise() }, t);
        moving.Add({ 0.3 + 0.2 * t, 1.2, -0.4 }, t);
    }
    int windows = 0, movingWindows = 0;
    const double spread = still.Take(windows);
    moving.Take(movingWindows);
    CHECK(windows >= 3);
    CHECK_NEAR(spread, 1e-3, 1.5e-4);            // RMS distance: √3 · (1/√3 mm) = 1 mm
    CHECK(movingWindows == 0);
    std::printf("  still-hand spread %.3f mm over %d windows\n", spread * 1e3, windows);
}

// PASSTHROUGH pose filter: smooths a still hand, follows steady motion, ignores a short runaway.
static void TestPoseFilter() {
    PoseFilter::Params prm;
    PoseFilter f;
    unsigned seed = 7;
    auto noise = [&]() { seed = seed * 1664525u + 1013904223u; return ((seed >> 8) / double(1u << 24) - 0.5) * 2e-3; };
    double t = 0, errStill = 0, rawStill = 0;
    Vec3 p, v, w;
    Quat q;
    for (int i = 0; i < 1000; ++i, t += 1.0 / 360) {           // still hand, ±1 mm noise
        const Vec3 x{ 0.2 + noise(), 1.1 + noise(), -0.3 + noise() };
        p = x;
        q = Quat{};
        f.Filter(p, q, v, w, t, prm);
        if (i > 200) {
            errStill += Dot(p - Vec3{ 0.2, 1.1, -0.3 }, p - Vec3{ 0.2, 1.1, -0.3 });
            rawStill += Dot(x - Vec3{ 0.2, 1.1, -0.3 }, x - Vec3{ 0.2, 1.1, -0.3 });
        }
    }
    CHECK(errStill < 0.25 * rawStill);                          // at least halves the noise (rms)
    double lagMove = 0;
    for (int i = 0; i < 360; ++i, t += 1.0 / 360) {            // steady 0.5 m/s along x for 1 s
        p = { 0.2 + 0.5 * (i + 1) / 360.0, 1.1, -0.3 };
        const double truth = p.x;
        q = Quat{};
        f.Filter(p, q, v, w, t, prm);
        if (i == 359) lagMove = truth - p.x;
    }
    CHECK(lagMove < 0.02);                                      // under 2 cm behind at 0.5 m/s
    CHECK_NEAR(v.x, 0.5 * prm.prediction, 0.1 * prm.prediction);   // and reports the motion's velocity, scaled
    const Vec3 rest = p;
    double worst = 0;
    for (int i = 0; i < 20; ++i, t += 1.0 / 360) {             // runaway: 8 m/s for 28 ms, then back
        p = (i < 10) ? Vec3{ rest.x + 0.1 + 8.0 * i / 360, rest.y, rest.z } : rest;
        q = Quat{};
        f.Filter(p, q, v, w, t, prm);
        worst = std::fmax(worst, Length(p - rest));
    }
    CHECK(worst < 0.03);                                        // the output stays within 3 cm
    std::printf("  pose filter: still noise x%.2f, lag at 0.5 m/s %.1f mm, runaway excursion %.1f mm\n",
                std::sqrt(errStill / rawStill), lagMove * 1e3, worst * 1e3);
}

// Grab, tap to hold: short press latches until the next press, long press only while held.
// The black button: a tap reaches the app as a click on release (long enough to be seen), a hold fires the
// long press once and never reaches the app.
static void TestLongPress() {
    LongPress lp(0.8, 0.06);
    bool fired = false, anyFired = false, anyDown = false;
    // a 0.15 s tap: nothing while held, then down for 0.06 s from the release
    double t = 0;
    for (; t < 0.15; t += 0.011) { anyDown |= lp.Update(true, t, fired); anyFired |= fired; }
    CHECK(!anyDown && !anyFired);
    CHECK(lp.Update(false, t, fired) && !fired);
    CHECK(lp.Update(false, t + 0.03, fired));
    CHECK(!lp.Update(false, t + 0.07, fired));
    // a 1.5 s hold: fires once at 0.8 s, never down
    int fires = 0;
    anyDown = false;
    const double t0 = 1.0;
    for (t = t0; t < t0 + 1.5; t += 0.011) { anyDown |= lp.Update(true, t, fired); fires += fired; }
    CHECK(fires == 1 && !anyDown);
    CHECK(!lp.Update(false, t, fired) && !fired);
    // pressed again right after a click: a new press, held back again
    lp.Update(true, 3.0, fired);
    CHECK(lp.Update(false, 3.1, fired));
    CHECK(lp.Update(true, 3.12, fired));                 // still the click's time
    CHECK(!lp.Update(true, 3.2, fired));                 // then held back as a new press
    CHECK(lp.Update(false, 3.3, fired));
}

static void TestTapHold() {
    TapHold g(0.2);
    double t = 0;
    auto step = [&](bool down, double dt) { t += dt; return g.Update(down, t); };
    CHECK(!step(false, 0.01));
    CHECK(step(true, 0.01));        // tap …
    CHECK(step(false, 0.10));       // … released after 0.1 s: stays held
    CHECK(step(false, 1.00));
    CHECK(step(true, 0.01));        // next press …
    CHECK(!step(false, 0.05));      // … lets go on release
    CHECK(step(true, 0.01));        // long press: held while down
    CHECK(step(true, 0.50));
    CHECK(!step(false, 0.01));      // … and released with the button
    CHECK(step(true, 0.01));        // tap to latch, then a long press ends it on release
    CHECK(step(false, 0.05));
    CHECK(step(true, 0.01));
    CHECK(step(true, 0.80));
    CHECK(!step(false, 0.01));
    CHECK(step(true, 0.01));        // latched, then the CyberFinger is lost
    CHECK(step(false, 0.05));
    g.Reset();
    CHECK(!step(false, 0.01));
    TapHold off(0.0);               // latching off
    CHECK(off.Update(true, 0.0));
    CHECK(!off.Update(false, 0.05));
}

// IMU fusion on a synthetic hand: known heading, mounting and optical lag; the IMU at 100 Hz, the optical stream
// at 90 Hz trailing it. The fusion must recover the heading, then follow the true hand, also through an occlusion.
static void TestImuFusion() {
    const double alpha = 40.0 * kPi / 180.0, lag = 0.03;
    const Quat yaw{ std::cos(alpha / 2), 0, std::sin(alpha / 2), 0 };
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };            // IMU z-up → SteamVR y-up
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    auto hand = [](double t) {                                          // slow wandering plus quicker turns
        return QuatFromEulerXYZDeg(30 * std::sin(2 * kPi * 0.13 * t) + 12 * std::sin(2 * kPi * 0.7 * t),
                                   25 * std::sin(2 * kPi * 0.17 * t + 1.0),
                                   35 * std::sin(2 * kPi * 0.11 * t + 2.0) + 10 * std::sin(2 * kPi * 0.9 * t));
    };
    auto angleDeg = [](const Quat& a, const Quat& b) {
        const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
        return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
    };
    ImuFusion f;
    f.Reset();
    Quat q;
    Vec3 w;
    double tImu = 0, tOpt = 0, worstSeen = 0, worstHidden = 0;
    bool earlyOutput = false;
    for (double t = 0; t < 33.0; t += 0.001) {
        while (tImu <= t) {
            f.AddImu(tImu, Normalize(Conj(yaw * toVr) * hand(tImu) * Conj(mount)));
            tImu += 0.01;
        }
        if (tOpt <= t) {
            const bool seen = t < 30.0;                                 // the last 3 s: hand out of view
            f.Observe(t, hand(t - lag), seen);
            tOpt += 1.0 / 90;
            const bool have = f.Orientation(t, q, w);
            if (t < 2.0 && have) earlyOutput = true;
            if (have && t > 20.0) (seen ? worstSeen : worstHidden) = std::fmax(seen ? worstSeen : worstHidden,
                                                                                angleDeg(q, hand(t)));
        }
    }
    const ImuFusion::Status st = f.GetStatus();
    CHECK(!earlyOutput);                                                // nothing before a calibration
    CHECK(st.calibrated);
    CHECK_NEAR(st.alphaDeg, 40.0, 3.0);
    CHECK_NEAR(st.lag, lag, 0.012);
    CHECK(worstSeen < 2.0);                                             // follows the true hand, not the late optics
    CHECK(worstHidden < 2.0);                                           // and carries it through the occlusion
    std::printf("  IMU fusion: heading %.1f deg, lag %.1f ms, mount fit %.2f deg; error seen %.2f, hidden %.2f deg\n",
                st.alphaDeg, st.lag * 1e3, st.residualDeg, worstSeen, worstHidden);
}

// A cold start from a saved mounting, the hand held still (no full solve possible): a near prior (12° off) starts
// the fusion within a second, on the hand; a far one (the sensor turned 90°) is refused.
static void TestImuFusionPrior() {
    const double alpha = -70.0 * kPi / 180.0;
    const Quat yaw{ std::cos(alpha / 2), 0, std::sin(alpha / 2), 0 };
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    auto hand = [](double t) { return QuatFromEulerXYZDeg(-40 + 1.5 * std::sin(3 * t), 15, 5 * std::sin(2 * t)); };
    auto run = [&](const Quat& priorMount, double& calibratedAt, double& errDeg) {
        ImuFusion f;
        f.Reset();
        f.SetPrior({ priorMount, 0.03, 0 });
        calibratedAt = -1;
        errDeg = 0;
        Quat q;
        Vec3 w;
        double tImu = 0, tOpt = 0;
        for (double t = 0; t < 4.0; t += 0.001) {
            while (tImu <= t) {
                f.AddImu(tImu, Normalize(Conj(yaw * toVr) * hand(tImu) * Conj(mount)));
                tImu += 0.01;
            }
            if (tOpt > t) continue;
            f.Observe(t, hand(t - 0.03), true);
            tOpt += 1.0 / 90;
            if (!f.Orientation(t, q, w)) continue;
            if (calibratedAt < 0) calibratedAt = t;
            const double d = std::fabs(q.w * hand(t).w + q.x * hand(t).x + q.y * hand(t).y + q.z * hand(t).z);
            errDeg = std::fmax(errDeg, 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi);
        }
        return f.GetStatus();
    };
    double at, err;
    ImuFusion::Status st = run(mount * QuatFromEulerXYZDeg(12, 0, 0), at, err);
    CHECK(st.calibrated && st.fromPrior && st.solves == 0);
    CHECK(at > 0 && at < 1.2);
    CHECK(err < 3.0);                                                   // a still hand: q_corr takes up the rest
    std::printf("  IMU fusion from a prior 12 deg off: running after %.2f s, fit %.1f deg, error %.2f deg\n", at,
                st.residualDeg, err);
    st = run(mount * QuatFromEulerXYZDeg(90, 0, 0), at, err);
    CHECK(!st.calibrated && st.priorResidualDeg > 25.0);
}

// Resync. Calibrated on one heading; then the IMU's heading jumps by 110° (the CyberFinger switched off and on) —
// after a 6 s silence (resynced by itself), or with no gap and Resync() asked for (the bridge's button, a triple
// tap). Either way the output is on the hand again within a second of the change; the mounting and the lag carry
// over. Without the resync, the gate refuses the headset until its escape.
static void TestImuFusionResync() {
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    auto yaw = [](double deg) {
        const double a = deg * kPi / 180.0;
        return Quat{ std::cos(a / 2), 0, std::sin(a / 2), 0 };
    };
    auto hand = [](double t) { return QuatFromEulerXYZDeg(-40 + 1.5 * std::sin(3 * t), 15, 5 * std::sin(2 * t)); };
    auto angle = [](const Quat& a, const Quat& b) {
        const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
        return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
    };
    // mode 0: an IMU gap of 6 s; 1: no gap, Resync() at the change; 2: no gap, no resync. Returns the seconds after
    // the change until the output stays within 5° of the hand (-1: never).
    auto run = [&](int mode, ImuFusion::Status& st) {
        ImuFusion f;
        f.Reset();
        f.SetPrior({ mount, 0.03, 0 });
        const double change = 3.0, gap = mode == 0 ? 6.0 : 0.0, resumed = change + gap;
        double tImu = 0, tOpt = 0, goodSince = -1;
        Quat q;
        Vec3 w;
        for (double t = 0; t < resumed + 6.0; t += 0.001) {
            while (tImu <= t) {
                if (tImu < change || tImu >= resumed) {
                    const Quat y = yaw(tImu < change ? -70.0 : 40.0);
                    f.AddImu(tImu, Normalize(Conj(y * toVr) * hand(tImu) * Conj(mount)));
                }
                tImu += 0.01;
            }
            if (mode == 1 && t >= change && t < change + 0.001) f.Resync();
            if (tOpt > t) continue;
            f.Observe(t, hand(t - 0.03), true);
            tOpt += 1.0 / 90;
            if (t < resumed) continue;
            const bool good = f.Orientation(t, q, w) && angle(q, hand(t)) < 5.0;
            if (!good) goodSince = -1;
            else if (goodSince < 0) goodSince = t;
        }
        st = f.GetStatus();
        return goodSince < 0 ? -1.0 : goodSince - resumed;
    };
    ImuFusion::Status st;
    const double afterGap = run(0, st);
    CHECK(st.resyncs == 1 && st.calibrated);
    CHECK(afterGap >= 0 && afterGap < 1.0);
    const double asked = run(1, st);
    CHECK(st.resyncs == 1 && st.calibrated);
    CHECK(asked >= 0 && asked < 1.0);
    const double without = run(2, st);
    CHECK(st.resyncs == 0);
    CHECK(without < 0 || without > asked + 1.0);
    std::printf("  IMU fusion after a 110 deg heading jump: right again after %.2f s (gap, resynced by itself), "
                "%.2f s (Resync asked for), %.2f s without (-1: not within 6 s)\n", afterGap, asked, without);
}

// Off the hand. Calibrated; then the CyberFinger lies on the floor, switched on, for 60 s while the headset watches the bare
// hand move about: found off the hand within ~3 s, no output and nothing learned meanwhile (no pairs, no escapes).
// Put back on: the IMU turns with the hand again, it resyncs, and the output is right within ~2 s.
static void TestImuFusionOffHand() {
    const double alpha = -70.0 * kPi / 180.0;
    const Quat yaw{ std::cos(alpha / 2), 0, std::sin(alpha / 2), 0 };
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    auto hand = [](double t) {
        return QuatFromEulerXYZDeg(-40 + 25 * std::sin(1.2 * t), 15 + 10 * std::sin(0.8 * t), 5 * std::sin(t));
    };
    auto angle = [](const Quat& a, const Quat& b) {
        const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
        return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
    };
    ImuFusion f;
    f.Reset();
    f.SetPrior({ mount, 0.03, 0 });
    const double off = 5.0, on = 65.0;
    const Quat lying = hand(off);                               // where the CyberFinger was put down
    double tImu = 0, tOpt = 0, foundOffAt = -1, goodSince = -1;
    size_t pairsWhileOff = 0, escapesBefore = 0;
    bool outputWhileOff = false;
    Quat q;
    Vec3 w;
    for (double t = 0; t < on + 6.0; t += 0.001) {
        while (tImu <= t) {
            const Quat h = tImu >= off && tImu < on ? lying : hand(tImu);
            f.AddImu(tImu, Normalize(Conj(yaw * toVr) * h * Conj(mount)));
            tImu += 0.01;
        }
        if (tOpt > t) continue;
        f.Observe(t, hand(t - 0.03), true);
        tOpt += 1.0 / 90;
        const ImuFusion::Status st = f.GetStatus();
        const bool out = f.Orientation(t, q, w);
        if (t < off) { escapesBefore = st.escapes; continue; }
        if (t < on) {
            if (st.offHand && foundOffAt < 0) foundOffAt = t - off;
            if (foundOffAt >= 0) {
                outputWhileOff = outputWhileOff || out;
                pairsWhileOff = std::max(pairsWhileOff, st.pairs);
            }
            continue;
        }
        const bool good = out && angle(q, hand(t)) < 5.0;
        if (!good) goodSince = -1;
        else if (goodSince < 0) goodSince = t;
    }
    const ImuFusion::Status st = f.GetStatus();
    CHECK(foundOffAt >= 0 && foundOffAt < 3.0);
    CHECK(!outputWhileOff);
    CHECK(st.escapes == escapesBefore);                         // no escape toward the bare hand
    CHECK(st.offHandTimes == 1 && st.resyncs == 1 && !st.offHand && st.calibrated);
    CHECK(goodSince >= on && goodSince - on < 2.0);
    std::printf("  IMU fusion off the hand: found after %.2f s; back on, right again after %.2f s\n", foundOffAt,
                goodSince - on);
}

// The gate. A hand turning slowly, calibrated from an exact prior; then the headset flips the palm (180°) for 1.5 s:
// the output stays on the hand. Then the CyberFinger slips on the hand by 35°: the headset, in full view, is refused
// until the escape (3 s), then followed. The same slip seen only from the edge of the view (trust 0.3): never.
static void TestImuFusionGate() {
    const double alpha = 25.0 * kPi / 180.0;
    const Quat yaw{ std::cos(alpha / 2), 0, std::sin(alpha / 2), 0 };
    const Quat toVr{ std::sqrt(0.5), -std::sqrt(0.5), 0, 0 };
    const Quat mount = QuatFromEulerXYZDeg(20, -30, 10);
    const Quat slip = QuatFromEulerXYZDeg(35, 0, 0);
    auto hand = [](double t) {
        return QuatFromEulerXYZDeg(-40 + 8 * std::sin(0.5 * t), 15 + 5 * std::sin(0.7 * t), 4 * std::sin(0.3 * t));
    };
    auto angle = [](const Quat& a, const Quat& b) {
        const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
        return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
    };
    auto run = [&](double trustAfterSlip, double& flipErr, double& slipErrAt2, double& slipErrEnd) {
        ImuFusion f;
        f.Reset();
        f.SetPrior({ mount, 0.03, 0 });
        flipErr = slipErrAt2 = slipErrEnd = 0;
        Quat q;
        Vec3 w;
        double tImu = 0, tOpt = 0;
        for (double t = 0; t < 16.0; t += 0.001) {
            const bool slipped = t >= 6.0;
            while (tImu <= t) {
                f.AddImu(tImu, Normalize(Conj(yaw * toVr) * hand(tImu) * Conj(slipped ? mount * slip : mount)));
                tImu += 0.01;
            }
            if (tOpt > t) continue;
            tOpt += 1.0 / 90;
            const bool flipped = t >= 3.0 && t < 4.5;
            const Quat opt = flipped ? hand(t - 0.03) * QuatFromEulerXYZDeg(180, 0, 0) : hand(t - 0.03);
            f.Observe(t, opt, true, slipped ? trustAfterSlip : 1.0);
            if (!f.Orientation(t, q, w)) continue;
            const double e = angle(q, hand(t));
            if (t >= 3.0 && t < 4.6) flipErr = std::fmax(flipErr, e);
            if (t >= 7.9 && t < 8.0) slipErrAt2 = e;                    // 2 s after the slip: still refused
            if (t >= 15.9) slipErrEnd = e;
        }
        return f.GetStatus();
    };
    double flipErr, at2, end;
    ImuFusion::Status st = run(1.0, flipErr, at2, end);
    CHECK(flipErr < 3.0);
    CHECK(at2 > 25.0);
    CHECK(end < 5.0 && st.escapes >= 1);
    std::printf("  IMU fusion gate: palm flip -> error %.1f deg; CyberFinger slip 35 deg: %.0f deg after 2 s, %.1f deg after 10 s "
                "(%zu escape)\n", flipErr, at2, end, st.escapes);
    st = run(0.3, flipErr, at2, end);
    CHECK(end > 25.0 && st.escapes == 0);
}

// Setting lists such as pose_filter_types: "a|b", case and blanks ignored.
static void TestSplitList() {
    const std::vector<std::string> l = SplitList(" svl_hand_interaction_augmented | VD_Hand_Controller || ");
    CHECK(l.size() == 2);
    CHECK(l.size() == 2 && l[0] == "svl_hand_interaction_augmented" && l[1] == "vd_hand_controller");
    CHECK(SplitList("").empty());
    CHECK(Lower("Svl_Hand") == "svl_hand");
}

int main(int argc, char** argv) {
    TestProtocol(argc > 1 ? argv[1] : "tests/protocol_vectors.bin");
    TestSynth();
    TestReroot();
    TestOffsetPose();
    TestSpread();
    TestPoseFilter();
    TestTapHold();
    TestLongPress();
    TestSplitList();
    TestImuFusion();
    TestImuFusionPrior();
    TestImuFusionResync();
    TestImuFusionOffHand();
    TestImuFusionGate();
    TestGestures();
    if (g_failures) {
        std::printf("%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("all driver tests passed\n");
    return 0;
}
