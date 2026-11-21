# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CyberFinger Bridge
# Build with: pyinstaller cyberfinger_bridge.spec

import os

block_cipher = None

import PyInstaller.utils.hooks

# Collect vgamepad's bundled DLLs (ViGEmClient.dll) and data files
vgamepad_datas, vgamepad_binaries, vgamepad_hiddenimports = \
    PyInstaller.utils.hooks.collect_all('vgamepad')

# pyopenvr loads its runtime DLL through a computed ctypes path, which
# PyInstaller cannot infer. Keep the DLLs beside the bundled openvr package.
openvr_binaries = PyInstaller.utils.hooks.collect_dynamic_libs('openvr')

# Collect Tcl/Tk's native DLLs when building with a conda Python: conda keeps tcl86t.dll/tk86t.dll (and their
# own dependencies) in Library\bin, not next to _tkinter.pyd in DLLs\, so PyInstaller's automatic dependency
# scan can miss them unless Library\bin happens to be on PATH at build time -- it isn't with a plain PATH
# prepend of the env root, only with a full "conda activate". Without this, the built exe fails at startup:
# "ImportError: DLL load failed while importing _tkinter: The specified module could not be found."
tk_binaries = []
try:
    from PyInstaller.utils.hooks.conda import collect_dynamic_libs
    tk_binaries = collect_dynamic_libs('tk')
except Exception:
    pass

# Collect all pystray submodules (backends are loaded dynamically)
pystray_submodules = PyInstaller.utils.hooks.collect_submodules('pystray')

# Discover ALL installed winrt namespace packages.
# Each winrt-Windows.X.Y pip package installs as winrt.windows.x.y
# PyInstaller can't auto-detect these because they're separate namespace packages.
import importlib, pkgutil
winrt_hiddenimports = ['winrt', 'winrt._winrt', 'winrt.system']
try:
    import winrt
    for path in winrt.__path__:
        pass
    # Walk all winrt subpackages
    for importer, modname, ispkg in pkgutil.walk_packages(winrt.__path__, prefix='winrt.'):
        winrt_hiddenimports.append(modname)
except Exception:
    pass

# Fallback: explicitly list known winrt modules in case walk fails
winrt_explicit = [
    'winrt.windows.devices.bluetooth',
    'winrt.windows.devices.bluetooth.genericattributeprofile',
    'winrt.windows.devices.enumeration',
    'winrt.windows.storage.streams',
    'winrt.windows.foundation',
    'winrt.windows.foundation.collections',
]
for m in winrt_explicit:
    if m not in winrt_hiddenimports:
        winrt_hiddenimports.append(m)

# The version the window shows, "v2.2.1 (<commit>)", stamped in now: the exe has no checkout to read it from.
import sys
sys.path.insert(0, SPECPATH)
import bridge_version
bridge_version_text = bridge_version.from_source()
version_file = os.path.join(workpath, bridge_version.FILE)
with open(version_file, 'w', encoding='utf-8') as f:
    f.write(bridge_version_text)
print('CyberFinger Bridge version:', bridge_version_text)

a = Analysis(
    ['cyberfinger_gui.py'],
    pathex=[],
    binaries=vgamepad_binaries + tk_binaries + openvr_binaries,
    datas=[
        ('assets/icon_32x32.png', 'assets'),
        ('assets/icon_32x32_bw.png', 'assets'),
        ('assets/icon.png', 'assets'),
        ('assets/cyberfinger_actions.json', 'assets'),
        ('assets/cyberfinger_bindings_cyberfinger.json', 'assets'),
        ('assets/cyberfinger_bindings_knuckles.json', 'assets'),
        ('assets/cyberfinger_bindings_oculus_touch.json', 'assets'),
        (version_file, '.'),
    ] + vgamepad_datas,
    hiddenimports=[
        'vgamepad',
        'pystray',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
    ] + vgamepad_hiddenimports + pystray_submodules + winrt_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Strip personal paths from .pyc bytecode ──
# Replaces "C:\Users\<user>\..." with just the filename
for d in a.pure:
    # d is (name, path, typecode)
    pass  # Analysis handles this; we strip below via --strip-paths

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='CyberFingerBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',  # Generated from PNG during build
)
