/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// ImuFusion.h — the CyberFinger's joint IMU fused with the headset's optical hand
// orientation, driver-side (a CFHS sender's fusion replaces all of this
// through the FUSED mode)
//
// The joint IMU sits on the back of the hand. Its orientation relates to the
// optical hand orientation (driver space, any hand-fixed frame) as
//
//     q_hand(t) = Y(α) · C · q_imu(t) · M
//
// C relabels the IMU's z-up world as SteamVR's y-up, α is the IMU's heading
// (arbitrary, drifting slowly: 6-axis, no magnetometer), M the sensor's mounting
// on the hand. While the headset sees the hand, α and M are solved from a window
// of (IMU, optical) pairs — a grid over α, the mean mounting for each — with the
// IMU sample taken `lag` earlier: the optical stream trails the IMU (Steam Link
// 10–90 ms, drifting), and the lag is measured continuously by correlating the two
// streams' angular velocities (their rates, until calibrated). The output is
//
//     q_out = q_corr · Y(α) · C · q_imu(newest) · M
//
// q_corr follows the optical orientation slowly (tauCorrection), so the absolute
// orientation is the headset's while the fast motion, and its timing, are the
// IMU's: ~3× less jitter at rest and no optical lag. While the hand is unseen
// q_corr is frozen and the IMU carries the orientation alone.
//
// Cold start: M belongs to the CyberFinger and how it sits on the hand, and the lag to
// the streamer, so both carry over between sessions (the driver saves them); α
// restarts with every IMU power-up. Given a prior M and lag, α alone is fitted
// from the first few pairs — no spread of orientations needed — and the fusion
// runs within a second; the full solve then refines M as usual.
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <deque>
#include <mutex>
#include <vector>
#include "MathUtil.h"

namespace cf {

class ImuFusion {
public:
    struct Params {
        double tauCorrection = 0.5;    // s: how fast the output settles on the optical orientation
        double defaultLag = 0.03;      // s: optical behind the IMU, until measured
        double maxLag = 0.15;          // s (Steam Link sessions measured 10-90 ms)
        double lagMemory = 15.0;       // s: the lag measurements averaged over about this long (Steam Link's lag
                                       // drifts within a session: 90 → 25 ms over 10 s, capture 2026-09-27 01:12)
        double lagMinWindows = 1.5;    // measurements (decaying count: 2 in a row) before the lag replaces the
                                       // prior's, or the default
        double minLagCorrelation = 0.5; // a window whose rotation rates correlate less at any shift is skipped
        double imuTimeout = 0.1;       // s without IMU data: no fused output
        double pairInterval = 0.1;     // s between calibration pairs
        double pairWindow = 60.0;      // s of pairs kept
        double solveEvery = 3.0;       // s
        size_t minPairs = 40;
        double minSpreadDeg = 12.0;    // the optical orientations must differ this much for a solve
        double maxResidualDeg = 25.0;  // a worse fit is rejected
        double maxCorrectionRate = 1.0; // rad/s: faster optical turns aren't compared (the streams disagree in
                                        // motion: Steam Link's optical rotation is a prediction; the IMU leads)
        size_t priorPairs = 5;         // pairs to fit the heading with a prior mounting
        double maxPriorResidualDeg = 25.0; // a prior mounting that fits worse is ignored (another CyberFinger, sensor turned).
                                           // Sessions of the same CyberFinger scatter by 8-23° (how it sits, the postures
                                           // seen), and even that start beats waiting for a full solve.
        // Once calibrated, an optical orientation this far from the fused one is a tracking error (a flipped palm, a
        // hand at the edge of the cameras' view), not a correction; the gate narrows to half as the view's trust
        // falls. After a loss it widens by what the IMU may have drifted meanwhile. 25°: on a 2-minute capture
        // (2026-09-27 02:21) the corrections turn the output 55 % less than ungated while the output agrees with
        // the headset's best views as well; at 15° the output can lock in a wrong state (p90 16 → 19°).
        double gateDeg = 25.0;
        double gapGateDegPerS = 3.0;   // wider after a loss, per second lost …
        double maxGapGateDeg = 30.0;   // … up to this much
        double escapeTime = 3.0;       // s of refusing every trusted view: then it's the output that's off
        double escapeTau = 0.15;       // s: how fast the output then settles
        double minPairTrust = 0.8;     // calibration pairs only from views trusted this much (HeadsetViewTrust)
        double resyncAfterGap = 5.0;   // s without IMU data (the CyberFinger switched off, or reconnecting), then Resync:
                                       // a CyberFinger switched off and on has a new heading, which the gate would
                                       // otherwise refuse the headset over until its escape
        // Off the hand: the headset sees the hand turn while the IMU lies still (the CyberFinger put down, switched on).
        // The fusion then learns nothing and outputs nothing (the headset alone) — no pairs, no corrections, no
        // escapes toward a bare hand — until the IMU turns with the hand again, which resyncs it. Rotation rates,
        // not orientations, so it needs no calibration: a CyberFinger on the hand turns as fast as the hand.
        // Both rates over offHandRateSpan: over the 40 ms the lag uses, the headset's jitter at rest (~1° between
        // frames) would read as 25°/s of turning; over 0.2 s as ~5°/s.
        double offHandOpticalRate = 0.25; // rad/s: the hand turning (15°/s) …
        double offHandImuRate = 0.05;  // rad/s: … and the IMU not (3°/s; a CyberFinger lying still shows < 0.01)
        double offHandRateSpan = 0.2;  // s
        double offHandAfter = 1.0;     // s of that (decaying over offHandMemory): off the hand
        double onHandAfter = 0.5;      // s of the IMU turning with the hand (rates within onHandRateMatch): back on
        double onHandRateMatch = 0.35; // |IMU − hand| / hand
        double offHandMemory = 3.0;    // s
    };

