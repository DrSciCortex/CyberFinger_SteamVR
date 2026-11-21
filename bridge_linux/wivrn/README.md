# CyberFinger driver for WiVRn

The Linux counterpart of the SteamVR driver in [`src/`](../../src): the CyberFinger's buttons, stick, trigger,
joint IMU and motor on the hands a standalone headset tracks, for [WiVRn](https://github.com/WiVRn/WiVRn) (and so
for OpenXR applications, and OpenVR ones through xrizer or OpenComposite).

> **Status:** new. It runs in WiVRn 26.9 with a Quest, tried in Resonite: the driver takes the hands and gives
> them back, the IMU fusion and haptics work. Not yet tried: the FluxActions, other applications, and the settings
> section in the dashboard itself (it is tested outside it).

## How it works

WiVRn is an OpenXR runtime, so there is no SteamVR to load a driver into: the driver is compiled into WiVRn's
server. WiVRn has a controller device per hand, which also carries the headset's hand joints. While a CyberFinger
sends its reports and the headset sees the bare hand, the driver keeps that hand on the controller device instead
of WiVRn's hand-interaction device, and applications see an ordinary controller:

```
Quest ──WiVRn──► wivrn-server
                   controller device, per hand
                     poses + hand joints   the headset's hand tracking, turned by the joint IMU (ImuFusion)
                     inputs                the CyberFinger's                ◄── CFG2, CFIM  UDP 27015 ◄── bridge ◄─BLE─ CyberFinger
                     haptics               to the CyberFinger's motor       ──► CFHP, CFOP  UDP 27016 ──► bridge ──BLE─►
                   C, D, E, the long press, gestures ──► FluxActions        UDP 42042 ──► Resonite (MoreFluxActions)
```

| | |
|---|---|
| Hand taken | A CyberFinger reporting (the bridge started in VR mode) and the headset reporting hand tracking, or nothing, for that hand. Pick up a controller and the hand is the controller's; put it down and the CyberFinger has it back. Two seconds without reports (`release_after_ms`) and the hand goes back to WiVRn's own hand tracking: with the bridge stopped, WiVRn behaves as without the driver |
| What applications see | An Oculus Touch controller (`profile`) in `/user/hand/left` or `right`, at the hand, with `XR_EXT_hand_tracking` joints as before. xrizer shows it to OpenVR games as `oculus_touch` with the hand skeleton |
| Poses | The headset's grip, aim, palm, pinch and poke poses for the hand. Once the IMU fusion is calibrated the whole hand, joints included, turns with the CyberFinger's joint IMU about the grip; the position stays the headset's. A hand the headset loses keeps its place for `occlusion_hold_ms`, still turning with the IMU |
| Haptics | An application's vibration goes to the CyberFinger's motor through the bridge (firmware 1.3.3+) |

The protocol is the SteamVR driver's ([`src/Protocol.h`](../../src/Protocol.h)), and the IMU fusion, the tap to
hold, the long press and the gestures are its code, compiled here as they are.

## Controls

Per hand, as the emulated Touch controller:

| CyberFinger | Controller | |
|---|---|---|
| Trigger | trigger (value, click) | |
| Grip | squeeze | tap to hold: a press under 200 ms holds until the next press (`grab_tap_to_hold`) |
| Stick, stick press | thumbstick, thumbstick click | |
| Black button (Start/Select) | A (right) / X (left) | reported on release; held 0.8 s it is FluxAction 1 / 2 instead (`black_hold_ms`) |
| MENU | B (right) / Y (left) | |
| Pink button, left | menu button | |
| Pink button, right | FluxAction 42 | when the bridge leaves it to the driver; otherwise the bridge mutes the microphone or fires a FluxAction itself |
| C, D, E | FluxAction 3 / 5 / 7 (left), 4 / 6 / 8 (right) | |
| Two-finger point, pinky pinch, index point | FluxAction 36 / 38 / 40 (left), 37 / 39 / 41 (right) | from the hand joints, debounced |

The controller's touch sensors (trigger, thumbstick, buttons, thumb rest) are never reported, as with the SteamVR
driver. A touch tells an application that a controller is in the hand:
[ProximityGrab](https://github.com/shawn-scicortex/ProximityGrab) turns its hand gestures off when it sees one.
ProximityGrab 0.11.0 as released also takes the grip for one, which here is the controller's analog squeeze (an
OpenXR Touch controller has no grip click): with it, a press of the CyberFinger's grip turns the gestures off until
the hand has been out of the headset's view. Builds from its current source count only the touch sensors.

