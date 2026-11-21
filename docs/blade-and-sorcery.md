# Blade & Sorcery: CyberFinger controls

The **NATIVE neon gestures** layout is for the CybrFngr Native ThunderScript
mod. SteamVR retains poses, skeletons, haptics, sticks, UI clicks and wrist
shortcuts; the mod owns gameplay grab/use/cast/TK and quick-key actions.
Native mode requires this driver's `src/NativeInput.h` publisher and the separate
CybrFngr Native game-mod DLL. The driver and mod compile and pass desktop checks;
headset/gameplay validation is still required. This repository defaults to the
ordinary gesture layout, which works without a game mod. Native is opt-in.

The C# mod source lives in the separate `CybrFngrBladeNSorcery` project under
`src/CybrFngr`. Install its `CybrFngr.Native.dll` and manifest together in
`BladeAndSorcery_Data/StreamingAssets/Mods/CybrFngrNative`. The driver does not
install that mod, and selecting the Native binding alone does not enable native
interaction: its grab/use/cast actions depend on the game mod being loaded.

| Native control | Action |
|---|---|
| Open/reach/close | Wrap a touched eligible handle; open releases/throws |
| Thumb-index pinch | Eligible small/light handles and bow strings |
| Grip key, either hand | Held-item alternate use/sliding, or empty-hand TK |
| Trigger, either hand | Use/cast/TK pull; UI click through SteamVR |
| Dominant / support MENU | Slow time / spell selection |
| MENU during TK | Spin; trigger already pulled when MENU is pressed selects repel |
| Left/right stick | Move/kick; turn/jump; UI scroll |
| Left black / left pink wrist | Game menu / dashboard |
| Right black wrist | External view; hold screenshot |
| Right pink wrist | Camera lock; route pink to SteamVR in the CyberFinger Bridge |

In **Mod Options / CybrFngr Native**, Left-handed swaps MENU roles while
preserving physical tracking/stick/wrist routes. Dual magic makes both MENU keys
select spells. Both grip keys remain contextual. Open your hands and release all
quick controls after enabling, mode changes or tracking recovery. No C/D/E needed.

Native options include pinch, catches, contact climbing, close threshold, optical
evidence, neon intensity, spell/kinetic circuits, traces, haptics and diagnostics.
Catch assistance only accepts incoming targets in native touch range. Tracking
loss stops key actions, then releases owned grips after a short grace. Ordinary
bindings below cannot provide these policies.

After building/deploying the telemetry driver and installing the mod:

```powershell
python tools\generate_blade_and_sorcery_bindings.py --game 'D:\SteamLibrary\steamapps\common\Blade & Sorcery' --native-default
```

Repeat with `--input-dir out/build/x64-Release/driver/cyberfinger/resources/input`
for an existing driver package. Restart SteamVR. A personal binding may override
the profile default; explicitly choose the default/native layout. Diagnostics
shows when the mod has detected competing gameplay bindings. When reverting,
disable the mod and select an ordinary binding to restore game-owned actions.

`--native-default` changes only our B&S default, preserving other applications
and refusing to replace a custom B&S default. Without it, generation preserves
the current default. Native has one file, `steam.app.629730_cyberfinger_native.json`;
handedness is managed inside the mod.

## Ordinary bindings without the native mod

Right-handed and left-handed presets target the installed PC game's existing
action set without a game mod. Select **CyberFinger Blade & Sorcery - right-handed
- gesture grab** or **CyberFinger Blade & Sorcery - left-handed - gesture grab**.
Each has a reduced buttons fallback. These are experimental bindings,
structurally checked against B&S 1.3.2.47651, not headset-validated.

The gesture layout is registered for `steam.app.629730`. It sends an ordinary
B&S grip press when CyberFinger's grasp value crosses 0.70, and releases below
0.40. These are starting thresholds, not a claim of calibrated performance.
B&S supplies its existing nearby-interaction behavior. The binding cannot
select a target, distinguish a deliberate catch from a fist passing a handle,
or apply tracking-loss recovery. Those belong to the Native layout and mod.

The dominant hand is the weapon hand; the support hand handles spell selection
and telekinesis. Left-handed mode swaps those roles, keeping triggers consistent
and locomotion in place. The core layout uses trigger, grip, MENU and joystick;
C/D/E are unassigned.

| Quick control | Right-handed mode | Left-handed mode |
|---|---|---|
| Left trigger | Use / cast / telekinesis pull / UI | Same |
| Right trigger | Use / cast / telekinesis pull / UI | Same |
| Left grip key | Telekinesis | Weapon alternate use |
| Right grip key | Weapon alternate use | Telekinesis |
| Left MENU key (`b`) | Spell selection / telekinesis spin | Slow time |
| Right MENU key (`b`) | Slow time | Spell selection / telekinesis spin |

| Control shared by both modes | Action |
|---|---|
| Close/open fingers | Grab/release; analog grasp also supplies GripAxis |
| Left stick | Move; click kicks |
| Right stick | Turn; click jumps; also stock external-camera height control |
| Either stick in UI | Scroll |
| Left black wrist, short | Game menu |
| Right black wrist, short | External view |
| Right black wrist, hold | Screenshot |
| Left pink wrist | Existing SteamVR dashboard binding |
| Right pink wrist, short | External-view lock |

