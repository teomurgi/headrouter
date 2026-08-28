# PyInstaller spec for the Headrouter tray frontend binary.
# NOTE (Linux): PyInstaller's gi hooks bundle the gi C extension, the GTK /
# AppIndicator typelibs and the needed shared libraries into the binary, so
# the tray is self-contained. The .deb still declares python3-gi /
# gir1.2-ayatanaappindicator3-0.1 under Depends: as a safety net.
# Build with any Python 3 that has pystray+Pillow+pyinstaller (CI uses the
# ubuntu-24.04-arm system python):
#   /usr/bin/python3 -m venv --system-site-packages /tmp/tray-venv
#   /tmp/tray-venv/bin/pip install pystray pillow pyinstaller
#   /tmp/tray-venv/bin/pyinstaller packaging/tray.spec
# Output: dist/headrouter-tray

block_cipher = None

a = Analysis(
    ["../tray_app.py"],
    pathex=[".."],
    binaries=[],
    datas=[("../static/icon.png", "static"), ("../static/icon-stopped.png", "static")],
    hiddenimports=["pystray._appindicator", "pystray._gtk", "pystray._xorg"],
    hookspath=[],
    hooksconfig={},
    # No runtime hook: prepending system dist-packages would let a system
    # Pillow (built for a different interpreter ABI) shadow the bundled PIL
    # and break `from PIL import Image` with an ImportError on _imaging.
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="headrouter-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # tray app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="static/icon.png",
)
