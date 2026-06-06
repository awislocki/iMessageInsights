# iMessage Insights — macOS menu-bar app

A native menu-bar (sys-tray) wrapper around the local server in this repo. It puts a
💬 icon in your menu bar, launches `server.py` in the background, and shows the UI in
a native window (WKWebView). Same functionality as the CLI version — just packaged as
a double-click app.

**Why a wrapper?** It reuses `server.py` 100% (no rewrite), and **Full Disk Access is
granted to this app** instead of your terminal — cleaner and more permanent.

## Build

Requires the Xcode command-line tools (`swiftc`) — already present on most dev Macs.

```bash
cd macos-app
./build.sh
```

This produces **`iMessage Insights.app`** in this folder.

## Install / run

1. `open "iMessage Insights.app"` (or double-click it). A 💬 icon appears in the menu bar.
2. **System Settings → Privacy & Security → Full Disk Access → enable “iMessage Insights”.**
3. Quit the app (menu bar → Quit) and reopen it.
4. Menu bar → **Open iMessage Insights** for the window, or **Open in Browser**.
   **Use Demo Data** restarts it on fictional data (no DB/key/vault needed).

Want it in `/Applications`? Drag the `.app` there.

## How it works

- On launch it spawns `python3 .../server.py --no-browser --port 8770` (the script is
  bundled in `Contents/Resources/`), waits for it to respond, then loads
  `http://localhost:8770/` in a `WKWebView` window.
- The app runs as a menu-bar **agent** (`LSUIElement`) — no Dock icon.
- Quitting the app terminates the server.
- Uses port **8770** so it won't collide with the CLI (`8765`).

## Runtime requirements

- macOS 11+
- `python3` available at `/usr/bin/python3` (Command Line Tools) or Homebrew. The app
  finds it automatically. (Only the Python **standard library** is used — no pip installs.)

## Distributing to other people

The build ad-hoc-signs the app so it runs on **your** Mac. To hand it to others without
Gatekeeper warnings, sign + notarize with an Apple Developer ID:

```bash
codesign --force --deep --options runtime --sign "Developer ID Application: <you>" "iMessage Insights.app"
xcrun notarytool submit ... && xcrun stapler staple "iMessage Insights.app"
```

For personal use, ad-hoc signing (the default in `build.sh`) is enough.
