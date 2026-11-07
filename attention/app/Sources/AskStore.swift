// The asks the daemon writes under ~/.attention/asks/, read every second, answered in place.
//
// The Python daemon (attention/attn.py) owns the queue, the HTTP side and the nag loop. This app
// is a renderer: it shows an open ask as a macOS notification and in the menu-bar popover, and
// when the owner answers it writes the terminal state into the same JSON file the daemon is
// polling. Nothing else talks to this app, so there is no socket and no protocol to version.

import Foundation
import AppKit

final class Ask: Identifiable, ObservableObject {
    let id: String
    let title: String
    let body: String
    let priority: String       // p0 | p1 | p2
    let from: String
    let reply: String          // none | text | choice | file
    let choices: [String]
    let created: Date
    let deadline: Date?
    let images: [URL]
    let context: String
    @Published var shown: Int
    @Published var state: String

    init?(url: URL) {
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = obj["id"] as? String else { return nil }
        self.id = id
        title = obj["title"] as? String ?? ""
        body = obj["body"] as? String ?? ""
        priority = obj["priority"] as? String ?? "p1"
        from = obj["from"] as? String ?? "unknown"
        reply = obj["reply"] as? String ?? "none"
        choices = obj["choices"] as? [String] ?? []
        created = Ask.parse(obj["created"] as? String) ?? Date()
        deadline = Ask.parse(obj["deadline"] as? String)
        images = (obj["images"] as? [String] ?? []).map { URL(fileURLWithPath: $0) }
        context = obj["context"] as? String ?? ""
        shown = obj["shown"] as? Int ?? 0
        state = obj["state"] as? String ?? "open"
    }

    static let iso: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    static func parse(_ s: String?) -> Date? {
        guard let s = s else { return nil }
        return iso.date(from: String(s.prefix(19)))
    }

    var remaining: TimeInterval? {
        guard let d = deadline else { return nil }
        return max(0, d.timeIntervalSinceNow)
    }

    /// 0…1 of the deadline still ahead, for the bar.
    var fraction: Double {
        guard let d = deadline else { return 1 }
        let total = d.timeIntervalSince(created)
        guard total > 0 else { return 0 }
        return max(0, min(1, d.timeIntervalSinceNow / total))
    }

    var label: String {
        switch priority { case "p0": return "NOW"; case "p1": return "SOON"; default: return "LATER" }
    }

    var tint: NSColor {
        switch priority {
        case "p0": return NSColor(red: 0.85, green: 0.47, blue: 0.34, alpha: 1)   // clay
        case "p1": return NSColor(red: 0.85, green: 0.64, blue: 0.25, alpha: 1)   // amber
        default:   return NSColor(red: 0.36, green: 0.55, blue: 0.75, alpha: 1)   // blue
        }
    }

    static func clock(_ t: TimeInterval?) -> String {
        guard let t = t else { return "" }
        let s = Int(t.rounded())
        if s >= 3600 { return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) }
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

final class AskStore: ObservableObject {
    static let home = URL(fileURLWithPath: ProcessInfo.processInfo.environment["ATTN_HOME"]
                          ?? NSHomeDirectory() + "/.attention")
    static let asks = home.appendingPathComponent("asks")

    @Published private(set) var open: [Ask] = []
    /// Called when an ask wants to be (re)shown: new, or `shown` went up (the daemon's nag).
    var onShow: ((Ask) -> Void)?
    /// Called when an ask stops being open, so its notification can go.
    var onClose: ((String) -> Void)?

    private var seenShown: [String: Int] = [:]
    private var timer: Timer?

    func start() {
        try? FileManager.default.createDirectory(at: AskStore.asks, withIntermediateDirectories: true)
        let pid = AskStore.home.appendingPathComponent("app.pid")
        try? "\(ProcessInfo.processInfo.processIdentifier)\n".write(to: pid, atomically: true, encoding: .utf8)
        scan()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.scan() }
        RunLoop.main.add(timer!, forMode: .common)
    }

    func scan() {
        let fm = FileManager.default
        let files = (try? fm.contentsOfDirectory(at: AskStore.asks, includingPropertiesForKeys: nil)) ?? []
        var now: [Ask] = []
        for f in files where f.lastPathComponent.hasPrefix("ask-") && f.pathExtension == "json" {
            guard let a = Ask(url: f) else { continue }
            if a.state == "open" {
                now.append(a)
                let before = seenShown[a.id]
                if before == nil || a.shown > before! {
                    seenShown[a.id] = a.shown
                    if a.shown > 0 { onShow?(a) }
                }
            } else if seenShown[a.id] != nil {
                seenShown[a.id] = nil
                onClose?(a.id)
            }
        }
        let order = ["p0": 0, "p1": 1, "p2": 2]
        now.sort { (order[$0.priority] ?? 9, $0.deadline ?? .distantFuture) < (order[$1.priority] ?? 9, $1.deadline ?? .distantFuture) }
        let ids = now.map { $0.id }
        if ids != open.map({ $0.id }) || zip(now, open).contains(where: { $0.shown != $1.shown }) {
            open = now
        }
    }

    // MARK: answering — the same fields attn.py's finish() writes, first terminal state wins

    func answer(_ ask: Ask, _ text: String) { settle(ask, state: "answered", answer: text, file: nil) }
    func decline(_ ask: Ask) { settle(ask, state: "declined", answer: nil, file: nil) }

    func answer(_ ask: Ask, file: URL) {
        let ext = file.pathExtension.isEmpty ? "" : "." + file.pathExtension.lowercased()
        let dest = AskStore.asks.appendingPathComponent("\(ask.id)-answer\(ext)")
        try? FileManager.default.removeItem(at: dest)
        guard (try? FileManager.default.copyItem(at: file, to: dest)) != nil else { return }
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: dest.path)
        settle(ask, state: "answered", answer: dest.lastPathComponent, file: dest.path)
    }

    private func settle(_ ask: Ask, state: String, answer: String?, file: String?) {
        let url = AskStore.asks.appendingPathComponent("\(ask.id).json")
        guard let data = try? Data(contentsOf: url),
              var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["state"] as? String) == "open" else { return }
        obj["state"] = state
        obj["answer"] = answer ?? NSNull()
        obj["answered_at"] = Ask.iso.string(from: Date())
        if let file = file { obj["answer_file"] = file }
        guard let out = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) else { return }
        let tmp = url.appendingPathExtension("tmp")
        try? out.write(to: tmp)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tmp.path)
        _ = try? FileManager.default.replaceItemAt(url, withItemAt: tmp)
        log("\(ask.id) \(state) (\(ask.priority), from \(ask.from)): \(ask.title)")
        scan()
    }

    func log(_ line: String) {
        let url = AskStore.home.appendingPathComponent("attn.log")
        let s = "\(Ask.iso.string(from: Date())) app: \(line)\n"
        if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(s.data(using: .utf8)!); try? h.close() }
        else { try? s.write(to: url, atomically: true, encoding: .utf8) }
    }
}
