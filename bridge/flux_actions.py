# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Fire the MoreFluxActions mod's FluxAction1..42 in Resonite straight from the bridge.

The mod's engine half listens on 127.0.0.1:PORT for one UDP datagram per change, naming the dynamic impulse it
fires: b"FluxAction42.Pressed", b"FluxAction42.Released". This skips SteamVR's bindings, so the bridge can pick the
action at run time (the right pink button, when set to a FluxAction).
"""

import socket

PORT = 42042                     # MoreFluxActions' FluxActionProtocol.UdpPort
COUNT = 42                       # FluxAction1..COUNT


def datagram(action, pressed):
    if not 1 <= action <= COUNT:
        raise ValueError(f"FluxAction{action}: only 1..{COUNT} exist")
    return f"FluxAction{action}.{'Pressed' if pressed else 'Released'}".encode("ascii")


class FluxActionSender:
    """Sends FluxAction presses and releases to the mod. Nothing answers: without Resonite and the mod running, the
    datagrams are simply dropped."""

    def __init__(self, log=print, port=PORT):
        self._log, self._addr = log, ("127.0.0.1", port)
        self._sock = None

    def send(self, action, pressed):
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.sendto(datagram(action, pressed), self._addr)
        except (OSError, ValueError) as e:
            self._log(f"FluxAction{action}: not sent: {e}")
