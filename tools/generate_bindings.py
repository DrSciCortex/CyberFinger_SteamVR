# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Generate the default SteamVR bindings shipped with the CyberFinger driver.

    python tools/generate_bindings.py [--steam "C:/Program Files (x86)/Steam"]

Writes into resources/input/:

  bindings/steam.app.438100_cyberfinger.json   VRChat   — VRChat's own Touch binding, laid out for the CyberFinger
  bindings/steam.app.2519830_cyberfinger.json  Resonite — its "Generic" action set (unknown controllers)
  bindings/vrcompositor_cyberfinger.json       SteamVR dashboard — derived from SteamVR's Index binding
  legacy_bindings_cyberfinger.json             legacy-input apps, emulating an Index controller

The CyberFinger has, per hand: push-stick (+click), analog trigger (primary), grip button (grab),
B = MENU (the context / rotary-dial button), A = Start/Select, and C/D/E. No touchpad, no capacitive touch.
Derived bindings therefore drop touch inputs, trackpad sources and VRChat's gesture activators.

The driver also exposes the standard hand-tracking gestures (index/middle/ring/pinky pinch, grasp, index
point). No app action is bound to them: the thumb-pinky pinch, once mapped like B (MENU) and to Resonite's dash,
fired too easily. The SteamVR dashboard opens from the left CyberFinger's pink button (/input/system), not from the
Quest palm pinch (the driver no longer forwards it by default).

In Resonite, the MoreFluxActions mod's Flux Actions (ProtoFlux on the avatar, nothing without flux) take the
buttons Resonite doesn't use and two of the gestures (left, right): black button held 1, 2; C 3, 4; D 5, 6;
E 7, 8; two-finger point 36, 37; pinky pinch 38, 39; index point 40, 41. 42 is left for the bridge's right
pink button. The gestures are the driver's clicks (/input/<gesture>/click: debounced, the pinch with hysteresis),
which the binding UI shows like any button; the points come from the skeleton.

In the VRChat and Resonite defaults, grab comes from /input/grab: the grip with the driver's tap to hold
(a press shorter than grab_tap_ms holds until the next press, a longer one grabs while held). Binding
/input/grip instead gives the plain button.

The VRChat binding emulates an Oculus Touch controller, without its render model, and binds VRChat's own menu
actions (vrchat_layout): stick press jump, menu button the action menu, black button the quick menu (left) and the
main menu (right), held the gesture toggle (left) and Safe Mode (right), right pink button mute. VRChat's driver
guide asks drivers not to emulate controllers: this is a choice of the default binding, which users can change. The
Resonite binding emulates an Oculus Touch controller, so Resonite always runs in its Touch mode with CyberFinger
(its only mode with a dash button), whatever the streamer; the driver hides the streamers' own emulated Touch
controllers (hide_other_hand_controllers), which Resonite would otherwise register instead or show as
trackers. It fills Resonite's Generic set as well (the hand pose comes from there in every mode), each set with
the skeletons: without them Resonite draws rigid canned hands. Re-run after an app update changes its manifest.
"""

import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "resources", "input")
HANDS = ("left", "right")
# App keys of Resonite builds that don't run as Steam app 2519830 (see resonite()); each needs an entry in
# default_bindings in cyberfinger_profile.json. SteamVR names a build it doesn't know after its renderer's
# executable; a build started from a non-Steam shortcut in Steam's VR library gets the shortcut's app id, which
# Steam assigns per install: steam.app.3454604410 is a private universe's shortcut on the author's PC.
RESONITE_OTHER_APP_KEYS = ("system.generated.renderite.renderer.exe", "steam.app.3454604410")


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(obj, rel):
    path = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    print("wrote", os.path.relpath(path, os.path.dirname(HERE)))


def header(app_key, name, description, options=None):
    return {
        "action_manifest_version": 0,
        "alias_info": {},
        "app_key": app_key,
        "bindings": {},
        "category": "steamvr_input",
        "controller_type": "cyberfinger",
        "description": description,
        "interaction_profile": "",
        "name": name,
        "options": options or {},
        "simulated_actions": [],
    }


def convert(binding, path_map, drop_output=lambda out: False):
    """Re-target a binding file onto CyberFinger components.

    path_map(path) returns the CyberFinger path, or None to drop the source. Touch inputs are removed
    (the CyberFinger has no capacitive sensors); sources left without inputs disappear, as do chords that
    reference a dropped component or a touch input.
    """
    out = {}
    for aset, sec in binding.get("bindings", {}).items():
        new = {}
        sources = []
        for src in sec.get("sources", []):
            path = path_map(src["path"])
            if path is None:
                continue
            inputs = {k: v for k, v in src.get("inputs", {}).items()
                      if k != "touch" and not drop_output(v.get("output", ""))}
            if not inputs and src.get("inputs"):
                continue
            s = copy.deepcopy(src)
            s["path"] = path
            s["inputs"] = inputs
            sources.append(s)
        if sources:
            new["sources"] = sources
        for kind in ("poses", "skeleton", "haptics"):
            items = []
            for item in sec.get(kind, []):
                path = path_map(item["path"])
                if path is not None and not drop_output(item.get("output", "")):
                    items.append(dict(item, path=path))
            if items:
                new[kind] = items
        chords = []
        for chord in sec.get("chords", []):
            if drop_output(chord.get("output", "")):
                continue
            ins = []
            for path, how in chord["inputs"]:
                mapped = path_map(path)
                if mapped is None or how == "touch":
                    ins = None
                    break
                ins.append([mapped, how])
            if ins:
                chords.append({"inputs": ins, "output": chord["output"]})
        if chords:
            new["chords"] = chords
        if new:
            out[aset] = new
    return out


def tap_to_hold_grab(bindings, outputs):
    """Bind these grab actions to /input/grab, the grip with the driver's tap to hold."""
    for sec in bindings.values():
        for src in sec.get("sources", []):
            if (src["path"].endswith("/input/grip") and src["mode"] == "button"
                    and src["inputs"].get("click", {}).get("output", "").lower() in outputs):
                src["path"] = src["path"][:-len("grip")] + "grab"
                src.pop("parameters", None)          # analog thresholds; /input/grab is a plain button
    return bindings


