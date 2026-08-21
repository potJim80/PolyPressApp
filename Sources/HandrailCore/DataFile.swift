import Foundation

/// One column of the chosen file, as far as reading the top of it can tell.
public struct Column: Identifiable, Sendable, Equatable {
    public var id: String { name }
    public let name: String
    public let type: ColumnType
    public let missing: Int
    public let sampled: Int
    public let example: String
    /// A value that looks like a missing-data marker but is not one to R —
    /// "Blank", "Unknown", "." and friends. Reported, never acted on: recoding
    /// somebody's missing-value convention is a decision about their data, and
    /// it belongs to them.
    public let sentinel: String?
    public let sentinelCount: Int
    /// True when the values that are not the sentinel are all numbers — the
    /// case where a column is really numeric and R will still read it as text.
    public let numericApartFromSentinel: Bool

    public var missingShare: Double { sampled == 0 ? 0 : Double(missing) / Double(sampled) }
    public var sentinelShare: Double { sampled == 0 ? 0 : Double(sentinelCount) / Double(sampled) }
}

/// The data file, read only far enough to fill the dropdowns.
///
/// The app never analyses anything — RStudio does that. All it needs is the
/// column names, roughly what each holds, and a few rows to show. So it reads
/// **the first couple of megabytes and stops**, whatever the file's size.
///
/// The first version did not. It loaded the whole file with
/// `String(contentsOf:)` and walked it with a `Character` iterator: on a real
/// 278 MB survey file that was over a gigabyte of memory and more than two
/// minutes, for four hundred rows it was going to keep. Two things caused it,
/// and both are worth naming because they are the standard way to get this
/// wrong in Swift:
///
/// - **Reading it all.** `Data(contentsOf:)` plus `String(data:encoding:)` holds
///   the file twice over. Nothing here ever needs the middle of the file.
/// - **`Character`.** Swift's `Character` is a grapheme cluster, so iterating a
///   `String` does Unicode segmentation on every step. For splitting on commas
///   and newlines that work is entirely wasted. This parses UTF-8 **bytes** and
///   decodes only the fields it keeps.
public struct DataFile: Sendable {
    public let url: URL
    public let columns: [Column]
    public let preview: [[String]]
    /// Every row that was read, not just the twelve shown. Kept so the values in
    /// a column can be listed without going back to disk — a few hundred rows is
    /// nothing to hold, and it is the difference between an instant answer and a
    /// spinner.
    public let sampleRows: [[String]]
    /// Which byte separates the fields, so a full-file scan reads it the same
    /// way this did.
    public let delimiter: UInt8
    public let sampledRows: Int
    /// Rows read from further down the file, on top of the ones at the top.
    /// See `readProbe` for why they exist.
    public let probedRows: Int
    /// How much of the file was actually read. The whole point is that this
    /// stays small however big the file is, so it is reported rather than
    /// assumed — `handrail-bench` prints it.
    public let bytesRead: Int
    /// True when the file is longer than what was read, which is the normal case
    /// and is said out loud in the UI rather than implied.
    public let truncated: Bool

    /// Built once when the file is read. It used to be a computed property,
    /// which meant allocating a 209-entry dictionary several times per
    /// keystroke: the code preview, the caution and the Add button each ask for
    /// it, and every one of them runs on every render.
    public let types: [String: ColumnType]

    /// Column names by what they hold, also built once, for the same reason —
    /// every picker in the form filters the column list on every render.
    public let namesByType: [ColumnType: [String]]
    public let allNames: [String]

    /// How many rows to look at before deciding what a column holds. Four
    /// hundred is plenty to catch a stray word in a column of numbers, and few
    /// enough to be instant.
    public static let sampleRows = 400
    /// The hard ceiling on what is read from the top, for a file with very wide
    /// rows. Two mebibytes is about 8,000 rows of a 209-column survey.
    public static let maxHeadBytes = 2 * 1024 * 1024
    /// How much to read from each of the two places further down the file.
    public static let probeBytes = 256 * 1024
    /// How many rows to keep from each of those.
    public static let probeRows = 200

    public enum Failure: LocalizedError {
        case unreadable(String)
        case empty
        case unsupported(String)

        public var errorDescription: String? {
            switch self {
            case .unreadable(let why): return "That file could not be read: \(why)"
            case .empty:               return "That file has no rows in it."
            case .unsupported(let ext):
                return "Handrail reads CSV, TSV and plain text files. This one is .\(ext) — "
                     + "open it in Excel and save a copy as CSV."
            }
        }
    }

