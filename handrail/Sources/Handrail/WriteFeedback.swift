import SwiftUI
import HandrailCore

/// The confirmation, the error and the receipt — attached wherever a step can be
/// added from.
///
/// This exists because they were not. The confirmation alert lived on the main
/// window only, so adding a step from the menu-bar panel to a script with
/// existing work set `confirm`, showed nothing, and silently did not write. A
/// button that appears to do nothing is worse than one that fails loudly.
struct WriteFeedback: ViewModifier {
    @Bindable var state: AppState

    func body(content: Content) -> some View {
        content
            .alert("Add to \(state.confirm?.scriptName ?? "this script")?",
                   isPresented: Binding(get: { state.confirm != nil },
                                        set: { if !$0 { state.confirm = nil } }),
                   presenting: state.confirm) { _ in
                Button("Add to the end of it") { state.confirmWrite() }
                Button("Cancel", role: .cancel) { state.confirm = nil }
            } message: { pending in
                Text("\(pending.scriptName) already has \(pending.lines) lines in it. "
                   + "Handrail only ever adds to the end — nothing above is touched — "
                   + "but if this is not the script you meant to build up, pick another "
                   + "one first.")
            }
    }
}

extension View {
    func writeFeedback(_ state: AppState) -> some View {
        modifier(WriteFeedback(state: state))
    }
}

/// The one-line receipt: what just happened, or what went wrong.
struct StatusLine: View {
    @Bindable var state: AppState

    var body: some View {
        if let problem = state.problem {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                Text(problem).fixedSize(horizontal: false, vertical: true)
            }
            .font(.callout)
        } else if let note = state.note {
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                Text(note)
            }
            .font(.callout)
        }
    }
}
