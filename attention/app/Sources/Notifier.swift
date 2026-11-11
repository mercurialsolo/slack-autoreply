// macOS notifications for asks: Option 2 (the banner) and Option 3 (the banner with a reply).
//
// A text ask gets a Reply action with an inline field; a choice ask gets one action per
// choice; a file or plain ask gets Open. Every one has Not now. Clicking the banner itself opens
// the popover on that ask. The banner's "time left" is rewritten in place every 15 seconds while
// the ask is open, so the countdown moves without re-alerting.

import Foundation
import AppKit
import UserNotifications

final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    let store: AskStore
    var openPopover: ((String?) -> Void)?
    private let center = UNUserNotificationCenter.current()
    private var categories: [String: UNNotificationCategory] = [:]
    private var refresh: Timer?

    init(store: AskStore) {
        self.store = store
        super.init()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { ok, err in
            if let err = err { store.log("notifications not authorised: \(err.localizedDescription)") }
            else if !ok { store.log("notifications declined in System Settings — the popover still works") }
        }
        refresh = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refreshCountdowns() }
        RunLoop.main.add(refresh!, forMode: .common)
    }

    // MARK: posting

    func show(_ ask: Ask, alert: Bool) {
        let cat = category(for: ask)
        categories[cat.identifier] = cat
        center.setNotificationCategories(Set(categories.values))
        let c = UNMutableNotificationContent()
        c.title = "\(ask.label) · \(ask.title)"
        c.subtitle = subtitle(ask)
        c.body = ask.body.isEmpty ? "Asked by \(ask.from)." : ask.body
        c.categoryIdentifier = cat.identifier
        c.threadIdentifier = ask.id
        c.userInfo = ["ask": ask.id]
        if alert && ask.priority != "p2" { c.sound = ask.priority == "p0" ? UNNotificationSound(named: UNNotificationSoundName("Basso.aiff")) : .default }
        if alert && ask.priority == "p2" { c.sound = .default }
        if #available(macOS 12, *) { c.interruptionLevel = ask.priority == "p0" ? .timeSensitive : .active }
        if let img = ask.images.first, let att = attachment(img) { c.attachments = [att] }
        let req = UNNotificationRequest(identifier: ask.id, content: c, trigger: nil)
        center.add(req) { [weak self] err in if let err = err { self?.store.log("notification failed: \(err.localizedDescription)") } }
    }

    func remove(_ id: String) {
        center.removeDeliveredNotifications(withIdentifiers: [id])
        center.removePendingNotificationRequests(withIdentifiers: [id])
    }

    private func subtitle(_ ask: Ask) -> String {
        var s = "from \(ask.from)"
        if let r = ask.remaining { s += " · \(Ask.clock(r)) left" }
        return s
    }

    private func refreshCountdowns() {
        for ask in store.open where ask.deadline != nil && ask.priority != "p2" && ask.shown > 0 {
            show(ask, alert: false)
        }
    }

    private func category(for ask: Ask) -> UNNotificationCategory {
        var actions: [UNNotificationAction] = []
        switch ask.reply {
        case "text":
            actions.append(UNTextInputNotificationAction(identifier: "reply", title: "Reply", options: [],
                                                         textInputButtonTitle: "Send", textInputPlaceholder: "paste or type"))
        case "choice":
            for (i, c) in ask.choices.prefix(4).enumerated() {
                actions.append(UNNotificationAction(identifier: "choice:\(i)", title: c, options: []))
            }
        default:
            actions.append(UNNotificationAction(identifier: "open", title: "Open", options: [.foreground]))
        }
        actions.append(UNNotificationAction(identifier: "notnow", title: "Not now", options: []))
        return UNNotificationCategory(identifier: "ask-\(ask.id)", actions: actions, intentIdentifiers: [], options: [])
    }

    /// The system takes ownership of an attachment's file, so it gets a copy.
    private func attachment(_ src: URL) -> UNNotificationAttachment? {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + "-" + src.lastPathComponent)
        guard (try? FileManager.default.copyItem(at: src, to: tmp)) != nil else { return nil }
        return try? UNNotificationAttachment(identifier: src.lastPathComponent, url: tmp, options: nil)
    }

    // MARK: the owner acted on a banner

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        defer { done() }
        guard let id = r.notification.request.content.userInfo["ask"] as? String,
              let ask = store.open.first(where: { $0.id == id }) else { return }
        switch r.actionIdentifier {
        case "reply":
            let text = (r as? UNTextInputNotificationResponse)?.userText.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if text.isEmpty { openPopover?(id) } else { store.answer(ask, text) }
        case let a where a.hasPrefix("choice:"):
            if let i = Int(a.dropFirst(7)), i < ask.choices.count { store.answer(ask, ask.choices[i]) }
        case "notnow":
            store.decline(ask)
        case UNNotificationDismissActionIdentifier:
            break
        default:   // the banner itself, or Open
            openPopover?(id)
        }
    }
}
