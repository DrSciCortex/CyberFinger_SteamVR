# SPDX-License-Identifier: GPL-3.0-only
"""Offline contract checks for the experimental CyberpunkVRPort bindings."""
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cyberpunk_bindings", ROOT / "tools/generate_cyberpunk_bindings.py")
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)
INPUT = ROOT / "resources/input"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CyberpunkBindings(unittest.TestCase):
    def test_app_aliases_ship_the_same_openxr_layout(self):
        self.assertIn("system.generated.openxr.cyberpunkvrport.cyberpunk2077.exe", cf.APP_KEYS)
        profile = read(INPUT / "cyberfinger_profile.json")
        for key in cf.APP_KEYS:
            entry, = [b for b in profile["default_bindings"] if b["app_key"] == key]
            data = read(INPUT / entry["binding_url"])
            self.assertEqual(data, cf.binding(key))
            self.assertEqual(data["category"], "openxr")
            self.assertEqual(data["interaction_profile"], "/interaction_profiles/valve/index_controller")

    def test_inputs_exist_and_output_types_match_mod_contract(self):
        inputs = read(INPUT / "cyberfinger_profile.json")["input_source"]
        action_types = {"trigger": "pull", "thumbstick": "position"}
        for name in ("thumbstick_click", "primary_button", "secondary_button", "menu",
                     "cf_weapon", "cf_inventory", "cf_scanner", "cf_heal", "cf_settings"):
            action_types[name] = "click"
        for pink in (False, True):
            gameplay = cf.binding(cf.APP_KEYS[0], pink)["bindings"][cf.ACTION_SET]
            seen = set()
            for source in gameplay["sources"]:
                control = "/" + source["path"].split("/", 4)[4]
                self.assertIn(control, inputs)
                self.assertNotIn(control, ("/input/grab", "/input/grasp"))
                for event, target in source["inputs"].items():
                    self.assertNotIn((source["path"], event), seen)
                    seen.add((source["path"], event))
                    self.assertEqual(action_types[target["output"].split("/")[-1]], event)
            self.assertNotIn("skeleton", gameplay)  # XR_EXT_hand_tracking is independent.
            self.assertEqual(len(gameplay["poses"]), 4)
            self.assertEqual(len(gameplay["haptics"]), 2)
            for category in ("poses", "haptics"):
                for item in gameplay[category]:
                    self.assertIn("/" + item["path"].split("/", 4)[4], inputs)

    def test_grip_utilities_and_menus_are_independent_of_finger_grab(self):
        sources = cf.binding(cf.APP_KEYS[0])["bindings"][cf.ACTION_SET]["sources"]
        buttons = {s["path"]: s["inputs"]["click"]["output"].split("/")[-1]
                   for s in sources if "click" in s["inputs"]}
        self.assertEqual(buttons["/user/hand/left/input/grip"], "cf_scanner")
        self.assertEqual(buttons["/user/hand/right/input/grip"], "thumbstick_click")
        self.assertEqual(buttons["/user/hand/left/input/b"], "cf_weapon")
        self.assertEqual(buttons["/user/hand/right/input/b"], "cf_inventory")
        self.assertEqual(buttons["/user/hand/right/input/thumbstick"], "primary_button")
        self.assertEqual(buttons["/user/hand/right/input/pink"], "cf_settings")
        self.assertEqual(buttons["/user/hand/right/input/a_hold"], "cf_heal")

    def test_pause_and_dashboard_never_share_a_source_in_paired_layouts(self):
        original = read(INPUT / "bindings/vrcompositor_cyberfinger.json")
        for pink in (False, True):
            dashboard = cf.black_hold_dashboard(original) if pink else original
            system_paths = {s["path"] for s in dashboard["bindings"]["/actions/system"]["sources"]
                            if any(v["output"].lower().endswith(("/toggledashboard", "/toggleroomview"))
                                   for v in s["inputs"].values())}
            game = cf.binding(cf.APP_KEYS[0], pink)["bindings"][cf.ACTION_SET]["sources"]
            self.assertFalse(system_paths.intersection(s["path"] for s in game))
        self.assertEqual(original, read(INPUT / "bindings/vrcompositor_cyberfinger.json"))

    def test_generation_is_idempotent_and_preserves_other_bindings(self):
        # Keep temporary files inside this checkout, with no installed-app reads.
        output = (ROOT / "out").resolve()
        output.mkdir(exist_ok=True)
        target = output / ("cyberpunk-test-" + uuid.uuid4().hex)
        target.mkdir()
        try:
            (target / "bindings").mkdir()
            for rel in ("cyberfinger_profile.json", "bindings/vrcompositor_cyberfinger.json"):
                shutil.copyfile(INPUT / rel, target / rel)
            before = read(target / "cyberfinger_profile.json")["default_bindings"]
            cf.generate(target, ("steam.app.1234567890",))
            first = {p.relative_to(target): p.read_bytes() for p in target.rglob("*.json")}
            cf.generate(target, ("steam.app.1234567890",))
            self.assertEqual(first, {p.relative_to(target): p.read_bytes() for p in target.rglob("*.json")})
            entries = read(target / "cyberfinger_profile.json")["default_bindings"]
            self.assertEqual(entries[:-1], before)
            self.assertEqual(entries[-1]["app_key"], "steam.app.1234567890")
            self.assertEqual((target / "bindings/vrcompositor_cyberfinger.json").read_bytes(),
                             (INPUT / "bindings/vrcompositor_cyberfinger.json").read_bytes())
            for bad in ("../outside", "C:/outside", "bad/key"):
                with self.assertRaises(ValueError):
                    cf.generate(target, (bad,))
        finally:
            if target.resolve().parent != output or not target.name.startswith("cyberpunk-test-"):
                raise RuntimeError("Test cleanup escaped its output directory")
            shutil.rmtree(target)


if __name__ == "__main__":
    unittest.main()
