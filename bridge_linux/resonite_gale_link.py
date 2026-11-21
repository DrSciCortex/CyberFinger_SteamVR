#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Makes a Resonite build load a Gale profile's mods however it is started, on Linux: what
tools/resonite_gale_link.ps1 does on Windows (README, "Loading Gale mods without Gale's launcher").

    resonite_gale_link.py                             Steam's Resonite, to Gale's Default profile
    resonite_gale_link.py --game ~/Games/YourPrivateUniverse-1.2.3.456 [--profile YourPrivateUniverse]
    resonite_gale_link.py [--game ...] --status
    resonite_gale_link.py [--game ...] --unlink

Gale starts Resonite with arguments that tell BepInEx where the profile is. Linking puts that into the build's
folder instead, so Steam, a script or a Proton command line start it with its mods:

  <build>/hookfxr.ini                     enable=true: the engine-side loader (BepisLoader) runs on every start
  <build>/LinuxBootstrap.sh               BepisLoader's, from the profile: the build's own starts the engine without
                                          looking at hookfxr.ini
  <build>/hostfxr.dll, BepisLoader.*      that loader, copied from the profile
  <build>/BepInEx                         a symlink to <profile>/BepInEx (the engine-side plugins)
  <build>/Renderer/doorstop_config.ini    enabled=true: the renderer-side loader (doorstop) runs on every start
  <build>/Renderer/winhttp.dll            that loader, from the profile's RenderiteHook
  <build>/Renderer/BepInEx                a symlink to <profile>/Renderer/BepInEx (the renderer-side plugins)
  <profile>/hookfxr.ini                   enable=true as well: Gale copies it into the build when it launches

Files and folders the build had are kept as *.pre-gale-link. <build>/CyberFinger-GaleLink.json records every
change, and --unlink undoes them. The profile needs BepisLoader, BepInExRenderer and RenderiteHook, from Gale.

The renderer is a Windows program, run by Proton: its loader is a winhttp.dll beside it, which Wine only loads
when told to, with WINEDLLOVERRIDES="winhttp=n,b" in the environment the build starts in.
"""

import argparse
import filecmp
import glob
import json
import os
import re
import shutil
import sys
import time

RECORD = "CyberFinger-GaleLink.json"
ENGINE_FILES = ("hostfxr.dll", "BepisLoader.dll", "BepisLoader.deps.json", "BepisLoader.runtimeconfig.json",
                "LinuxBootstrap.sh")
errors = 0


def say(kind, text):
    global errors
    errors += kind == "err"
    print({"ok": "  OK     ", "warn": "  WARN   ", "err": "  ERROR  ", "next": "  NEXT   ", "info": "         ",
           "head": ""}[kind] + text, flush=True)


def is_build(path):
    """A Resonite build's folder: the engine and the renderer."""
    engine = any(os.path.isfile(os.path.join(path, f)) for f in ("Renderite.Host.dll", "Renderite.Host.exe"))
    return engine and os.path.isfile(os.path.join(path, "Renderer", "Renderite.Renderer.exe"))


def steam_resonite():
    """Steam's Resonite, in the usual places of Steam's library."""
    for steam in (".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
        path = os.path.join(os.path.expanduser("~"), steam, "steamapps", "common", "Resonite")
        if is_build(path):
            return path
    return None


def rml_launch_options(game):
    """Steam launch options that load ResoniteModLoader themselves (-LoadAssembly ...ResoniteModLoader.dll), for a
    build in Steam's library: with ResoniteModLoaderLoader in the profile too, RML loads twice and Resonite stops
    as it starts."""
    if os.sep + "steamapps" + os.sep not in game:
        return []
    steam = game.split(os.sep + "steamapps" + os.sep)[0]
    hits = []
    for config in glob.glob(os.path.join(glob.escape(steam), "userdata", "*", "config", "localconfig.vdf")):
        try:
            with open(config, encoding="utf-8", errors="replace") as f:
                hits += [line.strip() for line in f if '"LaunchOptions"' in line and "ResoniteModLoader.dll" in line]
        except OSError:
            pass
    return hits


def gale_dir():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data, "com.kesomannen.gale")


