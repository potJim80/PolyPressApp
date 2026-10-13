// Polypress.app -- a window around the C program.
//
// Four sections (page/index.html): Compress and Restore, where files are
// dropped or chosen, listed, and then processed to a location the user picks
// (ask each time, the original's folder, or a chosen folder); History, kept
// between sessions in ~/Library/Application Support/Polypress/history.json;
// and Settings. The look is the suite theme shared with polypressapp.com.
//
// All the work is csrc/polypress, shipped inside the bundle and run as a
// process; this file only decides names, runs it, and hands the page a list
// to draw (page/index.html). Parquet goes through py/parquet.py when the
// system Python has pyarrow, and is simply not offered when it has not.
//
// Built by app/build_app.sh. `Polypress --selftest` runs every action on
// generated files without opening a window.

import AppKit
import UniformTypeIdentifiers
import WebKit

// ─── Where things live ───────────────────────────────────────────────────────
let RES = Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
let BINARY: String = {
    if let p = ProcessInfo.processInfo.environment["POLYPRESS"], !p.isEmpty { return p }
    return RES.appendingPathComponent("polypress").path
}()
let BRIDGE: String = {
    if let p = ProcessInfo.processInfo.environment["POLYPRESS_PARQUET"], !p.isEmpty { return p }
    return RES.appendingPathComponent("parquet.py").path
}()
let PYTHON = "/usr/bin/python3"

// The program's own version line, "polypress 1.00 (build f258d53)" -> the
// window shows "Polypress 1.00 · build f258d53"; each archive's details say
// which version made it (`info` reads that from the archive itself).
let VERSION: String = {
    let r = run(BINARY, ["--version"])
    let line = r.out.trimmingCharacters(in: .whitespacesAndNewlines)
    guard r.ok, line.hasPrefix("polypress ") else { return "" }
    return "Polypress " + line.dropFirst(10)
        .replacingOccurrences(of: " (build ", with: " · build ")
        .replacingOccurrences(of: ")", with: "")
}()
let PAGE = RES.appendingPathComponent("page/index.html")

// Above this a table is compressed a block at a time: memory stays bounded,
// the blocks run side by side, and the program can say how far it has got
// -- which is what the progress bar shows. Below it a file takes seconds.
var STREAM_ABOVE: Int64 = 64 << 20          // var: the self-test lowers it

let ARCHIVE_MAGICS: Set<String> = ["PPZ2", "PPZS"]
let TABLE_FORMATS = ["csv", "tsv", "json", "jsonl"]
// Stata, SPSS and SAS: kept byte for byte with their labels, never streamed
let STAT_FORMATS = ["dta", "sav", "zsav", "por", "sas7bdat", "xpt"]

// ─── Running things ──────────────────────────────────────────────────────────
struct Ran { let ok: Bool; let out: String; let err: String; let secs: Double; let stopped: Bool }

/// Run a program to the end. `line` sees each line it writes to stderr as it
/// arrives -- that is how progress gets here -- and `started` is handed the
/// process, so the Stop link can end it.
func run(_ exe: String, _ args: [String], line: ((String) -> Void)? = nil,
         started: ((Process) -> Void)? = nil) -> Ran {
    // Never this window's own executable. On a case-insensitive disk a missing
    // "polypress" beside "Polypress" names this very file, and each copy would
    // start another until the Mac runs out of processes.
    if let me = Bundle.main.executablePath, sameFile(exe, me) {
        return Ran(ok: false, out: "", err: "cannot start \(exe): it is Polypress itself, not the program",
                   secs: 0, stopped: false)
    }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    let o = Pipe(), e = Pipe()
    p.standardOutput = o
    p.standardError = e
    let t0 = Date()
    do { try p.run() } catch {
        return Ran(ok: false, out: "", err: "cannot start \(exe): \(error.localizedDescription)",
                   secs: 0, stopped: false)
    }
    started?(p)
    // read both before waiting, or a chatty process fills a pipe and stalls
    var outData = Data(), errText = ""
    let g = DispatchGroup()
    g.enter(); DispatchQueue.global().async { outData = o.fileHandleForReading.readDataToEndOfFile(); g.leave() }
    g.enter(); DispatchQueue.global().async {
        var pending = Data()
        let h = e.fileHandleForReading
        while true {
            let chunk = h.availableData
            if chunk.isEmpty { break }
            pending.append(chunk)
            while let nl = pending.firstIndex(of: 10) {
                let l = String(decoding: pending[pending.startIndex..<nl], as: UTF8.self)
                pending.removeSubrange(pending.startIndex...nl)
                if l.hasPrefix("progress ") || l.hasPrefix("stage ") { line?(l) }
                else { errText += l + "\n" }
            }
        }
        errText += String(decoding: pending, as: UTF8.self)
        g.leave()
    }
    p.waitUntilExit()
    g.wait()
    return Ran(ok: p.terminationStatus == 0, out: String(decoding: outData, as: UTF8.self),
               err: errText, secs: Date().timeIntervalSince(t0),
               stopped: p.terminationReason == .uncaughtSignal)
}

/// The same file on disk (device and file number), whatever the spelling or links.
func sameFile(_ a: String, _ b: String) -> Bool {
    let fm = FileManager.default
    guard let x = try? fm.attributesOfItem(atPath: (a as NSString).resolvingSymlinksInPath),
          let y = try? fm.attributesOfItem(atPath: (b as NSString).resolvingSymlinksInPath),
          let xd = x[.systemNumber] as? NSNumber, let xi = x[.systemFileNumber] as? NSNumber
    else { return false }
    return xd == y[.systemNumber] as? NSNumber && xi == y[.systemFileNumber] as? NSNumber
}

/// The program's refusal, as the sentence it already is -- with the file
/// called by its name rather than its full path, since the row already says
/// which file it is.
func reason(_ r: Ran, _ paths: [String] = []) -> String {
    var t = r.err.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.hasPrefix("polypress: ") { t = String(t.dropFirst(11)) }
    for p in paths.sorted(by: { $0.count > $1.count }) {
        t = t.replacingOccurrences(of: p, with: (p as NSString).lastPathComponent)
    }
    return t.isEmpty ? "something went wrong" : t.replacingOccurrences(of: "\n", with: " ")
}

func fileSize(_ path: String) -> Int64? {
    (try? FileManager.default.attributesOfItem(atPath: path)[.size] as? NSNumber)?.int64Value
}

func isArchive(_ path: String) -> Bool {
    let low = path.lowercased()
    if low.hasSuffix(".ppz") { return true }
    guard let h = FileHandle(forReadingAtPath: path) else { return false }
    defer { h.closeFile() }
    let d = h.readData(ofLength: 4)
    return ARCHIVE_MAGICS.contains(String(decoding: d, as: UTF8.self))
}

func isStat(_ path: String) -> Bool {
    STAT_FORMATS.contains((path as NSString).pathExtension.lowercased())
}

func isParquet(_ path: String) -> Bool {
    let low = path.lowercased()
    return low.hasSuffix(".parquet") || low.hasSuffix(".pq")
}

/// What a translation of a Stata/SPSS/SAS file left out: the indented lines
/// the program prints under "... is a translation of the Stata file:".
func translationNotes(_ out: String) -> [String] {
    guard out.contains("is a translation of") else { return [] }
    return out.split(separator: "\n").filter { $0.hasPrefix("  ") }
        .map { $0.trimmingCharacters(in: .whitespaces) }
}

