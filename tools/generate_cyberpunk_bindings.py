# SPDX-License-Identifier: GPL-3.0-only
"""Generate CyberpunkVRPort OpenXR bindings without reading an installed app.

    python tools/generate_cyberpunk_bindings.py
    python tools/generate_cyberpunk_bindings.py --app-key steam.app.YOUR_SHORTCUT_ID

Requires the experimental CyberFinger patch in CyberpunkVRPort, enabled at startup.
The optional --app-key also registers that launch alias in the driver profile.
This edits driver sources only; it never installs a driver or changes SteamVR settings.
"""

import argparse
import copy
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP_KEYS = ("system.generated.openxr.cyberpunkvrport.cyberpunk2077.exe",
            "system.generated.cyberpunk2077.exe", "steam.app.1091500")
ACTION_SET = "/actions/gameplay"


def binding(app_key, pink_pause=False):
    """Handedness is handled in the mod; sources always use physical hand roles."""
    sources, poses, haptics = [], [], []

    def source(hand, control, mode, **inputs):
        sources.append({
            "path": f"/user/hand/{hand}/input/{control}", "mode": mode,
            "inputs": {key: {"output": f"{ACTION_SET}/in/{action}"}
                       for key, action in inputs.items()},
        })

    for hand in ("left", "right"):
        source(hand, "trigger", "trigger", pull="trigger")
        source(hand, "thumbstick", "joystick", position="thumbstick",
               click="thumbstick_click" if hand == "left" else "primary_button")
        for pose, action in (("grip", "hand_pose"), ("tip", "hand_aim_pose")):
            poses.append({"path": f"/user/hand/{hand}/pose/{pose}",
                          "output": f"{ACTION_SET}/in/{action}"})
        haptics.append({"path": f"/user/hand/{hand}/output/haptic",
                        "output": f"{ACTION_SET}/out/haptic"})

    source("left", "grip", "button", click="cf_scanner")
    source("right", "grip", "button", click="thumbstick_click")
    source("left", "b", "button", click="cf_weapon")
    source("right", "b", "button", click="cf_inventory")
    source("left", "a", "button", click="primary_button")
    source("right", "a", "button", click="secondary_button")
    source("right", "pink", "button", click="cf_settings")
    source("right", "a_hold", "button", click="cf_heal")
    # Default: leave left pink entirely to the compositor. Holding black does
    # not also emit /input/a: the driver distinguishes short-release and hold.
    source("left", "pink" if pink_pause else "a_hold", "button", click="menu")
    return {
        "action_manifest_version": 0, "alias_info": {}, "app_key": app_key,
        "bindings": {ACTION_SET: {"sources": sources, "poses": poses, "haptics": haptics}},
        "category": "openxr", "controller_type": "cyberfinger",
        "description": "Requires CyberpunkVRPort CyberFinger patch (enabled=1). "
                       "Left Menu: weapon wheel; right Menu: inventory; both Menu hold: HUD. "
                       "Right pink: VR menu; F10 stays on the keyboard. "
                       "Black wrist on weapon hand: tap eject, hold pause; other black: tap reload, hold heal. Roles follow weapon handedness in the mod. " +
                       ("Left pink: pause; pair with the optional black-hold dashboard binding."
                        if pink_pause else "Default v2 wrist sources; left pink keeps SteamVR dashboard."),
        "interaction_profile": "/interaction_profiles/valve/index_controller",
        "name": "CyberFinger CyberpunkVRPort" + (" - pink pause" if pink_pause else ""),
        "options": {"mirror_actions": False}, "simulated_actions": [],
    }


def black_hold_dashboard(compositor):
    """Optional companion; deliberately not registered by default (global effect)."""
    result = copy.deepcopy(compositor)
    sources = result["bindings"]["/actions/system"]["sources"]
    found = False
    for source in sources:
        if source["path"] == "/user/hand/left/input/pink":
            source["path"] = "/user/hand/left/input/a_hold"
            # Two hold cycles is an unsuitable double click; leave room view
            # available through the existing system-button assignments.
            source["inputs"].pop("double", None)
            found = True
    if not found:
        raise ValueError("Dashboard binding has no left pink source to move")
    result["name"] = "CyberFinger dashboard - left black hold"
    result["description"] = ("Optional global companion to Cyberpunk's pink-pause layout. "
                             "Hold left black to open the dashboard; left pink is free for apps.")
    return result


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def generate(input_dir=None, extra_app_keys=()):
    input_dir = Path(input_dir) if input_dir is not None else ROOT / "resources/input"
    keys = tuple(dict.fromkeys(APP_KEYS + tuple(extra_app_keys)))
    for key in keys:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", key):
            raise ValueError("App key must contain only lowercase letters, digits, dot, underscore or hyphen")
    # Read dependencies before writing anything. No installed SteamVR files needed.
    compositor = json.loads((input_dir / "bindings/vrcompositor_cyberfinger.json").read_text(encoding="utf-8"))
    dashboard = black_hold_dashboard(compositor)
    profile_path = input_dir / "cyberfinger_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    for key in keys:
        for pink in (False, True):
            suffix = "_pink_pause" if pink else ""
            save(input_dir / f"bindings/{key}_cyberfinger{suffix}.json", binding(key, pink))
    save(input_dir / "bindings/vrcompositor_cyberfinger_black_hold.json", dashboard)
    # Default keys are checked in. Register an explicitly requested shortcut alias,
    # preserving all other apps and any existing choice for this key.
    changed = False
    for key in keys:
        if not any(entry["app_key"] == key for entry in profile["default_bindings"]):
            profile["default_bindings"].append({
                "app_key": key, "binding_url": f"bindings/{key}_cyberfinger.json"})
            changed = True
    if changed:
        save(profile_path, profile)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-key", action="append", default=[], help="Additional SteamVR launch alias")
    args = parser.parse_args()
    try:
        generate(extra_app_keys=args.app_key)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot generate Cyberpunk bindings: {error}\n")
    print("Generated CyberpunkVRPort bindings in resources/input/bindings (not installed).")


if __name__ == "__main__":
    main()
