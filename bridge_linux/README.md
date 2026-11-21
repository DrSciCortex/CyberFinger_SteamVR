# CyberFinger Bridge — Linux

GUI application that connects CyberFinger BLE controllers to your Linux PC
in three modes:

- **VR Mode** — forwards the CyberFingers to the CyberFinger driver for WiVRn
  ([`wivrn/`](wivrn/README.md)): their buttons, stick, trigger and IMUs on the hands the
  headset tracks, and applications' haptics back to their motors
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

Run it from this checkout: it shares its protocol, report decoding, haptics and pink-button
code with the Windows bridge in [`../bridge`](../bridge). Started without the environment
(no `bleak`), it opens but cannot start: Start then says so in the status line and the
Console tab.

### 6. The WiVRn driver (VR Mode)

VR Mode needs WiVRn's server built with the CyberFinger driver: see
[`wivrn/README.md`](wivrn/README.md) (`makepkg -si` there on Arch). The gamepad modes need
nothing but the bridge.

## VR Mode

Start WiVRn, connect the headset with hand tracking on, start the bridge in VR Mode and put
the controllers down. The driver takes each hand a CyberFinger reports for; stop the bridge
and the hands are WiVRn's own again. What each button does in VR is in
[`wivrn/README.md`](wivrn/README.md#controls); the driver's settings are in the WiVRn
dashboard's *Settings*, under **CyberFinger**.

- **Buttons + IMU tab** — both CyberFingers: buttons, stick, trigger, battery, each IMU's
  orientation (body 1, body 2, joint, as the firmware reports them), and a haptics strip
  that lights when an application vibrates the hand. Click the strip for a test pulse.
- **Skeleton tab** — the hands as the driver sees them (the headset's hand tracking), and
  the driver's mode per hand. Empty until the bridge runs in VR Mode with WiVRn up.
- **Stream mode** (the button under Resync IMU) — for streaming or recording: the options
  and tabs make way for one view of both CyberFingers that fills the window, with the
  skeletons, stick, trigger and buttons, the IMUs and the haptics, to capture beside the
  game. **Stop stream**, in the top line, brings the options back.
- **Right pink button** — left to the *driver* (FluxAction 42 in Resonite by default), or
  *mic mute* (the default microphone, through `wpctl` or `pactl`, confirmed on the motor:
  a pulse train muted, one buzz live), or a *FluxAction* of your choice sent straight to the
  MoreFluxActions mod. The left pink button always goes to the driver (the controller's menu
  button).
- **Resync IMU** — starts the driver's IMU fusion over for both hands; a triple tap on a
  CyberFinger's joint IMU does it for that hand (`tap_threshold_g` in the settings tunes how
  hard a tap must be).
- **Forward IMU to SlimeVR** — the CyberFingers as SlimeVR trackers (lower arm from the body
  IMU, hand from the joint IMU), to a SlimeVR server on UDP 6969. Works in every mode.
- **Haptics** — applications' vibrations reach the CyberFinger's motor (firmware 1.3.3+).

The bridge sends the driver one `CFG2` packet per BLE report (and `CFIM` with the IMUs) on
UDP 127.0.0.1:27015, and listens for its haptic requests and status on 27016
([protocol](../README.md#wire-protocol-v2)).

Settings are kept in `~/.config/cyberfinger-bridge/settings.json`: `right_pink`
(`driver` / `mic` / `flux`), `right_pink_flux`, `slimevr_enabled`, `slimevr_body_imu`,
`slimevr_host`, `slimevr_port`, `tap_threshold_g`, `skeleton_enabled`, `autostart`, `tab`.

## Resonite mods

The Resonite mods the Windows installer bundles
([what each does](../README.md#application-support)) are built from their GitHub
repositories and installed with:

```bash
./build_resonite_mods.sh --install     # needs git, dotnet-sdk and Resonite; close Resonite first
```

It builds them against the installed Resonite, then does what the installer does on Windows:

- **Links Resonite to its Gale profile** (`resonite_gale_link.py`; *Default*, or
  `--profile NAME`), so that Resonite loads the profile's mods however it starts, from Steam
  or WiVRn's launcher as well as Gale's button. ResoniteModLoader then loads through the
  profile's ResoniteModLoaderLoader: enable it in Gale, and remove
  `-LoadAssembly Libraries/ResoniteModLoader.dll` from Steam's launch options, or RML loads
  twice and Resonite stops as it starts (the script warns).
- **Copies the mods in**: MoreFluxActions and SteamVRRoleFix into the profile,
  CyberFingerMod and ProximityGrab into Resonite's `rml_mods`.

The profile needs, from Gale: BepisLoader, BepInExRenderer, RenderiteHook, InterprocessLib,
BepisResoniteWrapper, BepInExResoniteShim and ResoniteModLoaderLoader.

In Steam, Resonite's *Properties → Launch Options* should then read (plus any options of your own):

```
WINEDLLOVERRIDES="winhttp=n,b" %command%
```

- `WINEDLLOVERRIDES="winhttp=n,b"` is needed: the renderer is a Windows program run by
  Proton, and its mod loader is a `winhttp.dll` beside it that Wine loads only when told to.
  Without it the renderer-side mods (SteamVRRoleFix, MoreFluxActions' renderer part) don't
  load.
- No `-LoadAssembly Libraries/ResoniteModLoader.dll` any more, as above.

Started from WiVRn's application launcher in the headset, Resonite gets the same options:
WiVRn starts Steam games through Steam. (Its list has Steam's VR games, Steam shortcuts
marked as VR, and desktop entries whose `Categories` include `X-WiVRn-VR`; a build started
by a script of your own needs the variable set in that script.)

Without `--install` it only builds, into `../out/resonite_mods/` in the installer's layout.
`resonite_gale_link.py` also works by itself, for Steam's Resonite or another build
(`--game FOLDER --profile NAME`); `--status` shows a link, `--unlink` undoes it: every
change is recorded in the build's folder, and what was there before is kept as
`*.pre-gale-link`.

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
| VR driver | SteamVR driver (`../src`) | WiVRn driver (`wivrn/`) |
| Microphone mute | Core Audio | `wpctl` (PipeWire) or `pactl` |
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
driver_skeleton.py          The Skeleton tab's hands, from the driver's status stream
requirements.txt            Python dependencies
wivrn/                      The CyberFinger driver for WiVRn (VR Mode)
build_resonite_mods.sh      Builds the Resonite mods from their repositories, and installs them
resonite_gale_link.py       Links a Resonite build to its Gale profile (mods load however it starts)
assets/
  icon.png                 Full-size icon
  icon_32x32.png           Tray icon (running)
  icon_32x32_bw.png        Tray icon (idle)
```
