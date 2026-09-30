#!/bin/bash
# Build Polypress.app: one window around the C program.
#
#     ./app/build_app.sh           # -> ~/Applications/Polypress.app
#     ./app/build_app.sh dmg       # also dist/Polypress.dmg, to hand to someone
#     DEST=/some/dir ./app/build_app.sh     # build somewhere else
#
# What goes in the bundle:
#   Contents/MacOS/Polypress        the window (PolypressApp.swift)
#   Contents/Resources/polypress    the program (csrc/, liblzma linked in)
#   Contents/Resources/page/        the page the window draws, with its fonts
#   Contents/Resources/parquet.py   Parquet in and out, used only when the
#                                   system Python has pyarrow
#
# The app reads ~/Desktop only because it asks for the files the user drops
# or picks, so it needs no special permission; it installs to ~/Applications
# like the old one did. Nothing is ever opened on screen by this script.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ACTION="${1:-}"
DEST="${DEST:-$HOME/Applications}"
APP="$DEST/Polypress.app"
RES="$APP/Contents/Resources"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v swiftc >/dev/null || { echo "needs swiftc: xcode-select --install" >&2; exit 1; }

# ---- the program, then the window ------------------------------------------
"$ROOT/csrc/build.sh" >/dev/null
swiftc -swift-version 5 -O -target "$(uname -m)-apple-macos11.0" \
    -framework AppKit -framework WebKit \
    -o "$TMP/Polypress" "$HERE/PolypressApp.swift"

# ---- gate: every action, on real files, before anything is installed ------
if ! POLYPRESS="$ROOT/csrc/polypress" POLYPRESS_PARQUET="$ROOT/py/parquet.py" \
        "$TMP/Polypress" --selftest; then
  echo "self-test failed; not building" >&2
  exit 1
fi

# ---- the bundle ------------------------------------------------------------
mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"
cp "$TMP/Polypress" "$APP/Contents/MacOS/Polypress"
cp "$ROOT/csrc/polypress" "$ROOT/py/parquet.py" "$RES/"
cp -R "$HERE/page" "$RES/page"

if osascript -l JavaScript "$HERE/make_icon.js" "$TMP/icon.png" >/dev/null 2>&1; then
  SET="$TMP/Polypress.iconset"
  mkdir -p "$SET"
  for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" "128 128x128" \
              "256 128x128@2x" "256 256x256" "512 256x256@2x" "512 512x512" \
              "1024 512x512@2x"; do
    px="${spec%% *}"; name="${spec##* }"
    sips -z "$px" "$px" "$TMP/icon.png" --out "$SET/icon_$name.png" >/dev/null 2>&1
  done
  iconutil -c icns "$SET" -o "$RES/Polypress.icns" >/dev/null 2>&1 || true
fi

VERSION="$("$ROOT/csrc/polypress" --version | awk '{print $2}')"
# Finder hands us .ppz (and the pre-rename .tcz) on double-click, and offers
# us in "Open With" for the tables we can compress.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key><string>Polypress</string>
	<key>CFBundleDisplayName</key><string>Polypress</string>
	<key>CFBundleIdentifier</key><string>local.polypress</string>
	<key>CFBundleExecutable</key><string>Polypress</string>
	<key>CFBundleIconFile</key><string>Polypress</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleVersion</key><string>$VERSION</string>
	<key>CFBundleShortVersionString</key><string>$VERSION</string>
	<key>LSMinimumSystemVersion</key><string>11.0</string>
	<key>NSHighResolutionCapable</key><true/>
	<key>CFBundleDocumentTypes</key>
	<array>
		<dict>
			<key>CFBundleTypeName</key><string>Polypress Archive</string>
			<key>CFBundleTypeRole</key><string>Editor</string>
			<key>LSHandlerRank</key><string>Owner</string>
			<key>CFBundleTypeExtensions</key><array><string>ppz</string><string>tcz</string></array>
		</dict>
		<dict>
			<key>CFBundleTypeName</key><string>Data Table</string>
			<key>CFBundleTypeRole</key><string>Viewer</string>
			<key>LSHandlerRank</key><string>Alternate</string>
			<key>CFBundleTypeExtensions</key><array>
				<string>csv</string><string>tsv</string><string>psv</string><string>txt</string>
				<string>json</string><string>jsonl</string><string>ndjson</string><string>parquet</string>
			</array>
		</dict>
	</array>
</dict>
</plist>
PLIST
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Ad-hoc signature, last: anything that edits the bundle after signing breaks
# the seal, and a broken seal is reported as "damaged" where an unsigned app
# only gets "unidentified developer".
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "warning: could not sign $APP" >&2

# So double-clicking a .ppz opens this build now, not whatever was there before.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true
touch "$APP"
echo "Built $APP"

# ---- dmg -------------------------------------------------------------------
if [ "$ACTION" = "dmg" ]; then
  OUT="${DMG_OUT:-$ROOT/dist}"
  mkdir -p "$OUT"
  STAGE="$TMP/stage"
  mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  cat > "$STAGE/READ ME FIRST.txt" <<'NOTE'
Polypress
=========

Drag Polypress.app onto the Applications folder shown here.

THE FIRST TIME YOU OPEN IT, macOS WILL REFUSE.
It will say Polypress "cannot be opened because it is from an unidentified
developer", or that it is damaged. That is macOS Gatekeeper, and it says this
about every app not signed with a paid Apple Developer certificate.

To open it anyway:

  Right-click (or Control-click) Polypress.app  ->  Open  ->  Open

You only have to do this once.

Using it
--------
Drop a table on the window (CSV, TSV, JSON, JSON Lines, or Parquet if your
Python has pyarrow) and it is compressed into a .ppz next to the original.
Drop a .ppz and it is restored next to itself. Hover over a line for "show"
and for "as csv / tsv / json / jsonl / parquet", which writes the same table
in another format. Double-clicking a .ppz in Finder works too.

Nothing is written until the compressed file has been decompressed again and
compared with the original, cell for cell.
NOTE
  rm -f "$OUT/Polypress.dmg"
  hdiutil create -volname "Polypress" -srcfolder "$STAGE" -ov -format UDZO \
      "$OUT/Polypress.dmg" >/dev/null
  echo "Built $OUT/Polypress.dmg"
fi
