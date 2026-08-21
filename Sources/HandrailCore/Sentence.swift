import Foundation

extension RCode {
    /// The action as a sentence. It goes into the script as the comment above
    /// the code, and onto the screen as the description of what you have built.
    ///
    /// Deliberately the same string in both places: the comment in his script is
    /// the only part of this app that survives the app being closed, so it has
    /// to be the sentence he recognises.
    public static func sentence(for a: Action) -> String {
        func list(_ xs: [String]) -> String {
            switch xs.count {
            case 0:  return "?"
            case 1:  return xs[0]
            case 2:  return "\(xs[0]) and \(xs[1])"
            default: return xs.dropLast().joined(separator: ", ") + " and " + xs.last!
            }
        }
        let col = a.column.isEmpty ? "?" : a.column

        switch a.kind {
        case .filter:
            switch a.comparison {
            case .isMissing:  return "Keep only the rows where \(col) is missing"
            case .notMissing: return "Keep only the rows where \(col) is not missing"
            case .isIn:       return "Keep only the rows where \(col) is one of: \(list(a.values))"
            case .between:    return "Keep only the rows where \(col) is between \(a.value) and \(a.value2)"
            case .contains:   return "Keep only the rows where \(col) contains \"\(a.value)\""
            default:          return "Keep only the rows where \(col) \(a.comparison.label) \(a.value)"
            }
        case .sort:
            return a.descending ? "Sort by \(col), largest first"
                                : "Sort by \(col), smallest first"
        case .select:
            return "Keep only the columns \(list(a.columns))"
        case .mutate:
            let new = safeNewName(a.newName, fallback: "new_column")
            return "Add a column called \(new), worked out as \(a.expression)"
        case .rename:
            return "Rename \(col) to \(safeNewName(a.newName, fallback: "?"))"
        case .summarise:
            let stats = a.statistics.map { s -> String in
                s == .count ? "how many rows" : "\(s.label) of \(a.target)"
            }
            return "For each \(list(a.groups)), work out \(list(stats))"
        case .head:
            return "Keep only the first \(a.count) rows"
        case .distinct:
            return a.columns.isEmpty
                ? "Drop rows that repeat one above them"
                : "Drop rows that repeat one above them, judging by \(list(a.columns))"
        case .dropNA:
            return "Drop the rows with nothing in \(list(a.columns))"
        case .fillNA:
            return "Fill the gaps in \(col) with \(a.fill.label)"
        case .band:
            let new = safeNewName(a.newName, fallback: "band")
            return a.equalWidth
                ? "Sort \(col) into \(a.bands) bands of equal width, called \(new)"
                : "Sort \(col) into \(a.bands) bands of equal size, called \(new)"
        case .round:
            return "Round \(list(a.columns)) to \(a.digits) decimal place\(a.digits == 1 ? "" : "s")"
        case .toNumber:
            return "Treat \(col) as numbers"
        case .toDate:
            return "Treat \(col) as dates, written like \(a.dateFormat)"
        case .toText:
            return "Treat \(col) as text"
        case .markMissing:
            return "Treat \"\(a.value)\" in \(col) as missing"
        case .saveCSV:
            return "Save what you have as \(a.outputPath.isEmpty ? "a CSV" : a.outputPath)"
        case .saveRDS:
            return "Save what you have as \(a.outputPath.isEmpty ? "an R data file" : a.outputPath)"
        case .saveExcel:
            return "Save what you have as \(a.outputPath.isEmpty ? "an Excel file" : a.outputPath)"
        case .saveDelimited:
            let how = a.textFormat.label.lowercased()
            return "Save what you have as \(a.outputPath.isEmpty ? how : a.outputPath), \(how)"
        case .saveForStats:
            return "Save what you have for \(a.statsFormat.label), as "
                 + "\(a.outputPath.isEmpty ? "a file" : a.outputPath)"
        case .saveSummary:
            let by = a.groups.isEmpty ? "group" : list(a.groups)
            return "Save a summary by \(by) as \(a.outputPath.isEmpty ? "a CSV" : a.outputPath), "
                 + "leaving the data itself alone"
        case .peek:
            return "Show the first \(a.count) rows"
        case .viewIt:
            return "Open what you have in RStudio's viewer"
        case .countRows:
            return "Print how many rows are left"
        case .glimpse:
            return "List every column, its type and its first few values"
        case .summaryOf:
            return a.columns.isEmpty
                ? "Show the range, average and missing count of every column"
                : "Show the range, average and missing count of \(list(a.columns))"
        case .crossTab:
            return "Count every combination of \(col) and "
                 + "\(a.target.isEmpty ? "another column" : a.target)"
        case .datePart:
            let new = safeNewName(a.newName, fallback: a.datePiece.rawValue)
            return "Pull \(a.datePiece.label) out of \(col), into a column called \(new)"
        }
    }

