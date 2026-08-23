#!/bin/bash
# Build the app, replace the installed copy, and open it.
#
# One command, because a half-updated app is worse than no update: the old copy
# is quit and deleted before the new one is written, so there is never a moment
# where the version in /Applications and the version you are looking at differ.
#
# Xcode is not installed on this machine, only the Command Line Tools, so there
# is no .xcodeproj. That is fine: SwiftPM compiles the binary and an app bundle
# is a folder with an Info.plist in it. This script is the whole difference.
set -euo pipefail
cd "$(dirname "$0")"

NAME="Handrail"
BUNDLE_ID="com.handrail.app"
CONFIG="${1:-release}"
STAGED="build/$NAME.app"
INSTALLED="/Applications/$NAME.app"

echo "Building $NAME ($CONFIG)…"
swift build -c "$CONFIG"
BIN="$(swift build -c "$CONFIG" --show-bin-path)/$NAME"

# --- assemble the bundle ---------------------------------------------------
rm -rf "$STAGED"
mkdir -p "$STAGED/Contents/MacOS" "$STAGED/Contents/Resources"
cp "$BIN" "$STAGED/Contents/MacOS/$NAME"

cat > "$STAGED/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>$NAME</string>
  <key>CFBundleDisplayName</key>       <string>$NAME</string>
  <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>        <string>$NAME</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key>           <string>$(date +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key>    <string>14.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <!-- A regular app: it has a window AND a menu bar item, so it is not an
       accessory. LSUIElement would hide it from the Dock and Cmd-Tab. -->
  <key>LSApplicationCategoryType</key> <string>public.app-category.developer-tools</string>
</dict>
</plist>
PLIST

# Ad-hoc signing. Enough for an app you run yourself; not notarised, and not
# meant to be handed to strangers.
codesign --force --sign - "$STAGED" 2>/dev/null

# --- replace the running copy ----------------------------------------------
if pgrep -f "$INSTALLED/Contents/MacOS/$NAME" >/dev/null 2>&1; then
  echo "Quitting the running copy…"
  osascript -e "tell application \"$NAME\" to quit" 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -f "$INSTALLED/Contents/MacOS/$NAME" >/dev/null 2>&1 || break
    sleep 0.25
  done
  pkill -f "$INSTALLED/Contents/MacOS/$NAME" 2>/dev/null || true
fi

rm -rf "$INSTALLED"
cp -R "$STAGED" "$INSTALLED"

# macOS caches what it knows about an app by bundle id. Without this, a rebuilt
# app can keep showing the old icon and name until the Finder is restarted.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$INSTALLED" >/dev/null 2>&1 || true

echo "Installed $INSTALLED"
open "$INSTALLED"
