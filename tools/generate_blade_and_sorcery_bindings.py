# SPDX-License-Identifier: GPL-3.0-only
"""Generate CyberFinger bindings for the installed Blade & Sorcery action manifest.

python tools/generate_blade_and_sorcery_bindings.py --game "D:/SteamLibrary/steamapps/common/Blade & Sorcery"

Only reads the named game's actions.json and the driver's input profile. Writes
right-/left-handed gesture and buttons bindings. The stable default filename
uses --dominant-hand (right when omitted); explicit handed variants coexist.
Existing default choices, other apps, and SteamVR user settings are preserved.
The buttons layout is a fallback, never registered automatically.
"""

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP_KEY = "steam.app.629730"
ACTION_SET = "/actions/default"
FILENAME = f"{APP_KEY}_cyberfinger.json"
BUTTONS_FILENAME = f"{APP_KEY}_cyberfinger_buttons.json"
NATIVE_FILENAME = f"{APP_KEY}_cyberfinger_native.json"


def binding(buttons_only=False, dominant_hand="right"):
    if dominant_hand not in ("left", "right"):
        raise ValueError("Dominant hand must be left or right")
    support_hand = "left" if dominant_hand == "right" else "right"
    sources = []

    def source(hand, component, mode, parameters=None, **inputs):
        result = {
            "path": f"/user/hand/{hand}/input/{component}",
            "mode": mode,
            "inputs": {key: {"output": f"{ACTION_SET}/in/{value}"}
                       for key, value in inputs.items()},
        }
        if parameters:
            result["parameters"] = parameters
        sources.append(result)

    for hand in ("left", "right"):
        # Match the game's stock Index binding: position is per physical hand.
        source(hand, "thumbstick", "joystick",
               position="move" if hand == "left" else "turn",
               click="kick" if hand == "left" else "jump")
        source(hand, "thumbstick", "scroll", {"scroll_mode": "smooth"}, scroll="scrollui")
        for action in ("use", "cast", "telekinesispull"):
            source(hand, "trigger", "trigger", pull=action)
        source(hand, "trigger", "button", click="clickui")

        # Never use /input/grab: its tap-to-hold latch conflicts with opening
        # the hand to throw. Grasp is analog, so the button mode supplies a
        # Schmitt threshold, as in the driver's legacy trigger binding.
        grab = "grip" if buttons_only else "grasp"
        source(hand, grab, "button",
               {"click_activate_threshold": "0.70", "click_deactivate_threshold": "0.40",
                "haptic_amplitude": "0"}, click="grabgrip")
        source(hand, grab, "trigger", pull="gripaxis")
        # Gesture grab frees the physical grip for quick actions. The fallback
        # consumes both grip keys for ordinary grab/TK and omits alternate use
        # rather than firing it while acquiring an item.
        if buttons_only or hand == support_hand:
            source(hand, "grip", "button", click="telekinesis")
        else:
            source(hand, "grip", "button", click="alternateuse2")
        if hand == dominant_hand:
            source(hand, "b", "button", click="slowtime")
        else:
            source(hand, "b", "button", click="spellselector")
            source(hand, "b", "button", click="telekinesisspin")

    # Wrist controls: infrequent menu/camera shortcuts only. Left pink belongs
    # to the compositor. The bridge must pass right pink through to SteamVR.
    source("left", "a", "button", click="menu")
    source("right", "a", "button", click="externalview")
    source("right", "a_hold", "button", click="screenshot")
    source("right", "pink", "button", click="externalviewlock")
    source("right", "thumbstick", "joystick", position="height")

    return {
        "action_manifest_version": 0,
        "alias_info": {},
        "app_key": APP_KEY,
        "bindings": {ACTION_SET: {
            "sources": sources,
            "poses": [{"path": f"/user/hand/{hand}/pose/raw",
                       "output": f"{ACTION_SET}/in/pose"} for hand in ("left", "right")],
            "skeleton": [{"path": f"/user/hand/{hand}/input/skeleton/{hand}",
                          "output": f"{ACTION_SET}/in/skeleton{hand}hand"}
                         for hand in ("left", "right")],
            "haptics": [{"path": f"/user/hand/{hand}/output/haptic",
                         "output": f"{ACTION_SET}/out/haptic"} for hand in ("left", "right")],
        }},
        "category": "steamvr_input",
        "controller_type": "cyberfinger",
        "description": (
            "Blade & Sorcery stock actions; no mod required. "
            + ("Physical grip grabs and operates telekinesis. " if buttons_only else
               "Close/open fingers to grab/release. Dominant grip: weapon alternate use. Support grip: telekinesis. ")
            + "Sticks: move/kick, turn/jump. Trigger: use/cast/pull/UI. "
            f"Dominant hand: {dominant_hand}. Dominant MENU: slow time. Support MENU: spells/spin. "
            "No C/D/E required. Buttons fallback omits weapon alternate use. "
            "Left black: menu; right black: external view (hold: screenshot); "
            "right pink: camera lock (bridge must forward to SteamVR). "
            "Left pink keeps the dashboard. Experimental; headset validation required."
        ),
        "interaction_profile": "",
        "name": ("CyberFinger Blade & Sorcery - " + dominant_hand + "-handed - "
                 + ("buttons fallback" if buttons_only else "gesture grab")),
        "options": {"simulated_controller_type": "knuckles", "simulate_rendermodel": "none"},
        "simulated_actions": [],
    }


