/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// PoseFilter.h — clean up a streamed hand pose before SteamVR predicts it
//
// Steam Link streams the hand pose already predicted forward, with noisy
// velocity estimates (and occasional runaways of several m/s). SteamVR then
// extrapolates those velocities again to the display time, which amplifies
// the noise and overshoots when the hand stops. This filter ignores the
// source's velocities:
//
//   - position and rotation go through a One Euro filter (Casiez et al. 2012):
//     a low-pass whose cutoff rises with speed, so a still hand is smoothed
//     strongly and a moving one lags little. Its speed estimate is itself
//     low-passed, so a short burst of implausible motion (a runaway) can't
//     open the cutoff before the source snaps back;
//   - the velocities SteamVR extrapolates with come from the filtered pose,
//     low-passed, capped, and scaled by `prediction` / `rotPrediction`
//     (0 = no extrapolation).
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cstdint>
#include <cmath>
#include "MathUtil.h"

namespace cf {

class PoseFilter {
public:
    struct Params {
        bool   enabled = true;
        double minCutoff = 1.0;        // Hz, position cutoff of a still hand
        double beta = 15.0;            // Hz per m/s: how fast the cutoff opens with speed
        double rotMinCutoff = 1.0;     // Hz, rotation cutoff when still
        double rotBeta = 1.0;          // Hz per rad/s
        double speedCutoff = 1.0;      // Hz, low-pass of the speed that drives the cutoffs
        double velocityCutoff = 5.0;   // Hz, low-pass of the output velocities
        double prediction = 0.5;       // scale of the output linear velocity (SteamVR's extrapolation)
        double rotPrediction = 0.0;    // scale of the output angular velocity: off, as extrapolating any
                                       // estimate of it makes a pointing laser jitter (filter_eval)
        double maxSpeed = 4.0;         // m/s cap of the output velocity, and of plausible hand motion
        double maxAngularSpeed = 15.0; // rad/s, likewise
        double gate = 0.05;            // m beyond plausible motion: a sample farther off is a glitch
        double gateAngle = 0.5;        // rad, likewise for rotation
        double resync = 0.08;          // s of consecutive glitches after which the source is followed again
    };

    void Reset() { m_init = false; }
    uint32_t Rejected() const { return m_rejected; }

    // The last output pose, to hold while the source can't see the hand. False before the first sample.
    bool Current(Vec3& p, Quat& q) const {
        if (!m_init) return false;
        p = m_p;
        q = m_q;
        return true;
    }

    // Filters one sample taken at time t (s). v, w: velocities to publish (driver space).
    void Filter(Vec3& p, Quat& q, Vec3& v, Vec3& w, double t, const Params& prm) {
        if (!m_init || t - m_t > 0.25 || t < m_t) {   // start, a gap, or time went backwards: restart here
            Restart(p, q, t);
            v = w = {};
            return;
        }
        // Streamed samples can arrive in bursts (a backlog released at once): treat them as at least 2 ms
        // apart, so their spacing can't turn small steps into huge speeds.
        const double dt = std::max(t - m_t, 0.002);

        // Glitch gate: a sample farther from the prediction than a hand can move is ignored (the source
        // snaps back after a runaway); if the source stays there, follow it after `resync`.
        Quat qs = q;
        if (Dot4(qs, m_q) < 0) qs = { -qs.w, -qs.x, -qs.y, -qs.z };
        const double since = t - m_lastAccepted;
        const bool far = Length(p - (m_p + m_v * dt)) > prm.gate + prm.maxSpeed * since ||
                         AngleBetween(m_q, qs) > prm.gateAngle + prm.maxAngularSpeed * since;
        if (far) {
            ++m_rejected;
            if (since < prm.resync) {
                p = m_p;
                q = m_q;
                v = Cap(m_v, prm.maxSpeed) * prm.prediction;
                w = Cap(m_w, prm.maxAngularSpeed) * prm.rotPrediction;
                return;
            }
            Restart(p, q, t);                          // it stayed there: it's real
            v = w = {};
            return;
        }
        m_lastAccepted = t;
        m_t = t;

        // Speeds that open the cutoffs: the low-passed *signed* derivative (as in the One Euro filter), so
        // back-and-forth noise cancels instead of reading as motion.
        const double as = Alpha(prm.speedCutoff, dt);
        const Vec3 dp = p - m_p;
        m_dv = m_dv + (dp * (1.0 / dt) - m_dv) * as;
        m_dw = m_dw + (RotationVector(Normalize(qs * Conj(m_q))) * (1.0 / dt) - m_dw) * as;

        const Vec3 pNew = m_p + dp * Alpha(prm.minCutoff + prm.beta * Length(m_dv), dt);
        const Quat qNew = Slerp(m_q, qs, Alpha(prm.rotMinCutoff + prm.rotBeta * Length(m_dw), dt));

        // Output velocities from the filtered pose.
        const double av = Alpha(prm.velocityCutoff, dt);
        m_v = m_v + ((pNew - m_p) * (1.0 / dt) - m_v) * av;
        m_w = m_w + (RotationVector(Normalize(qNew * Conj(m_q))) * (1.0 / dt) - m_w) * av;

        m_p = pNew;
        m_q = Normalize(qNew);
        p = m_p;
        q = m_q;
        v = Cap(m_v, prm.maxSpeed) * prm.prediction;
        w = Cap(m_w, prm.maxAngularSpeed) * prm.rotPrediction;
    }

private:
    void Restart(const Vec3& p, const Quat& q, double t) {
        m_p = p;
        m_q = q;
        m_dv = m_dw = m_v = m_w = {};
        m_t = m_lastAccepted = t;
        m_init = true;
    }
    static double Alpha(double cutoffHz, double dt) {
        const double tau = 1.0 / (2.0 * kPi * std::max(cutoffHz, 1e-3));
        return 1.0 / (1.0 + tau / dt);
    }
    static double Dot4(const Quat& a, const Quat& b) { return a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z; }
    static double AngleBetween(const Quat& a, const Quat& b) {
        return 2.0 * std::acos(std::min(1.0, std::fabs(Dot4(a, b))));
    }
    // Axis * angle of a unit quaternion (world frame when q = q_new · q_old⁻¹).
    static Vec3 RotationVector(const Quat& q) {
        const Quat r = (q.w < 0) ? Quat{ -q.w, -q.x, -q.y, -q.z } : q;
        const double s = std::sqrt(r.x * r.x + r.y * r.y + r.z * r.z);
        if (s < 1e-9) return { 2 * r.x, 2 * r.y, 2 * r.z };
        const double angle = 2.0 * std::atan2(s, r.w);
        return Vec3{ r.x, r.y, r.z } * (angle / s);
    }
    static Vec3 Cap(const Vec3& a, double max) {
        const double l = Length(a);
        return (l > max && l > 0) ? a * (max / l) : a;
    }

    bool   m_init = false;
    double m_t = 0;
    double m_lastAccepted = 0;
    uint32_t m_rejected = 0;
    Vec3   m_p;
    Quat   m_q;
    Vec3   m_dv, m_dw;                         // low-passed signed velocities (drive the cutoffs)
    Vec3   m_v, m_w;
};

} // namespace cf
