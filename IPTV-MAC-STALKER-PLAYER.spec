# -*- mode: python ; coding: utf-8 -*-
import os, shutil
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

a = Analysis(
    ['STALKER PLAYER.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files("PyQt6"),
    hiddenimports=collect_submodules("PyQt6"),
    hookspath=[],
    runtime_hooks=[],
    excludes=['PyQt6.QtBluetooth'],  # exclude at analysis
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='IPTV-MAC-STALKER-PLAYER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# --- PRE-COLLECT CLEANUP ---
def nuke_bluetooth_build():
    build_path = os.path.join("build", "IPTV-MAC-STALKER-PLAYER")
    for root, dirs, _ in os.walk(build_path):
        for d in dirs:
            if "QtBluetooth.framework" in d:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

nuke_bluetooth_build()

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='IPTV-MAC-STALKER-PLAYER'
)

# --- POST-COLLECT CLEANUP ---
def nuke_bluetooth_dist():
    bt_path = os.path.join("dist", "IPTV-MAC-STALKER-PLAYER", "_internal", "PyQt6", "Qt6", "lib", "QtBluetooth.framework")
    if os.path.exists(bt_path):
        shutil.rmtree(bt_path, ignore_errors=True)

nuke_bluetooth_dist()
