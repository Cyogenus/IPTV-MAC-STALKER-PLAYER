# IPTV-MAC-STALKER-PLAYER.spec
# Custom PyInstaller spec for IPTV-MAC-STALKER-PLAYER
# Excludes unused Qt modules like Bluetooth to prevent Mac build errors

block_cipher = None

a = Analysis(
    ['STALKER PLAYER.py'],   # 👈 replace with your main script name if different
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    excludes=[
        'PyQt6.QtBluetooth',     # 🚫 exclude Bluetooth
        'PyQt6.QtMultimedia',    # 🚫 exclude Multimedia
        'PyQt6.QtPositioning',   # 🚫 exclude Positioning
    ],
    runtime_hooks=[],
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
    console=False,   # set True if you want a terminal window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='IPTV-MAC-STALKER-PLAYER',
)
