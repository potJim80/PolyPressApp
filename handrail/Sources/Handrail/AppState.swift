import Foundation
import SwiftUI
import HandrailCore

/// Everything the app knows, in one place.
///
/// There is very little of it on purpose: the app holds no data, computes no
/// results and remembers no analysis. Its whole job is to know which project,
/// which script and which file, and to turn a filled-in action into a line of R.
/// Anything more and it starts becoming the thing it exists to avoid.
/// UI state, so it lives on the main actor. Saying so is what lets the
/// whole-file count hand its work to a background task and come back safely,
/// rather than Swift having to assume the worst about every property.
@MainActor
@Observable
final class AppState {

    // MARK: - what it is pointed at

    var project: RProject? { didSet { rescanProject() } }

    /// The scripts and data files in the project, listed once when it is opened.
    ///
    /// These were being read straight from the menus, which put a directory scan
    /// on the render path — disk work on every keystroke in the form. They
    /// change when the project does, and when the user asks, and not otherwise.
    private(set) var scripts: [URL] = []
    private(set) var dataFiles: [URL] = []

    func rescanProject() {
        scripts = project?.scripts() ?? []
        dataFiles = project?.dataFiles() ?? []
    }
    var scriptURL: URL?
    var dataURL: URL?
    var data: DataFile?
    /// The variable the file is read into, named after the file. See
    /// RCode.frameName for why it is not `data`.
    var frameName: String = "my_data"

    /// A write waiting on a yes, because the script already has someone's work
    /// in it. Handrail appends, so it cannot damage what is there — but landing
    /// a chunk of unrelated R at the end of a finished program is still not
    /// something to do without asking.
    var confirm: PendingWrite?

    // MARK: - looking at a column's values

    /// The column whose values are on screen, if any.
    var valuesFor: String?
    var tally: ValueTally?
    var counting = false
    private var countingTask: Task<Void, Never>?

    /// Opens the values list.
    ///
    /// Two answers, in that order: the rows already read, which is instant, and
    /// then the whole file, which replaces it. The second one is not a button
    /// any more. "What is in this column" has to mean the column — a list built
    /// from the first few hundred rows is the same lie the app exists to stop,
    /// and it costs about a second on a 278 MB file to tell the truth.
    func showValues(for column: String) {
        guard let data else { return }
        countingTask?.cancel()
        counting = false
        valuesFor = column
        tally = ColumnValues.fromSample(data, column: column)
        if data.truncated { countWholeFile() }
    }

    func closeValues() {
        countingTask?.cancel()
        countingTask = nil
        counting = false
        valuesFor = nil
        tally = nil
    }

    /// Counts every row. A pass over the file, so it runs off the main thread
    /// and can be abandoned — the answer from the sample stays on screen until
    /// the real one replaces it.
    func countWholeFile() {
        guard let dataURL, let data, let column = valuesFor, !counting else { return }
        if tally?.complete == true { return }
        counting = true
        // Only Sendable values cross into the background task — the URL, the
        // already-read file, and the column name. Nothing reaches back for self.
        countingTask = Task {
            let result: ValueTally? = await Task.detached(priority: .userInitiated) {
                try? ColumnValues.fromWholeFile(dataURL, column: column, in: data,
                                                shouldStop: { Task.isCancelled })
            }.value
            if Task.isCancelled { return }
            if let result, result.complete { self.tally = result }
            self.counting = false
        }
    }

    /// Start a filter on a value the user picked out of that list — the reason
    /// for looking at the values in the first place.
    func filterBy(value: String) {
        guard let column = valuesFor else { return }
        var a = Action(kind: .filter)
        a.column = column
        a.comparison = .eq
        a.value = value
        action = a
        chosenKind = .filter
        closeValues()
    }

    struct PendingWrite: Identifiable {
        let id = UUID()
        let text: String
        let scriptName: String
        let lines: Int
    }

    // MARK: - the action being built

    var chosenKind: ActionKind?
    var action = Action(kind: .filter)
    var search: String = ""

    // MARK: - what happened

    /// What this app has appended, newest last. Only used to offer to take the
    /// last one back — the script itself is the record, not this.
    var added: [Addition] = []
    var problem: String?
    var note: String?

