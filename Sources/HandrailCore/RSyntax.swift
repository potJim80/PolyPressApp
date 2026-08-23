import Foundation

/// What a run of characters in a generated line is.
public enum RToken: String, Equatable, Sendable {
    case comment
    case string
    case function
}

/// A run of characters, by offset into `Array(source)`.
public struct RSpan: Equatable, Sendable {
    public let start: Int
    public let length: Int
    public let token: RToken

    public init(start: Int, length: Int, token: RToken) {
        self.start = start
        self.length = length
        self.token = token
    }
}

/// Where the colour goes in a generated block.
///
/// This is here rather than in the view layer for the same reason everything
/// else is: it can then be tested without a UI. It is deliberately the smallest
/// scanner that tells the truth about the three things worth distinguishing —
/// the English comment, a quoted value, and a function you could go and look up.
///
/// It is not an R parser and must not grow into one. Anything it cannot classify
/// with certainty is left plain, because miscolouring code in an app whose whole
/// promise is "what you see is what gets written" is worse than not colouring it.
public enum RSyntax {

    public static func spans(_ source: String) -> [RSpan] {
        let ch = Array(source)
        var out: [RSpan] = []
        var i = 0

        while i < ch.count {
            let c = ch[i]

            // A comment runs to the end of the line. Checked before anything
            // else, so a `#` inside a string is never reached here — the string
            // branch below consumes it first.
            if c == "#" {
                let start = i
                while i < ch.count && ch[i] != "\n" { i += 1 }
                out.append(RSpan(start: start, length: i - start, token: .comment))
                continue
            }

            if c == "\"" || c == "'" {
                let quote = c
                let start = i
                i += 1
                while i < ch.count {
                    if ch[i] == "\\" { i += 2; continue }   // \" does not close it
                    if ch[i] == quote { i += 1; break }
                    i += 1
                }
                out.append(RSpan(start: start, length: min(i, ch.count) - start, token: .string))
                continue
            }

            // A name is only a function if a `(` follows it immediately. That
            // rule is what keeps `age` in `filter(age > 65)` uncoloured while
            // `filter` is coloured, with no table of known names to maintain.
            if c.isLetter || c == "." {
                let start = i
                while i < ch.count,
                      ch[i].isLetter || ch[i].isNumber || ch[i] == "." || ch[i] == "_" {
                    i += 1
                }
                if i < ch.count && ch[i] == "(" {
                    out.append(RSpan(start: start, length: i - start, token: .function))
                }
                continue
            }

            i += 1
        }
        return out
    }
}