/// `dir/stem.ext`, or `dir/stem 2.ext`, `stem 3.ext`... -- never over a file.
func unused(_ dir: String, _ stem: String, _ ext: String) -> String {
    let dot = ext.isEmpty ? "" : "." + ext
    var p = (dir as NSString).appendingPathComponent(stem + dot)
    var n = 2
    while FileManager.default.fileExists(atPath: p) {
        p = (dir as NSString).appendingPathComponent("\(stem) \(n)\(dot)")
        n += 1
    }
    return p
}

/// "survey.csv.ppz" -> ("survey", "csv"); "x.ppz" -> ("x", "csv");
/// "survey.dta 2.ppz" -> ("survey 2", "dta").
func restoredName(_ archive: String) -> (String, String) {
    var base = (archive as NSString).lastPathComponent
    let low = base.lowercased()
    if low.hasSuffix(".ppz") { base = String(base.dropLast(4)) }
    var ext = (base as NSString).pathExtension.lowercased()
    var stem = (base as NSString).deletingPathExtension
    // "survey.csv 2.ppz" (a second archive of survey.csv) -> ("survey 2", "csv");
    // NSString sees no extension in "survey.csv 2", so split it by hand
    if let dot = base.lastIndex(of: "."), let sp = base.lastIndex(of: " "), sp > dot,
       Int(base[base.index(after: sp)...]) != nil {
        ext = base[base.index(after: dot)..<sp].lowercased()
        stem = String(base[..<dot]) + String(base[sp...])
    }
    let known = TABLE_FORMATS + STAT_FORMATS + ["psv", "txt", "parquet"]
    if known.contains(ext) { return (stem, ext == "txt" ? "csv" : ext) }
    return (base, "csv")
}

final class Scratch {
    let dir: String
    init() {
        dir = (NSTemporaryDirectory() as NSString).appendingPathComponent("polypress-" + UUID().uuidString)
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }
    func path(_ name: String) -> String { (dir as NSString).appendingPathComponent(name) }
    deinit { try? FileManager.default.removeItem(atPath: dir) }
}

// ─── History and settings ────────────────────────────────────────────────────
/// Every compression and restore, kept between sessions (newest first, at most
/// 500). POLYPRESS_HISTORY names another file, for the self-test.
final class History {
    struct Item: Codable {
        let id: String
        let date: Double             // ms since 1970
        let kind: String
        let src: String
        let out: String
        let from: Int64?
        let to: Int64?
        let state: String
        let message: String
        let secs: Double?
    }
    let url: URL
    private(set) var items: [Item] = []
    private var existsCache: [String: Bool] = [:]
    private var existsAt = Date.distantPast

    static var defaultURL: URL {
        if let p = ProcessInfo.processInfo.environment["POLYPRESS_HISTORY"], !p.isEmpty {
            return URL(fileURLWithPath: p)
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Polypress/history.json")
    }

    init(url: URL = History.defaultURL) {
        self.url = url
        guard let d = try? Data(contentsOf: url) else { return }
        if let v = try? JSONDecoder().decode([Item].self, from: d) { items = v; return }
        // unreadable (damaged, or written by a later version): keep it before it is replaced
        let bak = url.deletingLastPathComponent()
            .appendingPathComponent("history-unreadable-\(Int(Date().timeIntervalSince1970)).json")
        try? FileManager.default.copyItem(at: url, to: bak)
    }

    func add(_ j: Job) {
        items.insert(Item(id: UUID().uuidString, date: Date().timeIntervalSince1970 * 1000, kind: j.kind.rawValue,
                          src: j.src, out: j.out, from: j.from, to: j.to, state: j.state.rawValue,
                          message: j.message, secs: j.secs), at: 0)
        if items.count > 500 { items.removeLast(items.count - 500) }
        save()
    }
    func remove(_ id: String) { items.removeAll { $0.id == id }; save() }
    func clear() { items.removeAll(); save() }

    private func save() {
        existsAt = .distantPast
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let d = try? JSONEncoder().encode(items) { try? d.write(to: url, options: .atomic) }
    }

    /// For the page. Whether each saved file still exists is checked at most
    /// every two seconds, since the page is redrawn on every progress report.
    var json: [[String: Any]] {
        let fresh = Date().timeIntervalSince(existsAt) > 2
        if fresh { existsCache = [:]; existsAt = Date() }
        return items.map { x in
            if fresh || existsCache[x.out] == nil {
                existsCache[x.out] = !x.out.isEmpty && FileManager.default.fileExists(atPath: x.out)
            }
            var d: [String: Any] = ["id": x.id, "date": x.date, "kind": x.kind, "src": x.src,
                                    "srcName": (x.src as NSString).lastPathComponent, "out": x.out,
                                    "outName": (x.out as NSString).lastPathComponent, "state": x.state,
                                    "message": x.message, "outExists": existsCache[x.out] ?? false]
            if let v = x.from { d["from"] = v }
            if let v = x.to { d["to"] = v }
            if let v = x.secs { d["secs"] = v }
            return d
        }
    }
}

/// Where results are saved: "ask" (a save panel, or a folder for several
/// files), "same" (the original's folder) or "folder" (one chosen folder).
enum Prefs {
    static var saveMode: String {
        get { UserDefaults.standard.string(forKey: "saveMode") ?? "ask" }
        set { UserDefaults.standard.set(newValue, forKey: "saveMode") }
    }
    static var folder: String? {
        get { UserDefaults.standard.string(forKey: "saveFolder") }
        set { UserDefaults.standard.set(newValue, forKey: "saveFolder") }
    }
}

func isDirectory(_ p: String) -> Bool {
    var d: ObjCBool = false
    return FileManager.default.fileExists(atPath: p, isDirectory: &d) && d.boolValue
}

// ─── Jobs ────────────────────────────────────────────────────────────────────
enum Kind: String { case compress, restore, convert }
enum State: String { case queued, working, done, failed, stopped }

final class Job {
    static var next = 1
    let id: Int
    let src: String
    let kind: Kind
    let format: String?          // restore/convert into this; nil = the natural one
    var state = State.queued
    var note = ""
    var message = ""
    var out = ""
    var from: Int64?
    var to: Int64?
    var secs: Double?
    var stream = false
    var details: [String: Any]?
    var fraction: Double?        // 0...1 when the work can say how far it is
    var started: Date?
    var stageStarted: Date?
    var proc: Process?           // the program running for it, for Stop
    var stopAsked = false
    var outDir: String?          // save into this folder (a free name is chosen there)
    var outPath: String?         // or exactly here (the user picked it in a save panel)
    var recorded = false         // written to the history already

    init(src: String, kind: Kind, format: String? = nil) {
        id = Job.next; Job.next += 1
        self.src = src; self.kind = kind; self.format = format
        from = fileSize(src)
    }

