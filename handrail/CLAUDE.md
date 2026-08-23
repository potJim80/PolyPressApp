# CLAUDE.md — Handrail

Context for future sessions. Read this before changing anything.

## What this is, and the mistake it exists to correct

Handrail is a native macOS app that writes R into an RStudio script.

You point it at an RStudio project, pick a script and a data file, build a step
from menus, and press **Add to script**. The real dplyr goes into that script.
RStudio notices the file changed on disk and reloads it, so the line appears in
front of the person using it, and they run it there — with Cmd-Enter, in
RStudio, like anybody else.

It began on 2026-08-20 after two earlier attempts (both in `../archive/`) were
scrapped. Both failed the same way and it is the thing to keep guarding against:

> **Every hour spent building a table view, a chart picker or a console is an
> hour spent building a worse RStudio.**

The first attempt was an R package of plain-language verbs; the second a Shiny
GUI that had, without anyone deciding to, become a whole analysis environment.
Handrail's entire job is the gap between "I know what I want to do" and "I know
how to write it in R". Nothing else.

**The test for any proposed feature:** does RStudio already do this? If yes, the
answer is no. Viewing data, running code, plotting, debugging, installing
packages, managing the project — all RStudio's. Handrail does not even run R.

## The audience

The owner's son, and people like him: capable, working with real data, and
stopped by R's syntax rather than by the ideas. He is not being taught a
vocabulary to replace R's — he is being handed R's own, one line at a time, with
the sentence he chose sitting above it as a comment. That comment is the only
part of this app that survives the app being closed, which is why it matters.

## The rules

**1. Append, never rewrite.** Whatever is above the line we add is his,
including lines we wrote and he then changed. `ScriptWriter` only ever appends,
and `remove()` will only take back the last addition if the file still ends with
exactly that text. An app that reformats someone's file is one they stop
trusting, and this one is trusted with the only copy.

**2. One action, one idea — and every function on the line is one you can look
up.** Never a helper only this app knows. The mechanical test: no generated line
may call a function that is not documented in R or in a named package.

This was written as "one action, one dplyr verb" and that was never true —
`RCode.summariseChain` has always emitted `group_by |> summarise |> arrange`, and
`.band` emits a `mutate` wrapping `cut(quantile(...))`. The wording above is what
was actually being enforced. It matters because `ggplot() + geom_bar() + labs()`
has to pass and a `handrail_describe()` helper has to fail.

**3. Nothing is hidden.** `na.rm = TRUE` is written out rather than relied on,
because the next thing he reads about `mean()` will say the default is FALSE.
`fixed = TRUE` is written out on `grepl` because without it "1.5" also matches
"125". `var.equal`-style defaults do not get to be invisible.

**4. Half an action never reaches the script.** `RCode.call` returns nil until
there is enough to make legal R, and the Add button is disabled until then.

## Architecture

| Path | Purpose |
|---|---|
| `Sources/HandrailCore/Action.swift` | the vocabulary: what a step is, and what it is called on screen |
| `Sources/HandrailCore/RCode.swift` | action → dplyr source. The heart of it. |
| `Sources/HandrailCore/Sentence.swift` | action → the English comment, and the cautions |
| `Sources/HandrailCore/DataFile.swift` | reads the top of a CSV to fill the menus; a real CSV parser |
| `Sources/HandrailCore/RowStream.swift` | the one streaming parser — used by everything that reads past the head |
| `Sources/HandrailCore/ColumnValues.swift` | what is in a column, from the sample and from the whole file |
| `Sources/HandrailCore/DataPager.swift` | pages of rows for the viewer, resuming by byte offset |
| `Sources/HandrailCore/RProject.swift` | .Rproj discovery, script and data listing, relative paths |
| `Sources/HandrailCore/ScriptWriter.swift` | the preamble, the append, the take-back |
| `Sources/Handrail/AppState.swift` | the only state there is |
| `Sources/Handrail/DataViewer.swift` | the data window (⇧⌘D) and its column chooser |
| `Sources/Handrail/*.swift` | SwiftUI: window, menu-bar panel, the forms |
| `Sources/HandrailTests/main.swift` | the test runner |
| `example/` | an RStudio project to point it at |

`HandrailCore` knows nothing about SwiftUI, which is what lets the tests run
without a UI.