    struct Status {
        bool   calibrated = false;
        bool   fromPrior = false;      // running on the prior mounting (no full solve yet)
        double lag = 0;                // s, optical behind the IMU
        double lagCorrelation = 0;     // of the last lag measurement
        double lagRaw = 0;             // s: the last window's own best shift
        size_t lagWindows = 0;         // windows measured
        double alphaDeg = 0;
        double residualDeg = 0;        // rms spread of the mounting over the pairs at the last solve (or prior fit)
        size_t pairs = 0;
        size_t solves = 0;             // full solves
        double priorResidualDeg = -1;  // the prior mounting's fit at the last try (-1: not tried)
        size_t rejected = 0;           // optical samples the gate refused
        size_t escapes = 0;            // steady disagreements followed after all
        double corrTravelDeg = 0;      // how far the optical corrections turned the output, in all
        size_t resyncs = 0;            // Resync calls, asked for or after an IMU gap
        bool   offHand = false;        // the CyberFinger is off the hand (the IMU still while the hand turns): no output
        size_t offHandTimes = 0;       // times it was found off the hand
    };

    // What carries over to the next session: the mounting (hand-fixed frame of the optical source) and the lag.
    struct Calibration {
        Quat   mount;
        double lag = 0;                // s; 0: unknown
        double residualDeg = 0;
    };

    // The reference CyberFingers' calibration, the start when nothing is saved yet: the mean of three Steam Link
    // sessions (2026-09-26/27; each within 8-12° of it), the lag their middle (they measured 17-88 ms).
    static Calibration DefaultCalibration(int hand) {
        return hand ? Calibration{ { 0.4773, 0.4682, 0.5676, 0.4804 }, 0.05, 0 }
                    : Calibration{ { 0.4982, 0.4682, -0.5101, -0.5219 }, 0.05, 0 };
    }

    void SetParams(const Params& p) { std::lock_guard<std::mutex> g(m_lock); m_p = p; }

    void Reset() {
        std::lock_guard<std::mutex> g(m_lock);
        m_imu.clear();
        m_opt.clear();
        m_pairs.clear();
        m_st = Status{};
        m_st.lag = m_p.defaultLag;
        m_haveCorr = m_haveLag = m_havePrior = m_solved = false;
        m_lagSum.clear();
        m_lagW.clear();
        m_lagEff = 0;
        m_lagVector = false;
        m_nextPair = m_nextSolve = m_nextLag = m_lastObserve = m_nextPriorFit = 0;
        m_everSeen = m_inGap = m_escaping = false;
        m_extraGate = 0;
        m_refusedSince = -1;
    }

