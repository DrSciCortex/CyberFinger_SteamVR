/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// LongPress.h — a button with a long-press function (the black button: its
// long press is a second input, /input/a_hold)
//
// A press is held back until it's known which it is: released before `hold`,
// it's passed on as a click when it ends (reported for at least `click` seconds
// so every app sees it); held for `hold`, the long press fires once and the app
// never sees the button. Held back, because apps act on the press: Resonite opens
// its dash on it, so a long press passed on as it happened would open the dash.
// The price: the button reports on release, and can't be held in an app.
// ═══════════════════════════════════════════════════════════════════════════

namespace cf {

class LongPress {
public:
    explicit LongPress(double hold = 0.8, double click = 0.06) : m_hold(hold), m_click(click) {}

    // Feed the button state each frame; returns the state to report. `fired` turns true on the frame the long
    // press is recognized.
    bool Update(bool down, double now, bool& fired) {
        fired = false;
        switch (m_state) {
            case State::Idle:
                if (down) { m_state = State::Pressed; m_since = now; }
                return false;
            case State::Pressed:
                if (!down) { m_state = State::Clicking; m_since = now; return true; }   // a click: passed on now
                if (now - m_since >= m_hold) { m_state = State::Fired; fired = true; }
                return false;
            case State::Fired:
                if (!down) m_state = State::Idle;
                return false;
            case State::Clicking:
                if (now - m_since < m_click) return true;
                m_state = down ? State::Pressed : State::Idle;   // pressed again meanwhile: a new press
                m_since = now;
                return false;
        }
        return false;
    }

    // The long press has fired and the button is still held (until the release, which Update reports).
    bool Held() const { return m_state == State::Fired; }

    // Button state unknown (CyberFinger lost, device released): nothing pressed.
    void Reset() { m_state = State::Idle; }

private:
    enum class State { Idle, Pressed, Fired, Clicking };
    double m_hold, m_click;
    State  m_state = State::Idle;
    double m_since = 0;
};

} // namespace cf
