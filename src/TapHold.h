/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// TapHold.h — "tap to hold" for the grab button (/input/grab)
//
// A press shorter than the tap time latches the grab until the next press; a
// longer press grabs only while it lasts. The press that ends a latched grab
// releases it when it ends, however long it was. SteamVR's binding modes can't
// express this (a toggle button flips on every press), so the driver does it.
// ═══════════════════════════════════════════════════════════════════════════

namespace cf {

class TapHold {
public:
    explicit TapHold(double tapSeconds = 0.2) : m_tap(tapSeconds) {}

    // 0 turns latching off: the grab follows the button.
    void SetTapTime(double seconds) { m_tap = seconds; }

    // Feed the button state each frame; returns whether the grab is held.
    bool Update(bool down, double now) {
        if (down && !m_down) {                 // press
            m_pressTime = now;
            m_endsLatch = m_latched;
        } else if (!down && m_down) {          // release
            if (m_endsLatch) m_latched = false;
            else if (now - m_pressTime < m_tap) m_latched = true;
            m_endsLatch = false;
        }
        m_down = down;
        return m_down || m_latched;
    }

    // Button state unknown (CyberFinger lost, device released): let go.
    void Reset() { m_down = m_latched = m_endsLatch = false; }

private:
    double m_tap;
    double m_pressTime = 0;
    bool   m_down = false;
    bool   m_latched = false;
    bool   m_endsLatch = false;                // the current press started while latched
};

} // namespace cf
