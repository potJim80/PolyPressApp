import Foundation

/// A block of rows read straight out of the file, for looking at.
public struct DataPage: Sendable {
    /// The columns actually built, in the order they should be shown.
    public let columns: [String]
    public let rows: [[String]]
    /// 1-based number of the first row here, so the viewer can label them.
    public let firstRow: Int
    /// True when this page ran to the end of the file.
    public let atEnd: Bool
    /// Where to resume, handed back to the next call.
    public let offset: UInt64
}

/// Paging through a file's rows.
///
/// Two things keep this light on a file the size of a hospital dataset. It only
/// builds strings for the columns on screen — on a 209-column file that is a
/// twentieth of the work. And it resumes from a byte offset instead of counting
/// from the top again, so the tenth page costs the same as the first.
public enum DataPager {

    /// Rows per fetch. Enough to scroll through, small enough to arrive at once.
    public static let pageRows = 500

    /// The most rows the viewer will hold before it stops offering more. Beyond
    /// this the honest answer is "use R" — a viewer is for looking, not loading.
    public static let maxHeld = 20_000

    public static func page(_ url: URL, in file: DataFile, columns: [String],
                            from offset: UInt64 = 0, firstRow: Int = 1,
                            limit: Int = pageRows,
                            shouldStop: (() -> Bool)? = nil) throws -> DataPage {

        // Positions in the file for the columns asked for. RowStream hands back
        // fields in ascending position, so the shown order is rebuilt after.
        let positions = columns.compactMap { name in
            file.columns.firstIndex(where: { $0.name == name })
        }
        let ascending = Array(Set(positions)).sorted()
        let slotOf = Dictionary(uniqueKeysWithValues: ascending.enumerated().map { ($1, $0) })
        let pick = positions.map { slotOf[$0] ?? 0 }

        var out: [[String]] = []
        out.reserveCapacity(limit)

        let scan = try RowStream.read(url, delimiter: file.delimiter, wanted: ascending,
                                      from: offset, skipHeader: offset == 0,
                                      limit: limit, shouldStop: shouldStop) { fields in
            // Copying here is the point of the reused buffer: the cost is paid
            // once, for the rows actually kept.
            out.append(pick.map { fields[$0] })
            return .carryOn
        }

        return DataPage(columns: columns, rows: out, firstRow: firstRow,
                        atEnd: scan.atEnd, offset: scan.offset)
    }
}