    /// The warning worth showing beside an action before it is added. Not
    /// errors — these are the things that are legal R and still not what someone
    /// meant.
    public static func caution(for a: Action, types: [String: ColumnType]) -> String? {
        switch a.kind {
        case .filter:
            let t = types[a.column] ?? .text
            if t == .number, a.comparison.takesValue, a.comparison != .contains,
               !a.value.isEmpty, Double(a.value.trimmingCharacters(in: .whitespaces)) == nil {
                return "\(a.column) holds numbers, and \"\(a.value)\" is not one. "
                     + "R will compare text to numbers and quietly keep nothing."
            }
            if a.comparison == .contains, t == .number {
                return "\"contains\" looks inside text. On a number column it will "
                     + "not do what you expect."
            }
            return nil
        case .fillNA:
            return "Filling gaps invents data. If you report this, say that you did it."
        case .dropNA:
            return "This removes rows. Check how many you lose before you rely on it."
        case .toDate:
            return "Anything that does not match the format becomes missing, so look "
                 + "at the column afterwards."
        case .toNumber:
            return "Anything that is not a digit is stripped out, so check the result "
                 + "before trusting it."
        case .markMissing:
            return "This changes what counts as missing in your data. It is a decision "
                 + "about the data, so it goes into the script where a reader can see it."
        case .saveCSV:
            return "This writes a file, and overwrites one of that name without asking — "
                 + "R does, and so will this line every time the script is run."
        case .saveRDS:
            return "An .rds keeps every column type exactly as it is, but only R can "
                 + "open it. Use a CSV for anything you are sending to someone else."
        case .saveExcel:
            return "Needs the writexl package — install.packages(\"writexl\") once, if the "
                 + "script stops saying there is no such package. Excel also has a limit "
                 + "of about a million rows, and drops the rest without much of a fuss."
        case .saveDelimited:
            return a.textFormat.note + " Whoever opens it has to be told which separator "
                 + "it uses, because the file itself does not say."
        case .saveForStats:
            return "Needs the haven package — install.packages(\"haven\") once. Column names "
                 + "these programs cannot take are changed on the way out, so check the "
                 + "names in the file rather than assuming they survived."
        case .saveSummary:
            return "This writes the summary to a file and leaves your data as it is, so "
                 + "the steps after it still see every row."
        case .crossTab:
            return "Missing values are counted rather than dropped, so the table's total "
                 + "should match your row count. If it does not, something else is going on."
        case .summaryOf:
            return "summary() is a quick look, not a result to report — it rounds, and it "
                 + "treats a text column as a list of names rather than as categories."
        case .viewIt:
            return "View() opens a window in RStudio. It does nothing when the script is "
                 + "run outside RStudio, which is worth knowing if you send it to someone."
        case .band:
            return a.equalWidth
                ? "Equal width means equal value ranges, so some bands may hold very few rows."
                : "Equal size means the same number of rows per band, so the ranges will differ."
        case .head:
            return "This keeps whatever is at the top right now. Sort first, or the "
                 + "\"top\" is just the file's order."
        default:
            return nil
        }
    }
}
