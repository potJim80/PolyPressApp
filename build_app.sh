#!/bin/bash
# Build TableZip.app -- a real Mac bundle that shows up in the Dock.
#
# Two things macOS forces on us:
#
#   * The bundle is SELF-CONTAINED. The launcher cannot read source files out
#     of a folder like ~/Desktop, because an unsigned app has no Desktop
#     permission and gets "Operation not permitted". So the Python sources
#     are copied into Contents/Resources.
#
#   * It installs to ~/Applications, which is not privacy-protected. Files the
#     user picks through the open/save panel are granted individually, so the
#     app can still read any table they choose.
#
# The C library is compiled at build time, so the app never needs a compiler.
# Re-run this after changing any source file.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${DEST:-$HOME/Applications}"
APP="$DEST/TableZip.app"
PY="${PYTHON:-/usr/bin/python3}"
RES="$APP/Contents/Resources"

mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

# ---- payload --------------------------------------------------------------
cp "$HERE"/gui.py "$HERE"/fast.py "$HERE"/caccel.py "$HERE"/dtz.py \
   "$HERE"/codec.py "$HERE"/tcz.c "$RES/"

# Compile the accelerator now so the running app never shells out to cc.
if command -v cc >/dev/null; then
  cc -O3 -shared -fPIC -o "$RES/libtcz.so" "$HERE/tcz.c" 2>/dev/null || true
fi

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

cat > "$APP/Contents/MacOS/TableZip" <<'LAUNCH'
#!/bin/bash
# Errors before Tk is up would otherwise vanish silently.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/TableZip.log"

# /usr/bin/python3 is universal, and LaunchServices may start it as x86_64
# even on Apple Silicon. Site-packages (numpy) are built for the native arch,
# so a translated launch dies with "incompatible architecture". Pin it.
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  exec /usr/bin/arch -arm64 /usr/bin/python3 "$RES/gui.py" 2>>"$LOG"
fi
exec /usr/bin/python3 "$RES/gui.py" 2>>"$LOG"
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
    iconutil -c icns "$SET" -o "$RES/TableZip.icns" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
fi

touch "$APP"
echo "Built $APP"
echo
echo "  open '$APP'"
echo "  then right-click its Dock icon -> Options -> Keep in Dock"
