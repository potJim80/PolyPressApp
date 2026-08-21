import SwiftUI
import HandrailCore

/// The controls for the action being built, and the R it produces.
///
/// The form reads as the sentence it will write: "Keep only the rows where
/// [age] [is more than] [65]". A control that does not fit that sentence is a
/// control in the wrong place.
struct ActionForm: View {
    @Bindable var state: AppState
    let kind: ActionKind

    var body: some View {
        VStack(alignment: .leading, spacing: Style.gap) {
            header
            fields
            if let caution = state.caution { Caution(text: caution) }

            VStack(alignment: .leading, spacing: 7) {
                SectionLabel("The R this writes")
                if let block = state.block {
                    CodeBlock(text: block.trimmingCharacters(in: .newlines))
                } else {
                    Text("Fill the rest in and the R appears here.")
                        .font(.callout).foregroundStyle(.secondary)
                        .padding(.vertical, 6)
                }
            }

            if state.dataIsOutsideProject {
                Caution(text: "This data file is outside the project, so the script has to "
                            + "name it by its full path and will not run on another machine. "
                            + "Move it into the project folder to fix that.")
            }

            // No script yet is not a reason to grey a button out and explain it
            // elsewhere — it is a thing to offer to fix, right here.
            if state.scriptURL == nil {
                NoScriptYet(state: state)
            } else {
                HStack(spacing: 10) {
                    Button("Add to script") { state.addToScript() }
                        .keyboardShortcut(.return, modifiers: [.command])
                        .buttonStyle(.borderedProminent)
                        .disabled(!state.canAdd)
                    Button("Cancel") { state.resetAction() }
                    Spacer()
                    Text("Goes to the end of \(state.scriptURL!.lastPathComponent)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(kind.title).font(.title3.weight(.semibold))
                Spacer()
                Button { state.resetAction() } label: { Image(systemName: "xmark") }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
            }
            Text(state.sentence)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - the controls, one action at a time

    @ViewBuilder private var fields: some View {
        switch kind {
        case .filter:    filterFields
        case .sort:      sortFields
        case .select:    multiColumn("Keep these columns", binding: $state.action.columns)
        case .distinct:  multiColumn("Judged by (none = the whole row)", binding: $state.action.columns)
        case .dropNA:    multiColumn("Drop rows with nothing in", binding: $state.action.columns)
        case .round:     roundFields
        case .mutate:    mutateFields
        case .rename:    renameFields
        case .summarise: summariseFields
        case .head:      headFields
        case .fillNA:    fillFields
        case .band:      bandFields
        case .toNumber, .toText:  columnOnly
        case .markMissing:        markMissingFields
        case .saveCSV:            csvFields
        case .saveRDS:            outputFields(ext: "rds")
        case .saveExcel:          outputFields(ext: "xlsx")
        case .saveDelimited:      delimitedFields
        case .saveForStats:       statsFields
        case .saveSummary:        summaryFileFields
        case .peek:               peekFields
        case .summaryOf:          multiColumn("Which columns (none = all of them)",
                                              binding: $state.action.columns)
        case .crossTab:           crossTabFields
        case .viewIt, .countRows, .glimpse: EmptyView()
        case .toDate:    dateFields
        case .datePart:  datePartFields
        }
    }

    private func columnMenu(_ label: String, selection: Binding<String>,
                            limitedTo kinds: Set<ColumnType>? = nil) -> some View {
        let names = state.columnNames(kinds)
        return LabeledContent(label) {
            Picker("", selection: selection) {
                if names.isEmpty { Text("no suitable column").tag("") }
                ForEach(names, id: \.self) { Text($0).tag($0) }
            }
            .labelsHidden()
            .frame(maxWidth: 220)
        }
    }

    private func multiColumn(_ label: String, binding: Binding<[String]>,
                             limitedTo kinds: Set<ColumnType>? = nil) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.callout.weight(.medium))
            FlowChips(all: state.columnNames(kinds), chosen: binding)
        }
    }

    private var columnOnly: some View {
        columnMenu("Column", selection: $state.action.column)
    }

    private var filterFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Picker("", selection: $state.action.column) {
                    ForEach(state.columnNames(), id: \.self) { Text($0).tag($0) }
                }
                .labelsHidden().frame(maxWidth: 190)

                Picker("", selection: $state.action.comparison) {
                    ForEach(Comparison.allCases, id: \.self) { Text($0.label).tag($0) }
                }
                .labelsHidden().frame(maxWidth: 150)

                if state.action.comparison.takesValue {
                    if state.action.comparison == .between {
                        TextField("from", text: $state.action.value).frame(width: 80)
                        Text("and").foregroundStyle(.secondary)
                        TextField("to", text: $state.action.value2).frame(width: 80)
                    } else if state.action.comparison == .isIn {
                        TextField("one, two, three", text: valuesText).frame(maxWidth: 200)
                    } else {
                        TextField("value", text: $state.action.value).frame(maxWidth: 170)
                    }
                }
            }
            .textFieldStyle(.roundedBorder)

