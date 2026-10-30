import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class Store: ObservableObject {
    @Published private(set) var sessions: [Session] = []
    @Published private(set) var ghostty: GhosttyStatus?
    @Published private(set) var connected = false
    @Published private(set) var hooks = "installed"
    @Published private(set) var openAtLogin = SMAppService.mainApp.status == .enabled
    @Published var toast: String?
    @Published var now = Date()
    /// Open the panel by itself when a session starts needing you.
    @Published var popsUp = UserDefaults.standard.object(forKey: "popsUp") as? Bool ?? true {
        didSet { UserDefaults.standard.set(popsUp, forKey: "popsUp") }
    }
    /// Ticks clicked but not yet in a snapshot, so the box does not flicker back.
    @Published private var ticks: [String: Bool] = [:]
    /// Pickers answered from the panel, by tool_use id: locked until the snapshot drops them,
    /// since a second set of keys would land on whatever the terminal shows next.
    @Published private(set) var answered: [String: [[Int]]] = [:]

    private var server: Process?
    private var buffer = Data()
    private var clock: Timer?

    init(live: Bool = true) {
        guard live else { return }
        startServer()
        refreshHooks()
        clock = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date() }
        }
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.server?.terminate() }
        }
    }

    // MARK: Live data

    private func startServer() {
        let p = Backend.process(["serve"])
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in self?.receive(data) }
        }
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.connected = false
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self?.startServer()
            }
        }
        do {
            try p.run()
            server = p
        } catch {
            connected = false
        }
    }

    private func receive(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<newline)
            buffer = buffer.subdata(in: buffer.index(after: newline)..<buffer.endIndex)
            if let snapshot = Snapshot.decode(line) { show(snapshot) }
        }
    }

    func show(_ snapshot: Snapshot) {
        sessions = snapshot.sessions
        if ghostty != snapshot.ghostty { ghostty = snapshot.ghostty }
        for s in sessions {
            for item in s.followups where ticks[tickKey(s, item)] == (item.done ?? false) {
                ticks[tickKey(s, item)] = nil
            }
        }
        let waiting = Set(sessions.compactMap { $0.picker?.id })
        for id in answered.keys where !waiting.contains(id) { answered[id] = nil }
        connected = true
        now = Date()
    }

    func rows(_ group: SessionGroup) -> [Session] {
        let rows = sessions.filter { $0.group == group.rawValue }
        // Longest wait first for what blocks on you, most recent first elsewhere.
        return group == .needsYou ? rows.sorted { ($0.since ?? 0) < ($1.since ?? 0) } : rows
    }

    /// Folder labels only help when sessions run from different folders.
    var showsFolders: Bool { Set(sessions.map(\.folder)).count > 1 }

    // MARK: Actions

    func focus(_ s: Session) { perform(["focus", String(s.pid)], done: nil) }
    func reply(_ s: Session, _ text: String) { perform(["reply", String(s.pid)], input: text, done: "Sent to \(s.name)") }
    func clear(_ s: Session) { perform(["clear", String(s.pid)], done: "Cleared \(s.name)") }

    /// Answers the session's picker: per question, the indexes of the options chosen.
    func answer(_ s: Session, _ picker: Picker, _ answers: [[Int]]) {
        guard answered[picker.id] == nil,
              let data = try? JSONSerialization.data(withJSONObject: ["id": picker.id, "answers": answers] as [String: Any]),
              let json = String(data: data, encoding: .utf8) else { return }
        answered[picker.id] = answers
        perform(["answer", String(s.pid)], input: json, done: "Answered \(s.name)") { [weak self] in
            self?.answered[picker.id] = nil
        }
    }
    func close(_ s: Session) { perform(["close", String(s.pid)], done: "Closed \(s.name)") }

    /// Quits and reopens Ghostty: tabs come back in their folders, Claude sessions resumed.
    func restartGhostty(force: Bool) {
        say("Restarting Ghostty…")
        Task {
            let result = await Backend.run(["restart-ghostty"] + (force ? ["--force"] : []))
            let error = result.error.components(separatedBy: "\n").last(where: { !$0.isEmpty }) ?? "Ghostty restart failed"
            say(result.ok ? result.output : error)
            if !result.ok, error.contains("Automation"), let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private func tickKey(_ s: Session, _ item: Followup) -> String { s.sid + "\n" + item.text }

    func isChecked(_ s: Session, _ item: Followup) -> Bool { ticks[tickKey(s, item)] ?? item.done ?? false }

    func toggle(_ s: Session, _ item: Followup) {
        let key = tickKey(s, item)
        let done = !isChecked(s, item)
        ticks[key] = done
        Task {
            let result = await Backend.run([done ? "check" : "uncheck", s.sid], input: item.text)
            if !result.ok {
                ticks[key] = nil
                say(result.error.isEmpty ? "Could not save the tick" : result.error)
            }
        }
    }

    private func perform(_ args: [String], input: String? = nil, done: String?, failed: (() -> Void)? = nil) {
        Task {
            let result = await Backend.run(args, input: input)
            if !result.ok {
                failed?()
                let message = result.error.components(separatedBy: "\n").last(where: { !$0.isEmpty }) ?? "Failed"
                say(message)
                if message.contains("Automation"), let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation") {
                    NSWorkspace.shared.open(url)
                }
            } else if let done {
                say(done)
            }
        }
    }

    func say(_ text: String) {
        toast = text
        Task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            if toast == text { toast = nil }
        }
    }

    // MARK: Settings

    func refreshHooks() {
        Task {
            let result = await Backend.run(["hooks-status"])
            hooks = result.ok ? result.output : "missing"
        }
    }

    func installHooks() {
        Task {
            let result = await Backend.run(["install-hooks"])
            say(result.ok ? "Hooks installed. New turns get judged." : result.error)
            refreshHooks()
        }
    }

    func removeHooks() {
        Task {
            let result = await Backend.run(["uninstall-hooks"])
            say(result.ok ? "Hooks removed" : result.error)
            refreshHooks()
        }
    }

    func toggleOpenAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            say("Open at login failed: \(error.localizedDescription)")
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}
