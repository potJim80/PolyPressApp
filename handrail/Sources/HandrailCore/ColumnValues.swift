import Foundation

/// What is actually in a column.
///
/// You cannot write `filter(region == "North")` without knowing that "North" is
/// what the file says — not "north", not "North ", not "NORTH". Getting that
/// wrong produces legal R that quietly keeps no rows, which is the failure this
/// whole app exists to prevent.
///
/// Two answers, and the difference between them is stated rather than blurred:
/// the values in the rows already read, which is instant; and the values in the
/// whole file, which costs a pass over it and is only done when asked for.
public struct ValueTally: Sendable {
    public struct Entry: Sendable, Identifiable, Equatable {
        public var id: String { value }
        public let value: String
        public let count: Int
    }

    /// Most common first.
    public let entries: [Entry]
    public let distinct: Int
    public let rows: Int
    public let missing: Int
    /// True when this counted the whole file rather than the sample.
    public let complete: Bool
    /// True when there were more distinct values than it was willing to hold, so
    /// `distinct` is a floor and `entries` is not the whole story.
    public let tooMany: Bool

    public var present: Int { rows - missing }

    public func share(_ entry: Entry) -> Double {
        present == 0 ? 0 : Double(entry.count) / Double(present)
    }

    /// A column with thousands of distinct values is an identifier or a
    /// measurement, and listing it is not useful — saying so is.
    public var looksLikeAnIdentifier: Bool {
        present > 20 && Double(distinct) / Double(present) > 0.9
    }
}

public enum ColumnValues {

    /// The ceiling on distinct values held while counting. Past this the answer
    /// is "more than you want to look at", which is the useful answer anyway.
    public static let maxDistinct = 10_000

    /// Counted from rows already in hand. Instant, and honestly incomplete.
    public static func fromSample(_ file: DataFile, column: String) -> ValueTally {
        guard let index = file.columns.firstIndex(where: { $0.name == column }) else {
            return ValueTally(entries: [], distinct: 0, rows: 0, missing: 0,
                              complete: false, tooMany: false)
        }
        var counts: [String: Int] = [:]
        var missing = 0
        var rows = 0
        for row in file.sampleRows {
            rows += 1
            let v = index < row.count ? row[index] : ""
            if DataFile.isBlank(v) { missing += 1; continue }
            counts[v, default: 0] += 1
        }
        return tally(counts: counts, rows: rows, missing: missing,
                     complete: !file.truncated, tooMany: false)
    }

    /// Counted from every row in the file.
    ///
    /// Streams the file and keeps only the one column, so a 278 MB file costs
    /// about a second and a few kilobytes rather than being loaded.
    /// `shouldStop` lets the caller abandon it if the user moves on.
    ///
    /// The scanning lives in `RowStream` rather than here, so the viewer and
    /// this share one parser and one set of edge cases.
    public static func fromWholeFile(_ url: URL, column: String, in file: DataFile,
                                     shouldStop: (() -> Bool)? = nil) throws -> ValueTally {
        guard let index = file.columns.firstIndex(where: { $0.name == column }) else {
            return ValueTally(entries: [], distinct: 0, rows: 0, missing: 0,
                              complete: false, tooMany: false)
        }
        var counts: [String: Int] = [:]
        var missing = 0
        var tooMany = false

        let scan = try RowStream.read(url, delimiter: file.delimiter, wanted: [index],
                                      shouldStop: shouldStop) { fields in
            let value = fields[0]
            if DataFile.isBlank(value) {
                missing += 1
            } else if counts.count < maxDistinct || counts[value] != nil {
                counts[value, default: 0] += 1
            } else {
                // Past the ceiling the answer is "more than you want to look
                // at", which is the useful answer anyway.
                tooMany = true
            }
            return .carryOn
        }

        return tally(counts: counts, rows: scan.rows, missing: missing,
                     complete: scan.atEnd, tooMany: tooMany)
    }

    private static func tally(counts: [String: Int], rows: Int, missing: Int,
                              complete: Bool, tooMany: Bool) -> ValueTally {
        let entries = counts
            .map { ValueTally.Entry(value: $0.key, count: $0.value) }
            // Count first, then alphabetically, so the order is the same every
            // time rather than dictionary order.
            .sorted { $0.count == $1.count ? $0.value < $1.value : $0.count > $1.count }
        return ValueTally(entries: entries, distinct: entries.count, rows: rows,
                          missing: missing, complete: complete, tooMany: tooMany)
    }
}