def native_binding():
    """Only locomotion/UI/camera and transport remain SteamVR-owned."""
    result = binding()
    allowed = {"move", "turn", "kick", "jump", "height", "scrollui", "clickui",
               "menu", "externalview", "screenshot", "externalviewlock"}
    layout = result["bindings"][ACTION_SET]
    layout["sources"] = [s for s in layout["sources"]
                         if all(v["output"].rsplit("/", 1)[-1] in allowed for v in s["inputs"].values())]
    result["name"] = "CyberFinger Blade & Sorcery - NATIVE neon gestures"
    result["description"] = (
        "REQUIRES CybrFngr Native mod and native-telemetry CyberFinger driver. "
        "Wrap/pinch contact grabs, catches, spell circuits and throw traces. "
        "Triggers use/cast/pull symmetrically. Grip: held-item alternate use or empty-hand TK. "
        "Dominant MENU: slow time; support MENU: spells. Dual-magic and left-handed modes in mod options. "
        "During TK, MENU spins; trigger+MENU repels. Sticks move/kick and turn/jump. "
        "Wrist black: left menu/right camera (hold screenshot); right pink camera lock; left pink dashboard. "
        "Open hands and release keys once after activation. Hardware validation required."
    )
    return result


def variants(dominant_hand="right"):
    result = {FILENAME: binding(False, dominant_hand), BUTTONS_FILENAME: binding(True, dominant_hand)}
    result[NATIVE_FILENAME] = native_binding()
    for hand in ("left", "right"):
        for fallback in (False, True):
            suffix = "_buttons" if fallback else ""
            result[f"{APP_KEY}_cyberfinger_{hand}{suffix}.json"] = binding(fallback, hand)
    return result


def validate(data, manifest, profile):
    """Reject unknown action names, action sets, input paths and value types."""
    actions = {a["name"].lower(): a for a in manifest["actions"]}
    sets = {s["name"].lower() for s in manifest["action_sets"]}
    components = profile["input_source"]
    value_types = {
        ("button", "click"): "boolean", ("trigger", "pull"): "vector1",
        ("joystick", "position"): "vector2", ("joystick", "click"): "boolean",
        ("scroll", "scroll"): "vector2",
    }
    mode_components = {"button": {"button", "trigger", "joystick"},
                       "trigger": {"trigger"}, "joystick": {"joystick"}, "scroll": {"joystick"}}

    def check_output(output, expected, aset):
        action = actions.get(output.lower())
        if not action or action["type"] != expected or not output.lower().startswith(aset + "/"):
            raise ValueError(f"Unknown or incompatible {expected} action: {output}")
        return action

    for aset, section in data["bindings"].items():
        if aset not in sets:
            raise ValueError(f"Unknown action set: {aset}")
        for kind, entries in section.items():
            for entry in entries:
                parts = entry["path"].split("/")
                if parts[:3] != ["", "user", "hand"] or parts[3] not in ("left", "right"):
                    raise ValueError(f"Invalid hand source: {entry['path']}")
                hand = parts[3]
                component = "/" + "/".join(parts[4:])
                spec = components.get(component)
                if spec is None or (spec.get("side") and spec["side"] != hand):
                    raise ValueError(f"Unknown or wrong-hand component: {entry['path']}")
                if kind == "sources":
                    mode = entry["mode"]
                    if spec["type"] not in mode_components.get(mode, set()):
                        raise ValueError(f"Incompatible mode {mode}: {component}")
                    for slot, target in entry["inputs"].items():
                        expected = value_types.get((mode, slot))
                        if expected is None:
                            raise ValueError(f"Unsupported binding mode/input: {mode}/{slot}")
                        # Button mode can threshold an analog trigger with no
                        # native click component (the grasp gesture).
                        if slot == "click" and not spec.get("click") and not (
                                spec.get("value") and entry.get("parameters", {}).get("click_activate_threshold")):
                            raise ValueError(f"No click or scalar threshold: {component}")
                        check_output(target["output"], expected, aset)
                else:
                    expected = {"poses": "pose", "skeleton": "skeleton", "haptics": "vibration"}[kind]
                    if spec["type"] != expected:
                        raise ValueError(f"Incompatible {kind} source: {component}")
                    action = check_output(entry["output"], expected, aset)
                    if kind == "skeleton" and action.get("skeleton") != spec["skeleton"]:
                        raise ValueError(f"Mismatched skeleton: {component}")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def register_default(profile_text):
    """Append only our entry, preserving the profile's existing formatting."""
    profile = json.loads(profile_text)
    if any(entry["app_key"] == APP_KEY for entry in profile["default_bindings"]):
        return profile_text
    match = re.search(r'(?m)^([ \t]*)"default_bindings"\s*:\s*\[', profile_text)
    if match is None:
        raise ValueError("Cannot locate the profile's default_bindings array")
    start = match.end() - 1
    entries, length = json.JSONDecoder().raw_decode(profile_text[start:])
    end = start + length - 1
    newline = "\r\n" if "\r\n" in profile_text else "\n"
    entry = {"app_key": APP_KEY, "binding_url": f"bindings/{FILENAME}"}
    block = newline.join(match[1] + "  " + line for line in json.dumps(entry, indent=2).splitlines())
    result = (profile_text[:end].rstrip() + ("," if entries else "") + newline + block
              + newline + match[1] + profile_text[end:])
    profile["default_bindings"].append(entry)
    if json.loads(result) != profile:
        raise ValueError("Profile update would change unrelated content")
    return result