    struct Addition: Identifiable {
        let id = UUID()
        let sentence: String
        let text: String
    }

    // MARK: - remembering across launches

    private let defaults = UserDefaults.standard

    init() { restore() }

    func restore() {
        if let p = defaults.string(forKey: "projectFolder") {
            let url = URL(fileURLWithPath: p)
            if FileManager.default.fileExists(atPath: url.path) { project = RProject(folder: url) }
        }
        if let s = defaults.string(forKey: "scriptURL") {
            let url = URL(fileURLWithPath: s)
            if FileManager.default.fileExists(atPath: url.path) { scriptURL = url }
        }
        if let d = defaults.string(forKey: "dataURL") {
            let url = URL(fileURLWithPath: d)
            if FileManager.default.fileExists(atPath: url.path) { load(data: url) }
        }
    }

    private func remember() {
        defaults.set(project?.folder.path, forKey: "projectFolder")
        defaults.set(scriptURL?.path, forKey: "scriptURL")
        defaults.set(dataURL?.path, forKey: "dataURL")
    }

    // MARK: - pointing it somewhere

    func open(project folder: URL) {
        project = RProject(folder: folder)   // rescans
        // A project usually has one obvious script and one obvious data file.
        // Choosing them saves two clicks and is trivially overridden.
        // Deliberately NOT scripts.first. Picking the alphabetically first .R
        // file silently chose somebody's finished analysis script and started
        // appending to it — which is exactly what happened the first time this
        // was pointed at a real project.
        if let current = scriptURL, !current.path.hasPrefix(folder.path) { scriptURL = nil }
        if scriptURL == nil, scripts.count == 1,
           let only = scripts.first,
           ((try? String(contentsOf: only, encoding: .utf8))?.count ?? 0) < 400 {
            scriptURL = only   // one short script in the project is unambiguous
        }
        if dataURL == nil || !(dataURL!.path.hasPrefix(folder.path)) {
            if let first = dataFiles.first { load(data: first) }
        }
        remember()
    }

    func load(data url: URL) {
        do {
            data = try DataFile(url: url)
            dataURL = url
            frameName = RCode.frameName(for: url)
            problem = nil
            resetAction()
        } catch {
            data = nil
            dataURL = nil
            problem = error.localizedDescription
        }
        remember()
    }

    func choose(script url: URL) {
        scriptURL = url
        note = nil
        problem = nil
        remember()
    }

    // MARK: - building

    var columns: [Column] { data?.columns ?? [] }
    var types: [String: ColumnType] { data?.types ?? [:] }

    /// Column names, optionally only of certain kinds. Reads precomputed lists
    /// rather than filtering: this is called by every picker in the form, and a
    /// form re-renders on every keystroke.
    func columnNames(_ kinds: Set<ColumnType>? = nil) -> [String] {
        guard let data else { return [] }
        guard let kinds else { return data.allNames }
        if kinds.count == 1, let only = kinds.first { return data.namesByType[only] ?? [] }
        // Filtering the full list keeps the file's column order, which matters:
        // a picker that reorders the columns is a picker you cannot find things in.
        return data.allNames.filter { kinds.contains(data.types[$0] ?? .text) }
    }

    func resetAction() {
        chosenKind = nil
        search = ""
        action = Action(kind: .filter)
    }

