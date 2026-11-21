#!/bin/bash
# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

# Builds the Resonite mods the Windows installer bundles (MoreFluxActions with its renderer part, SteamVRRoleFix,
# CyberFingerMod, ProximityGrab) from their GitHub repositories, on Linux, and with --install sets Resonite up
# with them, as the installer does on Windows:
#
#   ./build_resonite_mods.sh [--install] [--profile NAME] [--resonite FOLDER] [--out FOLDER]
#
# Each is a fresh clone of what is pushed, built against the installed Resonite, and staged in out/resonite_mods
# (or --out) in the layout the installer uses. Needs git, the .NET SDK (dotnet-sdk) and Resonite. The building is
# tools/stage_resonite_mods.py's, which also stages them for the installer.
#
# --install then
#   links Resonite to its Gale profile (resonite_gale_link.py: Default, or --profile), so that it loads the
#     profile's mods however it starts, ResoniteModLoader through the profile's ResoniteModLoaderLoader;
#   copies the BepInEx plugins into that profile, and the ResoniteModLoader mods into Resonite's rml_mods.
# The profile needs, from Gale: BepisLoader, BepInExRenderer, RenderiteHook, InterprocessLib, BepisResoniteWrapper,
# BepInExResoniteShim and ResoniteModLoaderLoader. Close Resonite first.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
for tool in git dotnet python3; do
    command -v "$tool" > /dev/null || { echo "build_resonite_mods.sh: $tool is needed" >&2; exit 1; }
done

out="$repo/out/resonite_mods"
install=0 profile="" resonite=""
while [ $# -gt 0 ]; do
    case "$1" in
        --install) install=1; shift ;;
        --out) out="$2"; shift 2 ;;
        --profile) profile="$2"; shift 2 ;;
        --resonite) resonite="$2"; shift 2 ;;
        -h|--help) sed -n '6,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build_resonite_mods.sh: unknown argument $1" >&2; exit 1 ;;
    esac
done
if [ -z "$resonite" ]; then
    for steam in .steam/steam .local/share/Steam .var/app/com.valvesoftware.Steam/.local/share/Steam; do
        if [ -f "$HOME/$steam/steamapps/common/Resonite/FrooxEngine.dll" ]; then
            resonite="$HOME/$steam/steamapps/common/Resonite"
            break
        fi
    done
fi
[ -n "$resonite" ] || { echo "build_resonite_mods.sh: Resonite was not found in Steam's library: name its folder with --resonite" >&2; exit 1; }

export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
python3 "$repo/tools/stage_resonite_mods.py" --out "$out" --resonite "$resonite"
[ "$install" = 1 ] || exit 0

profiles="${XDG_DATA_HOME:-$HOME/.local/share}/com.kesomannen.gale/resonite/profiles"
case "$profile" in
    "") profile="$profiles/Default" ;;
    */*) ;;
    *) profile="$profiles/$profile" ;;
esac
[ -d "$profile" ] || { echo "build_resonite_mods.sh: Gale profile not found: $profile" >&2; exit 1; }

python3 "$here/resonite_gale_link.py" --game "$resonite" --profile "$profile"

echo
echo "Installing the mods"
# --remove-destination: a new file each time, never written through a link (Gale hard-links its cache)
cp -r --remove-destination "$out/BepInEx" "$out/Renderer" "$profile/"
echo "  OK     BepInEx plugins (MoreFluxActions, SteamVRRoleFix) -> $profile"
mkdir -p "$resonite/rml_mods"
cp --remove-destination "$out"/rml_mods/*.dll "$resonite/rml_mods/"
echo "  OK     ResoniteModLoader mods (CyberFingerMod, ProximityGrab) -> $resonite/rml_mods"
if [ ! -f "$resonite/Libraries/ResoniteModLoader.dll" ]; then
    echo "  WARN   $resonite has no Libraries/ResoniteModLoader.dll: install ResoniteModLoader for the rml_mods to load"
fi
