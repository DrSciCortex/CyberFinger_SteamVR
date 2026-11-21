#!/bin/bash
# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

# Builds WiVRn's server with the CyberFinger driver, without installing anything: WiVRn's sources are
# downloaded, patched (wivrn-cyberfinger.patch) and built in a cache folder, with the driver compiled from this
# checkout. The server then runs from there, in place of the installed one, to try the driver out:
#
#   ./build.sh
#   ~/.cache/cyberfinger/wivrn-26.9/build-server/server/wivrn-server
#
# The version must be the one of the WiVRn app on the headset (and of the installed wivrn-server, whose
# client libraries the games keep using). On Arch, the PKGBUILD beside this script makes a package that
# replaces the installed server instead, which is what the WiVRn dashboard starts.
#
#   WIVRN_VERSION        WiVRn release to build (default 26.9, what the patch was made for)
#   CF_WIVRN_BUILD_DIR   where to build (default ~/.cache/cyberfinger/wivrn-<version>)
#   further arguments go to cmake's configure step, e.g. -DWIVRN_USE_NVENC=OFF
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
version="${WIVRN_VERSION:-26.9}"
work="${CF_WIVRN_BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/cyberfinger/wivrn-$version}"
patch_file="$here/wivrn-cyberfinger.patch"
src="$work/WiVRn-$version"

for tool in cmake ninja curl patch tar; do
    command -v "$tool" > /dev/null || { echo "build.sh: $tool is needed" >&2; exit 1; }
done

mkdir -p "$work"
# Unpacked afresh whenever the patch changed: a patch can't be applied twice.
if [ ! -f "$src/.cyberfinger-patched" ] || [ "$patch_file" -nt "$src/.cyberfinger-patched" ]; then
    tarball="$work/wivrn-$version.tar.gz"
    [ -f "$tarball" ] || curl -fL -o "$tarball" "https://github.com/WiVRn/WiVRn/archive/refs/tags/v$version.tar.gz"
    rm -rf "$src"
    tar xf "$tarball" -C "$work"
    patch -d "$src" -p1 < "$patch_file"
    touch "$src/.cyberfinger-patched"
fi

cmake -S "$src" -B "$work/build-server" -G Ninja \
    -DGIT_TAG="v$version" \
    -DWIVRN_BUILD_SERVER=ON \
    -DWIVRN_BUILD_WIVRNCTL=ON \
    -DWIVRN_BUILD_CLIENT=OFF \
    -DWIVRN_BUILD_DASHBOARD=OFF \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DWIVRN_FEATURE_STEAMVR_LIGHTHOUSE=ON \
    -DWIVRN_CYBERFINGER_DIR="$repo" \
    -Wno-dev "$@"
cmake --build "$work/build-server"

echo
echo "Built. Stop the running WiVRn server (its dashboard, or: systemctl --user stop wivrn), then start this one:"
echo "  $work/build-server/server/wivrn-server"