    // Start from an earlier session's calibration: until the first full solve, the heading alone is fitted.
    // The lag (if > 0) is the starting point for its measurement.
    void SetPrior(const Calibration& c) {
        std::lock_guard<std::mutex> g(m_lock);
        m_prior = c;
        m_prior.mount = Normalize(c.mount);
        m_havePrior = true;
        if (!m_haveLag && c.lag > 0) {
            m_st.lag = std::min(c.lag, m_p.maxLag);
            m_haveLag = true;
        }
    }

    // Start the heading over, keeping what belongs to the CyberFinger and the streamer: the mounting (this session's
    // solve, else the prior) and the lag. For a CyberFinger whose IMU restarted (switched off and on: a new heading) or
    // that was off the hand (its pairs meaningless): the heading is fitted again from the next few trusted views,
    // within a second, instead of the gate refusing the headset until its escape and the stale pairs spoiling the
    // solves for a minute (pairWindow). Done by itself when the IMU resumes after resyncAfterGap. Any thread.
    void Resync() {
        std::lock_guard<std::mutex> g(m_lock);
        ResyncLocked();
    }

    // The latest full solve's mounting and the lag, to save for the next session. False before a full solve.
    bool GetCalibration(Calibration& c) const {
        std::lock_guard<std::mutex> g(m_lock);
        if (!m_solved) return false;
        c = { m_mount, m_haveLag ? m_st.lag : 0.0, m_st.residualDeg };
        return true;
    }

    // A joint IMU orientation (its own z-up world), stamped with the driver's clock. Any thread.
    void AddImu(double t, Quat q) {
        std::lock_guard<std::mutex> g(m_lock);
        q = Normalize(q);
        if (!m_imu.empty() && t > m_imu.back().t + m_p.resyncAfterGap) ResyncLocked();   // the CyberFinger was off
        if (!m_imu.empty()) {
            if (t <= m_imu.back().t) return;
            if (Dot4(q, m_imu.back().q) < 0) q = Neg(q);
        }
        m_imu.push_back({ t, q, true });
        while (!m_imu.empty() && m_imu.front().t < t - 5.0) m_imu.pop_front();   // a lag window reaches 4.15 s back
    }

