import SwiftUI
import HandrailCore

/// What is actually in the file, in a window of its own.
///
/// Every other part of Handrail describes the data — types, gaps, value counts.
/// At some point you just want to look at it, and "open it in RStudio to see"
/// is the exact wall this app exists to remove.
///
/// Two things keep it light on a file the size of a hospital dataset: only the
/// columns on screen are turned into strings at all, and pages resume from a
/// byte offset instead of counting from the top. Reading page ten costs what
/// page one cost.
@MainActor @Observable
final class ViewerModel {
    /// The columns on screen, in order. Not all of them, on a wide file — 209
    /// columns is not something anyone reads across.
    var shown: [String] = []
    var rows: [[String]] = []
    var atEnd = false
    var loading = false
    var failure: String?

    /// How many columns to open with. Enough to recognise the file by.
    static let firstColumns = 12

    private var offset: UInt64 = 0
    private var task: Task<Void, Never>?

    var firstRowNumber: Int { rows.isEmpty ? 0 : 1 }

    func begin(url: URL, file: DataFile, columns: [String]? = nil) {
        task?.cancel()
        shown = columns ?? Array(file.allNames.prefix(Self.firstColumns))
        rows = []
        offset = 0
        atEnd = false
        failure = nil
        loadMore(url: url, file: file)
    }

    /// Changing which columns are shown re-reads from the top, because the
    /// fields for the new column were never built on the way past.
    func show(_ columns: [String], url: URL, file: DataFile) {
        guard !columns.isEmpty else { return }
        begin(url: url, file: file, columns: columns)
    }

    func loadMore(url: URL, file: DataFile) {
        guard !loading, !atEnd else { return }
        loading = true
        let from = offset
        let want = shown
        let held = rows.count
        task = Task {
            let page: DataPage? = await Task.detached(priority: .userInitiated) {
                try? DataPager.page(url, in: file, columns: want, from: from,
                                    firstRow: held + 1,
                                    shouldStop: { Task.isCancelled })
            }.value
            if Task.isCancelled { return }
            if let page {
                self.rows.append(contentsOf: page.rows)
                self.offset = page.offset
                self.atEnd = page.atEnd || self.rows.count >= DataPager.maxHeld
            } else {
                self.failure = "Could not read any more of the file."
                self.atEnd = true
            }
            self.loading = false
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        loading = false
    }
}

struct DataViewer: View {
    static let windowID = "data"

    @Bindable var state: AppState
    @State private var model = ViewerModel()
    @State private var pickingColumns = false

    private let columnWidth: CGFloat = 132

    var body: some View {
        Group {
            if let url = state.dataURL, let file = state.data {
                content(url: url, file: file)
            } else {
                ContentUnavailableView("No data file chosen",
                                       systemImage: "tablecells",
                                       description: Text("Pick one in the Handrail window and it will show up here."))
            }
        }
        .frame(minWidth: 560, minHeight: 320)
        .onDisappear { model.stop() }
    }

    @ViewBuilder private func content(url: URL, file: DataFile) -> some View {
        VStack(spacing: 0) {
            header(url: url, file: file)
            Divider()
            if model.rows.isEmpty && model.loading {
                ProgressView("Reading…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.rows.isEmpty {
                ContentUnavailableView("Nothing to show", systemImage: "tablecells")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                grid
            }
            Divider()
            footer(url: url, file: file)
        }
        // Re-reads when the chosen file changes underneath the window.
        .task(id: url) { model.begin(url: url, file: file) }
    }

    private func header(url: URL, file: DataFile) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text(url.lastPathComponent).font(.headline).lineLimit(1)
                Text("\(file.allNames.count) columns · showing \(model.shown.count)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                pickingColumns = true
            } label: {
                Label("Columns", systemImage: "slider.horizontal.3")
            }
            .popover(isPresented: $pickingColumns, arrowEdge: .bottom) {
                columnPicker(url: url, file: file)
            }
        }
        .padding(.horizontal, Style.gutter)
        .padding(.vertical, 10)
        .background(.bar)
    }

