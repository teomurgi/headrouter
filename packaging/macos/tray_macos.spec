# PyInstaller spec for the Headrouter tray as a macOS .app bundle.
#
# On macOS pystray uses the native _darwin backend (PyObjC / NSStatusItem), so
# there is NO gi/AppIndicator dependency — the tray is fully self-contained.
# Build on macOS with a python that has pystray + pillow + pyobjc + pyinstaller:
#   python3 -m venv /tmp/tray-venv
#   /tmp/tray-venv/bin/pip install pystray pillow pyobjc pyinstaller
#   cd packaging && /tmp/tray-venv/bin/pyinstaller --noconfirm --clean macos/tray_macos.spec
# Output: packaging/dist/HeadrouterTray.app
#
# The bundle embeds the frozen headrouter-gateway binary as a resource; the
# tray's _resolve_launch finds it next to the executable inside the bundle.

import os

block_cipher = None

# Absolute path to packaging/ so this works regardless of invocation cwd.
# SPEC (injected by PyInstaller) points at packaging/macos/tray_macos.spec,
# one level deeper than packaging/, hence the double dirname.
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))  # noqa: F821
ROOT = os.path.dirname(PKG_DIR)

a = Analysis(
    [os.path.join(ROOT, "tray_app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "static", "icon.png"), "static"),
        (os.path.join(ROOT, "static", "icon-stopped.png"), "static"),
        # Embed the frozen gateway binary inside the .app resources.
        (os.path.join(PKG_DIR, "dist", "headrouter-gateway"), "bin"),
    ],
    hiddenimports=["pystray._darwin"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="headrouter-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # set to "arm64" or "universal2" for a specific slice
    codesign_identity=None,  # set to your Developer ID for signed builds
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="headrouter-tray",
)
app = BUNDLE(
    coll,
    name="HeadrouterTray.app",
    icon=os.path.join(ROOT, "packaging", "macos", "icon.icns"),
    bundle_identifier="dev.headrouter.tray",
    info_plist={
        "CFBundleName": "Headrouter",
        "CFBundleDisplayName": "Headrouter",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        # Menu-bar agent: no Dock icon.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
