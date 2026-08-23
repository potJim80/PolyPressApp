# TEST-16 — concatenated keys: a null result on this corpus

Run 2026-08-06. The ceiling probe (`probe_cross_column.py`, 2026-08-01) named
two patterns as doing nearly all of the cross-column win: geometry republished
as text (built in TEST-15, worth 19.3%) and **concatenated keys** — `row_id`
made of four columns pasted together, measured at **47.35%**, the single best
result in that probe. This builds the second detector.

## Result — it finds nothing the geometry detector had not already found

| table | cols | geo | key | pp full | pp geo | pp key | pp both | best gain |
|---|---|---|---|---|---|---|---|---|
| chicago_crimes | 22 | 1 | 1 | 940,201 | 738,281 | **738,274** | 738,552 | 21.5% |
| nyc_collisions | 29 | 1 | 1 | 674,333 | 560,808 | 561,486 | 565,464 | 16.8% |
| seattle_fire911 | 7 | 1 | 1 | 350,554 | 286,848 | **286,841** | 287,062 | 18.2% |

**The two detectors found the same column by two different routes.** A WKT
point is both "a template with numeric slots" and "a concatenation of pieces
with literal glue", so both descriptions fit and both produce the same saving
to within 7 bytes. Running both and combining is very slightly *worse* than
either alone (`pp both` > `pp geo`), because the recipes are written twice.

**On this corpus, concatenated keys are worth zero on top of geometry.** The
probe's 47% case is not in the curated 13; it must be in the unselected 100,
which is the sweep to run next.

## The detector

Segments a cell left to right, greedily preferring the longest matching column
value, into COLUMN and LITERAL parts. The recipe is derived from one row (the
first with a value long enough to be non-trivial), then **verified against
every row** — the cheap direction, since a wrong segmentation fails at once.
Rows that disagree become whole-cell exceptions, up to 10%.

Guards, both learned from TEST-15 rather than rediscovered:

- a source must be a surviving column, iterated to a fixed point (two columns
  can be concatenations of each other's pieces);
- at least two column references, and no piece shorter than 2 characters — a
  one-character "match" is noise, not a reference.

## What this predicts about the next detector

Both patterns that pay share a property: **the derived column is
high-cardinality.** `report_location` has as many distinct values as there are
distinct coordinate pairs. That is why removing it wins.

A *low*-cardinality functional dependency — `status_code` determines
`status_description`, `zip` determines `city` — should be worth much less here,
because **this codec already exploits it**: `pick_parents` scores conditional
entropy and sorts rows by the parent, which collapses the dependent column into
runs. A perfect low-cardinality dependency is close to free already.

That is a prediction, and TEST-17 exists to test it rather than assume it.
