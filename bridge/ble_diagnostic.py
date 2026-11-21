# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

#!/usr/bin/env python3
"""
CyberFinger BLE Diagnostic — Tests simultaneous notification subscription
Clears stale CCCD subscriptions, then subscribes to both devices at once.
"""
import asyncio
import struct


def ibuffer_to_bytes(ibuffer):
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(ibuffer)
    length = dr.unconsumed_buffer_length
    result = bytearray()
    for _ in range(length):
        result.append(dr.read_byte())
    return bytes(result)


async def main():
    print("CyberFinger BLE Diagnostic — Simultaneous Subscription Test")
    print("=" * 60)

    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus,
        GattClientCharacteristicConfigurationDescriptorValue,
    )

    print("Enumerating devices...")
    devices = await DeviceInformation.find_all_async()

    cf_ids = set()
    for dev in devices:
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
            raw_addr = ble_dev.bluetooth_address
            mac = ":".join(f"{(raw_addr >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
            name = ble_dev.name or "?"
            connected = (ble_dev.connection_status == BluetoothConnectionStatus.CONNECTED)
            if mac not in seen or connected:
                seen[mac] = (name, connected, ble_dev)
        except Exception:
            pass

    connected_devices = {}
    for mac, (name, connected, ble_dev) in sorted(seen.items()):
        status = "CONNECTED" if connected else "disconnected"
        print(f"  [{status:>13}]  {name}  {mac}")
        if connected:
            connected_devices[mac] = (name, ble_dev)

    if len(connected_devices) < 1:
        print("\nNo connected CyberFingers!")
        return

    # Step 1: Find CF01 on each device and CLEAR any stale subscriptions
    print(f"\n{'='*60}")
    print("Step 1: Finding CF01 characteristics and clearing stale subscriptions...")
    print(f"{'='*60}")

    device_chars = {}  # mac -> (name, vr_input_char)
    for mac, (name, ble_dev) in connected_devices.items():
        label = "LEFT" if "left" in name.lower() else "RIGHT"
        print(f"\n  {label} ({mac}):")

        svc_result = await ble_dev.get_gatt_services_async()
        if svc_result.status != GattCommunicationStatus.SUCCESS:
            print(f"    Failed to get services")
            continue

        vr_input = None
        for svc in svc_result.services:
            if "cf00" in str(svc.uuid).lower():
                char_result = await svc.get_characteristics_async()
                if char_result.status == GattCommunicationStatus.SUCCESS:
                    for char in char_result.characteristics:
                        if "cf01" in str(char.uuid).lower():
                            vr_input = char
                            break

        if not vr_input:
            print(f"    CF01 not found!")
            continue

        print(f"    CF01 found")

        # Clear any stale subscription by writing NONE to CCCD
        try:
            clear_result = await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
            if clear_result == GattCommunicationStatus.SUCCESS:
                print(f"    Cleared stale CCCD subscription")
            else:
                print(f"    CCCD clear returned: {clear_result}")
        except Exception as e:
            print(f"    CCCD clear error: {e}")

        device_chars[mac] = (name, vr_input)

    if len(device_chars) < 1:
        print("\nNo CF01 characteristics found!")
        return

    # Small delay after clearing
    await asyncio.sleep(0.5)

    # Step 2: Subscribe to ALL devices simultaneously
    print(f"\n{'='*60}")
    print(f"Step 2: Subscribing to {len(device_chars)} devices simultaneously...")
    print(f"{'='*60}")

    notifications = {}  # mac -> list of (timestamp, data)
    tokens = {}         # mac -> token

    for mac, (name, vr_input) in device_chars.items():
        label = "LEFT" if "left" in name.lower() else "RIGHT"
        notifications[mac] = []

        def make_handler(m):
            def on_notify(sender, args):
                try:
                    data = ibuffer_to_bytes(args.characteristic_value)
                    notifications[m].append((time.time(), data))
                except Exception:
                    pass
            return on_notify

        import time

        try:
            cccd_result = await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            if cccd_result == GattCommunicationStatus.SUCCESS:
                handler = make_handler(mac)
                token = vr_input.add_value_changed(handler)
                tokens[mac] = (vr_input, token)
                print(f"  {label}: [OK] Subscription succeeded")
            else:
                print(f"  {label}: [FAIL] CCCD write returned {cccd_result}")
        except Exception as e:
            print(f"  {label}: [FAIL] {e}")

    if not tokens:
        print("\nNo subscriptions succeeded!")
        return

    # Step 3: Listen
    print(f"\n{'='*60}")
    print("Step 3: Listening for 5 seconds...")
    print(f"{'='*60}")

    import time
    await asyncio.sleep(5.0)

    # Step 4: Unsubscribe and report
    print(f"\n{'='*60}")
    print("Step 4: Results")
    print(f"{'='*60}")

    for mac, (vr_input, token) in tokens.items():
        try:
            vr_input.remove_value_changed(token)
            # Also clear the CCCD
            await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
        except Exception:
            pass

    for mac, (name, vr_input) in device_chars.items():
        label = "LEFT" if "left" in name.lower() else "RIGHT"
        data_list = notifications.get(mac, [])
        subscribed = mac in tokens

        print(f"\n  {label} ({mac}):")
        print(f"    Subscribed: {'YES' if subscribed else 'NO'}")
        print(f"    Notifications received: {len(data_list)}")
        if data_list:
            rate = len(data_list) / 5.0
            print(f"    Rate: {rate:.1f} packets/sec")
            # Show first few
            for i, (ts, d) in enumerate(data_list[:3]):
                if len(d) >= 12:
                    h, btn = d[0], d[1]
                    jx, jy = struct.unpack_from('<hh', d, 2)
                    trig, bat = d[6], d[7]
                    seq = struct.unpack_from('<I', d, 8)[0]
                    side = "L" if h == 0 else "R"
                    print(f"      [{i}] {side} btn=0x{btn:02X} joy=({jx:+6d},{jy:+6d}) trig={trig} bat={bat}% seq={seq}")

    print(f"\n{'='*60}")
    print("Cleanup complete. All subscriptions cleared.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
