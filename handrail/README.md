# Handrail

A small macOS app that writes R into your RStudio script.

You point it at your project, pick a script and a data file, build a step from
menus, and press **Add to script**. The real dplyr goes into your script,
RStudio reloads it, and you run it there — normally.

It is not a replacement for RStudio. It does not run R, show your data, draw
charts or hold results. It closes the gap between knowing what you want to do
and knowing how to write it.

## Run it

```sh
./build.sh
open build/Handrail.app
```

Needs macOS 14 or newer. Nothing else — no R packages, no installer, no account.

## Where the code lives

This folder is the place to work, and it has no git remote of its own. Handrail
lives on GitHub inside
[potJim80/PolyPressApp](https://github.com/potJim80/PolyPressApp/tree/turbo/handrail),
under `handrail/`, merged in with its history rather than copied there.

That merge is a separate step, so committing here puts nothing on GitHub.
One command does the lot:

```sh
./push-to-github.sh "what you changed"
```

It commits, runs the tests, merges into a PolyPressApp checkout and pushes.
If the tests fail nothing is published; `--skip-tests` overrides that.

**It does not touch the website.** Nothing on polypressapp.com reads this
source — a published number changes when `content/handrail.json` in the website
repository changes. `HANDOFF.md` §4 has the procedure.

## Try it

There is an example project in `example/`. Open `example/analysis.R` in RStudio,
then in Handrail choose the `example` folder as the project. Build a step and
watch it appear.

## What it writes

Choosing "Keep only the rows where age is more than 65" appends this:

```r
# Keep only the rows where age is more than 65
data <- data |>
  filter(age > 65)
```

The comment is the sentence you picked. It stays in the script after the app is
closed, which is the point: next time, the R and the English are side by side.

The first step also writes the preamble, once, if the script does not already
read the data:

```r
# Built with Handrail. This is ordinary R — it runs with or without the app.
library(dplyr)

data <- read.csv("data/people.csv", stringsAsFactors = FALSE)
```

The path is relative to the project, so the script still runs on another machine.

And it ends with something. "Save this as a CSV" appends:

```r
# Save what you have as outputs/nhamcs_result.csv
write.csv(nhamcs_adult_data, "outputs/nhamcs_result.csv", row.names = FALSE)
```

`row.names = FALSE` is spelled out because R's default is `TRUE`, which puts an
unnamed column of 1, 2, 3… in the file — the classic way a CSV arrives looking
wrong to whoever opens it. Handrail creates the `outputs/` folder if it is not
there, because R will not.

## What it will not do

- **Rewrite your file.** It only ever appends. "Take that back out" removes the
  last thing it added, and only if the file still ends with exactly that text —
  if you have typed since, it refuses and tells you to use Undo in RStudio.
- **Hide a decision.** `na.rm = TRUE` and `fixed = TRUE` are written out rather
  than left to defaults, so the script agrees with the documentation you read
  next.
- **Write half a step.** The Add button stays disabled until the action would
  make legal R.
- **Choose your script for you.** It picks nothing unless the project holds one
  short script, and asks before adding to anything that already has work in it.

## Actions

Thirty-three, grouped by what you are trying to do and searchable by the word you
would actually type — "missing" finds "Drop rows with gaps in them", "xlsx" and
"spreadsheet" both find the Excel one.

**Fewer rows** — keep some rows, drop rows with gaps, keep the top few, drop
repeated rows.
**Boil it down** — count, average or total, for each group.
**Order and columns** — sort, add a worked-out column, keep some columns,
rename, round, sort numbers into bands.
**Fix a column that came in wrong** — fill the gaps, treat a value like `Blank`
as missing, treat a column as numbers, dates or text, pull the year or month out
of a date.
**Save it to a file** — a CSV (with the option Excel on Windows needs for
accented characters), an Excel `.xlsx`, tab- semicolon- or pipe-separated, an
`.rds`, a `.dta`/`.sav`/`.xpt` for Stata, SPSS or SAS, or a summary table written
to its own file while your data carries on untouched.
**Get an answer** — count how many rows have each value of a column, with
percentages; describe a number (how many, how many missing, the average and
spread, the middle and the quartiles, or every tenth); see how much is missing in
each column, worst first; find the rows that repeat.
**Look at it** — print the first few rows, list every column with `glimpse()`,
show ranges and averages with `summary()`, cross one column against another, open
RStudio's viewer, or print how many rows are left.

An answer is written as two lines — the table is given a name, and then the name
sits on its own so running it shows you something:

```r
# Count how many rows have each value of region, commonest first, with percentages
region_counts <- people |>
  count(region, name = "n") |>
  mutate(percent = round(100 * n / sum(n), 1)) |>
  arrange(desc(n))

region_counts
```

The name is so a later step can use that table. Handrail reads the names out of
your script rather than remembering them, so a table you wrote yourself counts
too — and a name already in use never gets quietly reassigned.

The Excel and Stata/SPSS/SAS steps generate `writexl::` and `haven::` calls
rather than attaching the packages, so the line says where the function came
from. If the package is not installed, the caution beside the step says so before
you run into it.

Clicking a column in the left-hand list offers only the steps that suit what
that column holds, and **See the values in it** lists what is actually in there
with a count against each — click one to filter by it. It shows the rows already
read straight away, then counts **the whole file** and replaces them — about a
second for 278 MB. A list of values built from the first few hundred rows is the
same kind of quiet lie the app exists to stop, so it is not something you have to
ask for.

## Looking at the data

**⇧⌘D**, or the button in the setup bar, opens the rows in a window of their own
— a window rather than a panel, so it can sit open beside the builder while you
fill a step in.

On a wide file it opens with the first twelve columns and a **Columns** button to
choose others. That is not only about the screen: it only turns the columns you
are looking at into text at all, which on a 209-column file is a twentieth of the
work. Pages of 500 rows load as you ask for them, each one resuming from where
the last stopped, so the twentieth page costs what the first did — **6 ms**. It
stops at 20,000 rows and says so; past that, the answer is R.

## Speed

It reads the top of your data file and stops — enough to fill the menus, and no
more. On a 278 MB, 209-column survey file that is **1.74 MB read in 0.04
seconds**. The app idles at around 100 MB, most of which is SwiftUI itself.

The two things that deliberately read more than that stay cheap too: counting
every row of one column costs 1.09 s and 21 MB, and a viewer page costs 6 ms.
One streaming parser (`RowStream`) does all three jobs, so there is one set of
edge cases — a quoted field straddling a read boundary, a last line with no
newline — rather than three.

```sh
swift run -c release handrail-bench yourfile.csv
```

prints what a file costs: size, seconds, bytes actually read, peak memory.

## Tests

```sh
swift run handrail-test
```

225 assertions. The important ones write a real CSV and a real script, run it
with `Rscript`, and check the answer — every action the app offers is verified
to produce R that actually runs.
