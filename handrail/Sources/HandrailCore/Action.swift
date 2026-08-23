import Foundation

/// What one step of an analysis is, before it becomes R.
///
/// The vocabulary is deliberately the same one the archived Shiny app settled
/// on, because that one was tested against real dplyr output for months. One
/// action maps to exactly one dplyr verb — that is what makes the generated
/// line something you can look up, ask about, and eventually write yourself.
public enum ActionKind: String, CaseIterable, Codable, Sendable {
    case filter, sort, select, mutate, rename, summarise
    case head, distinct, dropNA, fillNA, band, round
    case toNumber, toDate, toText, datePart, markMissing
    case saveCSV, saveRDS, saveExcel, saveDelimited, saveForStats, saveSummary
    case peek, viewIt, countRows, glimpse, summaryOf, crossTab
    case countValues, describeNumber, missingReport, duplicateReport

    /// What it is called on screen. Verb first, so the list reads as things to
    /// do rather than as a glossary.
    public var title: String {
        switch self {
        case .filter:    return "Keep only some rows"
        case .sort:      return "Sort the rows"
        case .select:    return "Keep only some columns"
        case .mutate:    return "Add a column worked out from others"
        case .rename:    return "Rename a column"
        case .summarise: return "Count, average or total, for each…"
        case .head:      return "Keep the top few"
        case .distinct:  return "Drop repeated rows"
        case .dropNA:    return "Drop rows with gaps in them"
        case .fillNA:    return "Fill in the gaps"
        case .band:      return "Sort numbers into bands"
        case .round:     return "Round numbers off"
        case .toNumber:  return "Treat a column as numbers"
        case .toDate:    return "Treat a column as dates"
        case .toText:    return "Treat a column as text"
        case .datePart:  return "Pull the year or month out of a date"
        case .markMissing: return "Treat a value as missing"
        case .saveCSV:   return "Save this as a CSV"
        case .saveRDS:   return "Save this to open again in R"
        case .saveExcel: return "Save this as an Excel file"
        case .saveDelimited: return "Save with a different separator"
        case .saveForStats:  return "Save for Stata, SPSS or SAS"
        case .saveSummary:   return "Save a summary table, not the rows"
        case .peek:      return "Show the first few rows"
        case .viewIt:    return "Open it in RStudio's viewer"
        case .countRows: return "Say how many rows are left"
        case .glimpse:   return "List every column and what is in it"
        case .summaryOf: return "Show the range and average of each column"
        case .crossTab:  return "Cross one column against another"
        case .countValues:     return "Count how many of each"
        case .describeNumber:  return "Describe a number"
        case .missingReport:   return "See how much is missing"
        case .duplicateReport: return "Find rows that repeat"
        }
    }

    /// One line on what it does, so two similar entries can be told apart
    /// without trying them.
    public var blurb: String {
        switch self {
        case .filter:    return "where a column is, is not, or is more than something"
        case .sort:      return "smallest first, or largest"
        case .select:    return "hide the ones you are not using"
        case .mutate:    return "income per person, say"
        case .rename:    return ""
        case .summarise: return "one row per group, with the numbers you pick"
        case .head:      return "the first few rows, after sorting"
        case .distinct:  return "rows identical to one above"
        case .dropNA:    return "rows where a column you name is empty"
        case .fillNA:    return "with an average, a zero, or a value you type"
        case .band:      return "turns ages into age groups"
        case .round:     return ""
        case .toNumber:  return "for $1,200 or 45% that arrived as text"
        case .toDate:    return "so sorting puts 2 March before 10 March"
        case .toText:    return ""
        case .datePart:  return ""
        case .markMissing: return "for a file that writes \"Blank\" or \"Unknown\" instead of leaving a gap"
        case .saveCSV:   return "a file you can open in Excel, or send to someone"
        case .saveRDS:   return "keeps the column types exactly; only R reads it"
        case .saveExcel: return "a real .xlsx, so nobody has to fight the import dialog"
        case .saveDelimited: return "tab or semicolon, for data with commas in it"
        case .saveForStats:  return "for a collaborator who does not use R"
        case .saveSummary:   return "one row per group, written to its own file"
        case .peek:      return "prints them in the console, so you can see it worked"
        case .viewIt:    return "the spreadsheet-style window"
        case .countRows: return "prints the count, to check a filter did what you meant"
        case .glimpse:   return "the fast way to see what you are working with"
        case .summaryOf: return "min, max, average and how many are missing, per column"
        case .crossTab:  return "counts for every combination — the two-way table"
        case .countValues:     return "one row per value, with percentages"
        case .describeNumber:  return "the average, the spread, the middle and the quartiles"
        case .missingReport:   return "how many gaps each column has, worst first"
        case .duplicateReport: return "which values appear on more than one row"
        }
    }

