#!/bin/bash
# Build TableZip.app -- a real Mac bundle that shows up in the Dock.
#
# The bundle launches gui.py from this project directory, so the app stays in
# sync with the code. Re-run this after changing Info.plist or the icon; code
# changes need no rebuild.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/TableZip.app"
PY="${PYTHON:-/usr/bin/python3}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>TableZip</string>
  <key>CFBundleDisplayName</key>       <string>TableZip</string>
  <key>CFBundleIdentifier</key>        <string>local.tablezip</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleExecutable</key>        <string>TableZip</string>
  <key>CFBundleIconFile</key>          <string>TableZip</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSMinimumSystemVersion</key>    <string>10.13</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/TableZip" <<LAUNCH
#!/bin/bash
# Errors before Tk is up would otherwise vanish silently.
exec "$PY" "$HERE/gui.py" 2>>"\$HOME/Library/Logs/TableZip.log"
LAUNCH
chmod +x "$APP/Contents/MacOS/TableZip"

# ---- icon -----------------------------------------------------------------
if command -v iconutil >/dev/null && command -v sips >/dev/null; then
  TMP="$(mktemp -d)"
  "$PY" "$HERE/make_icon.py" "$TMP/icon.png" >/dev/null 2>&1 || true
  if [ -f "$TMP/icon.png" ]; then
    SET="$TMP/TableZip.iconset"
    mkdir -p "$SET"
    for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" \
                "64 icon_32x32@2x" "128 icon_128x128" "256 icon_128x128@2x" \
                "256 icon_256x256" "512 icon_256x256@2x" "512 icon_512x512" \
                "1024 icon_512x512@2x"; do
      set -- $spec
      sips -z "$1" "$1" "$TMP/icon.png" --out "$SET/$2.png" >/dev/null 2>&1
    done
    iconutil -c icns "$SET" -o "$APP/Contents/Resources/TableZip.icns" \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
fi

# Make Finder pick up the new icon and bundle metadata immediately.
touch "$APP"
echo "Built $APP"
echo
echo "  open '$APP'                 launch it"
echo "  then right-click its Dock icon -> Options -> Keep in Dock"
