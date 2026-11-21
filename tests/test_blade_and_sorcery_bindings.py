# SPDX-License-Identifier: GPL-3.0-only
"""Control-policy and generator regression checks (no SteamVR or game launch)."""
import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bas_bindings", ROOT / "tools/generate_blade_and_sorcery_bindings.py")
bas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bas)


@contextmanager
def scratch_directory():
    # Inherit the workspace ACL. Windows Python's private 0700 temp directories
    # can exclude the restricted process token used by build/test sandboxes.
    parent = (ROOT / "tests").resolve()
    path = parent / ("scratch-" + uuid.uuid4().hex)
    path.mkdir(mode=0o777)
    try:
        yield path
    finally:
        if path.resolve().parent != parent or path.is_symlink():
            raise RuntimeError("Refusing to remove a scratch directory outside the test folder")
        shutil.rmtree(path)


class BladeAndSorceryBindings(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "resources/input/cyberfinger_profile.json").read_text(encoding="utf-8"))
        # Independent contract from the installed action manifest. Generation
        # separately validates against the complete live actions.json.
        types = {
            "boolean": "grabgrip telekinesis clickui kick jump spellselector telekinesisspin alternateuse2 telekinesisrepel2 slowtime menu externalview screenshot externalviewlock",
            "vector1": "use cast telekinesispull gripaxis",
            "vector2": "move turn scrollui height",
            "pose": "pose", "vibration": "haptic",
            "skeleton": "skeletonlefthand skeletonrighthand",
        }
        self.manifest = {"action_sets": [{"name": bas.ACTION_SET}], "actions": []}
        for kind, names in types.items():
            for name in names.split():
                action = {"name": bas.ACTION_SET + ("/out/" if kind == "vibration" else "/in/") + name,
                          "type": kind}
                if kind == "skeleton":
                    action["skeleton"] = "/skeleton/hand/" + ("left" if name == "skeletonlefthand" else "right")
                self.manifest["actions"].append(action)

    def routes(self, fallback=False, dominant_hand="right"):
        return [(s["path"], v["output"].split("/")[-1])
                for s in bas.binding(fallback, dominant_hand)["bindings"][bas.ACTION_SET]["sources"]
                for v in s["inputs"].values()]

    def test_wrist_shortcuts_never_emit_combat_actions(self):
        allowed = {"menu", "externalview", "externalviewlock", "screenshot"}
        for path, action in self.routes():
            if path.split("/")[-1] in ("a", "a_hold", "pink"):
                self.assertIn(action, allowed)
        self.assertFalse(any(p == "/user/hand/left/input/pink" for p, _ in self.routes()))

    def test_remote_telekinesis_requires_physical_grip(self):
        for hand in ("left", "right"):
            self.assertIn((f"/user/hand/{hand}/input/grasp", "grabgrip"), self.routes())
        self.assertIn(("/user/hand/left/input/grip", "telekinesis"), self.routes())
        self.assertFalse(any(action == "telekinesis" and not path.endswith("/grip")
                             for path, action in self.routes()))
        self.assertFalse(any(path.endswith("/grab") or "pinch" in path for path, _ in self.routes()))

    def test_fallback_uses_only_physical_grab(self):
        for path, action in self.routes(True):
            if action in ("grabgrip", "gripaxis"):
                self.assertTrue(path.endswith("/grip"))
            self.assertNotIn("grasp", path)

    def test_manifest_and_profile_compatibility(self):
        for data in bas.variants().values():
            bas.validate(data, self.manifest, self.profile)

    def test_native_layout_has_no_duplicate_gameplay_ownership(self):
        layout = bas.native_binding()["bindings"][bas.ACTION_SET]
        actions = {v["output"].rsplit("/", 1)[-1] for s in layout["sources"] for v in s["inputs"].values()}
        self.assertEqual(actions, {"move", "turn", "kick", "jump", "height", "scrollui", "clickui",
                                   "menu", "externalview", "screenshot", "externalviewlock"})
        self.assertEqual(len(layout["poses"]), 2)
        self.assertEqual(len(layout["skeleton"]), 2)
        self.assertEqual(len(layout["haptics"]), 2)
        for s in layout["sources"]:
            self.assertNotIn(s["path"].split("/")[-1], {"grip", "b", "grasp", "grab", "pinch"})

    def test_native_keeps_symmetric_ui_triggers_and_same_physical_sticks(self):
        layout = bas.native_binding()["bindings"][bas.ACTION_SET]
        routes = {(s["path"], v["output"].rsplit("/", 1)[-1]) for s in layout["sources"] for v in s["inputs"].values()}
        for hand in ("left", "right"):
            self.assertIn((f"/user/hand/{hand}/input/trigger", "clickui"), routes)
        self.assertIn(("/user/hand/left/input/thumbstick", "move"), routes)
        self.assertIn(("/user/hand/right/input/thumbstick", "turn"), routes)
        self.assertIn(bas.NATIVE_FILENAME, bas.variants())

    def test_native_default_is_explicit_and_preserves_other_apps(self):
        original = json.dumps(self.profile, indent=2)
        default = bas.register_default(original)
        native = bas.select_native_default(default)
        expected = json.loads(default)
        next(e for e in expected["default_bindings"] if e["app_key"] == bas.APP_KEY)["binding_url"] = "bindings/" + bas.NATIVE_FILENAME
        self.assertEqual(json.loads(native), expected)
        self.assertEqual(bas.register_default(native), native)
        self.assertEqual(bas.select_native_default(native), native)

    def test_native_default_preserves_a_custom_binding(self):
        profile = copy.deepcopy(self.profile)
        profile["default_bindings"] = [{"app_key": bas.APP_KEY, "binding_url": "bindings/custom-personal.json"}]
        with self.assertRaises(ValueError):
            bas.select_native_default(json.dumps(profile))

    def test_quick_controls_follow_dominant_and_support_roles(self):
        for dominant, support in (("right", "left"), ("left", "right")):
            routes = self.routes(dominant_hand=dominant)
            for hand, key, actions in ((dominant, "grip", {"alternateuse2"}),
                                       (support, "grip", {"telekinesis"}),
                                       (dominant, "b", {"slowtime"}),
                                       (support, "b", {"spellselector", "telekinesisspin"})):
                self.assertEqual({a for p, a in routes if p == f"/user/hand/{hand}/input/{key}"}, actions)

    def test_triggers_symmetric_and_physical_inputs_stay_on_correct_hand(self):
        right = bas.binding()
        left = bas.binding(dominant_hand="left")
        for hand in ("left", "right"):
            wanted = {"use", "cast", "telekinesispull", "clickui"}
            for mode in ("left", "right"):
                routes = self.routes(dominant_hand=mode)
                self.assertEqual({a for p, a in routes if p == f"/user/hand/{hand}/input/trigger"}, wanted)
        for kind in ("poses", "skeleton", "haptics"):
            self.assertEqual(left["bindings"][bas.ACTION_SET][kind], right["bindings"][bas.ACTION_SET][kind])
        for component in ("thumbstick", "a", "a_hold", "pink"):
            self.assertEqual([(p, a) for p, a in self.routes() if p.endswith("/" + component)],
                             [(p, a) for p, a in self.routes(dominant_hand="left") if p.endswith("/" + component)])

    def test_no_core_action_needs_c_d_or_e(self):
        for data in bas.variants().values():
            for source in data["bindings"][bas.ACTION_SET]["sources"]:
                self.assertIn(source["path"].split("/")[-1],
                              {"trigger", "grip", "grasp", "b", "thumbstick", "a", "a_hold", "pink"})

    def test_fallback_does_not_fire_weapon_alt_use_while_grabbing(self):
        for hand in ("left", "right"):
            routes = self.routes(True, hand)
            self.assertFalse(any(action == "alternateuse2" for _, action in routes))
            self.assertEqual({p.split("/")[3] for p, a in routes if a == "telekinesis"}, {"left", "right"})

    def test_rejects_missing_action_or_wrong_type_before_writing(self):
        for mutation in ("missing", "type"):
            manifest = copy.deepcopy(self.manifest)
            if mutation == "missing":
                manifest["actions"] = [a for a in manifest["actions"] if not a["name"].endswith("/grabgrip")]
            else:
                manifest["actions"][0]["type"] = "vector1"
            with self.assertRaises(ValueError):
                bas.validate(bas.binding(), manifest, self.profile)

    def test_rejects_wrong_hand_skeleton(self):
        data = bas.binding()
        data["bindings"][bas.ACTION_SET]["skeleton"][0]["path"] = "/user/hand/right/input/skeleton/left"
        with self.assertRaises(ValueError):
            bas.validate(data, self.manifest, self.profile)

    def test_generation_preserves_other_apps_and_user_default(self):
        with scratch_directory() as temp:
            root = Path(temp)
            profile = copy.deepcopy(self.profile)
            profile["default_bindings"] = [e for e in profile["default_bindings"] if e["app_key"] != bas.APP_KEY]
            profile["default_bindings"].append({"app_key": bas.APP_KEY, "binding_url": "bindings/custom.json"})
            bas.save(root / "actions.json", self.manifest)
            bas.save(root / "input/cyberfinger_profile.json", profile)
            original = (root / "input/cyberfinger_profile.json").read_bytes()
            bas.generate(root, root / "input")
            bas.generate(root, root / "input")
            self.assertEqual(original, (root / "input/cyberfinger_profile.json").read_bytes())
            self.assertEqual(bas.binding(), json.loads((root / "input/bindings" / bas.FILENAME).read_text()))

    def test_generation_registers_only_one_game_default(self):
        with scratch_directory() as temp:
            root = Path(temp)
            profile = copy.deepcopy(self.profile)
            profile["default_bindings"] = [e for e in profile["default_bindings"] if e["app_key"] != bas.APP_KEY]
            before = copy.deepcopy(profile["default_bindings"])
            bas.save(root / "actions.json", self.manifest)
            bas.save(root / "input/cyberfinger_profile.json", profile)
            bas.generate(root, root / "input")
            bas.generate(root, root / "input")
            entries = json.loads((root / "input/cyberfinger_profile.json").read_text())["default_bindings"]
            self.assertEqual(entries[:-1], before)
            self.assertEqual(entries[-1], {"app_key": bas.APP_KEY, "binding_url": "bindings/" + bas.FILENAME})

    def test_registration_preserves_profile_formatting(self):
        original = '{\r\n  "points": [ 1, 2 ],\r\n  "default_bindings": [\r\n  ]\r\n}\r\n'
        updated = bas.register_default(original)
        self.assertIn('"points": [ 1, 2 ]', updated)
        self.assertEqual(updated.count("\n"), updated.count("\r\n"))
        self.assertEqual(bas.register_default(updated), updated)

    def test_left_default_generation_keeps_both_explicit_modes(self):
        with scratch_directory() as root:
            bas.save(root / "actions.json", self.manifest)
            bas.save(root / "input/cyberfinger_profile.json", self.profile)
            before = (root / "input/cyberfinger_profile.json").read_bytes()
            bas.generate(root, root / "input", dominant_hand="left")
            self.assertEqual(before, (root / "input/cyberfinger_profile.json").read_bytes())
            for name, expected in bas.variants("left").items():
                self.assertEqual(json.loads((root / "input/bindings" / name).read_text()), expected)
            with self.assertRaises(ValueError):
                bas.binding(dominant_hand="invalid")


if __name__ == "__main__":
    unittest.main()
