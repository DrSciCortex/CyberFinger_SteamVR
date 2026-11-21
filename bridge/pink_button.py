# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""The right CyberFinger's pink button in VR mode: the default microphone's mute (mic_mute.py: Windows, Linux), or one
of the MoreFluxActions mod's FluxActions in Resonite. (The left pink button is SteamVR's system button, handled by
the driver.)"""

import threading

import mic_mute
from flux_actions import FluxActionSender

PINK = 0x01                      # cyberfinger_report.PINK


class PinkButton:
    """on_input from the BLE thread with each report's extension buttons.

    action: a callable returning what the button does right now: 0 toggles the microphone on each click, confirmed
    on the CyberFinger's motor (a pulse train: muted; one buzz: live); 1..42 fires that FluxAction, pressed and released
    with the button, without vibration (the flux it drives can ask for its own); -1 leaves it to SteamVR (the driver's
    /input/pink, for the app's binding) and does nothing here. The microphone is switched on a worker thread.
    The vibration is the bridge's alone: the firmware only plays what it's sent (VR_CMD_HAPTIC).
    haptics: a callable returning the current cyberfinger_control.HapticSender (or None)."""

    def __init__(self, log, haptics=lambda: None, action=lambda: 0, hand=1, sender=None):
        self._log, self._haptics, self._action, self._hand = log, haptics, action, hand
        self._sender = sender or FluxActionSender(log)
        self._down = False
        self._firing = 0             # the FluxAction pressed, released with the button even if the setting changes
        self._busy = threading.Lock()

    def on_input(self, hand, buttons2):
        if hand != self._hand:
            return
        down = bool(buttons2 & PINK)
        if down and not self._down:
            action = self._action()
            if action < 0:
                pass                    # SteamVR's: the driver hands it to the app's binding
            elif action:
                self._firing = action
                self._sender.send(action, True)
            elif self._busy.acquire(blocking=False):
                threading.Thread(target=self._toggle_mic, daemon=True).start()
        elif not down and self._down and self._firing:
            self._sender.send(self._firing, False)
            self._firing = 0
        self._down = down

    def _buzz(self, seconds, hz):
        haptics = self._haptics()
        if haptics is not None:
            haptics.request(self._hand, seconds, hz, 1.0)

    def _toggle_mic(self):
        try:
            muted = mic_mute.toggle()
            if muted is None:
                self._log("Mic: no microphone found")
                return
            self._log(f"Mic: {'muted' if muted else 'live'} (right pink button)")
            self._buzz(0.35 if muted else 0.15, 8.0 if muted else 0.0)
        except OSError as e:
            self._log(f"Mic: could not switch the microphone: {e}")
        finally:
            self._busy.release()