    /// The grid. One horizontal scroll around a lazy vertical one, with the
    /// header pinned, so a wide file scrolls sideways without the column names
    /// wandering off the top.
    private var grid: some View {
        ScrollView([.horizontal, .vertical]) {
            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                Section {
                    ForEach(Array(model.rows.enumerated()), id: \.offset) { index, row in
                        HStack(spacing: 0) {
                            Text("\(index + 1)")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.tertiary)
                                .frame(width: 60, alignment: .trailing)
                                .padding(.trailing, 8)
                            ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                                Text(cell.isEmpty ? "—" : cell)
                                    .font(.system(.callout, design: .monospaced))
                                    .foregroundStyle(cell.isEmpty ? .tertiary : .primary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                    .frame(width: columnWidth, alignment: .leading)
                                    .padding(.horizontal, 6)
                            }
                        }
                        .padding(.vertical, 3)
                        .background(index.isMultiple(of: 2) ? Color.clear
                                                            : Color.primary.opacity(0.035))
                    }
                } header: {
                    HStack(spacing: 0) {
                        Text("")
                            .frame(width: 60)
                            .padding(.trailing, 8)
                        ForEach(model.shown, id: \.self) { name in
                            Text(name)
                                .font(.caption.weight(.semibold))
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .frame(width: columnWidth, alignment: .leading)
                                .padding(.horizontal, 6)
                        }
                    }
                    .padding(.vertical, 6)
                    .background(.bar)
                }
            }
        }
    }

    private func footer(url: URL, file: DataFile) -> some View {
        HStack(spacing: 12) {
            if let failure = model.failure {
                Label(failure, systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.orange)
            } else {
                Text(status(file)).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if model.loading {
                ProgressView().controlSize(.small)
            } else if !model.atEnd {
                Button("Show \(DataPager.pageRows) more") {
                    model.loadMore(url: url, file: file)
                }
            } else if model.rows.count >= DataPager.maxHeld {
                Text("That is as far as this window goes — the rest is a job for R.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, Style.gutter)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private func status(_ file: DataFile) -> String {
        let shown = "\(model.rows.count.formatted()) row\(model.rows.count == 1 ? "" : "s")"
        return model.atEnd && model.rows.count < DataPager.maxHeld
            ? "\(shown) — the whole file"
            : "\(shown) so far"
    }

    private func columnPicker(url: URL, file: DataFile) -> some View {
        ColumnChooser(all: file.allNames, chosen: model.shown) { picked in
            model.show(picked, url: url, file: file)
            pickingColumns = false
        }
        .frame(width: 320, height: 420)
    }
}

/// Picking which columns to look at. Its own view because a 209-column list
/// needs a search box and a lazy stack, and inlining that made the viewer
/// unreadable.
private struct ColumnChooser: View {
    let all: [String]
    @State var chosen: [String]
    let done: ([String]) -> Void
    @State private var search = ""

    private var shown: [String] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? all : all.filter { $0.lowercased().contains(q) }
    }

    var body: some View {
        VStack(spacing: 0) {
            TextField("Find a column", text: $search)
                .textFieldStyle(.roundedBorder)
                .padding(10)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(shown, id: \.self) { name in
                        Toggle(isOn: Binding(
                            get: { chosen.contains(name) },
                            set: { on in
                                if on { if !chosen.contains(name) { chosen.append(name) } }
                                else { chosen.removeAll { $0 == name } }
                            })) {
                                Text(name).lineLimit(1).truncationMode(.middle)
                            }
                            .toggleStyle(.checkbox)
                    }
                }
                .padding(.horizontal, 12)
            }
            Divider()
            HStack {
                Button("First \(ViewerModel.firstColumns)") {
                    chosen = Array(all.prefix(ViewerModel.firstColumns))
                }
                Spacer()
                Button("Show these") { done(chosen) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(chosen.isEmpty)
            }
            .padding(10)
        }
    }
}


/// The menu item and buttons that open the viewer. One place, so the main
/// window, the menu bar and the File menu all open the same window rather than
/// three copies of it.
struct DataWindowButton: View {
    @Bindable var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button {
            openWindow(id: DataViewer.windowID)
        } label: {
            Label("Look at the data", systemImage: "tablecells")
        }
        .disabled(state.data == nil)
        .help("Open the rows in a window of their own (⇧⌘D)")
    }
}
