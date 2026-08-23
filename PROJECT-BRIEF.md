# Handrail — project brief

A self-contained description of the project, written to be handed to someone
building a website about it. Everything here is accurate as of **2026-08-23**.
Nothing is aspirational unless it is explicitly labelled as roadmap.

---

## 1. One sentence

Handrail is a native macOS app that writes R into your RStudio script.

## 2. The longer version

You point it at an RStudio project, pick a script and a data file, build a step
from menus, and press **Add to script**. Real dplyr goes into that script.
RStudio notices the file changed on disk and reloads it, so the line appears in
front of you, and you run it there — with Cmd-Enter, in RStudio, like anybody
else.

Handrail does not run R. It does not display your data as the source of truth,
draw charts, hold results, manage packages or open a console. Its entire job is
the gap between *"I know what I want to do"* and *"I know how to write it in
R"*. Nothing else.

Above every generated line sits the plain-English sentence you chose, as a
comment. That comment is the only part of the app that survives the app being
closed — which is the whole point. Next time you open the script, the English
and the R are side by side.

## 3. Origin, and the mistake it exists to correct

Handrail began on **2026-08-20**, after two earlier attempts were scrapped. The
first was an R package of plain-language verbs. The second was a Shiny GUI that
had, without anyone deciding to, grown into a whole analysis environment.

Both failed the same way, and it is the thing the project guards against:

> **Every hour spent building a table view, a chart picker or a console is an
> hour spent building a worse RStudio.**

**The test for any proposed feature:** does RStudio already do this? If yes, the
answer is no. Viewing data, running code, plotting, debugging, installing
packages, managing the project — all RStudio's.

## 4. Who it is for

The author's son, and people like him: capable, working with real data, and
stopped by R's *syntax* rather than by the ideas. He is not being taught a
vocabulary that replaces R's — he is being handed R's own vocabulary, one line
at a time.

This matters for the website's tone. Handrail is not "R without code" and not a
no-code tool. It is a scaffold you are meant to eventually stop needing.

## 5. The four rules

These are enforced in code and pinned by tests. They are the design, not
guidelines.

**1. Append, never rewrite.** Whatever is above the line we add belongs to the
user — including lines we wrote and they then changed. `ScriptWriter` only ever
appends, and "take that back out" only works if the file *still ends with
exactly* the text we added. If the user has typed since, it refuses and points
them at Undo in RStudio. An app that reformats someone's file is one they stop
trusting, and this one is trusted with the only copy.

**2. One action, one idea — and every function on the generated line is one you
can look up.** Never a helper only this app knows. The mechanical test: no
generated line may call a function that isn't documented in R or in a named
package. `ggplot() + geom_bar() + labs()` passes; a `handrail_describe()` helper
fails.

**3. Nothing is hidden.** `na.rm = TRUE` is written out rather than relied on,
because the next thing you read about `mean()` will say the default is FALSE.
`fixed = TRUE` is written out on `grepl` because without it `"1.5"` also matches
`"125"`. `row.names = FALSE` is spelled out on `write.csv` because R's default
of TRUE is the classic reason a CSV arrives looking wrong.

**4. Half an action never reaches the script.** Code generation returns nil
until there is enough to make legal R, and the Add button stays disabled until
then.

## 6. What it actually writes

Choosing *"Keep only the rows where age is more than 65"* appends:

```r
# Keep only the rows where age is more than 65
data <- data |>
  filter(age > 65)
```

The first step also writes the preamble, once, and only if the script doesn't
already read the data:

```r
# Built with Handrail. This is ordinary R — it runs with or without the app.
library(dplyr)

data <- read.csv("data/people.csv", stringsAsFactors = FALSE)
```

The path is relative to the project, so the script still runs on another
machine. If the data file lives outside the project, a comment says so and the
app warns you in the form.

Saving:

```r
# Save what you have as outputs/nhamcs_result.csv
write.csv(nhamcs_adult_data, "outputs/nhamcs_result.csv", row.names = FALSE)
```

