import Foundation

/// Runs the bundled `cockpit.py` with a Python 3 found on this Mac.
enum Backend {
    static let script: String = {
        if let override = ProcessInfo.processInfo.environment["COCKPIT_SCRIPT"] { return override }
        return Bundle.main.path(forResource: "cockpit", ofType: "py") ?? "cockpit.py"
    }()

    static let python: String = {
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
    }()

    /// Apps started from Finder or at login get a bare PATH; the judge needs to find `claude`.
    static let environment: [String: String] = {
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        let extra = ["\(home)/.local/bin", "\(home)/.claude/local", "/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = (extra + [env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]).joined(separator: ":")
        return env
    }()

    static func process(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script] + args
        p.environment = environment
        return p
    }

    struct Result {
        let ok: Bool
        let output: String
        let error: String
    }

    /// Runs one command to completion off the main thread.
    static func run(_ args: [String], input: String? = nil) async -> Result {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = process(args)
                let out = Pipe(), err = Pipe(), inp = Pipe()
                p.standardOutput = out
                p.standardError = err
                p.standardInput = inp
                do {
                    try p.run()
                } catch {
                    continuation.resume(returning: Result(ok: false, output: "", error: "Could not start Python at \(python)"))
                    return
                }
                if let input { inp.fileHandleForWriting.write(Data(input.utf8)) }
                try? inp.fileHandleForWriting.close()
                let output = out.fileHandleForReading.readDataToEndOfFile()
                let error = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                continuation.resume(returning: Result(
                    ok: p.terminationStatus == 0,
                    output: String(decoding: output, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines),
                    error: String(decoding: error, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                ))
            }
        }
    }
}
