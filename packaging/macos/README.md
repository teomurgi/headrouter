# Headrouter on macOS

The macOS build produces a `Headrouter.app` menu-bar bundle that manages the
gateway as a subprocess.

## How it differs from Linux

- **Tray backend**: pystray uses the native `_darwin` backend (PyObjC /
  `NSStatusItem`). There is **no** `gi` / AppIndicator dependency, so the tray
  is fully self-contained — unlike the Linux build, nothing needs to come from
  the OS.
- **LSUIElement**: the bundle sets `LSUIElement = true`, so it lives in the menu
  bar with no Dock icon.
- **Gateway**: the frozen `headrouter-gateway` binary is embedded inside the
  `.app` (`Contents/Resources/bin`) and spawned by the tray. `_resolve_launch`
  in `tray_app.py` searches the bundle layout.

## Build

Run on macOS (cannot be cross-built from Linux):

```sh
./packaging/macos/build-app.sh
```

Output: `packaging/dist/Headrouter.app`

## Icon

The bundle references `packaging/macos/icon.icns`. Generate it from the PNG:

```sh
mkdir icon.iconset
sips -z 512 512 static/icon.png --out icon.iconset/icon_512x512.png
sips -z 256 256 static/icon.png --out icon.iconset/icon_256x256.png
iconutil -c icns icon.iconset -o packaging/macos/icon.icns
```

## Signing & notarization

For distribution outside Gatekeeper, sign with a Developer ID and notarize:

```sh
codesign --deep --force --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  packaging/dist/Headrouter.app

# package into a dmg/zip, then:
xcrun notarytool submit Headrouter.zip \
  --apple-id you@example.com --team-id TEAMID --password app-specific-password \
  --wait
xcrun stapler staple packaging/dist/Headrouter.app
```

See the commented section at the bottom of `build-app.sh`.
