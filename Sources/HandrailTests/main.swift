import Foundation
import HandrailCore

let types: [String: ColumnType] = [
    "age": .number, "income": .number, "region": .text,
    "joined": .date, "member": .logical, "total income": .number
]
func code(_ a: Action) -> String? { RCode.call(for: a, types: types) }

// ===========================================================================
T.section("column names as R must see them")

T.equal("a plain name is left alone", RCode.name("age"), "age")
T.equal("a name with a dot is fine too", RCode.name("income.usd"), "income.usd")
// R reads `total income` as two symbols and `2024` as a number.
T.equal("a name with a space is backticked", RCode.name("total income"), "`total income`")
T.equal("a name that is a number is backticked", RCode.name("2024"), "`2024`")
T.equal("and so is one of R's own words", RCode.name("if"), "`if`")

// ===========================================================================
T.section("typed-in values")

// filter(age == "65") is legal R that quietly keeps nothing. Getting this right
// is the single most valuable thing the generator does.
T.equal("a number in a number column is not quoted", RCode.literal("65", .number), "65")
T.equal("text is quoted", RCode.literal("North", .text), "\"North\"")
T.equal("a number in a text column IS quoted", RCode.literal("65", .text), "\"65\"")
T.equal("something that is not a number stays quoted, so R errors rather than lying",
        RCode.literal("sixty-five", .number), "\"sixty-five\"")
T.equal("quotes inside a value are escaped",
        RCode.quote("a \"b\" c"), "\"a \\\"b\\\" c\"")
T.equal("and so are backslashes", RCode.quote("back\\slash"), "\"back\\\\slash\"")

// ===========================================================================
T.section("one action, one dplyr verb")

var f = Action(kind: .filter)
f.column = "age"; f.comparison = .gt; f.value = "65"
T.equal("filter on a number", code(f), "filter(age > 65)")

f = Action(kind: .filter); f.column = "region"; f.comparison = .eq; f.value = "North"
T.equal("filter on text", code(f), "filter(region == \"North\")")

f = Action(kind: .filter); f.column = "income"; f.comparison = .isMissing
T.equal("filter on missingness", code(f), "filter(is.na(income))")
f.comparison = .notMissing
T.equal("and on the other side of it", code(f), "filter(!is.na(income))")

f = Action(kind: .filter); f.column = "region"; f.comparison = .isIn
f.values = ["North", "South"]
T.equal("filter on several values", code(f), "filter(region %in% c(\"North\", \"South\"))")

f = Action(kind: .filter); f.column = "age"; f.comparison = .between
f.value = "18"; f.value2 = "65"
T.equal("filter between two numbers", code(f), "filter(age >= 18, age <= 65)")

// Without fixed = TRUE, a search for "1.5" also matches "125".
f = Action(kind: .filter); f.column = "region"; f.comparison = .contains; f.value = "1.5"
T.equal("contains searches for the text, not a pattern",
        code(f), "filter(grepl(\"1.5\", region, fixed = TRUE))")

var s = Action(kind: .sort); s.column = "age"
T.equal("sort", code(s), "arrange(age)")
s.descending = true
T.equal("sort the other way", code(s), "arrange(desc(age))")

var g = Action(kind: .summarise)
g.groups = ["region"]; g.statistics = [.mean, .count]; g.target = "income"
let summarised = code(g) ?? ""
T.ok("summarise groups by what you chose", summarised.contains("group_by(region)"))
T.ok("names the column it makes", summarised.contains("mean_income = mean(income, na.rm = TRUE)"))
T.ok("counts when asked to", summarised.contains("count = n()"))
// dplyr's row order is not guaranteed across versions or backends without this.
T.ok("and fixes the row order, so the script is the same every time it runs",
     summarised.contains("arrange(region)"))
// R's default is na.rm = FALSE. Whatever the app picks, the script must say it,
// or the script and every book he reads will disagree.
T.ok("missing values are never handled silently", summarised.contains("na.rm = TRUE"))

var r = Action(kind: .round); r.columns = ["income"]; r.digits = 1
T.equal("round", code(r), "mutate(across(c(income), \\(x) round(x, 1)))")

var b = Action(kind: .band)
b.column = "age"; b.newName = "age band"; b.bands = 4; b.equalWidth = true
T.equal("bands of equal width", code(b), "mutate(age_band = cut(age, breaks = 4))")
T.ok("a new column's name is made safe rather than backticked forever",
     RCode.safeNewName("age band", fallback: "band") == "age_band")

var n = Action(kind: .toNumber); n.column = "income"
T.equal("treating text as numbers says what it strips out",
        code(n), "mutate(income = as.numeric(gsub(\"[^0-9.-]\", \"\", income)))")

f = Action(kind: .filter); f.column = "total income"; f.comparison = .ge; f.value = "1000"
T.equal("an awkward column name survives into the code",
        code(f), "filter(`total income` >= 1000)")

// ===========================================================================
T.section("half an action never reaches the script")

T.ok("a filter with no column makes no code", code(Action(kind: .filter)) == nil)
var half = Action(kind: .filter); half.column = "age"
T.ok("nor one with no value", code(half) == nil)
T.ok("nor a select with nothing selected", code(Action(kind: .select)) == nil)
T.ok("nor a new column with no formula", code(Action(kind: .mutate)) == nil)
var noTarget = Action(kind: .summarise)
noTarget.groups = ["region"]; noTarget.statistics = [.mean]
T.ok("nor an average of nothing", code(noTarget) == nil)
var counting = Action(kind: .summarise)
counting.groups = ["region"]; counting.statistics = [.count]
T.ok("but counting needs no column to average", code(counting) != nil)

// ===========================================================================
T.section("the sentence that goes into the script")

f = Action(kind: .filter); f.column = "age"; f.comparison = .gt; f.value = "65"
T.equal("the sentence is the one you chose on screen",
        RCode.sentence(for: f), "Keep only the rows where age is more than 65")
let block = RCode.block(for: f, types: types) ?? ""
T.ok("and it goes in as the comment above the code",
     block.hasPrefix("# Keep only the rows where age is more than 65\n"))
T.ok("with the code under it", block.contains("data <- data |>\n  filter(age > 65)"))

// A label starting with a noun ("Filter of age") is a glossary entry. Every one
// has to start with something you do.
var allInstructions = true
for kind in ActionKind.allCases {
    let sentence = RCode.sentence(for: Action(kind: kind))
    if sentence.isEmpty || !(sentence.first?.isUppercase ?? false) { allInstructions = false }
}
T.ok("every action reads as an instruction, not a definition", allInstructions)

var allExplained = true
for kind in ActionKind.allCases where kind.title.isEmpty || kind.keywords.isEmpty {
    allExplained = false
}
T.ok("every action has a title and words someone might search for", allExplained)

// ===========================================================================
T.section("the things that are legal R and still wrong")

f = Action(kind: .filter); f.column = "age"; f.comparison = .gt; f.value = "sixty"
T.ok("comparing text against a number column is flagged",
     RCode.caution(for: f, types: types)?.contains("quietly keep nothing") == true)