def touch_to_cyberfinger(path):
    """Oculus Touch component paths → CyberFinger (left X/Y and right A/B both become a/b)."""
    parts = path.split("/")          # ['', 'user', 'hand', side, 'input', component, ...]
    if len(parts) < 6 or parts[1:3] != ["user", "hand"]:
        return path
    comp = parts[5]
    comp = {"joystick": "thumbstick", "x": "a", "y": "b"}.get(comp, comp)
    if comp in ("thumbrest",):
        return None
    parts[5] = comp
    return "/".join(parts)


def index_to_cyberfinger(path):
    """Index controller paths → CyberFinger (identical names; no trackpad)."""
    parts = path.split("/")
    if len(parts) >= 6 and parts[1:3] == ["user", "hand"] and parts[5] in ("trackpad", "pinch"):
        return None
    return path


def vrchat_layout(bindings):
    """The CyberFinger layout for VRChat (v2), over the Touch-derived sets. VRChat doesn't take the CyberFinger for a
    Touch controller despite the emulation (its log: "can use Oculus Touch controller = False"), and runs it in its
    generic mode, which opens the menus from their own actions: Touch's Menu (Combined) and stick press (Udon Stick
    Click, only for worlds' scripts) do nothing there. So the menu button B opens the action menu, the left black
    button the quick menu, the right one the main menu, and held, the left toggles gestures and the right is Safe
    Mode (on Touch the four-button chord, dropped here). The stick press jumps (Touch's A). The right pink button is
    VRChat's mute, when the bridge passes it on (its right pink set to SteamVR); in its other modes the bridge mutes
    the Windows microphone itself, or fires a FluxAction, and VRChat never sees the button. The One-Hand set has no
    menu actions of its own and keeps B on the stick press. Tested in VRChat 2026.3.2."""
    for aset in ("/actions/global", "/actions/one_hand"):
        sec = bindings.get(aset)
        if not sec:
            continue
        kept = []
        for src in sec.get("sources", []):
            comp = src["path"].split("/")[-1]
            if comp in ("a", "b"):
                continue                               # re-bound below
            click = src.get("inputs", {}).get("click")
            if comp == "thumbstick" and click and click.get("output", "").endswith("/in/stick_click"):
                click["output"] = aset + "/in/jump"
            kept.append(src)

        def button(side, comp, action):
            kept.append({"path": f"/user/hand/{side}/input/{comp}", "mode": "button",
                         "inputs": {"click": {"output": f"{aset}/in/{action}"}}})

        for side in HANDS:
            button(side, "b", "action_menu" if aset == "/actions/global" else "stick_click")
        button("left", "a_hold", "gesture_toggle")
        button("right", "pink", "mic")
        if aset == "/actions/global":
            button("left", "a", "quick_menu")
            button("right", "a", "main_menu")
            button("right", "a_hold", "safe_mode")
            sec.pop("chords", None)                    # Safe Mode has its button
        sec["sources"] = kept
    return bindings