            if state.action.comparison == .isIn {
                Text("Separate the values with commas.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            valueSuggestions
        }
    }

    /// The values actually in the column, when there are few enough to list.
    /// Typing a category name exactly right from memory is a classic dead end.
    @ViewBuilder private var valueSuggestions: some View {
        let type = state.types[state.action.column] ?? .text
        if type == .text, state.action.comparison == .eq || state.action.comparison == .ne {
            let seen = examples(for: state.action.column)
            if !seen.isEmpty {
                HStack(spacing: 6) {
                    Text("Seen in this column:").font(.caption).foregroundStyle(.secondary)
                    ForEach(seen.prefix(6), id: \.self) { value in
                        Button(value) { state.action.value = value }
                            .buttonStyle(.link).font(.caption)
                    }
                    if seen.count > 6 {
                        Text("and \(seen.count - 6) more").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private func examples(for column: String) -> [String] {
        guard let data = state.data,
              let index = data.columns.firstIndex(where: { $0.name == column }) else { return [] }
        var seen: [String] = []
        for row in data.preview where index < row.count {
            let v = row[index]
            if !v.isEmpty && !seen.contains(v) { seen.append(v) }
        }
        return seen
    }

    private var valuesText: Binding<String> {
        Binding(get: { state.action.values.joined(separator: ", ") },
                set: { state.action.values = $0.split(separator: ",")
                            .map { $0.trimmingCharacters(in: .whitespaces) }
                            .filter { !$0.isEmpty } })
    }

    private var sortFields: some View {
        HStack(spacing: 12) {
            Picker("", selection: $state.action.column) {
                ForEach(state.columnNames(), id: \.self) { Text($0).tag($0) }
            }.labelsHidden().frame(maxWidth: 200)
            Picker("", selection: $state.action.descending) {
                Text("smallest first").tag(false)
                Text("largest first").tag(true)
            }.labelsHidden().pickerStyle(.segmented).frame(maxWidth: 240)
        }
    }

    private var roundFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            multiColumn("Round these", binding: $state.action.columns, limitedTo: [.number])
            Stepper("Decimal places: \(state.action.digits)",
                    value: $state.action.digits, in: 0...8)
                .frame(maxWidth: 240)
        }
    }

    private var mutateFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            LabeledContent("Call it") {
                TextField("new_column", text: $state.action.newName)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 220)
            }
            LabeledContent("Worked out as") {
                TextField("income / age", text: $state.action.expression)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 300)
                    .font(.system(.body, design: .monospaced))
            }
            Text("Use the column names as they are, and + - * / . This goes into the "
               + "script exactly as typed, so R decides whether it makes sense.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var renameFields: some View {
        HStack(spacing: 10) {
            Picker("", selection: $state.action.column) {
                ForEach(state.columnNames(), id: \.self) { Text($0).tag($0) }
            }.labelsHidden().frame(maxWidth: 190)
            Text("to").foregroundStyle(.secondary)
            TextField("new name", text: $state.action.newName)
                .textFieldStyle(.roundedBorder).frame(maxWidth: 190)
        }
    }

    private var summariseFields: some View {
        VStack(alignment: .leading, spacing: 12) {
            multiColumn("For each", binding: $state.action.groups)
            VStack(alignment: .leading, spacing: 6) {
                Text("Work out").font(.callout.weight(.medium))
                FlowChips(all: Statistic.allCases.map(\.label),
                          chosen: statisticsBinding)
            }
            if state.action.statistics.contains(where: { $0 != .count }) {
                columnMenu("Of column", selection: $state.action.target, limitedTo: [.number])
            }
        }
    }

    private var statisticsBinding: Binding<[String]> {
        Binding(get: { state.action.statistics.map(\.label) },
                set: { labels in
                    state.action.statistics = Statistic.allCases.filter { labels.contains($0.label) }
                })
    }

    private var headFields: some View {
        Stepper("Keep the first \(state.action.count) rows",
                value: $state.action.count, in: 1...10_000)
            .frame(maxWidth: 300)
    }

    private var fillFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Column", selection: $state.action.column, limitedTo: [.number])
            LabeledContent("Fill the gaps with") {
                Picker("", selection: $state.action.fill) {
                    ForEach(FillMethod.allCases, id: \.self) { Text($0.label).tag($0) }
                }.labelsHidden().frame(maxWidth: 260)
            }
            if state.action.fill == .value {
                LabeledContent("That value") {
                    TextField("", text: $state.action.value)
                        .textFieldStyle(.roundedBorder).frame(maxWidth: 160)
                }
            }
        }
    }

    private var bandFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Number to band", selection: $state.action.column, limitedTo: [.number])
            LabeledContent("Call the new column") {
                TextField("band", text: $state.action.newName)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 200)
            }
            Stepper("How many bands: \(state.action.bands)",
                    value: $state.action.bands, in: 2...20).frame(maxWidth: 240)
            Picker("", selection: $state.action.equalWidth) {
                Text("same number of rows in each").tag(false)
                Text("same value range in each").tag(true)
            }.labelsHidden().pickerStyle(.radioGroup)
        }
    }

    /// Where a "save this" step writes. Shown as the path that will appear in
    /// the script, because that is the thing that has to be right.
    private func outputFields(ext: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            LabeledContent("Save it as") {
                HStack(spacing: 8) {
                    TextField("outputs/result.\(ext)", text: $state.action.outputPath)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.body, design: .monospaced))
                        .frame(maxWidth: 300)
                    Button("Choose…") { chooseOutput(ext: ext) }
                }
            }
            Text(state.action.outputPath.hasPrefix("/")
                 ? "An absolute path, so this script will only write there on this machine."
                 : "Relative to the project, so the script still works on another machine. "
                 + "Handrail makes the folder if it does not exist.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func chooseOutput(ext: String) {
        guard let project = state.project else { return }
        let panel = NSSavePanel()
        panel.directoryURL = project.folder
        panel.nameFieldStringValue = (state.action.outputPath as NSString).lastPathComponent
        panel.message = "Where should the script write this?"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        state.action.outputPath = project.referenceTo(url)
    }

    /// A CSV, plus the one option that stops accented characters arriving as
    /// mojibake for whoever opens it in Excel on Windows.
    private var csvFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            outputFields(ext: "csv")
            Toggle("Add the mark Excel on Windows needs for accented characters",
                   isOn: $state.action.forExcel)
                .font(.callout)
        }
    }

    private var delimitedFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            LabeledContent("Separated by") {
                Picker("", selection: $state.action.textFormat) {
                    ForEach(TextFormat.allCases, id: \.self) { Text($0.label).tag($0) }
                }.labelsHidden().frame(maxWidth: 260)
            }
            outputFields(ext: state.action.textFormat.fileExtension)
        }
    }

    private var statsFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            LabeledContent("For") {
                Picker("", selection: $state.action.statsFormat) {
                    ForEach(StatsFormat.allCases, id: \.self) { Text($0.label).tag($0) }
                }.labelsHidden().pickerStyle(.segmented).frame(maxWidth: 320)
            }
            outputFields(ext: state.action.statsFormat.fileExtension)
        }
    }

    /// Saving a summary is the summarise controls plus a path. It writes a file
    /// and leaves the data alone, which is the whole reason it is its own step.
    private var summaryFileFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            summariseFields
            Divider()
            outputFields(ext: "csv")
        }
    }

    private var crossTabFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Rows", selection: $state.action.column)
            columnMenu("Columns", selection: $state.action.target)
        }
    }

    private var peekFields: some View {
        Stepper("Show the first \(state.action.count) rows",
                value: $state.action.count, in: 1...200)
            .frame(maxWidth: 300)
    }

    /// The value to treat as missing, offered from what is actually in the
    /// column rather than typed from memory.
    private var markMissingFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Column", selection: $state.action.column)
            LabeledContent("Treat this value as missing") {
                TextField("Blank", text: $state.action.value)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 200)
            }
            if let marker = state.columns.first(where: { $0.name == state.action.column })?.sentinel,
               state.action.value != marker {
                Button("Use \"\(marker)\" — found in this column") {
                    state.action.value = marker
                }
                .buttonStyle(.link).font(.callout)
            }
        }
    }

    private var dateFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Column", selection: $state.action.column)
            LabeledContent("It is written like") {
                Picker("", selection: $state.action.dateFormat) {
                    Text("2024-03-15").tag("%Y-%m-%d")
                    Text("15/03/2024").tag("%d/%m/%Y")
                    Text("03/15/2024").tag("%m/%d/%Y")
                    Text("2024/03/15").tag("%Y/%m/%d")
                }.labelsHidden().frame(maxWidth: 200)
            }
        }
    }

    private var datePartFields: some View {
        VStack(alignment: .leading, spacing: 10) {
            columnMenu("Date column", selection: $state.action.column, limitedTo: [.date])
            LabeledContent("Pull out") {
                Picker("", selection: $state.action.datePiece) {
                    ForEach(DatePiece.allCases, id: \.self) { Text($0.label).tag($0) }
                }.labelsHidden().frame(maxWidth: 220)
            }
            LabeledContent("Call it") {
                TextField("year", text: $state.action.newName)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 200)
            }
            if state.columnNames([.date]).isEmpty {
                Text("No date columns yet. Use \"Treat a column as dates\" first.")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
    }
}

