import SwiftUI
import HandrailCore

/// Every value in a column, and how often each turns up.
///
/// The reason this exists: `filter(region == "North")` is legal R that keeps no
/// rows if the file actually says "north", or "North ", or "NORTH". Typing a
/// category name from memory is the commonest way to get a confidently empty
/// answer, so the values are shown and clicking one writes the filter.
///
/// It opens with the values from the rows already read — instant — and says so.
/// Counting every row is a pass over the file and happens only when asked.
struct ValuesSheet: View {
    @Bindable var state: AppState
    @State private var search = ""

    private var tally: ValueTally? { state.tally }

    private var shown: [ValueTally.Entry] {
        guard let tally else { return [] }
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return tally.entries }
        return tally.entries.filter { $0.value.lowercased().contains(q) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if let tally {
                if tally.entries.isEmpty {
                    empty
                } else {
                    list(tally)
                }
                Divider()
                footer(tally)
            } else {
                ProgressView().padding(30).frame(maxWidth: .infinity)
            }
        }
        .frame(width: 520, height: 560)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(state.valuesFor ?? "").font(.title3.weight(.semibold))
                if let tally {
                    Text(summary(tally)).font(.callout).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button { state.closeValues() } label: { Image(systemName: "xmark") }
                .buttonStyle(.plain).foregroundStyle(.secondary)
        }
        .padding(Style.gutter)
    }

    private func summary(_ tally: ValueTally) -> String {
        var bits: [String] = []
        bits.append(tally.tooMany ? "more than \(tally.distinct) different values"
                                  : "\(tally.distinct) different value\(tally.distinct == 1 ? "" : "s")")
        bits.append("in \(tally.rows.formatted()) row\(tally.rows == 1 ? "" : "s")")
        if tally.missing > 0 { bits.append("\(tally.missing.formatted()) empty") }
        bits.append(tally.complete ? "the whole file"
                                  : state.counting ? "the rows read so far — still counting"
                                                   : "the rows read so far")
        return bits.joined(separator: " · ")
    }

    @ViewBuilder private var empty: some View {
        VStack(spacing: 8) {
            Text("Nothing but empty cells in the rows read.")
                .foregroundStyle(.secondary)
            if !(tally?.complete ?? true) {
                Text("Counting every row may find values further down the file.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(Style.gutter)
    }

    private func list(_ tally: ValueTally) -> some View {
        VStack(spacing: 0) {
            if tally.entries.count > 12 {
                TextField("Find a value", text: $search)
                    .textFieldStyle(.roundedBorder)
                    .padding(.horizontal, Style.gutter)
                    .padding(.vertical, 10)
            }
            if tally.looksLikeAnIdentifier {
                Text("Nearly every row has a different value here, so this is probably an "
                   + "identifier or a measurement rather than a category.")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, Style.gutter)
                    .padding(.bottom, 8)
            }
            ScrollView {
                LazyVStack(spacing: 1) {
                    ForEach(shown) { entry in
                        ValueRow(entry: entry, share: tally.share(entry)) {
                            state.filterBy(value: entry.value)
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
            }
            if shown.isEmpty {
                Text("No value with that in it.")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.bottom, 10)
            }
        }
    }

    private func footer(_ tally: ValueTally) -> some View {
        HStack(spacing: 12) {
            if tally.complete {
                Label("Counted every row", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green).font(.callout)
            } else if state.counting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Counting every row…").font(.callout).foregroundStyle(.secondary)
                }
            } else {
                Button("Count every row") { state.countWholeFile() }
                Text("Reads the whole file — a moment on a big one.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text("Click a value to filter by it").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, Style.gutter)
        .padding(.vertical, 12)
    }
}

private struct ValueRow: View {
    let entry: ValueTally.Entry
    let share: Double
    let pick: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: pick) {
            HStack(spacing: 10) {
                Text(entry.value.isEmpty ? "(empty)" : entry.value)
                    .lineLimit(1)
                    .foregroundStyle(entry.value.isEmpty ? .secondary : .primary)
                Spacer(minLength: 10)
                // A bar reads faster than a percentage, and the number is there
                // for when the exact figure is what matters.
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.accentColor.opacity(0.35))
                    .frame(width: max(2, 90 * share), height: 6)
                Text(entry.count.formatted())
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .frame(width: 62, alignment: .trailing)
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .contentShape(Rectangle())
            .background(hovering ? Color(nsColor: .selectedContentBackgroundColor).opacity(0.18)
                                 : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