def vrchat(steam):
    src = os.path.join(steam, "steamapps", "common", "VRChat", "VRChat_Data", "StreamingAssets",
                       "SteamVR", "bindings_oculus_touch.json")
    touch = load(src)
    b = header("steam.app.438100", "CyberFinger defaults for VRChat",
               "Emulates an Oculus Touch controller, without its model. Stick press: jump. Menu button: action menu. "
               "Black button: quick menu (left), main menu (right); held: gesture toggle (left), Safe Mode (right). "
               "Right pink button: mute (with the bridge's right pink set to SteamVR). Grab: a quick tap of the grip "
               "holds until the next press, a longer press grabs while held. C/D/E and the hand gestures are left "
               "free for the user.",
               # No simulated render model: VRChat draws the controllers with its menus open, and a Touch controller
               # would show there next to the hand
               options={"simulated_controller_type": "oculus_touch", "simulate_rendermodel": "none"})
    b["bindings"] = convert(touch, touch_to_cyberfinger, drop_output=lambda o: "gesture" in o.lower())
    tap_to_hold_grab(b["bindings"], {"/actions/global/in/grab", "/actions/one_hand/in/grab"})
    vrchat_layout(b["bindings"])
    save(b, "bindings/steam.app.438100_cyberfinger.json")


def resonite():
    b = header("steam.app.2519830", "CyberFinger defaults for Resonite",
               "Emulates an Oculus Touch controller: Resonite's Touch mode, the one with a dash button. Black "
               "button (Start/Select): dash. MENU: context menu. Left pink button: SteamVR dashboard. Grab: a "
               "quick tap "
               "of the grip holds until the next press, a longer press grabs while held. Precision grab is "
               "implemented in Resonite from the skeleton. Flux Actions (MoreFluxActions mod), left/right: black "
               "button held 1/2, C 3/4, D 5/6, E 7/8, two-finger point 36/37, pinky pinch 38/39, index point "
               "40/41.",
               options={"simulated_controller_type": "oculus_touch", "simulate_rendermodel": "full"})
    sources = []
    for h in HANDS:
        sources += [
            {"path": f"/user/hand/{h}/input/trigger", "mode": "trigger",
             "inputs": {"click": {"output": "/actions/generic/in/actionprimary"},
                        "pull": {"output": "/actions/generic/in/strength"}}},
            {"path": f"/user/hand/{h}/input/thumbstick", "mode": "joystick",
             "inputs": {"click": {"output": "/actions/generic/in/actionsecondary"},
                        "position": {"output": "/actions/generic/in/axis"}}},
            {"path": f"/user/hand/{h}/input/grab", "mode": "button",
             "inputs": {"click": {"output": "/actions/generic/in/actiongrab"}}},
            {"path": f"/user/hand/{h}/input/b", "mode": "button",
             "inputs": {"click": {"output": "/actions/generic/in/actionmenu"}}},
        ]
    b["bindings"]["/actions/generic"] = {
        "sources": sources,
        "poses": [{"path": f"/user/hand/{h}/pose/raw", "output": "/actions/generic/in/pose"} for h in HANDS],
        "skeleton": [{"path": "/user/hand/left/input/skeleton/left", "output": "/actions/generic/in/lefthand"},
                     {"path": "/user/hand/right/input/skeleton/right", "output": "/actions/generic/in/righthand"}],
        "haptics": [{"path": f"/user/hand/{h}/output/haptic", "output": "/actions/generic/out/haptic"} for h in HANDS],
    }
    # Resonite's OculusTouch set: Resonite picks its controller mode from the render model of the devices it
    # registers as hands, and the emulation above shows CyberFinger to it as a Touch controller. Without the
    # skeletons here it draws rigid canned Touch hands. Its hand pose always comes from the Generic set's pose
    # (the Touch offset it applies to that is undone for the hand). The black button (A) opens the dash.
    touch = []
    for h in HANDS:
        touch += [
            {"path": f"/user/hand/{h}/input/trigger", "mode": "trigger",
             "inputs": {"click": {"output": "/actions/oculustouch/in/trigger_click"},
                        "pull": {"output": "/actions/oculustouch/in/trigger"}}},
            {"path": f"/user/hand/{h}/input/thumbstick", "mode": "joystick",
             "inputs": {"click": {"output": "/actions/oculustouch/in/joystick_click"},
                        "position": {"output": "/actions/oculustouch/in/joystick"}}},
            {"path": f"/user/hand/{h}/input/grab", "mode": "button",
             "inputs": {"click": {"output": "/actions/oculustouch/in/grip_click"}}},
            {"path": f"/user/hand/{h}/input/a", "mode": "button",
             "inputs": {"click": {"output": "/actions/oculustouch/in/button_xa"}}},
            {"path": f"/user/hand/{h}/input/b", "mode": "button",
             "inputs": {"click": {"output": "/actions/oculustouch/in/button_yb"}}},
        ]
    b["bindings"]["/actions/oculustouch"] = {
        "sources": touch,
        "poses": [{"path": f"/user/hand/{h}/pose/raw", "output": "/actions/oculustouch/in/pose"} for h in HANDS],
        "skeleton": [{"path": "/user/hand/left/input/skeleton/left", "output": "/actions/oculustouch/in/left_hand"},
                     {"path": "/user/hand/right/input/skeleton/right",
                      "output": "/actions/oculustouch/in/right_hand"}],
        "haptics": [{"path": f"/user/hand/{h}/output/haptic", "output": "/actions/oculustouch/out/haptic"}
                    for h in HANDS],
    }
    # Flux Actions, which the MoreFluxActions mod adds to Resonite's manifest for ProtoFlux on the avatar, on what
    # Resonite leaves free: FLUX_ACTIONS below, left then right. Without the mod this action set doesn't exist
    # (to check: that SteamVR still loads the rest of the binding then).
    sources = []
    for component, mode, input_name, first in FLUX_ACTIONS:
        for i, h in enumerate(HANDS):
            src = {"path": f"/user/hand/{h}/input/{component}", "mode": mode,
                   "inputs": {input_name: {"output": f"/actions/fluxactions/in/fluxaction{first + i}"}}}
            sources.append(src)
    # The right pink button (with the bridge's right pink set to SteamVR): FluxAction42, which the MoreFluxActions
    # mod's MuteToggleAction (42 by default) makes Resonite's mute (Resonite has no mute action of its own for SteamVR, and
    # ProtoFlux can't set a user's voice mode).
    sources.append({"path": "/user/hand/right/input/pink", "mode": "button",
                    "inputs": {"click": {"output": "/actions/fluxactions/in/fluxaction42"}}})
    b["bindings"]["/actions/fluxactions"] = {"sources": sources}
    save(b, "bindings/steam.app.2519830_cyberfinger.json")
    # The same layout for a Resonite build SteamVR doesn't know as Steam app 2519830, e.g. a private universe
    # started from a non-Steam shortcut: SteamVR names it after its renderer's executable.
    for key in RESONITE_OTHER_APP_KEYS:
        save(dict(b, app_key=key), f"bindings/{key}_cyberfinger.json")