def select_native_default(profile_text):
    """Explicit opt-in, replacing only our B&S entry without reformatting."""
    registered = register_default(profile_text)
    profile = json.loads(registered)
    entries = [e for e in profile["default_bindings"] if e["app_key"] == APP_KEY]
    if len(entries) != 1:
        raise ValueError("Expected one B&S app default")
    old = entries[0]["binding_url"]
    new = f"bindings/{NATIVE_FILENAME}"
    if old == new: return registered
    if Path(old).name not in variants():
        raise ValueError("B&S has a custom default; select Native in SteamVR explicitly")
    for match in re.finditer(r'\{[^{}]*\}', registered):
        try: entry = json.loads(match[0])
        except ValueError: continue
        if entry.get("app_key") == APP_KEY:
            replacement = match[0].replace(json.dumps(old), json.dumps(new), 1)
            result = registered[:match.start()] + replacement + registered[match.end():]
            entries[0]["binding_url"] = new
            if json.loads(result) != profile: raise ValueError("Native default would change unrelated profile fields")
            return result
    raise ValueError("Cannot locate B&S binding entry")


def generate(game, input_dir=None, dominant_hand="right", native_default=False):
    input_dir = Path(input_dir) if input_dir is not None else ROOT / "resources/input"
    manifest = json.loads((Path(game) / "actions.json").read_text(encoding="utf-8-sig"))
    profile_path = input_dir / "cyberfinger_profile.json"
    profile_bytes = profile_path.read_bytes()
    profile_text = profile_bytes.decode("utf-8-sig")
    profile = json.loads(profile_text)
    registered = select_native_default(profile_text) if native_default else register_default(profile_text)
    layouts = variants(dominant_hand)
    for data in layouts.values():
        validate(data, manifest, profile)
    for name, data in layouts.items():
        save(input_dir / "bindings" / name, data)
    if registered != profile_text:
        bom = b"\xef\xbb\xbf" if profile_bytes.startswith(b"\xef\xbb\xbf") else b""
        profile_path.write_bytes(bom + registered.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--game", type=Path, required=True, help="Blade & Sorcery install directory (read only)")
    parser.add_argument("--input-dir", type=Path, help="Driver resources/input directory; defaults to this checkout")
    parser.add_argument("--dominant-hand", choices=("left", "right"), default="right",
                        help="Handedness of the stable default filenames; explicit variants are always generated")
    parser.add_argument("--native-default", action="store_true", help="Opt in to the native mod layout; requires rebuilt telemetry driver and mod")
    args = parser.parse_args()
    try:
        generate(args.game, args.input_dir, args.dominant_hand, args.native_default)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot generate Blade & Sorcery bindings: {error}\n")
    print(f"Validated right-/left-handed B&S layouts; default files use {args.dominant_hand} hand. Existing app defaults preserved.")


if __name__ == "__main__":
    main()
