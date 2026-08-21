import SwiftUI
import HandrailCore

@main
struct HandrailApp: App {
    @State private var state = AppState()

    var body: some Scene {
        Window("Handrail", id: "main") {
            MainWindow(state: state)
        }
        .defaultSize(width: 980, height: 660)
        .commands {
            CommandGroup(replacing: .newItem) { }
            CommandGroup(after: .appInfo) {
                Button("Open the script in RStudio") { state.openInRStudio() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                    .disabled(state.scriptURL == nil)
            }
        }

        // Looking at the data is a window rather than a sheet on purpose: the
        // point is to have it open beside the builder while filling a step in,
        // not to cover the thing you are filling in.
        Window("Data", id: DataViewer.windowID) {
            DataViewer(state: state)
        }
        .defaultSize(width: 1020, height: 640)
        .keyboardShortcut("d", modifiers: [.command, .shift])

        // The menu bar copy: the same builder, close to hand, so adding a step
        // while working in RStudio never means finding another window.
        MenuBarExtra("Handrail", systemImage: "text.append") {
            MenuBarPanel(state: state)
        }
        .menuBarExtraStyle(.window)
    }
}
