import AppKit
import SwiftUI

struct CockpitView: View {
    @EnvironmentObject var store: Store
    var scrolls = true
    var shortcut: String?
    @State private var listHeight: CGFloat = 0
    @State private var confirmingRestart = false

    /// The popover sizes itself to its content, so the list needs a definite height.
    private var maxListHeight: CGFloat {
        min(700, (NSScreen.main?.visibleFrame.height ?? 800) - 160)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if store.hooks != "installed" { hooksBar; Divider() }
            if let ghostty = store.ghostty, !ghostty.stale.isEmpty || ghostty.restarting { ghosttyBar(ghostty); Divider() }
            if store.sessions.isEmpty {
                Text(store.connected ? "No Claude Code sessions open" : "Starting…")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 90)
            } else if scrolls {
                ScrollView {
                    groups.background(GeometryReader { g in
                        Color.clear.preference(key: ListHeight.self, value: g.size.height)
                    })
                }
                .frame(height: min(max(listHeight, 60), maxListHeight))
                .onPreferenceChange(ListHeight.self) { listHeight = $0 }
            } else {
                groups
            }
            Divider()
            footer
        }
        .frame(width: 470)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Text("Claude sessions").font(.system(size: 13, weight: .semibold))
            Spacer()
            ForEach(SessionGroup.allCases) { group in
                let count = store.rows(group).count
                if count > 0 {
                    HStack(spacing: 4) {
                        Circle().fill(group.color).frame(width: 7, height: 7)
                        Text("\(count)").font(.system(size: 12, weight: .medium).monospacedDigit())
                    }
                    .help(group.title)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var hooksBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            Text(store.hooks == "outdated" ? "Claude Code hooks point to another copy" : "Claude Code hooks are not installed")
                .font(.system(size: 12))
            Spacer()
            Button("Install") { store.installHooks() }
                .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    /// A program was replaced on disk while running in Ghostty: macOS will soon refuse Ghostty
    /// access to Documents, until Ghostty restarts.
    private func ghosttyBar(_ g: GhosttyStatus) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: g.restarting ? "arrow.clockwise" : "exclamationmark.triangle.fill")
                .foregroundStyle(g.restarting ? Color.secondary : Color.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(g.restarting ? "Restarting Ghostty…" : "\(g.stale.joined(separator: ", ")) was updated while running in Ghostty")
                    .font(.system(size: 12))
                if !g.restarting {
                    Text(g.auto
                        ? "Ghostty restarts by itself, sessions resumed. \(g.waiting ?? "Starting")."
                        : "Restart that program or Ghostty soon, or Ghostty loses access to Documents.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            if !g.restarting {
                Button(confirmingRestart ? "Confirm restart" : "Restart Ghostty") {
                    if confirmingRestart {
                        confirmingRestart = false
                        store.restartGhostty(force: true)
                    } else {
                        confirmingRestart = true
                        Task {
                            try? await Task.sleep(nanoseconds: 4_000_000_000)
                            confirmingRestart = false
                        }
                    }
                }
                .controlSize(.small)
                .tint(confirmingRestart ? .red : nil)
                .help("Ends what runs in Ghostty's tabs, then reopens them in the same folders with Claude sessions resumed")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private var groups: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(SessionGroup.allCases) { group in
                let rows = store.rows(group)
                if !rows.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 6) {
                            Circle().fill(group.color).frame(width: 7, height: 7)
                            Text(group.title)
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(.secondary)
                        }
                        .padding(.leading, 2)
                        ForEach(rows) { SessionCard(session: $0, group: group) }
                    }
                }
            }
        }
        .padding(12)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if let toast = store.toast, !toast.isEmpty {
                Text(toast).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            } else if let shortcut {
                Text(shortcut)
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .help("Opens this panel from anywhere")
            }
            Spacer()
            if scrolls { settingsMenu } else { Image(systemName: "gearshape").foregroundStyle(.secondary) }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    // Offscreen renders (screenshots) cannot draw a Menu, so they get a plain icon instead.
    private var settingsMenu: some View {
        Menu {
            Toggle("Open when a session needs you", isOn: $store.popsUp)
            Toggle("Open at login", isOn: Binding(get: { store.openAtLogin }, set: { _ in store.toggleOpenAtLogin() }))
            Divider()
            Button("Restart Ghostty, resume sessions") { store.restartGhostty(force: false) }
            Divider()
            if store.hooks == "installed" {
                Button("Remove Claude Code hooks") { store.removeHooks() }
            } else {
                Button("Install Claude Code hooks") { store.installHooks() }
            }
            Divider()
            Button("Quit Claude Cockpit") { NSApplication.shared.terminate(nil) }
        } label: {
            Image(systemName: "gearshape")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
    }
}