    /// Words someone might actually type into the search box. The titles say
    /// what a step does in plain English; a beginner reaches for R's word, or
    /// Excel's. Both have to find it.
    public var keywords: String {
        switch self {
        case .filter:    return "subset where keep rows equals contains between greater less"
        case .sort:      return "order arrange ascending descending"
        case .select:    return "columns keep drop hide choose"
        case .mutate:    return "calculate new column formula compute derive ratio per"
        case .rename:    return "name column"
        case .summarise: return "group by aggregate mean average sum total count median each"
        case .head:      return "top n first limit"
        case .distinct:  return "duplicate duplicates unique"
        case .dropNA:    return "missing na empty blank gaps remove drop"
        case .fillNA:    return "missing na impute blank gaps replace"
        case .band:      return "cut bands buckets groups categorise categorize age brackets"
        case .round:     return "decimal places rounding"
        case .toNumber:  return "numeric number currency percent text convert type as.numeric"
        case .toDate:    return "date time convert type as.date"
        case .toText:    return "character string convert type as.character"
        case .datePart:  return "year month day weekday extract"
        case .markMissing: return "missing na blank unknown sentinel recode none null placeholder"
        case .saveCSV:   return "save write export csv excel output out file write.csv"
        case .saveRDS:   return "save rds serialize export r binary saverds"
        case .saveExcel: return "excel xlsx spreadsheet save write export workbook writexl"
        case .saveDelimited: return "tsv tab separator delimited semicolon pipe txt export save write"
        case .saveForStats:  return "stata spss sas dta sav xpt haven export save write collaborator"
        case .saveSummary:   return "summary table aggregate group counts stats export save write"
        case .peek:      return "print head show see look preview glimpse first rows"
        case .viewIt:    return "view viewer look see spreadsheet window inspect"
        case .countRows: return "count nrow how many rows check size"
        case .glimpse:   return "glimpse str structure columns types look see what is in"
        case .summaryOf: return "summary describe stats range min max quartiles overview"
        case .crossTab:  return "table crosstab cross tabulation two way counts by"
        case .countValues:     return "frequency table counts tally percent proportion distribution how many each value n breakdown"
        case .describeNumber:  return "mean average median sd standard deviation iqr quartile range spread describe summary statistics typical"
        case .missingReport:   return "missing na empty blank gaps how many complete completeness report audit quality"
        case .duplicateReport: return "duplicate duplicates repeated same twice unique identify check find"
        }
    }

    /// What shape the block takes in the script. Three, not two.
    ///
    /// A `.transform` is `frame <- frame |> verb(...)`. A `.statement` stands on
    /// its own — writing a file, printing, opening a viewer — and must never
    /// reassign the frame, or "save a copy" would quietly replace your data with
    /// the return value of write.csv, which is NULL.
    ///
    /// A `.result` is the third: it makes a new thing and leaves the data alone.
    /// It needs its own name so a later step can save it, and it needs to print,
    /// because a beginner who runs a line and sees nothing happen concludes the
    /// app is broken.
    public var emits: Emission {
        switch group {
        case .save, .look: return .statement
        case .answer:      return .result
        default:           return .transform
        }
    }

    /// Kept so every existing caller and test reads the same as before.
    public var isFinishing: Bool { emits != .transform }