def find_profile(given, game):
    """The Gale profile's folder: as given (a folder, or a name), else the one named like the build (without its
    version), else Default."""
    profiles = os.path.join(gale_dir(), "resonite", "profiles")
    if given:
        return given if os.path.isdir(given) else os.path.join(profiles, given)
    name = re.sub(r"-\d+(\.\d+)+$", "", os.path.basename(game))
    for candidate in (name, "Default"):
        if os.path.isdir(os.path.join(profiles, candidate)):
            return os.path.join(profiles, candidate)
    return os.path.join(profiles, name)


def profile_info(profile):
    """What a profile supplies: the engine side (BepisLoader), the renderer side (BepInExRenderer), and the
    renderer's loader (RenderiteHook carries it; Gale's download cache as a fallback)."""
    missing = [f for f in ENGINE_FILES + ("hookfxr.ini",) if not os.path.isfile(os.path.join(profile, f))]
    engine = os.path.isdir(os.path.join(profile, "BepInEx", "core")) and not missing
    renderer = os.path.isfile(os.path.join(profile, "Renderer", "BepInEx", "core", "BepInEx.Preloader.dll"))
    doorstop = None
    for root in (os.path.join(profile, "BepInEx", "plugins"), os.path.join(gale_dir(), "cache", "ResoniteModding-RenderiteHook")):
        hits = [h for h in glob.glob(os.path.join(glob.escape(root), "**", "Doorstop", "winhttp.dll"), recursive=True)
                if os.path.isfile(os.path.join(os.path.dirname(h), "doorstop_config.ini"))]
        if hits and not doorstop:
            doorstop = os.path.dirname(max(hits, key=os.path.getmtime))
    # Gale disables a mod by renaming its files to *.old
    rmll = [d for d in glob.glob(os.path.join(glob.escape(profile), "BepInEx", "plugins", "*ResoniteModLoaderLoader*"))
            if not os.path.isfile(os.path.join(d, "manifest.json.old"))]
    return {"engine": engine, "missing": missing, "renderer": renderer, "doorstop": doorstop, "rmll": bool(rmll)}


def microsoft_hostfxr(path):
    """Is this hostfxr.dll .NET's own (the build carries its runtime), not BepisLoader's stand-in?"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    at = data.find("CompanyName".encode("utf-16-le"))
    return at >= 0 and "Microsoft".encode("utf-16-le") in data[at:at + 120]


def running(game):
    """Processes started from the build's folder."""
    names = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if int(pid) != os.getpid() and (cwd == game or cwd.startswith(game + os.sep)) and "resonite_gale_link" not in cmd:
            names.append(cmd.split(" ")[0].rsplit("/", 1)[-1] or pid)
    return sorted(set(names))


# -- changes to the build, recorded so that --unlink can undo them ---------------

