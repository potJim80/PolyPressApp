import SwiftUI
import HandrailCore

/// The same builder, in the menu bar, for when RStudio is the window you are in.
///
/// Deliberately the same state as the main window: a step half-built in one is
/// half-built in the other. Two copies of the same idea that disagree would be
/// worse than not having the second one.
struct MenuBarPanel: View {
    @Bindable var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(state.project?.name ?? "No project")
                        .font(.headline)
                    Text(state.scriptURL?.lastPathComponent ?? "no script chosen")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                } label: {
                    Image(systemName: "macwindow")
                }
                .buttonStyle(.plain)
                .help("Open the main window")
            }

            Divider()

            if state.project == nil || state.data == nil {
                Text("Open the main window to choose a project and a data file.")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let kind = state.chosenKind {
                ScrollView {
                    ActionForm(state: state, kind: kind)
                }
                .frame(maxHeight: 460)
            } else {
                ActionPalette(state: state)
                    .frame(maxHeight: 420)
            }

            Divider()
            // The receipt and any error belong here too. Without them, a write
            // that failed from the menu bar looked exactly like one that worked.
            StatusLine(state: state)
            HStack {
                Button("Open in RStudio") { state.openInRStudio() }
                    .disabled(state.scriptURL == nil)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }
            .font(.callout)
        }
        .padding(16)
        .frame(width: 480)
        .writeFeedback(state)
    }
}