    /// Whether the result is built by piping the data into it. False for the
    /// ones that assemble something new — `missingReport` builds a data.frame
    /// out of `colSums`, and has no frame to pipe from.
    public var pipesFromFrame: Bool { self != .missingReport }

    /// Which drawer it sits in. Grouping by intent is what makes a list of
    /// sixteen readable; grouping by dplyr verb is what makes it a glossary.
    public var group: ActionGroup {
        switch self {
        case .filter, .dropNA, .head, .distinct:      return .fewerRows
        case .summarise:                              return .boilDown
        case .sort, .select, .mutate, .rename, .round, .band: return .shape
        case .fillNA, .toNumber, .toDate, .toText, .datePart, .markMissing: return .fix
        case .saveCSV, .saveRDS, .saveExcel, .saveDelimited, .saveForStats,
             .saveSummary:                            return .save
        case .peek, .viewIt, .countRows, .glimpse, .summaryOf, .crossTab:
                                                      return .look
        case .countValues, .describeNumber, .missingReport, .duplicateReport:
                                                      return .answer
        }
    }
}

/// The three shapes a generated block can take. See `ActionKind.emits`.
public enum Emission: Sendable {
    case transform, statement, result
}

public enum ActionGroup: String, CaseIterable, Sendable {
    case fewerRows = "Fewer rows"
    case boilDown  = "Boil it down"
    case shape     = "Order and columns"
    case fix       = "Fix a column that came in wrong"
    case answer    = "Get an answer"
    case save      = "Save it to a file"
    case look      = "Look at it"
}

/// How a filter compares. The label is what appears on screen; the operator is
/// what goes into the R.
public enum Comparison: String, CaseIterable, Codable, Sendable {
    case eq, ne, gt, ge, lt, le, isIn, between, contains, isMissing, notMissing

    public var label: String {
        switch self {
        case .eq:         return "is"
        case .ne:         return "is not"
        case .gt:         return "is more than"
        case .ge:         return "is at least"
        case .lt:         return "is less than"
        case .le:         return "is at most"
        case .isIn:       return "is one of"
        case .between:    return "is between"
        case .contains:   return "contains"
        case .isMissing:  return "is missing"
        case .notMissing: return "is not missing"
        }
    }

    public var op: String {
        switch self {
        case .eq: return "=="
        case .ne: return "!="
        case .gt: return ">"
        case .ge: return ">="
        case .lt: return "<"
        case .le: return "<="
        default:  return ""
        }
    }

    /// The ones that need no value typed beside them.
    public var takesValue: Bool {
        switch self {
        case .isMissing, .notMissing: return false
        default: return true
        }
    }
}

public enum Statistic: String, CaseIterable, Codable, Sendable {
    case count, mean, median, sum, min, max, sd

    public var label: String {
        switch self {
        case .count:  return "how many rows"
        case .mean:   return "the average"
        case .median: return "the middle value"
        case .sum:    return "the total"
        case .min:    return "the smallest"
        case .max:    return "the largest"
        case .sd:     return "the spread (standard deviation)"
        }
    }

    public var rFunction: String { self == .count ? "n" : rawValue }
}

/// Which numbers describe a column. Offered rather than chosen, because which
/// pair you report is a decision a reader has to be told about: mean and SD
/// assume a roughly symmetric spread, median and quartiles do not.
public enum NumberSummary: String, CaseIterable, Codable, Sendable {
    case meanSD, medianIQR, both, deciles

    public var label: String {
        switch self {
        case .meanSD:    return "the average and the spread"
        case .medianIQR: return "the middle value and the quartiles"
        case .both:      return "both, and the smallest and largest"
        case .deciles:   return "every tenth, from smallest to largest"
        }
    }

    public var note: String {
        switch self {
        case .meanSD:    return "The usual pair, and the right one when the values are roughly symmetric."
        case .medianIQR: return "The honest pair when the column is skewed or has a few extreme values."
        case .both:      return "Report both and let the reader judge."
        case .deciles:   return "The whole shape, as ten numbers."
        }
    }
}