    // An optical observation of the hand's orientation (driver space, any hand-fixed frame). seen: the headset
    // tracks the hand now (not extrapolating a lost one). trust: how far the headset's tracking is trusted where
    // the hand is (HeadsetViewTrust; 1 = fully), which slows its corrections and keeps it out of the calibration.
    // Any thread.
    void Observe(double t, Quat q, bool seen, double trust = 1.0) {
        std::lock_guard<std::mutex> g(m_lock);
        q = Normalize(q);
        if (!m_opt.empty()) {
            if (t <= m_opt.back().t) return;
            if (Dot4(q, m_opt.back().q) < 0) q = Neg(q);
        }
        m_opt.push_back({ t, q, seen });
        while (!m_opt.empty() && m_opt.front().t < t - 5.0) m_opt.pop_front();
        const double dt = std::min(0.1, std::max(0.0, t - m_lastObserve));
        m_lastObserve = t;
        if (!seen) {
            if (m_everSeen && !m_inGap) { m_inGap = true; m_gapStart = t; }
            m_refusedSince = -1;                                // a loss isn't time spent refusing
            return;
        }
        m_everSeen = true;
        if (m_inGap) {                                          // back after a loss: the IMU may have drifted
            m_inGap = false;
            m_extraGate = std::min(m_p.maxGapGateDeg, std::max(m_extraGate, m_p.gapGateDegPerS * (t - m_gapStart)));
        }

        // Off the hand, or back on it: the two rotation rates, from well-tracked views only.
        double optRate, imuRate;
        const double span = m_p.offHandRateSpan;
        if (trust >= 0.9 && RateAt(m_opt, t - 0.5 * span - 0.005, optRate, span) &&
            RateAt(m_imu, t - m_st.lag - 0.5 * span - 0.005, imuRate, span)) {
            const double keep = std::exp(-dt / m_p.offHandMemory);
            const bool turning = optRate > m_p.offHandOpticalRate;
            if (!m_st.offHand) {
                m_offEvidence = m_offEvidence * keep + (turning && imuRate < m_p.offHandImuRate ? dt : 0.0);
                if (m_offEvidence > m_p.offHandAfter) {
                    m_st.offHand = true;
                    ++m_st.offHandTimes;
                    m_onEvidence = 0;
                }
            } else {
                const bool together = turning && std::fabs(imuRate - optRate) < m_p.onHandRateMatch * optRate;
                m_onEvidence = together ? m_onEvidence + dt : m_onEvidence * keep;
                if (m_onEvidence > m_p.onHandAfter) {           // back on the hand: start the heading over
                    ResyncLocked();
                    return;
                }
            }
        }
        if (m_st.offHand) return;                               // learn nothing from a bare hand

        if (t >= m_nextLag) { m_nextLag = t + 1.0; MeasureLag(t); }

        // Only a slowly turning hand is compared: in motion the two streams disagree by several degrees
        // (timing, and the optical stream's prediction), and at rest they agree to ~1°. So the output rides the
        // IMU through fast motion and settles on the optical orientation whenever the hand slows down.
        double rate;
        if (!RateAt(m_opt, t - 0.025, rate) || rate > m_p.maxCorrectionRate) return;

        Quat qi;
        const bool haveImu = ImuAt(t - m_st.lag, qi);
        // The gate, once calibrated: the optical orientation against the fused one (lag-aligned). Refusing every
        // trusted view for escapeTime means the output is what's off (the IMU's heading drifted over a long loss,
        // the CyberFinger slipped on the hand): followed after all, quickly. The headset's own errors come and go (a
        // flipped palm, a glitch), and those at the edges of the view never count.
        bool escaping = false;
        if (m_st.calibrated && m_haveCorr && haveImu) {
            const Quat target = Normalize(q * Conj(Model(qi)));
            if (AngleDeg(target, m_corr) > (m_p.gateDeg + m_extraGate) * (0.5 + 0.5 * std::min(1.0, trust))) {
                ++m_st.rejected;
                if (trust < 0.9) return;
                if (m_refusedSince < 0) { m_refusedSince = t; m_refusedCount = 0; }
                if (++m_refusedCount < 30 || t - m_refusedSince < m_p.escapeTime) { m_escaping = false; return; }
                escaping = true;
                if (!m_escaping) {
                    // The output was off, not the headset: the pairs so far describe how the CyberFinger sat before (it
                    // was adjusted, or slipped), and would spoil the next solves for a pairWindow. Start them over.
                    ++m_st.escapes;
                    m_pairs.clear();
                    m_st.pairs = 0;
                }
            } else if (trust >= 0.9) {
                m_refusedSince = -1;
            }
        }
        m_escaping = escaping;

        if (trust >= m_p.minPairTrust && t >= m_nextPair && haveImu) {
            m_nextPair = t + m_p.pairInterval;
            m_pairs.push_back({ t, qi, q });
            while (!m_pairs.empty() && m_pairs.front().t < t - m_p.pairWindow) m_pairs.pop_front();
            m_st.pairs = m_pairs.size();
        }
        if (!m_st.calibrated && m_havePrior && m_pairs.size() >= m_p.priorPairs && t >= m_nextPriorFit) {
            m_nextPriorFit = t + 0.2;
            FitHeading();
        }
        if (t >= m_nextSolve && m_pairs.size() >= m_p.minPairs) { m_nextSolve = t + m_p.solveEvery; Solve(); }

        // Settle slowly on the optical orientation, comparing like with like: the IMU `lag` earlier. The less the
        // view is trusted, the slower.
        if (m_st.calibrated && ImuAt(t - m_st.lag, qi)) {
            Quat target = Normalize(q * Conj(Model(qi)));
            if (!m_haveCorr) {
                m_corr = target;
                m_haveCorr = true;
            } else {
                if (Dot4(target, m_corr) < 0) target = Neg(target);
                const double tau = escaping ? m_p.escapeTau : m_p.tauCorrection / std::max(0.05, trust);
                const Quat before = m_corr;
                m_corr = Normalize(Slerp(m_corr, target, 1.0 - std::exp(-dt / std::max(1e-3, tau))));
                m_st.corrTravelDeg += AngleDeg(before, m_corr);
            }
            m_extraGate *= std::exp(-dt);                       // the post-loss widening fades as corrections come in
        }
    }