    /// Starts an action, filling in whatever can be filled in from the column
    /// that was clicked. Landing on a half-built step with the column already
    /// chosen is the difference between "pick a verb" and "do this to that".
    func begin(_ kind: ActionKind, column: String? = nil) {
        var a = Action(kind: kind)
        let col = column ?? columns.first?.name ?? ""
        switch kind {
        case .filter, .sort, .rename, .toNumber, .toDate, .toText:
            a.column = col
        case .markMissing:
            a.column = col
            // Offer the marker actually found in that column, rather than a
            // blank box to guess into.
            a.value = columns.first { $0.name == col }?.sentinel ?? ""
        case .fillNA:
            a.column = column ?? columnNames([.number]).first ?? col
        case .band:
            a.column = column ?? columnNames([.number]).first ?? col
            a.newName = "\(a.column)_band"
        case .datePart:
            a.column = column ?? columnNames([.date]).first ?? col
            a.newName = "year"
        case .select, .distinct, .dropNA:
            a.columns = column.map { [$0] } ?? []
        case .round:
            a.columns = column.map { [$0] } ?? []
            if a.columns.isEmpty, let n = columnNames([.number]).first { a.columns = [n] }
        case .summarise:
            a.groups = column.map { [$0] } ?? []
            if a.groups.isEmpty, let t = columnNames([.text, .logical]).first { a.groups = [t] }
            a.target = columnNames([.number]).first ?? ""
        case .mutate:
            a.newName = "new_column"
        case .head:
            break
        case .saveCSV:
            // A name that says what it is and where it came from, ready to
            // change — better than an empty box or "output.csv".
            a.outputPath = suggestedOutput(extension: "csv")
        case .saveRDS:
            a.outputPath = suggestedOutput(extension: "rds")
        case .saveExcel:
            a.outputPath = suggestedOutput(extension: "xlsx")
        case .saveDelimited:
            a.outputPath = suggestedOutput(extension: a.textFormat.fileExtension)
        case .saveForStats:
            a.outputPath = suggestedOutput(extension: a.statsFormat.fileExtension)
        case .saveSummary:
            a.groups = column.map { [$0] } ?? []
            if a.groups.isEmpty, let t = columnNames([.text, .logical]).first { a.groups = [t] }
            a.target = columnNames([.number]).first ?? ""
            a.outputPath = suggestedOutput(extension: "csv")
        case .crossTab:
            a.column = column ?? columnNames([.text, .logical]).first ?? ""
            a.target = columnNames([.text, .logical]).first(where: { $0 != a.column }) ?? ""
        case .summaryOf:
            a.columns = column.map { [$0] } ?? []
        case .peek:
            a.count = 10
        case .viewIt, .countRows, .glimpse:
            break
        case .countValues:
            a.column = column ?? columnNames([.text, .logical]).first ?? col
            a.digits = 1
            a.descending = true
        case .describeNumber:
            a.column = column ?? columnNames([.number]).first ?? col
        case .missingReport:
            a.columns = column.map { [$0] } ?? []
            a.digits = 1
        case .duplicateReport:
            a.columns = column.map { [$0] } ?? []
        }
        if a.kind.emits == .result {
            // Read the names the script already uses, so step 11 cannot quietly
            // reassign what step 3 made. Reading a file is fine here — begin()
            // runs on a click, never in a view body.
            let taken = (try? writer?.assignedNames()) ?? []
            a.resultName = RCode.resultName(RCode.defaultResultName(for: a),
                                            fallback: "result",
                                            taken: taken)
        }
        if a.kind == .filter, let t = types[a.column] {
            // "contains" on a number is never what anyone means.
            a.comparison = t == .text ? .eq : .gt
        }
        action = a
        chosenKind = kind
    }

    /// Where a "save this" step should write by default: beside the project, in
    /// an outputs folder, named after the data it came from.
    func suggestedOutput(extension ext: String) -> String {
        let stem = frameName.isEmpty ? "result" : frameName
        return "outputs/\(stem)_result.\(ext)"
    }

    /// The folder a relative output path points at, created on demand — R will
    /// not make a directory for you, and `write.csv` into a folder that is not
    /// there fails with a message about a connection.
    func ensureOutputFolder(for relative: String) {
        guard let project, !relative.hasPrefix("/") else { return }
        let target = project.folder.appendingPathComponent(relative)
            .deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
    }

    /// The R this action would write, or nil while it is still half-filled.
    ///
    /// Worked out once per change rather than per read: the code preview, the
    /// Add button and the append path all ask for it, and each of them runs on
    /// every render of the form.
    ///
    /// It has to come from `RCode.block` and nowhere else. A second copy of the
    /// pipe wrapper lived here once, and it did not know that a finishing step
    /// stands alone — so saving a CSV wrote
    /// `data <- data |> write.csv(...)`, which assigns NULL over the data and
    /// then fails on the next line for a reason that reads like nonsense.
    private var cachedFor: Action?
    private var cachedFrame: String?
    private var cachedBlock: String??

