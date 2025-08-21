# IPTV-MAC-STALKER-PLAYER.spec
# Custom PyInstaller spec for IPTV-MAC-STALKER-PLAYER
# Excludes unused Qt modules like Bluetooth to prevent Mac build errors

# IPTV-MAC-STALKER-PLAYER.spec
# (trimmed for clarity)

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['STALKER PLAYER.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PyQt6',
        'telethon'
    ],
    hookspath=[],
    hooksconfig={
        "PyQt6": {
            "exclude": ["QtBluetooth"]   # ✅ exclude Bluetooth here
        }
    },
    runtime_hooks=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IPTV-MAC-STALKER-PLAYER",
    windowed=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="IPTV-MAC-STALKER-PLAYER",
)
