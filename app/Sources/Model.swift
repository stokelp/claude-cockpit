import SwiftUI

/// The first question of a pending AskUserQuestion picker.
struct Question: Decodable {
    let header: String?
    let question: String?
    let options: [String]
    let multi: Bool
    let more: Int
}

struct Followup: Decodable, Hashable {
    let who: String
    let text: String
}

/// One open Claude Code session, as described by `cockpit.py serve`.
struct Session: Decodable, Identifiable {
    let pid: Int
    let sid: String
    let name: String
    let folder: String
    let tab: Int?
    let status: String
    let since: Double?
    let lastPrompt: String?
    let group: String
    let kind: String?
    let judging: Bool
    let question: Question?
    let ask: String?
    let activity: String?
    let headline: String?
    let followups: [Followup]
    let canReply: Bool
    let canClear: Bool
    let canClose: Bool

    var id: String { sid }
}

struct Snapshot: Decodable {
    let at: Double
    let sessions: [Session]

    static func decode(_ data: Data) -> Snapshot? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(Snapshot.self, from: data)
    }
}

enum SessionGroup: String, CaseIterable, Identifiable {
    case needsYou = "needs_you"
    case working
    case followups
    case done
    case close
    case ready

    var id: String { rawValue }

    var title: String {
        switch self {
        case .needsYou: "Needs you"
        case .working: "In progress"
        case .followups: "Follow-ups"
        case .done: "Done"
        case .close: "Safe to close"
        case .ready: "Ready"
        }
    }

    var color: Color {
        switch self {
        case .needsYou: .orange
        case .working: .blue
        case .followups: Color(nsColor: .systemYellow)
        case .done: .gray
        case .close: .green
        case .ready: .teal
        }
    }
}