public enum FillMethod: String, CaseIterable, Codable, Sendable {
    case mean, median, zero, value
    public var label: String {
        switch self {
        case .mean:   return "the average of the column"
        case .median: return "the middle value of the column"
        case .zero:   return "zero"
        case .value:  return "a value I type"
        }
    }
}

public enum DatePiece: String, CaseIterable, Codable, Sendable {
    case year, month, monthName, day, weekday
    public var label: String {
        switch self {
        case .year:      return "the year"
        case .month:     return "the month number"
        case .monthName: return "the month name"
        case .day:       return "the day of the month"
        case .weekday:   return "the day of the week"
        }
    }
}

/// Separators other than the comma. A file whose own values contain commas is
/// the usual reason to need one.
public enum TextFormat: String, CaseIterable, Codable, Sendable {
    case tab, semicolon, pipe

    public var label: String {
        switch self {
        case .tab:       return "Tab-separated (.tsv)"
        case .semicolon: return "Semicolon-separated (.csv)"
        case .pipe:      return "Pipe-separated (.txt)"
        }
    }

    /// What goes inside the quotes in the R. Written as R would write it.
    public var separator: String {
        switch self {
        case .tab:       return "\\t"
        case .semicolon: return ";"
        case .pipe:      return "|"
        }
    }

    public var fileExtension: String {
        switch self {
        case .tab:       return "tsv"
        case .semicolon: return "csv"
        case .pipe:      return "txt"
        }
    }

    public var note: String {
        switch self {
        case .tab:       return "The safe choice when your data has commas in it."
        case .semicolon: return "What Excel expects in most of Europe."
        case .pipe:      return "Common in data sent by hospitals and registries."
        }
    }
}

/// The other statistics packages, through haven.
public enum StatsFormat: String, CaseIterable, Codable, Sendable {
    case stata, spss, sas

    public var label: String {
        switch self {
        case .stata: return "Stata (.dta)"
        case .spss:  return "SPSS (.sav)"
        case .sas:   return "SAS (.xpt)"
        }
    }

    public var rFunction: String {
        switch self {
        case .stata: return "write_dta"
        case .spss:  return "write_sav"
        case .sas:   return "write_xpt"
        }
    }

    public var fileExtension: String {
        switch self {
        case .stata: return "dta"
        case .spss:  return "sav"
        case .sas:   return "xpt"
        }
    }
}

/// One step, filled in. Every field is optional because the builder is filled in
/// a control at a time, and a half-built action has to be a legal value rather
/// than a crash.
public struct Action: Codable, Identifiable, Sendable, Equatable {
    public var id = UUID()
    public var kind: ActionKind
    public var column: String = ""
    public var columns: [String] = []
    public var groups: [String] = []
    public var comparison: Comparison = .eq
    public var value: String = ""
    public var value2: String = ""
    public var values: [String] = []
    public var statistics: [Statistic] = [.count]
    public var target: String = ""
    public var newName: String = ""
    /// Where a finishing step writes to, relative to the project when it can be.
    public var outputPath: String = ""
    public var expression: String = ""
    public var descending: Bool = false
    public var count: Int = 10
    public var digits: Int = 2
    public var bands: Int = 3
    public var equalWidth: Bool = false
    public var fill: FillMethod = .mean
    public var datePiece: DatePiece = .year
    public var dateFormat: String = "%Y-%m-%d"
    public var textFormat: TextFormat = .tab
    public var statsFormat: StatsFormat = .stata
    /// Writes a byte-order mark so Excel on Windows opens accented characters
    /// correctly. Off by default because it confuses some other readers.
    public var forExcel: Bool = false
    /// What a `.result` step calls the thing it makes, so a later step can refer
    /// to it. Empty means "work one out from the columns".
    public var resultName: String = ""
    /// Which earlier result this step reads. Empty means the data frame itself.
    public var source: String = ""
    public var spread: NumberSummary = .both

    public init(kind: ActionKind) { self.kind = kind }
}
