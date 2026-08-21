import Foundation

/// Putting the R into his script.
///
/// The one rule: **append, never rewrite.** Whatever is above the line we add is
/// his, including the parts he has changed by hand, including the parts we wrote
/// last time and he then edited. An app that reformats someone's file is an app
/// they stop trusting, and this one has to be trusted with the only copy.
///
/// RStudio watches open files. When one changes on disk and the editor has no
/// unsaved changes of its own, it reloads without asking, so the new lines just
/// appear. If he does have unsaved changes, RStudio asks — which is the correct
/// thing for it to do, and why `unsavedChangesWarning` exists below.
public struct ScriptWriter: Sendable {
    public let script: URL
    public let project: RProject

    public init(script: URL, project: RProject) {
        self.script = script
        self.project = project
    }

    public enum Failure: LocalizedError {
        case cannotRead(String)
        case cannotWrite(String)

        public var errorDescription: String? {
            switch self {
            case .cannotRead(let s):  return "That script could not be read: \(s)"
            case .cannotWrite(let s): return "That script could not be written to: \(s)"
            }
        }
    }

    public func read() throws -> String {
        if !FileManager.default.fileExists(atPath: script.path) { return "" }
        do { return try String(contentsOf: script, encoding: .utf8) }
        catch { throw Failure.cannotRead(error.localizedDescription) }
    }

    /// Whether a line matching this pattern appears anywhere in the script.
    ///
    /// `(?m)` is load-bearing. Swift's `range(of:options:.regularExpression)`
    /// does NOT turn on multiline mode, so a bare `^` anchors to the start of
    /// the whole file rather than the start of a line — and a script with so
    /// much as a comment above the read line would report having no preamble,
    /// every time, and get a fresh `library(dplyr)` and `read.csv()` stapled on
    /// with every step added.
    private func containsLine(_ pattern: String) throws -> Bool {
        try read().range(of: "(?m)" + pattern, options: [.regularExpression]) != nil
    }

    /// Everything a script needs before the first action: the packages, and the
    /// line that reads the data. Written once, only when it is not already
    /// there, so a script he started himself is joined rather than taken over.
    public func preamble(dataFile: URL, frame: String) -> String {
        let path = project.referenceTo(dataFile)
        let outside = !path.hasPrefix("/") ? "" : """

        # Note: this file lives outside the project, so the path above is absolute.
        # Move it into the project folder if you want this script to run elsewhere.
        """
        return """
        # Built with Handrail. This is ordinary R — it runs with or without the app.
        library(dplyr)
        \(outside)
        \(frame) <- read.csv("\(path)", stringsAsFactors = FALSE)

        """
    }

    /// How much is already in the script, for deciding whether to warn before
    /// adding to it. A script with two hundred lines in it is somebody's work,
    /// not a scratch pad.
    public func existingLineCount() throws -> Int {
        let text = try read()
        if text.isEmpty { return 0 }
        return text.split(whereSeparator: \.isNewline).count
    }

    /// Whether this app has written here before. Its own marker, so a script it
    /// has been building for days is recognised and not warned about again.
    public func hasWrittenHereBefore() throws -> Bool {
        try containsLine("^# Built with Handrail")
    }

    /// Whether the script already reads this file into this name. Looked for
    /// rather than remembered, so a script edited between sessions still counts.
    public func hasPreamble(frame: String) throws -> Bool {
        try containsLine("^\\s*\(frame)\\s*<-\\s*read\\.")
    }

    public func hasLibrary(_ pkg: String) throws -> Bool {
        try containsLine("^\\s*library\\(\(pkg)\\)")
    }

    /// Adds one action to the end of the script. Returns exactly the text that
    /// was appended, so it can be taken back off again if it was a mistake.
    @discardableResult
    public func append(_ text: String) throws -> String {
        let existing = try read()
        var addition = text
        if !existing.isEmpty {
            // One blank line between blocks, and never two.
            let trailing = existing.suffix(2)
            if !trailing.hasSuffix("\n\n") {
                addition = (trailing.hasSuffix("\n") ? "\n" : "\n\n") + addition
            }
        }
        do {
            try (existing + addition).write(to: script, atomically: true, encoding: .utf8)
        } catch {
            throw Failure.cannotWrite(error.localizedDescription)
        }
        return addition
    }

    /// Takes back the last thing this app appended — but only if the file still
    /// ends with exactly that text. If he has typed since, the file is his and
    /// the undo is refused rather than guessed at.
    public func canRemove(_ text: String) throws -> Bool {
        try read().hasSuffix(text)
    }

    @discardableResult
    public func remove(_ text: String) throws -> Bool {
        let existing = try read()
        guard existing.hasSuffix(text) else { return false }
        let shortened = String(existing.dropLast(text.count))
        do {
            try shortened.write(to: script, atomically: true, encoding: .utf8)
        } catch {
            throw Failure.cannotWrite(error.localizedDescription)
        }
        return true
    }

    /// Opens the script in RStudio. Used after the first append, so the file he
    /// is meant to be looking at is in front of him.
    public func revealInRStudio() {
        let rstudio = URL(fileURLWithPath: "/Applications/RStudio.app")
        guard FileManager.default.fileExists(atPath: rstudio.path) else {
            NSWorkspace.shared.open(script); return
        }
        NSWorkspace.shared.open([script], withApplicationAt: rstudio,
                                configuration: NSWorkspace.OpenConfiguration())
    }
}

#if canImport(AppKit)
import AppKit
#endif