An OpenXR controller has no place for the extra buttons, which SteamVR's bindings gave to Resonite as FluxActions.
Here the driver sends those FluxActions itself, as UDP datagrams to the
[MoreFluxActions](https://github.com/DrSciCortex/MoreFluxActionsMod) mod (its engine half; the numbers are the
SteamVR Resonite binding's). Without Resonite and the mod nothing listens, and they do nothing. A button given to
the controller in the settings (`button_a`, `button_b`, …) no longer fires its FluxAction.

## Installing

The driver's version of `wivrn-server` replaces the stock one, and with it a `wivrn-dashboard` that has the
driver's settings. It must be the same WiVRn version as the app on the headset; the patch is made for **26.9**.

**Arch** (replaces the AUR's `wivrn-server` and `wivrn-dashboard` packages; `lib32-wivrn-server` stays):

```bash
cd bridge_linux/wivrn
makepkg -si
```

**Anywhere, or to try it without installing:** `./build.sh` downloads WiVRn's sources, patches and builds the
server under `~/.cache/cyberfinger/`, and prints the path of the server to run in place of the installed one (stop
that first). Games keep using the installed WiVRn's client libraries, which is fine at the same version. The
dashboard isn't built this way: the settings are then the file's (below).

Both compile the driver from this checkout: after a `git pull`, build again. When WiVRn itself is updated (on Arch,
an AUR update puts the stock server back), set the version in `PKGBUILD` / `WIVRN_VERSION` and rebuild;
[`wivrn-cyberfinger.patch`](wivrn-cyberfinger.patch) is small (hooks in WiVRn's controller device, session and
settings page) but may need refreshing against newer sources.

## Running

1. Start WiVRn and connect the headset, with hand tracking on.
2. Start the bridge ([`../README.md`](../README.md)) in **VR Mode** and put the controllers down.

The hands are tracked for the driver from then on, with or without an application running: the bridge's Skeleton
tab shows them, and the IMU fusion calibrates.

For Resonite, [`../build_resonite_mods.sh --install`](../README.md#resonite-mods) builds and installs the mods
the Windows installer bundles (MoreFluxActions, which receives the driver's FluxActions, among them), and links
Resonite to its Gale profile so that they load when Resonite is started from Steam or from WiVRn's launcher.

The server's log says what the driver does, each line with `CyberFinger:` in it — the first packets from the
bridge, `[left] CyberFinger takes the hand`, the IMU calibration, and a status line per hand every 10 s. Started
from the dashboard, the log is the newest `~/.local/state/wivrn/wivrn-dashboard/server_logs_*.txt`; otherwise the
terminal `wivrn-server` runs in. The bridge's Skeleton tab draws the hands the driver sees, with
its mode per hand.

## Settings

In the WiVRn dashboard, *Settings* has a **CyberFinger** section below WiVRn's own: what SteamVR's settings show
for the SteamVR driver, where WiVRn has the same, and what is WiVRn's (the emulated controller, the FluxActions).
Changes apply as they are made, in the headset too, which is how to tune the controller's offset against the
hand; *Cancel* puts everything back, *Reset* returns to the driver's defaults.

Of SteamVR's list, not here because WiVRn does them itself or has no use for them: the Steam Link skeleton hook and
pose filter, the Quest palm pinch, hiding other controllers, the handover to the Touch controllers (always on: the
headset says what is in the hand), the skeletal tracking level, and debug captures.

The section edits `~/.config/cyberfinger/wivrn.json`, a JSON object in which every key is optional
([`wivrn.example.json`](wivrn.example.json) has them all at their defaults). The driver reads the file again
whenever it changes, so it can be edited by hand as well; everything but the ports applies while WiVRn runs. Keep
it plain JSON: the driver accepts comments, the dashboard then leaves the file alone and says so. Key names are the
SteamVR driver's where the setting is the same.

| Key | Default | |
|---|---|---|
| `active` | `true` | `false`: WiVRn behaves as without CyberFinger |
| `profile` | `"touch"` | the controller applications see: `touch`, `touch_plus`, `touch_pro`, `index`, `vive_focus3`, `pico4`, `pico_neo3`, `simple` |
| `button_a`, `button_b` | `"STSEL"`, `"MENU"` | CyberFinger buttons for the lower and upper face button. Names: `TRIG GRIP C D E MENU JCLK STSEL PINK NONE`, several joined with `\|` |
| `button_menu`, `button_system` | `"PINK"`, `"NONE"` | the left controller's menu button, the right one's system button |
| `black_hold_ms` | 800 | the black button's long press; 0: none, and A reports as pressed |
| `grab_tap_to_hold`, `grab_tap_ms` | `true`, 200 | |
| `release_after_ms` | 2000 | without CyberFinger reports, the hand goes back to WiVRn |
| `imu_fusion` | `true` | the hand's orientation from the joint IMU |
| `pose_rotation_prediction` | 0.0 | how much of the IMU's angular velocity carries the orientation ahead to the time applications ask for (0..1) |
| `occlusion_hold_ms` | 2000 | a lost hand keeps its place this long (with the IMU fusion); 0: lost at once |
| `grip_offset_m`, `grip_angle_deg` | `[0, 0, 0]` | moves the controller (grip, aim and palm poses) against the hand, in the grip's frame; angles as the SteamVR driver's `grip_angle_*`. For applications that place the hand or a tool by the controller |
| `flux_port` | 42042 | 0: no FluxActions |
| `flux_actions` | see above | `a_hold`, `c`, `d`, `e`, `two_finger_point`, `pinky_pinch`, `index_point`: the left hand's number (the right hand's is the next), `pink_right`; 0 turns one off |
| `handtracking_udp_port`, `context_udp_port`, `bind_loopback_only`, `legacy_5bit_buttons` | 27015, 27016, `true`, `false` | as the SteamVR driver's; read when WiVRn starts |
| `status_log` | `true` | the 10 s status lines |
| `gesture_log` | `false` | `true`: the log says why a hand shape is not quite a two-finger point, a line per hand every second at most, to tune the gesture |

## IMU fusion

[`src/ImuFusion.h`](../../src/ImuFusion.h), unchanged: the joint IMU's heading and mounting are solved against the
headset's hand orientation while the hand is seen, then the orientation comes from the IMU with a slow optical
correction. What differs on WiVRn:

- The headset sends each pose predicted for the times applications ask about, several per frame. The fusion takes
  the nearest prediction only, and each one when its time has come, so the lag it measures is the hand tracking's
  own.
- The solved mounting is saved per hand in `~/.local/state/cyberfinger/imu_calibration.txt` and the fusion starts
  from it. The first session has nothing to start from (the SteamVR driver's reference mounting is in Steam Link's
  hand frame) and waits for a full solve: move the hand through different orientations in view for some tens of
  seconds. The log says `IMU calibrated`.
- Resync (the bridge's button, or a triple tap on the joint IMU) works as on SteamVR.

If the hands point wrong, set `imu_fusion` to `false` (it applies at once) and please keep the log.

## Not here (yet)

- A fused hand (`CFHS`, the FUSED mode) is received but not used: its bones are SteamVR's.
- No pose filter: that one was made for Steam Link's noise.
- No device of its own: the hand is WiVRn's controller device, so there is no CyberFinger render model, icon or
  battery level in the runtime (the bridge shows the battery).
- Per-application bindings: applications bind the emulated controller as they would the real one.

## Tests and the simulator

Neither needs WiVRn or a headset:

```bash
cmake -B build bridge_linux/wivrn && cmake --build build
ctest --test-dir build --output-on-failure      # settings, inputs, gestures, poses and fusion, the UDP link,
                                                # and the dashboard's settings section (needs Qt 6 and KDE Frameworks)
build/wivrn_driver_sim --haptics                # the driver on its ports, with two made-up hands
```

With the simulator running (and WiVRn stopped: same ports), start the bridge in VR mode: the hands appear in its
Skeleton tab, the simulator prints what the driver makes of the buttons, and `--haptics` buzzes each held hand
every two seconds.

## Files

```
driver/CfHand.*       one hand: inputs, gestures, poses, the observations into the IMU fusion
driver/CfDriver.*     both hands, the UDP link (src/StudioLink), settings, saved calibration, status
driver/CfConfig.*     the settings file
driver/CfWivrn.*      WiVRn's types to the driver's and back; what the patch calls
dashboard/            the settings section for WiVRn's dashboard, and its access to the settings file
wivrn-cyberfinger.patch   the hooks in WiVRn (controller device, session, settings page, CMake)
PKGBUILD, build.sh    building wivrn-server and wivrn-dashboard with the driver
tests/                the tests, the simulator
```