    var dict: [String: Any] {
        var d: [String: Any] = ["id": id, "name": (src as NSString).lastPathComponent, "path": src,
                                "kind": kind.rawValue, "state": state.rawValue, "note": note,
                                "message": message, "stream": stream,
                                "out": out, "outName": (out as NSString).lastPathComponent]
        if let v = from { d["from"] = v }
        if let v = to { d["to"] = v }
        if let v = secs { d["secs"] = v }
        if let v = details { d["details"] = v }
        if let f = fraction { d["fraction"] = f }
        if let t = started { d["startedMs"] = t.timeIntervalSince1970 * 1000 }
        if let t = stageStarted { d["stageMs"] = t.timeIntervalSince1970 * 1000 }
        return d
    }
}

final class Work {
    var jobs: [Job] = []
    var parquet = false
    var changed: () -> Void = {}
    let queue = DispatchQueue(label: "polypress-jobs")   // one at a time: the encoder is threaded already
    let lock = NSLock()
    let history: History?
    /// Files chosen or dropped, waiting for the user to press Compress or Restore.
    var staged: [String: [String]] = ["compress": [], "restore": []]
    var notice = ""

    init(history: History? = nil) { self.history = history }

    /// Sort files into the two lists: archives to Restore, everything else to
    /// Compress. Returns the list that received files ("restore" only when every
    /// file was an archive), so the window can show it.
    @discardableResult
    func stage(_ paths: [String]) -> String {
        notice = ""
        var tables = 0, archives = 0, folders = 0, missing = 0
        for p in paths {
            if isDirectory(p) { folders += 1; continue }
            guard FileManager.default.fileExists(atPath: p) else { missing += 1; continue }
            let t = isArchive(p) ? "restore" : "compress"
            if t == "restore" { archives += 1 } else { tables += 1 }
            if !(staged[t] ?? []).contains(p) { staged[t, default: []].append(p) }
        }
        var notes: [String] = []
        if folders > 0 {
            notes.append(folders == 1 ? "A folder cannot be added. Choose the files inside it instead."
                                      : "\(folders) folders were not added. Choose the files inside them instead.")
        }
        if missing > 0 {
            notes.append(missing == 1 ? "The file could not be found. It may have been moved or deleted."
                                      : "\(missing) files could not be found. They may have been moved or deleted.")
        }
        notice = notes.joined(separator: " ")
        changed()
        return archives > 0 && tables == 0 ? "restore" : "compress"
    }

    func unstage(_ tab: String, _ path: String) { staged[tab]?.removeAll { $0 == path }; changed() }
    func clearStaged(_ tab: String) { staged[tab] = []; changed() }

    /// Start every staged file of a list. `outPath` is used only for a single file.
    func startStaged(_ tab: String, format: String?, outDir: String?, outPath: String?) {
        let items = staged[tab] ?? []
        guard !items.isEmpty else { return }
        staged[tab] = []
        notice = ""
        for p in items {
            let j = Job(src: p, kind: tab == "restore" ? .restore : .compress, format: format)
            j.outDir = outDir
            j.outPath = items.count == 1 ? outPath : nil
            enqueue(j)
        }
        changed()
    }

    func clearFinished(_ tab: String) {
        jobs.removeAll { (tab == "compress") == ($0.kind == .compress)
            && ($0.state == .done || $0.state == .failed || $0.state == .stopped) }
        changed()
    }

    /// Write a finished job to the history, once.
    func record(_ j: Job) {
        guard !j.recorded, j.state == .done || j.state == .failed || j.state == .stopped,
              let h = history else { return }
        j.recorded = true
        h.add(j)
    }

    var formats: [String] { TABLE_FORMATS + (parquet ? ["parquet"] : []) }

    func checkParquet(_ done: @escaping () -> Void = {}) {
        DispatchQueue.global().async {
            let ok = FileManager.default.fileExists(atPath: BRIDGE)
                && run(PYTHON, ["-c", "import pyarrow.parquet"]).ok
            DispatchQueue.main.async { self.parquet = ok; self.changed(); done() }
        }
    }

    func add(_ paths: [String]) {
        for p in paths {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: p, isDirectory: &isDir), isDir.boolValue {
                let j = Job(src: p, kind: .compress)
                j.state = .failed
                j.message = "a folder -- drop the tables inside it instead"
                jobs.append(j)
                continue
            }
            enqueue(Job(src: p, kind: isArchive(p) ? .restore : .compress))
        }
        changed()
    }


