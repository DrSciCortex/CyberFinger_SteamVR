# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

#!/usr/bin/env python3
"""
CyberFinger BLE-to-UDP Bridge (WinRT native)
Connects to both CyberFinger ESP32 devices via WinRT GATT,
reads VR controller input notifications, and forwards them
to the SteamVR merged_controller driver via UDP.

Requirements:
    pip install winrt-Windows.Devices.Bluetooth
    pip install winrt-Windows.Devices.Bluetooth.GenericAttributeProfile
    pip install winrt-Windows.Devices.Enumeration
    pip install winrt-Windows.Storage.Streams
    pip install winrt-Windows.Foundation

Usage:
    python cyberfinger_bridge.py
    python cyberfinger_bridge.py --debug
    python cyberfinger_bridge.py --left AA:BB:CC:DD:EE:FF --right BB:CC:DD:EE:FF:00
"""

import asyncio
import argparse
import struct
import socket
import time
import sys

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"
VR_CTRL_UUID    = "0000cf02-0000-1000-8000-00805f9b34fb"

GAMEPAD_MAGIC    = 0x50474643
GAMEPAD_PACK_FMT = "<IBBhhBB"
INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)

BUTTON_NAMES = {0x01: "TRIG", 0x02: "GRIP", 0x04: "B", 0x08: "JCLK", 0x10: "A"}


def fmt_buttons(btn):
    parts = [name for bit, name in BUTTON_NAMES.items() if btn & bit]
    return "+".join(parts) if parts else "none"


def ibuffer_to_bytes(ibuffer):
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(ibuffer)
    length = dr.unconsumed_buffer_length
    result = bytearray()
    for _ in range(length):
        result.append(dr.read_byte())
    return bytes(result)


