#!/bin/bash
# Build "iMessage Insights.app" — a menu-bar wrapper around the local server.
# Usage: ./build.sh        (run from the macos-app/ directory)
set -euo pipefail
cd "$(dirname "$0")"

APP="iMessage Insights.app"
EXE="iMessageInsights"

echo "→ compiling Swift…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -o "$APP/Contents/MacOS/$EXE" Sources/main.swift \
  -framework Cocoa -framework WebKit

echo "→ bundling server.py + Info.plist…"
cp ../server.py "$APP/Contents/Resources/server.py"
cp Info.plist  "$APP/Contents/Info.plist"

# Ad-hoc sign so it runs locally without "damaged app" warnings.
codesign --force --deep --sign - "$APP" 2>/dev/null || \
  echo "  (codesign skipped — app will still run locally)"

echo "✓ Built: $(pwd)/$APP"
echo
echo "Next:"
echo "  1) open \"$APP\"   (or double-click it in Finder)"
echo "  2) System Settings → Privacy & Security → Full Disk Access → enable 'iMessage Insights'"
echo "  3) Quit & reopen the app. Its icon (💬) sits in the menu bar."
