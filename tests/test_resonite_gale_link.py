# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""bridge_linux/resonite_gale_link.py on a made-up build and Gale profile: what linking puts into the build, that
unlinking takes all of it out again, and what it refuses."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("resonite_gale_link",
                                              os.path.join(ROOT, "bridge_linux", "resonite_gale_link.py"))
link = importlib.util.module_from_spec(spec)
spec.loader.exec_module(link)


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def tree(root):
    """Every file and link under root, with a file's content."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                out[rel] = "-> " + os.readlink(path)
            elif os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    out[rel] = f.read()
    return out


@unittest.skipIf(sys.platform == "win32", "the Linux script (Windows has resonite_gale_link.ps1)")
class GaleLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.game = os.path.join(self.tmp.name, "games", "Some Universe-1.2.3.456")
        self.profile = os.path.join(self.tmp.name, "gale", "resonite", "profiles", "Some Universe")
        # A build as unpacked: the engine, the renderer, its own bootstrap.
        write(os.path.join(self.game, "Renderite.Host.dll"))
        write(os.path.join(self.game, "Renderer", "Renderite.Renderer.exe"))
        write(os.path.join(self.game, "LinuxBootstrap.sh"), "the build's own\n")
        # A profile with BepisLoader, BepInExRenderer, RenderiteHook and ResoniteModLoaderLoader.
        for f in link.ENGINE_FILES:
            write(os.path.join(self.profile, f), f"the loader's {f}\n")
        write(os.path.join(self.profile, "hookfxr.ini"), "[hookfxr]\nenable=false\ntarget_assembly=Other.dll\n")
        write(os.path.join(self.profile, "BepInEx", "core", "BepInEx.Core.dll"))
        write(os.path.join(self.profile, "Renderer", "BepInEx", "core", "BepInEx.Preloader.dll"))
        hook = os.path.join(self.profile, "BepInEx", "plugins", "ResoniteModding-RenderiteHook", "Doorstop")
        write(os.path.join(hook, "winhttp.dll"), "doorstop\n")
        write(os.path.join(hook, "doorstop_config.ini"), "[General]\nenabled=false\ntarget_assembly=x\n")
        write(os.path.join(self.profile, "BepInEx", "plugins", "DevL0rd-ResoniteModLoaderLoader", "manifest.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_quiet(self, fn, *args):
        link.errors = 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = fn(*args)
        return ok, out.getvalue()

    def test_link_and_unlink(self):
        before_game, before_profile = tree(self.game), tree(self.profile)
        ok, out = self.run_quiet(link.link, self.game, self.profile, False)
        self.assertTrue(ok, out)
        g = self.game
        self.assertEqual(os.path.realpath(os.path.join(g, "BepInEx")), os.path.join(self.profile, "BepInEx"))
        self.assertEqual(os.path.realpath(os.path.join(g, "Renderer", "BepInEx")),
                         os.path.join(self.profile, "Renderer", "BepInEx"))
        self.assertEqual(link.ini_value(os.path.join(g, "hookfxr.ini"), "enable"), "true")
        self.assertEqual(link.ini_value(os.path.join(g, "hookfxr.ini"), "target_assembly"), "BepisLoader.dll")
        self.assertEqual(link.ini_value(os.path.join(g, "Renderer", "doorstop_config.ini"), "enabled"), "true")
        self.assertEqual(link.ini_value(os.path.join(g, "Renderer", "doorstop_config.ini"), "target_assembly"),
                         r"BepInEx\core\BepInEx.Preloader.dll")
        self.assertEqual(tree(g)["LinuxBootstrap.sh"], "the loader's LinuxBootstrap.sh\n")
        self.assertEqual(tree(g)["LinuxBootstrap.sh.pre-gale-link"], "the build's own\n")
        self.assertEqual(link.ini_value(os.path.join(self.profile, "hookfxr.ini"), "enable"), "true")
        self.assertNotIn("ResoniteModLoaderLoader", out)        # it's there: nothing to warn about

        # Again: nothing piles up; and the status agrees.
        record = tree(g)[link.RECORD]
        ok, _ = self.run_quiet(link.link, self.game, self.profile, False)
        self.assertTrue(ok)
        self.assertEqual(tree(g)[link.RECORD], record)
        ok, out = self.run_quiet(link.status, self.game)
        self.assertEqual(out.count("  OK  "), 2, out)

        ok, out = self.run_quiet(link.unlink, self.game)
        self.assertTrue(ok, out)
        self.assertEqual(tree(self.game), before_game)
        self.assertEqual(tree(self.profile), before_profile)

    def test_plugins_of_its_own_are_not_orphaned(self):
        write(os.path.join(self.game, "BepInEx", "plugins", "Mine", "Mine.dll"))
        ok, out = self.run_quiet(link.link, self.game, self.profile, False)
        self.assertFalse(ok)
        self.assertIn("plugin file(s) of its own", out)
        self.assertFalse(os.path.islink(os.path.join(self.game, "BepInEx")))
        ok, out = self.run_quiet(link.link, self.game, self.profile, True)      # --force: moved aside
        self.assertTrue(ok, out)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "BepInEx.pre-gale-link", "plugins", "Mine", "Mine.dll")))
        self.run_quiet(link.unlink, self.game)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "BepInEx", "plugins", "Mine", "Mine.dll")))

    def test_profile_without_loaders(self):
        os.remove(os.path.join(self.profile, "BepisLoader.dll"))
        os.remove(os.path.join(self.profile, "Renderer", "BepInEx", "core", "BepInEx.Preloader.dll"))
        ok, out = self.run_quiet(link.link, self.game, self.profile, False)
        self.assertFalse(ok)
        self.assertIn("Nothing to link", out)
        self.assertFalse(os.path.exists(os.path.join(self.game, link.RECORD)))

    def test_disabled_loader_loader_and_names(self):
        rmll = os.path.join(self.profile, "BepInEx", "plugins", "DevL0rd-ResoniteModLoaderLoader")
        self.assertTrue(link.profile_info(self.profile)["rmll"])
        os.rename(os.path.join(rmll, "manifest.json"), os.path.join(rmll, "manifest.json.old"))   # as Gale disables
        self.assertFalse(link.profile_info(self.profile)["rmll"])
        self.assertTrue(link.is_build(self.game))
        self.assertFalse(link.is_build(self.profile))
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp.name
        try:
            os.rename(os.path.join(self.tmp.name, "gale"), os.path.join(self.tmp.name, "com.kesomannen.gale"))
            found = link.find_profile(None, self.game)                # named like the build, without its version
            self.assertEqual(os.path.basename(found), "Some Universe")
            self.assertTrue(os.path.isdir(found))
        finally:
            if old is None:
                del os.environ["XDG_DATA_HOME"]
            else:
                os.environ["XDG_DATA_HOME"] = old


if __name__ == "__main__":
    unittest.main()