struct SessionCard: View {
    @EnvironmentObject var store: Store
    let session: Session
    let group: SessionGroup

    @State private var confirmingClose = false
    @State private var hovering = false
    @State private var showMissingTicks = false
    /// Options picked so far, per question, before Send (pickers with several answers).
    @State private var chosen: [Int: Set<Int>] = [:]
    @State private var showMissingAnswers = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            // The whole card opens the session's tab.
            Button { store.focus(session) } label: {
                VStack(alignment: .leading, spacing: 6) {
                    title
                    content
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(session.lastPrompt.map { "Last prompt: \($0)" } ?? "Open the tab")
            if group == .needsYou, session.kind == "picker", let p = session.picker { picker(p) }
            if hasChecklist { checklist }
            if session.canReply || session.canClear || session.canClose { actions }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(hovering ? 0.085 : 0.05)))
        .onHover { hovering = $0 }
        .onChange(of: session.picker?.id) { chosen = [:] }
    }

    private var title: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if let tab = session.tab {
                Text(tab <= 9 ? "⌘\(tab)" : "Tab \(tab)")
                    .font(.system(size: 11, weight: .medium).monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Text(session.name)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(1)
            if store.showsFolders, session.folder != session.name {
                Text(session.folder)
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .layoutPriority(-1)
            }
            Spacer(minLength: 6)
            Text(age(session.since, now: store.now))
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(.tertiary)
        }
    }

    @ViewBuilder private var content: some View {
        switch group {
        case .needsYou:
            if session.kind == "picker", session.picker != nil {
                EmptyView()  // drawn below the card's button: its option buttons take clicks
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    Text(session.ask ?? session.headline ?? "Waiting for you")
                        .font(.system(size: 12))
                        .fixedSize(horizontal: false, vertical: true)
                    if let headline = session.headline, headline != session.ask, session.kind == "asked" || session.kind == "interrupted" {
                        Text(headline)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case .working:
            HStack(spacing: 5) {
                if session.judging { ProgressView().controlSize(.mini) }
                Text(session.judging ? "Finished, judging…" : (session.activity ?? "Working"))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        case .ready:
            EmptyView()
        case .followups, .done, .close:
            if let headline = session.headline {
                Text(headline).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var hasChecklist: Bool {
        !session.followups.isEmpty && [.followups, .done, .close].contains(group)
    }

    /// Follow-ups as a checklist. Outside the card's button, so a tick does not open the tab.
    private var checklist: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(session.followups, id: \.text) { item in
                let checked = store.isChecked(session, item)
                Button { store.toggle(session, item) } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Image(systemName: checked ? "checkmark.square.fill" : "square")
                            .font(.system(size: 17))
                            .foregroundStyle(checked ? Color.secondary : (showMissingTicks ? Color.orange : Color.primary.opacity(0.55)))
                            .frame(width: 20)
                        Text(item.who == "claude" ? "Claude" : "You")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(checked || item.who == "claude" ? Color.secondary : Color.primary)
                            .frame(width: 38, alignment: .leading)
                        Text(item.text)
                            .font(.system(size: 11.5))
                            .strikethrough(checked)
                            .foregroundStyle(checked ? .tertiary : .secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 2)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(checked ? "Mark as not done" : "Mark as done")
            }
        }
    }

    /// One line ("Done: a; b.") so typing it into the terminal cannot submit half of it.
    private func tellTicked() {
        let ticked = session.followups.filter { store.isChecked(session, $0) }
        guard !ticked.isEmpty else {
            showMissingTicks = true
            store.say("Nothing ticked")
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                showMissingTicks = false
            }
            return
        }
        let items = ticked.map { $0.text.trimmingCharacters(in: CharacterSet(charactersIn: " .;")) }
        store.reply(session, "Done: " + items.joined(separator: "; ") + ".")
    }

    /// The picker's questions with their options as buttons. A lone single-choice question is
    /// answered by its click, as its number answers it in the terminal; otherwise the picks go
    /// out together with Send. Outside the card's button, so a pick does not open the tab.
    private func picker(_ p: Picker) -> some View {
        let sent = store.answered[p.id]
        let quick = p.questions.count == 1 && p.questions[0].multi == false
        return VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(p.questions.enumerated()), id: \.offset) { qi, q in
                VStack(alignment: .leading, spacing: 5) {
                    Button { store.focus(session) } label: {
                        Text(q.question ?? "")
                            .font(.system(size: 12))
                            .foregroundStyle(showMissingAnswers && (chosen[qi] ?? []).isEmpty ? Color.orange : Color.primary)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Open the tab")
                    FlowLayout(spacing: 4) {
                        ForEach(Array(q.options.enumerated()), id: \.offset) { oi, option in
                            let on = sent.map { qi < $0.count && $0[qi].contains(oi) } ?? (chosen[qi]?.contains(oi) ?? false)
                            Button { pick(p, qi, oi, quick: quick) } label: {
                                HStack(spacing: 4) {
                                    if q.multi { Image(systemName: on ? "checkmark.square.fill" : "square").font(.system(size: 11)) }
                                    Text(option.label).font(.system(size: 11)).lineLimit(1)
                                }
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3)
                                .foregroundStyle(on ? Color.white : Color.primary)
                                .background(Capsule().fill(on ? Color.accentColor : Color.primary.opacity(0.08)))
                                .contentShape(Capsule())
                            }
                            .buttonStyle(.plain)
                            .help(option.description ?? option.label)
                        }
                    }
                }
            }
            if !quick {
                HStack {
                    Spacer()
                    Button(sent == nil ? "Send" : "Sent") { send(p) }
                        .help("Answer the question\(p.questions.count > 1 ? "s" : "") in the session")
                }
                .controlSize(.small)
                .buttonStyle(.bordered)
            }
        }
        .disabled(sent != nil)
        .opacity(sent != nil ? 0.55 : 1)
    }

    private func pick(_ p: Picker, _ qi: Int, _ oi: Int, quick: Bool) {
        if quick {
            store.answer(session, p, [[oi]])
            return
        }
        var picked = chosen[qi] ?? []
        if p.questions[qi].multi {
            if picked.contains(oi) { picked.remove(oi) } else { picked.insert(oi) }
        } else {
            picked = [oi]
        }
        chosen[qi] = picked
    }

    private func send(_ p: Picker) {
        let answers = p.questions.indices.map { (chosen[$0] ?? []).sorted() }
        guard answers.allSatisfy({ !$0.isEmpty }) else {
            showMissingAnswers = true
            store.say("Answer every question")
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                showMissingAnswers = false
            }
            return
        }
        store.answer(session, p, answers)
    }

    private var actions: some View {
        HStack(spacing: 6) {
            Spacer()
            if session.canReply {
                if hasChecklist {
                    Button("Tell Claude") { tellTicked() }
                        .help("Send the ticked items to the session")
                } else {
                    Button("Continue") { store.reply(session, "continue") }
                        .help("Send \"continue\" to the session")
                }
            }
            if session.canClear {
                Button("Clear") { store.clear(session) }
                    .help("Start a fresh conversation in this tab (/clear)")
            }
            if session.canClose {
                Button(confirmingClose ? "Confirm close" : "Close") {
                    if confirmingClose {
                        confirmingClose = false
                        store.close(session)
                    } else {
                        confirmingClose = true
                        Task {
                            try? await Task.sleep(nanoseconds: 4_000_000_000)
                            confirmingClose = false
                        }
                    }
                }
                .tint(confirmingClose ? .red : nil)
            }
        }
        .controlSize(.small)
        .buttonStyle(.bordered)
    }
}