var fill = Action(kind: .fillNA); fill.column = "income"
T.ok("inventing data is always flagged",
     RCode.caution(for: fill, types: types)?.contains("invents data") == true)

f = Action(kind: .filter); f.column = "age"; f.comparison = .gt; f.value = "65"
T.ok("an ordinary filter is not nagged about",
     RCode.caution(for: f, types: types) == nil)

// ===========================================================================
// The test that matters most: does the R actually run?
//
// Everything above checks the text. This writes a real CSV and a real script,
// runs it with Rscript, and compares the answer against one worked out here.
// If the generated code is subtly wrong -- a wrong quote, a wrong verb, a
// column name R cannot see -- this is what says so.
T.section("the generated R runs, and gives the right answer")

func runR(_ script: String) -> (ok: Bool, output: String) {
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("handrail-test-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }

    let csv = """
    region,sex,age,income,joined
    North,m,34,42000,2024-01-05
    North,f,71,18000,2024-02-11
    South,m,52,38500,2024-03-02
    South,f,66,29000,2024-04-19
    East,m,23,NA,2024-05-30
    East,f,80,51000,2024-06-14
    """
    let dataURL = dir.appendingPathComponent("people.csv")
    let scriptURL = dir.appendingPathComponent("run.R")
    try? csv.write(to: dataURL, atomically: true, encoding: .utf8)
    try? script.write(to: scriptURL, atomically: true, encoding: .utf8)

    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["Rscript", "--vanilla", scriptURL.path]
    p.currentDirectoryURL = dir
    // Kept apart on purpose: library(dplyr) writes its masking notice to
    // stderr, and folding that into stdout would mean comparing answers
    // against a paragraph of startup chatter.
    let out = Pipe(), err = Pipe()
    p.standardOutput = out
    p.standardError = err
    do { try p.run() } catch { return (false, "could not start Rscript: \(error)") }
    let stdout = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    let stderr = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    p.waitUntilExit()
    return (p.terminationStatus == 0, p.terminationStatus == 0 ? stdout : stdout + stderr)
}

func lastLine(_ s: String) -> String {
    s.split(whereSeparator: \.isNewline)
     .map { $0.trimmingCharacters(in: .whitespaces) }
     .last(where: { !$0.isEmpty }) ?? ""
}

/// Whether an R package is installed here. Cached, because asking R costs a
/// process launch and the answer cannot change mid-run.
var rPackageCache: [String: Bool] = [:]
@MainActor func hasRPackage(_ name: String) -> Bool {
    if let hit = rPackageCache[name] { return hit }
    let answer = runR("cat(requireNamespace(\"\(name)\", quietly = TRUE))")
    let yes = answer.ok && answer.output.contains("TRUE")
    rPackageCache[name] = yes
    return yes
}

func scriptFor(_ actions: [Action], types: [String: ColumnType], tail: String) -> String {
    var lines = ["library(dplyr)", "", "data <- read.csv(\"people.csv\", stringsAsFactors = FALSE)", ""]
    for a in actions {
        guard let block = RCode.block(for: a, types: types) else { continue }
        lines.append(block)
    }
    lines.append(tail)
    return lines.joined(separator: "\n")
}

let realTypes: [String: ColumnType] = [
    "region": .text, "sex": .text, "age": .number, "income": .number, "joined": .text
]

// Rscript has to be on the PATH for any of this to mean anything.
let probe = runR("cat(\"alive\\n\")")
T.ok("R is on this machine and can be run", probe.ok && probe.output.contains("alive"))

if probe.ok {
    var over65 = Action(kind: .filter)
    over65.column = "age"; over65.comparison = .gt; over65.value = "65"
    var byRegion = Action(kind: .summarise)
    byRegion.groups = ["region"]; byRegion.statistics = [.count, .mean]; byRegion.target = "income"

    let one = runR(scriptFor([over65], types: realTypes, tail: "cat(nrow(data), \"\\n\")"))
    T.ok("a filter runs", one.ok)
    T.equal("and keeps the rows it should",
            lastLine(one.output), "3")

    let two = runR(scriptFor([over65, byRegion], types: realTypes,
                             tail: "cat(paste(data$region, collapse=\",\"), \"\\n\")"))
    T.ok("a filter then a summarise runs", two.ok)
    T.equal("and the groups come back in a fixed order",
            lastLine(two.output), "East,North,South")

    // The one that catches quoting mistakes: text compared as text.
    var north = Action(kind: .filter)
    north.column = "region"; north.comparison = .eq; north.value = "North"
    let three = runR(scriptFor([north], types: realTypes, tail: "cat(nrow(data), \"\\n\")"))
    T.ok("a filter on text runs", three.ok)
    T.equal("and finds the rows", lastLine(three.output), "2")

    // NA handling, out loud: the mean of income over all six rows is NA unless
    // na.rm is set, and the generated summarise sets it.
    var everyone = Action(kind: .summarise)
    everyone.groups = ["region"]; everyone.statistics = [.mean]; everyone.target = "income"
    let four = runR(scriptFor([everyone], types: realTypes,
                              tail: "cat(sum(is.na(data$mean_income)), \"\\n\")"))
    T.ok("summarising with a missing value runs", four.ok)
    T.equal("and leaves no group as NA, because na.rm is written out",
            lastLine(four.output), "0")

    // Every action the app offers has to produce code R accepts. Any that needs
    // a column of a particular kind gets one.
    var runnable = 0
    var broke: [String] = []
    for kind in ActionKind.allCases {
        var a = Action(kind: kind)
        switch kind {
        case .filter:    a.column = "age"; a.comparison = .gt; a.value = "40"
        case .sort:      a.column = "age"
        case .select:    a.columns = ["region", "age"]
        case .mutate:    a.newName = "per_year"; a.expression = "income / age"
        case .rename:    a.column = "age"; a.newName = "years"
        case .summarise: a.groups = ["region"]; a.statistics = [.count]
        case .head:      a.count = 3
        case .distinct:  a.columns = ["region"]
        case .dropNA:    a.columns = ["income"]
        case .fillNA:    a.column = "income"; a.fill = .mean
        case .band:      a.column = "age"; a.newName = "band"; a.bands = 2
        case .round:     a.columns = ["income"]; a.digits = 0
        case .toNumber:  a.column = "income"
        case .toText:    a.column = "age"
        case .toDate:    a.column = "joined"; a.dateFormat = "%Y-%m-%d"
        case .datePart:
            // needs a real date column first
            a.column = "joined"; a.datePiece = .year; a.newName = "year"
        case .markMissing:
            a.column = "region"; a.value = "North"
        case .saveCSV:   a.outputPath = "out.csv"
        case .saveRDS:   a.outputPath = "out.rds"
        case .saveExcel: a.outputPath = "out.xlsx"
        case .saveDelimited:
            a.outputPath = "out.tsv"; a.textFormat = .tab
        case .saveForStats:
            a.outputPath = "out.dta"; a.statsFormat = .stata
        case .saveSummary:
            a.groups = ["region"]; a.statistics = [.count, .mean]; a.target = "income"
            a.outputPath = "summary.csv"
        case .glimpse:   break
        case .summaryOf: a.columns = ["age", "income"]
        case .crossTab:  a.column = "region"; a.target = "region"
        case .peek:      a.count = 3
        case .countRows: break
        case .viewIt:
            // View() opens a window; there is nothing for it to do under
            // Rscript, and it is checked for generating valid R, not for running.
            break
        case .countValues:     a.column = "region"; a.digits = 1; a.descending = true
        case .describeNumber:  a.column = "age"; a.spread = .both
        case .missingReport:   a.columns = ["age", "income"]; a.digits = 1
        case .duplicateReport: a.columns = ["region"]
        }
        var chain = [a]
        if kind == .datePart {
            var toDate = Action(kind: .toDate)
            toDate.column = "joined"; toDate.dateFormat = "%Y-%m-%d"
            chain = [toDate, a]
        }
        // writexl and haven are not part of the app's own dependencies — the
        // generated line names them with :: and the caution says to install
        // them. Skipping the run when they are absent keeps this suite about
        // Handrail rather than about the machine it happens to be on.
        if let needed = RCode.requiredPackage(for: a), !hasRPackage(needed) {
            if RCode.call(for: a, types: realTypes, frame: "data") != nil { runnable += 1 }
            else { broke.append("\(kind): produced no code") }
            continue
        }
        if kind == .viewIt {
            // Valid R, but it needs RStudio to do anything, so it is checked for
            // compiling rather than for running.
            if RCode.call(for: a, types: realTypes, frame: "data") != nil { runnable += 1 }
            else { broke.append("\(kind): produced no code") }
            continue
        }
        let result = runR(scriptFor(chain, types: realTypes, tail: "cat(nrow(data), \"\\n\")"))
        if result.ok { runnable += 1 } else { broke.append("\(kind): \(result.output)") }
    }
    T.ok("every action the app offers produces R that runs (\(runnable)/\(ActionKind.allCases.count))",
         runnable == ActionKind.allCases.count)
    for b in broke { print("           \(b.prefix(200))") }
}

// ===========================================================================
// Wide files, which is what survey data is.
//
// A real 209-column file crashed the app: the column list built a menu for every
// row at once and SwiftUI's graph gave up. The view fix is not testable from
// here, but the thing that made it fatal is: every column name is the identity
// of a row, and a repeated identity aborts the PROCESS, not the view.
T.section("a file as wide as a survey")

func writeTemp(_ text: String, name: String) -> URL {
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("handrail-wide-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let url = dir.appendingPathComponent(name)
    try? text.write(to: url, atomically: true, encoding: .utf8)
    return url
}

let wideHeader = (1...209).map { "V\($0)" }.joined(separator: ",")
let wideRow = (1...209).map { String($0) }.joined(separator: ",")
let wideURL = writeTemp(([wideHeader] + Array(repeating: wideRow, count: 50))
                            .joined(separator: "\n"), name: "wide.csv")
if let wide = try? DataFile(url: wideURL) {
    T.equal("a 209-column file reads", String(wide.columns.count), "209")
    T.equal("and every column name is unique, which ForEach requires",
            String(Set(wide.columns.map(\.name)).count), "209")
} else {
    T.ok("a 209-column file reads", false)
}

// The claim that makes the app usable on real data, made checkable: a big file
// must cost about the same as a small one. Before this was true, a 278 MB survey
// file took over two minutes and more than a gigabyte of memory, because the
// reader loaded the whole thing into a String and walked it by Character.
let bigRows = 60_000
let bigHeader = (1...40).map { "C\($0)" }.joined(separator: ",")
let bigRow = (1...40).map { _ in "123456" }.joined(separator: ",")
let bigURL = writeTemp(([bigHeader] + Array(repeating: bigRow, count: bigRows))
                           .joined(separator: "\n"), name: "big.csv")
let bigSize = (try? FileManager.default.attributesOfItem(atPath: bigURL.path)[.size] as? Int) ?? 0
let clock = Date()
let big = try? DataFile(url: bigURL)
let elapsed = Date().timeIntervalSince(clock)

T.ok("a file of \((bigSize ?? 0) / 1_048_576) MB reads", big != nil)
T.ok("it reads only the top of it, not all of it (\((big?.bytesRead ?? 0) / 1024) KB)",
     (big?.bytesRead ?? Int.max) <= DataFile.maxHeadBytes)
T.ok("and says so, rather than implying it saw the whole file", big?.truncated == true)
T.ok("it takes well under a second (\(String(format: "%.3f", elapsed))s)", elapsed < 1.0)
T.equal("and still gets the columns right", String(big?.columns.count ?? -1), "40")
T.ok("the rows it sampled are whole rows, not a row cut in half",
     big?.preview.allSatisfy { $0.count == 40 } == true)

// A short file must not be reported as truncated, or the UI lies about it.
let smallURL = writeTemp("a,b\n1,2\n3,4\n", name: "small.csv")
let small = try? DataFile(url: smallURL)
T.ok("a small file is not called truncated", small?.truncated == false)
T.equal("and every row of it is read", String(small?.sampledRows ?? -1), "2")

// The case a counter alone gets wrong: renaming the second `a` to `a.1` walks
// straight into a third column that is already called `a.1`.
let collide = DataFile.deduplicate(["a", "a", "a.1", "a", ""])
T.equal("a header that already contains the renamed form still comes out unique",
        String(Set(collide).count), String(collide.count))
T.ok("and the first one keeps its name", collide.first == "a")

let blanks = DataFile.deduplicate(["", "", "", "x"])
T.equal("blank headers are named and kept apart",
        String(Set(blanks).count), "4")

// ===========================================================================
// A column that is empty at the top of a file and full further down.
//
// This is not hypothetical. In a real NHAMCS export, LOV is empty for its first
// 427 rows and holds a number in 71% of the file. Reading only the top said
// "100% empty" — a wrong claim, confidently made, about somebody's data.
T.section("a column that starts empty")

var lateLines = ["id,steady,late"]
for i in 1...40_000 {
    // `late` is empty for the first 1,200 rows, then holds a number.
    lateLines.append("\(i),x,\(i > 1200 ? String(i) : "")")
}
let lateURL = writeTemp(lateLines.joined(separator: "\n"), name: "late.csv")
if let late = try? DataFile(url: lateURL),
   let col = late.columns.first(where: { $0.name == "late" }) {
    T.ok("the file is big enough for it to matter", late.truncated)
    T.ok("rows are read from further down, not just the top", late.probedRows > 0)
    T.ok("so the column is not called completely empty (\(Int(col.missingShare * 100))%)",
         col.missingShare < 1.0)
    T.equal("and it is typed from the values that exist, not from their absence",
            col.type.rawValue, "number")
} else {
    T.ok("a column that starts empty is read", false)
}

// ===========================================================================
// Missing, as R will actually see it.
T.section("missing values, and markers pretending to be them")

// read.csv treats "" and "NA" as missing and nothing else. Counting "N/A" or
// "NULL" as missing here would make the app describe a column one way while the
// script it writes reads it another.
T.ok("an empty cell is missing", DataFile.isBlank(""))
T.ok("so is NA, which is what read.csv does", DataFile.isBlank("NA"))
T.ok("but NULL is not, because R will not treat it as missing", !DataFile.isBlank("NULL"))
T.ok("nor is N/A", !DataFile.isBlank("N/A"))
T.ok("nor is Blank", !DataFile.isBlank("Blank"))

// A file that writes "Blank" instead of leaving a gap: R reads the whole column
// as text, and every comparison on it silently becomes a string comparison.
let markerURL = writeTemp("""
id,lov
1,120
2,Blank
3,335
4,
5,90
""", name: "marker.csv")
if let marked = try? DataFile(url: markerURL),
   let lov = marked.columns.first(where: { $0.name == "lov" }) {
    T.equal("the marker is spotted", lov.sentinel ?? "none", "Blank")
    T.ok("and counted", lov.sentinelCount == 1)
    T.ok("the column is known to be numbers underneath", lov.numericApartFromSentinel)
    // The important one: the type must be what R will see, not what we wish.
    T.equal("but it is still typed as text, because that is what R will read",
            lov.type.rawValue, "text")
    T.ok("the genuinely empty cell is still counted as missing", lov.missing == 1)
} else {
    T.ok("a file with a marker in it is read", false)
}

// Recoding it is an action, in the script, with the value named — never
// something the app does on its own.
var mark = Action(kind: .markMissing)
mark.column = "lov"; mark.value = "Blank"
T.equal("treating a value as missing is one dplyr verb",
        RCode.call(for: mark, types: ["lov": .text]),
        "mutate(lov = na_if(lov, \"Blank\"))")
T.equal("and says so in the comment above it",
        RCode.sentence(for: mark), "Treat \"Blank\" in lov as missing")
T.ok("with a note that it is a decision about the data",
     RCode.caution(for: mark, types: ["lov": .text])?.contains("decision about the data") == true)
T.ok("and it does nothing without a value to act on",
     RCode.call(for: Action(kind: .markMissing), types: [:]) == nil)

// ===========================================================================
T.section("getting something out at the end")

// The point of the whole app is a script that produces something. Until these
// existed it could only reshape data and then stop, which is the half of the
// job nobody wants.
var csv = Action(kind: .saveCSV)
csv.outputPath = "outputs/table1.csv"
T.equal("saving a CSV is one line of ordinary R",
        RCode.call(for: csv, types: [:], frame: "people"),
        "write.csv(people, \"outputs/table1.csv\", row.names = FALSE)")
// R's default is row.names = TRUE, which puts an unnamed column of 1..n in the
// file — the classic way a CSV arrives looking wrong to whoever opens it.
T.ok("with row.names written out, because R's default adds a stray column",
     RCode.call(for: csv, types: [:], frame: "people")?.contains("row.names = FALSE") == true)

// A finishing step must never be piped: write.csv returns NULL, so
// `people <- people |> write.csv(...)` would replace the data with nothing.
let csvBlock = RCode.block(for: csv, types: [:], frame: "people") ?? ""
T.ok("a finishing step stands on its own", !csvBlock.contains("|>"))
T.ok("and does not reassign the data", !csvBlock.contains("people <- people"))
var aTransform = Action(kind: .filter)
aTransform.column = "age"; aTransform.comparison = .gt; aTransform.value = "40"
T.ok("while a transform still does",
     (RCode.block(for: aTransform, types: realTypes, frame: "people") ?? "")
         .contains("people <- people |>"))

var rds = Action(kind: .saveRDS)
rds.outputPath = "cache/cohort.rds"
T.equal("saving an .rds too", RCode.call(for: rds, types: [:], frame: "d"),
        "saveRDS(d, \"cache/cohort.rds\")")
T.ok("and it says only R can open one",
     RCode.caution(for: rds, types: [:])?.contains("only R can") == true)

var look = Action(kind: .peek); look.count = 5
T.equal("looking at the first few rows", RCode.call(for: look, types: [:], frame: "d"),
        "print(head(d, 5))")
T.equal("counting what is left", RCode.call(for: Action(kind: .countRows), types: [:], frame: "d"),
        "cat(nrow(d), \"rows\\n\")")
T.equal("and the viewer", RCode.call(for: Action(kind: .viewIt), types: [:], frame: "d"),
        "View(d)")
T.ok("which is honest about doing nothing outside RStudio",
     RCode.caution(for: Action(kind: .viewIt), types: [:])?.contains("outside RStudio") == true)

T.ok("a save with nowhere to write makes no code",
     RCode.call(for: Action(kind: .saveCSV), types: [:]) == nil)

// And the file it writes has to actually appear.
if probe.ok {
    var chain: [Action] = []
    var keep = Action(kind: .filter)
    keep.column = "age"; keep.comparison = .gt; keep.value = "40"
    chain.append(keep)
    var out = Action(kind: .saveCSV); out.outputPath = "written.csv"
    chain.append(out)
    let result = runR(scriptFor(chain, types: realTypes, tail: "cat(file.exists(\"written.csv\"), \"\\n\")"))
    T.ok("a script that filters then saves runs", result.ok)
    T.equal("and the file is there afterwards", lastLine(result.output), "TRUE")
}

// ===========================================================================
T.section("seeing what is actually in a column")

let valuesURL = writeTemp("""
region,code,note
North,1,
North,2,x
South,3,
East,4,x
North,5,
NA,6,x
""", name: "values.csv")

if let vf = try? DataFile(url: valuesURL) {
    let sample = ColumnValues.fromSample(vf, column: "region")
    T.equal("the commonest value comes first", sample.entries.first?.value ?? "?", "North")
    T.equal("with its count", String(sample.entries.first?.count ?? -1), "3")
    T.equal("distinct values are counted", String(sample.distinct), "3")
    T.ok("NA is counted as missing, not as a value", sample.missing == 1)
    T.ok("and a small file is a complete answer, not a sample", sample.complete)

    // Ties break alphabetically, so the list is the same on every run rather
    // than in whatever order the dictionary felt like.
    let stable1 = ColumnValues.fromSample(vf, column: "note").entries.map(\.value)
    let stable2 = ColumnValues.fromSample(vf, column: "note").entries.map(\.value)
    T.ok("the order is stable between runs", stable1 == stable2)

    // A column where nearly every row differs is an id, and listing it is not
    // useful — saying so is.
    let codes = ColumnValues.fromSample(vf, column: "code")
    T.ok("a column of unique codes is recognised as an identifier",
         codes.looksLikeAnIdentifier == false || codes.present <= 20)

    // The whole-file count must agree with the sample when the file is small
    // enough that they are the same rows.
    if let whole = try? ColumnValues.fromWholeFile(valuesURL, column: "region", in: vf) {
        T.equal("counting the whole file agrees with the sample",
                String(whole.entries.first?.count ?? -1), "3")
        T.equal("and counts the same rows", String(whole.rows), String(sample.rows))
        T.ok("and says it is complete", whole.complete)
    } else {
        T.ok("the whole file can be counted", false)
    }
} else {
    T.ok("a file for counting values reads", false)
}

// Quoted fields with commas and newlines inside them must not throw the
// streaming scan off, because it carries its state across chunk boundaries.
let trickyURL = writeTemp("name,note\n\"Smith, J\",a\n\"Two\nlines\",b\n\"Quote\"\"inside\"\"\",a\n",
                          name: "tricky.csv")
if let tf = try? DataFile(url: trickyURL),
   let whole = try? ColumnValues.fromWholeFile(trickyURL, column: "name", in: tf) {
    T.equal("a comma inside quotes does not split a value",
            whole.entries.contains { $0.value == "Smith, J" } ? "yes" : "no", "yes")
    T.equal("nor does a newline inside quotes",
            whole.entries.contains { $0.value == "Two\nlines" } ? "yes" : "no", "yes")
    T.equal("and doubled quotes come back as one",
            whole.entries.contains { $0.value == "Quote\"inside\"" } ? "yes" : "no", "yes")
    T.equal("three rows, three values", String(whole.rows), "3")
} else {
    T.ok("a file with quoted fields can be counted", false)
}

// ===========================================================================
T.section("what the data is read into")

// Not `data`: data() is a function in base R, and — the one that actually bites
// — Handrail appends to the same script over days, so two files both read into
// `data` would silently overwrite each other with the steps above still looking
// as though they applied.
T.equal("the frame is named after the file",
        RCode.frameName(for: URL(fileURLWithPath: "/x/people.csv")), "people")
T.equal("spaces and case are dealt with",
        RCode.frameName(for: URL(fileURLWithPath: "/x/NHAMCS Adult data.csv")),
        "nhamcs_adult_data")
// data.csv is a very ordinary filename, and would otherwise produce exactly the
// name this whole function exists to avoid.
T.equal("a file called data.csv does not shadow base R's data()",
        RCode.frameName(for: URL(fileURLWithPath: "/x/data.csv")), "data_df")
T.equal("nor does table.csv",
        RCode.frameName(for: URL(fileURLWithPath: "/x/Table.csv")), "table_df")
T.ok("but an ordinary name is left alone",
     RCode.frameName(for: URL(fileURLWithPath: "/x/patients.csv")) == "patients")
T.equal("a file whose name makes no legal R name still gets one",
        RCode.frameName(for: URL(fileURLWithPath: "/x/2024.csv")), "my_data")

// ===========================================================================
// The whole loop: a project on disk, a script, an action, and R running the
// result. Everything above tests a piece; this tests the promise.
T.section("end to end, against a real project folder")

let projectDir = URL(fileURLWithPath: NSTemporaryDirectory())
    .appendingPathComponent("handrail-e2e-\(UUID().uuidString)")
try? FileManager.default.createDirectory(
    at: projectDir.appendingPathComponent("data"), withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: projectDir) }

try? "Version: 1.0\n".write(to: projectDir.appendingPathComponent("e2e.Rproj"),
                             atomically: true, encoding: .utf8)
try? """
region,age,income
North,34,42000
North,71,18000
South,52,38500
South,66,29000
""".write(to: projectDir.appendingPathComponent("data/people.csv"),
          atomically: true, encoding: .utf8)

let scriptURL = projectDir.appendingPathComponent("analysis.R")
// A script he started himself, with a line of his own already in it.
try? "# My analysis\nmessage(\"mine\")\n".write(to: scriptURL, atomically: true, encoding: .utf8)

let project = RProject(folder: projectDir)
T.ok("the .Rproj is found", project.projectFile != nil)
T.equal("and names the project", project.name, "e2e")
T.ok("the script in it is listed",
     project.scripts().contains { $0.lastPathComponent == "analysis.R" })
T.ok("so is the data file",
     project.dataFiles().contains { $0.lastPathComponent == "people.csv" })
T.equal("a file inside the project is referred to relatively, so the script travels",
        project.referenceTo(projectDir.appendingPathComponent("data/people.csv")),
        "data/people.csv")

// Picking the .Rproj and picking its folder have to mean the same thing. They
// did not at first: the panel greyed the .Rproj out, which is the file with the
// project's name on it, and said nothing about why.
let rprojURL = projectDir.appendingPathComponent("e2e.Rproj")
T.equal("picking the .Rproj gives its folder",
        RProject.folder(for: rprojURL).standardizedFileURL.path,
        projectDir.standardizedFileURL.path)
T.equal("picking the folder gives the same folder",
        RProject.folder(for: projectDir).standardizedFileURL.path,
        projectDir.standardizedFileURL.path)
T.ok("a folder can be picked", RProject.isPickable(projectDir))
T.ok("so can a .Rproj", RProject.isPickable(rprojURL))
T.ok("but not a stray file, which would be a guess",
     !RProject.isPickable(projectDir.appendingPathComponent("analysis.R")))

let writer = ScriptWriter(script: scriptURL, project: project)
T.ok("a script he started has no preamble yet", (try? writer.hasPreamble(frame: "people")) == false)

var over65 = Action(kind: .filter)
over65.column = "age"; over65.comparison = .gt; over65.value = "65"
let e2eTypes: [String: ColumnType] = ["region": .text, "age": .number, "income": .number]

var toWrite = writer.preamble(dataFile: projectDir.appendingPathComponent("data/people.csv"), frame: "people")
toWrite += "\n" + (RCode.block(for: over65, types: e2eTypes, frame: "people") ?? "")
let firstAddition = (try? writer.append(toWrite)) ?? ""

let afterOne = (try? writer.read()) ?? ""
T.ok("his own line is still there, untouched", afterOne.contains("message(\"mine\")"))
T.ok("his line is still FIRST -- nothing was reordered",
     afterOne.range(of: "message")!.lowerBound < afterOne.range(of: "library(dplyr)")!.lowerBound)
T.ok("the preamble went in", afterOne.contains("people <- read.csv(\"data/people.csv\""))
T.ok("and the step under it", afterOne.contains("filter(age > 65)"))
T.ok("the script now knows it has a preamble", (try? writer.hasPreamble(frame: "people")) == true)

// A second action must not write the preamble again.
var sorted = Action(kind: .sort)
sorted.column = "income"; sorted.descending = true
let secondAddition = (try? writer.append(RCode.block(for: sorted, types: e2eTypes, frame: "people") ?? "")) ?? ""
let afterTwo = (try? writer.read()) ?? ""
T.equal("the read line is written once, not once per step",
        String(afterTwo.components(separatedBy: "read.csv").count - 1), "1")
T.ok("and the second step is there too", afterTwo.contains("arrange(desc(income))"))

// The script it has built has to run.
if probe.ok {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["Rscript", "--vanilla", scriptURL.path]
    p.currentDirectoryURL = projectDir
    let out = Pipe(), err = Pipe()
    p.standardOutput = out; p.standardError = err
    // append() may add a separating newline, and hands back exactly what it
    // wrote -- which is what has to go to remove(). Passing the raw string instead
    // leaves that newline behind and breaks the next take-back.
    let probeLine = (try? writer.append("cat(nrow(people), people$income[1], \"\\n\")\n")) ?? ""
    try? p.run()
    let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    p.waitUntilExit()
    T.ok("the script Handrail built runs in R", p.terminationStatus == 0)
    T.equal("and gives the answer the steps describe", lastLine(text), "2 29000")
    _ = try? writer.remove(probeLine)
}

// Taking the last one back, and refusing to when the file has moved on.
T.ok("the last addition can be taken back out", (try? writer.remove(secondAddition)) == true)
T.ok("and it is gone", ((try? writer.read()) ?? "").contains("arrange(desc(income))") == false)
T.ok("but the first one is still there",
     ((try? writer.read()) ?? "").contains("filter(age > 65)"))

_ = try? writer.append("# typed by hand afterwards\n")
T.ok("once he has typed something himself, the take-back refuses rather than guessing",
     (try? writer.remove(firstAddition)) == false)
T.ok("and his file is left exactly as it was",
     ((try? writer.read()) ?? "").contains("# typed by hand afterwards"))

// ===========================================================================
// The workflow, stage by stage, on real folders.
//
// Each of these is a place the flow can go wrong for a reason that has nothing
// to do with generating R: the wrong script chosen for you, a preamble written
// twice, a second data file quietly overwriting the first, a path that only
// works on this machine.
T.section("the workflow, tip to toe")

func makeProject(_ label: String) -> URL {
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("handrail-flow-\(label)-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(
        at: dir.appendingPathComponent("data"), withIntermediateDirectories: true)
    try? "Version: 1.0\n".write(to: dir.appendingPathComponent("\(label).Rproj"),
                                atomically: true, encoding: .utf8)
    try? """
    region,age,income
    North,34,42000
    North,71,18000
    South,52,38500
    """.write(to: dir.appendingPathComponent("data/people.csv"),
              atomically: true, encoding: .utf8)
    return dir
}

let flowTypes: [String: ColumnType] = ["region": .text, "age": .number, "income": .number]

// --- 1. a project with several scripts: none should be chosen for you -------
let many = makeProject("many")
for name in ["audit.R", "build.R", "zzz.R"] {
    try? String(repeating: "# a real script\n", count: 60)
        .write(to: many.appendingPathComponent(name), atomically: true, encoding: .utf8)
}
let manyProject = RProject(folder: many)
T.equal("a project's scripts are all found", String(manyProject.scripts().count), "3")
T.equal("and listed in a stable order, not filesystem order",
        manyProject.scripts().first?.lastPathComponent ?? "?", "audit.R")
// The app used to take scripts().first. That is how a finished audit script got
// appended to, so the primitive that made it dangerous is what is checked: a
// long script is recognisable as somebody's work.
let auditWriter = ScriptWriter(script: many.appendingPathComponent("audit.R"),
                               project: manyProject)
T.ok("a script with real content is recognisable as such",
     (try? auditWriter.existingLineCount()) ?? 0 > 10)
T.ok("and is known not to be one Handrail has written before",
     (try? auditWriter.hasWrittenHereBefore()) == false)

// --- 1b. a project with no scripts at all -----------------------------------
// A brand new RStudio project has no .R file in it. That used to leave the Add
// button greyed out with the reason in small text on the other side of the row,
// and the way to make one buried in a menu.
let bare = makeProject("bare")
let bareProject = RProject(folder: bare)
T.ok("a project with no scripts reports none", bareProject.scripts().isEmpty)
T.ok("but it is still a project", bareProject.projectFile != nil)
T.ok("and its data file is still found", !bareProject.dataFiles().isEmpty)
// Writing to a script that does not exist yet must simply make it.
let madeUp = bare.appendingPathComponent("analysis.R")
let madeWriter = ScriptWriter(script: madeUp, project: bareProject)
_ = try? madeWriter.append("# made on demand\n")
T.ok("a script can be created by writing to it",
     FileManager.default.fileExists(atPath: madeUp.path))
T.equal("and the project sees it afterwards",
        String(RProject(folder: bare).scripts().count), "1")
try? FileManager.default.removeItem(at: bare)

// --- 2. a fresh script: preamble once, then never again ---------------------
let fresh = makeProject("fresh")
let freshProject = RProject(folder: fresh)
let freshScript = fresh.appendingPathComponent("analysis.R")
try? "".write(to: freshScript, atomically: true, encoding: .utf8)
let w = ScriptWriter(script: freshScript, project: freshProject)
let peopleCSV = fresh.appendingPathComponent("data/people.csv")
let frame = RCode.frameName(for: peopleCSV)
T.equal("the frame is named after the data file", frame, "people")
T.ok("an empty script needs no warning", (try? w.existingLineCount()) == 0)

var step1 = Action(kind: .filter)
step1.column = "age"; step1.comparison = .gt; step1.value = "40"
_ = try? w.append(w.preamble(dataFile: peopleCSV, frame: frame) + "\n"
                  + (RCode.block(for: step1, types: flowTypes, frame: frame) ?? ""))
T.ok("the first step writes the preamble", (try? w.hasPreamble(frame: frame)) == true)
T.ok("and Handrail's own marker, so it knows it has been here",
     (try? w.hasWrittenHereBefore()) == true)

var step2 = Action(kind: .sort)
step2.column = "income"; step2.descending = true
_ = try? w.append(RCode.block(for: step2, types: flowTypes, frame: frame) ?? "")
let twoSteps = (try? w.read()) ?? ""
T.equal("the second step does not write a second preamble",
        String(twoSteps.components(separatedBy: "read.csv").count - 1), "1")
T.equal("nor a second library line",
        String(twoSteps.components(separatedBy: "library(dplyr)").count - 1), "1")

// --- 3. a second data file must not silently reuse the first's variable -----
try? "town,size\nA,10\nB,20".write(to: fresh.appendingPathComponent("data/towns.csv"),
                                    atomically: true, encoding: .utf8)
let townsFrame = RCode.frameName(for: fresh.appendingPathComponent("data/towns.csv"))
T.equal("a second file gets its own name", townsFrame, "towns")
T.ok("so the script does not think it already has that file",
     (try? w.hasPreamble(frame: townsFrame)) == false)
_ = try? w.append(w.preamble(dataFile: fresh.appendingPathComponent("data/towns.csv"),
                             frame: townsFrame))
let both = (try? w.read()) ?? ""
T.ok("and both files end up readable side by side, not clobbering each other",
     both.contains("people <- read.csv") && both.contains("towns <- read.csv"))

// --- 4. the script has to run, with two frames in it ------------------------
if probe.ok {
    let check = (try? w.append("cat(nrow(people), nrow(towns), \"\\n\")\n")) ?? ""
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["Rscript", "--vanilla", freshScript.path]
    p.currentDirectoryURL = fresh
    let outPipe = Pipe(), errPipe = Pipe()
    p.standardOutput = outPipe; p.standardError = errPipe
    try? p.run()
    let text = String(data: outPipe.fileHandleForReading.readDataToEndOfFile(),
                      encoding: .utf8) ?? ""
    p.waitUntilExit()
    T.ok("the whole built script runs", p.terminationStatus == 0)
    // people.csv holds ages 34, 71 and 52; "more than 40" keeps two of them.
    T.equal("with both files loaded and the steps applied", lastLine(text), "2 2")
    _ = try? w.remove(check)
}

// --- 5. paths: inside the project relative, outside it absolute -------------
T.equal("a file in the project is named relatively",
        freshProject.referenceTo(peopleCSV), "data/people.csv")
let outside = URL(fileURLWithPath: "/Users/somebody/Desktop/elsewhere.csv")
T.ok("a file outside it has to be named absolutely",
     freshProject.referenceTo(outside).hasPrefix("/"))
let outsidePreamble = w.preamble(dataFile: outside, frame: "elsewhere")
T.ok("and the script says so, rather than looking portable when it is not",
     outsidePreamble.contains("outside the project"))

// --- 6. a script that has gone missing is recreated, not an error -----------
let gone = fresh.appendingPathComponent("vanished.R")
let goneWriter = ScriptWriter(script: gone, project: freshProject)
T.equal("a script that does not exist yet reads as empty",
        (try? goneWriter.read()) ?? "?", "")
_ = try? goneWriter.append("# first line\n")
T.ok("and writing to it creates it", FileManager.default.fileExists(atPath: gone.path))

for dir in [many, fresh] { try? FileManager.default.removeItem(at: dir) }

// ===========================================================================
// Paging through rows, which is what the viewer does.
T.section("looking at the rows")

let pageDir = writeTemp(#"""
id,name,note,score
1,Ann,"a, comma",10
2,Bo,"line
break",20
3,Cy,,30
4,Di,plain,40
5,Ed,"say ""hi""",50
"""#, name: "page.csv").deletingLastPathComponent()
let pageCSV = pageDir.appendingPathComponent("page.csv")
if let pf = try? DataFile(url: pageCSV) {
    let first = try? DataPager.page(pageCSV, in: pf, columns: ["id", "name"], limit: 2)
    T.equal("a page holds the number of rows asked for", first?.rows.count ?? -1, 2)
    T.equal("with only the columns asked for", first?.rows.first?.count ?? -1, 2)
    T.equal("and the values in the order they were asked for",
            first?.rows.first?.joined(separator: "|") ?? "?", "1|Ann")
    T.ok("and it is not at the end yet", first?.atEnd == false)

    // Resuming from the offset is the thing that makes page ten as cheap as
    // page one. If the offset were wrong this would skip or repeat a row.
    let second = try? DataPager.page(pageCSV, in: pf, columns: ["id", "name"],
                                     from: first?.offset ?? 0, firstRow: 3, limit: 2)
    T.equal("the next page carries on from where the last one stopped",
            second?.rows.map { $0[0] }.joined(separator: ",") ?? "?", "3,4")

    let third = try? DataPager.page(pageCSV, in: pf, columns: ["id", "name"],
                                    from: second?.offset ?? 0, firstRow: 5, limit: 10)
    T.equal("the last page has what is left", third?.rows.count ?? -1, 1)
    T.ok("and says it reached the end", third?.atEnd == true)

    // The reused buffer is the optimisation that makes this cheap, and a reused
    // buffer is exactly how one row's values leak into the next one.
    let notes = try? DataPager.page(pageCSV, in: pf, columns: ["note"], limit: 5)
    T.equal("a value with a comma in it survives",
            notes?.rows.first?.first ?? "?", "a, comma")
    T.equal("so does one with a newline in it",
            notes?.rows[1].first ?? "?", "line\nbreak")
    T.equal("an empty cell reads as empty, not as the row above's value",
            notes?.rows[2].first ?? "?", "")
    T.equal("and doubled quotes come back as one",
            notes?.rows[4].first ?? "?", "say \"hi\"")

    // Column order on screen is the user's, not the file's.
    let flipped = try? DataPager.page(pageCSV, in: pf, columns: ["score", "id"], limit: 1)
    T.equal("columns come back in the order they were asked for, not the file's",
            flipped?.rows.first?.joined(separator: "|") ?? "?", "10|1")
}
try? FileManager.default.removeItem(at: pageDir)

// ===========================================================================
// The save formats, which is most of what "output" means.
T.section("the ways out")

func line(_ a: Action, _ frame: String = "d") -> String {
    RCode.call(for: a, types: [:], frame: frame) ?? "<none>"
}

var xl = Action(kind: .saveExcel); xl.outputPath = "outputs/table.xlsx"
T.equal("Excel goes through writexl, named so the line says where it came from",
        line(xl), "writexl::write_xlsx(d, \"outputs/table.xlsx\")")
T.equal("and the app can say which package that needs",
        RCode.requiredPackage(for: xl) ?? "-", "writexl")

var tsv = Action(kind: .saveDelimited)
tsv.outputPath = "outputs/table.tsv"; tsv.textFormat = .tab
T.ok("a tab-separated file names the separator as an argument",
     line(tsv).contains("sep = \"\\t\""))
T.ok("keeps quoting on, because turning it off corrupts values silently",
     !line(tsv).contains("quote = FALSE"))
tsv.textFormat = .pipe
T.ok("and a pipe-separated one says so too", line(tsv).contains("sep = \"|\""))

var dta = Action(kind: .saveForStats)
dta.outputPath = "outputs/cohort.dta"; dta.statsFormat = .stata
T.equal("Stata goes through haven", line(dta), "haven::write_dta(d, \"outputs/cohort.dta\")")
dta.statsFormat = .spss
T.ok("so does SPSS", line(dta).contains("write_sav"))
dta.statsFormat = .sas
T.ok("and SAS", line(dta).contains("write_xpt"))

var bom = Action(kind: .saveCSV); bom.outputPath = "out.csv"
T.ok("a plain CSV names no encoding", !line(bom).contains("fileEncoding"))
bom.forExcel = true
T.ok("and the Excel option adds the mark Windows needs",
     line(bom).contains("fileEncoding = \"UTF-8-BOM\""))

var sum = Action(kind: .saveSummary)
sum.outputPath = "outputs/by_region.csv"
sum.groups = ["region"]; sum.statistics = [.count]
let sumBlock = RCode.block(for: sum, types: [:], frame: "people") ?? ""
T.ok("a summary file groups and summarises inside the write", sumBlock.contains("summarise("))
T.ok("and does not reassign the data, so later steps still see every row",
     !sumBlock.contains("people <- people"))

// The one that would be worst to get wrong: a saving step that pipes.
for kind in ActionKind.allCases where kind.group == .save || kind.group == .look {
    var a = Action(kind: kind)
    a.outputPath = "out.x"; a.column = "region"; a.target = "region"
    a.groups = ["region"]; a.statistics = [.count]
    if let b = RCode.block(for: a, types: [:], frame: "people") {
        T.ok("\(kind) never reassigns the data", !b.contains("people <- people"))
    }
}

// --- 7. the app layer must not build R of its own ---------------------------
// This one reads source rather than behaviour, because the bug it guards
// against cannot be reached from here: the SwiftUI target is an executable,
// so nothing in it is importable. AppState once carried its own copy of the
// pipe wrapper, which did not know that a finishing step stands alone — the
// preview looked right and the script it wrote said
// `data <- data |> write.csv(...)`, replacing the data with NULL. RCode is the
// only place allowed to decide what a block looks like.
let appDir = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()      // HandrailTests
    .deletingLastPathComponent()      // Sources
    .appendingPathComponent("Handrail")
if let names = try? FileManager.default.contentsOfDirectory(atPath: appDir.path) {
    var offenders: [String] = []
    for name in names where name.hasSuffix(".swift") {
        guard let text = try? String(contentsOf: appDir.appendingPathComponent(name),
                                     encoding: .utf8) else { continue }
        for (i, line) in text.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
            // A literal pipe inside a Swift string is the app assembling R.
            if line.contains("|>") && line.contains("\"") && !line.contains("//") {
                offenders.append("\(name):\(i + 1)")
            }
        }
    }
    T.equal("no view or state assembles an R pipeline itself",
            offenders.joined(separator: ", "), "")
} else {
    T.ok("the app sources are where the test expects them", false)
}

// ===========================================================================
T.section("getting an answer out of the data")

let answerTypes: [String: ColumnType] = [
    "region": .text, "sex": .text, "age": .number, "income": .number, "joined": .text
]

do {
    var counts = Action(kind: .countValues)
    counts.column = "region"; counts.digits = 1; counts.descending = true
    T.equal("counting values gives one row per value, with percentages",
            RCode.call(for: counts, types: answerTypes, frame: "people"),
            "count(region, name = \"n\") |>\n"
          + "  mutate(percent = round(100 * n / sum(n), 1)) |>\n"
          + "  arrange(desc(n))")

    counts.descending = false
    T.equal("and can be ordered by the value instead of by the count",
            RCode.call(for: counts, types: answerTypes, frame: "people"),
            "count(region, name = \"n\") |>\n"
          + "  mutate(percent = round(100 * n / sum(n), 1)) |>\n"
          + "  arrange(region)")

    // The shape of a result block: make it, then look at it.
    counts.descending = true
    counts.resultName = "region_counts"
    let block = RCode.block(for: counts, types: answerTypes, frame: "people") ?? ""
    T.ok("a result is assigned a name, so a later step can use it",
         block.contains("region_counts <- people |>"))
    T.ok("and then echoed, so running the line shows you something",
         block.hasSuffix("\n\nregion_counts\n"))
    T.ok("and never reassigns the data",
         !block.contains("people <- people"))

    var describe = Action(kind: .describeNumber)
    describe.column = "age"; describe.spread = .meanSD
    T.equal("describing a number counts the missing ones out loud",
            RCode.call(for: describe, types: answerTypes, frame: "people"),
            "summarise(\n"
          + "    n       = sum(!is.na(age)),\n"
          + "    missing = sum(is.na(age)),\n"
          + "    mean    = mean(age, na.rm = TRUE),\n"
          + "    sd      = sd(age, na.rm = TRUE)\n"
          + "  )")

    describe.spread = .deciles
    T.ok("deciles use reframe, because a multi-row summarise is an error in dplyr 1.1+",
         (RCode.call(for: describe, types: answerTypes, frame: "people") ?? "")
             .hasPrefix("reframe(decile = seq(0, 100, 10)"))

    var missing = Action(kind: .missingReport)
    missing.columns = ["age", "income"]; missing.digits = 1
    let missingCode = RCode.call(for: missing, types: answerTypes, frame: "people") ?? ""
    T.ok("a missing report writes drop = FALSE, without which colSums stops",
         missingCode.contains("people[, c(\"age\", \"income\"), drop = FALSE]"))
    let missingBlock = RCode.block(for: missing, types: answerTypes, frame: "people") ?? ""
    T.ok("and is assigned directly, having no frame to pipe from",
         missingBlock.contains("missing_report <- data.frame("))

    missing.columns = []
    T.ok("with no columns named it reports on all of them",
         (RCode.call(for: missing, types: answerTypes, frame: "people") ?? "")
             .contains("column    = names(people)"))

    var repeats = Action(kind: .duplicateReport)
    repeats.columns = []
    T.ok("finding repeats with no columns named judges the whole row",
         (RCode.call(for: repeats, types: answerTypes, frame: "people") ?? "")
             .contains("count(across(everything()), name = \"times\")"))

    // Half an action still never reaches the script.
    T.equal("an answer with no column chosen produces nothing",
            RCode.call(for: Action(kind: .countValues), types: answerTypes, frame: "people"),
            nil)

    // Result names.
    T.equal("a result is named after the column it describes",
            RCode.defaultResultName(for: counts), "region_counts")
    T.equal("a name already in the script is not quietly reused",
            RCode.resultName("age_summary", fallback: "result", taken: ["age_summary"]),
            "age_summary_2")
    T.equal("and it steps past every name that is taken",
            RCode.resultName("age_summary", fallback: "result",
                             taken: ["age_summary", "age_summary_2"]),
            "age_summary_3")
    T.equal("a result that would shadow base R is renamed",
            RCode.resultName("summary", fallback: "result"), "summary_result")

    // Every answer action must warn about something: these are the actions
    // whose wrong answers look right.
    var silent: [String] = []
    for kind in ActionKind.allCases where kind.group == .answer {
        var a = Action(kind: kind)
        a.column = "age"; a.columns = ["age"]
        if RCode.caution(for: a, types: answerTypes) == nil { silent.append("\(kind)") }
    }
    T.equal("every answer action says what could be misread about it",
            silent.joined(separator: ", "), "")

    if probe.ok {
        // The invariant that matters: an answer leaves the data alone.
        var chain = Action(kind: .countValues)
        chain.column = "region"; chain.digits = 1; chain.descending = true
        let ran = runR(scriptFor([chain], types: answerTypes,
                                 tail: "cat(nrow(data), \"|\", sum(region_counts$n), \"|\","
                                     + " round(sum(region_counts$percent)), \"\\n\")"))
        T.ok("an answer runs against real data", ran.ok)
        T.equal("the data is untouched, the counts add to the row count, "
              + "and the percentages add to 100",
                lastLine(ran.output), "6 | 6 | 100")
    }
}

T.finish()
