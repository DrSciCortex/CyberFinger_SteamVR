# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Bridge → CyberFinger commands on the VR GATT control characteristic (0xCF02; firmware src/vr_gatt.h).

  VR_CMD_HAPTIC  0x10   cmd u8, amplitude u8 (0..255, 0 stops), duration_ms u16 LE, frequency_hz u16 LE  (6 bytes)

A SteamVR haptic event (the driver's CFHP, driver_link.py) becomes one VR_CMD_HAPTIC, written without response.
Firmware 1.3.3+ drives its DRV2605L + ERM motor with it (src/haptics.h: every pulse lasts at least ~35 ms, the
motor's spin-up; back-to-back requests extend the vibration); older firmware logs an unknown command and ignores
it, as do boards without the motor.

HapticSender does the writing from the BLE manager's asyncio loop, per hand, coalescing: while a write is in
flight, later requests merge into one (the latest amplitude and frequency, the longest duration), so an app that
pulses every frame can't queue up a backlog on the BLE link.
"""

import asyncio
import struct

CONTROL_UUID = "0000cf02-0000-1000-8000-00805f9b34fb"
CMD_HAPTIC = 0x10
HAPTIC = struct.Struct("<BBHH")


def pack_haptic(duration_s, frequency_hz, amplitude):
    """VR_CMD_HAPTIC for a SteamVR haptic event (seconds, Hz, amplitude 0..1)."""
    amp = max(0, min(255, int(round(float(amplitude) * 255))))
    if amp == 0 and amplitude > 0:
        amp = 1                                   # a faint request still vibrates (0 would stop the motor)
    dur = max(0, min(65535, int(round(float(duration_s) * 1000))))
    freq = max(0, min(65535, int(round(float(frequency_hz)))))
    return HAPTIC.pack(CMD_HAPTIC, amp, dur, freq)


def pack_haptic_stop():
    return HAPTIC.pack(CMD_HAPTIC, 0, 0, 0)


class BleakControl:
    """The control characteristic on a bleak client (the Linux bridge): what HapticSender.attach() takes there in
    place of a WinRT GattCharacteristic. char: its UUID, or the BleakGATTCharacteristic found on the client."""

    def __init__(self, client, char=CONTROL_UUID):
        self.client, self.char = client, char

    async def write(self, payload):
        # Without response, as on Windows, unless the characteristic only takes acknowledged writes.
        props = getattr(self.char, "properties", None) or ()
        response = "write-without-response" not in props and "write" in props
        await self.client.write_gatt_char(self.char, bytes(payload), response=response)   # raises on failure
        return True


async def write_without_response(char, payload):
    """Write bytes to the control characteristic without waiting for the CyberFinger's response. Returns success.
    char: a WinRT GattCharacteristic (Windows), or a BleakControl (Linux)."""
    if isinstance(char, BleakControl):
        return await char.write(payload)
    from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus, GattWriteOption
    from winrt.windows.storage.streams import DataWriter
    writer = DataWriter()
    writer.write_bytes(payload)
    status = await char.write_value_with_option_async(writer.detach_buffer(), GattWriteOption.WRITE_WITHOUT_RESPONSE)
    return status == GattCommunicationStatus.SUCCESS


class HapticSender:
    """Forwards haptic requests to each CyberFinger's control characteristic. attach() from the BLE loop once the
    characteristic is found; request() from any thread."""

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        self._loop = None
        self._chars = {}          # hand (0 left, 1 right) -> GattCharacteristic (WinRT) or BleakControl
        self._pending = {}        # hand -> (duration_s, frequency_hz, amplitude), loop thread only
        self._busy = set()        # hands with a write in flight
        self._sent = set()        # hands whose first write was logged
        self._failed = set()      # hands whose failure was logged

    def attach(self, hand, char):
        """BLE loop: the CyberFinger's control characteristic, found."""
        self._loop = asyncio.get_running_loop()
        self._chars[hand] = char

    def detach_all(self):
        self._chars.clear()
        self._pending.clear()
        self._loop = None

    def available(self, hand):
        return hand in self._chars and self._loop is not None

    def request(self, hand, duration_s, frequency_hz, amplitude):
        """Any thread: vibrate that hand's CyberFinger. False when it isn't reachable."""
        loop = self._loop
        if loop is None or hand not in self._chars:
            return False
        try:
            loop.call_soon_threadsafe(self._queue, hand, duration_s, frequency_hz, amplitude)
        except RuntimeError:                      # the loop just closed
            return False
        return True

    def _queue(self, hand, duration_s, frequency_hz, amplitude):
        pending = self._pending.get(hand)
        if pending is not None:
            duration_s = max(duration_s, pending[0])
        self._pending[hand] = (duration_s, frequency_hz, amplitude)
        if hand not in self._busy:
            self._busy.add(hand)
            asyncio.ensure_future(self._drain(hand))

    async def _drain(self, hand):
        name = "right" if hand else "left"
        try:
            while hand in self._pending:
                duration_s, frequency_hz, amplitude = self._pending.pop(hand)
                char = self._chars.get(hand)
                if char is None:
                    break
                ok = await write_without_response(char, pack_haptic(duration_s, frequency_hz, amplitude))
                if ok and hand not in self._sent:
                    self._sent.add(hand)
                    self._log(f"Haptics: forwarding to the {name} CyberFinger")
                elif not ok and hand not in self._failed:
                    self._failed.add(hand)
                    self._log(f"Haptics: the {name} CyberFinger refused the vibration command")
        except Exception as e:                    # noqa: BLE001 — a BLE hiccup must not kill the loop
            if hand not in self._failed:
                self._failed.add(hand)
                self._log(f"Haptics: writing to the {name} CyberFinger failed: {e!r}")
        finally:
            self._busy.discard(hand)
