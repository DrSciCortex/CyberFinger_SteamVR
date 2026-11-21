/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// filter_eval.cpp — run PoseFilter over a recorded source-hand capture
//
//   filter_eval <capture.csv> [name=value ...]
//
//   names: enabled minCutoff beta rotMinCutoff rotBeta speedCutoff
//          velocityCutoff prediction rotPrediction      e.g.  beta=10 prediction=0.5
//
// Captures come from the driver (tools/analyze_tap_capture.py --capture N).
// Samples arriving in one burst are merged to the last one, as the driver's
// republish thread does. For each hand, compares what an app displays with
// the raw stream and with the filter, when SteamVR extrapolates the pose
// `ahead` seconds with the published velocities:
//
//   pos      the device position
//   laser    a laser target 2 m along the device's -Z: rotation noise shows
//            here magnified (1 mrad = 2 mm)
//
// jitter is the RMS distance from a centred 100 ms mean; error is the distance
// from where the hand really is `ahead` later (the source path smoothed with a
// centred 60 ms median), rms and max.
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include "PoseFilter.h"

using namespace cf;

struct Sample { double t, offset; Vec3 p, v, w; Quat q; };

// Per sample: distance (m) from a centred moving average over `window` s.
static std::vector<double> Jitter(const std::vector<double>& t, const std::vector<Vec3>& x, double window) {
    std::vector<double> out(t.size());
    size_t j0 = 0, j1 = 0;
    Vec3 acc;
    for (size_t i = 0; i < t.size(); ++i) {
        while (j1 < t.size() && t[j1] <= t[i] + window / 2) acc = acc + x[j1++];
        while (t[j0] < t[i] - window / 2) acc = acc - x[j0++];
        out[i] = Length(x[i] - acc * (1.0 / double(j1 - j0)));
    }
    return out;
}

// The hand's path: a centred median over `window` s, per axis (short glitches don't move a median).
static std::vector<Vec3> Centered(const std::vector<double>& t, const std::vector<Vec3>& x, double window) {
    std::vector<Vec3> out(x.size());
    size_t j0 = 0, j1 = 0;
    std::vector<double> a;
    for (size_t i = 0; i < t.size(); ++i) {
        while (j1 < t.size() && t[j1] <= t[i] + window / 2) ++j1;
        while (t[j0] < t[i] - window / 2) ++j0;
        double m[3];
        for (int k = 0; k < 3; ++k) {
            a.clear();
            for (size_t j = j0; j < j1; ++j) a.push_back(k == 0 ? x[j].x : k == 1 ? x[j].y : x[j].z);
            std::nth_element(a.begin(), a.begin() + a.size() / 2, a.end());
            m[k] = a[a.size() / 2];
        }
        out[i] = { m[0], m[1], m[2] };
    }
    return out;
}

// Rotation by angular velocity w (world frame) over dt, applied to q.
static Quat Extrapolate(const Quat& q, const Vec3& w, double dt) {
    const Vec3 r = w * dt;
    const double a = Length(r);
    if (a < 1e-12) return q;
    const double s = std::sin(a / 2) / a;
    return Normalize(Quat{ std::cos(a / 2), r.x * s, r.y * s, r.z * s } * q);
}

static Vec3 LaserTarget(const Vec3& p, const Quat& q) { return p + Rotate(q, Vec3{ 0, 0, -2.0 }); }

// Per sample: distance (m) of `shown` from `truth` `ahead` s later; < 0 where there's no truth yet.
static std::vector<double> Errors(const std::vector<double>& t, const std::vector<Vec3>& shown,
                                  const std::vector<Vec3>& truth, double ahead) {
    std::vector<double> out(t.size(), -1);
    size_t k = 0;
    for (size_t i = 0; i < t.size(); ++i) {
        k = std::max(k, i);
        while (k + 1 < t.size() && t[k] < t[i] + ahead) ++k;
        if (t[k] >= t[i] + ahead - 0.005) out[i] = Length(shown[i] - truth[k]);
    }
    return out;
}

