import SwiftUI
import AppKit
import HandrailCore

/// Asking for a project.
///
/// The thing on screen that says "this is the project" is the .Rproj file, so
/// that is what someone reaches for. The app wants the folder it sits in. Making
/// the file unselectable and hoping they work that out is how the first version
/// of this panel went: greyed-out .Rproj files, and no way to tell why.
///
/// So both work. Pick the folder, or pick the .Rproj inside it, and either lands
/// on the same project.
enum ProjectPicker {

    /// Only folders and .Rproj files are selectable. A stray .md or .csv is
    /// visible but greyed, because picking one would be a guess about which
    /// folder was meant.
    private final class OnlyProjects: NSObject, NSOpenSavePanelDelegate {
        func panel(_ sender: Any, shouldEnable url: URL) -> Bool {
            RProject.isPickable(url)
        }
    }

    nonisolated(unsafe) private static let delegate = OnlyProjects()

    /// The chosen project folder, or nil if the panel was cancelled.
    @MainActor static func run() -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.delegate = delegate
        panel.prompt = "Use this project"
        panel.message = "Pick your RStudio project — either the .Rproj file or the "
                      + "folder it is in."
        guard panel.runModal() == .OK, let url = panel.url else { return nil }
        return RProject.folder(for: url)
    }
}