    public init(url: URL) throws {
        let ext = url.pathExtension.lowercased()
        guard ["csv", "tsv", "txt", ""].contains(ext) else { throw Failure.unsupported(ext) }

        let head: Head
        do {
            head = try DataFile.readHead(url, wantRows: DataFile.sampleRows + 1,
                                         maxBytes: DataFile.maxHeadBytes)
        } catch {
            throw Failure.unreadable(error.localizedDescription)
        }
        guard !head.bytes.isEmpty else { throw Failure.empty }

        let delimiter: UInt8 = ext == "tsv" ? 0x09 : DataFile.sniffDelimiter(head.bytes)
        var rows = CSV.parse(head.bytes, delimiter: delimiter,
                             limit: DataFile.sampleRows + 1)
        // If the read stopped mid-file, the last row may be half a row.
        if head.truncated, rows.count > 1 { rows.removeLast() }
        guard let header = rows.first, !header.isEmpty else { throw Failure.empty }
        rows.removeFirst()
        let headRows = rows.count

        // Rows from further down, when there are any.
        //
        // The top of a file is not a sample of it. A real NHAMCS export has LOV
        // empty for its first 427 rows and filled for 71% of the file: reading
        // only the top said "100% empty" — wrong — and typed a number column as
        // text, which would have produced filter(LOV == "174") and silently kept
        // nothing. Two more small reads, from the middle and near the end, cost
        // half a megabyte and catch it.
        var probed = 0
        var probeBytes = 0
        // Any file with unread bytes left gets probed, not just a huge one: the
        // gate used to be "bigger than the head ceiling", which meant a 600 KB
        // file with a late-filling column still got the wrong answer.
        if head.truncated, let size = DataFile.fileSize(url), size > head.bytes.count {
            for fraction in [0.45, 0.85] {
                // Never behind what was already read — that would just re-read
                // the same rows and learn nothing.
                let offset = max(head.bytes.count, Int(Double(size) * fraction))
                guard offset < size else { continue }
                guard let chunk = try? DataFile.readProbe(url, at: offset,
                                                          maxBytes: DataFile.probeBytes),
                      !chunk.isEmpty else { continue }
                probeBytes += chunk.count
                // Only rows with exactly the right number of fields are kept: a
                // chunk can start or end inside a quoted field, and a row that
                // came out the wrong width is a row that was read wrong.
                let extra = CSV.parse(chunk, delimiter: delimiter, limit: DataFile.probeRows)
                    .filter { $0.count == header.count }
                rows.append(contentsOf: extra)
                probed += extra.count
            }
        }

        // A file with no header row gives columns named after its first row of
        // data. That is exactly what R does too, so it is left alone rather than
        // guessed at — and the preview makes it obvious when it happened.
        let names = DataFile.deduplicate(header)
        var cols: [Column] = []
        cols.reserveCapacity(names.count)
        for (i, name) in names.enumerated() {
            var missing = 0
            var example = ""
            var forType: [String] = []
            var marker: String?
            var markerCount = 0
            var nonMarker: [String] = []
            forType.reserveCapacity(min(rows.count, 200))
            for row in rows {
                let v = i < row.count ? row[i] : ""
                if DataFile.isBlank(v) { missing += 1; continue }
                let trimmed = v.trimmingCharacters(in: .whitespaces)
                if DataFile.sentinels.contains(trimmed) {
                    markerCount += 1
                    if marker == nil { marker = trimmed }
                } else {
                    if example.isEmpty { example = v }
                    if nonMarker.count < 200 { nonMarker.append(trimmed) }
                }
                if forType.count < 200 { forType.append(v) }
            }
            // The type is what R will see, sentinel and all — say otherwise and
            // the app would offer `LOV > 100` on a column R holds as text, which
            // compares strings and is quietly wrong.
            let type = DataFile.sniffType(forType)
            let numericUnderneath = markerCount > 0 && !nonMarker.isEmpty
                && DataFile.sniffType(nonMarker) == .number
            cols.append(Column(name: name, type: type,
                               missing: missing, sampled: rows.count,
                               example: example.isEmpty ? (marker ?? "") : example,
                               sentinel: marker, sentinelCount: markerCount,
                               numericApartFromSentinel: numericUnderneath))
        }

        self.url = url
        self.columns = cols
        self.preview = Array(rows.prefix(12))
        self.sampleRows = rows
        self.delimiter = delimiter
        self.sampledRows = headRows
        self.probedRows = probed
        self.bytesRead = head.bytes.count + probeBytes
        self.truncated = head.truncated
        self.types = Dictionary(uniqueKeysWithValues: cols.map { ($0.name, $0.type) })
        self.allNames = cols.map(\.name)
        var byType: [ColumnType: [String]] = [:]
        for c in cols { byType[c.type, default: []].append(c.name) }
        self.namesByType = byType
    }

    // MARK: - reading only the top

    struct Head {
        let bytes: [UInt8]
        let truncated: Bool
    }

