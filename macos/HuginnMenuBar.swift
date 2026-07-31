import AppKit
import Foundation

private struct SessionEnvelope: Decodable {
    let sessions: [AgentSession]
    let attention: Int
}

private struct AgentSession: Decodable {
    let key: String
    let name: String
    let source: String
    let state: String
    let cwd: String?
    let last_prompt: String?
}

/// How to spawn the daemon. Needed only when no daemon is live, which is
/// exactly the case a hardcoded developer path never survives (issue #37).
private struct DaemonCommand {
    let executable: URL
    let arguments: [String]
    let workingDirectory: URL
}

@main
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statePath = NSString(string: "~/.local/state/huginn").expandingTildeInPath
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var daemon: Process?
    private var timer: Timer?
    private var sessions: [AgentSession] = []
    private var attention = 0
    private var isQuitting = false
    private var refreshFailures = 0

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if let path = Bundle.main.path(forResource: "bird", ofType: "svg"),
           let bird = NSImage(contentsOfFile: path) {
            bird.isTemplate = true
            bird.size = NSSize(width: 21, height: 21)
            statusItem.button?.image = bird
        }
        statusItem.button?.imagePosition = .imageLeading
        statusItem.menu = menu
        menu.delegate = self
        rebuildMenu()
        ensureDaemon()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refreshSessions()
        }
        refreshSessions(after: 0.8)
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        if !isQuitting { stopDaemon() }
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshSessions()
    }

    private var tokenPath: String { statePath + "/token" }
    private var portPath: String { statePath + "/port" }
    private var daemonStatePath: String { statePath + "/daemon.json" }

    private func ensureDaemon() {
        if daemon?.isRunning == true { return }
        if liveDaemonPID() != nil {
            refreshSessions()
            return
        }
        guard let command = resolveDaemonCommand() else {
            rebuildMenu(error: "Could not locate Huginn install")
            return
        }
        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        process.currentDirectoryURL = command.workingDirectory

        let logURL = URL(fileURLWithPath: statePath + "/menubar.log")
        try? FileManager.default.createDirectory(at: URL(fileURLWithPath: statePath),
                                                 withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            _ = try? log.seekToEnd()
            process.standardOutput = log
            process.standardError = log
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.isQuitting else { return }
                self.daemon = nil
                self.rebuildMenu(error: "Daemon stopped")
            }
        }
        do {
            try process.run()
            daemon = process
        } catch {
            rebuildMenu(error: "Could not start daemon")
        }
    }

    /// Where to find an interpreter that can run `huginn.cli`, in order of how
    /// much we trust it. The old code hardcoded one developer's checkout
    /// (issue #37), which meant a clean machine could never spawn the daemon --
    /// the bug hid because a daemon was normally already running.
    ///
    /// 1. A runtime bundled inside the .app: self-contained, needs no checkout.
    /// 2. A checkout enclosing the .app: true for a build left in `dist/`.
    /// 3. `daemon.json`, which the daemon now stamps with the interpreter and
    ///    root it is itself running from -- the only source that is known to
    ///    have worked, and the one that follows a moved checkout.
    /// 4. The build-time `HuginnRepoPath`, correct only while the checkout that
    ///    built this bundle stays put -- hence last among path-bearing sources.
    /// 5. A `huginn` console script in the usual user/Homebrew prefixes. A GUI
    ///    app inherits a minimal PATH, so these are probed by absolute path.
    private func resolveDaemonCommand() -> DaemonCommand? {
        if let bundled = pythonCommand(inRoot: Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/runtime")) {
            return bundled
        }
        var ancestor = Bundle.main.bundleURL.deletingLastPathComponent()
        while ancestor.path != "/" {
            if FileManager.default.fileExists(atPath: ancestor
                .appendingPathComponent("huginn/cli.py").path),
               let command = pythonCommand(inRoot: ancestor) {
                return command
            }
            ancestor = ancestor.deletingLastPathComponent()
        }
        if let recorded = recordedDaemonCommand() { return recorded }
        if let buildTime = Bundle.main.infoDictionary?["HuginnRepoPath"] as? String,
           !buildTime.isEmpty,
           let command = pythonCommand(inRoot: URL(fileURLWithPath: buildTime)) {
            return command
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        for script in [home.appendingPathComponent(".local/bin/huginn"),
                       URL(fileURLWithPath: "/opt/homebrew/bin/huginn"),
                       URL(fileURLWithPath: "/usr/local/bin/huginn")]
            where FileManager.default.isExecutableFile(atPath: script.path) {
            return DaemonCommand(executable: script,
                                 arguments: ["serve", "--no-open"],
                                 workingDirectory: home)
        }
        return nil
    }

    /// A `.venv`/`bin` interpreter under `root`, if one is actually there.
    /// Validating beats trusting any recorded string: a stale path is exactly
    /// the failure mode issue #37 is about.
    private func pythonCommand(inRoot root: URL) -> DaemonCommand? {
        for relative in [".venv/bin/python3", "bin/python3"] {
            let python = root.appendingPathComponent(relative)
            if FileManager.default.isExecutableFile(atPath: python.path) {
                return command(python: python, root: root)
            }
        }
        return nil
    }

    private func command(python: URL, root: URL) -> DaemonCommand {
        var isDirectory: ObjCBool = false
        let usable = FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory)
            && isDirectory.boolValue
        return DaemonCommand(
            executable: python,
            arguments: ["-m", "huginn.cli", "serve", "--no-open"],
            // `python -m huginn.cli` resolves the package by install, not by
            // cwd, so a non-checkout root (site-packages) is no reason to fail.
            workingDirectory: usable ? root : FileManager.default.homeDirectoryForCurrentUser)
    }

    /// The interpreter and root the last daemon ran from, as recorded in
    /// `daemon.json`. Absent for a daemon predating that field, hence optional.
    private func recordedDaemonCommand() -> DaemonCommand? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: daemonStatePath)),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let python = object["python"] as? String, !python.isEmpty,
              FileManager.default.isExecutableFile(atPath: python) else { return nil }
        let root = (object["repo"] as? String).flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
        return command(python: URL(fileURLWithPath: python),
                       root: root ?? FileManager.default.homeDirectoryForCurrentUser)
    }

    private func liveDaemonPID() -> pid_t? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: daemonStatePath)),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let number = object["pid"] as? NSNumber else { return nil }
        let pid = pid_t(number.intValue)
        return kill(pid, 0) == 0 ? pid : nil
    }

    private func stopDaemon() {
        if let pid = liveDaemonPID() {
            kill(pid, SIGTERM)
            for _ in 0..<20 {
                if kill(pid, 0) != 0 { break }
                usleep(50_000)
            }
            // A long-lived SSE request can keep Uvicorn draining after it has
            // closed the listening socket. Do not leave a live-but-unusable
            // PID that ensureDaemon() will mistake for a healthy server.
            if kill(pid, 0) == 0 {
                kill(pid, SIGKILL)
                for _ in 0..<20 {
                    if kill(pid, 0) != 0 { break }
                    usleep(50_000)
                }
            }
        } else if let daemon, daemon.isRunning {
            daemon.terminate()
        }
    }

    private func refreshSessions(after delay: TimeInterval = 0) {
        if delay > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.refreshSessions() }
            return
        }
        guard let token = try? String(contentsOfFile: tokenPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let port = try? String(contentsOfFile: portPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: "http://127.0.0.1:\(port)/api/sessions") else {
            ensureDaemon()
            return
        }
        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: "X-Huginn-Token")
        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let data, error == nil,
                  let envelope = try? JSONDecoder().decode(SessionEnvelope.self, from: data) else {
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.refreshFailures += 1
                    if self.refreshFailures >= 3 {
                        self.refreshFailures = 0
                        self.restartDaemon()
                    } else {
                        self.ensureDaemon()
                    }
                }
                return
            }
            DispatchQueue.main.async {
                self?.refreshFailures = 0
                self?.sessions = envelope.sessions
                self?.attention = envelope.attention
                self?.rebuildMenu()
            }
        }.resume()
    }

    private func rebuildMenu(error: String? = nil) {
        menu.removeAllItems()
        let needsAttention = sessions.filter {
            ["waiting_permission", "waiting_input", "error"].contains($0.state)
        }
        statusItem.button?.title = attention > 0 ? " \(attention)" : ""

        if let error {
            let item = NSMenuItem(title: error, action: #selector(restartDaemon), keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        } else if needsAttention.isEmpty {
            let item = NSMenuItem(title: "No agents need attention", action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        } else {
            let heading = NSMenuItem(title: "Needs attention", action: nil, keyEquivalent: "")
            heading.isEnabled = false
            menu.addItem(heading)
            for session in needsAttention {
                let marker = session.state == "waiting_permission" ? "Approve" :
                    (session.state == "error" ? "Error" : "Reply")
                let title = "\(marker): \(session.name)"
                let item = NSMenuItem(title: title, action: #selector(focusAgent(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = session.key
                let context = session.last_prompt?.trimmingCharacters(in: .whitespacesAndNewlines)
                item.toolTip = context?.isEmpty == false ? context : session.cwd
                menu.addItem(item)
            }
        }

        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Console", action: #selector(openConsole), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        let demo = NSMenuItem(title: "Open Demo Console", action: #selector(openDemo), keyEquivalent: "")
        demo.target = self
        menu.addItem(demo)
        let refresh = NSMenuItem(title: "Refresh", action: #selector(refreshNow), keyEquivalent: "r")
        refresh.target = self
        menu.addItem(refresh)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Huginn", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        let restart = NSMenuItem(
            title: "Restart Huginn", action: #selector(restartDaemon), keyEquivalent: "q")
        restart.target = self
        restart.isAlternate = true
        restart.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(restart)
    }

    @objc private func refreshNow() { refreshSessions() }

    @objc private func restartDaemon() {
        stopDaemon()
        daemon = nil
        ensureDaemon()
        refreshSessions(after: 0.8)
    }

    @objc private func openConsole() {
        guard let token = try? String(contentsOfFile: tokenPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let port = try? String(contentsOfFile: portPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: "http://127.0.0.1:\(port)/#t=\(token)") else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func openDemo() {
        guard let port = try? String(contentsOfFile: portPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: "http://127.0.0.1:\(port)/?demo=1") else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func focusAgent(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String,
              let token = try? String(contentsOfFile: tokenPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let port = try? String(contentsOfFile: portPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              let escaped = key.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "http://127.0.0.1:\(port)/api/sessions/\(escaped)/focus") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(token, forHTTPHeaderField: "X-Huginn-Token")
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func quitApp() {
        isQuitting = true
        timer?.invalidate()
        stopDaemon()
        NSApp.terminate(nil)
    }
}