// Speed classes of the laser target's path: pointing at something, slow moves, fast sweeps.
enum { kStill, kSlow, kFast, kClasses };
static const char* kClassName[kClasses] = { "still (<0.3 m/s)", "slow (0.3-1.5)", "fast (>1.5)" };
static std::vector<int> Classes(const std::vector<double>& t, const std::vector<Vec3>& path) {
    std::vector<int> out(t.size(), kStill);
    size_t a = 0, b = 0;
    for (size_t i = 0; i < t.size(); ++i) {
        while (t[a] < t[i] - 0.015) ++a;
        while (b + 1 < t.size() && t[b + 1] <= t[i] + 0.015) ++b;
        const double speed = (t[b] > t[a]) ? Length(path[b] - path[a]) / (t[b] - t[a]) : 0;
        out[i] = speed < 0.3 ? kStill : speed < 1.5 ? kSlow : kFast;
    }
    return out;
}

struct Score { double jitter, rms, p99; size_t n; };   // mm
static Score ScoreClass(const std::vector<double>& jit, const std::vector<double>& err, const std::vector<int>& cls,
                        int c) {
    double j2 = 0, e2 = 0;
    size_t n = 0, ne = 0;
    std::vector<double> es;
    for (size_t i = 0; i < jit.size(); ++i) {
        if (cls[i] != c) continue;
        j2 += jit[i] * jit[i];
        ++n;
        if (err[i] >= 0) { e2 += err[i] * err[i]; es.push_back(err[i]); ++ne; }
    }
    double p99 = 0;
    if (!es.empty()) {
        const size_t k = std::min(es.size() - 1, size_t(0.99 * es.size()));
        std::nth_element(es.begin(), es.begin() + k, es.end());
        p99 = es[k];
    }
    return { n ? std::sqrt(j2 / n) * 1e3 : 0, ne ? std::sqrt(e2 / ne) * 1e3 : 0, p99 * 1e3, n };
}

