#!/bin/bash
# Build the app and package it into a draggable .dmg installer.
# Usage: ./make-dmg.sh        (run from the macos-app/ directory)
set -euo pipefail
cd "$(dirname "$0")"

APP="iMessage Insights.app"
VOL="iMessage Insights"
DMG="iMessageInsights.dmg"

./build.sh

echo "→ packaging $DMG…"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"      # drag-to-Applications target
rm -f "$DMG"
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "✓ $(pwd)/$DMG"
echo
echo "Note: this is ad-hoc signed (no Apple Developer ID). After downloading,"
echo "users must clear the quarantine flag once:"
echo "    xattr -cr \"/Applications/$APP\""