    // The fused orientation for display, from the newest IMU sample, and its angular velocity (driver space).
    // False until calibrated, or when the IMU is silent.
    bool Orientation(double now, Quat& q, Vec3& w) {
        std::lock_guard<std::mutex> g(m_lock);
        if (!m_st.calibrated || !m_haveCorr || m_st.offHand || m_imu.size() < 3 ||
            now - m_imu.back().t > m_p.imuTimeout)
            return false;
        const Stamped& b = m_imu.back();
        q = Normalize(m_corr * Model(b.q));
        // Angular velocity over at least 20 ms of IMU samples, in driver space. (Reports are stamped with their
        // BLE arrival, and a connection event often delivers two within a millisecond: a shorter span is noise.)
        size_t ia = m_imu.size() - 2;
        while (ia > 0 && b.t - m_imu[ia].t < 0.02) --ia;
        const Stamped& a = m_imu[ia];
        const Quat d = Normalize(Model(b.q) * Conj(Model(a.q)));
        w = Rotate(m_corr, RotationVector(d) * (1.0 / std::max(0.005, b.t - a.t)));
        // Carried on to `now` (the IMU reports every ~9 ms): smooth between reports instead of stepping.
        q = Normalize(FromRotationVector(w * std::min(0.03, std::max(0.0, now - b.t))) * q);
        return true;
    }

    Status GetStatus() const { std::lock_guard<std::mutex> g(m_lock); return m_st; }

private:
    struct Stamped { double t; Quat q; bool seen; };
    struct Pair { double t; Quat imu, opt; };

    static double Dot4(const Quat& a, const Quat& b) { return a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z; }
    static Quat Neg(const Quat& q) { return { -q.w, -q.x, -q.y, -q.z }; }
    static double AngleDeg(const Quat& a, const Quat& b) {
        return 2.0 * std::acos(std::min(1.0, std::fabs(Dot4(a, b)))) * 180.0 / kPi;
    }
    static Vec3 RotationVector(Quat q) {
        if (q.w < 0) q = Neg(q);
        const double s = std::sqrt(q.x * q.x + q.y * q.y + q.z * q.z);
        if (s < 1e-9) return { 2 * q.x, 2 * q.y, 2 * q.z };
        return Vec3{ q.x, q.y, q.z } * (2.0 * std::atan2(s, q.w) / s);
    }
    static Quat FromRotationVector(const Vec3& r) {
        const double a = Length(r);
        if (a < 1e-12) return {};
        const double s = std::sin(a / 2) / a;
        return { std::cos(a / 2), r.x * s, r.y * s, r.z * s };
    }
    static Quat YawQ(double a) { return { std::cos(a / 2), 0, std::sin(a / 2), 0 }; }
    // IMU z-up world → SteamVR y-up: (x, y, z) → (x, z, -y), a -90° turn about x.
    static Quat ImuToVr() { return { std::sqrt(0.5), -std::sqrt(0.5), 0, 0 }; }

    Quat Model(const Quat& imu) const { return YawQ(m_alpha) * ImuToVr() * imu * m_mount; }

    // IMU orientation at time t (interpolated; the newest sample for up to 50 ms after it).
    bool ImuAt(double t, Quat& q) const { return At(m_imu, t, q); }
    static bool At(const std::deque<Stamped>& s, double t, Quat& q) {
        if (s.size() < 2 || t < s.front().t || t > s.back().t + 0.05) return false;
        if (t >= s.back().t) { q = s.back().q; return true; }
        size_t lo = 0, hi = s.size() - 1;                  // s[lo].t <= t < s[hi].t
        while (hi - lo > 1) {
            const size_t mid = (lo + hi) / 2;
            (s[mid].t <= t ? lo : hi) = mid;
        }
        const double f = (t - s[lo].t) / std::max(1e-9, s[hi].t - s[lo].t);
        q = Normalize(Slerp(s[lo].q, s[hi].q, f));
        return true;
    }

    // Rotation rate (rad/s) of a stream around time t, over 40 ms: frame-independent, so the two streams compare.
    static bool RateAt(const std::deque<Stamped>& s, double t, double& r, double span = 0.04) {
        Quat a, b;
        if (!At(s, t - 0.5 * span, a) || !At(s, t + 0.5 * span, b)) return false;
        r = 2.0 * std::acos(std::min(1.0, std::fabs(Dot4(a, b)))) / span;
        return true;
    }

