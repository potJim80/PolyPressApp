#!/bin/bash
# Build TableZip.app -- a real Mac bundle that shows up in the Dock, and
# that you can double-click a .tcz onto.
#
# It is an AppleScript applet, not a plain shell wrapper, for one reason:
# only an applet receives the `on open` Apple Event that Finder sends when
# you double-click or drop a document. A shell launcher never sees the path.
#
# Three things macOS forces on us:
#
#   * SELF-CONTAINED. An unsigned app has no permission to read ~/Desktop and
#     dies with "Operation not permitted", so the Python sources are copied
#     into Contents/Resources and it installs to ~/Applications.
#
#   * NATIVE ARCH. /usr/bin/python3 is universal and LaunchServices may start
#     it as x86_64 while site-packages are arm64. run.sh pins it.
#
#   * The C library is compiled at build time, so the app never needs cc.
#
# Re-run after changing any source file.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${DEST:-$HOME/Applications}"
APP="$DEST/TableZip.app"
PY="${PYTHON:-/usr/bin/python3}"
RES="$APP/Contents/Resources"

# ---- gate: every AppleScript we can generate must compile ------------------
if ! "$PY" "$HERE/gui.py" --selftest; then
  echo "gui.py --selftest failed; not building" >&2
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$APP"

# ---- the applet ------------------------------------------------------------
TMP="$(mktemp -d)"
cat > "$TMP/applet.applescript" <<'APPLET'
on run
	tzRun("")
end run

on open theFiles
	repeat with f in theFiles
		tzRun(POSIX path of (f as text))
	end repeat
end open

on tzRun(p)
	set res to (POSIX path of (path to me)) & "Contents/Resources/"
	set cmd to quoted form of (res & "run.sh")
	if p is not "" then set cmd to cmd & " " & quoted form of p
	-- Big tables take minutes; the default two-minute AppleScript timeout
	-- would abandon the job halfway through.
	with timeout of 36000 seconds
		do shell script cmd
	end timeout
end tzRun
APPLET
osacompile -o "$APP" "$TMP/applet.applescript"

# ---- payload ---------------------------------------------------------------
cp "$HERE"/gui.py "$HERE"/fast.py "$HERE"/caccel.py "$HERE"/dtz.py \
   "$HERE"/codec.py "$HERE"/tzip.py "$HERE"/tcz.c "$RES/"

if command -v cc >/dev/null; then
  cc -O3 -shared -fPIC -o "$RES/libtcz.so" "$HERE/tcz.c" 2>/dev/null || true
fi

cat > "$RES/run.sh" <<'RUN'
#!/bin/bash
RES="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/TableZip.log"
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  exec /usr/bin/arch -arm64 /usr/bin/python3 "$RES/gui.py" "$@" 2>>"$LOG"
fi
exec /usr/bin/python3 "$RES/gui.py" "$@" 2>>"$LOG"
RUN
chmod +x "$RES/run.sh"

# ---- identity and document types -------------------------------------------
PL="$APP/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleName TableZip" "$PL" 2>/dev/null || \
  $PB -c "Add :CFBundleName string TableZip" "$PL"
$PB -c "Add :CFBundleDisplayName string TableZip" "$PL" 2>/dev/null || true
$PB -c "Set :CFBundleIdentifier local.tablezip" "$PL" 2>/dev/null || \
  $PB -c "Add :CFBundleIdentifier string local.tablezip" "$PL"
$PB -c "Add :NSHighResolutionCapable bool true" "$PL" 2>/dev/null || true

# Tell Finder we own .tcz, so double-click and "Open With" work.
$PB -c "Delete :CFBundleDocumentTypes" "$PL" 2>/dev/null || true
$PB -c "Add :CFBundleDocumentTypes array" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0 dict" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string TableZip Archive" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Editor" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Owner" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions array" "$PL"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions:0 string tcz" "$PL"
# Second entry: tables we can compress, so "Open With" offers us there too.
$PB -c "Add :CFBundleDocumentTypes:1 dict" "$PL"
$PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeName string Data Table" "$PL"
$PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeRole string Viewer" "$PL"
$PB -c "Add :CFBundleDocumentTypes:1:LSHandlerRank string Alternate" "$PL"
$PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeExtensions array" "$PL"
i=0
for e in csv tsv psv txt dat json jsonl ndjson parquet; do
  $PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeExtensions:$i string $e" "$PL"
  i=$((i + 1))
done

# ---- icon ------------------------------------------------------------------
if command -v iconutil >/dev/null && command -v sips >/dev/null; then
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
    # osacompile names the icon applet.icns or droplet.icns depending on
    # whether the script has an `on open` handler. Ask the plist rather than
    # guessing -- guessing produced a bundle with the stock droplet icon.
    ICON="$($PB -c "Print :CFBundleIconFile" "$PL" 2>/dev/null || echo applet)"
    ICON="${ICON%.icns}"
    iconutil -c icns "$SET" -o "$RES/$ICON.icns" >/dev/null 2>&1 || true
  fi
fi
rm -rf "$TMP"

# Nudge LaunchServices so the .tcz association takes effect now.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true
touch "$APP"

echo "Built $APP"
echo
echo "  open '$APP'                  compress or restore via dialogs"
echo "  double-click any .tcz        restores it"
echo "  drop files on the Dock icon  same thing"
echo "  right-click Dock icon -> Options -> Keep in Dock"
