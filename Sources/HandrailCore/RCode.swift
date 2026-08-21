import Foundation

/// What a column holds, as far as we can tell by looking at it.
public enum ColumnType: String, Codable, Sendable {
    case number, text, date, logical

    public var label: String {
        switch self {
        case .number:  return "Number"
        case .text:    return "Text"
        case .date:    return "Date"
        case .logical: return "Yes/no"
        }
    }
}

/// Turning an action into R.
///
/// This is the whole point of the app, so two rules govern it.
///
/// **One action, one dplyr verb.** Never two verbs stitched together, never a
/// helper only this app knows about. The line that lands in the script has to be
/// a line he could have written, and could look up.
///
/// **Nothing is hidden.** `na.rm = TRUE` is written out rather than relied on,
/// because the next thing he reads about `mean()` will say the default is FALSE
/// and he needs the script in front of him to agree with the book.
public enum RCode {

    // MARK: - names and values

    private static let reserved: Set<String> = [
        "if", "else", "for", "while", "repeat", "function", "break", "next",
        "TRUE", "FALSE", "NULL", "NA", "Inf", "NaN", "in"
    ]

    /// A column name as R must see it. `income` stays bare; `total income` and
    /// `2024` get backticks, because R would otherwise read them as two symbols
    /// or as a number.
    public static func name(_ raw: String) -> String {
        let ok = raw.range(of: "^[a-zA-Z.][a-zA-Z0-9._]*$", options: .regularExpression) != nil
        return (ok && !reserved.contains(raw)) ? raw : "`\(raw)`"
    }