    // Angular velocity (rad/s) of a stream around time t, over 40 ms, taken to driver space by `pre` (the IMU's
    // heading and axes; the mounting cancels out of a rotation between two samples).
    static bool OmegaAt(const std::deque<Stamped>& s, double t, const Quat& pre, Vec3& w) {
        Quat a, b;
        if (!At(s, t - 0.02, a) || !At(s, t + 0.02, b)) return false;
        w = RotationVector(Normalize(pre * b * Conj(pre * a))) * (1.0 / 0.04);
        return true;
    }

    // The optical stream's lag behind the IMU: the shift that best matches their rotations. Once calibrated,
    // the angular velocity vectors are compared in driver space (cosine similarity); before, only the rotation
    // rates can be (Pearson correlation), which time the envelope of each movement rather than the movement: they
    // read 10-25 ms later than the lag that best aligns the orientations, the one the fusion needs. Each window
    // (the last 4 s, the hand seen throughout and moving) gives a curve over the shifts; the curves are averaged
    // over about lagMemory, and the average's peak is the lag. A single window's own peak is unreliable (now and
    // then it lands on 0 or the largest shift); the average's is steady.
    void MeasureLag(double t) {
        const double t0 = t - 4.0, t1 = t - 0.1;
        size_t seen = 0, total = 0;
        for (const Stamped& o : m_opt)
            if (o.t >= t0) { ++total; if (o.seen) ++seen; }
        if (total == 0 || seen < total * 9 / 10) return;
        const double step = 0.005;
        const int n = int((t1 - t0) / step);
        std::vector<Vec3> opt(n);
        for (int i = 0; i < n; ++i)
            if (!OmegaAt(m_opt, t0 + i * step, Quat{}, opt[i])) return;
        double mean = 0, var = 0;
        for (const Vec3& v : opt) mean += Length(v);
        mean /= n;
        for (const Vec3& v : opt) var += (Length(v) - mean) * (Length(v) - mean);
        if (var / n < 0.3 * 0.3) return;                    // too little motion to time anything
        const bool vec = m_st.calibrated;
        const Quat pre = (vec && m_haveCorr ? m_corr : Quat{}) * YawQ(m_alpha) * ImuToVr();
        const int maxShift = int(m_p.maxLag / step);
        // The IMU once, on the grid extended back by the largest shift: imu[j] is at t0 + (j - maxShift) * step.
        std::vector<Vec3> imu(n + maxShift);
        std::vector<char> imuOk(n + maxShift, 0);
        for (int j = 0; j < n + maxShift; ++j)
            imuOk[j] = OmegaAt(m_imu, t0 + (j - maxShift) * step, pre, imu[j]);
        std::vector<double> corr(maxShift + 1, kNoCorr);
        for (int k = 0; k <= maxShift; ++k) {
            double sa = 0, sb = 0, sab = 0, saa = 0, sbb = 0;
            int m = 0;
            for (int i = 0; i < n; ++i) {
                const int j = i - k + maxShift;                 // the IMU at t0 + (i - k) * step
                if (!imuOk[j]) continue;
                if (vec) {
                    sab += Dot(opt[i], imu[j]); saa += Dot(opt[i], opt[i]); sbb += Dot(imu[j], imu[j]);
                } else {
                    const double a = Length(opt[i]), b = Length(imu[j]);
                    sa += a; sb += b; sab += a * b; saa += a * a; sbb += b * b;
                }
                ++m;
            }
            if (m < n / 2) continue;
            if (vec) {
                if (saa > 0 && sbb > 0) corr[k] = sab / std::sqrt(saa * sbb);
            } else {
                const double cov = sab / m - (sa / m) * (sb / m);
                const double va = saa / m - (sa / m) * (sa / m), vb = sbb / m - (sb / m) * (sb / m);
                if (va > 0 && vb > 0) corr[k] = cov / std::sqrt(va * vb);
            }
        }
        const int own = int(std::max_element(corr.begin(), corr.end()) - corr.begin());
        if (corr[own] < m_p.minLagCorrelation) return;     // the streams don't match here: times nothing
        m_st.lagRaw = PeakShift(corr, own) * step;
        ++m_st.lagWindows;

        // Rates and vectors read differently: once the vectors can be compared, the rates' average is dropped.
        if (vec != m_lagVector) {
            m_lagSum.clear();
            m_lagVector = vec;
        }
        if (m_lagSum.size() != corr.size()) {               // first window (or maxLag changed)
            m_lagSum.assign(corr.size(), 0.0);
            m_lagW.assign(corr.size(), 0.0);
            m_lagEff = 0;
            m_lagCurveT = t;
        }
        const double decay = std::exp(-(t - m_lagCurveT) / std::max(1.0, m_p.lagMemory));
        m_lagCurveT = t;
        m_lagEff = m_lagEff * decay + 1.0;
        for (size_t k = 0; k < corr.size(); ++k) {
            m_lagSum[k] *= decay;
            m_lagW[k] *= decay;
            if (corr[k] > kNoCorr) { m_lagSum[k] += corr[k]; m_lagW[k] += 1.0; }
        }
        if (m_lagEff < m_p.lagMinWindows) return;           // until then the prior's lag (or the default)
        std::vector<double> avg(corr.size(), kNoCorr);
        for (size_t k = 0; k < corr.size(); ++k)
            if (m_lagW[k] > 0.5 * m_lagEff) avg[k] = m_lagSum[k] / m_lagW[k];
        const int best = int(std::max_element(avg.begin(), avg.end()) - avg.begin());
        m_st.lag = PeakShift(avg, best) * step;
        m_st.lagCorrelation = avg[best];
        m_haveLag = true;
    }

