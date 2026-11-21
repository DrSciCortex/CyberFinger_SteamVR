# CyberpunkVRPort experimental binding

These bindings target the **CyberFinger-patched CyberpunkVRPort**, with
`config/cyberfinger.ini` containing `enabled=1`. The stock mod does not create the
five `cf_*` actions. This is a source integration, not a tested game release;
SteamVR selection, hand poses, finger tracking and gameplay still need a headset
test. No game DLL is included here.

The driver's default bindings register `system.generated.cyberpunk2077.exe`
(expected for a direct/GOG executable launch) and `steam.app.1091500` (Steam).
The observed OpenXR key is `system.generated.openxr.cyberpunkvrport.cyberpunk2077.exe`
(SteamVR log, 2026-11-14); this alias is registered too. Without it SteamVR
auto-remapped Index bindings and omitted the four `cf_*` actions. A non-Steam
shortcut can have its own key: use the key in SteamVR's exported binding, then run
`python tools/generate_cyberpunk_bindings.py --app-key YOUR_KEY` to generate and
register that alias. Do not add `_openxr` to the app key; that suffix is used in
SteamVR's per-app settings, while the binding's `category` is `openxr`.

## Controls

For your left-handed weapon setup, enable **Left-handed controls** in the mod
and leave **Left-handed bindings** at **Same as right-handed**:

```ini
xr_input_actions=1
xr_xinput_hook=1
xr_finger_bones=1
xr_grip_from_fingers=1
xr_left_handed=1
xr_left_handed_bindings=0
```

Merge these into the mod's existing `config/vrport.ini`. For a right-handed weapon,
set `xr_left_handed=0`. Both triggers keep their physical hand roles in the binding;
the mod handles the weapon-side swap. Other mirroring modes can also swap ordinary
buttons/sticks. Black wrist roles always follow the weapon hand: weapon hand ejects/pauses; other hand reloads/heals. Pink buttons stay on their physical sides. F10 is keyboard-only.

| Control | Default function with the left-handed preset |
|---|---|
| Left/right stick | Move / turn, dash and crouch |
| Left/right stick click | Click to sprint (center to stop) / jump; left hold also supports cross-hand shortcuts |
| Left/right trigger | Fire / aim or block |
| Left/right physical grip | Scanner / R3 (melee, slide release; context dependent) |
| Left/right Menu | Weapon wheel (tap next weapon) / inventory (hold 0.65 s for OS cyberware) |
| Left/right black wrist, short | Magazine eject / quick reload |
| Left/right black wrist, hold | Pause / heal |
| Left/right pink wrist | SteamVR dashboard / world VR menu (`cf_settings`) |
| Close fingers near a target | Grab, evaluated by the patched mod |

Set the bridge's **Right pink button** option to **SteamVR**. Short black presses
arrive on release; a long press emits `a_hold` instead. Tap right pink wrist to
open/close the world-space VR menu. Hold both Menu buttons for 0.5 seconds to
toggle the larger HUD; prompts and conversations temporarily show it. Release
both before repeating. Left-stick + right-Grip is retired. Full-forward movement
alone never starts sprint for CF.
With the unified-menu plugin, Trigger selects, Menu goes Back/cancels on release,
and Grip provides secondary/context actions. In native Cyberpunk menus (including
wardrobe), these are A, B and X; hold Grip for hold-X prompts. In the world-space
VR menu, Grip opens existing item actions or sorting options. In F10, Grip is
right-click for panel options, while holding its title handle still moves it.
Menu dismisses an F10 popup/edit before closing the panel. Both-Menu emits no B.
Left stick navigates and right stick switches tabs. Install the matching plugin
DLL and Lua scripts together; no driver or binding change is needed.
During conversation choices right stick up/down selects and right trigger confirms.
Left-stick movement and right-stick left/right turning remain available.
The right vertical axis and trigger resume gameplay only after release. Physical grip uses raw `/input/grip`,
not the driver's latched `/input/grab`. Pinch, fist and grab inputs are unbound;
the mod reads finger joints with `XR_EXT_hand_tracking`. There are no invented
OpenVR skeleton actions in this OpenXR binding. Grip/aim poses and haptics are
bound for both hands. SlimeVR tracker roles are configured separately in SteamVR
and the mod; they are not CyberFinger controller inputs.

The new right-pink/black-hold layout requires the matching new game plugin.
The older selected layout keeps right pink assigned to heal until replaced.

## Package and select

`python tools/generate_cyberpunk_bindings.py` regenerates just these assets using
the checked-in dashboard binding. It does not read an installed SteamVR or game.
The general `tools/generate_bindings.py` also regenerates them after its other apps.

The existing CMake build/install packages `resources/input`, including these files.
After updating your installed driver, restart SteamVR and launch the patched mod.
In SteamVR controller bindings, select **CyberFinger CyberpunkVRPort** for the game.
A previously saved personal binding takes priority over a driver default; changing
these files does not replace that personal binding. If the five CyberFinger actions
are absent, check the mod patch and `enabled=1` before changing the binding.

### Optional: left pink pauses the game

Legacy option: the `*_cyberfinger_pink_pause.json` variants do not implement the new handed black-wrist layout. Use the default binding for the controls above. These older variants moved the left black-hold action to left pink.
Pair one with `vrcompositor_cyberfinger_black_hold.json`, which moves the dashboard
to left black hold. Neither variant is registered by default. In the driver's
`cyberfinger_profile.json`, change the game's `binding_url` to its pink-pause file
and the `openvr.component.vrcompositor` entry to the black-hold file, then update
the driver and select both layouts in SteamVR. This dashboard change applies to
**all apps**, including Resonite and VRChat; consider their black-hold assignments.
Restore both original binding URLs/layouts to return to the default.

Do not pair the pink-pause game variant with the original dashboard layout: the
same pink press would request both pause and the dashboard.

## Offline validation and references

Run `python -B -m unittest discover -s tests -p test_cyberpunk_bindings.py`.
These checks cover action types, available inputs, pose/haptic completeness,
dashboard conflicts and alias generation. They do not replace a SteamVR playtest.

Binding priority and OpenXR interaction-profile selection follow Valve's
[driver documentation](https://github.com/ValveSoftware/openvr/blob/master/docs/Driver_API_Documentation.md#binding-profiles).
The `openxr` category follows Valve's binding UI's `SelectedBindingIsOpenXR` and
`SaveCurrentBinding` implementation, mirrored in
[GameTracking-SteamVR](https://github.com/SteamDatabase/GameTracking-SteamVR/blob/master/resources/webinterface/dashboard_prettier/controllerbindingui.js).