def load_record(game):
    try:
        with open(os.path.join(game, RECORD), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_record(game, rec):
    with open(os.path.join(game, RECORD), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)


def move_aside(path, rec):
    aside = path + ".pre-gale-link"
    if os.path.lexists(aside):
        aside += time.strftime("-%Y%m%d%H%M%S")
    os.rename(path, aside)
    rec["backups"].append({"path": path, "backup": aside})
    return aside


def set_link(link, target, rec, force):
    """Points <link> at <target>. A real folder in the way, holding no plugins (BepInEx's own core and config
    only), is moved aside; one with plugins only with --force, as they would stop loading."""
    if os.path.islink(link):
        if os.path.realpath(link) == os.path.realpath(target):
            if link not in rec["links"]:
                rec["links"].append(link)
            return True
        os.unlink(link)
    elif os.path.lexists(link):
        plugins = [f for _, _, files in os.walk(os.path.join(link, "plugins")) for f in files]
        if plugins and not force:
            say("err", f"{link} holds {len(plugins)} plugin file(s) of its own; linking would stop them loading.")
            say("next", "Move those plugins into the Gale profile (or remove them), then run this again; or run "
                        "again with --force to move the folder aside.")
            return False
        say("info", f"Moved the build's own {os.path.basename(link)} folder aside: {move_aside(link, rec)}")
    os.symlink(target, link, target_is_directory=True)
    if link not in rec["links"]:
        rec["links"].append(link)
    return True


def copy_in(source, dest, rec):
    """Copies a file in. A file the build had that differs is kept as *.pre-gale-link; an identical one is left
    alone."""
    if os.path.lexists(dest) and dest not in rec["files"]:
        if os.path.isfile(dest) and filecmp.cmp(source, dest, shallow=False):
            return
        backup = dest + ".pre-gale-link"
        if not os.path.lexists(backup):
            shutil.copy2(dest, backup)
            rec["backups"].append({"path": dest, "backup": backup})
    if os.path.lexists(dest):
        os.unlink(dest)                        # never write through a hard link (Gale's cache shares its files)
    shutil.copy2(source, dest)
    if dest not in rec["files"]:
        rec["files"].append(dest)


def ini_text(path, values):
    """The ini file with some keys set, its line endings kept."""
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = [next((f"{k}={v}" for k, v in values.items() if re.match(rf"\s*{re.escape(k)}\s*=", line)), line)
             for line in text.splitlines()]
    return eol.join(lines) + eol


def write_ini(source, dest, values, rec):
    tmp = dest + ".new"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(ini_text(source, values))
    copy_in(tmp, dest, rec)
    os.unlink(tmp)


def ini_value(path, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                found = re.match(rf"\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
                if found:
                    return found.group(1)
    except OSError:
        pass
    return None


def set_ini_value(path, key, value):
    """Sets one key (no backup): the profile's hookfxr.ini. Written as a new file, as copy_in does."""
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(ini_text(path, {key: value}))
    os.replace(tmp, path)


def link(game, profile, force):
    info = profile_info(profile)
    say("head", f"\nLinking {os.path.basename(game)}  ({game})")
    say("head", f"  to Gale profile '{os.path.basename(profile)}'  ({profile})")
    busy = running(game)
    if busy:
        say("err", f"It is running ({', '.join(busy)}).")
        say("next", "Close it, then run this again.")
        return False
    want_engine = info["engine"]
    if want_engine and microsoft_hostfxr(os.path.join(game, "hostfxr.dll")):
        want_engine = False
        say("warn", "The build carries its own .NET runtime (a hostfxr.dll by Microsoft), which BepisLoader cannot "
                    "load in front of: engine-side mods will not load.")
    want_renderer = info["renderer"] and bool(info["doorstop"])
    name = os.path.basename(profile)
    if not info["engine"]:
        say("warn", f"Profile '{name}' has no BepisLoader"
                    + (f" (missing: {', '.join(info['missing'])})" if info["missing"] else "")
                    + ": engine-side mods will not load. In Gale, install BepisLoader into it.")
    if not info["renderer"]:
        say("warn", f"Profile '{name}' has no BepInExRenderer: renderer-side mods (SteamVRRoleFix) will not load. "
                    "In Gale, install BepInExRenderer into it.")
    elif not info["doorstop"]:
        say("warn", f"Profile '{name}' has no RenderiteHook, which carries the renderer's loader: renderer-side "
                    "mods will not load. In Gale, install RenderiteHook into it.")
    if not want_engine and not want_renderer:
        say("err", "Nothing to link: the profile has neither loader.")
        say("next", f"In Gale, select profile '{name}' and install BepisLoader, BepInExRenderer and RenderiteHook "
                    "(or copy a working profile), then run this again.")
        return False

    old = load_record(game) or {}
    rec = {"mode": "gale", "profile": profile, "engine": want_engine, "renderer": want_renderer,
           "links": old.get("links", []), "files": old.get("files", []), "backups": old.get("backups", []),
           "iniEdits": old.get("iniEdits", [])}
    if old.get("profile") and old["profile"] != profile:
        say("info", f"It was linked to {old['profile']}: relinking.")
    try:
        if want_engine:
            for f in ENGINE_FILES:
                copy_in(os.path.join(profile, f), os.path.join(game, f), rec)
            write_ini(os.path.join(profile, "hookfxr.ini"), os.path.join(game, "hookfxr.ini"),
                      {"enable": "true", "target_assembly": "BepisLoader.dll"}, rec)
            # Gale copies the profile's hookfxr.ini into the game folder when it launches: switch it on there too.
            pini = os.path.join(profile, "hookfxr.ini")
            was = ini_value(pini, "enable")
            if was != "true":
                if not any(e["file"] == pini for e in rec["iniEdits"]):
                    rec["iniEdits"].append({"file": pini, "key": "enable", "old": was})
                set_ini_value(pini, "enable", "true")
            if not set_link(os.path.join(game, "BepInEx"), os.path.join(profile, "BepInEx"), rec, force):
                save_record(game, rec)
                return False
            say("ok", "Engine side: hookfxr.ini enable=true, BepisLoader's LinuxBootstrap.sh, BepInEx -> the "
                      "profile's BepInEx (BepisLoader plugins)")
        if want_renderer:
            r = os.path.join(game, "Renderer")
            for f in ("winhttp.dll", ".doorstop_version"):
                if os.path.isfile(os.path.join(info["doorstop"], f)):
                    copy_in(os.path.join(info["doorstop"], f), os.path.join(r, f), rec)
            write_ini(os.path.join(info["doorstop"], "doorstop_config.ini"), os.path.join(r, "doorstop_config.ini"),
                      {"enabled": "true", "target_assembly": r"BepInEx\core\BepInEx.Preloader.dll"}, rec)
            if not set_link(os.path.join(r, "BepInEx"), os.path.join(profile, "Renderer", "BepInEx"), rec, force):
                save_record(game, rec)
                return False
            say("ok", "Renderer side: Renderer/doorstop_config.ini enabled=true, Renderer/BepInEx -> the profile's "
                      "Renderer/BepInEx")
    except OSError as e:
        say("err", f"Linking failed: {e}")
        say("next", f"Check that nothing from {game} is running and that you can write there, then run this again "
                    "(or --unlink to undo what was done).")
        save_record(game, rec)
        return False
    save_record(game, rec)
    if not info["rmll"] and os.path.isfile(os.path.join(game, "Libraries", "ResoniteModLoader.dll")):
        say("warn", f"Profile '{name}' has no ResoniteModLoaderLoader enabled: ResoniteModLoader mods (rml_mods) "
                    "load only if the build is started with -LoadAssembly Libraries/ResoniteModLoader.dll. Enable "
                    "it in Gale, and drop that argument.")
    elif info["rmll"] and want_engine and rml_launch_options(game):
        say("warn", "Steam's launch options load ResoniteModLoader themselves (-LoadAssembly "
                    "...ResoniteModLoader.dll), and the profile's ResoniteModLoaderLoader will too: loaded twice, "
                    "Resonite stops as it starts.")
        say("next", "In Steam, Resonite's Properties, remove -LoadAssembly Libraries/ResoniteModLoader.dll from "
                    "the launch options (or disable ResoniteModLoaderLoader in Gale).")
    say("ok", f"Linked. Every change is recorded in {os.path.join(game, RECORD)}.")
    say("next", f"Start {os.path.basename(game)} as usual: it loads Gale profile '{name}'.")
    if want_renderer:
        say("next", 'The renderer\'s loader needs WINEDLLOVERRIDES="winhttp=n,b" in the environment it starts in '
                    '(in Steam: the launch options WINEDLLOVERRIDES="winhttp=n,b" %command%).')
    say("next", f"Logs: {profile}/BepInEx/LogOutput.log (engine side), {profile}/Renderer/BepInEx/LogOutput.log "
                "(renderer side). --unlink undoes the link.")
    return True


def unlink(game):
    say("head", f"\nUnlinking {os.path.basename(game)}  ({game})")
    rec = load_record(game)
    if not rec:
        say("ok", "Nothing to undo (no record of a link here).")
        return True
    busy = running(game)
    if busy:
        say("err", f"It is running ({', '.join(busy)}).")
        say("next", "Close it, then run this again.")
        return False
    for path in rec.get("links", []):
        if os.path.islink(path):
            os.unlink(path)
    for path in rec.get("files", []):
        if os.path.isfile(path) or os.path.islink(path):
            os.unlink(path)
    for b in rec.get("backups", []):
        if os.path.lexists(b["backup"]) and not os.path.lexists(b["path"]):
            os.rename(b["backup"], b["path"])
    for e in rec.get("iniEdits", []):
        if os.path.isfile(e["file"]) and e.get("old") is not None:
            set_ini_value(e["file"], e["key"], e["old"])
    os.unlink(os.path.join(game, RECORD))
    say("ok", f"Undone: {os.path.basename(game)} is as it was before, and loads no mods through this link (the Gale "
              "profile keeps its mods).")
    return True


def status(game):
    rec = load_record(game)
    say("head", f"\n{os.path.basename(game)}  ({game})")
    if not rec:
        say("info", "Not linked to a Gale profile by this script.")
        return True
    say("info", f"Linked to {rec.get('profile')}")
    for side, path, key in (("engine", os.path.join(game, "hookfxr.ini"), "enable"),
                            ("renderer", os.path.join(game, "Renderer", "doorstop_config.ini"), "enabled")):
        if not rec.get(side):
            say("info", f"{side.capitalize()} side: not linked (the profile lacked its loader)")
            continue
        folder = os.path.join(game, "BepInEx" if side == "engine" else os.path.join("Renderer", "BepInEx"))
        on = ini_value(path, key) == "true"
        good = os.path.islink(folder) and os.path.isdir(folder)
        say("ok" if on and good else "warn",
            f"{side.capitalize()} side: {os.path.basename(path)} {key}={ini_value(path, key)}, "
            + (f"{os.path.relpath(folder, game)} -> {os.path.realpath(folder)}" if good
               else f"{os.path.relpath(folder, game)} is not a link into the profile any more: link again"))
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--game", help="the build's folder, or a file in it (its launcher); default: Steam's Resonite")
    p.add_argument("--profile", help="the Gale profile, by name or folder (default: the one named like the build, "
                                     "else Default)")
    p.add_argument("--unlink", action="store_true", help="undo the link")
    p.add_argument("--status", action="store_true", help="show what is linked")
    p.add_argument("--force", action="store_true", help="move aside a BepInEx folder of the build's that holds plugins")
    args = p.parse_args()

    if not args.game and not steam_resonite():
        sys.exit("Steam's Resonite was not found: name the build's folder with --game.")
    game = os.path.abspath(os.path.expanduser(args.game or steam_resonite()))
    if os.path.isfile(game):
        game = os.path.dirname(game)
    if not is_build(game):
        sys.exit(f"Not a Resonite build (no Renderite.Host and Renderer/Renderite.Renderer.exe): {game}")
    if args.status:
        ok = status(game)
    elif args.unlink:
        ok = unlink(game)
    else:
        profile = os.path.abspath(os.path.expanduser(find_profile(args.profile, game)))
        if not os.path.isdir(profile):
            sys.exit(f"Gale profile not found: {profile}\nCreate it in Gale (BepisLoader, BepInExRenderer, "
                     "RenderiteHook), or name another with --profile.")
        ok = link(game, profile, args.force)
    sys.exit(0 if ok and not errors else 1)


if __name__ == "__main__":
    main()