# (component, binding mode, input, FluxAction for the left hand; the right hand gets the next one)
FLUX_ACTIONS = (
    ("a_hold", "button", "click", 1),              # the black button, held (the driver's black_hold_ms)
    ("c", "button", "click", 3),
    ("d", "button", "click", 5),
    ("e", "button", "click", 7),
    ("two_finger_point", "button", "click", 36),   # hand tracking: index and middle out, the thumb over the others
    ("pinky_pinch", "trigger", "click", 38),       # hand tracking: thumb-pinky pinch (the driver's click)
    ("index_point", "button", "click", 40),        # hand tracking: index finger pointing (the driver's click)
)


def compositor(steam):
    src = os.path.join(steam, "steamapps", "common", "SteamVR", "resources", "config",
                       "vrcompositor_bindings_knuckles.json")
    knuckles = load(src)
    b = header("openvr.component.vrcompositor", "CyberFinger SteamVR dashboard bindings",
               "Derived from SteamVR's Index dashboard binding. Trigger: click (a light press first locks the "
               "laser, so the click lands where it points); grip: right click; stick: scroll, push = middle "
               "click; B (MENU): back; A (black button): home. The left pink button toggles the dashboard (twice: "
               "room view); so does the system button, if a CyberFinger button is set to it.")
    b["bindings"] = convert(knuckles, index_to_cyberfinger)
    # The left pink button: the dashboard, as the system button would (click, twice: room view). Not its hold: held
    # 5-7 s it switches the CyberFinger off, which mustn't recenter or chord on the way. The right one is the apps'.
    b["bindings"]["/actions/system"]["sources"].append(
        {"path": "/user/hand/left/input/pink", "mode": "button",
         "inputs": {"click": {"output": "/actions/system/in/ToggleDashboard"},
                    "double": {"output": "/actions/system/in/ToggleRoomView"}}})
    # Index right-clicks with the trackpad, which the CyberFinger doesn't have: use the plain grip button
    # (/input/grip, not /input/grab: a right click must not latch).
    b["bindings"]["/actions/lasermouse"]["sources"] += [
        {"path": f"/user/hand/{h}/input/grip", "mode": "button",
         "inputs": {"click": {"output": "/actions/lasermouse/in/rightclick"}}} for h in HANDS]
    save(b, "bindings/vrcompositor_cyberfinger.json")