    /// Stop one file: a queued one never starts; a running one has its
    /// program ended, and the program removes its own half-written output.
    func stop(_ id: Int) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        lock.lock(); j.stopAsked = true; let p = j.proc; lock.unlock()
        if j.state == .queued { j.state = .stopped; j.message = "stopped"; record(j) }
        p?.terminate()
        changed()
    }

    func stopAll() {
        for j in jobs where j.state == .queued || j.state == .working { stop(j.id) }
    }

    private func enqueue(_ j: Job) {
        jobs.append(j)
        queue.async {
            if j.stopAsked { return }
            self.update(j) { $0.state = .working; $0.note = self.verb(j); $0.started = Date(); $0.stageStarted = Date() }
            self.perform(j)
        }
    }

    /// The program, run for a job: its progress lines move the bar, and the
    /// job can stop it. Returns nil (and marks the job stopped) if it was.
    private func tool(_ j: Job, _ exe: String, _ args: [String], stage: String? = nil) -> Ran? {
        if let s = stage { update(j) { $0.note = s; $0.fraction = nil; $0.stageStarted = Date() } }
        let r = run(exe, args, line: { l in
            let f = l.split(separator: " ")
            if f.first == "progress", f.count >= 3, let a = Double(f[1]), let b = Double(f[2]), b > 0 {
                self.update(j) { $0.fraction = min(1, a / b) }
            } else if f.first == "stage", f.count >= 2 {
                let words = ["reading": "reading", "compressing": "compressing",
                             "verifying": "checking every cell", "unpacking": "unpacking Parquet"]
                let w = words[String(f[1])] ?? String(f[1])
                self.update(j) {
                    if !$0.stream || w != "compressing" { $0.fraction = nil }
                    $0.note = $0.stream && w == "compressing" ? "compressing in blocks" : w
                    $0.stageStarted = Date()
                }
            }
        }, started: { p in
            self.lock.lock(); j.proc = p; let stop = j.stopAsked; self.lock.unlock()
            if stop { p.terminate() }
        })
        lock.lock(); j.proc = nil; let asked = j.stopAsked; lock.unlock()
        if r.stopped || (asked && !r.ok) {
            update(j) { $0.state = .stopped; $0.message = "stopped" ; $0.fraction = nil }
            return nil
        }
        return r
    }

    private func verb(_ j: Job) -> String {
        switch j.kind {
        case .compress: return "reading"
        case .restore: return "restoring"
        case .convert: return "converting"
        }
    }

    private func update(_ j: Job, _ f: (Job) -> Void) {
        if Thread.isMainThread { f(j); record(j); changed() }
        else { DispatchQueue.main.sync { f(j); record(j); changed() } }
    }

    private func fail(_ j: Job, _ msg: String) {
        update(j) { $0.state = .failed; $0.message = msg; $0.fraction = nil }
    }

    private func info(_ path: String) -> [String: Any]? {
        let r = run(BINARY, ["info", path, "--json"])
        guard r.ok, let d = r.out.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    }

    /// A message about the scratch CSV made from a Parquet file, about the Parquet file instead.
    private func named(_ msg: String, _ j: Job, _ input: String) -> String {
        input == j.src ? msg : msg.replacingOccurrences(of: (input as NSString).lastPathComponent,
                                                        with: (j.src as NSString).lastPathComponent)
    }

    /// Run the job synchronously. Called on the job queue (and by selftest).
    func perform(_ j: Job) {
        if let o = j.outPath,
           (o as NSString).standardizingPath == (j.src as NSString).standardizingPath {
            return fail(j, "The result cannot replace the file it is made from. Choose another name.")
        }
        let dir = (j.src as NSString).deletingLastPathComponent
        let sc = Scratch()
        let t0 = Date()

        // Parquet in: through the bridge to a CSV the program can read
        var input = j.src
        if j.kind != .restore && isParquet(j.src) {
            guard parquet else { return fail(j, "reading Parquet needs Python with pyarrow (pip3 install pyarrow)") }
            input = sc.path("in.csv")
            guard let r = tool(j, PYTHON, [BRIDGE, "to-csv", j.src, input, "--progress"],
                               stage: "unpacking Parquet") else { return }
            if !r.ok { return fail(j, reason(r)) }
        }

        switch j.kind {
        case .compress:
            let name = (j.src as NSString).lastPathComponent
            let out = j.outPath ?? unused(j.outDir ?? dir, name, "ppz")
            let size = fileSize(input) ?? 0
            // block-at-a-time compression reads delimited text only (not JSON, not Stata/SPSS/SAS)
            let delimited = ["csv", "tsv", "psv", "txt", "tab", "dat"].contains((input as NSString).pathExtension.lowercased())
            let stream = size > STREAM_ABOVE && delimited
            update(j) { $0.stream = stream }
            var extra = ["--progress"]
            if stream, let rpb = ProcessInfo.processInfo.environment["POLYPRESS_TEST_ROWS"] {
                extra += ["--rows", rpb]            // self-test only: force several blocks
            }
            guard let r = tool(j, BINARY, (stream ? ["stream-compress", input, "-o", out]
                                                  : ["compress", input, "-o", out]) + extra)
            else { return }
            if !r.ok { return fail(j, named(reason(r, [j.src, input]), j, input)) }
            let d = info(out)
            update(j) {
                $0.state = .done; $0.out = out; $0.stream = stream; $0.fraction = nil
                $0.to = fileSize(out); $0.secs = Date().timeIntervalSince(t0); $0.details = d
            }

        case .restore, .convert:
            var (stem, ext) = j.kind == .restore ? restoredName(j.src)
                : (((j.src as NSString).lastPathComponent as NSString).deletingPathExtension, "csv")
            if let f = j.format { ext = f }
            if j.kind == .convert && isParquet(j.src) && ext == "parquet" { stem += " copy" }
            let out = j.outPath ?? unused(j.outDir ?? dir, stem, ext)
            let pq = ext == "parquet"
            if pq && !parquet { return fail(j, "writing Parquet needs Python with pyarrow (pip3 install pyarrow)") }
            let target = pq ? sc.path("out.csv") : out
            var details = j.kind == .restore ? info(j.src) : nil
            guard let r = tool(j, BINARY, j.kind == .restore ? ["restore", j.src, "-o", target]
                                                             : ["convert", input, target],
                               stage: j.kind == .restore ? "restoring" : "converting") else { return }
            if !r.ok { return fail(j, named(reason(r, [j.src, input]), j, input)) }
            let dropped = translationNotes(r.out)
            if !dropped.isEmpty { details = (details ?? [:]).merging(["translation": dropped]) { $1 } }
            if pq {
                // written beside the scratch CSV first, so a failure never touches `out`,
                // which may be a file the user chose to replace
                // on the destination's own volume, so replacing works on external and network drives
                let near = try? FileManager.default.url(for: .itemReplacementDirectory, in: .userDomainMask,
                                                        appropriateFor: URL(fileURLWithPath: out), create: true)
                defer { if let n = near { try? FileManager.default.removeItem(at: n) } }
                let tmp = near?.appendingPathComponent("out.parquet").path ?? sc.path("out.parquet")
                guard let b = tool(j, PYTHON, [BRIDGE, "from-csv", target, tmp], stage: "writing Parquet")
                else { return }
                if !b.ok { return fail(j, reason(b)) }
                do {
                    if FileManager.default.fileExists(atPath: out) {
                        _ = try FileManager.default.replaceItemAt(URL(fileURLWithPath: out), withItemAt: URL(fileURLWithPath: tmp))
                    } else {
                        try FileManager.default.moveItem(atPath: tmp, toPath: out)
                    }
                } catch { return fail(j, "could not save \((out as NSString).lastPathComponent): \(error.localizedDescription)") }
            }
            update(j) {
                $0.state = .done; $0.out = out
                $0.to = fileSize(out); $0.secs = Date().timeIntervalSince(t0); $0.details = details
            }
        }
    }

    var stateJSON: String {
        let files = staged.mapValues { $0.map { p -> [String: Any] in
            var f: [String: Any] = ["path": p, "name": (p as NSString).lastPathComponent]
            if let n = fileSize(p) { f["size"] = n }
            return f
        } }
        let d: [String: Any] = ["formats": formats, "jobs": jobs.map { $0.dict }, "version": VERSION,
                                "staged": files, "history": history?.json ?? [], "parquet": parquet,
                                "notice": notice, "prefs": ["saveMode": Prefs.saveMode, "folder": (Prefs.folder as Any?) ?? NSNull()] as [String: Any]]
        let data = (try? JSONSerialization.data(withJSONObject: d)) ?? Data("{\"jobs\":[]}".utf8)
        return String(decoding: data, as: UTF8.self)
    }
}

// ─── The web view, which also takes the drops ────────────────────────────────
// WKWebView is itself a drop target and would otherwise open a dropped file
// as a page. Taking the drag here, before WebKit sees it, turns a drop into
// paths instead.
final class DropWeb: WKWebView {
    var onDrag: (Bool) -> Void = { _ in }
    var onDrop: ([String]) -> Void = { _ in }

    private func paths(_ info: NSDraggingInfo) -> [String] {
        let opts: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let urls = info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: opts) as? [URL]
        return (urls ?? []).map { $0.path }
    }

    override func draggingEntered(_ info: NSDraggingInfo) -> NSDragOperation {
        if paths(info).isEmpty { return [] }
        onDrag(true)
        return .copy
    }
    override func draggingUpdated(_ info: NSDraggingInfo) -> NSDragOperation {
        paths(info).isEmpty ? [] : .copy
    }
    override func draggingExited(_ info: NSDraggingInfo?) { onDrag(false) }
    override func prepareForDragOperation(_ info: NSDraggingInfo) -> Bool { !paths(info).isEmpty }
    override func performDragOperation(_ info: NSDraggingInfo) -> Bool {
        let p = paths(info)
        onDrag(false)
        if p.isEmpty { return false }
        onDrop(p)
        return true
    }
    override func concludeDragOperation(_ info: NSDraggingInfo?) {}
}

// ─── Window ──────────────────────────────────────────────────────────────────
final class Window: NSWindowController, WKScriptMessageHandler {
    let web: DropWeb
    let work: Work
    var ready = false
    var pendingTab: String?      // a section to show once the page has loaded

