/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// GestureClick.h — a hand-tracking gesture as a button
//
// Hand tracking flickers at the edges of a pose; bound to an action (a Flux Action in Resonite), every flicker
// would be a press and a release. The click turns on once the gesture has held for `on` seconds and off once it
// has been gone for `off`.
// ═══════════════════════════════════════════════════════════════════════════

namespace cf {

class GestureClick {
public:
    explicit GestureClick(double on = 0.06, double off = 0.12) : m_on(on), m_off(off) {}

    // Feed the raw gesture each frame; returns the debounced click.
    bool Update(bool raw, double now) {
        if (raw == m_state) {
            m_since = -1;
            return m_state;
        }
        if (m_since < 0) m_since = now;
        if (now - m_since >= (raw ? m_on : m_off)) {
            m_state = raw;
            m_since = -1;
        }
        return m_state;
    }

    void Reset() {
        m_state = false;
        m_since = -1;
    }

private:
    double m_on, m_off;
    double m_since = -1;               // the raw gesture has differed from the click since then (-1: it hasn't)
    bool   m_state = false;
};

} // namespace cf
