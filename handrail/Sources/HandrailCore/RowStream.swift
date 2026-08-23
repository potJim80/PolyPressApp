import Foundation

/// Reading a delimited file without holding it.
///
/// One scanner, shared by everything that needs more than the rows already in
/// hand: counting a column's values across a whole file, and paging through
/// rows in the viewer. Having one copy is the point — the state here (a quoted
/// field straddling a 1 MB read boundary, a last line with no newline after it)
/// is exactly the sort of thing that gets fixed in one copy and left broken in
/// the other.
public enum RowStream {

    /// Read size. Large enough that the syscall cost disappears, small enough
    /// that the buffer stays in cache.
    public static let chunkBytes = 1 << 20

    /// What the caller wants after a row.
    public enum Step: Sendable { case carryOn, stop }

    /// Where a scan got to, so the next one can pick up rather than start over.
    public struct Result: Sendable {
        /// Data rows handed to `onRow`. Rows passed over by `skip` are not counted.
        public let rows: Int
        /// Rows seen at all, `skip` included. With `skip` 0 this equals `rows`.
        public let seen: Int
        /// True if it read to the end of the file, false if it was stopped.
        public let atEnd: Bool
        /// Byte offset just past the last complete row, for resuming.
        public let offset: UInt64
    }

    /// Streams `url`, building strings only for the fields named in `wanted`.
    ///
    /// `onRow` is handed a buffer that is **reused between rows** — read it, or
    /// copy it if you mean to keep it. Its positions correspond to `wanted` in
    /// ascending order, whatever order `wanted` arrived in. The header row is
    /// never passed on.
    ///
    /// Everything outside `wanted` is skipped at the byte level, never turned
    /// into a String. That is what makes counting one column of a 209-column
    /// file cost about the same as reading the bytes.
    ///
    /// - Parameters:
    ///   - from: byte offset to start at. Must be the first byte of a row.
    ///   - skipHeader: false when resuming from an offset past the header.
    ///   - skip: data rows to pass over before delivering any. Skipped rows cost
    ///     only the scan — no strings are built for them.
    @discardableResult
    public static func read(_ url: URL,
                            delimiter: UInt8,
                            wanted: [Int],
                            from startOffset: UInt64 = 0,
                            skipHeader: Bool = true,
                            skip: Int = 0,
                            limit: Int = .max,
                            shouldStop: (() -> Bool)? = nil,
                            onRow: (inout [String]) -> Step) throws -> Result {

        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        if startOffset > 0 { try handle.seek(toOffset: startOffset) }

        // Field position -> slot in the buffer, or -1 for "do not build this".
        let order = Array(Set(wanted)).sorted()
        let maxWanted = order.last ?? -1
        var slot = [Int](repeating: -1, count: max(0, maxWanted + 1))
        for (i, w) in order.enumerated() where w >= 0 { slot[w] = i }

        var buffer = [String](repeating: "", count: order.count)
        var field: [UInt8] = []
        field.reserveCapacity(64)

        var fieldIndex = 0
        var inQuotes = false
        var quoteWasLast = false
        var isHeader = skipHeader
        var seen = 0            // data rows passed, skipped ones included
        var delivered = 0
        var stopped = false
        var consumed = startOffset
        var offset = startOffset

        // Recomputed whenever the row or the field changes, so the inner byte
        // loop only has to look at one Int.
        var keepingRow = !isHeader && seen >= skip
        var target = -1
        func retarget() {
            target = (keepingRow && fieldIndex <= maxWanted) ? slot[fieldIndex] : -1
        }
        retarget()

        func finishField() {
            if target >= 0 { buffer[target] = String(decoding: field, as: UTF8.self) }
            field.removeAll(keepingCapacity: true)
            fieldIndex += 1
            retarget()
        }

        func finishRow() {
            finishField()
            if isHeader {
                isHeader = false
            } else {
                seen += 1
                if seen > skip {
                    delivered += 1
                    if onRow(&buffer) == .stop { stopped = true }
                    if delivered >= limit { stopped = true }
                }
            }
            offset = consumed
            fieldIndex = 0
            keepingRow = !isHeader && seen >= skip
            retarget()
            // Short rows must not show the previous row's values through the
            // gap, so the buffer is cleared rather than overwritten in place.
            if keepingRow { for i in buffer.indices { buffer[i] = "" } }
        }

        var done = false
        while !done && !stopped {
            if shouldStop?() == true { break }
            // The autorelease pool is load-bearing, not tidiness. FileHandle
            // hands back an autoreleased Data per read, and without a pool
            // inside the loop a 278 MB file leaves 278 one-megabyte buffers
            // alive until this returns — measured at 304 MB peak for counting a
            // single column, against 20 MB with the pool.
            try autoreleasepool {
                guard let chunk = try handle.read(upToCount: chunkBytes), !chunk.isEmpty else {
                    done = true
                    return
                }
                for b in chunk {
                    consumed += 1
                    if inQuotes {
                        if quoteWasLast {
                            quoteWasLast = false
                            if b == 0x22 { if target >= 0 { field.append(0x22) }; continue }
                            inQuotes = false
                            // fall through: this byte is an ordinary one
                        } else if b == 0x22 {
                            quoteWasLast = true
                            continue
                        } else {
                            if target >= 0 { field.append(b) }
                            continue
                        }
                    }
                    switch b {
                    case 0x22: inQuotes = true
                    case delimiter: finishField()
                    case 0x0A: finishRow(); if stopped { return }
                    case 0x0D: break
                    default: if target >= 0 { field.append(b) }
                    }
                }
            }
        }
        // A last line with no newline after it is still a row.
        if !stopped && (!field.isEmpty || fieldIndex > 0) { finishRow() }

        return Result(rows: delivered, seen: seen,
                      atEnd: done && shouldStop?() != true, offset: offset)
    }
}