    init(work: Work) {
        self.work = work
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 620),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Polypress"
        win.setFrameAutosaveName("PolypressWindow2")
        win.minSize = NSSize(width: 720, height: 460)
        win.isReleasedWhenClosed = false
        let cfg = WKWebViewConfiguration()
        web = DropWeb(frame: .zero, configuration: cfg)
        super.init(window: win)
        cfg.userContentController.add(self, name: "polypress")
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        web.onDrag = { [weak self] on in self?.js("dragging(\(on))") }
        web.onDrop = { [weak self] p in self?.stage(p) }
        win.contentView = web
        win.center()
        web.loadFileURL(PAGE, allowingReadAccessTo: PAGE.deletingLastPathComponent())
        work.changed = { [weak self] in self?.render() }
    }
    required init?(coder: NSCoder) { fatalError() }

    func js(_ s: String) { if ready { web.evaluateJavaScript(s, completionHandler: nil) } }
    func render() { js("render(\(work.stateJSON))") }

    /// Files from a drop, the Dock, the Finder or a panel: list them and show their section.
    func stage(_ paths: [String]) {
        let tab = work.stage(paths)
        if ready { js("showTab('\(tab)')") } else { pendingTab = tab }
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let b = m.body as? [String: Any], let type = b["type"] as? String else { return }
        let tab = (b["tab"] as? String) == "restore" ? "restore" : "compress"
        switch type {
        case "ready":
            ready = true
            render()
            if let t = pendingTab { pendingTab = nil; js("showTab('\(t)')") }
        case "pick": pick(tab)
        case "stage": work.stage(b["paths"] as? [String] ?? [])
        case "unstage": if let p = b["path"] as? String { work.unstage(tab, p) }
        case "clearStaged": work.clearStaged(tab)
        case "start": start(tab, format: b["format"] as? String)
        case "setSaveMode":
            if let m = b["mode"] as? String, ["ask", "same", "folder"].contains(m) {
                Prefs.saveMode = m
                if m == "folder" && !(Prefs.folder.map(isDirectory) ?? false) { chooseFolder() }
                render()
            }
        case "chooseFolder": chooseFolder()
        case "dismissNotice": work.notice = ""; render()
        case "reveal":
            if let p = b["path"] as? String, FileManager.default.fileExists(atPath: p) {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)])
            }
        case "stop": if let id = b["id"] as? Int { work.stop(id) }
        case "clearFinished": work.clearFinished(tab)
        case "forget": if let id = b["id"] as? String { work.history?.remove(id); render() }
        case "clearHistory": clearHistory()
        case "openURL":
            if let s = b["url"] as? String, let u = URL(string: s),
               ["www.polypressapp.com", "github.com"].contains(u.host ?? "") {
                NSWorkspace.shared.open(u)
            }
        default: break
        }
    }

    /// Choose files for a section (or, from the File menu, for either).
    func pick(_ tab: String?) {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = true
        p.canChooseDirectories = false
        if tab == "restore" {
            p.message = "Choose .ppz archives to restore"
            if let t = UTType(filenameExtension: "ppz") { p.allowedContentTypes = [t] }
        } else if tab == "compress" {
            p.message = "Choose tables to compress"
        }
        p.prompt = "Choose"
        p.beginSheetModal(for: window!) { r in
            if r == .OK { self.stage(p.urls.map { $0.path }) }
        }
    }

    func chooseFolder(then: ((String) -> Void)? = nil, message: String = "Choose the folder to save files in") {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        p.allowsMultipleSelection = false
        p.message = message
        p.prompt = "Choose"
        if let f = Prefs.folder { p.directoryURL = URL(fileURLWithPath: f) }
        p.beginSheetModal(for: window!) { r in
            guard r == .OK, let u = p.url else {
                // nothing chosen: a "chosen folder" setting without a folder falls back to asking
                if Prefs.saveMode == "folder" && !(Prefs.folder.map(isDirectory) ?? false) { Prefs.saveMode = "ask" }
                self.render()
                return
            }
            // choosing a folder from Settings or the menu also means "save there"
            if then == nil { Prefs.folder = u.path; Prefs.saveMode = "folder" }
            self.render()
            then?(u.path)
        }
    }

    /// Where a single file's result goes, as the save panel suggests it.
    /// A name that is free in the source's folder, so accepting the suggestion
    /// never replaces a file (above all the original table, or the archive itself).
    func suggestion(_ src: String, _ tab: String, _ format: String?) -> (String, String) {
        let dir = (src as NSString).deletingLastPathComponent
        if tab == "compress" {
            return ((unused(dir, (src as NSString).lastPathComponent, "ppz") as NSString).lastPathComponent, "ppz")
        }
        var (stem, ext) = restoredName(src)
        if let f = format { ext = f }
        return ((unused(dir, stem, ext) as NSString).lastPathComponent, ext)
    }

    func start(_ tab: String, format: String?) {
        let items = work.staged[tab] ?? []
        guard !items.isEmpty, let win = window else { return }
        let fmt = (format == nil || format == "original") ? nil : format
        let needsParquet = fmt == "parquet"
            || (tab == "restore" && fmt == nil && items.contains { restoredName($0).1 == "parquet" })
            || (tab == "compress" && items.contains(where: isParquet))
        if needsParquet && !work.parquet {
            work.notice = "Parquet files require Python with the pyarrow library (pip3 install pyarrow)."
            return render()
        }
        let go: (String?, String?) -> Void = { dir, path in
            self.work.startStaged(tab, format: fmt, outDir: dir, outPath: path)
        }
        switch Prefs.saveMode {
        case "same":
            go(nil, nil)
        case "folder":
            if let f = Prefs.folder, isDirectory(f) { go(f, nil) }
            else { chooseFolder(then: { Prefs.folder = $0; go($0, nil) }) }
        default:
            if items.count == 1 {
                let (name, ext) = suggestion(items[0], tab, fmt)
                let p = NSSavePanel()
                p.nameFieldStringValue = name
                p.directoryURL = URL(fileURLWithPath: (items[0] as NSString).deletingLastPathComponent)
                p.canCreateDirectories = true
                p.isExtensionHidden = false
                if let t = UTType(filenameExtension: ext) { p.allowedContentTypes = [t] }
                p.message = tab == "compress" ? "Save the archive as" : "Save the restored table as"
                p.prompt = "Save"
                p.beginSheetModal(for: win) { r in
                    if r == .OK, let u = p.url { go(nil, u.path) }
                }
            } else {
                let noun = tab == "compress" ? "archives" : "tables"
                chooseFolder(then: { go($0, nil) }, message: "Choose where to save the \(items.count) \(noun)")
            }
        }
    }

    func clearHistory() {
        guard let win = window, let h = work.history, !h.items.isEmpty else { return }
        let a = NSAlert()
        a.messageText = "Clear the history?"
        a.informativeText = "This removes all \(h.items.count) entries from the list. No files are deleted."
        a.addButton(withTitle: "Clear History")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: win) { r in
            if r == .alertFirstButtonReturn { h.clear(); self.render() }
        }
    }
}

// ─── App ─────────────────────────────────────────────────────────────────────
final class AppDelegate: NSObject, NSApplicationDelegate {
    let work = Work(history: History())
    var controller: Window?
    var pending: [String] = []

    func applicationDidFinishLaunching(_ n: Notification) {
        removeOldScratch()
        buildMenu()
        let c = Window(work: work)
        controller = c
        c.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        work.checkParquet()
        if !pending.isEmpty { c.stage(pending); pending = [] }
    }

