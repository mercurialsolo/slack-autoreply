// Option 4: the menu-bar popover. Every open ask as a card with its context, its input and a
// live countdown; the same design as the canvas, in SwiftUI.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct AsksView: View {
    @ObservedObject var store: AskStore
    var focus: String?

    var body: some View {
        VStack(spacing: 12) {
            if store.open.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "checkmark.circle").font(.system(size: 26)).foregroundStyle(.secondary)
                    Text("Nothing needs you").font(.system(size: 13, weight: .semibold))
                    Text("Solo and the agents will put time-sensitive asks here.").font(.system(size: 11)).foregroundStyle(.secondary)
                }.padding(.vertical, 28)
            } else {
                ForEach(store.open) { ask in
                    AskCard(store: store, ask: ask, focused: ask.id == focus)
                }
            }
        }
        .padding(12)
        .frame(width: 364)
    }
}

struct AskCard: View {
    let store: AskStore
    @ObservedObject var ask: Ask
    var focused: Bool
    @State private var text = ""
    @State private var choice: String? = nil
    @State private var file: URL? = nil
    @State private var dropping = false
    @FocusState private var fieldFocus: Bool

    var tint: Color { Color(nsColor: ask.tint) }
    var canSend: Bool {
        switch ask.reply {
        case "text": return !text.trimmingCharacters(in: .whitespaces).isEmpty
        case "choice": return choice != nil
        case "file": return file != nil
        default: return true
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // header: pill · from · live countdown
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(spacing: 8) {
                    Text(ask.label)
                        .font(.system(size: 10, weight: .bold)).tracking(1)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(tint, in: Capsule())
                        .foregroundStyle(ask.priority == "p1" ? Color.black : Color.white)
                    Text("from \(ask.from)").font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    if let r = ask.remaining {
                        HStack(spacing: 5) {
                            Ring(fraction: ask.fraction, tint: tint).frame(width: 14, height: 14)
                            Text(Ask.clock(r)).font(.system(size: 12, weight: .semibold).monospacedDigit()).foregroundStyle(tint)
                        }
                    }
                }
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(ask.title).font(.system(size: 14, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                if !ask.body.isEmpty {
                    Text(ask.body).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            if !ask.images.isEmpty {
                HStack(spacing: 6) {
                    ForEach(ask.images, id: \.self) { url in
                        if let img = NSImage(contentsOf: url) {
                            Image(nsImage: img).resizable().aspectRatio(contentMode: .fill)
                                .frame(width: 104, height: 68).clipShape(RoundedRectangle(cornerRadius: 8))
                                .onTapGesture { NSWorkspace.shared.open(url) }
                        }
                    }
                }
            }
            input
            HStack(spacing: 8) {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    ProgressView(value: ask.deadline == nil ? 1 : ask.fraction).tint(tint)
                }
                Button("Not now") { store.decline(ask) }.buttonStyle(.bordered).controlSize(.small)
                Button("Send") { send() }.buttonStyle(.borderedProminent).tint(ask.priority == "p0" ? tint : .primary).controlSize(.small).disabled(!canSend)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color(nsColor: .windowBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(focused ? tint : Color.primary.opacity(0.08), lineWidth: focused ? 1.5 : 1))
        .onAppear { if focused { fieldFocus = true } }
    }

    @ViewBuilder var input: some View {
        switch ask.reply {
        case "text":
            TextField("paste or type here", text: $text)
                .textFieldStyle(.roundedBorder).font(.system(size: 15).monospacedDigit())
                .focused($fieldFocus).onSubmit { send() }
        case "choice":
            HStack(spacing: 6) {
                ForEach(ask.choices, id: \.self) { c in
                    Button(c) { choice = c }
                        .buttonStyle(.bordered).controlSize(.regular)
                        .tint(choice == c ? tint : nil)
                }
            }
        case "file":
            HStack(spacing: 10) {
                Image(systemName: "arrow.up.doc").font(.system(size: 18)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(file?.lastPathComponent ?? "Drop a file here").font(.system(size: 12, weight: .semibold)).lineLimit(1)
                    Text(file == nil ? "or paste a screenshot with ⌘V" : "ready to send").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Choose file…") { choose() }.controlSize(.small)
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(dropping ? 0.08 : 0.03)))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4])).foregroundStyle(dropping ? tint : Color.primary.opacity(0.25)))
            .onDrop(of: [.fileURL, .image], isTargeted: $dropping) { providers in drop(providers) }
            .onPasteCommand(of: [.fileURL, .image, .png]) { providers in _ = drop(providers) }
        default:
            EmptyView()
        }
    }

    func send() {
        guard canSend else { return }
        switch ask.reply {
        case "text": store.answer(ask, text.trimmingCharacters(in: .whitespacesAndNewlines))
        case "choice": if let c = choice { store.answer(ask, c) }
        case "file": if let f = file { store.answer(ask, file: f) }
        default: store.answer(ask, "")
        }
    }

    func choose() {
        let p = NSOpenPanel()
        p.canChooseDirectories = false; p.allowsMultipleSelection = false
        if p.runModal() == .OK, let u = p.url { file = u }
    }

    func drop(_ providers: [NSItemProvider]) -> Bool {
        guard let p = providers.first else { return false }
        if p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            p.loadItem(forTypeIdentifier: UTType.fileURL.identifier) { item, _ in
                if let d = item as? Data, let u = URL(dataRepresentation: d, relativeTo: nil) { DispatchQueue.main.async { file = u } }
            }
            return true
        }
        if p.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
            p.loadDataRepresentation(forTypeIdentifier: UTType.png.identifier) { data, _ in
                guard let data = data else { return }
                let u = FileManager.default.temporaryDirectory.appendingPathComponent("pasted-\(Int(Date().timeIntervalSince1970)).png")
                try? data.write(to: u)
                DispatchQueue.main.async { file = u }
            }
            return true
        }
        return false
    }
}

struct Ring: View {
    var fraction: Double
    var tint: Color
    var body: some View {
        ZStack {
            Circle().stroke(Color.primary.opacity(0.12), lineWidth: 2)
            Circle().trim(from: 0, to: fraction).stroke(tint, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
        }
    }
}
