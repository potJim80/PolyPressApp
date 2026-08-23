import SwiftUI
import HandrailCore

struct MainWindow: View {
    @Bindable var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            SetupBar(state: state)
            Divider()

            if state.project == nil {
                FirstRun(state: state)
            } else {
                HSplitView {
                    ColumnList(state: state)
                        .padding(Style.gutter)
                        .frame(minWidth: 230, idealWidth: 270, maxWidth: 340)

                    // Either pane has to be able to be taller than the window:
                    // with 209 columns to choose from, "for each" alone is fifty
                    // rows of chips. The form manages its own scrolling, because
                    // it pins the R and the Add button below it.
                    Group {
                        if let kind = state.chosenKind {
                            ActionForm(state: state, kind: kind)
                        } else {
                            ScrollView {
                                VStack(alignment: .leading, spacing: Style.gap) {
                                    if let problem = state.problem { Problem(text: problem) }
                                    ActionPalette(state: state)
                                }
                                .padding(Style.gutter)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                    .frame(minWidth: 440)
                }
                Divider()
                AddedStrip(state: state)
            }
        }
        .frame(minWidth: 820, minHeight: 560)
        .writeFeedback(state)
        .sheet(isPresented: Binding(get: { state.valuesFor != nil },
                                    set: { if !$0 { state.closeValues() } })) {
            ValuesSheet(state: state)
        }
    }
}

/// What has been added this session, and the way back out of the last one.
private struct AddedStrip: View {
    @Bindable var state: AppState

    var body: some View {
        HStack(spacing: 12) {
            if let note = state.note {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                Text(note)
            } else if state.added.isEmpty {
                // The one thing that confuses people the first time: RStudio
                // reloads a changed file silently only when the editor has no
                // unsaved edits of its own. Otherwise it asks, and the honest
                // answer is always "reload".
                Text("Nothing added yet. What you build lands at the end of your script — "
                   + "keep it saved in RStudio and the lines just appear.")
                    .foregroundStyle(.secondary)
            } else {
                Text("\(state.added.count) added this session")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let last = state.added.last {
                Text(last.sentence)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: 340, alignment: .trailing)
                Button("Take that back out") { state.undoLast() }
            }
        }
        .font(.callout)
        .padding(.horizontal, Style.gutter)
        .padding(.vertical, 11)
        .background(.bar)
    }
}

/// The first screen: one instruction, not a dashboard.
private struct FirstRun: View {
    @Bindable var state: AppState

    var body: some View {
        VStack(spacing: 22) {
            Spacer()
            VStack(spacing: 10) {
                Image(systemName: "text.append")
                    .font(.system(size: 40, weight: .light))
                    .foregroundStyle(Color.accentColor)
                Text("Build R, one step at a time")
                    .font(.largeTitle.weight(.semibold))
                Text("Point Handrail at the RStudio project you are working in. Choose a "
                   + "script and a data file, build a step from the menus, and the real R "
                   + "is written into that script. You run it in RStudio, as normal.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 560)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button {
                if let folder = ProjectPicker.run() { state.open(project: folder) }
            } label: {
                Text("Choose a project…").padding(.horizontal, 10).padding(.vertical, 3)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)

            Text("Nothing is copied and nothing is sent anywhere. Handrail reads the top of "
               + "your data file to fill in the menus, and writes to the one script you choose.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(Style.gutter)
    }
}
