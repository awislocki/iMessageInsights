// iMessage Insights — native macOS menu-bar wrapper around the local Python server.
// Launches server.py (bundled in Resources), shows the UI in a WKWebView window,
// and lives in the menu bar. Full Disk Access is granted to THIS app.

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    let port = 8770          // distinct from the CLI default (8765)
    var demo = false

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)        // menu-bar agent, no Dock icon
        buildStatusItem()
        buildWindow()
        startServer()
        showWindow()
    }

    func applicationWillTerminate(_ note: Notification) { stopServer() }

    // MARK: menu bar

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            if let img = NSImage(systemSymbolName: "message.fill", accessibilityDescription: "iMessage Insights") {
                b.image = img
            } else { b.title = "💬" }
        }
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open iMessage Insights", action: #selector(showWindow), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Open in Browser", action: #selector(openBrowser), keyEquivalent: ""))
        let d = NSMenuItem(title: "Use Demo Data", action: #selector(toggleDemo(_:)), keyEquivalent: "")
        d.state = demo ? .on : .off
        menu.addItem(d)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
    }

    // MARK: server process

    func serverScript() -> String? {
        if let p = Bundle.main.path(forResource: "server", ofType: "py") { return p }
        let dev = FileManager.default.currentDirectoryPath + "/../server.py"   // dev fallback
        return FileManager.default.fileExists(atPath: dev) ? dev : nil
    }

    /// Environment for the python child. DEVELOPER_DIR points the /usr/bin shims at
    /// the Command Line Tools so an unaccepted full-Xcode license can't break them.
    func childEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        if FileManager.default.fileExists(atPath: "/Library/Developer/CommandLineTools") {
            env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"
        }
        return env
    }

    /// First python3 that actually RUNS (not just exists) — /usr/bin/python3 is a shim
    /// that can fail (e.g. Xcode license not accepted) even though it's executable.
    func pythonPath() -> String? {
        for p in ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"] {
            guard FileManager.default.isExecutableFile(atPath: p) else { continue }
            let t = Process()
            t.executableURL = URL(fileURLWithPath: p)
            t.arguments = ["-c", "pass"]
            t.environment = childEnv()
            t.standardOutput = FileHandle.nullDevice
            t.standardError = FileHandle.nullDevice
            do { try t.run(); t.waitUntilExit(); if t.terminationStatus == 0 { return p } }
            catch { continue }
        }
        return nil
    }

    func startServer() {
        guard let script = serverScript() else { alert("Could not find server.py in the app bundle."); return }
        guard let python = pythonPath() else {
            alert("No working python3 found. Install the Apple Command Line Tools "
                + "(xcode-select --install) or Homebrew Python, then relaunch.")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        var args = [script, "--no-browser", "--port", String(port)]
        if demo { args.append("--demo") }
        p.arguments = args
        p.environment = childEnv()
        p.currentDirectoryURL = URL(fileURLWithPath: (script as NSString).deletingLastPathComponent)
        p.terminationHandler = { [weak self] proc in
            guard let self = self, proc == self.server else { return }  // ignore our own restarts
            self.server = nil
            DispatchQueue.main.async {
                self.alert("The background server stopped unexpectedly "
                    + "(exit \(proc.terminationStatus)). Use the menu-bar icon to reopen, "
                    + "or check that python3 works in Terminal.")
            }
        }
        do { try p.run(); server = p } catch { alert("Failed to start server: \(error.localizedDescription)") }
    }

    func stopServer() { server?.terminate(); server = nil }

    // MARK: window / webview

    func buildWindow() {
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1240, height: 820),
                            configuration: WKWebViewConfiguration())
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "iMessage Insights"
        window.contentView = webView
        window.center()
        window.isReleasedWhenClosed = false
    }

    func loadWhenReady(_ tries: Int = 30) {
        let url = URL(string: "http://localhost:\(port)/")!
        var req = URLRequest(url: url); req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if resp is HTTPURLResponse {
                DispatchQueue.main.async { self.webView.load(URLRequest(url: url)) }
            } else if tries > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.loadWhenReady(tries - 1) }
            }
        }.resume()
    }

    // MARK: actions

    @objc func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        loadWhenReady()
    }
    @objc func openBrowser() { NSWorkspace.shared.open(URL(string: "http://localhost:\(port)/")!) }
    @objc func toggleDemo(_ sender: NSMenuItem) {
        demo.toggle(); sender.state = demo ? .on : .off
        stopServer(); startServer(); loadWhenReady()
    }
    @objc func quit() { stopServer(); NSApp.terminate(nil) }

    func alert(_ msg: String) {
        let a = NSAlert(); a.messageText = "iMessage Insights"; a.informativeText = msg
        a.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
