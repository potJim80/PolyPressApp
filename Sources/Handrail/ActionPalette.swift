import SwiftUI
import HandrailCore

/// The list of things you can do, when what you want is not about one column.
///
/// Twenty-nine entries, grouped by what you are trying to do rather than by which
/// dplyr verb they are, searchable, and each one saying in a line what it does.
/// The search matches words nobody put on screen — typing "missing" finds "Drop
/// rows with gaps in them", because that is R's word for it and his book's.
struct ActionPalette: View {
    @Bindable var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: Style.gap) {
            Text("What do you want to do?")
                .font(.title3.weight(.semibold))

            TextField("Search — try \"average\", \"missing\", \"per group\"",
                      text: $state.search)
                .textFieldStyle(.roundedBorder)
                .font(.body)

            let groups = state.actions(matching: state.search)
            if groups.isEmpty {
                Text("Nothing matches that. Try a word like \"missing\", \"average\" or \"each\".")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.top, 4)
            } else {
                Group {
                    VStack(alignment: .leading, spacing: 18) {
                        ForEach(groups, id: \.0) { group, kinds in
                            VStack(alignment: .leading, spacing: 3) {
                                SectionLabel(group.rawValue).padding(.leading, 8)
                                ForEach(kinds, id: \.self) { kind in
                                    PaletteRow(kind: kind) { state.begin(kind) }
                                }
                            }
                        }
                    }
                    .padding(.bottom, 8)
                }
            }
        }
    }
}

private struct PaletteRow: View {
    let kind: ActionKind
    let choose: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: choose) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(kind.title)
                Spacer(minLength: 12)
                if !kind.blurb.isEmpty {
                    Text(kind.blurb)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                }
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
            .background(hovering ? Color(nsColor: .selectedContentBackgroundColor).opacity(0.18)
                                 : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