    /// Double-clicking a .ppz, or dropping files on the Dock icon.
    func application(_ sender: NSApplication, openFiles names: [String]) {
        if let c = controller { c.stage(names) } else { pending += names }
        controller?.showWindow(nil)
        sender.reply(toOpenOrPrint: .success)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    /// Quitting (or closing the window) while files are being processed asks first.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let busy = work.jobs.filter { $0.state == .working || $0.state == .queued }.count
        guard busy > 0 else { return .terminateNow }
        let a = NSAlert()
        a.messageText = busy == 1 ? "Stop the file in progress and quit?" : "Stop \(busy) files in progress and quit?"
        a.informativeText = "Files that have not finished are not saved. Files already saved are not affected."
        a.addButton(withTitle: "Stop and Quit")
        a.addButton(withTitle: "Cancel")
        if a.runModal() == .alertFirstButtonReturn { return .terminateNow }
        controller?.showWindow(nil)
        return .terminateCancel
    }

    /// Quitting stops whatever is running; the program deletes its own
    /// half-written output when it is stopped, so nothing is left behind.
    func applicationWillTerminate(_ n: Notification) {
        for j in work.jobs where j.state == .working || j.state == .queued {
            j.state = .stopped
            j.message = "stopped when Polypress quit"
            work.record(j)
        }
        work.stopAll()
    }

    /// Scratch folders (Scratch: "polypress-<UUID>") left by a run that ended
    /// abruptly. Only a day old or more, so a self-test or a second copy running
    /// now keeps its own; the program's "polypress-restore…" files are not ours.
    func removeOldScratch() {
        let fm = FileManager.default, tmp = NSTemporaryDirectory()
        let dayAgo = Date(timeIntervalSinceNow: -86_400)
        for name in (try? fm.contentsOfDirectory(atPath: tmp)) ?? []
        where name.hasPrefix("polypress-") && UUID(uuidString: String(name.dropFirst(10))) != nil {
            let p = (tmp as NSString).appendingPathComponent(name)
            if let m = (try? fm.attributesOfItem(atPath: p))?[.modificationDate] as? Date, m < dayAgo {
                try? fm.removeItem(atPath: p)
            }
        }
    }

    @objc func choose() { controller?.pick(nil) }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Polypress",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Polypress", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Polypress", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem(), fileMenu = NSMenu(title: "File")
        let open = NSMenuItem(title: "Open…", action: #selector(choose), keyEquivalent: "o")
        open.target = self
        fileMenu.addItem(open)
        fileMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let winItem = NSMenuItem(), winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = winMenu
        main.addItem(winItem)
        NSApp.windowsMenu = winMenu
        NSApp.mainMenu = main
    }
}

// ─── Self-test: every action, on real files, no window ───────────────────────
// tiny.dta (3 rows: id, sex with value labels, a %td date) as a .ppz archive
let TINY_DTA_PPZ = "UFBaMgAAASoAAAGUAAAAAeACNwEiXQA9iInnY8eHJIMjJlVUkZxEItGWpy94vf/P+KgAawgaWTeOuWWXyXhkJBYKjezAQojhfrg9uGMntcb2hMKp7l/opr7oZxp18647c6SeD5QQHj/Vgcxgya70gpqVLtY0oDIDhjHpU3tohH7sbyQq2SyvFgOGRSHycat8a/GGfhIeJRlBMFEsonSOkDli0Cf/Jk6kaqMY4dqWhJbtwsYE9l9wxV8Y12EoMCS1NKh5xRiQSTQ1joQ7zxifCqkUflSIA18Xcicw0Kr7Zm1jRAE009KsYyn7OXidR0CbuCDZ9NwJxV+gL5MRGI/hF6WcuB6jpd5GxYyMzX05BybT3S04pqjLriZ0D/38CyUkc7nn+w8X/TTAML1151npR9hRzJvn2RrIQADgCrMBjF0AHhzKhihAdnSft5jZXH3tsD7/ymVcXjA+e8/ZVHW8Y7IM+XZJEeJmxzNUKW93NsOx/I0bmckpVZmp7B1EIsfgguPL+sBWIUM+IypT7nKLv0uIGVU3pqBe0u2r8ekhAc24w0+KUBB78uqlTk6ROI3j+FfoejkWV6nmE2vlpipHn9NK0bBCgYk482VhuzYMYLRnmsq66Bk/QjnZsMO9GPpPzCmSGo21THW2jTveBik0qJbVNnmh+5xq04cELUhy7we4EqFLB0DhwDm/0udBzXRE5FDQJp9gbo5c7h/JsS++s8H3FvanqfaFsye/xzmkn62LinS4RsWzIDh530ngunclsUsgod6e2Ae63QD4V2ynYd8KWYnoPFsKcKMVFZXF5dCWXrn0dIu/k4JfDv/04KaPGdP4N0BOb+cbDLR/oC4j3q9apZEDlwIRwdhrH+h1y7krXHjFUCcDVb81JERw4D6D5s1T8gmcsVq55TMn0KkN30/us/cSUokqZ+qewVXLLDh6uVZlDZN7FcDd5zAAAAA="

