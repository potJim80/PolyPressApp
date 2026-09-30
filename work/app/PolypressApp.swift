// Polypress.app -- one quiet window around the C program.
//
// Drop a table and it is compressed next to itself; drop a .ppz and it is
// restored next to itself. Each file is one line: what went in, what came
// out, how much smaller, and -- on hover -- "show" and "as csv tsv json ...",
// which writes the same table in another format. That is the whole app.
//
// All the work is csrc/polypress, shipped inside the bundle and run as a
// process; this file only decides names, runs it, and hands the page a list
// to draw (page/index.html). Parquet goes through py/parquet.py when the
// system Python has pyarrow, and is simply not offered when it has not.
//
// Built by app/build_app.sh. `Polypress --selftest` runs every action on
// generated files without opening a window.

import AppKit
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
let PAGE = RES.appendingPathComponent("page/index.html")

// Above this a table is compressed a block at a time: memory stays bounded,
// the blocks run side by side, and the program can say how far it has got
// -- which is what the progress bar shows. Below it a file takes seconds.
var STREAM_ABOVE: Int64 = 64 << 20          // var: the self-test lowers it

let ARCHIVE_MAGICS: Set<String> = ["PPZ1", "FAST", "PPZX", "PPZB", "PPZS"]
let TABLE_FORMATS = ["csv", "tsv", "json", "jsonl"]

// ─── Running things ──────────────────────────────────────────────────────────
struct Ran { let ok: Bool; let out: String; let err: String; let secs: Double; let stopped: Bool }

/// Run a program to the end. `line` sees each line it writes to stderr as it
/// arrives -- that is how progress gets here -- and `started` is handed the
/// process, so the Stop link can end it.
func run(_ exe: String, _ args: [String], line: ((String) -> Void)? = nil,
         started: ((Process) -> Void)? = nil) -> Ran {
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
    if low.hasSuffix(".ppz") || low.hasSuffix(".tcz") { return true }
    guard let h = FileHandle(forReadingAtPath: path) else { return false }
    defer { h.closeFile() }
    let d = h.readData(ofLength: 4)
    return ARCHIVE_MAGICS.contains(String(decoding: d, as: UTF8.self))
}

