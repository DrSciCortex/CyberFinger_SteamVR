# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

#!/usr/bin/env python3
"""
CyberFinger Gamepad Bridge

Connects to both CyberFinger ESP32 devices via BLE (WinRT) and combines
their inputs into a single virtual Xbox 360 gamepad using ViGEm.

Mapping (joy.cpl button numbers):
  RIGHT HAND                         XBOX GAMEPAD
  ─────────────────────────────────────────────────
  0x01 Trigger                 →     btn 1  (A)
  0x02 Grip                    →     btn 2  (B)
  0x04 B button                →     btn 6  (RB)
  0x08 Joystick click          →     btn 10 (R3)
  0x10 A button                →     btn 8  (Start)

  LEFT HAND                          XBOX GAMEPAD
  ─────────────────────────────────────────────────
  0x01 Trigger                 →     btn 3  (X)
  0x02 Grip                    →     btn 4  (Y)
  0x04 B button                →     btn 5  (LB)
  0x08 Joystick click          →     btn 9  (L3)
  0x10 A button                →     btn 7  (Back)

  Joysticks: L→Left Stick, R→Right Stick (Y axis inverted)

Requirements:
    pip install vgamepad
    pip install winrt-Windows.Devices.Bluetooth
    pip install winrt-Windows.Devices.Bluetooth.GenericAttributeProfile
    pip install winrt-Windows.Devices.Enumeration
    pip install winrt-Windows.Storage.Streams
    pip install winrt-Windows.Foundation

Usage:
    python cyberfinger_gamepad_bridge.py
    python cyberfinger_gamepad_bridge.py --debug
    python cyberfinger_gamepad_bridge.py --left AA:BB:CC:DD:EE:FF --right BB:CC:DD:EE:FF:00
"""

import asyncio
import argparse
import struct
import time
import sys

try:
    import vgamepad as vg
except ImportError:
    print("ERROR: vgamepad not installed. Run: pip install vgamepad")
    print("       (ViGEmBus driver must also be installed: https://github.com/nefarius/ViGEmBus/releases)")
    sys.exit(1)

# ── BLE protocol (matches ESP32 firmware) ────────────────────────────────

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"

INPUT_REPORT_FMT  = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)

BTN_TRIGGER = 0x01
BTN_GRIP    = 0x02
BTN_B       = 0x04
BTN_JCLICK  = 0x08
BTN_A       = 0x10

BUTTON_NAMES = {
    BTN_TRIGGER: "TRIG",
    BTN_GRIP:    "GRIP",
    BTN_B:       "B",
    BTN_JCLICK:  "JCLK",
    BTN_A:       "A",
}


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


# ── Per-hand state ───────────────────────────────────────────────────────

class HandState:
    def __init__(self):
        self.buttons = 0
        self.joy_x = 0
        self.joy_y = 0
        self.trigger = 0
        self.battery = 100
        self.timestamp = 0.0
        self.packet_count = 0

    @property
    def btn_trigger(self): return bool(self.buttons & BTN_TRIGGER)
    @property
    def btn_grip(self):    return bool(self.buttons & BTN_GRIP)
    @property
    def btn_b(self):       return bool(self.buttons & BTN_B)
    @property
    def btn_jclick(self):  return bool(self.buttons & BTN_JCLICK)
    @property
    def btn_a(self):       return bool(self.buttons & BTN_A)

    @property
    def joy_x_float(self):
        return max(-1.0, min(1.0, self.joy_x / 32767.0))

    @property
    def joy_y_float(self):
        return max(-1.0, min(1.0, self.joy_y / 32767.0))

    @property
    def trigger_float(self):
        if self.trigger > 10:
            return self.trigger / 255.0
        return 1.0 if self.btn_trigger else 0.0


# ── Main bridge ──────────────────────────────────────────────────────────