func selftest() -> Int32 {
    var bad: Int32 = 0
    func check(_ name: String, _ ok: Bool, _ detail: String = "") {
        print(String(format: "  %-22@ %@", name as NSString, ok ? "OK" : "FAIL " + detail))
        if !ok { bad += 1 }
    }
    let sc = Scratch()
    let src = sc.path("t.csv")
    var csv = "id,colour,value,note\n"
    for i in 0..<400 { csv += "\(i),\(["red", "green", "blue"][i % 3]),\(String(format: "%.2f", Double(i) * 1.5)),\"note, \(i % 7)\"\n" }
    try? csv.write(toFile: src, atomically: true, encoding: .utf8)

    let w = Work()
    let g = DispatchSemaphore(value: 0)
    w.checkParquet { g.signal() }
    // checkParquet reports on the main queue; spin it until the answer lands
    while g.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05)) }
    check("binary", FileManager.default.isExecutableFile(atPath: BINARY) && !VERSION.isEmpty, BINARY)
    if VERSION.isEmpty {
        print("\nno program to test: set POLYPRESS to csrc/polypress")
        return 1
    }
    if let me = Bundle.main.executablePath {
        let r = run(me.lowercased(), ["--version"])
        check("refuses/run-itself", !r.ok && r.err.contains("Polypress itself") && !sameFile(BINARY, me), r.err)
    }

    func canon(_ p: String) -> String {
        var path = p
        if isParquet(p) { path = sc.path("pq-check.csv"); _ = run(PYTHON, [BRIDGE, "to-csv", p, path]) }
        let out = sc.path("canon.csv")
        try? FileManager.default.removeItem(atPath: out)
        _ = run(BINARY, ["convert", path, out])
        return (try? String(contentsOfFile: out, encoding: .utf8)) ?? "<unreadable>"
    }
    func go(_ j: Job) -> Job { w.jobs.append(j); w.perform(j); return j }
    let want = canon(src)

    let c = go(Job(src: src, kind: .compress))
    check("compress", c.state == .done && c.out.hasSuffix("t.csv.ppz"), c.message)
    check("compress/details", (c.details?["rows"] as? Int) == 400, "\(String(describing: c.details))")
    check("version", VERSION.hasPrefix("Polypress ") && VERSION.contains(" · build "), VERSION)
    check("version/made-by", (c.details?["made_by"] as? String) == VERSION.split(separator: " ")[1].description,
          "\(String(describing: c.details?["made_by"]))")
    let c2 = go(Job(src: src, kind: .compress))
    check("compress/no-overwrite", c2.out.hasSuffix("t.csv 2.ppz"), c2.out)
    check("archive-detected", isArchive(c.out) && !isArchive(src))

    let r = go(Job(src: c.out, kind: .restore))
    check("restore/name", r.out.hasSuffix("t 2.csv"), r.out)     // t.csv is still there
    check("restore/cells", r.state == .done && canon(r.out) == want, r.message)

    for f in w.formats {
        let j = go(Job(src: c.out, kind: .restore, format: f))
        check("restore/" + f, j.state == .done && canon(j.out) == want, j.message)
        let k = go(Job(src: src, kind: .convert, format: f))
        check("convert/" + f, k.state == .done && canon(k.out) == want, k.message)
    }
    if w.parquet {
        let pq = go(Job(src: src, kind: .convert, format: "parquet"))
        let pc = go(Job(src: pq.out, kind: .compress))
        let pr = go(Job(src: pc.out, kind: .restore, format: "csv"))
        check("parquet/in-and-out", pr.state == .done && canon(pr.out) == want, pr.message + pc.message)
    }

    // a Stata file: restored from its archive byte for byte, compressed whole
    // even above the streaming size, translated with its losses named
    let tinyArc = sc.path("tiny.dta.ppz")
    try? Data(base64Encoded: TINY_DTA_PPZ)?.write(to: URL(fileURLWithPath: tinyArc))
    let sr = go(Job(src: tinyArc, kind: .restore))
    check("stata/restore", sr.state == .done && sr.out.hasSuffix("tiny.dta"), sr.out + sr.message)
    check("version/pre-1.00", (sr.details?["made_by"] as? String) == "before 1.00",
          "\(String(describing: sr.details?["made_by"]))")
    let saved = STREAM_ABOVE
    STREAM_ABOVE = 1
    let sc1 = go(Job(src: sr.out, kind: .compress))
    STREAM_ABOVE = saved
    check("stata/compress", sc1.state == .done && !sc1.stream
          && (sc1.details?["container"] as? String) == "original", sc1.message)
    let back = go(Job(src: sc1.out, kind: .restore))
    check("stata/byte-for-byte", FileManager.default.contentsEqual(atPath: back.out, andPath: sr.out), back.out)
    let st = go(Job(src: sc1.out, kind: .restore, format: "csv"))
    let notes = st.details?["translation"] as? [String] ?? []
    check("stata/as-csv", st.state == .done && canon(st.out).contains("2,2,2018-05-06")
          && notes.contains { $0.hasPrefix("value labels") }, st.message + "\(notes)")

    // derived columns (PPZ2): location rebuilt from lat and lon
    let geo = sc.path("geo.csv")
    var gcsv = "id,lat,lon,location\n"
    for i in 0..<600 {
        let la = String(format: "%.6f", 41.6 + Double(i * 7919 % 4001) / 10000.0)
        let lo = String(format: "%.6f", -87.9 + Double(i * 104729 % 3001) / 10000.0)
        gcsv += "\(i),\(la),\(lo),POINT (\(lo) \(la))\n"
    }
    try? gcsv.write(toFile: geo, atomically: true, encoding: .utf8)
    let gc = go(Job(src: geo, kind: .compress))
    let gmagic = FileHandle(forReadingAtPath: gc.out).map { h -> String in
        defer { h.closeFile() }
        return String(decoding: h.readData(ofLength: 4), as: UTF8.self)
    } ?? ""
    let ghidden = sc.path("geo-archive.bin")            // no .ppz: found by magic alone
    try? FileManager.default.copyItem(atPath: gc.out, toPath: ghidden)
    check("derived/archive", gc.state == .done && gmagic == "PPZ2" && isArchive(ghidden), gmagic)
    let gr = go(Job(src: gc.out, kind: .restore))
    check("derived/cells", gr.state == .done && canon(gr.out) == canon(geo), gr.message)

    let dup = sc.path("dup.csv")
    try? "a,a\n1,2\n".write(toFile: dup, atomically: true, encoding: .utf8)
    let dc = go(Job(src: dup, kind: .compress))
    let dj = go(Job(src: dc.out, kind: .restore, format: "json"))
    check("refuses/duplicate-json", dj.state == .failed && dj.message.contains("duplicate"), dj.message)
    let xl = sc.path("x.xlsx")
    try? "PK\u{3}\u{4}junk".write(toFile: xl, atomically: true, encoding: .utf8)
    let xj = go(Job(src: xl, kind: .compress))
    check("refuses/xlsx", xj.state == .failed && xj.message.contains("Excel"), xj.message)
    let bogus = sc.path("bogus.ppz")
    try? "PPZ2 not really".write(toFile: bogus, atomically: true, encoding: .utf8)
    let bj = go(Job(src: bogus, kind: .restore))
    check("refuses/damaged", bj.state == .failed && bj.message.contains("damaged"), bj.message)
    w.add([sc.dir])
    check("refuses/folder", w.jobs.last?.state == .failed)

    // big-file mode: real progress, and Stop leaves nothing behind
    let big = sc.path("big.csv")
    var rows = "id,region,kind,value,note\n"
    for i in 0..<120_000 {
        rows += "\(i),r\(i % 17),k\(i % 5),\(Double(i % 997) / 7.0),\"n \(i % 23), x\"\n"
    }
    try? rows.write(toFile: big, atomically: true, encoding: .utf8)
    STREAM_ABOVE = 1 << 20
    setenv("POLYPRESS_TEST_ROWS", "5000", 1)
    var fractions: [Double] = []
    let seen = w.changed
    w.changed = { if let f = w.jobs.last?.fraction { fractions.append(f) } }
    let bj2 = go(Job(src: big, kind: .compress))
    check("big/done-in-blocks", bj2.state == .done && bj2.stream, bj2.message)
    check("big/progress-reported", fractions.count >= 2 && (fractions.last ?? 0) > 0.9,
          "\(fractions.count) reports")
    let bb = go(Job(src: bj2.out, kind: .restore))
    check("big/cells", bb.state == .done && canon(bb.out) == canon(big), bb.message)
    w.changed = seen

    let sj = Job(src: big, kind: .compress)
    w.jobs.append(sj)
    // press Stop the moment the program is running -- the one-pass encoder
    // finishes this file in well under a second, so a fixed delay can lose
    DispatchQueue.global().async {
        while true {
            w.lock.lock(); let running = sj.proc != nil; w.lock.unlock()
            if running || sj.state == .done { break }
            usleep(2000)
        }
        w.stop(sj.id)
    }
    w.perform(sj)
    let leftovers = ((try? FileManager.default.contentsOfDirectory(atPath: sc.dir)) ?? [])
        .filter { $0.contains(".part") || $0 == "big.csv 3.ppz" }
    check("stop/stops", sj.state == .stopped, sj.state.rawValue + " " + sj.message)
    check("stop/nothing-left", leftovers.isEmpty, leftovers.joined(separator: ", "))
    STREAM_ABOVE = 64 << 20
    check("state-json", w.stateJSON.contains("\"jobs\""))

    // ── the window's flows: staging, destinations, history ──
    let histURL = URL(fileURLWithPath: sc.path("history.json"))
    let hw = Work(history: History(url: histURL))
    hw.parquet = w.parquet
    func waitAll(_ x: Work) {
        let end = Date().addingTimeInterval(120)
        while x.jobs.contains(where: { $0.state == .queued || $0.state == .working }) && Date() < end {
            RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        }
    }
    let arcCopy = sc.path("staged.csv.ppz")
    try? FileManager.default.copyItem(atPath: c.out, toPath: arcCopy)
    let tab1 = hw.stage([src, arcCopy, sc.dir, sc.path("missing.csv"), src])
    check("stage/sorted", hw.staged["compress"] == [src] && hw.staged["restore"] == [arcCopy],
          "\(hw.staged)")
    check("stage/folder-notice", hw.notice.contains("folder"), hw.notice)
    check("stage/tab", tab1 == "compress" && hw.stage([arcCopy]) == "restore")
    check("stage/no-duplicates", hw.staged["restore"]?.count == 1)

    let outDir = sc.path("out")
    try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
    hw.startStaged("compress", format: nil, outDir: outDir, outPath: nil)
    waitAll(hw)
    let hc = hw.jobs.last!
    check("dest/folder", hc.state == .done && hc.out == (outDir as NSString).appendingPathComponent("t.csv.ppz"), hc.out + hc.message)
    check("dest/staged-cleared", hw.staged["compress"]?.isEmpty == true)

    hw.staged["restore"] = [hc.out]
    let exact = sc.path("chosen name.jsonl")
    hw.startStaged("restore", format: "jsonl", outDir: nil, outPath: exact)
    waitAll(hw)
    let hr = hw.jobs.last!
    check("dest/exact-path", hr.state == .done && hr.out == exact && canon(exact) == want, hr.out + hr.message)

    // replacing a file the user chose in the save panel
    let target = sc.path("replace me.csv")
    try? "old,contents\n1,2\n".write(toFile: target, atomically: true, encoding: .utf8)
    hw.staged["restore"] = [hc.out]
    hw.startStaged("restore", format: "csv", outDir: nil, outPath: target)
    waitAll(hw)
    check("dest/replace", hw.jobs.last?.state == .done && canon(target) == want, hw.jobs.last?.message ?? "")

    // a failed restore must leave the file it was going to replace untouched
    try? "keep,me\n1,2\n".write(toFile: target, atomically: true, encoding: .utf8)
    hw.staged["restore"] = [bogus]
    hw.startStaged("restore", format: "csv", outDir: nil, outPath: target)
    waitAll(hw)
    let kept = (try? String(contentsOfFile: target, encoding: .utf8)) ?? ""
    check("dest/failure-keeps-file", hw.jobs.last?.state == .failed && kept == "keep,me\n1,2\n", kept)
    if hw.parquet {
        let pqTarget = sc.path("replace me.parquet")
        try? "not parquet".write(toFile: pqTarget, atomically: true, encoding: .utf8)
        hw.staged["restore"] = [hc.out]
        hw.startStaged("restore", format: "parquet", outDir: nil, outPath: pqTarget)
        waitAll(hw)
        check("dest/replace-parquet", hw.jobs.last?.state == .done && canon(pqTarget) == want, hw.jobs.last?.message ?? "")
    }

    // history: every finished job, newest first, kept on disk
    let h2 = History(url: histURL)
    check("history/persisted", h2.items.count == hw.jobs.count && h2.items.first?.state == hw.jobs.last?.state.rawValue
          && h2.items.first?.out == hw.jobs.last?.out,
          "\(h2.items.count) items, \(hw.jobs.count) jobs")
    check("history/fields", h2.items.last?.kind == "compress" && h2.items.last?.out == hc.out
          && h2.items.last?.from != nil && h2.items.last?.to != nil)
    let hj = h2.json
    check("history/exists", (hj.last?["outExists"] as? Bool) == true)
    try? FileManager.default.removeItem(atPath: hc.out)
    check("history/gone", (History(url: histURL).json.last?["outExists"] as? Bool) == false)
    if let first = h2.items.first?.id { h2.remove(first) }
    check("history/remove", History(url: histURL).items.count == hw.jobs.count - 1)
    for _ in 0..<520 { h2.add(hc) }
    check("history/capped", History(url: histURL).items.count == 500)
    h2.clear()
    check("history/clear", History(url: histURL).items.isEmpty)

    // a queued job that is stopped is recorded too
    let qw = Work(history: History(url: URL(fileURLWithPath: sc.path("h2.json"))))
    let qj = Job(src: src, kind: .compress)
    qw.jobs.append(qj)
    qw.stop(qj.id)
    check("history/stopped", qw.history?.items.first?.state == "stopped")

    hw.clearFinished("restore")
    check("clear-finished", hw.jobs.allSatisfy { $0.kind == .compress } && !hw.jobs.isEmpty)
    // fixes from the review: large JSON is compressed whole, a result may not
    // replace its own source, unreadable history is kept, missing files are named
    let bigJSON = sc.path("big.jsonl")
    var jl = ""
    for i in 0..<3000 { jl += "{\"id\": \(i), \"k\": \"v\(i % 7)\"}\n" }
    try? jl.write(toFile: bigJSON, atomically: true, encoding: .utf8)
    STREAM_ABOVE = 1024
    let jc = go(Job(src: bigJSON, kind: .compress))
    STREAM_ABOVE = 64 << 20
    check("large-json/compressed-whole", jc.state == .done && !jc.stream, jc.message)
    let selfJob = Job(src: c.out, kind: .restore, format: "csv")
    selfJob.outPath = c.out
    let sj2 = go(selfJob)
    check("refuses/replace-own-source", sj2.state == .failed && isArchive(c.out), sj2.message)
    let badURL = URL(fileURLWithPath: sc.path("bad-history.json"))
    try? "{not json".write(to: badURL, atomically: true, encoding: .utf8)
    let bh = History(url: badURL)
    bh.add(hc)
    let kept2 = ((try? FileManager.default.contentsOfDirectory(atPath: sc.dir)) ?? []).filter { $0.hasPrefix("history-unreadable-") }
    check("history/unreadable-kept", bh.items.count == 1 && kept2.count == 1, "\(kept2)")
    hw.stage([sc.path("gone.csv")])
    check("stage/missing-notice", hw.notice.contains("could not be found"), hw.notice)

    let parsed = try? JSONSerialization.jsonObject(with: Data(hw.stateJSON.utf8)) as? [String: Any]
    check("state-json/keys", parsed?["staged"] != nil && parsed?["history"] != nil && parsed?["prefs"] != nil
          && parsed?["parquet"] != nil, String(hw.stateJSON.prefix(120)))
    print(bad == 0 ? "\nall passed" : "\n\(bad) failed")
    return bad
}

if CommandLine.arguments.contains("--selftest") {
    exit(selftest())
}
// answers without opening a window (and is what the self-test's run-itself check asks)
if CommandLine.arguments.contains("--version") {
    print(VERSION.isEmpty ? "Polypress (program not found)" : VERSION)
    exit(VERSION.isEmpty ? 1 : 0)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
