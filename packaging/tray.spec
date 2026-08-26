# PyInstaller spec for the Headrouter tray frontend binary.
# NOTE (Linux): the tray uses AppIndicator via system GObject introspection
# (python3-gi / gir1.2-ayatanaappindicator3-0.1). PyInstaller CANNOT bundle
# those native typelibs, so this binary depends on them at runtime — they are
# declared as Depends: in the .deb. Build with a Python 3.14 interpreter that
# matches the system gi ABI and has pystray+Pillow+pyinstaller:
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
    # runtime hook prepends system dist-packages so `import gi` resolves to
    # the OS GObject introspection bindings at runtime (same 3.14 ABI).
    runtime_hooks=["tray_runtime_hook.py"],
    excludes=[],  # do NOT exclude gi: it must resolve from the system at runtime
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
