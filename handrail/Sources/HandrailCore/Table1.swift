import Foundation

/// A baseline characteristics table — "Table 1".
///
/// This is the one part of Handrail that produces statistics rather than
/// reshaping data, so the rule that governs everything here is the one about
/// methods: **anything a statistician would have to report cannot be chosen
/// quietly.** In practice that means three things.
///
/// - The test for each row is suggested, never applied until it has been seen,
///   and its name goes into the table itself beside the p-value. A p-value with
///   no test named next to it is a methods section with a hole in it.
/// - The generated code is plain dplyr and base R. `gtsummary` and `tableone`
///   are the standards and both are better-looking, and both choose a test per
///   variable by their own rules — which is exactly the decision that is not
///   theirs to make.
/// - Weighted arithmetic is written out, formula and all, rather than hidden in
///   a helper package, so the weighting can be read and checked.
public struct Table1: Sendable, Equatable {
    public var groupBy: String = ""
    public var includeOverall: Bool = true
    public var rows: [Row] = []
    public var outputPath: String = "outputs/table1.csv"
    public var digits: Int = 1

    public init() {}

    public struct Row: Sendable, Identifiable, Equatable {
        public var id = UUID()
        public var column: String
        public var kind: Kind
        public var summary: Summary
        public var test: Test

        public init(column: String, kind: Kind, summary: Summary, test: Test) {
            self.column = column
            self.kind = kind
            self.summary = summary
            self.test = test
        }
    }

    public enum Kind: String, Sendable, CaseIterable, Equatable {
        case continuous, categorical
        public var label: String {
            self == .continuous ? "a number" : "categories"
        }
    }

    public enum Summary: String, Sendable, CaseIterable, Equatable {
        case meanSD, medianIQR, count

        public var label: String {
            switch self {
            case .meanSD:    return "mean (SD)"
            case .medianIQR: return "median [IQR]"
            case .count:     return "n (%)"
            }
        }
    }

    public enum Test: String, Sendable, CaseIterable, Equatable {
        case none, tTest, welch, wilcoxon, anova, kruskal, chiSquare, fisher

        public var label: String {
            switch self {
            case .none:      return "no test"
            case .welch:     return "Welch t-test"
            case .tTest:     return "Student's t-test"
            case .wilcoxon:  return "Wilcoxon rank-sum"
            case .anova:     return "one-way ANOVA"
            case .kruskal:   return "Kruskal-Wallis"
            case .chiSquare: return "chi-squared"
            case .fisher:    return "Fisher's exact"
            }
        }

        /// The name that goes into the table, written the way it would be
        /// written in a paper.
        public var methodName: String {
            switch self {
            case .none:      return ""
            case .welch:     return "Welch two-sample t-test"
            case .tTest:     return "Student's two-sample t-test (equal variances)"
            case .wilcoxon:  return "Wilcoxon rank-sum test"
            case .anova:     return "One-way ANOVA"
            case .kruskal:   return "Kruskal-Wallis test"
            case .chiSquare: return "Pearson's chi-squared test"
            case .fisher:    return "Fisher's exact test"
            }
        }

        /// Why it might be the one you want, in a sentence. Shown beside the
        /// choice, because "Wilcoxon" means nothing to someone who has not been
        /// taught it and everything to whoever reads the paper.
        public var because: String {
            switch self {
            case .none:      return "leave this row without a p-value"
            case .welch:     return "compares two averages without assuming the groups vary equally — R's default, and the safer one"
            case .tTest:     return "compares two averages, assuming both groups vary by the same amount"
            case .wilcoxon:  return "compares two groups by rank, so a few extreme values do not dominate"
            case .anova:     return "compares averages across three or more groups"
            case .kruskal:   return "compares three or more groups by rank"
            case .chiSquare: return "asks whether the split across categories differs between groups"
            case .fisher:    return "the same question, for when some counts are small"
            }
        }

        public var suitsContinuous: Bool {
            [.welch, .tTest, .wilcoxon, .anova, .kruskal, .none].contains(self)
        }
        public var suitsCategorical: Bool {
            [.chiSquare, .fisher, .none].contains(self)
        }
    }

    /// What to suggest for a row — proposed, never applied on its own.
    ///
    /// Deliberately conservative: Welch over Student because it assumes less,
    /// and chi-squared over Fisher because Fisher's is the answer to a small-count
    /// problem that only the real table can reveal.
    public static func suggestedTest(kind: Kind, groups: Int) -> Test {
        guard groups >= 2 else { return .none }
        switch kind {
        case .continuous:  return groups == 2 ? .welch : .anova
        case .categorical: return .chiSquare
        }
    }

    /// Why that one was suggested, so the suggestion can be argued with.
    public static func suggestionReason(kind: Kind, groups: Int) -> String {
        guard groups >= 2 else {
            return "There is only one group, so there is nothing to compare."
        }
        switch kind {
        case .continuous:
            return groups == 2
                ? "Two groups and a number, so a t-test. Welch's, because it does not "
                + "assume the two groups vary by the same amount — if you know they do, "
                + "Student's is the other option. If the values are skewed or have "
                + "outliers, Wilcoxon compares them by rank instead."
                : "\(groups) groups and a number, so one-way ANOVA. Kruskal-Wallis is "
                + "the rank-based alternative if the values are skewed."
        case .categorical:
            return "Categories against \(groups) groups, so a chi-squared test. If any "
                 + "expected count comes out below about five, R will warn you and "
                 + "Fisher's exact test is the answer."
        }
    }
}
