# CyberFinger Bridge — Linux

GUI application that connects CyberFinger BLE controllers to your Linux PC
in two modes:

- **VR Mode** — forwards BLE input via UDP to the CyberFinger SteamVR driver
- **Gamepad Mode** — combines left + right controllers into a virtual Xbox 360 gamepad via uinput

## Quick Start

```bash
pip install -r requirements.txt
python cyberfinger_gui_linux.py
```

## Dependencies

| Package | Purpose | Install |
|---------|---------|---------|
| bleak | BLE communication | `pip install bleak` |
| evdev | Virtual gamepad (uinput) | `pip install evdev` |
| pystray | System tray icon | `pip install pystray` |
| pillow | Tray icon images | `pip install pillow` |
| tkinter | GUI | Usually pre-installed; see below |

### tkinter

tkinter is part of the Python standard library but sometimes needs to be
installed separately:

```bash
# Ubuntu / Debian
sudo apt install python3-tk

# Fedora
sudo dnf install python3-tkinter

# Arch
sudo pacman -S tk
```

## uinput Setup (for Gamepad Mode)

The virtual gamepad requires access to `/dev/uinput`. You can either run as root
or set up permissions:

```bash
# Load the kernel module
sudo modprobe uinput

# Make it load on boot
echo "uinput" | sudo tee /etc/modules-load.d/uinput.conf

# Create uinput group and add your user
sudo groupadd -f uinput
sudo usermod -aG uinput "$USER"

# Create udev rule for permissions
echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | \
    sudo tee /etc/udev/rules.d/99-uinput.rules

# Reload udev rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# Log out and back in for group membership to take effect
```

## Bluetooth Setup

Make sure your Linux system has BlueZ installed and the Bluetooth service
is running:

```bash
# Check Bluetooth service
sudo systemctl status bluetooth

# Start if needed
sudo systemctl enable --now bluetooth

# Pair CyberFinger devices (if not already paired)
bluetoothctl
# Then: scan on, pair XX:XX:XX:XX:XX:XX, trust XX:XX:XX:XX:XX:XX
```

The bridge uses bleak which talks to BlueZ via D-Bus. No special
Bluetooth permissions needed beyond normal user access.

## Differences from Windows Version

| Feature | Windows | Linux |
|---------|---------|-------|
| BLE library | WinRT | bleak (BlueZ) |
| Device discovery | System device enumeration | BLE scan (5s) |
| Virtual gamepad | ViGEmBus + vgamepad | uinput + evdev |
| Gamepad identity | Xbox 360 (ViGEm) | Xbox 360 (uinput VID/PID) |
| Config location | %APPDATA%\CyberFingerBridge | ~/.config/cyberfinger-bridge |

## Gamepad Button Mapping

Same mapping as Windows version:

| Input          | Right Hand  | Left Hand  |
|----------------|-------------|------------|
| 0x01 Trigger   | BTN_A       | BTN_X      |
| 0x02 Grip      | BTN_B       | BTN_Y      |
| 0x04 B button  | BTN_TR (RB) | BTN_TL (LB)|
| 0x08 Joy click | BTN_THUMBR  | BTN_THUMBL |
| 0x10 A button  | BTN_START   | BTN_SELECT |
| Joystick       | ABS_RX/RY   | ABS_X/Y    |

Y axis is inverted on both sticks.

## Files

```
cyberfinger_gui_linux.py    Main GUI application
requirements.txt            Python dependencies
assets/
  icon_32x32.png           Tray icon (running)
  icon_32x32_bw.png        Tray icon (idle)
```
