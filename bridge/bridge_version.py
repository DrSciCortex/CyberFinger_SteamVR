# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""The bridge's version as it shows in its window: "v2.2.1 (7563af4)".

The number is the project's, from CMakeLists.txt (the installer's setup.iss carries the same one). The hash is the
git commit the bridge was built from, with "-dirty" when the checkout had uncommitted changes, so the hash alone
doesn't say what the build holds. A built exe has the string stamped in: cyberfinger_bridge.spec writes it to
version.txt, which goes into the bundle. Run from source, it's read live.
"""

import os
import re
import subprocess
import sys

FILE = "version.txt"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


def from_source(root=ROOT):
    """The version of the checkout at root: "v<number> (<hash>)", or "v<number>" without git."""
    number = "?"
    try:
        with open(os.path.join(root, "CMakeLists.txt"), encoding="utf-8") as f:
            found = re.search(r"\bproject\s*\(\s*\w+\s+VERSION\s+([0-9.]+)", f.read())
        if found:
            number = found.group(1)
    except OSError:
        pass
    try:
        commit = _git(root, "rev-parse", "--short", "HEAD")
        if commit and _git(root, "status", "--porcelain", "--untracked-files=no"):
            commit += "-dirty"
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return f"v{number} ({commit})" if commit else f"v{number}"


def get():
    """The version of the running bridge: stamped into a built exe, else from the checkout."""
    if hasattr(sys, "_MEIPASS"):
        try:
            with open(os.path.join(sys._MEIPASS, FILE), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return "v?"
    return from_source()
