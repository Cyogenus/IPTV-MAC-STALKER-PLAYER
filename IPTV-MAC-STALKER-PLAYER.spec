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
    excludes=['PyQt6.QtBluetooth'],   # Exclude at analysis
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

# Forcefully remove QtBluetooth.framework BEFORE COLLECT
def nuke_bluetooth():
    dist_path = os.path.join("build", "IPTV-MAC-STALKER-PLAYER")
    for root, dirs, files in os.walk(dist_path):
        for d in dirs:
            if "QtBluetooth.framework" in d:
                bt_path = os.path.join(root, d)
                print("Removing:", bt_path)
                shutil.rmtree(bt_path, ignore_errors=True)

nuke_bluetooth()

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

# Run cleanup again after COLLECT
final_path = os.path.join("dist", "IPTV-MAC-STALKER-PLAYER", "_internal", "PyQt6", "Qt6", "lib")
bt_path = os.path.join(final_path, "QtBluetooth.framework")
if os.path.exists(bt_path):
    shutil.rmtree(bt_path, ignore_errors=True)