The black and pink buttons are the two wrist buttons on each hand. All frequent
actions are on finger gestures, trigger/grip/MENU or sticks. Both hands retain
their own trigger and gesture grab. Switching handedness never exchanges the
left/right poses, skeletons, haptics, wrist shortcuts or stick axes/clicks.
This is a binding choice; it does not alter the game's player settings.

The trigger deliberately shares the game's contextual use/cast/pull actions.
Support MENU shares spell selection and telekinesis spin, following the stock
Index binding's contextual pairing. Verify that MENU opens spell selection when
idle and spins when telekinesis is active. Dominant grip emits AlternateUse2
directly; dominant MENU emits only SlowTime, with no delayed gesture or wrist
reach required.

These presets specialize in weapon + support-hand magic. Spell selection and
telekinesis acquisition are on the support hand; alternate use is on the weapon
hand. This does not provide dual-hand spell selection, support-weapon alternate
use, or a dedicated telekinesis repel action. The native context router adds
those cases. The binding alone cannot tell whether a hand is
holding a weapon; adding several conflicting actions to grip would not implement
that router. Trigger casting still works on either hand with an equipped spell.

The buttons fallback binds physical grip to both GrabGrip and telekinesis,
matching the usual controller intent. It keeps finger animation and MENU roles,
but omits weapon alternate use because that grip key now grabs items. It is a
diagnostic fallback, not equivalent to the gesture layout. Select it when tuning
gesture thresholds or comparing behavior. Both
layouts use raw `/input/grip`; neither uses CyberFinger's latched `/input/grab`.
Pinches, index-point and two-finger-point are left unassigned to avoid incidental
spell or menu activation.

Launch the game in **SteamVR mode** with CyberFinger active. The binding emulates
Index controller identity and preserves both raw poses, both skeleton actions
and both haptic outputs. Check hand alignment in the game before evaluating
grabs: Index emulation and skeleton transport do not prove correct palm offsets
or full per-joint finger animation. B&S may animate from curls and its own held
weapon poses.

In SteamVR's controller bindings for Blade & Sorcery, choose the CyberFinger
gesture layout with the desired handedness. If a previous personal binding remains selected, use the
default binding or select/import the generated JSON through the binding UI.
The fallback file is provided alongside the default; it is not automatically
activated. File generation cannot prove which binding SteamVR has selected.

For the right pink shortcut, set the CyberFinger Bridge's **Right pink button** routing
to **SteamVR**; mic-mute/Flux routing consumes it before the game receives it.
Black short presses normally arrive on release; holding produces `a_hold`
instead. Screenshot needs that hold mode enabled (`black_hold_ms`, normally
800). The left pink dashboard assignment is kept in the compositor; no game
action competes with it. Keep existing power-off long-press behavior in mind
when using the pink keys.

```powershell
python tools\generate_blade_and_sorcery_bindings.py --game 'D:\SteamLibrary\steamapps\common\Blade & Sorcery'

# Make the stable default filenames left-handed; explicit variants still coexist.
python tools\generate_blade_and_sorcery_bindings.py --game 'D:\SteamLibrary\steamapps\common\Blade & Sorcery' --dominant-hand left
```

The generator validates before writing and preserves existing app defaults.
Without `--dominant-hand`, the stable filenames use the right-handed layout.
Explicit left/right variants always remain available, so selecting a mode in
SteamVR does not require regenerating files. An existing personal binding or a
profile entry pointing elsewhere is not replaced by the generator.
It is intentionally separate from `generate_bindings.py`, so regenerating B&S
does not need access to VRChat, SteamVR's installation or another game. The
normal driver build copies the input resources into its packaged driver.
The existing package at `out/build/x64-Release/driver/cyberfinger` can receive
these JSON resources without rebuilding the DLL. If SteamVR uses a different
driver installation, deploy those resources using the existing driver workflow;
this task did not inspect SteamVR's registration/settings outside the allowed
directories. Restart SteamVR after changing driver profiles.

Files under `resources/input/bindings/`:

- `steam.app.629730_cyberfinger_right.json` and `_left.json`: explicit gesture modes.
- `steam.app.629730_cyberfinger_right_buttons.json` and `_left_buttons.json`: explicit fallbacks.
- `steam.app.629730_cyberfinger.json` and `_buttons.json`: selected-hand aliases.

Only the stable `steam.app.629730_cyberfinger.json` is added to
`cyberfinger_profile.json` when no B&S default already exists. Update the source
and packaged input directories together when changing the alias default; an
`--input-dir` argument selects the destination. Restart SteamVR and verify the
active game binding after deployment. Native mode uses a persistent handedness
setting in Mod Options.

## Driver-side validation

From a configured driver checkout, the normal CMake test build includes native
telemetry validation. It exercises the same packet builder used by the driver
and writes `native-frame.bin` into the build directory for cross-language checks.
It does not create live SteamVR mappings or require the game to run.

```powershell
cmake --build out/build/x64-Release --target driver_cyberfinger driver_tests native_input_tests
ctest --test-dir out/build/x64-Release --output-on-failure
python -B -m unittest discover -s tests -p test_blade_and_sorcery_bindings.py -v
```

The Windows-only `native_input_tests` target covers freshness, physical keys,
optical evidence and the 128-byte packet layout. The binding tests cover both
handed modes, the buttons fallback, Native action ownership and preserving app
defaults. These checks do not establish headset feel or Unity rendering quality.
