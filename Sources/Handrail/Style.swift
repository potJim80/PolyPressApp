import SwiftUI

/// The few shared pieces of look, in one place.
///
/// Nothing elaborate: this app sits beside RStudio, so it should look like a
/// macOS utility rather than compete for attention. Room to breathe is the only
/// thing it insists on — the previous attempt failed partly on being cramped.
enum Style {
    static let gutter: CGFloat = 20
    static let gap: CGFloat = 14
    static let corner: CGFloat = 10
}

extension View {
    /// A titled section, used everywhere so the eye can find its way by shape.
    func card() -> some View {
        self
            .padding(Style.gutter)
            .background(Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: Style.corner))
            .overlay(
                RoundedRectangle(cornerRadius: Style.corner)
                    .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1))
    }
}

struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
            .kerning(0.6)
    }
}

/// Code, shown the way RStudio shows it, so the thing on screen and the thing in
/// his editor are recognisably the same thing.
struct CodeBlock: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(.body, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1))
    }
}

struct Caution: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
        .font(.callout)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct Problem: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
        .font(.callout)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
