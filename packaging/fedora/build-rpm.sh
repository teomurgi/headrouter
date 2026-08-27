#!/usr/bin/env bash
# Build the Headrouter .rpm package (Fedora/RHEL/openSUSE).
#
#   ./packaging/fedora/build-rpm.sh
#
# Freezes the gateway and tray binaries with PyInstaller (same interpreter
# split as the .deb build), stages the package tree, and runs rpmbuild.
#
# Runs on Ubuntu CI runners: rpmbuild is provided by the `rpm` apt package.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

GATEWAY_PYINSTALLER="$ROOT/.venv/bin/pyinstaller"
TRAY_VENV="${TRAY_VENV:-/tmp/tray-venv}"
SYSTEM_PY="${SYSTEM_PY:-/usr/bin/python3}"

PKG="headrouter"
VERSION="$(grep -m1 '^%global\|^Version:' packaging/fedora/headrouter.spec 2>/dev/null | head -1 || true)"
# Version is owned by the workflow (Set version from tag step), which writes it
# into packaging/fedora/version. Fall back to pyproject.toml for local builds.
if [[ -f "$ROOT/packaging/fedora/version" ]]; then
  VERSION="$(cat "$ROOT/packaging/fedora/version")"
else
  VERSION="$(grep -m1 '^version' pyproject.toml | sed -E 's/version = "(.*)"/\1/')"
fi
# Sanitize: rpmbuild forbids '-' and path/whitespace chars in Version. Keep only
# alphanumerics, '.', '_', '~', '+'. Turn any stray '-' into '~'.
VERSION="$(printf '%s' "$VERSION" | tr -d ' \t\r\n' | sed 's/-/~/g; s/[^A-Za-z0-9._~+]//g')"

# rpmbuild wants x86_64 / aarch64, not dpkg-style amd64 / arm64.
case "$(uname -m)" in
  x86_64)  ARCH="x86_64" ;;
  aarch64) ARCH="aarch64" ;;
  *)       ARCH="$(uname -m)" ;;
esac

BUILD="$ROOT/packaging/fedora/build"
STAGE="$BUILD/stage"
TOPDIR="$BUILD/rpmbuild"

echo ">> cleaning previous build"
rm -rf "$BUILD" "$ROOT/packaging/dist" "$ROOT/packaging/build"
mkdir -p "$STAGE" "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# --- provision a tray freeze venv on system python (gi ABI must match) -------
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
echo ">> freezing tray binary (system python for gi/AppIndicator)"
"$TRAY_VENV/bin/pyinstaller" --noconfirm --clean tray.spec >/dev/null
cd "$ROOT"

echo ">> staging package tree -> $STAGE"
mkdir -p "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" \
         "$STAGE/usr/share/headrouter" \
         "$STAGE/etc/xdg/autostart"

cp "packaging/dist/headrouter-gateway" "$STAGE/usr/bin/headrouter-gateway"
cp "packaging/dist/headrouter-tray"    "$STAGE/usr/bin/headrouter-tray"
cp packaging/ubuntu/headrouter.desktop "$STAGE/usr/share/applications/"
cp static/icon.png "$STAGE/usr/share/icons/hicolor/256x256/apps/headrouter.png"
# Ship the template (not providers.json, which holds API keys) as a reference.
cp providers.example.json "$STAGE/usr/share/headrouter/providers.example.json" 2>/dev/null || true
# autostart the tray on login
cp packaging/ubuntu/headrouter.desktop "$STAGE/etc/xdg/autostart/headrouter.desktop"

chmod 0755 "$STAGE/usr/bin/headrouter-gateway" "$STAGE/usr/bin/headrouter-tray"

echo ">> building .rpm (${VERSION}, ${ARCH})"
rpmbuild \
  --define "_topdir $TOPDIR" \
  --define "headrouter_version $VERSION" \
  --define "headrouter_arch $ARCH" \
  --define "headrouter_stage $STAGE" \
  --target "$ARCH" \
  -bb packaging/fedora/headrouter.spec

# rpmbuild drops the package under RPMS/<arch>/; copy it next to the .deb output.
mkdir -p "$BUILD"
find "$TOPDIR/RPMS" -name '*.rpm' -exec cp {} "$BUILD/" \;

echo ""
echo "Built:"
find "$BUILD" -maxdepth 1 -name '*.rpm'
echo "Install with: sudo dnf install <file>.rpm"