/// Multi-select as a row of toggles, because a multi-select list box is the
/// control everyone gets wrong on the first try.
///
/// It also has to hold up against a survey file: two hundred chips is not a
/// control, so past a couple of dozen it gains a filter, and whatever is already
/// chosen stays pinned at the top where it can be taken off again.
struct FlowChips: View {
    let all: [String]
    @Binding var chosen: [String]
    @State private var filter = ""

    private static let filterAbove = 24
    private static let showAtMost = 60

    private var matching: [String] {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        let rest = all.filter { !chosen.contains($0) }
        guard !q.isEmpty else { return rest }
        return rest.filter { $0.lowercased().contains(q) }
    }

    private var visible: [String] { Array(matching.prefix(Self.showAtMost)) }
    private var hidden: Int { max(0, matching.count - visible.count) }

    var body: some View {
        if all.isEmpty {
            Text("no suitable columns").font(.callout).foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 7) {
                if all.count > Self.filterAbove {
                    TextField("Find a column", text: $filter)
                        .textFieldStyle(.roundedBorder)
                        .font(.callout)
                        .frame(maxWidth: 260)
                }

                // Chosen first, always visible, so a filter can never hide what
                // you have already picked.
                if !chosen.isEmpty { grid(chosen, on: true) }
                if !visible.isEmpty { grid(visible, on: false) }

                if hidden > 0 {
                    Text("\(hidden) more — type to narrow the list.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if !filter.isEmpty && visible.isEmpty {
                    Text("No column with that in its name.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func grid(_ names: [String], on: Bool) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 110), spacing: 6)],
                  alignment: .leading, spacing: 6) {
            ForEach(names, id: \.self) { name in
                Button {
                    if chosen.contains(name) { chosen.removeAll { $0 == name } }
                    else { chosen.append(name) }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: on ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(on ? Color.accentColor : Color.secondary)
                        Text(name).lineLimit(1)
                    }
                    .font(.callout)
                    .padding(.vertical, 5).padding(.horizontal, 9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(on ? Color.accentColor.opacity(0.12)
                                   : Color(nsColor: .controlBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                    .overlay(RoundedRectangle(cornerRadius: 7)
                        .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1))
                }
                .buttonStyle(.plain)
            }
        }
    }
}
