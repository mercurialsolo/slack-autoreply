// Option 3 as our own window: a banner at the top right that looks like a notification, floats
// over everything including fullscreen apps, never takes focus until clicked, and holds the
// whole ask — context, input, choices, file, a live countdown, Not now and Send.
//
// Why not a real notification: macOS only lets a signed, Developer-ID app post banners with
// reply fields; an ad-hoc build gets "Notifications are not allowed". A panel needs no
// permission and shows where the owner is, including over a fullscreen Slack where the menu
// bar (and the bell) is hidden.

import AppKit
import SwiftUI

final class BannerWindow: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class Banners {
    let store: AskStore
    private var panels: [String: BannerWindow] = [:]
    private var hideTimers: [String: Timer] = [:]
    private let width: CGFloat = 380
    private let margin: CGFloat = 12

    init(store: AskStore) { self.store = store }

    func show(_ ask: Ask) {
        if let p = panels[ask.id] {
            p.contentViewController = NSHostingController(rootView: BannerView(store: store, ask: ask))
            fit(p); p.orderFrontRegardless()
        } else {
            let p = BannerWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 200),
                                 styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
                                 backing: .buffered, defer: false)
            p.isFloatingPanel = true
            p.level = .statusBar
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            p.hidesOnDeactivate = false
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = true
            p.isMovableByWindowBackground = true
            p.contentViewController = NSHostingController(rootView: BannerView(store: store, ask: ask))
            panels[ask.id] = p
            fit(p)
            p.orderFrontRegardless()
        }
        layout()
        sound(ask)
        if ask.priority == "p2" {
            hideTimers[ask.id]?.invalidate()
            hideTimers[ask.id] = Timer.scheduledTimer(withTimeInterval: 10, repeats: false) { [weak self] _ in self?.hide(ask.id) }
        }
    }

    func hide(_ id: String) {
        hideTimers[id]?.invalidate(); hideTimers[id] = nil
        panels[id]?.orderOut(nil)
        panels[id] = nil
        layout()
    }

    func refresh() {
        for (id, p) in panels {
            guard let ask = store.open.first(where: { $0.id == id }) else { hide(id); continue }
            _ = ask
            fit(p)
        }
        layout()
    }

    private func fit(_ p: NSPanel) {
        guard let v = p.contentViewController?.view else { return }
        let size = v.fittingSize
        let h = max(120, size.height)
        if abs(p.frame.height - h) > 1 { p.setContentSize(NSSize(width: width, height: h)) }
    }

    /// Stack them under the menu bar at the top right, most urgent first.
    private func layout() {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let vf = screen.visibleFrame
        var y = vf.maxY - margin
        // store.open is already most urgent first
        let ids = store.open.map { $0.id }.filter { panels[$0] != nil }
        for id in ids {
            guard let p = panels[id] else { continue }
            let h = p.frame.height
            p.setFrameOrigin(NSPoint(x: vf.maxX - width - margin, y: y - h))
            y -= h + 10
        }
    }

    private func sound(_ ask: Ask) {
        switch ask.priority {
        case "p0": NSSound(named: "Basso")?.play()
        case "p1": NSSound(named: "Glass")?.play()
        default: NSSound(named: "Tink")?.play()
        }
    }
}

struct BannerView: View {
    let store: AskStore
    @ObservedObject var ask: Ask
    var body: some View {
        AskCard(store: store, ask: ask, focused: ask.priority == "p0")
            .frame(width: 380)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.primary.opacity(0.1), lineWidth: 0.5))
            .padding(6)
    }
}