class CyberFingerBridge:
    def __init__(self, udp_port=27015, debug=False):
        self.udp_port = udp_port
        self.debug = debug
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = ("127.0.0.1", udp_port)
        self.left_count = 0
        self.right_count = 0
        self.last_status_time = 0
        self.last_buttons = [0, 0]
        self.first_packet = [True, True]
        self._last_joy = [0.0, 0.0]

    def _handle_data(self, data):
        if len(data) < INPUT_REPORT_SIZE:
            return
        hand, buttons, joy_x, joy_y, trigger, battery, seq = \
            struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])
        h = min(hand, 1)
        hn = "L" if h == 0 else "R"

        if self.first_packet[h]:
            self.first_packet[h] = False
            print(f"[Bridge] * First packet from {hn}: btn=0x{buttons:02X}({fmt_buttons(buttons)}) "
                  f"joy=({joy_x},{joy_y}) trig={trigger} bat={battery}% seq={seq}")

        if buttons != self.last_buttons[h]:
            old = self.last_buttons[h]
            pressed = buttons & ~old
            released = ~buttons & old
            parts = []
            if pressed: parts.append(f"+{fmt_buttons(pressed)}")
            if released: parts.append(f"-{fmt_buttons(released)}")
            print(f"[Bridge] {hn} BTN: 0x{buttons:02X} ({fmt_buttons(buttons)}) [{' '.join(parts)}]")
            self.last_buttons[h] = buttons

        now = time.time()
        if self.debug and now - self._last_joy[h] > 1.0:
            if abs(joy_x) > 3000 or abs(joy_y) > 3000:
                self._last_joy[h] = now
                print(f"[Bridge] {hn} JOY: ({joy_x:+6d},{joy_y:+6d}) trig={trigger} bat={battery}%")

        pkt = struct.pack(GAMEPAD_PACK_FMT,
                          GAMEPAD_MAGIC, hand, buttons,
                          joy_x, joy_y, trigger, battery)
        self.sock.sendto(pkt, self.target)

        if hand == 0:
            self.left_count += 1
        else:
            self.right_count += 1

        if now - self.last_status_time > 5.0:
            self.last_status_time = now
            print(f"[Bridge] -- L:{self.left_count} R:{self.right_count} packets forwarded --")

    async def find_devices(self, left_mac=None, right_mac=None):
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus

        print("[Bridge] Enumerating devices...")
        all_devices = await DeviceInformation.find_all_async()

        cf_ids = set()
        for dev in all_devices:
            name = dev.name or ""
            dev_id = dev.id or ""
            if "cyberfinger" in name.lower() and "bthledevice" in dev_id.lower():
                cf_ids.add(dev_id)

        seen = {}
        for dev_id in cf_ids:
            try:
                ble_dev = await BluetoothLEDevice.from_id_async(dev_id)
                if not ble_dev:
                    continue
                raw = ble_dev.bluetooth_address
                mac = ":".join(f"{(raw >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
                name = ble_dev.name or "?"
                connected = (ble_dev.connection_status == BluetoothConnectionStatus.CONNECTED)
                if mac not in seen or connected:
                    seen[mac] = (name, connected, ble_dev)
            except Exception:
                pass

        left_dev = right_dev = None
        for mac, (name, connected, ble_dev) in seen.items():
            if not connected:
                continue
            nl = name.lower()
            if left_mac and mac.upper() == left_mac.upper():
                left_dev = (mac, name, ble_dev)
                print(f"  LEFT:  {name}  ->  {mac}")
            elif right_mac and mac.upper() == right_mac.upper():
                right_dev = (mac, name, ble_dev)
                print(f"  RIGHT: {name}  ->  {mac}")
            elif not left_mac and "left" in nl and not left_dev:
                left_dev = (mac, name, ble_dev)
                print(f"  LEFT:  {name}  ->  {mac}")
            elif not right_mac and "right" in nl and not right_dev:
                right_dev = (mac, name, ble_dev)
                print(f"  RIGHT: {name}  ->  {mac}")
            if left_dev and right_dev:
                break

        return left_dev, right_dev

    async def setup_device(self, label, ble_dev):
        """Get CF01 char and try notification subscription. Returns (mode, char, token)."""
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
            GattClientCharacteristicConfigurationDescriptorValue,
        )

        svc_result = await ble_dev.get_gatt_services_async()
        if svc_result.status != GattCommunicationStatus.SUCCESS:
            print(f"[Bridge] {label}: Failed to get GATT services")
            return None

        vr_svc = None
        for svc in svc_result.services:
            if "cf00" in str(svc.uuid).lower():
                vr_svc = svc
                break
        if not vr_svc:
            print(f"[Bridge] {label}: 0xCF00 service not found!")
            return None

        char_result = await vr_svc.get_characteristics_async()
        if char_result.status != GattCommunicationStatus.SUCCESS:
            print(f"[Bridge] {label}: Failed to get characteristics")
            return None

        vr_input = None
        for char in char_result.characteristics:
            if "cf01" in str(char.uuid).lower():
                vr_input = char
                break
        if not vr_input:
            print(f"[Bridge] {label}: CF01 not found!")
            return None

        # Clear any stale subscription from a previous run
        try:
            await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
        except Exception:
            pass
        await asyncio.sleep(0.1)

        # Try notification subscription
        bridge = self

        def on_notify(sender, args):
            try:
                data = ibuffer_to_bytes(args.characteristic_value)
                bridge._handle_data(data)
            except Exception:
                pass

        try:
            cccd_result = await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            if cccd_result == GattCommunicationStatus.SUCCESS:
                token = vr_input.add_value_changed(on_notify)
                print(f"[Bridge] {label}: Notifications active")
                return ("notify", vr_input, token)
            else:
                print(f"[Bridge] {label}: CCCD write failed, using polling")
                return ("poll", vr_input, None)
        except Exception as e:
            print(f"[Bridge] {label}: Notify failed ({e}), using polling")
            return ("poll", vr_input, None)

    async def poll_char(self, char):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
        try:
            result = await char.read_value_async()
            if result.status == GattCommunicationStatus.SUCCESS:
                data = ibuffer_to_bytes(result.value)
                self._handle_data(data)
        except Exception:
            pass

    async def run(self, left_mac=None, right_mac=None):
        print(f"[Bridge] CyberFinger BLE-to-UDP Bridge (WinRT)")
        print(f"[Bridge] UDP target: 127.0.0.1:{self.udp_port}")
        print()

        left_dev, right_dev = await self.find_devices(left_mac, right_mac)

        if not left_dev and not right_dev:
            print("[Bridge] ERROR: No connected CyberFinger devices found!")
            print("[Bridge] Run ble_diagnostic.py to find addresses.")
            return

        subscriptions = []
        polling_chars = []

        # Subscribe sequentially to avoid Windows BLE resource exhaustion
        if left_dev:
            result = await self.setup_device("LEFT", left_dev[2])
            if result:
                mode, char, token = result
                if mode == "notify":
                    subscriptions.append(("LEFT", char, token))
                else:
                    polling_chars.append(("LEFT", char))

        if right_dev:
            result = await self.setup_device("RIGHT", right_dev[2])
            if result:
                mode, char, token = result
                if mode == "notify":
                    subscriptions.append(("RIGHT", char, token))
                else:
                    polling_chars.append(("RIGHT", char))

        if not subscriptions and not polling_chars:
            print("[Bridge] ERROR: No data channels established!")
            return

        print()
        for label, _, _ in subscriptions:
            print(f"[Bridge] {label}: notify mode")
        for label, _ in polling_chars:
            print(f"[Bridge] {label}: polling mode (~100Hz)")
        print(f"[Bridge] Running. Press Ctrl+C to stop.")
        print()

        try:
            while True:
                for _, char in polling_chars:
                    await self.poll_char(char)
                if polling_chars:
                    await asyncio.sleep(0.01)
                else:
                    await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[Bridge] Shutting down...")
        finally:
            for _, char, token in subscriptions:
                try:
                    char.remove_value_changed(token)
                except Exception:
                    pass
            self.sock.close()
            print(f"[Bridge] Done. L:{self.left_count} R:{self.right_count} total.")


def main():
    parser = argparse.ArgumentParser(description="CyberFinger BLE-to-UDP Bridge")
    parser.add_argument("--port", type=int, default=27015, help="UDP port (default: 27015)")
    parser.add_argument("--left", type=str, default=None, help="BLE MAC of left CyberFinger")
    parser.add_argument("--right", type=str, default=None, help="BLE MAC of right CyberFinger")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    bridge = CyberFingerBridge(udp_port=args.port, debug=args.debug)
    asyncio.run(bridge.run(left_mac=args.left, right_mac=args.right))


if __name__ == "__main__":
    main()
