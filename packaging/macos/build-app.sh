#!/usr/bin/env bash
# Build the Headrouter macOS .app bundle (run on macOS).
#
#   ./packaging/macos/build-app.sh
#
# Produces: packaging/dist/HeadrouterTray.app
#
# Steps:
#   1. Freeze the gateway binary (self-contained) with the project interpreter.
#   2. Freeze the tray as a windowed .app (pystray _darwin backend — no gi).
# The gateway binary is embedded in the .app and spawned by the tray.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: build-app.sh must be run on macOS (Darwin)." >&2
  exit 1
fi

# Project interpreter with headroom-ai/onnxruntime (for the gateway).
GATEWAY_PYINSTALLER="${GATEWAY_PYINSTALLER:-$ROOT/.venv/bin/pyinstaller}"
# Tray freeze env: needs pystray + pillow + pyobjc + pyinstaller.
TRAY_VENV="${TRAY_VENV:-/tmp/tray-venv-macos}"

PKG="headrouter"
VERSION="0.1.0"

echo ">> cleaning previous build"
rm -rf "$ROOT/packaging/dist" "$ROOT/packaging/build"

# --- provision the tray freeze venv ----------------------------------------
if [[ ! -x "$TRAY_VENV/bin/pyinstaller" ]]; then
  echo ">> provisioning tray freeze venv at $TRAY_VENV"
  python3 -m venv "$TRAY_VENV"
  "$TRAY_VENV/bin/pip" install -q pystray pillow pyobjc pyinstaller
fi

# --- gateway binary ----------------------------------------------------------
cd "$ROOT/packaging"
echo ">> freezing gateway binary"
"$GATEWAY_PYINSTALLER" --noconfirm --clean gateway.spec >/dev/null

# --- tray .app bundle (embeds the gateway binary) ---------------------------
echo ">> freezing tray .app bundle"
"$TRAY_VENV/bin/pyinstaller" --noconfirm --clean macos/tray_macos.spec >/dev/null
cd "$ROOT"

APP="$ROOT/packaging/dist/HeadrouterTray.app"
echo ""
echo "Built: $APP"

# --- optional: signing + notarization (uncomment and configure) -------------
# CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
# echo ">> signing"
# codesign --deep --force --options runtime --sign "$CODESIGN_IDENTITY" "$APP"
# echo ">> creating dmg + notarizing"
# ... (create-dmg / xcrun notarytool submit ...) — see packaging/macos/README.md