    /// Reads from the start of the file until there are enough line breaks or
    /// the ceiling is hit, then trims back to the last complete line.
    ///
    /// Trimming at a newline is always a safe place to cut a UTF-8 stream: 0x0A
    /// never appears inside a multi-byte sequence, so the bytes kept are always
    /// valid text.
    static func readHead(_ url: URL, wantRows: Int, maxBytes: Int) throws -> Head {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }

        var bytes: [UInt8] = []
        bytes.reserveCapacity(min(maxBytes, 256 * 1024))
        var newlines = 0
        var reachedEnd = false

        var stop = false
        while !stop && bytes.count < maxBytes {
            // Same reason as in ColumnValues: FileHandle's Data is autoreleased.
            try autoreleasepool {
                let want = min(256 * 1024, maxBytes - bytes.count)
                guard let chunk = try handle.read(upToCount: want), !chunk.isEmpty else {
                    reachedEnd = true
                    stop = true
                    return
                }
                for b in chunk where b == 0x0A { newlines += 1 }
                bytes.append(contentsOf: chunk)
                if newlines >= wantRows { stop = true }
            }
        }

        // Whether there is more file after what was read.
        let more = !reachedEnd && (fileSize(url).map { bytes.count < $0 } ?? true)

        if more, let lastBreak = bytes.lastIndex(of: 0x0A) {
            bytes.removeSubrange(bytes.index(after: lastBreak)...)
        }
        // A file whose last line has no newline is complete, not truncated.
        return Head(bytes: bytes, truncated: more)
    }

    static func fileSize(_ url: URL) -> Int? {
        (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int
    }

    /// A chunk from the middle of the file, starting at the first whole line
    /// after `offset`.
    ///
    /// Seeking into a CSV lands mid-row, so the first partial line is dropped
    /// and the last one is too. Everything between them is complete — and rows
    /// are checked for the right field count afterwards, because a chunk can
    /// still begin inside a quoted field that happens to contain newlines.
    static func readProbe(_ url: URL, at offset: Int, maxBytes: Int) throws -> [UInt8] {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        try handle.seek(toOffset: UInt64(offset))
        guard let data = try handle.read(upToCount: maxBytes), !data.isEmpty else { return [] }

        var bytes = [UInt8](data)
        guard let firstBreak = bytes.firstIndex(of: 0x0A) else { return [] }
        bytes.removeSubrange(...firstBreak)
        if let lastBreak = bytes.lastIndex(of: 0x0A) {
            bytes.removeSubrange(bytes.index(after: lastBreak)...)
        } else {
            return []   // no complete line in this chunk
        }
        return bytes
    }

    /// Which character separates the fields, decided from the first line.
    static func sniffDelimiter(_ bytes: [UInt8]) -> UInt8 {
        var commas = 0, tabs = 0, semis = 0
        for b in bytes {
            if b == 0x0A || b == 0x0D { break }
            switch b {
            case 0x2C: commas += 1
            case 0x09: tabs += 1
            case 0x3B: semis += 1
            default: break
            }
        }
        if tabs > commas && tabs > semis { return 0x09 }
        if semis > commas && semis > tabs { return 0x3B }
        return 0x2C
    }

    /// Missing, as R will see it.
    ///
    /// Only "" and "NA", because that is exactly what `read.csv` treats as
    /// missing by default — and the script this app writes calls `read.csv`.
    /// Counting "N/A" or "NULL" as missing here made the app describe a column
    /// one way while the script it wrote read it another, which is the kind of
    /// disagreement nobody catches until the numbers are wrong.
    public static func isBlank(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.isEmpty || t == "NA"
    }

    /// Values that conventionally mean "no data" but that R reads as ordinary
    /// text. Their presence is what makes a column of numbers arrive as a column
    /// of words.
    public static let sentinels: Set<String> = [
        "Blank", "blank", "BLANK", "Unknown", "unknown", "UNKNOWN",
        "N/A", "n/a", "NULL", "null", "None", "none", "Missing", "missing",
        ".", "-", "--", "?", "Not Applicable", "not applicable", "Refused",
        "Not Recorded", "Not recorded"
    ]

    /// What a column holds, decided by what parses rather than by the header.
    /// Every value has to agree: one stray word in a column of numbers means the
    /// column is text, which is exactly the trap "Treat as numbers" exists for.
    public static func sniffType(_ values: [String]) -> ColumnType {
        guard !values.isEmpty else { return .text }
        var allBool = true, allNumber = true, allDate = true
        for v in values {
            if allBool, !["TRUE", "FALSE", "T", "F", "true", "false"].contains(v) { allBool = false }
            if allNumber, Double(v.trimmingCharacters(in: .whitespaces)) == nil { allNumber = false }
            if allDate, !looksLikeDate(v) { allDate = false }
            if !allBool && !allNumber && !allDate { return .text }
        }
        if allBool { return .logical }
        if allNumber { return .number }
        if allDate { return .date }
        return .text
    }

    /// Dates, without a regular expression: this runs once per value per column,
    /// and NSRegularExpression on 200 × 209 values is measurable on its own.
    static func looksLikeDate(_ s: String) -> Bool {
        let u = Array(s.trimmingCharacters(in: .whitespaces).utf8)
        func digits(_ range: Range<Int>) -> Bool {
            range.allSatisfy { $0 < u.count && u[$0] >= 0x30 && u[$0] <= 0x39 }
        }
        func sep(_ i: Int) -> Bool { i < u.count && (u[i] == 0x2D || u[i] == 0x2F) }

        // 2024-03-15 and 2024/3/5
        if u.count >= 8, u.count <= 10, digits(0..<4), sep(4) {
            let rest = u[5...].split(separator: u[4], omittingEmptySubsequences: false)
            return rest.count == 2 && rest.allSatisfy {
                (1...2).contains($0.count) && $0.allSatisfy { $0 >= 0x30 && $0 <= 0x39 }
            }
        }
        // 15/03/2024 and 3-5-2024
        if u.count >= 8, u.count <= 10, let firstSep = u.firstIndex(where: { $0 == 0x2F || $0 == 0x2D }),
           firstSep <= 2 {
            let parts = u.split(separator: u[firstSep], omittingEmptySubsequences: false)
            return parts.count == 3 && parts[2].count == 4
                && parts.allSatisfy { $0.allSatisfy { $0 >= 0x30 && $0 <= 0x39 } }
        }
        return false
    }

    /// R renames a repeated column to `a.1`, so the app shows the same names R
    /// will, rather than two dropdown entries that look identical.
    ///
    /// The names are also the identity of every row in the column list, and
    /// SwiftUI aborts the process — not the view, the process — when a ForEach
    /// sees the same id twice. So the result is checked to be unique rather than
    /// assumed to be: a header of `a, a, a.1` renames the second `a` to `a.1`
    /// and collides with the third column, which a counter alone would miss.
    public static func deduplicate(_ names: [String]) -> [String] {
        var used = Set<String>()
        var out: [String] = []
        out.reserveCapacity(names.count)
        for raw in names {
            let base = raw.trimmingCharacters(in: .whitespaces)
            var name = base.isEmpty ? "X" : base
            if used.contains(name) {
                var n = 1
                while used.contains("\(name).\(n)") { n += 1 }
                name = "\(name).\(n)"
            }
            used.insert(name)
            out.append(name)
        }
        return out
    }
}

