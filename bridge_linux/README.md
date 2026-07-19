# CyberFinger Bridge — Linux

GUI application that connects CyberFinger BLE controllers to your Linux PC
in three modes:

- **VR Mode** — forwards BLE input via UDP to the CyberFinger SteamVR driver
- **Gamepad (Resonite)** — virtual Xbox 360 gamepad via uinput, mapped for Resonite
- **Gamepad (VRChat)** — virtual Xbox 360 gamepad via uinput with VRChat-optimal
  mapping plus OSC (UseLeft, Grab, Voice)

## Install (Arch Linux)

### 1. System packages

```bash
sudo pacman -S uv tk
sudo pacman -S gtk3 gobject-introspection-runtime
sudo pacman -S libayatana-appindicator
```

`gtk3` and `libayatana-appindicator` enable the AppIndicator system tray backend,
which supports a proper right-click context menu. Without them pystray falls back
to the bare X11 backend which has no menu support.

### 2. Bluetooth

Follow the [Arch Linux Bluetooth wiki](https://wiki.archlinux.org/title/Bluetooth)
for initial setup, then:

```bash
sudo systemctl enable --now bluetooth
```

Pair the CyberFinger controllers once:

```bash
bluetoothctl
# scan on
# pair XX:XX:XX:XX:XX:XX
# trust XX:XX:XX:XX:XX:XX
# connect XX:XX:XX:XX:XX:XX
```

### 3. uinput (Gamepad Mode)

```bash
# Load the kernel module
sudo modprobe uinput

# Make it load on boot
echo "uinput" | sudo tee /etc/modules-load.d/uinput.conf

# Create uinput group and add your user
sudo groupadd -f uinput
sudo usermod -aG uinput "$USER"

# Create udev rule
echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | \
    sudo tee /etc/udev/rules.d/99-uinput.rules

sudo udevadm control --reload-rules
sudo udevadm trigger

# Log out and back in for group membership to take effect
```

### 4. Python environment

```bash
cd bridge_linux
uv venv
uv pip install -r requirements.txt
```

### 5. Run

```bash
source .venv/bin/activate
python cyberfinger_gui_linux.py &
```

## Python Dependencies

| Package | Purpose |
|---------|---------|
| bleak | BLE communication via BlueZ D-Bus |
| evdev | Virtual gamepad via uinput |
| pystray | System tray icon |
| pillow | Tray icon images |
| python-osc | VRChat OSC (optional) |

tkinter is provided by the system `tk` package (installed above).

## Platform Notes

| Feature | Windows | Linux |
|---------|---------|-------|
| BLE library | WinRT | bleak (BlueZ) |
| Device discovery | System enumeration | Paired devices via bluetoothctl |
| Virtual gamepad | ViGEmBus + vgamepad | uinput + evdev |
| Config location | %APPDATA%\CyberFingerBridge | ~/.config/cyberfinger-bridge |

## Gamepad (Resonite) Mapping

Standard Xbox 360 layout — all CyberFinger inputs go to gamepad buttons/axes.

| CyberFinger   | Right Hand       | Left Hand        |
|---------------|------------------|------------------|
| Joystick      | ABS_RX / ABS_RY  | ABS_X / ABS_Y    |
| Trigger       | ABS_RZ (analog)  | ABS_Z (analog)   |
| Trigger btn   | BTN_A            | BTN_X            |
| Grip          | BTN_B            | BTN_Y            |
| Menu          | BTN_TR (RB)      | BTN_TL (LB)      |
| Joy click     | BTN_THUMBR (R3)  | BTN_THUMBL (L3)  |
| ST/SE         | BTN_START        | BTN_SELECT       |
| C             | DPAD_UP          | DPAD_RIGHT       |
| D             | DPAD_DOWN        | BTN_MODE (guide) |
| E             | DPAD_LEFT        | —                |

## Gamepad (VRChat) Mapping

VRChat-optimal mapping. Some inputs route to the gamepad, others go through
OSC on UDP `127.0.0.1:9000`, and right-C triggers an F12 screenshot via
synthetic keyboard input.

Enable OSC in VRChat: **Action Menu → Options → OSC → Enabled**.
See the [VRChat OSC-as-input docs](https://docs.vrchat.com/docs/osc-as-input-controller).

### Gamepad side

| CyberFinger       | Right Hand          | Left Hand                  |
|-------------------|---------------------|----------------------------|
| Joystick          | ABS_RX / ABS_RY (turn + look) | ABS_X / ABS_Y (move) |
| Trigger (analog)  | ABS_RZ (Use/Interact) | — (routed to OSC only)   |
| Trigger btn       | (folded into ABS_RZ) | (routed to OSC only)      |
| Grip              | (OSC only)          | (OSC only)                 |
| Menu              | BTN_THUMBR (Action Menu R) | BTN_START (Quick Menu) |
| Joy click         | BTN_A (Jump — either hand)  | BTN_A (Jump — either hand) |
| C                 | (F12 screenshot)    | BTN_X (Mute)               |
| D                 | DPAD_RIGHT          | DPAD_LEFT                  |
| E                 | DPAD_UP             | DPAD_DOWN                  |
| ST/SE             | (OSC only — chatbox) | (OSC only — voice mute)   |

### OSC side (UDP 9000, path `/input/*` and `/chatbox/*`)

| CyberFinger input | OSC address          | Value / behaviour                          |
|-------------------|----------------------|--------------------------------------------|
| Left Trigger      | `/input/UseLeft`     | `1` on press, `0` on release               |
| Right Grip        | `/input/GrabRight`   | tap (<200 ms) toggles; hold releases on lift |
| Left Grip         | `/input/GrabLeft`    | tap (<200 ms) toggles; hold releases on lift |
| Right ST/SE       | `/chatbox/input`     | `["", false, false]` — opens chatbox (rising edge) |
| Left ST/SE        | `/input/Voice`       | `1` on press, `0` on release (mute toggle) |
| Right C           | (keyboard)           | F12 screenshot on rising edge (pynput)     |

## Files

```
cyberfinger_gui_linux.py    Main application
requirements.txt            Python dependencies
assets/
  icon.png                 Full-size icon
  icon_32x32.png           Tray icon (running)
  icon_32x32_bw.png        Tray icon (idle)
```
