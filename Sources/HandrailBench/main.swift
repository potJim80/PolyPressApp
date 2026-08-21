import Foundation
import HandrailCore

/// What reading a file actually costs.
///
/// "Efficiency is key" is not a thing you can hold in your head while editing —
/// it needs a number you can re-measure. This prints the two numbers that matter
/// for a desktop app: how long the user waits, and how much memory is still held
/// afterwards.
func peakMemoryMB() -> Double {
    var info = rusage()
    getrusage(RUSAGE_SELF, &info)
    // maxrss is bytes on Darwin, kilobytes on Linux.
    return Double(info.ru_maxrss) / 1_048_576.0
}

func residentMB() -> Double {
    var info = mach_task_basic_info()
    var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size) / 4
    let result = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count)
        }
    }
    return result == KERN_SUCCESS ? Double(info.resident_size) / 1_048_576.0 : -1
}

let args = CommandLine.arguments
guard args.count > 1 else {
    print("usage: handrail-bench <file.csv> [more files…]")
    exit(2)
}

func pad(_ s: String, _ n: Int) -> String {
    s.count >= n ? String(s.prefix(n)) : s + String(repeating: " ", count: n - s.count)
}
func lead(_ s: String, _ n: Int) -> String {
    s.count >= n ? s : String(repeating: " ", count: n - s.count) + s
}

print(pad("file", 42) + lead("size MB", 10) + lead("seconds", 10)
      + lead("read MB", 10) + lead("peak MB", 10) + lead("cols", 7))

for path in args.dropFirst() {
    let url = URL(fileURLWithPath: path)
    let attrs = try? FileManager.default.attributesOfItem(atPath: path)
    let size = (attrs?[.size] as? Int) ?? 0
    let sizeMB = Double(size) / 1_048_576.0

    let before = residentMB()
    let start = Date()
    let file = try? DataFile(url: url)
    let seconds = Date().timeIntervalSince(start)
    let after = residentMB()

    print(pad(url.lastPathComponent, 42)
          + lead(String(format: "%.1f", sizeMB), 10)
          + lead(String(format: "%.3f", seconds), 10)
          + lead(String(format: "%.2f", Double(file?.bytesRead ?? 0) / 1_048_576.0), 10)
          + lead(String(format: "%.1f", peakMemoryMB()), 10)
          + lead(String(file?.columns.count ?? -1), 7))
    print(String(format: "   resident %.1f MB before, %.1f MB after (grew %.1f MB)",
                 before, after, after - before))
}

// What one column looks like, for checking a specific claim the app makes.
if let column = ProcessInfo.processInfo.environment["COLUMN"],
   let url = args.dropFirst().first.map({ URL(fileURLWithPath: $0) }),
   let file = try? DataFile(url: url),
   let c = file.columns.first(where: { $0.name == column }) {
    print("\n\(c.name): \(c.type.label), \(c.missing) of \(c.sampled) sampled empty "
        + "(\(Int(c.missingShare * 100))%), e.g. \"\(c.example)\"")
    print("   \(file.sampledRows) rows from the top, \(file.probedRows) from further down")
    if let marker = c.sentinel {
        print("   marker \"\(marker)\" in \(c.sentinelCount) rows; numbers underneath: "
            + "\(c.numericApartFromSentinel)")
    } else {
        print("   no missing-value marker spotted")
    }
    if let i = file.columns.firstIndex(where: { $0.name == column }) {
        let seen = file.preview.compactMap { i < $0.count ? $0[i] : nil }
        print("   first preview values: \(seen.prefix(6).map { "\"\($0)\"" }.joined(separator: ", "))")
    }
}

// How long a full-file value count takes — the one operation that deliberately
// reads everything.
if let column = ProcessInfo.processInfo.environment["COUNT"],
   let url = args.dropFirst().first.map({ URL(fileURLWithPath: $0) }),
   let file = try? DataFile(url: url) {
    let started = Date()
    if let whole = try? ColumnValues.fromWholeFile(url, column: column, in: file) {
        print("\ncounting every row of \(column): \(String(format: "%.2f", Date().timeIntervalSince(started)))s")
        print("   \(whole.rows.formatted()) rows, \(whole.distinct) distinct, "
            + "\(whole.missing.formatted()) missing, peak \(String(format: "%.0f", peakMemoryMB())) MB")
        for e in whole.entries.prefix(5) {
            print("   \(e.value.isEmpty ? "(empty)" : e.value): \(e.count.formatted())")
        }
    }
}


// What the viewer costs: the first page, and then a page much further in. They
// should cost the same, because paging resumes from a byte offset rather than
// counting from the top again.
if ProcessInfo.processInfo.environment["PAGES"] != nil,
   let url = args.dropFirst().first.map({ URL(fileURLWithPath: $0) }),
   let file = try? DataFile(url: url) {
    let shown = Array(file.allNames.prefix(12))
    print("\nviewer: \(shown.count) of \(file.allNames.count) columns, "
        + "\(DataPager.pageRows) rows a page")
    var offset: UInt64 = 0
    var row = 1
    for page in 1...20 {
        let started = Date()
        guard let p = try? DataPager.page(url, in: file, columns: shown,
                                          from: offset, firstRow: row) else { break }
        let took = Date().timeIntervalSince(started)
        if page <= 2 || page == 20 {
            print(String(format: "   page %2d: %.4fs, %d rows, peak %.0f MB",
                         page, took, p.rows.count, peakMemoryMB()))
        }
        offset = p.offset
        row += p.rows.count
        if p.atEnd { break }
    }
}

// A look at what the newer steps actually write, for eyeballing the shape of it.
if ProcessInfo.processInfo.environment["SHOW"] != nil {
    var demos: [Action] = []
    var a = Action(kind: .saveExcel); a.outputPath = "outputs/cohort.xlsx"; demos.append(a)
    var b = Action(kind: .saveDelimited); b.outputPath = "outputs/cohort.tsv"; demos.append(b)
    var c = Action(kind: .saveForStats); c.outputPath = "outputs/cohort.dta"; demos.append(c)
    var d = Action(kind: .saveSummary); d.outputPath = "outputs/by_sex.csv"
    d.groups = ["SEX"]; d.statistics = [.count, .mean]; d.target = "AGE"; demos.append(d)
    var e = Action(kind: .crossTab); e.column = "SEX"; e.target = "RESIDNC"; demos.append(e)
    var f = Action(kind: .summaryOf); f.columns = ["AGE", "LOV"]; demos.append(f)
    var g = Action(kind: .saveCSV); g.outputPath = "outputs/cohort.csv"; g.forExcel = true
    demos.append(g)
    for demo in demos {
        print("\n" + (RCode.block(for: demo, types: [:], frame: "nhamcs") ?? "<nothing>"), terminator: "")
    }
}
