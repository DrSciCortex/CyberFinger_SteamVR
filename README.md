# CyberFinger — SteamVR Driver

A SteamVR driver that turns **two CyberFingers** into a pair of SteamVR hand controllers: the CyberFinger's
push-stick, trigger, grip and buttons, plus a full hand skeleton and pose from the headset's hand tracking
(Quest via Steam Link).

Note: this is a *work-in-progress*, and currently alpha software.

**Documentation:** [docs/README.md](docs/README.md) maps the pieces (CyberFingers, bridge, driver, the MoreFluxActions
mod for Resonite) and where each is documented. The user manual is in [docs/manual](docs/manual/).

## How it works

- The driver adds two controllers, `CYBERFINGER_L` and `CYBERFINGER_R`, which take the left/right hand roles
  (Steam Link's hand devices declare hand priority −1, CyberFinger 1000). Apps see CyberFinger, not a second
  pair of Steam Link hands.
- **Pose and fingers**, per hand, from the first source available:
  - **FUSED** — an externally fused hand (`CFHS`), sent over UDP; no bridge here sends it
    (`cf_driver_probe.py --fake-hand` exercises it);
  - **PASSTHROUGH** — the headset's own hand tracking as streamed by Steam Link or Virtual Desktop,
    captured inside vrserver by observe-only hooks on the streamer's pose and skeleton updates. Each update
    is republished the moment the streamer submits it, with its own timing, so apps get the same stream as
    from the streamer's hands;
  - **NO_POSE** — nothing tracks the hand: the pose is reported invalid, buttons keep working.
- **The pink wrist buttons** (firmware 1.3.3+), `/input/pink` on each hand: the left one opens the SteamVR dashboard
  (through the dashboard's binding, so it can be rebound). The right one is the bridge's "Right pink button"
  option: *SteamVR* (the default) passes the button to SteamVR for the app's binding (VRChat's own mute;
  FluxAction42 in Resonite, which the MoreFluxActions mod makes Resonite's mute: its `MuteToggleAction`, 42 by default);
  *mic mute* mutes and unmutes the Windows microphone for every app, confirmed on the CyberFinger's motor;
  *FluxAction* fires one (1–42, default 42) straight to the MoreFluxActions mod over loopback UDP
  (`bridge/flux_actions.py`). In the last two the bridge keeps the button to itself, so nothing acts twice.
- **Buttons only.** Actions come from the CyberFinger's buttons. The standard hand-tracking gestures (pinches,
  grasp, index point) are exposed for binding but unassigned by default. The Quest left-palm pinch doesn't open
  the dashboard: while CyberFinger is active the driver holds the headset hands' system button back from SteamVR
  (`forward_tap_system_button` passes it on as CyberFinger's own instead). It fired too easily.
- **Haptics** requested by apps reach the CyberFinger's motor through the bridge (firmware 1.3.3+, CFV1BP boards: the
  hardware revisions with a motor). Without one, the requests are simply ignored.
- **Apps:** VRChat and Resonite get native bindings; every other app sees an Index controller (SteamVR
  automatic rebinding + legacy binding emulation).

## Building

### Prerequisites

- Visual Studio 2022 (or newer) with the **Desktop development with C++** workload — MSVC and a Windows SDK
- CMake 3.16+ (bundled with Visual Studio, or `pip install cmake ninja`)
- The submodules: [OpenVR SDK](https://github.com/ValveSoftware/openvr) (`openvr/`) and
  [MinHook](https://github.com/TsudaKageyu/minhook) (`third_party/minhook/`)
- Python with WinRT for the bridge (`pip install -r bridge/requirements.txt`, winrt v3.x)
- The "steamvr" branch of the CyberFingerFW_ESP32 firmware on the CyberFingers:
  https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr

### Windows

Double-click `tools\build_driver.cmd` (it finds Visual Studio, fetches missing submodules, builds Release
and runs the tests), or from a *Developer PowerShell for VS*:

```bash
git submodule update --init
cmake -S . -B out/build/x64-Release -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build out/build/x64-Release
ctest --test-dir out/build/x64-Release
```

The driver is assembled in `out/build/x64-Release/driver/cyberfinger/`. It links the C runtime statically,
so it needs no Visual C++ redistributable.

### The installer

`tools\build_installer.cmd` builds the driver, the bridge app (`bridge\build.bat`, PyInstaller) and the
Resonite mods, and packages them with Inno Setup 6.3+ into
`bridge\dist\installer\CyberFingerBridge_Setup_<version>.exe`. Run it where `python` is the bridge's Python with
PyInstaller installed (e.g. `conda activate cybrgui`). Inno Setup is found in its default folders, or set
`INNO_ISCC` to `ISCC.exe`. The ViGEmBus installer is bundled when
`bridge\installer\ViGEmBus_1.22.0_x64_x86_arm64.exe` is present.

The Resonite mods are built by `tools\stage_resonite_mods.py` into `bridge\installer\resonite_mods\`, each from a
fresh shallow clone of its GitHub repository (default branch), made anew on every run under
`out\resonite_mods_src\`: the installer gets what is pushed, not what's in local checkouts. `VERSIONS.txt` there
records the commits. It also fetches the latest BepInExRenderer and RenderiteHook from Thunderstore into
`resonite_mods\standalone\`: BepInEx for the renderer and its loader, with which setup installs SteamVRRoleFix
in builds without Gale (LGPL-2.1, with `THIRD-PARTY-NOTICES.txt`). The mods compile against Resonite's
assemblies, so this needs git, the .NET SDK, Resonite and internet access. On a PC without Resonite, stage them
on one that has it (`python tools\stage_resonite_mods.py`), copy `bridge\installer\resonite_mods\` over and set
`CF_RESONITE_MODS=keep`; or set `CF_RESONITE_MODS=skip` to build the installer without them.

### Linux

On Linux the driver is not this one: standalone headsets stream through [WiVRn](https://github.com/WiVRn/WiVRn),
an OpenXR runtime, and the CyberFinger driver is built into its server from the same code. See
[bridge_linux/wivrn](bridge_linux/wivrn/README.md), and [bridge_linux](bridge_linux/README.md) for the Linux bridge
and for [building and installing the Resonite mods](bridge_linux/README.md#resonite-mods) there, which the
installer does on Windows.

## Installation

Quit SteamVR first: it holds the driver while running and reads the registration at startup.

- **Installer** — run `CyberFingerBridge_Setup_<version>.exe`. It installs the bridge app and the SteamVR
  driver, and registers the driver with SteamVR for your user. An earlier CyberFinger Bridge is upgraded in
  place (same folder, one entry in *Settings → Apps*). Uninstall from Windows *Settings → Apps*, which also
  unregisters the driver and puts the Resonite builds and Gale profiles setup changed back as they were.
- **From a build** — `tools\install_driver.cmd` copies `out\build\x64-Release\driver\cyberfinger` to
  `%LOCALAPPDATA%\CyberFinger\SteamVR\cyberfinger` and registers it; `tools\uninstall_driver.cmd` undoes it.
  `install_driver.cmd -InPlace` registers the build folder itself, and `-Status` shows what is installed.

Either way only one CyberFinger driver is registered at a time, and what the previous proof-of-concept
driver had you set up by hand is cleaned up:

- a copy inside `SteamVR\drivers\cyberfinger` is deleted (the installer asks first);
- `TrackingOverrides` entries for `/devices/cyberfinger/...` are removed from
  `<Steam>/config/steamvr.vrsettings`, keeping a backup as `steamvr.vrsettings.cyberfinger-backup`. This driver
  takes the hand roles itself, and the overrides would hide your hands whenever it is switched off.

Then:

1. Stream with hand tracking on. **Steam Link:** keep *Enable Hand Tracking Passthrough* on in its SteamVR
   settings. **Virtual Desktop:** nothing to set; CyberFinger follows VD's hand devices (`HANDL`/`HANDR`, type
   `vd_hand_controller`).
2. Start SteamVR. *Settings → Startup/Shutdown → Manage Add-ons* should list `cyberfinger` as on.
3. For Resonite, install its mods (what each does is under [Application support](#application-support)):
   [MoreFluxActions](https://github.com/DrSciCortex/MoreFluxActionsMod) with its renderer part,
   [SteamVRRoleFix](https://github.com/DrSciCortex/SteamVRRoleFix),
   [CyberFingerMod](https://github.com/DrSciCortex/CyberFingerMod) 1.10+ and
   [ProximityGrab](https://github.com/SciCortex/ProximityGrab). The installer's *Install CyberFinger mods for
   Resonite* option does it, with a box for each mod:
   - **MoreFluxActions and SteamVRRoleFix** are BepInEx plugins, and load from the BepInEx that Resonite starts
     with. With [Gale](https://github.com/Kesomannen/gale), that's the profile's (Gale starts Resonite with
     `--bepinex-target <profile>\BepInEx`), so the installer lists your Gale profiles, the last used one and
     those your builds load ticked, and copies them into
     `%APPDATA%\com.kesomannen.gale\resonite\profiles\<profile>\BepInEx\plugins` and
     `…\Renderer\BepInEx\plugins`. With BepisLoader installed in the Resonite folder itself (no mod manager:
     `<Resonite>\BepInEx\core`), it lists that folder too. A Gale user's game folder also has a
     `Renderer\BepInEx`: that's Gale's loader, not where its plugins go.
   - **CyberFingerMod and ProximityGrab** are ResoniteModLoader mods, and go into the `rml_mods` folder of each
     build with RML that's ticked on the *Resonite builds* page (below): Steam's Resonite, and others found such as a
     private universe. An unticked build gets nothing. The supported setup starts
     RML with the ResoniteModLoaderLoader package in the Gale profile, and nothing passes
     `-LoadAssembly Libraries/ResoniteModLoader.dll` (see [ResoniteModLoader](#resonitemodloader)). Without a
     config of its own, CyberFingerMod gets one with `GamepadBindings` off.
   - It installs neither the loaders nor MoreFluxActions' dependencies (BepisLoader, BepInExRenderer,
     BepisResoniteWrapper, BepInExResoniteShim, InterprocessLib, RenderiteHook, and for RML mods
     ResoniteModLoaderLoader). It checks for them, and lists what's missing or disabled to add in Gale.
   - **Gotcha: RML loaded twice.** Resonite stops as it starts, its window left on the renderer, and the log in
     `<Resonite>\Logs` says `An item with the same key has already been added. Key: ResoniteModLoader`. RML is
     being loaded twice: ResoniteModLoaderLoader in the Gale profile loads it, and so does a
     `-LoadAssembly Libraries/ResoniteModLoader.dll` in how Resonite starts (RML's own setup puts one in *Steam →
     Resonite → Properties → Launch Options*; Gale launches through Steam, so those apply to it too). Remove
     `-LoadAssembly …` there, and from any shortcut that passes it. Setup and the link script say where they find
     one.
   - **Resonite builds:** setup also offers to make each Resonite build it finds load its Gale profile's mods
     however it starts, without Gale's *Launch* or a launch script, and without Gale, to install SteamVRRoleFix
     on its own: [Loading Gale mods without Gale's launcher](#loading-gale-mods-without-gales-launcher). Run
     setup again after installing another build, such as a private universe, to set it up too.
   - Uninstalling removes the copies and puts back what they replaced (kept as `*.pre-cyberfinger`). The mods are
     also in `<install folder>\ResoniteMods`, laid out as they install, to copy by hand into another mod
     manager's profile.

## Loading Gale mods without Gale's launcher

[Gale](https://github.com/Kesomannen/gale) loads a profile's mods only when its *Launch* button starts Resonite:
it passes the profile's location on the command line. Started any other way (Steam's library, SteamVR's
dashboard, a shortcut, a private universe's own launcher), Resonite loads none of them; launch
scripts such as a `LaunchModded.bat` work around that. Linking a build to its Gale profile puts the profile's
location into the build's folder instead, so the build loads the profile's mods however it starts, and Gale keeps
managing them.

What follows is for Windows. On Linux, `bridge_linux/resonite_gale_link.py` makes the same link (with symlinks,
and BepisLoader's `LinuxBootstrap.sh` in the build): see [bridge_linux](bridge_linux/README.md#resonite-mods).

### Setting it up

1. **Install Gale** and add Resonite. Recommended even for just the CyberFinger mods: it installs the loaders
   (BepisLoader, BepInExRenderer, RenderiteHook) and the mods' dependencies, which MoreFluxActions needs.
2. **Give each build a Gale profile** with InterprocessLib and ResoniteModLoaderLoader (which loads the
   ResoniteModLoader mods, CyberFingerMod and ProximityGrab): Gale adds what they depend on, which is all the
   CyberFinger mods need (BepisLoader, BepisResoniteWrapper, BepInExResoniteShim, BepInExRenderer, RenderiteHook).
   Steam's Resonite uses the profile named *Resonite*, else *Default* (Gale's first profile). Another build, such
   as a private universe, uses a profile named like the build: a build of its own can be
   on another Resonite version than Steam's, so its mods are best kept apart.
3. **Run CyberFinger Bridge setup** with *Install CyberFinger mods for Resonite*. Its *Resonite builds* page
   lists every build it finds, ticked: *load Gale profile "…"* links the build, and builds without a profile get
   SteamVRRoleFix on its own ([below](#steamvrrolefix-without-gale)). Ticked builds also get CyberFingerMod and
   ProximityGrab; untick a build to leave it as it is. At the end, setup shows what was done, with
   a *WARN* and a *NEXT* line for anything left to fix.
4. **Clear the launch options.** Take launch scripts (`LaunchModded.bat %command%`), BepInEx arguments
   (`--bepinex-target …`, `--doorstop-…`) and `-LoadAssembly Libraries/ResoniteModLoader.dll` out of *Steam →
   Resonite → Properties → Launch Options*, a Steam shortcut's launch options and shortcuts' targets, keeping your
   own options (`-SkipIntroTutorial`, say). The build's folder now does what they did, and with
   ResoniteModLoaderLoader in the profile a `-LoadAssembly` would load RML twice. Setup's report and `-Status`
   list each one they find, and where to change it; setup doesn't edit them itself (Steam keeps them in its own
   settings, per Steam account).
5. **Start the build however you like.** For a build other than Steam's, use its own shortcut or launcher, not
   Gale's *Launch*: Gale's game location is set per game, not per profile, so *Launch* starts the same copy
   (Steam's, unless you changed it) whichever profile is selected.

Run setup again after installing another build or making its profile: the build shows up, and one that had
SteamVRRoleFix on its own switches to its profile. The link script does the same without setup: *Start menu →
CyberFinger Bridge → Link Resonite builds to Gale profiles* lists the builds and asks which to link. Or run it
from a command prompt in `<install folder>\tools` (by default `C:\Program Files (x86)\CyberFinger Bridge\tools`;
in this repository, `tools\`):

```
resonite_gale_link.cmd -Status
resonite_gale_link.cmd -Game "C:\Games\YourPrivateUniverse-1.2.3.456" [-GaleProfile YourPrivateUniverse]
```

It finds Steam's Resonite, Steam's shortcuts to other games, desktop and Start menu shortcuts that start a folder
holding `Renderer\Renderite.Renderer.exe`, and the builds linked before. For a build started some other way,
pass its folder (or its launcher) with `-Game`.

### What linking changes

In the build's folder:

| File or folder | Change |
|---|---|
| `hookfxr.ini` | `enable=true`: the engine-side loader (BepisLoader) runs on every start. |
| `hostfxr.dll`, `BepisLoader.dll`, `BepisLoader.deps.json`, `BepisLoader.runtimeconfig.json` | That loader, copied from the profile. |
| `BepInEx` | A junction to `<profile>\BepInEx`, where BepisLoader looks when no arguments say otherwise: the engine-side plugins. |
| `Renderer\doorstop_config.ini` | `enabled=true`, `target_assembly=BepInEx\core\BepInEx.Preloader.dll`: the renderer-side loader (UnityDoorstop) runs on every start. |
| `Renderer\winhttp.dll`, `Renderer\.doorstop_version` | That loader, from the profile's RenderiteHook. |
| `Renderer\BepInEx` | A junction to `<profile>\Renderer\BepInEx`: the renderer-side plugins, such as SteamVRRoleFix. |
| `CyberFinger-GaleLink.json` | The record of every change, which undoing works from. |

In the profile, `hookfxr.ini` gets `enable=true` as well (Gale's *Launch* copies it into the build, which would
switch the engine side off again), and `CyberFinger-GaleLink.txt` names the builds that load the profile.
`%LOCALAPPDATA%\CyberFinger\gale_links.json` lists the linked builds, for the uninstaller.

A file the build had that differs is kept as `<name>.pre-gale-link`. A `BepInEx` or `Renderer\BepInEx` folder of
the build's own is moved aside as `….pre-gale-link` when it holds no plugins (Gale leaves such a
`Renderer\BepInEx` in the Steam copy). One with plugins stops the link, with a message to move them into the
profile, or to pass `-Force` to move the folder aside anyway. The profile's mods are neither copied nor changed:
Gale's switch for each mod applies as with its *Launch*, which still works (its arguments take precedence over
these files). So does a launch script that passes `--bepinex-target` and `--doorstop-target-assembly`, which is
no longer needed.

### Switching the mods off, for troubleshooting

```
resonite_gale_link.cmd -Status                               each build: what it loads, each side on or off
resonite_gale_link.cmd -Game "<build folder>" -Mods Off      no mods at all; -Mods On loads them again
resonite_gale_link.cmd -Game "<build folder>" -Engine Off    no engine-side mods; -Engine On
```

`-Mods Off` sets `enable=false` in `hookfxr.ini` (the build's and the profile's) and `enabled=false` in
`Renderer\doorstop_config.ini`. `-Engine Off` sets only `hookfxr.ini`: no engine-side mods, which include the
ResoniteModLoader ones (ResoniteModLoaderLoader is an engine-side plugin), while renderer-side mods such as
SteamVRRoleFix still load. By hand, change the same lines in Notepad, and for no mods change both: while the
engine-side loader runs, RenderiteHook rewrites `Renderer\doorstop_config.ini` on every start. Either way this
applies to starts without Gale's *Launch*, whose arguments switch the loaders on regardless. A `-LoadAssembly`
in how the build starts loads RML outside BepInEx, so its mods stay on: `-Mods Off` names where one is. To
leave out one mod, disable it in Gale as usual.

`-Status` also shows a side switched off by Gale itself: updating BepisLoader in Gale can reset the profile's
`hookfxr.ini` to the package's `enable=false`, which Gale's *Launch* then copies into the build. `-Engine On` (or linking
again) switches it back on.

The logs are `<profile>\BepInEx\LogOutput.log` (engine side), `<profile>\Renderer\BepInEx\LogOutput.log`
(renderer side), and Resonite's own in `<build>\Logs`.

### Undoing it

```
resonite_gale_link.cmd -Game "<build folder>" -Unlink
resonite_gale_link.cmd -Unlink -All                          every build linked on this PC
```

This removes the junctions and the copied files, puts back the `*.pre-gale-link` originals and the profile's
`hookfxr.ini` setting, and deletes the record: the build is as it was, and the profile keeps its mods.

**Uninstalling CyberFinger Bridge** does `-Unlink -All`, then removes the mods setup copied into builds and
profiles and puts back what they replaced (kept as `*.pre-cyberfinger`). If a build can't be restored (a file in
use, say), it asks whether to go on; *No* cancels the uninstall, so you can close what holds the file and
uninstall again.

Without the script: in a command prompt, `rmdir "<build>\BepInEx"` and `rmdir "<build>\Renderer\BepInEx"` remove
the junctions (`rmdir` without `/s` removes a junction only, never the profile's folder it points to). Then, from
`CyberFinger-GaleLink.json`: delete the files under `files` and the folders under `dirs`, rename each `backup`
under `backups` back to its `path`, set `enable=` in the profile's `hookfxr.ini` back to the `old` value under
`iniEdits`, and delete the record.

### SteamVRRoleFix without Gale

For a build without a Gale profile, or without Gale, setup installs SteamVRRoleFix on its own when it is
selected; so does `resonite_gale_link.cmd -Game "<build folder>" -Standalone`. That copies BepInEx for the
renderer (from BepInExRenderer) and its loader (UnityDoorstop, from RenderiteHook) into `<build>\Renderer`, sets
`enabled=true` in `Renderer\doorstop_config.ini`, and puts SteamVRRoleFix into `Renderer\BepInEx\plugins`. A
renderer that already has BepInEx gets just the plugin. It is recorded the same way: `-Mods Off` and `-Unlink`
work as above, and the uninstaller undoes it. Only renderer-side mods load like this: MoreFluxActions and other
engine-side mods need Gale. Linking the build later (make its profile, run setup again) replaces it. BepInEx and
UnityDoorstop are LGPL-2.1: see `<install folder>\ResoniteMods\standalone\THIRD-PARTY-NOTICES.txt`.

### ResoniteModLoader

The supported setup: ResoniteModLoaderLoader in each Gale profile loads ResoniteModLoader from the build's
`Libraries\ResoniteModLoader.dll`, and nothing that starts the build passes
`-LoadAssembly Libraries/ResoniteModLoader.dll` (Steam's launch options, a Steam shortcut's, a shortcut's
target). RML then loads with the profile however the build starts, and `-Mods Off` switches its mods off with
the rest. With both, RML loads twice and Resonite stops as it starts (the gotcha under
[Installation](#installation)). Linking and `-Status` check each build against this: they warn about a double
load or an RML that nothing loads, point out a `-LoadAssembly` that should go, and say where to change it.

## Switching it off

- **For a while, live:** *SteamVR Settings → CyberFinger → CyberFinger controllers active* (setting `active`).
  Off, both CyberFinger devices report disconnected and the headset's own hand tracking takes the hands
  back; on, CyberFinger takes them again. No restart needed.
- **Completely:** *Settings → Startup/Shutdown → Manage Add-ons → cyberfinger* off. SteamVR then does not
  load the driver at all (no devices, no hooks) from its next start.

## Running

1. Start SteamVR (Steam Link connected, hands visible to the headset).
2. Start a bridge in **VR mode**: `python bridge/cyberfinger_gui.py`. Put the CyberFingers in "VR mode".
3. Check what the driver sees: `python bridge/tools/cf_driver_probe.py` (stop the bridge's CyberFinger link first —
   both listen on UDP 27016). With your hands in view it shows `PASSTHROUGH`, `skeleton live 31 bones`, and
   `[SYSTEM pressed]` when the headset's hand makes its system gesture (the Quest palm pinch); haptic requests
   are printed as they arrive.
   `--fake-hand right` drives the right hand from a synthetic fused stream to test the FUSED path.

## Controls

| CyberFinger | SteamVR component | VRChat default | Resonite default |
|---|---|---|---|
| Primary (trigger, analog + click) | `/input/trigger` | Use / interact | Primary |
| Grab (grip) | `/input/grab` (tap to hold) | Grab: a quick tap holds until the next press, a longer press grabs while held | same |
| Joystick (+ push) | `/input/thumbstick` | Move (L), turn (R); push: jump | Axis, secondary (push) |
| MENU — context / rotary-dial button | `/input/b` (B/Y) | Action menu (L/R) | Context menu |
| Start/Select (black wrist button) | `/input/a` (A/X) | Quick menu (L), main menu (R) | Dash |
| Black wrist button held (≥ 0.8 s) | `/input/a_hold` | Gesture toggle (L), Safe Mode (R) | FluxAction1 (L), FluxAction2 (R), with the MoreFluxActions mod |
| Left pink wrist button (power key, a short press) | `/input/pink` (left) | SteamVR dashboard (the dashboard's binding) | same |
| Right pink wrist button (power key, a short press) | `/input/pink` (right), with the bridge's right pink on *SteamVR* | Mute | FluxAction42 (Resonite's mute, with the mod's `MuteToggleAction` at its default, 42) |
| C, D, E (some hardware revisions and models only) | `/input/c`, `/input/d`, `/input/e` | unbound | FluxAction3/4 (C, L/R), 5/6 (D), 7/8 (E), with the mod |
| Pinches, grasp, index point, two-finger point (hand tracking) | `/input/index_pinch` … `/input/index_point`, `/input/two_finger_point` | unbound | Two-finger point FluxAction36/37 (L/R), pinky pinch 38/39, index point 40/41, with the mod (their `/click`s); the rest unbound |

`/input/pinky_pinch/click` is stricter than the pinch's value: it counts only with the palm toward the face (within
50°), the index and middle fingers relaxed, and the thumb on the pinky alone (the ring tip 1.5 cm further from it,
the middle 3.5 cm away), so a thumb on the last two or three fingers doesn't count. SteamVR's log shows each
closing: `pinky pinch counts` or why not, with the palm angle, curls and distances (`PinkyPinchMeant`).

Everything unbound can be bound per app in SteamVR's *Controller bindings* UI, which shows the controls on a
picture of the CyberFinger. A CyberFinger button can also be made the dashboard button with the `button_system` setting.
Holding a pink button shows the CyberFinger's power-off notice after a moment; held about 5–7 s in all, the CyberFinger
switches off and its screen goes dark.

The black button has a long press: held for `black_hold_ms` (800 ms), it is `/input/a_hold` instead of A,
for as long as you keep holding it. In Resonite it is FluxAction1 on the left hand and FluxAction2 on the right,
actions the MoreFluxActions mod adds for ProtoFlux on your avatar. Because of the long press, A reports when you
release it, and can't be held in an app; `black_hold_ms` = 0 gives the plain button back.

In the SteamVR dashboard: the trigger clicks (a light press first locks the laser, so the click lands where it
points), the grip right-clicks, the stick scrolls (push: middle click), B goes back, A (black button) goes
home. The left pink button toggles the dashboard (twice: room view); rebind it in SteamVR's binding UI, under
the dashboard's bindings.

**Grab, tap to hold.** SteamVR's binding modes either toggle on every press or follow the button, so the driver
does this itself: `/input/grab` is the grip button with a latch. A press shorter than `grab_tap_ms` (200 ms)
holds the grab until the next press; a longer press grabs while held. `/input/grip` is the plain button.
`grab_tap_to_hold` (SteamVR settings: *Grab: tap to hold*, on by default) switches the latch off live: the grab
then follows the grip button.

Gesture values come from Steam Link's own hand-tracking gestures while it tracks the hand, and from the
hand skeleton otherwise.

## CyberFinger IMU fusion

With a bridge running and a CyberFinger with the joint IMU connected (some hardware revisions; the body IMU in the
wrist module, which every CyberFinger has, isn't used here), the driver fuses the CyberFinger's joint IMU (on the back of the hand)
with the headset's hand orientation (the FUSED mode bypasses it).
While the headset sees the hand and it turns slowly, the driver learns the IMU's heading and how it sits on the
hand, and measures how far the headset's stream trails the IMU. The hand orientation then comes from the IMU,
kept aligned with the headset's: less jitter, none of the headset stream's lag or overshoot, and it keeps turning
with the hand while the headset has lost it (the position is held then). The status line shows
`imu(lag … ms, mount fit … deg, … solves)`. Switch it off live with `imu_fusion`; `pose_rotation_prediction`
also scales the IMU's angular velocity.

How the IMU sits on the hand, and the streamer's lag, carry over between sessions: the driver keeps them in
`%LOCALAPPDATA%\CyberFinger\imu_calibration.txt` (per hand and hand-tracking source, averaged over sessions)
and starts from them, so only the IMU's heading, new with every power-up, is left to find. The fusion then runs
within a second of the headset seeing the hand, even held still; the full calibration, which needs a few seconds
of turning the hand in view, refines it after. Before anything is saved it starts from the reference CyberFingers'
calibration. A starting point more than 25° off (another CyberFinger, the sensor turned) is ignored, and the status
line says so (`the starting mount is off by … deg`); delete the file to start afresh.

The headset's hand tracking isn't equally good everywhere, and the IMU is the referee. Measured against it, the
headset's orientation goes wrong mostly when one hand is behind the other, when the hand is beside or behind the
head or overhead, and very close to or far from the headset (`src/TrackingTrust.h` has the numbers, Quest 3 over
Steam Link). The less a view is trusted, the slower it may correct the IMU, and only trusted views calibrate it.
And a headset orientation more than 25° from the fused one (narrower in poor views) is refused outright: palm
flips, phantom spins and edge-of-view errors no longer turn the hand. If the headset, in full view, is refused
for 3 s on end, it's the IMU that's off (the CyberFinger slipped on the hand), and the hand follows the headset again;
the calibration pairs from before are dropped, since they describe how the CyberFinger sat then. The status line counts
the refused samples.

Taking the CyberFinger off and putting it back needs nothing:
- **CyberFinger put down, switched on:** the headset sees the hand turn (over 15°/s) while the IMU lies still (under
  3°/s). After about a second of that, the fusion treats the CyberFinger as off the hand: the headset alone gives the
  orientation, and the fusion learns nothing from the bare hand. Once the IMU turns with the hand again (their
  rotation rates within 35 % for half a second), it resyncs.
- **CyberFinger switched off and on:** the IMU's new heading is caught by the data gap. More than 5 s without IMU data
  also resyncs.
- **A resync** keeps the mounting and the lag, and fits the heading again from the next views: the fused
  orientation is back within about half a second of seeing the hand. On the recorded sessions, worn throughout,
  the off-hand test never fired.
- **By hand:** the bridge's **Resync IMU** button, or a **triple tap** on a CyberFinger's joint IMU (the module on the
  back of the hand; three firm taps, 0.1–0.5 s apart, the hand otherwise still), does the same for both hands.
  The tapped CyberFinger answers with two short pulses. The bridge's console logs each tap's size;
  `tap_threshold_g` in its `settings.json` (default 1.0 g) sets how firm a tap must be.

The resync reaches the driver as a count in CFG2's `resync` byte (see [Wire protocol](#wire-protocol-v2)).

Offline, `out\build\x64-Release\fusion_eval.exe <capture.csv> [prior=default|<other capture.csv>]` replays a
capture (with the IMUs) through the same code, optionally starting from another session's calibration.
`tools/handover_check.py <capture>` maps where the headset's tracking goes wrong (it needs a capture with the
headset's pose, which captures record since 2026-09-27, along with SlimeVR's body trackers).

## Haptics

When an app vibrates a CyberFinger hand, the driver sends the request (duration, frequency, amplitude) to
the bridge (`CFHP`, UDP 27016). The CyberFinger GUI shows it at the bottom of each hand panel:
the LED lights while the vibration is requested (brightness = amplitude), the
waveform is drawn at the requested frequency, and a bar shows the time left. `cf_driver_probe.py` prints
the requests.

In VR mode the bridges forward them to the CyberFinger (`bridge/cyberfinger_control.py`): a 6-byte command on the VR
service's control characteristic (`0xCF02`, write without response; amplitude, duration, frequency), merged per
hand while a write is in flight. Firmware 1.3.3+ drives the CyberFinger's DRV2605L and ERM coin motor with it
(CFV1BP boards; others ignore it). An ERM motor can't render short clicks or high frequencies, so the firmware
stretches every pulse to at least ~35 ms, gives any non-zero amplitude enough drive to be felt, extends a
running vibration with each new request, and pulses the motor at the requested frequency only below 30 Hz. The
tuning constants are in the firmware's `src/haptics.h`. Click a hand panel's HAPTIC strip in the CyberFinger GUI
(VR mode) for a test pulse.

## Application support

- **VRChat** — a binding that emulates an Oculus Touch controller, without its model: stick press jump, the menu
  button the action menu, the black button the quick menu (left) and the main menu (right), held the gesture
  toggle (left) and Safe Mode (right); trigger, grip (tap to hold) and sticks as on Touch. VRChat runs CyberFinger
  in its generic controller mode all the same (its log: `can use Oculus Touch controller = False`), where Touch's
  menu actions do nothing, so the binding uses VRChat's own *Quick Menu*, *Action Menu* and *Main Menu* actions.
  With the Touch model simulated, VRChat would draw a Quest controller next to each hand while its menus are open.
  Mute is the right pink button: VRChat's own, with the bridge's right pink on *SteamVR* (the default). VRChat's
  [driver guide](https://creators.vrchat.com/platforms/pc/steamvr-drivers/) asks drivers not to emulate other
  controllers: the emulation is only the default binding's choice, and a binding without it works too. At the
  default skeletal tracking level *Full*, VRChat also turns on its hand-gesture
  controls; `skeletal_tracking_level` = `partial` keeps them off. Check the fingers with *Settings → Controls →
  Accurate* hands. VRChat comes second for now: the defaults are tuned for Resonite.
  Suggested VRChat settings, as its hand-tracking gestures read the same fingers CyberFinger passes on:
  - *Main Menu → Audio & Voice → Microphone Behaviour*: **Toggle**. The right pink button sends one short press;
    with *Push to Talk* the microphone would open only for that moment.
  - *Main Menu → Tracking & IK → Finger Tracking*: finger walking (moving with a thumb and middle finger pinch)
    and *Jump Gesture* **off**. The stick moves and jumps; on, a pinch or a hand pose does too.
  - *Main Menu → User Interface → General UI Settings → One Handed Movement [VR]*: **off**, moving on the left
    stick and turning on the right, as the binding lays them out.
  - *Main Menu → Controls → SteamVR*: *Avatars Use Finger Tracking* **on**, and *Finger Tracking Exclusive Mode*
    **off**: on, VRChat ignores SteamVR drivers' inputs while hand tracking is active.
- **Resonite** — native binding that emulates an Oculus Touch controller. Resonite picks its controller mode
  from the render model of the devices it registers as hands, and only its Touch mode has a dash button, so
  CyberFinger shows itself to Resonite as a Touch controller: the black button (A) opens the dash, B the context
  menu, plus hand skeletons, trigger, grab and stick, on any streamer. Steam Link and Virtual Desktop
  also emulate Touch controllers from hand tracking; while CyberFinger holds the hands the driver marks those
  *never tracked* (`hide_other_hand_controllers`), or Resonite would register them instead of CyberFinger or
  draw them as trackers on the hands. The headset's own hand-tracking devices (Steam Link's hand trackers) are
  hidden the whole time CyberFinger is active: with Touch simulated, SteamVR gives them the same serial as
  CyberFinger (`<headset>_Controller_Left`), and Resonite's engine, which tells controllers apart by serial, would
  let the idle one overwrite CyberFinger's pose and input every frame. A custom binding needs the skeletons in the *OculusTouch* set: without
  them Resonite draws rigid canned hands at a Touch offset. Resonite builds the hand from the *Generic* set's
  pose (`/pose/raw`) and the skeleton (*WithoutController*, model space), undoing its Touch offset for the
  hand, and ignores the tracking level. **Grabbing with the hand** is the
  [ProximityGrab](https://github.com/SciCortex/ProximityGrab) mod's, from the skeleton: a fist grabs what's near the hand (the grab
  sphere, not the laser), an index-thumb pinch is a precision grab at the pinch point, and the grab button grabs
  as before. It detects the gestures itself, so leave the pinch and fist gestures unbound in SteamVR (the defaults
  bind neither). It can be switched off from the Grabbing context menu.
  **Switching between CyberFinger and the Quest controllers** needs two Resonite mods. Resonite's renderer picks
  up a hand's device only when that device connects, and only if it holds the hand's role at that moment; its
  engine binds locomotion to the controller it registered last.
  [SteamVRRoleFix](https://github.com/DrSciCortex/SteamVRRoleFix), a BepInExRenderer plugin, makes the renderer
  follow SteamVR's hand roles, so hands and input don't freeze on the idle device. It also keeps hand controllers
  waiting for their hand from being drawn as trackers, and (0.3+) clears the inputs of the device switched away
  from, so no button stays held.
  [CyberFingerMod](https://github.com/DrSciCortex/CyberFingerMod) 1.10+ (`FollowActiveController`) rebinds
  locomotion to the controller in use; with this driver, set its `GamepadBindings` off. Each take-back from the
  controllers also restarts the IMU fusion as from cold (from the saved calibration).
  **Programmable buttons:** with the [MoreFluxActions](https://github.com/DrSciCortex/MoreFluxActionsMod) mod,
  the binding's *Flux Actions* set maps the black button's hold (`/input/a_hold`) to FluxAction1 (left) and
  FluxAction2 (right), C/D/E to 3–8, the two-finger point to 36/37 and the pinky pinch and index point to 38–41
  (left, then right): dynamic
  impulses for your own ProtoFlux, nothing until you build some. 42 is left for the bridge's right pink button. See
  [docs/README.md](docs/README.md#programmable-buttons-in-resonite).
- **CyberpunkVRPort (experimental patched build)** ? dedicated OpenXR bindings for separate Menu actions,
  raw-grip utilities, right stick click jump and finger-driven nearby grabs. Weapon hand is configurable
  in the mod; the documented preset is left-handed. See [setup and controls](docs/cyberpunk-vr-port.md).
- **Blade & Sorcery (PC SteamVR, experimental)** - right-/left-handed gesture grab,
  physical quick actions and wrist menu/camera shortcuts; includes a buttons fallback.
  Ordinary gestures are the default and need no game mod. The optional Native layout
  and tracking telemetry support the separate CybrFngr Native B&S mod DLL.
  [Controls and native setup](docs/blade-and-sorcery.md). Generate this game with
  `python tools/generate_blade_and_sorcery_bindings.py --game "D:/SteamLibrary/steamapps/common/Blade & Sorcery"`.
- **Other SteamVR Input and OpenXR apps** — SteamVR converts the app's Index binding (then Touch, then Vive)
  and tells the app it is talking to an Index controller (`resources/input/cyberfinger_remapping.json`).
  Trackpad bindings are dropped: the CyberFinger has no trackpad.
- **Legacy-input apps** — Index emulation through `legacy_bindings_cyberfinger.json`.

The bindings are generated with `python tools/generate_bindings.py` (VRChat/dashboard read installed app files).
Cyberpunk alone needs no installed-app reads: `python tools/generate_cyberpunk_bindings.py`.
The render models (a small marker at the wrist plus the Index controller's pose locators) come from
`python tools/make_rendermodels.py`.

## Configuration

Settings live in `resources/settings/default.vrsettings` (section `driver_cyberfinger`); override them in
`<Steam>/config/steamvr.vrsettings`. The main ones also appear in SteamVR's settings UI.

| Setting | Default | Description |
|---|---|---|
| `active` | `true` | Live on/off: off hands the hand roles back to the headset's own hand tracking |
| `loadPriority` | `2000` | Loads the driver before Virtual Desktop's (1000) and Steam Link's (110), so the hooks see their hands being created |
| `hand_selection_priority` | `1000` | Hand-role priority (Steam Link hands: −1) |
| `skeletal_tracking_level` | `full` | Reported to apps: `full` (camera-tracked hands) or `partial` (VRChat keeps its hand-gesture controls off). Resonite ignores it |
| `optical_tap` / `optical_tap_hook` | `true` / `true` | Use the headset's hand tracking; capture its skeleton with the hook |
| `tap_serial_left` / `_right` | `Hand_Left` / `Hand_Right` | Serial substrings of the headset hand devices (Steam Link: `VRLINKQ_Hand_Left`); `a\|b` lists |
| `tap_controller_types` | `svl_hand_interaction_augmented\|vd_hand_controller` | Controller types of headset hand devices (Steam Link, Virtual Desktop). Only the skeletons of these devices and of `tap_serial_*` matches are followed: a controller's (Steam Link's Touch controllers estimate fingers from their sensors) would keep CyberFinger from releasing the hand to it |
| `forward_tap_system_button` | `false` | Also forward the headset hand's system button (Quest left-palm pinch) to the dashboard; the left pink button opens it anyway (through the dashboard's binding) |
| `black_hold_ms` | `800` | Held this long, the black button is `/input/a_hold` instead of A (Resonite: FluxAction1 left, FluxAction2 right, with the MoreFluxActions mod); A then reports on release. 0 = no long press |
| `grab_tap_to_hold` | `true` | `/input/grab`'s tap to hold (live); off, the grab follows the grip button |
| `grab_tap_ms` | `200` | With tap to hold: a press shorter than this holds until the next press |
| `passthrough_events` | `true` | Republish the headset hands' updates as they arrive; `false`: once per frame |
| `pose_filter` | `true` | Filter the headset hand pose in PASSTHROUGH, for the sources in `pose_filter_types` (live) |
| `pose_filter_types` | `svl_hand_interaction_augmented` | Source controller types whose poses are filtered: Steam Link's. Others (Virtual Desktop's `vd_hand_controller`) pass through as streamed; add types with `\|` (live) |
| `pose_filter_min_cutoff` / `pose_filter_beta` | `1.0` / `15` | One Euro filter: cutoff (Hz) of a still hand, and how fast it opens with speed (live) |
| `pose_prediction` | `0.5` | Scale of the linear velocity SteamVR extrapolates with: 0 = no prediction (live) |
| `pose_rotation_prediction` | `0.0` | Likewise for the angular velocity; above 0 a pointing laser jitters (live) |
| `pose_filter_gate_cm` | `5` | A pose this far beyond plausible hand motion is treated as a glitch (live) |
| `imu_fusion` | `true` | In PASSTHROUGH, the hand orientation from the CyberFinger's joint IMU (calibrated against the headset's, which keeps it aligned), also while the hand is out of view; needs a bridge with the CyberFinger connected (live) |
| `yield_to_controllers` | `true` | When a hand's headset hand tracking stops while a controller for that hand is tracked (you picked the Touch controllers up), release the hand to the controller (after ~0.5 s); take it back as soon as hand tracking is live again |
| `hide_other_hand_controllers` | `true` | While CyberFinger holds the hands, mark other drivers' hand controllers (the Touch controllers Steam Link and Virtual Desktop emulate from hand tracking) *never tracked*, so apps skip them; the headset's hand-tracking devices the whole time CyberFinger is active (CyberFinger still reads them); undone when CyberFinger is switched off |
| `debug_captures` | `false` | Record the hand data on request (`tools/analyze_tap_capture.py --capture N`); a debugging tool |
| `button_a` / `button_b` / `button_system` | `STSEL` / `MENU` / `NONE` | CyberFinger buttons for A, B and the dashboard; names: `TRIGGER GRIP C D E MENU STICK STSEL`, combine with `\|` |
| `legacy_5bit_buttons` | `false` | Decode legacy `CFGP` packets with the pre-2026 firmware button layout |
| `fused_timeout_ms` | `150` | Fall back from FUSED to PASSTHROUGH after this silence |
| `disconnect_after_ms` | `0` | Report the device disconnected after this long without any data (0 = never) |
| `handtracking_udp_port` / `context_udp_port` | `27015` / `27016` | Bridge → driver / driver → bridge |
| `bind_loopback_only` | `true` | Accept packets from this PC only |
| `grip_angle_*`, `pose_offset_*` | −60, 35, 0 / 0, −0.1, 0 | Only used if no Steam Link skeleton was ever seen (hook off): offset from the Steam Link hand pose to CyberFinger's |

## Wire protocol (v2)

Little-endian, packed, localhost. The layouts are defined in [`src/Protocol.h`](src/Protocol.h) and mirrored in
[`bridge/cf_protocol.py`](bridge/cf_protocol.py); `tests/` keep them in sync.

| Packet | Direction | Port | Content |
|---|---|---|---|
| `CFG2` (34 B) | bridge → driver | 27015 | CyberFinger buttons (firmware bit layout), analog trigger, centred stick (+y up), battery, the pink button (`buttons2`), the IMU fusion resync count (`resync`: a new value resyncs that hand) |
| `CFHS` (1140 B) | bridge → driver | 27015 | Fused hand: `/pose/raw` + velocities in SteamVR raw space, curls/splay, 31 bones |
| `CFOP` (2200 B) | driver → bridge | 27016 | HMD pose, driver mode per hand, Steam Link hand pose + skeleton + system button |
| `CFHP` (36 B) | driver → bridge | 27016 | Haptic request: hand, duration, frequency, amplitude |
| `CFIM` (96 B) | bridge → driver | 27015 | Raw CyberFinger IMU slots (quaternions, accel) per BLE report: the driver's IMU fusion and its captures (`CFOP` header flag `0x1` marks a capture) |
| `CFGP` (12 B) | bridge → driver | 27015 | Legacy CyberFinger packet, still accepted (stick uncentred, +y down) |

Firmware button bits: `0x01` trigger, `0x02` grip, `0x04` C, `0x08` D, `0x10` E, `0x20` MENU, `0x40` stick click,
`0x80` Start/Select.

## Tests

```bash
python -m unittest discover -s tests          # protocol (Python)
ctest --test-dir out/build/x64-Release        # protocol vectors, skeleton synthesis, re-rooting (C++)
```

The WiVRn driver's tests (Linux): [bridge_linux/wivrn](bridge_linux/wivrn/README.md#tests-and-the-simulator).

## Troubleshooting

The driver logs to `<Steam>/logs/vrserver.txt` with the prefix `cyberfinger:`, including a status line per
hand every 10 s.

- **Steam Link's hands show instead of CyberFinger, or buttons do nothing in apps** — check for
  `activated as device … hand priority 1000` in the log. `tools\install_driver.cmd -Status` also lists old
  `TrackingOverrides` entries left in place.
- **Hand follows the headset tracking but the fingers don't move** — the skeleton hook isn't capturing:
  look for `OpticalTap:` lines in the log; the probe shows `no skeleton`.
- **Hands stutter or lag behind the streamer's** — the status line should read `mode=2 (event)` with
  matching `rates(src …, republished …)`: the driver passes each update on as it arrives. Without the pose
  hook (`OpticalTap: … pose tap unavailable`) it falls back to once per frame.
- **Hands overshoot or jitter (Steam Link)** — Steam Link streams the hand pose already predicted forward, with
  noisy velocities, and SteamVR extrapolates those again to the display time. The pose filter (`pose_filter`)
  smooths the pose, drops runaways and publishes its own velocities; compare it on and off live in SteamVR's
  settings, against Steam Link's own pointer (drawn on the headset from local data). It applies to Steam Link
  only (`pose_filter_types`): Virtual Desktop's hands are clean and pass through untouched; the log says which
  (`headset hand source …: pose filter …`). With *Debug captures* on (advanced settings), record the raw stream
  with `python tools/analyze_tap_capture.py --capture 10`: it also records what CyberFinger publishes over the
  same seconds and compares the two (wrist and a laser along the index finger: jitter, error, delay, occlusion
  holds). With the bridge running, the CyberFinger IMUs are recorded too, on the driver's clock: their timing against
  the optical rotation, and rotation jitter side by side. Replay a capture through the filter offline with
  `out\build\x64-Release\filter_eval.exe <capture.csv> [name=value ...]` (e.g. `beta=10 rotPrediction=0.5`):
  it reports position and laser-target (2 m) jitter and error, as displayed 0 and 40 ms ahead, for a still,
  slowly moving and fast moving hand.
- **Hands are noisy** — hold them still in view for ten seconds. The status line's `noise(held still …)`
  gives the source's jitter: RMS spread of the wrist and fingertips in the world, and of the fingertips
  relative to the wrist (`fingers`). Compare streamers or settings with it; `OpticalTap: … now follows …`
  names the source.
- **Buttons don't register** — the bridge must run in VR mode; the log shows
  `StudioLink: first CyberFinger packet (CFG2)`.
- **Resonite: hands freeze, or no input, after switching back from the Quest controllers** — install
  [SteamVRRoleFix](https://github.com/DrSciCortex/SteamVRRoleFix). The driver log should show the take-back
  (`hand tracking is back: taking the hand again`). With the plugin, its `BepInEx/LogOutput.log` shows
  `registering device …` when it stepped in.
- **Resonite: with the Quest controllers, X/A doesn't open the dash, and a double X toggles UI edit mode** — a
  button held on the other device at the switch is still held for Resonite. Update
  [SteamVRRoleFix](https://github.com/DrSciCortex/SteamVRRoleFix) to 0.3+ and restart Resonite; its log shows
  `is no longer read: cleared its inputs (…)` when it releases one.
- **Resonite (or a private universe) starts without its mods, or stops as it starts** — `resonite_gale_link.cmd
  -Status` shows what each build loads, whether each side is on, and what loads RML twice; `-Mods Off` starts
  a build without mods. See [Loading Gale mods without Gale's launcher](#loading-gale-mods-without-gales-launcher).
- **Resonite: tools work after switching back, but you can't move or jump** — install
  [CyberFingerMod](https://github.com/DrSciCortex/CyberFingerMod) 1.10+ with `FollowActiveController` on. The
  Resonite log shows `rebinding locomotion to it` at each switch.
- **CyberFinger doesn't take the hands back from the controllers** — the status line shows `pose=0 skel=0`: the
  headset is still in controller mode and sends no hand tracking. Switch the controllers off, or keep them still,
  or turn off the Quest's automatic switching and pick hand tracking yourself.
- **The left pink button doesn't open the dashboard** — it needs firmware 1.3.3+ (the bridge log shows
  `L PINK`), and the dashboard's binding must be CyberFinger's (SteamVR's binding UI, the dashboard's bindings).
- **The right pink button doesn't mute** — the bridge must be in VR mode. On *mic mute*, its log shows
  `Mic: muted` / `Mic: live`: it mutes Windows' default recording device (and the default communications one).
  On *SteamVR*, the app's binding mutes: in Resonite that needs the MoreFluxActions mod with `MuteToggleAction`
  at 42, its default (its log shows `FluxAction42: muted`), and Resonite's binding the CyberFinger default.

## Architecture

- `src/ServerProvider` — driver setup and the per-frame loop
- `src/CyberFingerController` — the two controller devices, output modes, inputs
- `src/OpticalTap` — Steam Link hand capture: MinHook observation hooks on `IVRDriverInput` and
  `IVRServerDriverHost::TrackedDevicePoseUpdated`
- `src/StudioLink` — UDP link to the bridges
- `src/SkeletonSynth` — procedural SteamVR hand (port of Valve's `handskeletonsimulation` sample), used when
  no tracked skeleton is available
- `bridge/` — the CyberFinger GUI (`cyberfinger_gui.py`), the protocol module (`cf_protocol.py`), the driver
  link and haptics display (`driver_link.py`, `haptics_view.py`), the hand drawing both bridges' GUIs share
  (`hand_views.py`), tools
- `archive/` — files from the earlier proof-of-concept driver, kept for reference

## License

The CyberFinger SteamVR driver is licensed under the GNU General Public License v3.0 (GPL-3.0-only).
Unless otherwise noted in individual source file headers, all source code in this repository is licensed under GPL-3.0-only. 
Any redistribution of this software—whether in source or binary form, including distribution in physical devices—must comply with the terms of GPL-3.0, 
including the obligation to provide corresponding source code and installation information for modified versions.

The full license text is provided in the LICENSE file. 

Third-party code: [MinHook](https://github.com/TsudaKageyu/minhook) (BSD-2-Clause, `third_party/minhook`);
`src/SkeletonSynth.cpp` ports Valve's OpenVR SDK sample `handskeletonsimulation` (BSD-3-Clause, notice in the file).

## Contributing

By contributing to this project, you agree to the Contributor License Agreement in CLA.md.
