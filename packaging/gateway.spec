# PyInstaller spec for the self-contained Headrouter gateway binary.
# Build with the PROJECT venv python (3.12) so headroom-ai/onnxruntime are bundled:
#   .venv/bin/pyinstaller packaging/gateway.spec
# Output: dist/headrouter-gateway

import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# headroom pulls in ML deps dynamically; include its submodules and data.
hiddenimports = (
    collect_submodules("headroom")
    + collect_submodules("uvicorn")
    # First-party app modules referenced only via the string "app:app" in
    # uvicorn.run() are not seen by the static analyzer; collect them all.
    + [
        "app",
        "compression_service",
        "config",
        "config_store",
        "request_log",
        "schemas",
    ]
    + collect_submodules("routes")
    + collect_submodules("middleware")
    + collect_submodules("adapters")
)
datas = collect_data_files("headroom", include_py_files=False)

a = Analysis(
    ["../gateway_launcher.py"],
    pathex=[".."],
    binaries=[],
    datas=datas
    + [
        ("../static/icon.png", "static"),
        # The app package source so uvicorn can import "app:app" at runtime.
        ("../app.py", "."),
        ("../static/admin.html", "static"),
    ],
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="headrouter-gateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
