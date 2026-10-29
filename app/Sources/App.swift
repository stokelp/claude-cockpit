import AppKit
import SwiftUI

struct CockpitApp: App {
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra {
            CockpitView().environmentObject(store)
        } label: {
            MenuBarLabel(store: store)
        }
        .menuBarExtraStyle(.window)
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
    let view = CockpitView(scrolls: false)
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
        CockpitApp.main()
    }
}