    static constexpr double kNoCorr = -2.0;                 // a shift without enough overlapping samples

    // The peak of a correlation curve, refined between grid points by a parabola (in grid steps).
    static double PeakShift(const std::vector<double>& c, int best) {
        double shift = best;
        if (best > 0 && best + 1 < int(c.size()) && c[best - 1] > kNoCorr && c[best + 1] > kNoCorr) {
            const double den = c[best - 1] - 2 * c[best] + c[best + 1];
            if (den < 0) shift += 0.5 * (c[best - 1] - c[best + 1]) / den;
        }
        return shift;
    }

    // α and M from the pairs: for each heading on a grid, the mean mounting; the heading whose mountings agree
    // best wins, refined on a finer grid. The output stays continuous across a new solution.
    void Solve() {
        // The optical orientations must span enough to separate heading from mounting.
        Quat mean = { 0, 0, 0, 0 };
        for (const Pair& p : m_pairs) {
            const double s = Dot4(p.opt, m_pairs.front().opt) < 0 ? -1 : 1;
            mean = { mean.w + s * p.opt.w, mean.x + s * p.opt.x, mean.y + s * p.opt.y, mean.z + s * p.opt.z };
        }
        mean = Normalize(mean);
        double spread = 0;
        for (const Pair& p : m_pairs) spread = std::max(spread, AngleDeg(p.opt, mean));
        if (spread < m_p.minSpreadDeg) return;

        Quat mount;
        auto fit = [&](double alpha) {
            const Quat pre = YawQ(alpha) * ImuToVr();
            Quat sum = { 0, 0, 0, 0 }, first;
            bool haveFirst = false;
            for (const Pair& p : m_pairs) {
                Quat m = Normalize(Conj(pre * p.imu) * p.opt);
                if (!haveFirst) { first = m; haveFirst = true; }
                if (Dot4(m, first) < 0) m = Neg(m);
                sum = { sum.w + m.w, sum.x + m.x, sum.y + m.y, sum.z + m.z };
            }
            mount = Normalize(sum);
            double cost = 0;
            for (const Pair& p : m_pairs) {
                const double d = Dot4(Normalize(Conj(pre * p.imu) * p.opt), mount);
                cost += 1.0 - d * d;
            }
            return cost;
        };
        double bestC;
        const double bestA = BestHeading(fit, bestC);
        fit(bestA);
        const double residual = Residual(bestC);
        if (residual > m_p.maxResidualDeg) return;
        Apply(bestA, mount);
        m_st.fromPrior = false;
        m_st.residualDeg = residual;
        m_solved = true;
        ++m_st.solves;
    }

    // With the prior mounting, the heading alone: any pairs will do, a still hand included.
    void FitHeading() {
        auto fit = [&](double alpha) {
            const Quat pre = YawQ(alpha) * ImuToVr();
            double cost = 0;
            for (const Pair& p : m_pairs) {
                const double d = Dot4(Normalize(pre * p.imu * m_prior.mount), p.opt);
                cost += 1.0 - d * d;
            }
            return cost;
        };
        double bestC;
        const double bestA = BestHeading(fit, bestC);
        const double residual = Residual(bestC);
        m_st.priorResidualDeg = residual;
        if (residual > m_p.maxPriorResidualDeg) return;
        Apply(bestA, m_prior.mount);
        m_st.fromPrior = true;
        m_st.residualDeg = residual;
    }