Handrail creates the `outputs/` folder if it isn't there, because R won't.

An **answer** is written as two statements — the table gets a name, then the
name sits on its own line so running it shows you something:

```r
# Count how many rows have each value of region, commonest first, with percentages
region_counts <- people |>
  count(region, name = "n") |>
  mutate(percent = round(100 * n / sum(n), 1)) |>
  arrange(desc(n))

region_counts
```

## 7. The three block shapes

This is the single most important internal concept, and worth showing on a
technical page.

| Shape | Form | Why |
|---|---|---|
| `.transform` | `frame <- frame \|> verb(...)` | the ordinary case |
| `.statement` | bare, no assignment | `write.csv`, `saveRDS`, `print`, `View`, `nrow` do something *with* the data, not *to* it. Piping one would assign its return value over the frame — `data <- data \|> write.csv(...)` sets `data` to NULL. |
| `.result` | assign, then echo the name | A bare pipeline prints but nothing can refer to it, which kills `ggsave` and Word output before they're written. An assignment with no echo runs and shows nothing, which reads as the app being broken. Two statements is the smallest form that does both. |

A test asserts a finishing block contains no `|>`.

## 8. The thirty-three actions

Grouped by intent, not by dplyr verb — grouping by verb makes a glossary, and a
glossary is what this app exists to avoid. Each is searchable by the word a
beginner would actually type: "missing" finds *Drop rows with gaps in them*;
"xlsx" and "spreadsheet" both find the Excel one.

**Fewer rows**
- Keep only some rows — where a column is, is not, or is more than something
- Drop rows with gaps in them
- Keep the top few — the first few rows, after sorting
- Drop repeated rows

**Boil it down**
- Count, average or total, for each… — one row per group, with the numbers you pick

**Order and columns**
- Sort the rows
- Keep only some columns
- Add a column worked out from others — income per person, say
- Rename a column
- Round numbers off
- Sort numbers into bands — turns ages into age groups

**Fix a column that came in wrong**
- Fill in the gaps — with an average, a zero, or a value you type
- Treat a value as missing — for a file that writes "Blank" or "Unknown" instead of leaving a gap
- Treat a column as numbers — for `$1,200` or `45%` that arrived as text
- Treat a column as dates — so sorting puts 2 March before 10 March
- Treat a column as text
- Pull the year or month out of a date

**Get an answer** (each produces a named, printed result table)
- Count how many of each — one row per value, with percentages
- Describe a number — the average, the spread, the middle and the quartiles
- See how much is missing — how many gaps each column has, worst first
- Find rows that repeat

**Save it to a file**
- Save this as a CSV — with a UTF-8-BOM option for Excel on Windows
- Save this to open again in R — `.rds`, keeps column types exactly
- Save this as an Excel file — a real `.xlsx` via `writexl::write_xlsx`
- Save with a different separator — tab, semicolon or pipe, for data with commas in it
- Save for Stata, SPSS or SAS — `haven::write_dta` / `write_sav` / `write_xpt`
- Save a summary table, not the rows — written to its own file while your data carries on untouched

**Look at it**
- Show the first few rows
- List every column and what is in it — `glimpse()`
- Show the range and average of each column — `summary()`
- Cross one column against another — `table()` with `useNA = "ifany"`
- Open it in RStudio's viewer
- Say how many rows are left

Two details worth surfacing:

- `writexl` and `haven` are called with `::` rather than attached, so the line
  says where the function came from and the preamble stays about the data. The
  app names the required package in the caution *before* R stops with "there is
  no package called".
- The cross-tab uses `useNA = "ifany"` because a cross-tab that silently drops
  the missing rows is exactly the quiet wrong answer this app exists to stop.

## 9. Naming results without an in-app registry

`ScriptWriter.assignedNames()` reads every name the script assigns to, off disk,
with one regex. That is how a later step finds an earlier result.

