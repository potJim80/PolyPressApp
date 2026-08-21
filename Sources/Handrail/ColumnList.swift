import SwiftUI
import HandrailCore

/// The columns in the file, and what can be done to each.
///
/// Clicking a column is the main way into an action, because that is how the
/// question actually arrives: not "which of sixteen verbs do I want" but "I want
/// to do something to this column". The verb list is still there for the steps
/// that are about the whole table.
///
/// **This list has to survive a wide file.** The first version put a Menu in
/// every row inside a plain VStack, which SwiftUI builds all of at once: a real
/// 209-column survey file made several thousand views in one go and the app
/// died in AttributeGraph. So the rows are lazy, and the actions for a column
/// are built only for the one column that is open.
struct ColumnList: View {
    @Bindable var state: AppState
    @State private var search = ""
    @State private var openColumn: String?

    private var shown: [Column] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return state.columns }
        return state.columns.filter { $0.name.lowercased().contains(q) }
    }

    /// What was actually looked at, in the file's own terms.
    private var readNote: String {
        guard let data = state.data else { return "" }
        let mb = Double(data.bytesRead) / 1_048_576.0
        let read = mb < 1 ? String(format: "%.0f KB", Double(data.bytesRead) / 1024)
                          : String(format: "%.1f MB", mb)
        if data.truncated {
            return "Read \(data.sampledRows) rows from the top and \(data.probedRows) from "
                 + "further down (\(read)) — the middle and end too, because a column can "
                 + "be empty at the start of a file and full later. R reads all of it when "
                 + "the script runs."
        }
        return "Read all \(data.sampledRows) rows (\(read))."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionLabel("Columns")
                Spacer()
                if let data = state.data {
                    Text(search.isEmpty ? "\(data.columns.count)"
                                        : "\(shown.count) of \(data.columns.count)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            if state.data == nil {
                Text("Choose a data file and its columns appear here.")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                // A file with two hundred columns is normal in survey data, and
                // scrolling to find one is not a way to work.
                if state.columns.count > 12 {
                    TextField("Find a column", text: $search)
                        .textFieldStyle(.roundedBorder)
                        .font(.callout)
                }

                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(shown) { column in
                            ColumnRow(state: state, column: column, openColumn: $openColumn)
                        }
                    }
                }

                if shown.isEmpty {
                    Text("No column with that in its name.")
                        .font(.callout).foregroundStyle(.secondary)
                }

                Text(readNote)
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }
}

private struct ColumnRow: View {
    @Bindable var state: AppState
    let column: Column
    @Binding var openColumn: String?
    @State private var hovering = false

    private var isOpen: Bool { openColumn == column.name }

    var body: some View {
        Button {
            openColumn = isOpen ? nil : column.name
        } label: {
            HStack(spacing: 9) {
                Image(systemName: icon)
                    .foregroundStyle(.secondary)
                    .frame(width: 16)
                VStack(alignment: .leading, spacing: 1) {
                    Text(column.name).lineLimit(1)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(flagged ? Color.orange : Color.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.down")
                    .font(.caption2)
                    .foregroundStyle(hovering || isOpen ? AnyShapeStyle(.secondary)
                                                        : AnyShapeStyle(Color.clear))
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
            .background(isOpen ? Color.accentColor.opacity(0.14)
                               : (hovering ? Color(nsColor: .selectedContentBackgroundColor).opacity(0.18)
                                           : Color.clear))
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        // Built only for the column that is open. One popover at a time is the
        // difference between ten views and two thousand.
        .popover(isPresented: Binding(get: { isOpen },
                                      set: { if !$0 && isOpen { openColumn = nil } }),
                 arrowEdge: .trailing) {
            ColumnActions(state: state, column: column) { openColumn = nil }
        }
    }

    private var icon: String {
        switch column.type {
        case .number:  return "number"
        case .text:    return "textformat.abc"
        case .date:    return "calendar"
        case .logical: return "checkmark.square"
        }
    }

    /// A column with a sentinel in it is the one worth interrupting for: it
    /// looks like numbers, R reads it as text, and every comparison on it is
    /// quietly wrong until that is dealt with.
    private var flagged: Bool { column.numericApartFromSentinel || column.missingShare > 0.2 }

    private var detail: String {
        if let marker = column.sentinel, column.numericApartFromSentinel {
            return "Numbers, but \"\(marker)\" is in it — R reads it as text"
        }
        if let marker = column.sentinel, column.sentinelShare > 0.2 {
            return "\(column.type.label) · \"\(marker)\" in \(Int(column.sentinelShare * 100))% of rows"
        }
        if column.missing > 0 {
            return "\(column.type.label) · \(Int(column.missingShare * 100))% empty in the rows read"
        }
        if !column.example.isEmpty {
            return "\(column.type.label) · e.g. \(column.example)"
        }
        return column.type.label
    }
}

/// What can be done to one column. Only the steps that suit what it holds —
/// offering "Round numbers off" on a column of names is how a list stops being
/// read.
private struct ColumnActions: View {
    @Bindable var state: AppState
    let column: Column
    let dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            VStack(alignment: .leading, spacing: 1) {
                Text(column.name).font(.headline).lineLimit(1)
                Text(summary).font(.caption).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 10)
            .padding(.top, 10)
            .padding(.bottom, 6)

            Divider()

            // Above the actions on purpose: knowing what is in a column is
            // usually the thing you need before you can choose an action at all.
            Button {
                state.showValues(for: column.name)
                dismiss()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "list.bullet.rectangle").foregroundStyle(.secondary)
                    Text("See the values in it")
                    Spacer()
                }
                .padding(.vertical, 6).padding(.horizontal, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.vertical, 4)

            Divider()

            ForEach(offered, id: \.self) { kind in
                Button {
                    state.begin(kind, column: column.name)
                    dismiss()
                } label: {
                    Text(kind.title)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 6).padding(.horizontal, 10)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            .padding(.vertical, 4)
        }
        .frame(width: 270)
    }

    private var summary: String {
        var bits = [column.type.label]
        if let marker = column.sentinel {
            bits.append("\"\(marker)\" in \(Int(column.sentinelShare * 100))% of rows read")
        }
        if column.missing > 0 {
            bits.append("\(Int(column.missingShare * 100))% empty in the rows read")
        }
        if !column.example.isEmpty { bits.append("e.g. \(column.example)") }
        return bits.joined(separator: " · ")
    }

    private var offered: [ActionKind] {
        var kinds: [ActionKind] = []
        // First, because until it is done every comparison on this column is a
        // string comparison and quietly wrong.
        if column.sentinel != nil { kinds.append(.markMissing) }
        // "What is in this column" is the first question anyone has, so the
        // step that answers it comes before the ones that change the data.
        switch column.type {
        case .number:         kinds.append(.describeNumber)
        case .text, .logical: kinds.append(.countValues)
        case .date:           break
        }
        kinds += [.filter, .sort, .summarise, .rename, .select]
        switch column.type {
        case .number:  kinds += [.band, .round, .fillNA, .toText]
        case .text:    kinds += [.toNumber, .toDate]
        case .date:    kinds += [.datePart, .toText]
        case .logical: break
        }
        if column.missing > 0 { kinds += [.dropNA] }
        return kinds
    }
}
