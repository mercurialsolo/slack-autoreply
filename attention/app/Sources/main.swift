// Attention — the owner's menu-bar app. A bell in the menu bar with the most urgent countdown
// beside it, a popover with every open ask, and macOS notifications for each one. It renders
// what attention/attn.py queues; it never talks to the network.

import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    let store = AskStore()
    var notifier: Notifier!
    var banners: Banners!
    var item: NSStatusItem!
    let popover = NSPopover()
    var focus: String? = nil
    var tick: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "bell", accessibilityDescription: "Solo")
        item.button?.imagePosition = .imageLeading
        item.button?.action = #selector(toggle)
        item.button?.target = self
        popover.behavior = .transient
        popover.animates = true

        notifier = Notifier(store: store)
        notifier.openPopover = { [weak self] id in self?.open(id) }
        banners = Banners(store: store)
        store.onShow = { [weak self] ask in self?.banners.show(ask); self?.notifier.show(ask, alert: false) }
        store.onClose = { [weak self] id in self?.banners.hide(id); self?.notifier.remove(id); self?.render() }
        store.start()
        render()
        tick = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.render() }
        RunLoop.main.add(tick!, forMode: .common)
        store.log("app started, pid \(ProcessInfo.processInfo.processIdentifier)")
    }

    /// The menu bar: a bell, badged when something is open, with the most urgent countdown live.
    func render() {
        guard let b = item.button else { return }
        let open = store.open
        b.image = NSImage(systemSymbolName: open.isEmpty ? "bell" : "bell.badge.fill", accessibilityDescription: "Solo")
        if let first = open.first(where: { $0.priority != "p2" }), let r = first.remaining {
            let s = NSMutableAttributedString(string: " " + Ask.clock(r))
            s.addAttributes([.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold),
                             .foregroundColor: first.tint], range: NSRange(location: 0, length: s.length))
            b.attributedTitle = s
        } else {
            b.attributedTitle = NSAttributedString(string: "")
        }
        if popover.isShown { popover.contentViewController = NSHostingController(rootView: AsksView(store: store, focus: focus)) }
    }

    @objc func toggle() {
        if popover.isShown { popover.performClose(nil) } else { open(nil) }
    }

    func open(_ id: String?) {
        focus = id
        popover.contentViewController = NSHostingController(rootView: AsksView(store: store, focus: focus))
        guard let b = item.button else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
