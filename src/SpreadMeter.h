/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// SpreadMeter.h — how much a tracked point wanders while it is held still
//
// The spread (RMS distance from the mean) of a point in 1 s windows, averaged
// over the windows in which the point stayed put (mean moved < 1 cm since the
// previous window). Hold a hand still for a few seconds and this is the
// tracking noise, largely free of the motion itself.
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include "MathUtil.h"

namespace cf {

class SpreadMeter {
public:
    void Add(const Vec3& x, double now) {
        if (m_n > 0 && now - m_start > 2 * kWindow) m_n = 0;   // gap in the data: start over
        if (m_n == 0) {
            m_start = now;
            m_sum = {};
            m_sumSq = 0;
        }
        m_sum = m_sum + x;
        m_sumSq += Dot(x, x);
        if (++m_n >= 2 && now - m_start >= kWindow) Close();
    }

    // RMS spread (m) over the still windows since the last call, and how many there were.
    double Take(int& windows) {
        windows = m_windows;
        const double rms = m_windows ? std::sqrt(m_varSum / m_windows) : 0.0;
        m_varSum = 0;
        m_windows = 0;
        return rms;
    }

private:
    static constexpr double kWindow = 1.0;   // s
    static constexpr double kStill = 0.01;   // m the window mean may move and still count

    void Close() {
        const Vec3 mean = m_sum * (1.0 / m_n);
        const double var = std::max(0.0, m_sumSq / m_n - Dot(mean, mean));
        if (m_haveMean && m_n >= 10 && Length(mean - m_lastMean) < kStill) {
            m_varSum += var;
            ++m_windows;
        }
        m_lastMean = mean;
        m_haveMean = true;
        m_n = 0;
    }

    Vec3   m_sum;
    double m_sumSq = 0;
    int    m_n = 0;
    double m_start = 0;
    Vec3   m_lastMean;
    bool   m_haveMean = false;
    double m_varSum = 0;
    int    m_windows = 0;
};

} // namespace cf
