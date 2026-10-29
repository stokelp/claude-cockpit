import AppKit
import SwiftUI

struct CockpitView: View {
    @EnvironmentObject var store: Store
    var scrolls = true
    var shortcut: String?
    @State private var listHeight: CGFloat = 0

    /// The popover sizes itself to its content, so the list needs a definite height.
    private var maxListHeight: CGFloat {
        min(700, (NSScreen.main?.visibleFrame.height ?? 800) - 160)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if store.hooks != "installed" { hooksBar; Divider() }
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
            Toggle("Open at login", isOn: Binding(get: { store.openAtLogin }, set: { _ in store.toggleOpenAtLogin() }))
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

    @State private var replying = false
    @State private var draft = ""
    @State private var confirmingClose = false
    @State private var hovering = false
    @FocusState private var fieldFocused: Bool

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
            if replying { replyField }
            if session.canReply || session.canClose { actions }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(hovering ? 0.085 : 0.05)))
        .onHover { hovering = $0 }
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
            if session.kind == "picker", let q = session.question {
                picker(q)
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
        case .followups, .done, .close:
            VStack(alignment: .leading, spacing: 4) {
                if let headline = session.headline {
                    Text(headline).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
                }
                ForEach(session.followups, id: \.self) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(item.who == "claude" ? "Claude" : "You")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(item.who == "claude" ? Color.secondary : Color.primary)
                            .frame(width: 38, alignment: .leading)
                        Text(item.text)
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func picker(_ q: Question) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(q.question ?? "").font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            FlowLayout(spacing: 4) {
                ForEach(Array(q.options.enumerated()), id: \.offset) { _, option in
                    Text(option)
                        .font(.system(size: 11))
                        .lineLimit(1)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.primary.opacity(0.07)))
                }
            }
            let notes = [q.multi ? "Several answers allowed" : nil, q.more > 0 ? "+\(q.more) more question\(q.more > 1 ? "s" : "")" : nil].compactMap { $0 }
            if !notes.isEmpty {
                Text(notes.joined(separator: ", ")).font(.system(size: 11)).foregroundStyle(.tertiary)
            }
        }
    }

    private var replyField: some View {
        HStack(spacing: 6) {
            TextField("Message", text: $draft)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))
                .focused($fieldFocused)
                .onSubmit(send)
            Button("Send", action: send)
                .controlSize(.small)
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .onAppear { fieldFocused = true }
    }

    private var actions: some View {
        HStack(spacing: 6) {
            Spacer()
            if session.canReply {
                // One-click answers that get the session going again.
                Button("Done") { store.reply(session, "done") }
                    .help("Send \"done\" to the session")
                Button("Retry") { store.reply(session, "retry") }
                    .help("Send \"retry\" to the session")
                Button(replying ? "Cancel" : "Reply") {
                    replying.toggle()
                    if !replying { draft = "" }
                }
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

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        store.reply(session, text)
        draft = ""
        replying = false
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
