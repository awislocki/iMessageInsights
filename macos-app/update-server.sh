#!/bin/bash
# Update the installed menu-bar app's server WITHOUT rebuilding the app.
# The app prefers ~/.imessage-export/server.py over its bundled copy, so this
# never changes the app's signature — Full Disk Access stays granted.
# Usage: ./update-server.sh     (from macos-app/, after git pull)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ~/.imessage-export
cp ../server.py ~/.imessage-export/server.py
echo "✓ server updated at ~/.imessage-export/server.py"
echo "Restart the app (menu bar 💬 → Quit, reopen) to pick it up."
