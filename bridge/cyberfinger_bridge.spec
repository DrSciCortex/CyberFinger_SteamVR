# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CyberFinger Bridge
# Build with: pyinstaller cyberfinger_bridge.spec

import os

block_cipher = None

# Strip personal paths from the binary
import PyInstaller.utils.hooks
import os

a = Analysis(
    ['cyberfinger_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/icon_32x32.png', 'assets'),
        ('assets/icon_32x32_bw.png', 'assets'),
        ('assets/icon.png', 'assets'),
    ],
    hiddenimports=[
        'winrt',
        'winrt.windows.devices.bluetooth',
        'winrt.windows.devices.bluetooth.genericattributeprofile',
        'winrt.windows.devices.enumeration',
        'winrt.windows.storage.streams',
        'winrt.windows.foundation',
        'vgamepad',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
    ],
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
# Replaces "C:\Users\bengu\..." with just the filename
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
