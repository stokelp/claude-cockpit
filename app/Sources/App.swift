import AppKit
import Carbon.HIToolbox
import Combine
import SwiftUI

/// The global shortcut that opens the panel, shown in its footer.
enum Shortcut {
    static let keyCode = UInt32(kVK_ANSI_C)
    static let modifiers = UInt32(controlKey | optionKey | cmdKey)
    static let label = "⌃⌥⌘C"
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private let store = Store()
    private var item: NSStatusItem!
    private let popover = NSPopover()
    private var anchor: NSWindow?
    private var hotKey: HotKey?
    private var watchers = Set<AnyCancellable>()
    /// Sessions in Needs you at the last snapshot; nil until the first one arrives.
    private var needing: Set<String>?
    /// The panel opened by itself and nobody has touched it since.
    private var autoOpened = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = "ClaudeCockpit"
        item.button?.target = self
        item.button?.action = #selector(toggle)
        item.button?.imagePosition = .imageLeading
        updateButton(needsYou: 0)
        store.$sessions
            .map { $0.filter { $0.group == SessionGroup.needsYou.rawValue }.count }
            .removeDuplicates()
            .receive(on: RunLoop.main)
            .sink { [weak self] count in self?.updateButton(needsYou: count) }
            .store(in: &watchers)
        store.$sessions
            .dropFirst()  // the empty list from before the first snapshot
            .receive(on: RunLoop.main)
            .sink { [weak self] sessions in self?.watchNeeds(sessions) }
            .store(in: &watchers)

        let host = NSHostingController(rootView: CockpitView(shortcut: Shortcut.label).environmentObject(store))
        host.sizingOptions = .preferredContentSize
        popover.contentViewController = host
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self

        hotKey = HotKey(keyCode: Shortcut.keyCode, modifiers: Shortcut.modifiers) { [weak self] in self?.toggle() }
    }

    private func updateButton(needsYou count: Int) {
        let image = NSImage(systemSymbolName: count > 0 ? "exclamationmark.bubble.fill" : "rectangle.stack", accessibilityDescription: "Claude Cockpit")
        image?.isTemplate = true
        item.button?.image = image
        item.button?.title = count > 0 ? " \(count)" : ""
        item.button?.toolTip = "Claude Cockpit (\(Shortcut.label))"
    }

    @objc func toggle() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        show(activate: true)
    }

    /// Opened by itself, the panel leaves the keyboard where it is, so typing goes on.
    private func show(activate: Bool) {
        autoOpened = !activate
        if activate { NSApp.activate(ignoringOtherApps: true) }
        if let button = item.button, iconIsVisible(button) {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        } else if let view = fallbackAnchor().contentView {
            popover.show(relativeTo: view.bounds, of: view, preferredEdge: .minY)
        }
        guard let window = popover.contentViewController?.view.window else { return }
        if activate {
            window.makeKey()
        } else {
            // An app in the background gets its windows ordered behind the active app's.
            window.level = .statusBar
            window.orderFrontRegardless()
        }
    }

    /// Opens the panel when a session starts needing you, unless its tab is the one in front of you.
    /// Closes it again once nothing needs you, if it opened by itself.
    private func watchNeeds(_ sessions: [Session]) {
        let rows = sessions.filter { $0.group == SessionGroup.needsYou.rawValue }
        let now = Set(rows.map(\.sid))
        defer { needing = now }
        guard let before = needing else { return }  // nothing pops at launch
        if now.isEmpty {
            if autoOpened, popover.isShown { popover.performClose(nil) }
            return
        }
        let fresh = rows.filter { !before.contains($0.sid) }
        guard !fresh.isEmpty, store.popsUp, !popover.isShown else { return }
        let pids = Set(fresh.map(\.pid))
        Task {
            let front = await Backend.run(["focused"])
            if let pid = Int(front.output), pids == [pid] { return }
            // Still waiting on you after the check, and the panel is still closed.
            guard !popover.isShown, let needing, !needing.isDisjoint(with: fresh.map(\.sid)) else { return }
            show(activate: false)
        }
    }

    /// A full menu bar hides the newest icons, behind the notch on MacBooks.
    private func iconIsVisible(_ button: NSStatusBarButton) -> Bool {
        guard let window = button.window, let screen = window.screen else { return false }
        let frame = window.frame
        if let right = screen.auxiliaryTopRightArea, screen.safeAreaInsets.top > 0 {
            return frame.minX >= right.minX && frame.maxX <= right.maxX + 1
        }
        return window.occlusionState.contains(.visible) && screen.frame.contains(frame)
    }

    /// An invisible point near the top right corner for the panel to hang from.
    private func fallbackAnchor() -> NSWindow {
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
        let visible = screen.visibleFrame
        let frame = NSRect(x: visible.maxX - 260, y: visible.maxY - 2, width: 1, height: 1)
        let window = anchor ?? {
            let w = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
            w.isOpaque = false
            w.backgroundColor = .clear
            w.level = .statusBar
            w.ignoresMouseEvents = true
            w.isReleasedWhenClosed = false
            return w
        }()
        window.setFrame(frame, display: false)
        window.orderFrontRegardless()
        anchor = window
        return window
    }

    func popoverDidClose(_ notification: Notification) {
        autoOpened = false
        anchor?.orderOut(nil)
    }
}

/// A system-wide shortcut through Carbon, which needs no accessibility permission.
final class HotKey {
    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: @MainActor () -> Void

    init(keyCode: UInt32, modifiers: UInt32, action: @escaping @MainActor () -> Void) {
        self.action = action
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            guard let userData else { return noErr }
            let me = Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue()
            MainActor.assumeIsolated { me.action() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), &handler)
        let id = EventHotKeyID(signature: OSType(0x434B_5054), id: 1)  // 'CKPT'
        RegisterEventHotKey(keyCode, modifiers, id, GetApplicationEventTarget(), 0, &ref)
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
        if let handler { RemoveEventHandler(handler) }
    }
}

/// `ClaudeCockpit --render out.png [--demo] [--dark]` draws the panel to a PNG and exits.
/// --demo uses sample sessions (for the README); without it, the live sessions.
@MainActor
func render(to path: String, demo: Bool, dark: Bool) -> Int32 {
    let p = Backend.process(demo ? ["demo"] : ["snapshot", "--no-judge"])
    let out = Pipe()
    p.standardOutput = out
    guard (try? p.run()) != nil else { return 1 }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    guard let snapshot = Snapshot.decode(data) else {
        FileHandle.standardError.write(Data("could not read the snapshot\n".utf8))
        return 1
    }
    let store = Store(live: false)
    store.show(snapshot)
    let view = CockpitView(scrolls: false, shortcut: Shortcut.label)
        .environmentObject(store)
        .background(dark ? Color(white: 0.17) : Color(white: 0.97))
        .environment(\.colorScheme, dark ? .dark : .light)
    let renderer = ImageRenderer(content: view)
    renderer.scale = 2
    guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
          let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]),
          (try? png.write(to: URL(fileURLWithPath: path))) != nil
    else {
        FileHandle.standardError.write(Data("render failed\n".utf8))
        return 1
    }
    print("wrote \(path)")
    return 0
}

@main
enum Main {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render"), i + 1 < args.count {
            let status = MainActor.assumeIsolated {
                render(to: args[i + 1], demo: args.contains("--demo"), dark: args.contains("--dark"))
            }
            exit(status)
        }
        MainActor.assumeIsolated {
            let app = NSApplication.shared
            let delegate = AppDelegate()
            app.delegate = delegate
            app.setActivationPolicy(.accessory)
            withExtendedLifetime(delegate) { app.run() }
        }
    }
}
