import SwiftUI
import UniformTypeIdentifiers
import HandrailCore

/// Where it is pointed: a project, a script inside it, and a data file.
///
/// Three choices, made once, and then it is out of the way. The project comes
/// first because it is the thing he already opens in RStudio — following it is
/// what makes "it just appears in his script" true without anything being told
/// twice.
struct SetupBar: View {
    @Bindable var state: AppState

    var body: some View {
        HStack(spacing: Style.gap) {
            picker(icon: "folder",
                   label: "Project",
                   value: state.project?.name,
                   placeholder: "Choose an RStudio project…",
                   menu: AnyView(EmptyView()),
                   action: chooseProject)

            Divider().frame(height: 26)

            scriptPicker
            Divider().frame(height: 26)
            dataPicker

            Spacer()

            DataWindowButton(state: state)

            Button {
                state.openInRStudio()
            } label: {
                Label("Open in RStudio", systemImage: "arrow.up.forward.app")
            }
            .disabled(state.scriptURL == nil)
        }
        .padding(.horizontal, Style.gutter)
        .padding(.vertical, 12)
        .background(.bar)
    }

    // MARK: - the three pickers

    private func picker(icon: String, label: String, value: String?,
                        placeholder: String, menu: AnyView,
                        action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(label).font(.caption).foregroundStyle(.secondary)
                    Text(value ?? placeholder)
                        .font(.body.weight(value == nil ? .regular : .medium))
                        .foregroundStyle(value == nil ? .secondary : .primary)
                        .lineLimit(1)
                }
            }
        }
        .buttonStyle(.plain)
    }

    private var scriptPicker: some View {
        Menu {
            if let project = state.project {
                let scripts = state.scripts
                if scripts.isEmpty {
                    Text("No .R files in this project yet")
                } else {
                    ForEach(scripts, id: \.self) { url in
                        Button(project.referenceTo(url)) { state.choose(script: url) }
                    }
                }
                Divider()
                Button("New script…") { newScript() }
                Button("Choose another file…") { chooseScript() }
                Button("Look again") { state.rescanProject() }
            } else {
                Text("Choose a project first")
            }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "doc.text").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Writes into").font(.caption).foregroundStyle(.secondary)
                    Text(state.scriptURL?.lastPathComponent ?? "none yet — pick one")
                        .font(.body.weight(state.scriptURL == nil ? .semibold : .medium))
                        .foregroundStyle(state.scriptURL == nil
                                         ? AnyShapeStyle(Color.orange)
                                         : AnyShapeStyle(.primary))
                        .lineLimit(1)
                }
            }
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
    }

    private var dataPicker: some View {
        Menu {
            if let project = state.project {
                let files = state.dataFiles
                if files.isEmpty {
                    Text("No CSV or TSV files found in this project")
                } else {
                    ForEach(files, id: \.self) { url in
                        Button(project.referenceTo(url)) { state.load(data: url) }
                    }
                }
                Divider()
                Button("Look again") { state.rescanProject() }
            }
            Button("Choose another file…") { chooseData() }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "tablecells").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Data").font(.caption).foregroundStyle(.secondary)
                    Text(state.dataURL?.lastPathComponent ?? "Choose a file…")
                        .font(.body.weight(state.dataURL == nil ? .regular : .medium))
                        .foregroundStyle(state.dataURL == nil ? .secondary : .primary)
                        .lineLimit(1)
                }
            }
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
    }

    // MARK: - panels

    private func chooseProject() {
        if let folder = ProjectPicker.run() { state.open(project: folder) }
    }

    private func chooseScript() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "R") ?? .plainText]
        panel.message = "Pick the R script to write into."
        if panel.runModal() == .OK, let url = panel.url { state.choose(script: url) }
    }

    private func chooseData() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.commaSeparatedText, .tabSeparatedText, .plainText]
        panel.message = "Pick the data file you are working from."
        if panel.runModal() == .OK, let url = panel.url { state.load(data: url) }
    }

    private func newScript() {
        guard let folder = state.project?.folder else { return }
        let panel = NSSavePanel()
        panel.directoryURL = folder
        panel.nameFieldStringValue = "analysis.R"
        panel.message = "Name the script to build up."
        if panel.runModal() == .OK, let url = panel.url {
            try? "".write(to: url, atomically: true, encoding: .utf8)
            state.choose(script: url)
        }
    }
}
