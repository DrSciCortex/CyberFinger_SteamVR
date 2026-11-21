# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Build the Resonite mods and stage them for the installer's "Resonite mods" option.

    python tools/stage_resonite_mods.py [--out FOLDER] [--resonite FOLDER]

Windows or Linux (bridge_linux/build_resonite_mods.sh). Writes bridge/installer/resonite_mods/ (or --out), laid
out as the installer copies it:

    BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions/          engine plugin (BepisLoader)
    Renderer/BepInEx/plugins/DrSciCortex-MoreFluxActions/...              renderer plugins (BepInExRenderer)
    Renderer/BepInEx/plugins/DrSciCortex-SteamVRRoleFix/
    rml_mods/CyberFingerMod.dll, rml_mods/ProximityGrab.dll                ResoniteModLoader mods
    standalone/Renderer/...                                                BepInEx for the renderer, without Gale

and VERSIONS.txt, the commit each mod was built from. Each mod is built from a fresh shallow clone of its GitHub
repository (default branch) under out/resonite_mods_src/, made anew on every run: the installer gets what is
pushed, whatever is in local checkouts. Needs git, the .NET SDK and Resonite installed: the mods compile against
Resonite's assemblies. The projects find Resonite in Steam's default folder; --resonite names another (on Linux
the other usual Steam folders are looked in too).

standalone/ holds what SteamVRRoleFix needs on a build without Gale (tools/resonite_gale_link.ps1 -Standalone): the
renderer's BepInEx from the latest ResoniteModding-BepInExRenderer on Thunderstore, and the doorstop that loads it
from the latest ResoniteModding-RenderiteHook, with THIRD-PARTY-NOTICES.txt (BepInEx and UnityDoorstop: LGPL-2.1).
"""

import argparse
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "bridge", "installer", "resonite_mods")
CLONES = os.path.join(ROOT, "out", "resonite_mods_src")

# name, clone URL, [(project to build, extra build properties)], [(built file, staged folder)]
MODS = [
    ("MoreFluxActionsMod", "https://github.com/DrSciCortex/MoreFluxActionsMod",
     [("MoreFluxActions/MoreFluxActions.csproj", []),
      ("MoreFluxActions.Renderer/MoreFluxActions.Renderer.csproj", [])],
     [("MoreFluxActions/bin/Release/MoreFluxActions.dll",
       "BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions"),
      ("MoreFluxActions.Renderer/bin/Release/MoreFluxActions.Renderer.dll",
       "Renderer/BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions.Renderer")]),
    ("SteamVRRoleFix", "https://github.com/DrSciCortex/SteamVRRoleFix",
     [("SteamVRRoleFix.csproj", [])],
     [("bin/Release/SteamVRRoleFix.dll", "Renderer/BepInEx/plugins/DrSciCortex-SteamVRRoleFix")]),
    ("CyberFingerMod", "https://github.com/DrSciCortex/CyberFingerMod",
     [("CyberFingerMod/CyberFingerMod.csproj", ["-p:CopyToMods=false"])],
     [("CyberFingerMod/bin/Release/net10.0-windows/CyberFingerMod.dll", "rml_mods")]),
    ("ProximityGrab", "https://github.com/SciCortex/ProximityGrab",
     [("ProximityGrab/ProximityGrab.csproj", ["-p:CopyToMods=false"])],
     [("ProximityGrab/bin/Release/net10.0/ProximityGrab.dll", "rml_mods")]),
]


def run(args, cwd=None):
    print(">", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def remove_tree(path):
    """shutil.rmtree, also for git's read-only object files on Windows."""
    def writable_again(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=writable_again)
        else:
            shutil.rmtree(path, onerror=writable_again)


def fresh_clone(name, url):
    """A new shallow clone of the mod's repository: nothing left over from an earlier build."""
    clone = os.path.join(CLONES, name)
    remove_tree(clone)
    os.makedirs(CLONES, exist_ok=True)
    run(["git", "clone", "--depth", "1", "--quiet", url, clone])
    return clone