static bool SetParam(PoseFilter::Params& prm, const char* arg) {
    const char* eq = std::strchr(arg, '=');
    if (!eq) return false;
    const std::string name(arg, eq);
    const double v = std::atof(eq + 1);
    struct { const char* n; double* p; } fields[] = {
        { "minCutoff", &prm.minCutoff }, { "beta", &prm.beta }, { "rotMinCutoff", &prm.rotMinCutoff },
        { "rotBeta", &prm.rotBeta }, { "speedCutoff", &prm.speedCutoff }, { "velocityCutoff", &prm.velocityCutoff },
        { "prediction", &prm.prediction }, { "rotPrediction", &prm.rotPrediction },
    };
    if (name == "enabled") { prm.enabled = v != 0; return true; }
    for (auto& f : fields)
        if (name == f.n) { *f.p = v; return true; }
    return false;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("usage: filter_eval <capture.csv> [name=value ...]\n");
        return 2;
    }
    PoseFilter::Params prm;
    for (int i = 2; i < argc; ++i)
        if (!SetParam(prm, argv[i])) {
            std::printf("unknown parameter %s\n", argv[i]);
            return 2;
        }
    std::ifstream f(argv[1]);
    std::string line;
    std::getline(f, line);
    std::vector<Sample> hands[2];
    while (std::getline(f, line)) {
        std::stringstream ss(line);
        std::string cell;
        std::vector<double> c;
        while (std::getline(ss, cell, ',')) c.push_back(std::atof(cell.c_str()));
        if (c.size() < 26 || c[2] != 0) continue;            // pose records only
        const double* v = &c[4];
        auto& h = hands[int(c[1]) & 1];
        const Sample s{ c[0], v[0], { v[1], v[2], v[3] }, { v[8], v[9], v[10] }, { v[11], v[12], v[13] },
                        Normalize(Quat{ v[4], v[5], v[6], v[7] }) };
        if (!h.empty() && s.t - h.back().t < 0.0005) h.back() = s;   // a burst: only its last sample is sent
        else h.push_back(s);
    }
    std::printf("minCutoff %.2f beta %.1f rotMinCutoff %.2f rotBeta %.1f speedCutoff %.1f velocityCutoff %.1f "
                "prediction %.2f rotPrediction %.2f%s\n", prm.minCutoff, prm.beta, prm.rotMinCutoff, prm.rotBeta,
                prm.speedCutoff, prm.velocityCutoff, prm.prediction, prm.rotPrediction,
                prm.enabled ? "" : " (filter off)");
    std::printf("  raw -> filtered, mm: jitter (from a 100 ms mean) | error vs the hand's path, rms / p99\n");
    double total[2] = {};                            // laser, 40 ms ahead: jitter still + error rms slow
    for (int h = 0; h < 2; ++h) {
        const auto& s = hands[h];
        if (s.size() < 100) continue;
        std::vector<double> t;
        std::vector<Vec3> rawP, rawT;
        std::vector<Sample> out;
        PoseFilter pf;
        for (const Sample& x : s) {
            Sample y = x;
            if (prm.enabled) {
                pf.Filter(y.p, y.q, y.v, y.w, x.t, prm);
                y.offset = 0;                                // the driver republishes with no offset
            }
            t.push_back(x.t);
            rawP.push_back(x.p);
            rawT.push_back(LaserTarget(x.p, x.q));
            out.push_back(y);
        }
        const std::vector<Vec3> pathP = Centered(t, rawP, 0.06), pathT = Centered(t, rawT, 0.06);
        const std::vector<int> cls = Classes(t, pathT);
        std::printf("%s hand, %zu samples after merging bursts, %u rejected as glitches:\n", h ? "right" : "left",
                    s.size(), pf.Rejected());
        for (double ahead : { 0.0, 0.04 }) {
            std::vector<Vec3> rp(s.size()), rt(s.size()), fp(s.size()), ft(s.size());
            for (size_t i = 0; i < s.size(); ++i) {
                // SteamVR extrapolates from the pose's own time (sample time + poseTimeOffset) to display time.
                const double dr = ahead - s[i].offset, df = ahead - out[i].offset;
                rp[i] = s[i].p + s[i].v * dr;
                rt[i] = LaserTarget(rp[i], Extrapolate(s[i].q, s[i].w, dr));
                fp[i] = out[i].p + out[i].v * df;
                ft[i] = LaserTarget(fp[i], Extrapolate(out[i].q, out[i].w, df));
            }
            const auto jrp = Jitter(t, rp, 0.1), jfp = Jitter(t, fp, 0.1), jrt = Jitter(t, rt, 0.1), jft = Jitter(t, ft, 0.1);
            const auto erp = Errors(t, rp, pathP, ahead), efp = Errors(t, fp, pathP, ahead);
            const auto ert = Errors(t, rt, pathT, ahead), eft = Errors(t, ft, pathT, ahead);
            std::printf("  %2.0f ms ahead\n", ahead * 1e3);
            for (int c = 0; c < kClasses; ++c) {
                const Score a = ScoreClass(jrp, erp, cls, c), b = ScoreClass(jfp, efp, cls, c);
                const Score d = ScoreClass(jrt, ert, cls, c), e = ScoreClass(jft, eft, cls, c);
                if (!a.n) continue;
                std::printf("    %-17s %4.0f%%  pos %5.1f -> %5.1f | %5.1f / %5.0f -> %5.1f / %5.0f    "
                            "laser %6.1f -> %6.1f | %6.1f / %5.0f -> %6.1f / %5.0f\n",
                            kClassName[c], 100.0 * a.n / s.size(), a.jitter, b.jitter, a.rms, a.p99, b.rms, b.p99,
                            d.jitter, e.jitter, d.rms, d.p99, e.rms, e.p99);
                if (ahead > 0 && c == kStill) total[0] += e.jitter;
                if (ahead > 0 && c == kSlow) total[1] += e.rms;
            }
        }
    }
    std::printf("summary (laser, 40 ms ahead, both hands): still jitter %.1f mm, slow error rms %.1f mm\n",
                total[0], total[1]);
    return 0;
}