    // The heading minimising cost(α): a 5° grid, then 0.5° around the best.
    template <class Cost> static double BestHeading(Cost cost, double& bestC) {
        double bestA = 0;
        bestC = 1e30;
        for (int i = 0; i < 72; ++i) {
            const double a = i * 5.0 * kPi / 180.0;
            const double c = cost(a);
            if (c < bestC) { bestC = c; bestA = a; }
        }
        const double coarse = bestA;
        for (int i = -10; i <= 10; ++i) {
            const double a = coarse + i * 0.5 * kPi / 180.0;
            const double c = cost(a);
            if (c < bestC) { bestC = c; bestA = a; }
        }
        return bestA;
    }

    double Residual(double cost) const {
        return 2.0 * std::asin(std::sqrt(std::max(0.0, cost / m_pairs.size()))) * 180.0 / kPi;
    }

    void ResyncLocked() {
        if (m_solved) {                                 // this session's mounting beats the one it started from
            m_prior.mount = m_mount;
            m_havePrior = true;
        }
        m_imu.clear();
        m_opt.clear();
        m_pairs.clear();
        const Status was = m_st;                        // the lag, and the running counts, carry on
        m_st = Status{};
        m_st.lag = was.lag;
        m_st.lagCorrelation = was.lagCorrelation;
        m_st.lagRaw = was.lagRaw;
        m_st.lagWindows = was.lagWindows;
        m_st.solves = was.solves;
        m_st.rejected = was.rejected;
        m_st.escapes = was.escapes;
        m_st.corrTravelDeg = was.corrTravelDeg;
        m_st.resyncs = was.resyncs + 1;
        m_st.offHandTimes = was.offHandTimes;
        m_haveCorr = m_solved = false;
        m_nextPair = m_nextSolve = m_nextPriorFit = 0;
        m_everSeen = m_inGap = m_escaping = false;
        m_extraGate = 0;
        m_refusedSince = -1;
        m_offEvidence = m_onEvidence = 0;
    }

    void Apply(double alpha, const Quat& mount) {
        if (m_st.calibrated && m_haveCorr && !m_imu.empty()) {   // keep the output where it is
            const Quat before = m_corr * Model(m_imu.back().q);
            m_alpha = alpha;
            m_mount = mount;
            m_corr = Normalize(before * Conj(Model(m_imu.back().q)));
        } else {
            m_alpha = alpha;
            m_mount = mount;
        }
        m_st.calibrated = true;
        m_st.alphaDeg = alpha * 180.0 / kPi;
    }

    mutable std::mutex m_lock;
    Params m_p;
    Status m_st{ false, false, 0.03 };
    std::deque<Stamped> m_imu, m_opt;
    std::deque<Pair> m_pairs;
    double m_alpha = 0;
    Quat   m_mount;
    Quat   m_corr;
    Calibration m_prior;
    bool   m_haveCorr = false;
    bool   m_haveLag = false;
    bool   m_havePrior = false;
    bool   m_solved = false;           // a full solve happened: m_mount is this session's
    std::vector<double> m_lagSum, m_lagW;   // per shift: decaying sums of the windows' correlations, and their weights
    double m_lagEff = 0;               // decaying count of the windows
    bool   m_lagVector = false;        // the sums are of angular velocity vectors (else of rates)
    double m_lagCurveT = 0;            // time of the last window
    double m_nextPair = 0, m_nextSolve = 0, m_nextLag = 0, m_lastObserve = 0, m_nextPriorFit = 0;
    // the gate
    bool   m_everSeen = false, m_inGap = false, m_escaping = false;
    double m_gapStart = 0;
    double m_extraGate = 0;            // deg: the gate's widening after a loss
    double m_refusedSince = -1;        // every trusted view refused since then (-1: one was accepted since) …
    size_t m_refusedCount = 0;         // … this many of them
    // off the hand
    double m_offEvidence = 0;          // s (decaying) of the hand turning while the IMU is still
    double m_onEvidence = 0;           // s of the IMU turning with the hand, while off
};

} // namespace cf