## Build and test

```sh
./build.sh              # build, replace /Applications/Handrail.app, open it
./build.sh debug        # same, faster to compile
swift run handrail-test # 225 assertions
swift run -c release handrail-bench <file.csv>   # size, seconds, bytes read, peak MB
```

`build.sh` quits the running copy, deletes it, installs the new one and opens
it, so there is never a moment when two versions exist.

**There is no Xcode on this machine — only the Command Line Tools.** That is why
there is no `.xcodeproj`, why `build.sh` assembles the app bundle by hand, and
why the tests are a plain executable instead of XCTest (XCTest ships with
Xcode). None of this is a workaround to be tidied away later; it builds a real,
signed, working `.app`.

## Efficiency, and how it is kept

The app sits beside RStudio all day. It has to cost nothing.

**Measure, do not assert.** `swift run handrail-bench <file.csv>` prints size,
seconds, bytes actually read, and peak memory. Re-run it after touching
`DataFile`.

Current, on a real 278 MB / 209-column survey file:

| | before | after |
|---|---|---|
| time to load | > 120 s | **0.04 s** |
| bytes read | 278 MB | **1.74 MB** |
| process memory | > 1.1 GB | **~103 MB** (SwiftUI's own baseline is most of that) |

And the two operations that deliberately read more than the head, on the same
file — both cheap enough that neither needs to be a button or a warning:

| | cost | peak memory |
|---|---|---|
| count every row of one column (96,539 rows) | 1.09 s | 21 MB |
| one viewer page, 500 rows × 12 of 209 columns | 6 ms | 20 MB |
| the twentieth viewer page | **also 6 ms** | 20 MB |

The last row is the point of `DataPager`: pages resume from a byte offset, so
paging does not degrade as you go down a file. `PAGES=1 swift run handrail-bench
<file.csv>` re-measures it; `COLUMN=<name>` and `COUNT=<name>` do the others.

**The three mistakes that caused it**, all of them the standard way to get this
wrong in Swift, and all pinned by tests now:

1. **Reading the whole file.** `Data(contentsOf:)` + `String(data:encoding:)`
   holds a 278 MB file twice over, to keep 400 rows. `DataFile.readHead` now
   streams 256 KB at a time until it has enough line breaks or hits
   `maxHeadBytes`, then trims back to the last newline — always a safe cut in
   UTF-8, since 0x0A never appears inside a multi-byte sequence.
2. **`Character`.** Swift's `Character` is a grapheme cluster, so iterating a
   `String` runs Unicode segmentation per step. Splitting on `,` and `\n` needs
   none of it. `CSV.parse` works on UTF-8 **bytes** and decodes only finished
   fields.
3. **Work on the render path.** A SwiftUI body runs on every keystroke, so
   anything in one runs hundreds of times. Three were: `DataFile.types` built a
   209-entry dictionary per read (now built once), `columnNames(_:)` filtered
   the column list per picker per render (now precomputed by type), and the
   script/data menus called `project.scripts()` — a directory scan — from inside
   a view body (now cached, with a "Look again" item).

**`FileHandle.read(upToCount:)` returns autoreleased `Data`.** Reading a file in
a loop without an `autoreleasepool` inside it keeps every chunk alive until the
function returns: counting one column of a 278 MB file peaked at **304 MB**, and
**21 MB** with the pool. Same time either way, which is why nothing but a
measurement would have found it.

**The other half of it was the view tree.** A plain `VStack` in a `ScrollView`
is eager, and every column row carried its own `Menu`: 209 columns produced
thousands of views in one go and SwiftUI's AttributeGraph aborted the *process*
with SIGABRT. Now `LazyVStack`, one popover built only for the column that is
open, and a filter box past a dozen columns. `FlowChips` got the same treatment
past two dozen options.

**Duplicate ids abort the process too.** Column names are the identity of every
row in that list, so `DataFile.deduplicate` guarantees uniqueness rather than
trusting a counter — a header of `a, a, a.1` used to emit `a.1` twice.

**A finishing step is a statement, not a pipeline link.** `write.csv`, `saveRDS`,
`print`, `View` and `nrow` do something *with* the data rather than to it, and
`RCode.block` emits them bare. Piping one would assign its return value over the
frame — `data <- data |> write.csv(...)` sets `data` to NULL. `ActionKind.isFinishing`
is what keeps the two apart, and a test asserts a finishing block contains no `|>`.

## Non-obvious technical findings

**The most valuable test runs R.** `Sources/HandrailTests/main.swift` writes a
real CSV and a real script, runs it with `Rscript --vanilla`, and compares the
answer. Every action the app offers is checked this way — 16 of 16 produce R
that runs. Text-comparison tests cannot catch a wrong quote or a column name R
cannot see; this does.

**Keep stdout and stderr apart when running R.** `library(dplyr)` writes its
masking notice to stderr. Folding it into stdout means comparing answers against
a paragraph of startup chatter — four tests failed on exactly that.

**Quoting is the whole ballgame.** `filter(age == "65")` is legal R that
silently keeps nothing. `RCode.literal` decides by the column's sniffed type,
and deliberately leaves a non-numeric value quoted even in a number column, so R
errors rather than lying.

**RStudio reloads externally-changed files** when the editor buffer is
unmodified, and asks when it is not. That is correct behaviour on its part and
the reason the script should be saved before using Handrail.

**Not sandboxed**, so file access is direct. Paths are remembered in
UserDefaults; if macOS ever gates a remembered path, the fix is to re-pick it
through the panel, which is what grants access.

## The workflow, and what guards it

Stage by stage, with what goes wrong at each — every one of these was a real
failure, not a hypothetical:

| Stage | What can go wrong | Guard |
|---|---|---|
| Choose a project | — | `.Rproj` or its folder both work (`RProject.folder(for:)`) |
| Choose a script | It picks somebody's finished analysis | It picks **nothing**, unless the project holds exactly one short script |
| First write to it | Unrelated R lands at the end of a 200-line program | Confirmation naming the file and its line count, once per script |
| Data outside project | Absolute path, script will not travel | Caution in the form, and a comment in the script saying so |
| First step added | The line lands on disk and nothing happens on screen | RStudio only reloads a file it has OPEN, so the first write opens it, once |
| Second step | A second `library()` and `read.csv()` | `hasPreamble(frame:)` and `hasLibrary(_:)`, both multiline-anchored |
| A second data file | It reads into `data` and silently clobbers the first | The frame is named after the file (`RCode.frameName`) |
| Look at a column's values | A list from 570 rows presented as "the values" | Opens from the sample, labelled as such, with an explicit "Count every row" |
| Take it back | It deletes something the user typed | `remove()` only acts if the file still ends with exactly that text |
| From the menu bar | The confirmation never appears and the write silently no-ops | `writeFeedback` and `StatusLine` are shared by both surfaces |

`Sources/HandrailTests/main.swift` walks all of it against real folders, and runs
the finished script through `Rscript` with two data files in it.

## State and what is next

Working: project/script/data selection, **thirty-three actions**, the column menu,
the searchable palette, the code preview, the cautions, appending, taking the
last one back, opening the script in RStudio, the menu-bar panel, and the data
viewer window.

**The roadmap lives at `~/.claude/plans/mutable-imagining-bumblebee.md`** and was
approved on 2026-08-21. Read it before planning anything: it carries the user
segmentation, the phase order, the weighted-p-value decision, and the full
nineteen-action design for "getting an answer out". This file records what is
*built*; that file records what is *next*.

**Under git since 2026-08-21.** The first commit captures the app exactly as it
stood before any of this. An app trusted with the only copy of someone's script
had no floor under its own `ScriptWriter` for two days; it does now.

**Getting an answer out (2026-08-22) — Phase A1 of the plan.** Every one of the
first twenty-nine actions *prepared* data. None produced a result. The evidence
that this was the gap: across 81 real R scripts in `~/Desktop/Research`, every
project ends in a Table 1 with p-values, a `gt`/`flextable` table and a figure.

Three things landed, and the first is the one to understand:

- **A third block shape.** `ActionKind.emits` returns `.transform` (`frame <-
  frame |> verb()`), `.statement` (bare, for the save/look steps that must never
  reassign the frame) or `.result`. `isFinishing` is kept as a shim so nothing
  else changed. A `.result` is **assign, then echo** — the name on its own line
  after the assignment. A bare pipeline prints but nothing can ever refer to it,
  which kills `ggsave` and Word output before they are written; an assignment
  with no echo runs and shows nothing, which reads as the app being broken. Two
  statements is the smallest form that does both.
- **`ScriptWriter.assignedNames()`** — every name the script assigns to, read off
  disk with one regex. This is how a later step will find an earlier result.
  Deliberately *not* an in-app registry: reading the file survives a relaunch,
  survives the user renaming something by hand, and picks up tables they wrote
  themselves. `RCode.resultName(_:fallback:taken:)` then steps past what is taken,
  so step 11 cannot quietly reassign what step 3 made.
- **Four actions** in a new `"Get an answer"` group: `countValues` (one row per
  value, with percentages), `describeNumber`, `missingReport`, `duplicateReport`.
  Clicking a column offers the right one first, because "what is in this column"
  is the first question anyone has.

Two R facts worth keeping, both measured rather than remembered:

- **`reframe()`, not `summarise()`, for deciles.** Since dplyr 1.1 a `summarise()`
  returning more than one row is a hard *error* — `` `decile` must be size 1, not
  11 `` — where it used to be a warning. Checked against dplyr 1.2.1 here.
- **`drop = FALSE` in `missingReport` is load-bearing.** `people[, c("age")]`
  collapses to a vector and `colSums()` then stops. It is written out both because
  it has to be and because it is the kind of trap worth seeing on the page.

Two tests exist specifically to stop this class of action going wrong: every
`.result` action must leave `nrow(data)` unchanged, and **every action in the
`.answer` group must return a non-nil caution** — `Sentence.caution` ends in
`default: return nil`, so without that test a new answer action ships silent, and
these are exactly the ones whose wrong answers look right.

**Next: Phase A2** — `compareNumber`, `compareCategories` (the percentaged
cross-tab; the existing `crossTab` gives raw counts only and is not a data frame),
and `overTime`. Still no p-values: those are A4, and the weighted-data refusal
guard goes in before the first one.

**Ways out (2026-08-21).** CSV, RDS, Excel (`writexl::write_xlsx`), tab /
semicolon / pipe (`write.table` with `sep` named and quoting left **on** —
`quote = FALSE` is tidier to read and corrupts any value holding the separator),
Stata / SPSS / SAS (`haven::write_dta` / `write_sav` / `write_xpt`), and a
summary table written to its own file without disturbing the data. CSV has a
UTF-8-BOM option for Excel on Windows. `writexl` and `haven` are named with `::`
rather than attached, so the line says where the function came from and the
preamble stays about the data; `RCode.requiredPackage(for:)` is what lets the app
name the package before R stops with "there is no package called".

**Ways of looking (2026-08-21).** `glimpse()`, `summary()` (all columns or a
chosen few), and a two-way `table()` with `useNA = "ifany"` — a cross-tab that
silently drops the missing rows is exactly the quiet wrong answer this app is
for. Plus the viewer window itself.

**The data viewer.** ⇧⌘D, or the button in the setup bar. A separate `Window`
scene rather than a sheet, so it can sit open beside the builder. Two things keep
it cheap on a 278 MB file, and both are load-bearing: it builds strings **only
for the columns on screen** (twelve of 209 by default), and each page resumes
from the **byte offset** the last one ended at instead of counting from the top.
Measured: **6 ms a page, and page 20 costs what page 1 costs.** It stops offering
more at `DataPager.maxHeld` (20,000 rows) and says so — a viewer is for looking.

**"What is in this column" now means the column.** The whole-file count used to
be a button. It is not any more: opening the values list shows the sample
instantly and then replaces it with the real count. It costs **1.09 s and 21 MB**
on the 278 MB file, and a list built from the first few hundred rows is the same
class of lie the app exists to stop.

**In progress, stopped mid-build on 2026-08-20: Table 1.** `Table1.swift` holds
the spec, the test suggestions and the reasons for them, and compiles. Nothing is
wired to it yet — no code generation, no UI. The decisions already taken, so they
do not get re-litigated:

- **Plain dplyr and base R, not `gtsummary` or `tableone`.** Both of those are
  better-looking and both choose a test per variable by their own rules, which is
  the one decision that is not theirs to make. The owner said "a mix depending on
  the request"; plain R is the floor because it is the only option that chooses
  nothing.
- **Tests are suggested and confirmed, never applied.** Welch over Student
  (assumes less), chi-squared over Fisher (Fisher answers a small-count problem
  only the real table can reveal). The reason is shown beside the suggestion, and
  the test's name goes into the table beside the p-value.
- **Weighting is an action, not a table option.** The owner's call, and it is the
  right one: "create an option to weigh data as an action, that will fix the
  problem at the root." A `.useWeights` step names the weight column, and every
  summarising step after it generates weighted arithmetic explicitly —
  `sum(DISCWT)` rather than `n()`, `weighted.mean(x, DISCWT)` rather than
  `mean(x)`, and the weighted-SD formula written out rather than hidden.

**The unresolved problem, and it is a real one.** A naive t-test or chi-squared
on survey-weighted data is **wrong**: NEDS needs the design — strata
(`NEDS_STRATUM`), clusters (`HOSP_ED`) and weights (`DISCWT`) — and that means
`survey::svydesign` with `svyttest`/`svychisq`, not base R. So when a weight
column is set, Handrail must either refuse to write p-values and say why, or
generate real `survey` code. Refusing with an explanation is the honest floor;
generating survey code is the useful answer and needs the strata and cluster
columns named too. **Decide this before writing the p-value generator** — writing
naive weighted tests would be the worst bug this app could ship, because the
numbers would look fine.

**Deferred by the owner on 2026-08-21, to be picked up later: reading anything
that is not a delimited text file.** Asked whether Parquet was wanted, they said
"1 + 2, but later development after this session. Keep it in the docs for future
steps", and separately, of the save formats, "should also take all of the above"
— meaning Handrail should eventually *open* Excel, Stata, SPSS and Parquet, not
only write them. Treat these as **one piece of work, not four**, because they
share one mechanism and one decision:

1. **Parquet as a save format** — `arrow::write_parquet`, trivial, no reader
   needed. `arrow` is already installed on this machine.
2. **Convert-once-then-read-fast** — offer to turn a big CSV into Parquet and
   write `arrow::read_parquet()` into the preamble instead of `read.csv`. This is
   the one with the real payoff: the 278 MB NHAMCS file costs R about a minute
   through `read.csv` on *every run*, against about a second through Parquet.
   Handrail gains nothing from it — its own reader is already 0.04 s — so this is
   for the script, not for the app, and should be presented that way.
3. **Opening non-CSV files** — `.xlsx`, `.dta`, `.sav`, `.parquet`. `DataFile`
   reads delimited text only. Everything else needs a bridge: shell out to
   `Rscript` (or the `duckdb` CLI, present at `/opt/homebrew/bin/duckdb`) to get
   the header and a sample, land it as a temporary CSV, and read that with the
   existing fast path.

**The decision to make first, before any of it is built:** a bridge means the app
depends on something outside itself, and the failure mode is a beginner being
told "cannot open file" by a tool that is supposed to be the easy part. Either it
detects the dependency and says plainly what is missing and how to get it, or it
does not offer the format at all. Also note what the bridge quietly costs: the
whole-file column count and the viewer's byte-offset paging both assume a
delimited file on disk. Through a bridge, either they run against the temporary
CSV — which is then a copy that can go stale — or they stop being available and
the app has to say so rather than silently going back to sampling.

Not done, roughly in order of how much it would help:
- **Charts.** One action producing a `ggplot()` call would fit the rules, and it
  is the thing beginners most want to write and least can.
- **Statistical tests.** Same shape, but the archived project's rule applies with
  full force: a method a statistician would have to report in a methods section
  can never be chosen silently. If a t-test action lands, `var.equal` is on the
  screen, not in a default.
- **A proper icon**, and a `.dmg` if it is ever handed to anyone else.
- **The NEDS header problem**, still open from the archived project: the NEDS
  Core file has no header row, so every column reads as `V1`–`V56`. Needs a "this
  file has no header row" option plus column names pasted in or parsed from the
  HCUP `.sas` load program.
- `audit_table1.R` in `~/Desktop/Research/Projects/NEDS_R6521` still has **lines
  181-192** that Handrail appended to it by mistake, back when it auto-picked the
  alphabetically first script. The owner has not said whether to remove them.

## Working preferences

Responses must be precise and concise. No preamble, no filler.

Ask clarifying questions before starting work. Standing rule, not per-task.