    /// A typed-in value as R source. Quoted for text, bare for numbers — the
    /// commonest way a generated line fails is `filter(age == "65")`, which
    /// compares a number to a string and silently keeps nothing.
    public static func literal(_ raw: String, _ type: ColumnType) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        switch type {
        case .number:
            if Double(trimmed) != nil { return trimmed }
            return quote(trimmed)
        case .logical:
            let up = trimmed.uppercased()
            if ["TRUE", "T", "YES"].contains(up) { return "TRUE" }
            if ["FALSE", "F", "NO"].contains(up) { return "FALSE" }
            return quote(trimmed)
        case .text, .date:
            return quote(trimmed)
        }
    }

    /// A string as R source, with the characters R would otherwise read as
    /// instructions escaped.
    public static func quote(_ raw: String) -> String {
        var out = ""
        for ch in raw {
            switch ch {
            case "\\": out += "\\\\"
            case "\"": out += "\\\""
            case "\n": out += "\\n"
            case "\t": out += "\\t"
            default:   out.append(ch)
            }
        }
        return "\"\(out)\""
    }

    /// A name safe to use as a new column: R will accept anything in backticks,
    /// but a name he has to backtick forever is a name that will trip him up.
    public static func safeNewName(_ raw: String, fallback: String) -> String {
        var cleaned = raw.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: "[^A-Za-z0-9._]+", with: "_", options: .regularExpression)
        cleaned = cleaned.replacingOccurrences(of: "^[^A-Za-z.]+", with: "", options: .regularExpression)
        cleaned = cleaned.replacingOccurrences(of: "_+$", with: "", options: .regularExpression)
        return cleaned.isEmpty ? fallback : cleaned
    }

    /// The variable a file is read into: named after the file, lowercased.
    ///
    /// Not `data`. Two reasons, and the second is the one that bites. `data()`
    /// is a function in base R, so `data <- read.csv(...)` shadows it — a habit
    /// worth not teaching. And Handrail appends to the same script over days: a
    /// second file read into `data` would silently overwrite the first, and the
    /// steps above would still look as though they applied.
    public static func frameName(for file: URL) -> String {
        let stem = file.deletingPathExtension().lastPathComponent.lowercased()
        var name = safeNewName(stem, fallback: "my_data")
        if name.count > 28 { name = String(name.prefix(28)) }
        // A file called data.csv or table.csv would otherwise produce exactly
        // the name this function exists to avoid.
        return shadowsBaseR.contains(name) ? name + "_df" : name
    }

    /// Names that would shadow something in base R common enough to matter. Not
    /// a complete list of base R — an exhaustive one would rename half the files
    /// anyone owns — just the ones that would bite.
    private static let shadowsBaseR: Set<String> = [
        "data", "table", "matrix", "list", "file", "df", "mean", "sum", "max",
        "min", "range", "rev", "sort", "order", "length", "names", "rep", "seq",
        "sample", "summary", "format", "date", "class", "levels", "factor", "t", "c"
    ]

    // MARK: - the generator

    /// The dplyr call for one action, without the pipe in front of it.
    /// Returns nil when the action is not filled in enough to make legal R —
    /// half an action must never reach the script.
    /// Whether a field the user was meant to fill in has been.
    static func has(_ s: String) -> Bool { !s.trimmingCharacters(in: .whitespaces).isEmpty }

    /// group_by / summarise / arrange, as one chain.
    ///
    /// Lives here rather than inside the `.summarise` case because saving a
    /// summary table to a file needs exactly the same chain — and two copies of
    /// it would drift the moment a statistic is added to one of them.
    static func summariseChain(_ a: Action) -> String? {
        guard !a.groups.isEmpty, !a.statistics.isEmpty else { return nil }
        let needsTarget = a.statistics.contains { $0 != .count }
        if needsTarget && !has(a.target) { return nil }
        let groups = a.groups.map(name).joined(separator: ", ")
        var parts: [String] = []
        for stat in a.statistics {
            if stat == .count {
                parts.append("count = n()")
            } else {
                let label = safeNewName("\(stat.rawValue)_\(a.target)",
                                        fallback: stat.rawValue)
                parts.append("\(name(label)) = \(stat.rFunction)(\(name(a.target)), na.rm = TRUE)")
            }
        }
        let body = parts.joined(separator: ",\n            ")
        // arrange() is not decoration: it makes the row order the same every
        // time the script runs, wherever it runs.
        return "group_by(\(groups)) |>\n  summarise(\(body), .groups = \"drop\") |>\n  arrange(\(groups))"
    }

    public static func call(for a: Action, types: [String: ColumnType],
                            frame: String = "data") -> String? {
        func type(_ col: String) -> ColumnType { types[col] ?? .text }

        switch a.kind {

        case .filter:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            switch a.comparison {
            case .isMissing:  return "filter(is.na(\(col)))"
            case .notMissing: return "filter(!is.na(\(col)))"
            case .isIn:
                guard !a.values.isEmpty else { return nil }
                let vals = a.values.map { literal($0, type(a.column)) }.joined(separator: ", ")
                return "filter(\(col) %in% c(\(vals)))"
            case .between:
                guard has(a.value), has(a.value2) else { return nil }
                return "filter(\(col) >= \(literal(a.value, type(a.column))), "
                     + "\(col) <= \(literal(a.value2, type(a.column))))"
            case .contains:
                guard has(a.value) else { return nil }
                // fixed = TRUE is spelled out: without it the value is read as a
                // regular expression, so a search for "1.5" also matches "125".
                return "filter(grepl(\(quote(a.value)), \(col), fixed = TRUE))"
            default:
                guard has(a.value) else { return nil }
                return "filter(\(col) \(a.comparison.op) \(literal(a.value, type(a.column))))"
            }

        case .sort:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            return a.descending ? "arrange(desc(\(col)))" : "arrange(\(col))"

        case .select:
            guard !a.columns.isEmpty else { return nil }
            return "select(\(a.columns.map(name).joined(separator: ", ")))"

        case .mutate:
            guard has(a.expression) else { return nil }
            let new = safeNewName(a.newName, fallback: "new_column")
            return "mutate(\(name(new)) = \(a.expression.trimmingCharacters(in: .whitespaces)))"

        case .rename:
            guard has(a.column), has(a.newName) else { return nil }
            let new = safeNewName(a.newName, fallback: "renamed")
            return "rename(\(name(new)) = \(name(a.column)))"

        case .summarise:
            return summariseChain(a)

        case .head:
            guard a.count > 0 else { return nil }
            return "head(\(a.count))"

        case .distinct:
            return a.columns.isEmpty
                ? "distinct()"
                : "distinct(\(a.columns.map(name).joined(separator: ", ")), .keep_all = TRUE)"

        case .dropNA:
            guard !a.columns.isEmpty else { return nil }
            let tests = a.columns.map { "!is.na(\(name($0)))" }.joined(separator: ", ")
            return "filter(\(tests))"

        case .fillNA:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            let replacement: String
            switch a.fill {
            case .mean:   replacement = "mean(\(col), na.rm = TRUE)"
            case .median: replacement = "median(\(col), na.rm = TRUE)"
            case .zero:   replacement = "0"
            case .value:
                guard has(a.value) else { return nil }
                replacement = literal(a.value, type(a.column))
            }
            return "mutate(\(col) = ifelse(is.na(\(col)), \(replacement), \(col)))"

        case .band:
            guard has(a.column), a.bands >= 2 else { return nil }
            let col = name(a.column)
            let new = name(safeNewName(a.newName, fallback: "band"))
            if a.equalWidth {
                return "mutate(\(new) = cut(\(col), breaks = \(a.bands)))"
            }
            // Equal-sized bands need the cut points read off the data, so the
            // quantiles are spelled out rather than hidden in a helper.
            return "mutate(\(new) = cut(\(col),\n"
                 + "    breaks = quantile(\(col), probs = seq(0, 1, length.out = \(a.bands + 1)), na.rm = TRUE),\n"
                 + "    include.lowest = TRUE))"

        case .round:
            guard !a.columns.isEmpty else { return nil }
            let cols = a.columns.map(name).joined(separator: ", ")
            return "mutate(across(c(\(cols)), \\(x) round(x, \(a.digits))))"

        case .toNumber:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            // The gsub is what makes "$1,200.50" a number. It is written out so
            // that when a value goes missing, the reason is on the screen.
            return "mutate(\(col) = as.numeric(gsub(\"[^0-9.-]\", \"\", \(col))))"

        case .toDate:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            return "mutate(\(col) = as.Date(\(col), format = \(quote(a.dateFormat))))"

        case .toText:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            return "mutate(\(col) = as.character(\(col)))"

        case .markMissing:
            guard has(a.column), has(a.value) else { return nil }
            // na_if is dplyr's own verb for exactly this, so the line is one he
            // can look up. What counts as missing stays his decision, written
            // down in the script where a reader can see it.
            return "mutate(\(name(a.column)) = na_if(\(name(a.column)), \(quote(a.value))))"

        // ---- finishing steps: statements, not pipeline links ---------------

        case .saveCSV:
            guard has(a.outputPath) else { return nil }
            // row.names = FALSE is written out because R's default is TRUE, and
            // a stray unnamed first column of 1..n in a file you send someone is
            // the classic way a CSV arrives looking wrong.
            let encoding = a.forExcel ? ", fileEncoding = \"UTF-8-BOM\"" : ""
            return "write.csv(\(frame), \(quote(a.outputPath)), row.names = FALSE\(encoding))"

        case .saveRDS:
            guard has(a.outputPath) else { return nil }
            return "saveRDS(\(frame), \(quote(a.outputPath)))"

        case .saveExcel:
            guard has(a.outputPath) else { return nil }
            // writexl:: rather than library(writexl), so the line says where the
            // function came from and the preamble stays about the data.
            return "writexl::write_xlsx(\(frame), \(quote(a.outputPath)))"

        case .saveDelimited:
            guard has(a.outputPath) else { return nil }
            // sep is the whole point of this step, so it is the visible argument.
            // quote = FALSE would be tidier to read and silently corrupts any
            // value containing the separator, so it stays on.
            return "write.table(\(frame), \(quote(a.outputPath)),\n"
                 + "            sep = \"\(a.textFormat.separator)\", row.names = FALSE, na = \"\")"

        case .saveForStats:
            guard has(a.outputPath) else { return nil }
            return "haven::\(a.statsFormat.rFunction)(\(frame), \(quote(a.outputPath)))"

        case .saveSummary:
            guard has(a.outputPath), let chain = summariseChain(a) else { return nil }
            // The chain is indented one level further because it sits inside the
            // write.csv call rather than at the top of a pipeline. Two spaces,
            // not four: the chain's own lines already carry two, and adding four
            // steps group_by and summarise out of line with each other.
            let inner = chain.split(separator: "\n", omittingEmptySubsequences: false)
                             .joined(separator: "\n  ")
            return "write.csv(\n  \(frame) |>\n    \(inner),\n"
                 + "  \(quote(a.outputPath)), row.names = FALSE)"

        case .peek:
            return "print(head(\(frame), \(max(1, a.count))))"

        case .viewIt:
            return "View(\(frame))"

        case .countRows:
            return "cat(nrow(\(frame)), \"rows\\n\")"

        case .glimpse:
            return "glimpse(\(frame))"

        case .summaryOf:
            if a.columns.isEmpty { return "summary(\(frame))" }
            let cols = a.columns.map(name).joined(separator: ", ")
            return "summary(select(\(frame), \(cols)))"

        case .crossTab:
            guard has(a.column), has(a.target) else { return nil }
            // useNA = "ifany" because a two-way table that silently drops the
            // missing rows is the quiet wrong answer this app exists to stop.
            return "table(\(frame)$\(name(a.column)), \(frame)$\(name(a.target)),\n"
                 + "      dnn = c(\(quote(a.column)), \(quote(a.target))), useNA = \"ifany\")"

        case .datePart:
            guard has(a.column) else { return nil }
            let col = name(a.column)
            let new = name(safeNewName(a.newName, fallback: a.datePiece.rawValue))
            switch a.datePiece {
            case .year:      return "mutate(\(new) = as.integer(format(\(col), \"%Y\")))"
            case .month:     return "mutate(\(new) = as.integer(format(\(col), \"%m\")))"
            case .monthName: return "mutate(\(new) = format(\(col), \"%B\"))"
            case .day:       return "mutate(\(new) = as.integer(format(\(col), \"%d\")))"
            case .weekday:   return "mutate(\(new) = weekdays(\(col)))"
            }
        }
    }

    /// The whole block that gets appended: a plain-English comment, then the
    /// pipe. The comment is not decoration — it is the sentence he chose in the
    /// app, sitting above the R it produced, which is how one turns into the
    /// other in his head.
    public static func block(for a: Action, types: [String: ColumnType],
                             frame: String = "data") -> String? {
        guard let call = call(for: a, types: types, frame: frame) else { return nil }
        // A finishing step stands on its own. Piping it would assign the return
        // value of write.csv — which is NULL — over the data.
        if a.kind.isFinishing { return "# \(sentence(for: a))\n\(call)\n" }
        return "# \(sentence(for: a))\n\(frame) <- \(frame) |>\n  \(call)\n"
    }

    /// Which packages the generated line needs. Kept honest rather than
    /// assumed: the preamble writes exactly the library() lines the script uses.
    public static func packages(for actions: [Action]) -> [String] {
        actions.isEmpty ? [] : ["dplyr"]
    }

    /// A package the generated line needs but does not attach — the `pkg::fn`
    /// ones. Named so the app can say "this needs writexl" before he runs a
    /// script that stops with "there is no package called".
    public static func requiredPackage(for a: Action) -> String? {
        switch a.kind {
        case .saveExcel:    return "writexl"
        case .saveForStats: return "haven"
        default:            return nil
        }
    }
}