private struct ListHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// Wraps its children onto as many lines as needed.
struct FlowLayout: Layout {
    var spacing: CGFloat = 4

    private func size(of view: LayoutSubview, maxWidth: CGFloat) -> CGSize {
        let ideal = view.sizeThatFits(.unspecified)
        return ideal.width <= maxWidth ? ideal : view.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? 10_000
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, width: CGFloat = 0
        for view in subviews {
            let s = size(of: view, maxWidth: maxWidth)
            if x > 0, x + s.width > maxWidth { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            x += s.width + spacing
            rowHeight = max(rowHeight, s.height)
            width = max(width, x - spacing)
        }
        return CGSize(width: min(width, maxWidth), height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let s = size(of: view, maxWidth: bounds.width)
            if x > bounds.minX, x + s.width > bounds.maxX { x = bounds.minX; y += rowHeight + spacing; rowHeight = 0 }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            rowHeight = max(rowHeight, s.height)
        }
    }
}

func age(_ since: Double?, now: Date) -> String {
    guard let since, since > 0 else { return "" }
    let seconds = max(0, now.timeIntervalSince1970 - since)
    if seconds < 60 { return "now" }
    if seconds < 3600 { return "\(Int(seconds / 60))m" }
    if seconds < 86400 { return "\(Int(seconds / 3600))h" }
    return "\(Int(seconds / 86400))d"
}
