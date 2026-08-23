# TEST-15 — cross-column redundancy, built rather than bounded

Run 2026-08-06. The first predictor in this project for the backlog item that
`benchmarks/probe_cross_column.py` bounded on 2026-08-01 and never built.

**Why now:** LAW 3 (TEST-11) located the weakness precisely — on the seven
tables where row reordering does not fire, a byte-level context model that has
no idea the file is a table gets within 1–5% of us. Cross-column redundancy is
the one structure a general compressor **structurally cannot see**:
`report_location = "POINT (-122.272834 47.52331)"` beside `latitude` and
`longitude` is invisible to LZ77 at any window size, because the copies sit at
different precisions and share no long byte run.

## Result — 19.3% through the shipping codec, on the tables that carry it

| table | cols | found | xz full | xz strip | xz gain | pp full | pp strip | **pp gain** |
|---|---|---|---|---|---|---|---|---|
| chicago_crimes | 22 | 1 | 1,220,318 | 1,107,319 | 9.3% | 940,201 | 738,281 | **21.5%** |
| nyc_collisions | 29 | 1 | 909,115 | 809,209 | 11.0% | 674,333 | 560,808 | **16.8%** |
| seattle_fire911 | 7 | 1 | 525,052 | 474,162 | 9.7% | 350,554 | 286,848 | **18.2%** |
| **TOTAL** | | 3 | 2,654,485 | 2,390,690 | 9.9% | 1,965,088 | 1,585,937 | **19.3%** |

Recipe cost: **221, 278 and 3,978 bytes.**

**The transform is worth twice as much to Polypress as to xz** (19.3% vs 9.9%).
That is the right shape and worth stating plainly: xz recovers part of this
redundancy already, by matching the shared digit runs at short range. Polypress
does not — its columns are separated and compressed apart, so the duplicate is
never adjacent to its source. Removing it therefore helps us more.

Two of the three tables — `chicago_crimes` (0.769) and `nyc_collisions`
(0.971) — are ones where TEST-11 found PPMd close behind or level. This is a
direct answer to LAW 3.

## The transform

A cell is parsed into a **template** and its **numeric tokens**:

    "POINT (-122.272834 47.52331)"  ->  "POINT (\0 \0)" + ["-122.272834", "47.52331"]

For each slot the encoder looks for a source column that reproduces the slot
string row by row under one of two rules:

    EXACT      the source string is the slot string
    TRUNC(n)   the slot string is the source string cut to n characters

`TRUNC` is the rule that matters. The repo already measured that a *substring*
detector finds almost nothing while numeric-token matching took
`chicago_permits` from 0.49% to 5.47% — real duplication is at a different
**precision**, not a different position.

The column is then dropped. The decoder rebuilds it from the recipe and the
source columns it has already decoded.

## Three bugs, and two of them are this repo's own lessons repeating

1. **The all-or-nothing gate, walked into while quoting it.** The first version
   required every row of a column to share a template. It found **nothing on
   the entire corpus.** `seattle_fire911.report_location` is
   `POINT (-122.272834 47.52331)` on **39,922 of 40,000 rows and blank on 78**,
   and those 78 blanks cost the whole column. The gate now takes the **modal**
   template and stores non-matching rows as whole-cell exceptions. That single
   change took the result from 0 tables to 3.
2. **Derived columns can chain — and cycle.** Real data crashed the rebuild
   with a `TypeError`: two columns each derivable from the other, so both were
   dropped and neither could be rebuilt. The `turbo` branch hit the same shape
   from the other side (commit `12fb00d`, "decode derived columns in dependency
   order — they can chain"). Fixed here by the cheaper rule: a source must be a
   surviving column, iterated to a fixed point, because dropping one recipe can
   invalidate another.
3. **Partial recipes silently wrote wrong cells.** A column with two slots where
   only one had a source filled the other with `""` and rebuilt a corrupt value
   — with no error. A column is now either fully derivable or left alone.

Bug 3 is the dangerous one and the reason invariant 4 exists: it produced a
table that looked fine and was wrong. It was caught only because every variant
here is rebuilt and compared cell by cell.

## Caveats

- **3 of 13 tables**, and the corpus was curated. The unselected-100 run is in
  `results-100.txt`; the older ceiling probe found 40 of 100 carried *some*
  derivable column, using a looser definition than this predictor's.
- Only the geometry/template pattern is implemented. **Concatenated keys**
  (`row_id` = four columns pasted together, measured at 47% by the ceiling
  probe) are not — that needs a different detector.
- The recipe serialisation is deliberately plain text, not a packed format, so
  it is if anything overcharged. Exceptions dominate it when a rule is weak.
- Nothing here is wired into `fast.py`. Shipping it means the same transform in
  `csrc/ppz_encode.c` in the same commit (invariant 1), and a measured gate
  (invariant 2) — the ceiling probe found 6 of 40 tables get *worse*.
