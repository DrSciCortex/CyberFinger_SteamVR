/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// MathUtil.h — small double-precision vector / quaternion / rigid-transform
// helpers. Quaternions are Hamilton, (w, x, y, z); an Xform maps child-local
// coordinates into its parent: p_parent = q · p_local + p.
// ═══════════════════════════════════════════════════════════════════════════

#include <cmath>
#include "VrTypes.h"

namespace cf {

constexpr double kPi = 3.14159265358979323846;
inline double DegToRad(double d) { return d * kPi / 180.0; }

struct Vec3 { double x = 0, y = 0, z = 0; };
struct Quat { double w = 1, x = 0, y = 0, z = 0; };
struct Xform { Quat q; Vec3 p; };

inline Vec3 operator+(const Vec3& a, const Vec3& b) { return { a.x + b.x, a.y + b.y, a.z + b.z }; }
inline Vec3 operator-(const Vec3& a, const Vec3& b) { return { a.x - b.x, a.y - b.y, a.z - b.z }; }
inline Vec3 operator-(const Vec3& a) { return { -a.x, -a.y, -a.z }; }
inline Vec3 operator*(const Vec3& a, double s) { return { a.x * s, a.y * s, a.z * s }; }
inline double Dot(const Vec3& a, const Vec3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline Vec3 Cross(const Vec3& a, const Vec3& b) {
    return { a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x };
}
inline double Length(const Vec3& a) { return std::sqrt(Dot(a, a)); }

inline Quat operator*(const Quat& a, const Quat& b) {
    return { a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
             a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
             a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
             a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w };
}
inline Quat Conj(const Quat& q) { return { q.w, -q.x, -q.y, -q.z }; }
inline Quat Normalize(const Quat& q) {
    const double n = std::sqrt(q.w * q.w + q.x * q.x + q.y * q.y + q.z * q.z);
    if (n < 1e-12) return {};
    return { q.w / n, q.x / n, q.y / n, q.z / n };
}
inline Vec3 Rotate(const Quat& q, const Vec3& v) {
    // v' = v + 2w(u×v) + 2u×(u×v), u = (x, y, z)
    const Vec3 u{ q.x, q.y, q.z };
    const Vec3 t = Cross(u, v) * 2.0;
    return v + t * q.w + Cross(u, t);
}

inline Xform operator*(const Xform& a, const Xform& b) { return { a.q * b.q, a.p + Rotate(a.q, b.p) }; }
inline Xform Inverse(const Xform& a) {
    const Quat qi = Conj(a.q);
    return { qi, Rotate(qi, -a.p) };
}

inline Quat Slerp(const Quat& a, Quat b, double t) {
    double d = a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z;
    if (d < 0) { b = { -b.w, -b.x, -b.y, -b.z }; d = -d; }
    if (d > 0.9995) {
        return Normalize({ a.w + t * (b.w - a.w), a.x + t * (b.x - a.x),
                           a.y + t * (b.y - a.y), a.z + t * (b.z - a.z) });
    }
    const double th = std::acos(d), s = std::sin(th);
    const double wa = std::sin((1 - t) * th) / s, wb = std::sin(t * th) / s;
    return { wa * a.w + wb * b.w, wa * a.x + wb * b.x, wa * a.y + wb * b.y, wa * a.z + wb * b.z };
}
inline Vec3 Lerp(const Vec3& a, const Vec3& b, double t) { return a + (b - a) * t; }
inline Xform Blend(const Xform& a, const Xform& b, double t) { return { Slerp(a.q, b.q, t), Lerp(a.p, b.p, t) }; }

#ifndef CF_NO_OPENVR
// Rotation part of a 3x4 row-major matrix (Shepperd's method).
inline Quat QuatFromMatrix(const vr::HmdMatrix34_t& m) {
    const double tr = m.m[0][0] + m.m[1][1] + m.m[2][2];
    Quat q;
    if (tr > 0) {
        const double s = std::sqrt(tr + 1.0) * 2;
        q = { 0.25 * s, (m.m[2][1] - m.m[1][2]) / s, (m.m[0][2] - m.m[2][0]) / s, (m.m[1][0] - m.m[0][1]) / s };
    } else if (m.m[0][0] > m.m[1][1] && m.m[0][0] > m.m[2][2]) {
        const double s = std::sqrt(1.0 + m.m[0][0] - m.m[1][1] - m.m[2][2]) * 2;
        q = { (m.m[2][1] - m.m[1][2]) / s, 0.25 * s, (m.m[0][1] + m.m[1][0]) / s, (m.m[0][2] + m.m[2][0]) / s };
    } else if (m.m[1][1] > m.m[2][2]) {
        const double s = std::sqrt(1.0 + m.m[1][1] - m.m[0][0] - m.m[2][2]) * 2;
        q = { (m.m[0][2] - m.m[2][0]) / s, (m.m[0][1] + m.m[1][0]) / s, 0.25 * s, (m.m[1][2] + m.m[2][1]) / s };
    } else {
        const double s = std::sqrt(1.0 + m.m[2][2] - m.m[0][0] - m.m[1][1]) * 2;
        q = { (m.m[1][0] - m.m[0][1]) / s, (m.m[0][2] + m.m[2][0]) / s, (m.m[1][2] + m.m[2][1]) / s, 0.25 * s };
    }
    return Normalize(q);
}
inline Xform XformFromMatrix(const vr::HmdMatrix34_t& m) {
    return { QuatFromMatrix(m), { m.m[0][3], m.m[1][3], m.m[2][3] } };
}
#endif

// Intrinsic X-Y-Z Euler angles in degrees — the convention of the old
// grip_angle_* settings, kept so previously tuned values mean the same thing.
inline Quat QuatFromEulerXYZDeg(double ax, double ay, double az) {
    const double hx = DegToRad(ax) * 0.5, hy = DegToRad(ay) * 0.5, hz = DegToRad(az) * 0.5;
    const double cx = std::cos(hx), sx = std::sin(hx);
    const double cy = std::cos(hy), sy = std::sin(hy);
    const double cz = std::cos(hz), sz = std::sin(hz);
    return { cx * cy * cz + sx * sy * sz, sx * cy * cz - cx * sy * sz,
             cx * sy * cz + sx * cy * sz, cx * cy * sz - sx * sy * cz };
}

inline Xform BoneToXform(const vr::VRBoneTransform_t& b) {
    return { { b.orientation.w, b.orientation.x, b.orientation.y, b.orientation.z },
             { b.position.v[0], b.position.v[1], b.position.v[2] } };
}
inline vr::VRBoneTransform_t XformToBone(const Xform& x) {
    vr::VRBoneTransform_t b{};
    b.position.v[0] = float(x.p.x); b.position.v[1] = float(x.p.y);
    b.position.v[2] = float(x.p.z); b.position.v[3] = 1.f;
    b.orientation.w = float(x.q.w); b.orientation.x = float(x.q.x);
    b.orientation.y = float(x.q.y); b.orientation.z = float(x.q.z);
    return b;
}
#ifndef CF_NO_OPENVR
inline vr::HmdQuaternion_t ToHmdQuat(const Quat& q) { return { q.w, q.x, q.y, q.z }; }
inline Quat FromHmdQuat(const vr::HmdQuaternion_t& q) { return { q.w, q.x, q.y, q.z }; }
inline Vec3 FromArray(const double v[3]) { return { v[0], v[1], v[2] }; }
inline void ToArray(const Vec3& a, double v[3]) { v[0] = a.x; v[1] = a.y; v[2] = a.z; }

// The pose of a point rigidly attached to a device: src · offset, with that point's velocity and
// acceleration. Timing (poseTimeOffset), the driver → world transform and the tracking state carry
// over, so SteamVR predicts it exactly as it predicts the source. Velocities are in driver space,
// like the position.
inline vr::DriverPose_t OffsetDriverPose(const vr::DriverPose_t& src, const Xform& offset) {
    vr::DriverPose_t p = src;
    const Quat q = FromHmdQuat(src.qRotation);
    const Vec3 r = Rotate(q, offset.p);
    const Vec3 w = FromArray(src.vecAngularVelocity), a = FromArray(src.vecAngularAcceleration);
    ToArray(FromArray(src.vecPosition) + r, p.vecPosition);
    p.qRotation = ToHmdQuat(Normalize(q * offset.q));
    ToArray(FromArray(src.vecVelocity) + Cross(w, r), p.vecVelocity);
    ToArray(FromArray(src.vecAcceleration) + Cross(a, r) + Cross(w, Cross(w, r)), p.vecAcceleration);
    return p;
}
#endif

} // namespace cf
