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

@main
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let repoPath = "/Users/hljod/Projects/huginn"
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
        let process = Process()
        process.executableURL = URL(fileURLWithPath: repoPath + "/.venv/bin/python3")
        process.arguments = ["-m", "huginn.cli", "serve", "--no-open"]
        process.currentDirectoryURL = URL(fileURLWithPath: repoPath)

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