    var block: String? {
        if cachedFor == action, cachedFrame == frameName, let hit = cachedBlock { return hit }
        let out = RCode.block(for: action, types: types, frame: frameName)
        cachedFor = action
        cachedFrame = frameName
        cachedBlock = out
        return out
    }

    var sentence: String { RCode.sentence(for: action) }

    var caution: String? { RCode.caution(for: action, types: types) }

    var canAdd: Bool { block != nil && scriptURL != nil }

    // MARK: - writing it out

    var writer: ScriptWriter? {
        guard let scriptURL, let project else { return nil }
        return ScriptWriter(script: scriptURL, project: project)
    }

    /// Appends the action to the script. The preamble goes in first if the
    /// script does not already read the data — once, and only when it is
    /// missing, so a script he started himself is joined rather than taken over.
    func addToScript() {
        guard let writer, let block, let dataURL, let scriptURL else { return }
        do {
            var text = ""
            if try !writer.hasPreamble(frame: frameName) {
                text += writer.preamble(dataFile: dataURL, frame: frameName)
                text += "\n"
            }
            text += block

            // First time into a script that already holds work: ask.
            let lines = try writer.existingLineCount()
            if try !writer.hasWrittenHereBefore(), lines > 10 {
                confirm = PendingWrite(text: text,
                                       scriptName: scriptURL.lastPathComponent,
                                       lines: lines)
                return
            }
            commit(text)
        } catch {
            problem = error.localizedDescription
        }
    }

    func confirmWrite() {
        guard let pending = confirm else { return }
        confirm = nil
        commit(pending.text)
    }

    /// Scripts already shown in RStudio this session, so it is opened once and
    /// then left alone rather than stealing focus on every step.
    private var revealed = Set<String>()

    private func commit(_ text: String) {
        guard let writer, let scriptURL else { return }
        if action.kind.group == .save {
            ensureOutputFolder(for: action.outputPath)
        }
        do {
            let appended = try writer.append(text)
            added.append(Addition(sentence: sentence, text: appended))
            problem = nil

            // RStudio reloads a file it has OPEN. If the script is not open,
            // the line lands on disk and absolutely nothing happens on screen —
            // which is the whole promise failing quietly. So the first step
            // written to a script opens it, once.
            if !revealed.contains(scriptURL.path) {
                revealed.insert(scriptURL.path)
                writer.revealInRStudio()
                note = "Added to \(scriptURL.lastPathComponent), and opened it in RStudio."
            } else {
                note = "Added to \(scriptURL.lastPathComponent)."
            }
            resetAction()
        } catch {
            problem = error.localizedDescription
        }
    }

    /// True when the data file sits outside the project, so the script has to
    /// name it by absolute path and will not run on another machine.
    var dataIsOutsideProject: Bool {
        guard let dataURL, let project else { return false }
        return !dataURL.standardizedFileURL.path
            .hasPrefix(project.folder.standardizedFileURL.path + "/")
    }

    /// Takes the last addition back off — but only if the file still ends with
    /// exactly what was written. If he has typed since, the file is his.
    func undoLast() {
        guard let writer, let last = added.last else { return }
        do {
            if try writer.remove(last.text) {
                added.removeLast()
                note = "Took that back out of \(scriptURL!.lastPathComponent)."
            } else {
                problem = "The script has changed since that was added, so it was left alone. "
                        + "Use Undo in RStudio instead."
            }
        } catch {
            problem = error.localizedDescription
        }
    }

    func openInRStudio() { writer?.revealInRStudio() }

    // MARK: - the palette

    func actions(matching query: String) -> [(ActionGroup, [ActionKind])] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        var out: [(ActionGroup, [ActionKind])] = []
        for group in ActionGroup.allCases {
            let kinds = ActionKind.allCases.filter { kind in
                guard kind.group == group else { return false }
                guard !q.isEmpty else { return true }
                return kind.title.lowercased().contains(q)
                    || kind.blurb.lowercased().contains(q)
                    || kind.keywords.contains(q)
            }
            if !kinds.isEmpty { out.append((group, kinds)) }
        }
        return out
    }
}