Deliberately *not* an in-app registry: reading the file survives a relaunch,
survives the user renaming something by hand, and picks up tables they wrote
themselves. `RCode.resultName(_:fallback:taken:)` steps past what's taken, so
step 11 can't quietly reassign what step 3 made.

## 10. The interface

- **Main window** — setup bar (project / script / data), a searchable action
  palette, the form for the chosen action, a live code preview, the cautions,
  and Add to script / Take that back out.
- **Column list** — click a column and it offers only the steps that suit what
  that column holds. "What is in this column" is the first question anyone has,
  so the answer actions are offered first.
- **See the values in it** — lists what is actually in a column with a count
  against each; click one to filter by it. It shows the already-read rows
  instantly, then counts **the whole file** and replaces them. That is not a
  button you have to press: a list built from the first few hundred rows is the
  same class of lie the app exists to stop.
- **Data viewer** — ⇧⌘D, a separate window (not a sheet) so it can sit open
  beside the builder. Opens with the first twelve columns on a wide file, with a
  Columns button. Pages of 500 rows, each resuming from where the last stopped.
  Stops at 20,000 rows and says so; past that, the answer is R.
- **Menu-bar panel** — the same builder, from the menu bar, sharing the same
  status line and write-confirmation code as the window.

## 11. Speed, measured not asserted

The app sits beside RStudio all day. It has to cost nothing. There is a
benchmark executable: `swift run -c release handrail-bench <file.csv>` prints
size, seconds, bytes actually read, and peak memory.

On a real **278 MB / 209-column** survey file:

| | before | after |
|---|---|---|
| time to load | > 120 s | **0.04 s** |
| bytes read | 278 MB | **1.74 MB** |
| process memory | > 1.1 GB | **~103 MB** (SwiftUI's own baseline is most of that) |

The two operations that deliberately read more than the head, same file:

| | cost | peak memory |
|---|---|---|
| count every row of one column (96,539 rows) | 1.09 s | 21 MB |
| one viewer page, 500 rows × 12 of 209 columns | 6 ms | 20 MB |
| the **twentieth** viewer page | **also 6 ms** | 20 MB |

That last row is the point of the pager: pages resume from a byte offset, so
paging doesn't degrade as you go down a file.

### The five mistakes that caused the "before" column

All of them the standard way to get this wrong in Swift, and all pinned by tests
now. This list is the most interesting technical content in the project.

1. **Reading the whole file.** `Data(contentsOf:)` + `String(data:encoding:)`
   holds a 278 MB file twice over, to keep 400 rows. The reader now streams
   256 KB at a time until it has enough line breaks, then trims back to the last
   newline — always a safe cut in UTF-8, since `0x0A` never appears inside a
   multi-byte sequence.
2. **`Character`.** Swift's `Character` is a grapheme cluster, so iterating a
   `String` runs Unicode segmentation per step. Splitting on `,` and `\n` needs
   none of it. The CSV parser works on UTF-8 **bytes** and decodes only finished
   fields.
3. **Work on the render path.** A SwiftUI body runs on every keystroke, so
   anything in one runs hundreds of times. Three were: a 209-entry type
   dictionary rebuilt per read, a column-list filter run per picker per render,
   and a directory scan called from inside a view body.
4. **`FileHandle.read(upToCount:)` returns autoreleased `Data`.** Reading a file
   in a loop without an `autoreleasepool` *inside* the loop keeps every chunk
   alive until the function returns: counting one column of the 278 MB file
   peaked at **304 MB**, and **21 MB** with the pool. Same time either way —
   which is why nothing but a measurement would have found it.
5. **The view tree.** A plain `VStack` in a `ScrollView` is eager, and every
   column row carried its own `Menu`: 209 columns produced thousands of views at
   once and SwiftUI's AttributeGraph aborted the **process** with SIGABRT. Now
   `LazyVStack`, one popover built only for the open column, and a filter box
   past a dozen columns.

Also: **duplicate `id`s abort the process too.** Column names are the identity
of every row in that list, so deduplication is guaranteed rather than trusted to
a counter — a header of `a, a, a.1` used to emit `a.1` twice.

One streaming parser (`RowStream`) does all three reading jobs — head, column
count, viewer page — so there is one set of edge cases (a quoted field
straddling a read boundary, a last line with no newline) rather than three.

## 12. Testing

```sh
swift run handrail-test    # 225 assertions
```

**The most valuable test runs R.** The test runner writes a real CSV and a real
script, runs it with `Rscript --vanilla`, and compares the answer. Every action
the app offers is checked this way. Text-comparison tests can't catch a wrong
quote or a column name R can't see; this does.

Two findings from building it:

- **Keep stdout and stderr apart when running R.** `library(dplyr)` writes its
  masking notice to stderr. Folding it into stdout means comparing answers
  against a paragraph of startup chatter — four tests failed on exactly that.
- **Quoting is the whole ballgame.** `filter(age == "65")` is legal R that
  silently keeps nothing. The literal generator decides by the column's sniffed
  type, and *deliberately* leaves a non-numeric value quoted even in a number
  column, so R errors rather than lying.

Two tests exist specifically to stop the answer actions going wrong: every
`.result` action must leave `nrow(data)` unchanged, and **every action in the
"Get an answer" group must return a non-nil caution** — without that test a new
answer action ships silent, and these are exactly the ones whose wrong answers
look right.

## 13. Architecture

| Path | Purpose |
|---|---|
| `Sources/HandrailCore/Action.swift` | the vocabulary: what a step is, and what it's called on screen |
| `Sources/HandrailCore/RCode.swift` | action → dplyr source. The heart of it. |
| `Sources/HandrailCore/Sentence.swift` | action → the English comment, and the cautions |
| `Sources/HandrailCore/DataFile.swift` | reads the top of a CSV to fill the menus; a real CSV parser |
| `Sources/HandrailCore/RowStream.swift` | the one streaming parser |
| `Sources/HandrailCore/ColumnValues.swift` | what's in a column, from the sample and from the whole file |
| `Sources/HandrailCore/DataPager.swift` | pages of rows for the viewer, resuming by byte offset |
| `Sources/HandrailCore/RProject.swift` | `.Rproj` discovery, script and data listing, relative paths |
| `Sources/HandrailCore/ScriptWriter.swift` | the preamble, the append, the take-back |
| `Sources/HandrailCore/Table1.swift` | spec for the unbuilt Table 1 feature (compiles, unwired) |
| `Sources/Handrail/AppState.swift` | the only state there is |
| `Sources/Handrail/DataViewer.swift` | the data window and its column chooser |
| `Sources/Handrail/*.swift` | SwiftUI: window, menu-bar panel, the forms |
| `Sources/HandrailTests/main.swift` | the test runner |
| `example/` | an RStudio project to point it at |

`HandrailCore` knows nothing about SwiftUI, which is what lets the tests run
without a UI. About **6,200 lines** of Swift in total.

**There is no Xcode on the build machine — only the Command Line Tools.** That
is why there is no `.xcodeproj`, why `build.sh` assembles the app bundle by
hand, and why the tests are a plain executable instead of XCTest (XCTest ships
with Xcode). None of this is a workaround; it builds a real, signed, working
`.app`.

**Not sandboxed**, so file access is direct. Paths are remembered in
UserDefaults.

Requires macOS 14 or newer. No R packages, no installer, no account.

```sh
./build.sh              # build, replace /Applications/Handrail.app, open it
./build.sh debug        # same, faster to compile
swift run handrail-test
swift run -c release handrail-bench <file.csv>
```

`build.sh` quits the running copy, deletes it, installs the new one and opens
it, so there is never a moment when two versions exist.

## 14. The workflow, and what guards each stage

Every one of these was a real failure, not a hypothetical.

| Stage | What can go wrong | Guard |
|---|---|---|
| Choose a project | — | `.Rproj` or its folder both work |
| Choose a script | It picks somebody's finished analysis | It picks **nothing**, unless the project holds exactly one short script |
| First write to it | Unrelated R lands at the end of a 200-line program | Confirmation naming the file and its line count, once per script |
| Data outside project | Absolute path, script won't travel | Caution in the form, and a comment in the script saying so |
| First step added | The line lands on disk and nothing happens on screen | RStudio only reloads a file it has OPEN, so the first write opens it, once |
| Second step | A second `library()` and `read.csv()` | Multiline-anchored checks for an existing preamble |
| A second data file | It reads into `data` and silently clobbers the first | The frame is named after the file |
| Look at a column's values | A list from 570 rows presented as "the values" | Opens from the sample, labelled as such, then replaced by the whole-file count |
| Take it back | It deletes something the user typed | Only acts if the file still ends with exactly that text |
| From the menu bar | The confirmation never appears and the write silently no-ops | The feedback and status line are shared by both surfaces |

One regex detail worth keeping: **`(?m)` is load-bearing.** Swift's
`range(of:options:.regularExpression)` does *not* turn on multiline mode, so a
bare `^` anchors to the start of the whole file rather than the start of a line
— and a script with so much as a comment above the read line would report having
no preamble, every time, and get a fresh `library(dplyr)` stapled on with every
step added.

**RStudio reloads externally-changed files** when the editor buffer is
unmodified, and asks when it isn't. That's correct behaviour on its part, and
the reason the script should be saved before using Handrail.

## 15. Two R facts, measured rather than remembered

- **`reframe()`, not `summarise()`, for deciles.** Since dplyr 1.1 a
  `summarise()` returning more than one row is a hard *error* — `` `decile` must
  be size 1, not 11 `` — where it used to be a warning. Checked against dplyr
  1.2.1.
- **`drop = FALSE` is load-bearing** in the missing-data report.
  `people[, c("age")]` collapses to a vector and `colSums()` then stops. It's
  written out both because it has to be and because it's the kind of trap worth
  seeing on the page.

## 16. Status

Under git since 2026-08-21; five commits. Working today: project/script/data
selection, **thirty-three actions**, the column menu, the searchable palette,
the code preview, the cautions, appending, taking the last one back, opening the
script in RStudio, the menu-bar panel, and the data viewer window.

The evidence that "getting an answer out" was the gap: across **81 real R
scripts** on the author's machine, every project ends in a Table 1 with
p-values, a `gt`/`flextable` table and a figure. The first twenty-nine actions
all *prepared* data; none produced a result. Four answer actions landed on
2026-08-22, and the block-shape work that made them possible is described in §7.

### In progress, stopped mid-build: Table 1

`Table1.swift` holds the spec, the test suggestions and the reasons for them,
and compiles. Nothing is wired to it — no code generation, no UI. Decisions
already taken:

- **Plain dplyr and base R, not `gtsummary` or `tableone`.** Both are
  better-looking and both choose a test per variable by their own rules, which
  is the one decision that isn't theirs to make. Plain R is the floor because
  it's the only option that chooses nothing.
- **Tests are suggested and confirmed, never applied.** Welch over Student
  (assumes less), chi-squared over Fisher (Fisher answers a small-count problem
  only the real table can reveal). The reason is shown beside the suggestion,
  and the test's name goes into the table beside the p-value.
- **Weighting is an action, not a table option.** A "use weights" step names the
  weight column, and every summarising step after it generates weighted
  arithmetic explicitly — `sum(DISCWT)` rather than `n()`, `weighted.mean(x,
  DISCWT)` rather than `mean(x)`, and the weighted-SD formula written out rather
  than hidden.

### The unresolved problem, and it is a real one

A naive t-test or chi-squared on survey-weighted data is **wrong**. A dataset
like NEDS needs the design — strata, clusters and weights — which means
`survey::svydesign` with `svyttest`/`svychisq`, not base R. So when a weight
column is set, Handrail must either refuse to write p-values and say why, or
generate real `survey` code.

Refusing with an explanation is the honest floor; generating survey code is the
useful answer, and needs the strata and cluster columns named too. **This has to
be decided before the p-value generator is written**, because writing naive
weighted tests would be the worst bug this app could ship — the numbers would
look fine.

### Deferred: reading anything that isn't a delimited text file

Handrail currently reads delimited text only. Opening `.xlsx`, `.dta`, `.sav`
and `.parquet` is one piece of work, not four, because they share one mechanism
and one decision:

1. **Parquet as a save format** — `arrow::write_parquet`, trivial, no reader
   needed.
2. **Convert-once-then-read-fast** — offer to turn a big CSV into Parquet and
   write `arrow::read_parquet()` into the preamble instead of `read.csv`. This
   is the one with the real payoff: a 278 MB file costs R about a minute through
   `read.csv` on *every run*, against about a second through Parquet. Handrail
   gains nothing from it — its own reader is already 0.04 s — so it is for the
   script, not for the app, and should be presented that way.
3. **Opening non-CSV files** — needs a bridge: shell out to `Rscript` or the
   `duckdb` CLI to get the header and a sample, land it as a temporary CSV, and
   read that with the existing fast path.

**The decision to make first:** a bridge means the app depends on something
outside itself, and the failure mode is a beginner being told "cannot open file"
by the tool that's supposed to be the easy part. Either it detects the
dependency and says plainly what is missing and how to get it, or it doesn't
offer the format at all. The bridge also quietly costs the whole-file column
count and the viewer's byte-offset paging, both of which assume a delimited file
on disk.

### Not done, roughly in order of how much it would help

- **Charts.** One action producing a `ggplot()` call would fit the rules, and
  it's the thing beginners most want to write and least can.
- **Statistical tests.** Same shape, but with full force of the rule that a
  method a statistician would have to report in a methods section can never be
  chosen silently. If a t-test action lands, `var.equal` is on the screen, not
  in a default.
- **A proper icon**, and a `.dmg` if it's ever handed to anyone else.
- **The no-header-row problem.** Some public datasets (HCUP's NEDS Core file)
  have no header row, so every column reads as `V1`–`V56`. Needs a "this file
  has no header row" option plus column names pasted in or parsed from the
  vendor's SAS load program.

### Next planned

`compareNumber`, `compareCategories` (a percentaged cross-tab — the existing
cross-tab gives raw counts only and isn't a data frame), and `overTime`. Still
no p-values: those come later, and the weighted-data guard goes in before the
first one.

---

## 17. Notes for whoever builds the website

**Audience:** developers and technically-minded readers. Treat it as a project
page, not a product launch.

**Tone:** the project's own writing is plain, declarative, and specific — it
states measured numbers rather than adjectives, and explains *why* a decision
was made rather than asserting that it was good. Match that. Avoid marketing
language ("effortless", "powerful", "revolutionise"). The most persuasive
material here is concrete: the 278 MB → 0.04 s numbers, the five performance
mistakes, the `filter(age == "65")` quoting trap, and the "worse RStudio" line.

**Suggested shape:**
1. The one-sentence description, plus a code block showing an appended step
   (§6). Show the comment above the R — that *is* the product.
2. "The mistake it exists to correct" (§3) — this is the hook, and it's
   unusually honest for a project page.
3. The four rules (§5).
4. The action list (§8), probably as a grouped grid.
5. Performance (§11), with the tables, and the five mistakes as a
   deep-dive section — this is the part a technical reader will share.
6. Testing (§12) — "the most valuable test runs R" is a good subhead.
7. Architecture and build constraints (§13).
8. Status and open problems (§16) — including the survey-weighting problem,
   stated as an open question rather than hidden.

**Things not to do:**
- Don't present it as a replacement for RStudio, or as "R without code".
- Don't claim features from §16 as shipped.
- Don't invent screenshots or a download link — there is no `.dmg` and no icon
  yet. If a hero image is needed, render the generated-R code blocks.
- Don't soften the survey-weighting section. Its being an open, named problem is
  the point.

**Attribution/date:** the project started 2026-08-20; this brief is accurate to
2026-08-23.
