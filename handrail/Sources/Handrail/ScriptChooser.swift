import SwiftUI
import AppKit
import UniformTypeIdentifiers
import HandrailCore

/// Choosing, or making, the script to write into.
///
/// This exists because the app had a dead end in it: a project with no .R file
/// in it left the Add button disabled with the reason in small grey text on the
/// other side of the row, and the only way forward buried in a menu. A disabled
/// button that explains itself somewhere else is a dead end however good the
/// explanation is.
enum ScriptChooser {

    @MainActor static func choose(in project: RProject) -> URL? {
        let panel = NSOpenPanel()
        panel.directoryURL = project.folder
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [UTType(filenameExtension: "R") ?? .plainText]
        panel.message = "Pick the R script to add your steps to."
        guard panel.runModal() == .OK else { return nil }
        return panel.url
    }

    /// Makes a new, empty script in the project. Suggests `analysis.R`, which is
    /// what the file usually ends up being called anyway.
    @MainActor static func create(in project: RProject, suggesting name: String = "analysis.R") -> URL? {
        let panel = NSSavePanel()
        panel.directoryURL = project.folder
        panel.nameFieldStringValue = name
        panel.allowedContentTypes = [UTType(filenameExtension: "R") ?? .plainText]
        panel.message = "Name the script to build up. Handrail adds each step to the end of it."
        guard panel.runModal() == .OK, let url = panel.url else { return nil }
        if !FileManager.default.fileExists(atPath: url.path) {
            try? "".write(to: url, atomically: true, encoding: .utf8)
        }
        return url
    }
}

/// What to do when there is nowhere to write yet — offered where the Add button
/// would be, so the way forward is under the cursor rather than in a menu.
struct NoScriptYet: View {
    @Bindable var state: AppState

    private var projectHasScripts: Bool { !state.scripts.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                Button(projectHasScripts ? "Choose a script to write into…"
                                         : "Make a script for this project…") {
                    guard let project = state.project else { return }
                    let url = projectHasScripts ? ScriptChooser.choose(in: project)
                                                : ScriptChooser.create(in: project)
                    if let url {
                        state.choose(script: url)
                        state.rescanProject()
                    }
                }
                .buttonStyle(.borderedProminent)

                if projectHasScripts {
                    Button("New script…") {
                        guard let project = state.project else { return }
                        if let url = ScriptChooser.create(in: project) {
                            state.choose(script: url)
                            state.rescanProject()
                        }
                    }
                }
                Button("Cancel") { state.resetAction() }
            }
            Text(projectHasScripts
                 ? "Your step is ready — it just needs a script to go into."
                 : "\(state.project?.name ?? "This project") has no R script yet. "
                 + "Handrail will make one and add your step to it.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
