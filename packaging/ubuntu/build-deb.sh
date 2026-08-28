#!/usr/bin/env bash
# Build the Headrouter .deb package (Ubuntu/Debian).
#
#   ./packaging/ubuntu/build-deb.sh
#
# Freezes the gateway and tray binaries with PyInstaller, lays out the package
# tree, and runs dpkg-deb.
#
# Interpreter split (deliberate):
#   * gateway  -> project .venv Python 3.12  (bundles headroom-ai/onnxruntime)
#   * tray     -> system Python 3.14          (must match the system python3-gi
#                                              ABI so AppIndicator loads; the
#                                              typelibs cannot be bundled and
#                                              are pulled in via Depends:)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

GATEWAY_PYINSTALLER="$ROOT/.venv/bin/pyinstaller"
TRAY_VENV="${TRAY_VENV:-/tmp/tray-venv}"
SYSTEM_PY="${SYSTEM_PY:-/usr/bin/python3}"

PKG="headrouter"
VERSION="$(grep -m1 '^Version:' packaging/ubuntu/control | awk '{print $2}')"
ARCH="$(dpkg --print-architecture)"
BUILD="$ROOT/packaging/ubuntu/build"
STAGE="$BUILD/${PKG}_${VERSION}_${ARCH}"

echo ">> cleaning previous build"
rm -rf "$BUILD" "$ROOT/packaging/dist" "$ROOT/packaging/build"
mkdir -p "$STAGE"

# --- provision a tray freeze venv (frozen interpreter's ABI defines what the
# --- bundle needs; PyInstaller bundles gi + typelibs, so it is self-contained)
if [[ ! -x "$TRAY_VENV/bin/pyinstaller" ]]; then
  echo ">> provisioning tray freeze venv at $TRAY_VENV (system python + pystray)"
  "$SYSTEM_PY" -m venv --system-site-packages "$TRAY_VENV"
  "$TRAY_VENV/bin/pip" install -q pystray pillow pyinstaller
fi

# Specs reference ../<module>, so run PyInstaller from packaging/; it writes
# to packaging/dist/.
cd "$ROOT/packaging"
echo ">> freezing gateway binary (self-contained, .venv python 3.12)"
"$GATEWAY_PYINSTALLER" --noconfirm --clean gateway.spec >/dev/null
echo ">> freezing tray binary (self-contained: gi + typelibs bundled)"
"$TRAY_VENV/bin/pyinstaller" --noconfirm --clean tray.spec >/dev/null
cd "$ROOT"

echo ">> staging package tree -> $STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" \
         "$STAGE/usr/share/headrouter"

cp "packaging/dist/headrouter-gateway" "$STAGE/usr/bin/headrouter-gateway"
cp "packaging/dist/headrouter-tray"    "$STAGE/usr/bin/headrouter-tray"
cp packaging/ubuntu/headrouter.desktop "$STAGE/usr/share/applications/"
cp static/icon.png "$STAGE/usr/share/icons/hicolor/256x256/apps/headrouter.png"
# Ship the template (not providers.json, which holds API keys) as a reference.
cp providers.example.json "$STAGE/usr/share/headrouter/providers.example.json" 2>/dev/null || true
chmod 0644 "$STAGE/usr/share/headrouter/providers.example.json" 2>/dev/null || true

chmod 0755 "$STAGE/usr/bin/headrouter-gateway" "$STAGE/usr/bin/headrouter-tray"

# render control with the detected architecture
sed "s/@ARCH@/${ARCH}/" packaging/ubuntu/control > "$STAGE/DEBIAN/control"
# autostart the tray on login
mkdir -p "$STAGE/etc/xdg/autostart"
cp packaging/ubuntu/headrouter.desktop "$STAGE/etc/xdg/autostart/headrouter.desktop"

echo ">> building .deb"
dpkg-deb --build --root-owner-group "$STAGE" "$BUILD/${PKG}_${VERSION}_${ARCH}.deb"

echo ""
echo "Built: $BUILD/${PKG}_${VERSION}_${ARCH}.deb"
echo "Install with: sudo apt install $BUILD/${PKG}_${VERSION}_${ARCH}.deb"