func isParquet(_ path: String) -> Bool {
    let low = path.lowercased()
    return low.hasSuffix(".parquet") || low.hasSuffix(".pq")
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

/// "survey.csv.ppz" -> ("survey", "csv"); "x.ppz" -> ("x", "csv").
func restoredName(_ archive: String) -> (String, String) {
    var base = (archive as NSString).lastPathComponent
    let low = base.lowercased()
    if low.hasSuffix(".ppz") || low.hasSuffix(".tcz") { base = String(base.dropLast(4)) }
    let ext = (base as NSString).pathExtension.lowercased()
    let known = TABLE_FORMATS + ["psv", "txt", "parquet"]
    if known.contains(ext) { return ((base as NSString).deletingPathExtension, ext == "txt" ? "csv" : ext) }
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

    init(src: String, kind: Kind, format: String? = nil) {
        id = Job.next; Job.next += 1
        self.src = src; self.kind = kind; self.format = format
        from = fileSize(src)
    }

    var dict: [String: Any] {
        var d: [String: Any] = ["id": id, "name": (src as NSString).lastPathComponent, "path": src,
                                "kind": kind.rawValue, "state": state.rawValue, "note": note,
                                "message": message, "stream": stream,
                                "outName": (out as NSString).lastPathComponent]
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

    func saveAs(_ id: Int, _ format: String) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        // a compressed table converts from its source; an archive restores
        let kind: Kind = j.kind == .restore ? .restore : .convert
        enqueue(Job(src: j.src, kind: kind, format: format))
        changed()
    }

    func clear() {
        jobs.removeAll { $0.state == .done || $0.state == .failed || $0.state == .stopped }
        changed()
    }

    /// Stop one file: a queued one never starts; a running one has its
    /// program ended, and the program removes its own half-written output.
    func stop(_ id: Int) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        lock.lock(); j.stopAsked = true; let p = j.proc; lock.unlock()
        if j.state == .queued { j.state = .stopped; j.message = "stopped" }
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
        if asked || r.stopped {
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
        if Thread.isMainThread { f(j); changed() }
        else { DispatchQueue.main.sync { f(j); changed() } }
    }

    private func fail(_ j: Job, _ msg: String) {
        update(j) { $0.state = .failed; $0.message = msg; $0.fraction = nil }
    }

    private func info(_ path: String) -> [String: Any]? {
        let r = run(BINARY, ["info", path, "--json"])
        guard r.ok, let d = r.out.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    }

    /// Run the job synchronously. Called on the job queue (and by selftest).
    func perform(_ j: Job) {
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
            let out = unused(dir, name, "ppz")
            let size = fileSize(input) ?? 0
            let stream = size > STREAM_ABOVE
            update(j) { $0.stream = stream }
            var extra = ["--progress"]
            if stream, let rpb = ProcessInfo.processInfo.environment["POLYPRESS_TEST_ROWS"] {
                extra += ["--rows", rpb]            // self-test only: force several blocks
            }
            guard let r = tool(j, BINARY, (stream ? ["stream-compress", input, "-o", out]
                                                  : ["compress", input, "-o", out]) + extra)
            else { return }
            if !r.ok { return fail(j, reason(r, [j.src, input])) }
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
            let out = unused(dir, stem, ext)
            let pq = ext == "parquet"
            if pq && !parquet { return fail(j, "writing Parquet needs Python with pyarrow (pip3 install pyarrow)") }
            let target = pq ? sc.path("out.csv") : out
            let details = j.kind == .restore ? info(j.src) : nil
            guard let r = tool(j, BINARY, j.kind == .restore ? ["restore", j.src, "-o", target]
                                                             : ["convert", input, target],
                               stage: j.kind == .restore ? "restoring" : "converting") else { return }
            if !r.ok { return fail(j, reason(r, [j.src, input])) }
            if pq {
                guard let b = tool(j, PYTHON, [BRIDGE, "from-csv", target, out], stage: "writing Parquet")
                else { try? FileManager.default.removeItem(atPath: out); return }
                if !b.ok { try? FileManager.default.removeItem(atPath: out); return fail(j, reason(b)) }
            }
            update(j) {
                $0.state = .done; $0.out = out
                $0.to = fileSize(out); $0.secs = Date().timeIntervalSince(t0); $0.details = details
            }
        }
    }

    var stateJSON: String {
        let d: [String: Any] = ["formats": formats, "jobs": jobs.map { $0.dict }]
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

    init(work: Work) {
        self.work = work
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 420),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Polypress"
        win.setFrameAutosaveName("PolypressWindow")
        win.minSize = NSSize(width: 440, height: 280)
        win.isReleasedWhenClosed = false
        let cfg = WKWebViewConfiguration()
        web = DropWeb(frame: .zero, configuration: cfg)
        super.init(window: win)
        cfg.userContentController.add(self, name: "polypress")
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        web.onDrag = { [weak self] on in self?.js("dragging(\(on))") }
        web.onDrop = { [weak self] p in self?.work.add(p) }
        win.contentView = web
        win.center()
        web.loadFileURL(PAGE, allowingReadAccessTo: PAGE.deletingLastPathComponent())
        work.changed = { [weak self] in self?.render() }
    }
    required init?(coder: NSCoder) { fatalError() }

    func js(_ s: String) { if ready { web.evaluateJavaScript(s, completionHandler: nil) } }
    func render() { js("render(\(work.stateJSON))") }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let b = m.body as? [String: Any], let type = b["type"] as? String else { return }
        let id = b["id"] as? Int ?? -1
        switch type {
        case "ready": ready = true; render()
        case "choose": choose()
        case "clear": work.clear()
        case "reveal":
            if let j = work.jobs.first(where: { $0.id == id }), !j.out.isEmpty {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: j.out)])
            }
        case "saveAs":
            if let f = b["format"] as? String { work.saveAs(id, f) }
        case "stop": work.stop(id)
        default: break
        }
    }

    @objc func choose() {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = true
        p.canChooseDirectories = false
        p.prompt = "Open"
        p.beginSheetModal(for: window!) { r in
            if r == .OK { self.work.add(p.urls.map { $0.path }) }
        }
    }
}

// ─── App ─────────────────────────────────────────────────────────────────────
final class AppDelegate: NSObject, NSApplicationDelegate {
    let work = Work()
    var controller: Window?
    var pending: [String] = []

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        let c = Window(work: work)
        controller = c
        c.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        work.checkParquet()
        if !pending.isEmpty { work.add(pending); pending = [] }
    }

    /// Double-clicking a .ppz, or dropping files on the Dock icon.
    func application(_ sender: NSApplication, openFiles names: [String]) {
        if controller == nil { pending += names } else { work.add(names) }
        controller?.showWindow(nil)
        sender.reply(toOpenOrPrint: .success)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    /// Quitting stops whatever is running; the program deletes its own
    /// half-written output when it is stopped, so nothing is left behind.
    func applicationWillTerminate(_ n: Notification) { work.stopAll() }

    @objc func choose() { controller?.choose() }

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
    check("binary", FileManager.default.isExecutableFile(atPath: BINARY), BINARY)

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
    try? "PPZ1 not really".write(toFile: bogus, atomically: true, encoding: .utf8)
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
    print(bad == 0 ? "\nall passed" : "\n\(bad) failed")
    return bad
}

if CommandLine.arguments.contains("--selftest") {
    exit(selftest())
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
