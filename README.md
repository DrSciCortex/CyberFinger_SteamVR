# CyberFinger — SteamVR Driver

A SteamVR driver that merges **two CyberFingers** + **hand tracking** into a pair of
virtual VR controllers (soon with full skeletal hand data).

## Building

### Prerequisites

- CMake 3.16+
- C++17 compiler (MSVC 2019+, GCC 9+, Clang 10+)
- [OpenVR SDK](https://github.com/ValveSoftware/openvr) — clone it into the
  project root as `openvr/`, or set `-DOPENVR_SDK=<path>`
- Python WinRT
- "steamvr" branch of the CyberFingerFW_ESP32 installed on CyberFingers : https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr 

```bash
pip install winrt-Windows.Devices.Enumeration winrt-Windows.Devices.Bluetooth
pip install winrt-Windows.Devices.Bluetooth.GenericAttributeProfile winrt-Windows.Storage.Streams
```

Needs winrt v3.x 
```bash
pip show winrt-Windows.Devices.Bluetooth winrt-Windows.Devices.Enumeration winrt-runtime
```

### Windows

```bash
git clone https://github.com/ValveSoftware/openvr.git
mkdir build && cd build
cmake .. -G "Visual Studio 17 2022" -A x64
cmake --build . --config Release
```

or just use Visual Studio to build. It should install automatically in the standard steam driver location. 


### Linux

(This is WIP ... not yet confirmed working)

```bash
git clone https://github.com/ValveSoftware/openvr.git
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

## Installation

### 1. Copy the driver to SteamVR

Copy the built driver folder into SteamVR's driver directory:

```
<Steam>/steamapps/common/SteamVR/drivers/cyberfinger/
├── driver.vrdrivermanifest
├── resources/
│   ├── settings/default.vrsettings
│   └── input/
│       ├── cyberfinger_profile.json
│       └── cyberfinger_bindings.json
└── bin/
    └── win64/   (or linux64/)
        └── cyberfinger_controller.dll  (or .so)
```

(That is, if Visual Studio didn't do it already for you above)

### 2. Enable the driver

Add to `<Steam>/config/steamvr.vrsettings`:

```json
"driver_cyberfinger": {
    "enable": true,
    "handtracking_udp_port": 27015
},

"TrackingOverrides" : {
  "/devices/cyberfinger/CYBERFINGER_L" : "/user/hand/left",
  "/devices/cyberfinger/CYBERFINGER_R" : "/user/hand/right"
},
```

### 3. Run the cyberfinger bridge

Under the bridge directory:

```bash
python cyberfinger_bridge.py
```
add --debug for more info. 


Launch these *after* SteamVR is running but *before* your VR application.

Make sure your cyberfinger is in "VR mode" where each hand communicates directly over BLE with the cyberfinger_bridge using a custom protocol, 
not the legacy "Gamepad" mode (which has the two cyberfingers merged into one XInput device).
Note VR mode is available only for the steamvr branch of the firmware:
https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr
This version must be installed on your CyberFingers, or the steamvr driver will not work.  

## Configuration

All settings are in `default.vrsettings` or the global SteamVR settings file:

| Setting                    | Default          | Description                                  |
|----------------------------|------------------|----------------------------------------------|
| `enable`                   | `true`           | Enable/disable the driver                    |
| `serialNumber_left`        | `MERGED_CTRL_L`  | Serial number for left controller            |
| `serialNumber_right`       | `MERGED_CTRL_R`  | Serial number for right controller           |
| `handtracking_udp_port`    | `27015`          | UDP port for hand tracking data              |

# Wire Protocol (UDP)

Both packet types are sent to `127.0.0.1:<port>` (default 27015) and distinguished by their magic bytes.

## Hand Tracking Packet (bridge → driver)

(This bridge is not currently needed)
Source: C++ hand tracking bridge (reads from OpenVR/Ultraleap).
See `HandTrackingReceiver.h :: HandTrackingPacket`.

```
Offset  Size    Field
0       4       Magic: 0x4B535448 ('HTSK')
4       1       Version: 1
5       1       Hand: 0=left, 1=right
6       1       Confidence: 0-255
7       1       Reserved
8       12      Position: float[3] (xyz meters, tracking space)
20      16      Orientation: float[4] (wxyz quaternion)
36      868     Bones: float[31][7] (per bone: xyz pos + wxyz quat)
904     20      Curls: float[5] (thumb, index, middle, ring, pinky; 0-1)
─────────────────
Total: 924 bytes
```

## Gamepad Packet (BLE bridge → driver)

Source: Python BLE bridge (`cyberfinger_bridge.py`), forwarding VR GATT notifications from ESP32 CyberFinger devices.
See `HandTrackingReceiver.h :: GamepadPacket`.

```
Offset  Size    Field
0       4       Magic: 0x50474643 ('CFGP')
4       1       Hand: 0=left, 1=right
5       1       Buttons (bitmask):
                  bit0 = Trigger (digital)
                  bit1 = Grip
                  bit2 = B
                  bit3 = Joy click
                  bit4 = A
6       2       Joystick X: int16 (-32767..32767)
8       2       Joystick Y: int16 (-32767..32767)
10      1       Trigger analog: uint8 (0-255)
11      1       Battery percent: uint8 (0-100)
─────────────────
Total: 12 bytes
```

## SteamVR Input Mapping

| Gamepad | SteamVR Component | Notes |
|---------|-------------------|-------|
| Trigger (bit0) | `/input/trigger/value` | Analog from `trigger_analog`, digital fallback |
| Grip (bit1) | `/input/grip/value` | Digital (0 or 1) |
| B (bit2) | `/input/b/click` (R) `/input/y/click` (L) | Secondary button |
| Joy click (bit3) | `/input/joystick/click` | |
| A (bit4) | `/input/a/click` (R) `/input/x/click` (L) | Primary button |
| Joystick X | `/input/joystick/x` | Normalized to -1..1 |
| Joystick Y | `/input/joystick/y` | Normalized to -1..1, **inverted** |


## Troubleshooting

- **Controllers show up but no position**: The hand tracking bridge isn't
  running or isn't receiving data. Check that Steam Link hand tracking is
  enabled on your Quest, or that your Ultraleap is connected.

- **Buttons don't register**: Check cyberfinger_bridge.py is running and receiving events. Check cyberfingers are in "VR mode".

- **Skeleton not animating**: Verify the bridge is sending data (check
  `vrserver.txt` log for "HandTrackingReceiver" messages). The bridge must be
  started after SteamVR.

## Architecture

The project has three main components:

1. **SteamVR Driver** (`driver_cyberfinger.dll/.so`): Loaded by SteamVR,
   creates two virtual controller devices. Listens on a UDP port for hand tracking
   and cyberfinger button and joy events, and updates SteamVR with
   merged input each frame.

2. **Hand Tracking Bridge** (`handtracking_bridge`): Standalone process that
   reads hand tracking data from SteamVR's input API (which receives it from
   Quest via Steam Link, or from Ultraleap's SteamVR plugin) and forwards it
   over UDP localhost to the driver. (not currently needed)
   
3. **CyberFinger Bridge** (`cyberfinger_bridge.py`): Standalone process that 
   reads joystick and button events sent from the cyberfinger firmware 
   operating in "VR mode". 

The bridge exists as a separate process because SteamVR drivers cannot use the
client-side VR input API to read hand tracking from *other* drivers — they can
only provide input, not consume it.

## License

The CyberFinger SteamVR driver is licensed under the GNU General Public License v3.0 (GPL-3.0-only).
Unless otherwise noted in individual source file headers, all source code in this repository is licensed under GPL-3.0-only. 
Any redistribution of this software—whether in source or binary form, including distribution in physical devices—must comply with the terms of GPL-3.0, 
including the obligation to provide corresponding source code and installation information for modified versions.

The full license text is provided in the LICENSE file. 

## Contributing

By contributing to this project, you agree to the Contributor License Agreement in CLA.md.