def find_resonite():
    """Resonite's folder on Linux, where Steam's library can be in several places (the projects know only
    ~/.steam/steam). None on Windows: there the projects' own default is right."""
    if sys.platform == "win32":
        return None
    home = os.path.expanduser("~")
    for steam in (".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
        path = os.path.join(home, steam, "steamapps", "common", "Resonite")
        if os.path.isfile(os.path.join(path, "FrooxEngine.dll")):
            return path
    return None


def gale_profiles():
    """Gale's Resonite profiles on Linux (the installer's own setup finds them on Windows), Default first."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    root = os.path.join(data, "com.kesomannen.gale", "resonite", "profiles")
    names = sorted(os.listdir(root), key=lambda n: (n != "Default", n.lower())) if os.path.isdir(root) else []
    return [os.path.join(root, n) for n in names if os.path.isdir(os.path.join(root, n))]


def renderer_bepinex_core(resonite, out):
    """The renderer's BepInEx 5, which SteamVRRoleFix compiles against. Its project finds it in Gale's Default
    profile under %APPDATA% or in the game folder; on Linux it is found here instead: a Gale profile, the game
    folder, else the copy staged for standalone/. None on Windows."""
    if sys.platform == "win32":
        return None
    for base in gale_profiles() + ([resonite] if resonite else []) + [os.path.join(out, "standalone")]:
        core = os.path.join(base, "Renderer", "BepInEx", "core")
        if os.path.isfile(os.path.join(core, "BepInEx.dll")):
            return core
    return None


def fetch(url):
    # Thunderstore refuses urllib's default User-Agent (403)
    req = urllib.request.Request(url, headers={"User-Agent": "CyberFinger-installer-staging/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def thunderstore_latest(namespace, name):
    """The latest version of a Thunderstore package: (version, the zip's bytes)."""
    info = json.loads(fetch(f"https://thunderstore.io/api/experimental/package/{namespace}/{name}/"))
    latest = info["latest"]
    return latest["version_number"], fetch(latest["download_url"])


def stage_standalone(out_root):
    """BepInEx for the renderer and its doorstop, for builds without Gale (SteamVRRoleFix only)."""
    out = os.path.join(out_root, "standalone", "Renderer")
    versions = []
    ver, data = thunderstore_latest("ResoniteModding", "BepInExRenderer")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        core = [n for n in z.namelist() if "/Renderer/BepInEx/core/" in "/" + n and not n.endswith("/")]
        for n in core:
            dest = os.path.join(out, "BepInEx", "core", os.path.basename(n))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(n))
    if not any(n.endswith("BepInEx.Preloader.dll") for n in core):
        raise OSError("BepInExRenderer package without Renderer/BepInEx/core/BepInEx.Preloader.dll")
    versions.append(f"ResoniteModding-BepInExRenderer {ver} (Thunderstore): renderer BepInEx core, {len(core)} files")
    ver, data = thunderstore_latest("ResoniteModding", "RenderiteHook")
    licence = ""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        door = [n for n in z.namelist() if "/Doorstop/" in "/" + n and not n.endswith("/")]
        for n in door:
            with open(os.path.join(out, os.path.basename(n)), "wb") as f:
                f.write(z.read(n))
        lic = [n for n in z.namelist() if os.path.basename(n).upper().startswith("LICENSE")]
        if lic:
            licence = z.read(lic[0]).decode("utf-8", "replace")
    if not all(os.path.isfile(os.path.join(out, f)) for f in ("winhttp.dll", "doorstop_config.ini")):
        raise OSError("RenderiteHook package without Doorstop/winhttp.dll and doorstop_config.ini")
    versions.append(f"ResoniteModding-RenderiteHook {ver} (Thunderstore): the renderer's doorstop")
    notices = [
        "Third-party software in this folder",
        "",
        "Renderer/BepInEx/core: BepInEx (https://github.com/BepInEx/BepInEx), LGPL-2.1, as packaged for",
        "Resonite by ResoniteModding (https://thunderstore.io/c/resonite/p/ResoniteModding/BepInExRenderer/).",
        "Renderer/winhttp.dll, doorstop_config.ini, .doorstop_version: UnityDoorstop",
        "(https://github.com/NeighTools/UnityDoorstop), LGPL-2.1, from ResoniteModding's RenderiteHook",
        "(https://github.com/ResoniteModding/RenderiteHook, MIT No Attribution).",
        "Both are unmodified; their source is at the addresses above.",
    ]
    for title, url in (("BepInEx", "https://raw.githubusercontent.com/BepInEx/BepInEx/master/LICENSE"),
                       ("UnityDoorstop", "https://raw.githubusercontent.com/NeighTools/UnityDoorstop/master/LICENSE")):
        try:
            notices += ["", "=" * 78, f"{title} licence ({url})", "=" * 78, fetch(url).decode("utf-8", "replace")]
        except OSError:
            notices += ["", f"{title} licence: {url}"]
    if licence:
        notices += ["", "=" * 78, "RenderiteHook licence", "=" * 78, licence]
    with open(os.path.join(out_root, "standalone", "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(notices) + "\n")
    return versions


def main(out, resonite):
    remove_tree(out)
    standalone = stage_standalone(out)      # first: on Linux its BepInEx may be what SteamVRRoleFix compiles against
    resonite = resonite or find_resonite()
    common = []
    if resonite:
        common.append("-p:ResonitePath=" + os.path.join(os.path.abspath(resonite), ""))
    core = renderer_bepinex_core(resonite, out)
    if core:
        common.append("-p:BepInExCore=" + os.path.join(core, ""))
    versions = []
    for name, url, projects, outputs in MODS:
        repo = fresh_clone(name, url)
        for project, props in projects:
            run(["dotnet", "build", os.path.join(repo, project), "-c", "Release", "-nologo", "-v", "q",
                 *common, *props])
        for built, folder in outputs:
            dest = os.path.join(out, folder)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(os.path.join(repo, built), dest)
        commit = git(repo, "log", "-1", "--format=%h %cs %s")
        versions.append(f"{name}: {commit} ({url})")
    versions += standalone
    with open(os.path.join(out, "VERSIONS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(versions) + "\n")
    print("\nStaged in " + out + ":\n  " + "\n  ".join(versions))
    if sys.platform != "win32":
        # No installer on Linux: where the folders go.
        profiles = gale_profiles()
        print("\nTo use them:\n  BepInEx/ and Renderer/ go into the mod profile Resonite starts with: "
              + (profiles[0] if profiles else "the Gale profile, or the Resonite folder with BepisLoader")
              + "\n  rml_mods/*.dll go into " + os.path.join(resonite or "<Resonite>", "rml_mods")
              + "\n  standalone/ is for a Resonite without Gale (README: SteamVRRoleFix without Gale)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Resonite mods from their GitHub repositories and stage "
                                                 "them as the installer bundles them.")
    parser.add_argument("--out", default=OUT, help="where to stage them (default: bridge/installer/resonite_mods)")
    parser.add_argument("--resonite", help="Resonite's folder, if not in Steam's default library")
    args = parser.parse_args()
    out = os.path.abspath(args.out)
    try:
        main(out, args.resonite)
    except (subprocess.CalledProcessError, OSError) as e:
        remove_tree(out)                    # no half-staged mods in the installer
        sys.exit(f"Staging the Resonite mods failed: {e}\nThe mods need git, the .NET SDK and Resonite installed. "
                 "For tools\\build_installer.cmd, CF_RESONITE_MODS=keep packages a resonite_mods folder staged on "
                 "another PC, and CF_RESONITE_MODS=skip builds the installer without the mods.")