def legacy():
    b = header("", "CyberFinger legacy bindings (Index emulation)",
               "For apps using the legacy input API: presents CyberFinger as an Index controller.",
               options={"mirror_actions": False, "simulated_controller_type": "knuckles",
                        "simulate_rendermodel": True})
    del b["app_key"]
    sources = []
    for h in HANDS:
        H = h.capitalize()
        sources += [
            {"path": f"/user/hand/{h}/input/trigger", "mode": "trigger",
             "inputs": {"pull": {"output": f"/actions/legacy/in/{H}_Axis1_Value"}}},
            {"path": f"/user/hand/{h}/input/trigger", "mode": "button",
             "parameters": {"click_activate_threshold": "0.55", "click_deactivate_threshold": "0.5",
                            "haptic_amplitude": "0"},
             "inputs": {"click": {"output": f"/actions/legacy/in/{H}_Axis1_Press"}}},
            {"path": f"/user/hand/{h}/input/thumbstick", "mode": "joystick",
             "inputs": {"position": {"output": f"/actions/legacy/in/{H}_Axis0_Value"},
                        "click": {"output": f"/actions/legacy/in/{H}_Axis0_Press"}}},
            {"path": f"/user/hand/{h}/input/grip", "mode": "trigger",
             "inputs": {"pull": {"output": f"/actions/legacy/in/{H}_Axis2_Value1"}}},
            {"path": f"/user/hand/{h}/input/grip", "mode": "button",
             "inputs": {"click": {"output": f"/actions/legacy/in/{H}_Grip_Press"}}},
            {"path": f"/user/hand/{h}/input/a", "mode": "button",
             "inputs": {"click": {"output": f"/actions/legacy/in/{H}_A_Press"}}},
            {"path": f"/user/hand/{h}/input/b", "mode": "button",
             "inputs": {"click": {"output": f"/actions/legacy/in/{H}_ApplicationMenu_Press"}}},
            {"path": f"/user/hand/{h}/input/system", "mode": "button",
             "inputs": {"click": {"output": f"/actions/legacy/in/{H}_System_Press"}}},
        ]
    b["bindings"]["/actions/legacy"] = {
        "sources": sources,
        "poses": [{"path": f"/user/hand/{h}/pose/raw", "output": f"/actions/legacy/in/{h.capitalize()}_Pose"}
                  for h in HANDS],
        "haptics": [{"path": f"/user/hand/{h}/output/haptic", "output": f"/actions/legacy/out/{h.capitalize()}_Haptic"}
                    for h in HANDS],
    }
    save(b, "legacy_bindings_cyberfinger.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--steam", default=r"C:\Program Files (x86)\Steam", help="Steam install folder")
    args = ap.parse_args(argv)
    try:
        vrchat(args.steam)
        compositor(args.steam)
    except FileNotFoundError as e:
        print(f"missing source file: {e.filename}", file=sys.stderr)
        return 1
    resonite()
    legacy()
    from generate_cyberpunk_bindings import generate
    generate(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