/// A CSV reader that handles what a real spreadsheet export contains — quoted
/// fields, delimiters and newlines inside them, doubled quotes — and does it on
/// UTF-8 bytes.
///
/// Bytes, not Characters, on purpose: splitting on `,` and `\n` needs no Unicode
/// segmentation, and asking for it on every step is what made the first version
/// take minutes. Only the finished fields are decoded into `String`.
public enum CSV {
    public static func parse(_ bytes: [UInt8], delimiter: UInt8 = 0x2C,
                             limit: Int = Int.max) -> [[String]] {
        var rows: [[String]] = []
        var row: [String] = []
        var field: [UInt8] = []
        field.reserveCapacity(32)
        var inQuotes = false
        var i = 0
        let n = bytes.count

        func endField() {
            row.append(String(decoding: field, as: UTF8.self))
            field.removeAll(keepingCapacity: true)
        }
        func endRow() {
            endField()
            // A trailing newline must not produce a row of one empty string.
            if !(row.count == 1 && row[0].isEmpty) { rows.append(row) }
            row = []
            row.reserveCapacity(rows.first?.count ?? 8)
        }

        while i < n {
            let b = bytes[i]
            if inQuotes {
                if b == 0x22 {
                    if i + 1 < n && bytes[i + 1] == 0x22 { field.append(0x22); i += 2; continue }
                    inQuotes = false
                } else {
                    field.append(b)
                }
            } else {
                switch b {
                case 0x22: inQuotes = true
                case delimiter: endField()
                case 0x0A:
                    endRow()
                    if rows.count >= limit { return rows }
                case 0x0D: break   // \r\n is one break; a lone \r is rare enough to ignore
                default: field.append(b)
                }
            }
            i += 1
        }
        if !field.isEmpty || !row.isEmpty { endRow() }
        return rows
    }

    /// Convenience for tests and small strings.
    public static func parse(_ text: String, delimiter: Character = ",",
                             limit: Int = Int.max) -> [[String]] {
        parse(Array(text.utf8), delimiter: Array(String(delimiter).utf8)[0], limit: limit)
    }
}