class CyberFingerGamepadBridge:
    def __init__(self, debug=False):
        self.debug = debug
        self.left = HandState()
        self.right = HandState()
        self.last_buttons = [0, 0]
        self.first_packet = [True, True]
        self.last_status_time = 0.0
        self.last_log_joy = [0.0, 0.0]

        print("[Gamepad] Creating virtual Xbox 360 controller...")
        self.gamepad = vg.VX360Gamepad()
        print("[Gamepad] Virtual controller created!")

    def _handle_data(self, data):
        if len(data) < INPUT_REPORT_SIZE:
            return

        hand, buttons, joy_x, joy_y, trigger, battery, seq = \
            struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])

        h = min(hand, 1)
        hn = "L" if h == 0 else "R"
        state = self.left if h == 0 else self.right

        state.buttons = buttons
        state.joy_x = joy_x
        state.joy_y = joy_y
        state.trigger = trigger
        state.battery = battery
        state.timestamp = time.time()
        state.packet_count += 1

        if self.first_packet[h]:
            self.first_packet[h] = False
            print(f"[Gamepad] First packet {hn}: btn=0x{buttons:02X}({fmt_buttons(buttons)}) "
                  f"joy=({joy_x},{joy_y}) trig={trigger} bat={battery}%")

        if buttons != self.last_buttons[h]:
            old = self.last_buttons[h]
            pressed = buttons & ~old
            released = ~buttons & old
            parts = []
            if pressed:
                parts.append(f"+{fmt_buttons(pressed)}")
            if released:
                parts.append(f"-{fmt_buttons(released)}")
            print(f"[Gamepad] {hn} BTN: 0x{buttons:02X} ({fmt_buttons(buttons)}) [{' '.join(parts)}]")
            self.last_buttons[h] = buttons

        if self.debug:
            now = time.time()
            if now - self.last_log_joy[h] > 1.0:
                if abs(joy_x) > 3000 or abs(joy_y) > 3000:
                    self.last_log_joy[h] = now
                    print(f"[Gamepad] {hn} JOY: ({joy_x:+6d},{joy_y:+6d}) trig={trigger}")

        self._update_gamepad()

    def _update_gamepad(self):
        """Map combined left+right CyberFinger state to Xbox 360 controller.

        Button mapping (joy.cpl numbers → Xbox 360):
          1=A  2=B  3=X  4=Y  5=LB  6=RB  7=Back  8=Start  9=L3  10=R3

        RIGHT HAND:
          0x01 TRIG  → btn 1  (A)
          0x02 GRIP  → btn 2  (B)
          0x04 B     → btn 6  (RB)
          0x08 JCLK  → btn 10 (R3)
          0x10 A     → btn 8  (Start)

        LEFT HAND:
          0x01 TRIG  → btn 3  (X)
          0x02 GRIP  → btn 4  (Y)
          0x04 B     → btn 5  (LB)
          0x08 JCLK  → btn 9  (L3)
          0x10 A     → btn 7  (Back)
        """
        gp = self.gamepad
        L = self.left
        R = self.right

        gp.reset()

        # Sticks (Y axis inverted)
        gp.left_joystick_float(x_value_float=L.joy_x_float, y_value_float=-L.joy_y_float)
        gp.right_joystick_float(x_value_float=R.joy_x_float, y_value_float=-R.joy_y_float)

        # Right hand buttons
        if R.btn_trigger:  # 0x01 → btn 1 (A)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
        if R.btn_grip:     # 0x02 → btn 2 (B)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
        if R.btn_b:        # 0x04 → btn 6 (RB)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
        if R.btn_jclick:   # 0x08 → btn 10 (R3)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)
        if R.btn_a:        # 0x10 → btn 8 (Start)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_START)

        # Left hand buttons
        if L.btn_trigger:  # 0x01 → btn 3 (X)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
        if L.btn_grip:     # 0x02 → btn 4 (Y)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
        if L.btn_b:        # 0x04 → btn 5 (LB)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
        if L.btn_jclick:   # 0x08 → btn 9 (L3)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
        if L.btn_a:        # 0x10 → btn 7 (Back)
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)

        gp.update()

    # ── BLE discovery ────────────────────────────────────────────────────

    async def find_devices(self, left_mac=None, right_mac=None):
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus

        print("[Gamepad] ── BLE Discovery ─────────────────────────")
        print("[Gamepad] Enumerating system devices...")
        all_devices = await DeviceInformation.find_all_async()
        print(f"[Gamepad] Total system devices: {len(all_devices)}")

        # Pass 1: collect CyberFinger BLE entries with their enumeration names
        # IMPORTANT: we keep the name from enumeration because from_id_async()
        # returns a generic "Bluetooth XX:XX:XX..." name instead of the real
        # advertised device name.
        cf_ble_entries = []
        for dev in all_devices:
            name = dev.name or ""
            dev_id = dev.id or ""
            if "cyberfinger" in name.lower() and "bthledevice" in dev_id.lower():
                cf_ble_entries.append((name, dev_id))

        print(f"[Gamepad] CyberFinger BLE entries: {len(cf_ble_entries)}")
        if not cf_ble_entries:
            print("[Gamepad] No CyberFinger BLE devices in system list!")
            return None, None

        # Pass 2: open each, get MAC + connection status, use enum name
        seen = {}
        for enum_name, dev_id in cf_ble_entries:
            try:
                ble_dev = await BluetoothLEDevice.from_id_async(dev_id)
                if not ble_dev:
                    continue
                raw = ble_dev.bluetooth_address
                mac = ":".join(f"{(raw >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
                connected = (ble_dev.connection_status == BluetoothConnectionStatus.CONNECTED)

                if mac not in seen or (connected and not seen[mac][1]):
                    seen[mac] = (enum_name, connected, ble_dev)
            except Exception:
                pass

        if not seen:
            print("[Gamepad] Could not open any CyberFinger BLE devices!")
            return None, None

        print(f"\n[Gamepad] ── Unique devices: {len(seen)} ──")
        for mac, (enum_name, connected, ble_dev) in sorted(seen.items()):
            conn_str = "CONNECTED" if connected else "disconnected"
            nl = enum_name.lower()
            if "left" in nl:
                lr_tag = "←LEFT"
            elif "right" in nl:
                lr_tag = "RIGHT→"
            else:
                lr_tag = "?UNKNOWN?"
            print(f"[Gamepad]   [{conn_str:>12}] [{lr_tag:>9}]  \"{enum_name}\"  {mac}")

        connected_devs = [(mac, enum_name, ble_dev)
                          for mac, (enum_name, connected, ble_dev) in seen.items()
                          if connected]

        if not connected_devs:
            print("\n[Gamepad] ERROR: No CONNECTED CyberFinger devices!")
            return None, None

        print(f"[Gamepad] Connected: {len(connected_devs)} device(s)")

        left_dev = right_dev = None

        if left_mac or right_mac:
            print(f"[Gamepad] MAC overrides: --left={left_mac} --right={right_mac}")
        for mac, enum_name, ble_dev in connected_devs:
            if left_mac and mac.upper() == left_mac.upper():
                left_dev = (mac, enum_name, ble_dev)
                print(f"[Gamepad]   LEFT  ← MAC match: \"{enum_name}\" {mac}")
            if right_mac and mac.upper() == right_mac.upper():
                right_dev = (mac, enum_name, ble_dev)
                print(f"[Gamepad]   RIGHT ← MAC match: \"{enum_name}\" {mac}")

        for mac, enum_name, ble_dev in connected_devs:
            if left_dev and left_dev[0] == mac:
                continue
            if right_dev and right_dev[0] == mac:
                continue

            nl = enum_name.lower()
            if not left_dev and "left" in nl:
                left_dev = (mac, enum_name, ble_dev)
                print(f"[Gamepad]   LEFT  ← name match: \"{enum_name}\" {mac}")
            elif not right_dev and "right" in nl:
                right_dev = (mac, enum_name, ble_dev)
                print(f"[Gamepad]   RIGHT ← name match: \"{enum_name}\" {mac}")
            else:
                print(f"[Gamepad]   UNASSIGNED: \"{enum_name}\" {mac}")
                print(f"[Gamepad]     (use --left or --right MAC to assign)")

        print(f"\n[Gamepad] ── Assignment result ──")
        if left_dev:
            print(f"  LEFT:  \"{left_dev[1]}\"  {left_dev[0]}")
        else:
            print(f"  LEFT:  (none)")
        if right_dev:
            print(f"  RIGHT: \"{right_dev[1]}\"  {right_dev[0]}")
        else:
            print(f"  RIGHT: (none)")
        print()

        return left_dev, right_dev

    async def setup_device(self, label, ble_dev):
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
            GattClientCharacteristicConfigurationDescriptorValue,
        )

        svc_result = await ble_dev.get_gatt_services_async()
        if svc_result.status != GattCommunicationStatus.SUCCESS:
            print(f"[Gamepad] {label}: Failed to get GATT services")
            return None

        vr_svc = None
        for svc in svc_result.services:
            if "cf00" in str(svc.uuid).lower():
                vr_svc = svc
                break
        if not vr_svc:
            print(f"[Gamepad] {label}: 0xCF00 service not found!")
            return None

        char_result = await vr_svc.get_characteristics_async()
        if char_result.status != GattCommunicationStatus.SUCCESS:
            print(f"[Gamepad] {label}: Failed to get characteristics")
            return None

        vr_input = None
        for char in char_result.characteristics:
            if "cf01" in str(char.uuid).lower():
                vr_input = char
                break
        if not vr_input:
            print(f"[Gamepad] {label}: CF01 not found!")
            return None

        try:
            await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
        except Exception:
            pass
        await asyncio.sleep(0.1)

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
                print(f"[Gamepad] {label}: Notifications active")
                return ("notify", vr_input, token)
            else:
                print(f"[Gamepad] {label}: CCCD write failed, using polling")
                return ("poll", vr_input, None)
        except Exception as e:
            print(f"[Gamepad] {label}: Notify failed ({e}), using polling")
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
        print()
        print("═══════════════════════════════════════════════════")
        print("  CyberFinger Gamepad Bridge")
        print("  Combines L+R into virtual Xbox 360 controller")
        print("═══════════════════════════════════════════════════")
        print()

        left_dev, right_dev = await self.find_devices(left_mac, right_mac)

        if not left_dev and not right_dev:
            print("[Gamepad] ERROR: No CyberFinger devices could be assigned!")
            print("[Gamepad] Run ble_diagnostic.py to inspect the BLE stack.")
            return

        subscriptions = []
        polling_chars = []

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
            print("[Gamepad] ERROR: No data channels established!")
            return

        print()
        print("[Gamepad] Button mapping (joy.cpl #):")
        print("  R-TRIG(0x01)→ btn1(A)   L-TRIG(0x01)→ btn3(X)")
        print("  R-GRIP(0x02)→ btn2(B)   L-GRIP(0x02)→ btn4(Y)")
        print("  R-B   (0x04)→ btn6(RB)  L-B   (0x04)→ btn5(LB)")
        print("  R-JCLK(0x08)→ btn10(R3) L-JCLK(0x08)→ btn9(L3)")
        print("  R-A   (0x10)→ btn8(Start) L-A (0x10)→ btn7(Back)")
        print("  L-Stick / R-Stick (Y inverted)")
        print()
        for label, _, _ in subscriptions:
            print(f"[Gamepad] {label}: notify mode")
        for label, _ in polling_chars:
            print(f"[Gamepad] {label}: polling mode (~100Hz)")
        print("[Gamepad] Running. Press Ctrl+C to stop.")
        print()

        try:
            while True:
                for _, char in polling_chars:
                    await self.poll_char(char)

                now = time.time()
                if now - self.last_status_time > 10.0:
                    self.last_status_time = now
                    lc = self.left.packet_count
                    rc = self.right.packet_count
                    lb = self.left.battery
                    rb = self.right.battery
                    print(f"[Gamepad] L:{lc} pkts ({lb}% bat)  R:{rc} pkts ({rb}% bat)")

                if polling_chars:
                    await asyncio.sleep(0.01)
                else:
                    await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[Gamepad] Shutting down...")
        finally:
            for _, char, token in subscriptions:
                try:
                    char.remove_value_changed(token)
                except Exception:
                    pass
            self.gamepad.reset()
            self.gamepad.update()
            print(f"[Gamepad] Done. L:{self.left.packet_count} R:{self.right.packet_count} total.")


def main():
    parser = argparse.ArgumentParser(description="CyberFinger Gamepad Bridge")
    parser.add_argument("--left", type=str, default=None, help="BLE MAC of left CyberFinger")
    parser.add_argument("--right", type=str, default=None, help="BLE MAC of right CyberFinger")
    parser.add_argument("--debug", action="store_true", help="Verbose joystick logging")
    args = parser.parse_args()

    bridge = CyberFingerGamepadBridge(debug=args.debug)
    asyncio.run(bridge.run(left_mac=args.left, right_mac=args.right))


if __name__ == "__main__":
    main()
