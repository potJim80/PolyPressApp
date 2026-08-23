# TEST-17 — functional dependencies, and the reorder that already had them

Run 2026-08-06. Tests a prediction written down in TEST-16's README **before**
the measurement, and a research pass's top-ranked recommendation.

The research pass measured unary functional dependencies (`iucr → description`,
`crime_type → ucr_code`, `city → state`) on this corpus and found detection
costs 0.02–1.2 s per table and stripping the determined columns is worth
**12.7% / 7.6% / 4.3% / 3.7% against `xz -9e`** on four tables. It ranked this
the #1 thing to build, and — to its credit — named the experiment that could
kill it: run the strip through the real codec first.

## Result — through `fast.encode` the gain is 0.5%, and negative on 5 of 10

| table | cols | FDs | dropped | pp full | pp stripped | maps | **gain** | (vs xz) |
|---|---|---|---|---|---|---|---|---|
| austin_incidents | 19 | 4 | 2 | 572,320 | 522,486 | 4,070 | **+8.7%** | +4.3% |
| chicago_crimes | 22 | 4 | 4 | 940,201 | 936,409 | 5,781 | +0.4% | +7.6% |
| usgs_quakes_deep | 22 | 4 | 2 | 466,369 | 465,603 | 131 | +0.2% | +0.8% |
| usgs_quakes | 22 | 1 | 1 | 472,280 | 471,909 | 73 | +0.1% | — |
| nyc_311 | 44 | 4 | 2 | 1,356,445 | 1,358,528 | 384 | −0.2% | +0.6% |
| chicago_permits | 116 | 177 | 9 | 3,462,565 | 3,469,838 | 4,299 | −0.2% | −0.1% |
| wa_ev_population | 16 | 9 | 5 | 327,609 | 335,541 | 10,938 | −2.4% | +3.7% |
| noaa_gsoy_ord | 102 | 180 | 8 | 6,545 | 6,717 | 231 | −2.6% | — |
| noaa_gsoy_sea | 106 | 198 | 11 | 6,521 | 6,740 | 268 | −3.4% | — |
| **cdc_nndss** | 16 | 11 | 4 | **24,575** | **25,745** | 1,487 | **−4.8%** | **+12.7%** |
| **TOTAL** | | | | 7,635,430 | 7,599,516 | | **+0.5%** | |

**The row reordering already has the functional dependencies.** `pick_parents`
scores conditional entropy — which is exactly **zero** for a hard FD — then
sorts the rows by that parent, collapsing the dependent column into runs.
Shipping an explicit map instead is redundant, and pays for the map twice.

`cdc_nndss` is the cleanest refutation: **+12.7% against xz becomes −4.8%
against us**, and it is the table where reordering wins 6.4x, so the overlap is
total. The prediction in TEST-16's README holds.

## The exception is real and worth its own work

**`austin_incidents` gains 8.7% — more through Polypress than through xz
(+4.3%).** It is the one table with several *independent* FDs
(`crime_type→ucr_code`, `ucr_category→category_description`, …), and LAW 1 says
Polypress pays for **one row order shared across all columns**. One sort
collapses one FD chain; the other three go unexploited, and stripping them
recovers exactly that.

It is also one of the six tables kanzi beats us on (0.799), and TEST-18 found
`ucr_code` and `crime_type` carrying 58% of that gap between them. Three
measurements pointing at the same table for the same reason.

**That points at the real fix, which is not FD stripping**: composite
(multi-key) sort parents. `pick_parents` returns one parent per column and
`np.lexsort` appears nowhere in `fast.py`. Sorting by `(A, then B)` costs zero
bytes for the same reason sorting by `A` does. Recorded as candidate C1 in
`THINKING-LOG.md` and still unmeasured.

## Three bugs, all in the same family as TEST-15's

1. **A bijection determines in both directions**, so collecting every pair and
   then filtering drops *nothing* — each side disqualifies the other.
   Committing one pair at a time breaks the tie.
2. **A determinant chosen early can be dropped later as someone else's
   dependent**, leaving the first pair with no source — a `KeyError` on real
   data. Same fixed-point resolution as TEST-15, same shape the `turbo` branch
   fixed in `12fb00d`.
3. **The near-key trap**, named by CORDS (SIGMOD 2004) and rediscovered by the
   research pass on this corpus: `latitude → longitude` holds with 28,656
   distinct values in 40,000 rows, and the map costs as much as the column. The
   gate is `card(A)/n ≤ 0.25`, not the FD itself.

## Caveats

- Exact FDs only. An *approximate* FD holding on 39,922 of 40,000 rows is found
  by the same pass and was not tested; TEST-15 went from 0 tables to 3 by
  exactly that relaxation, so these counts are a floor.
- One determinant per dependent, coarsest first. A cleverer choice exists.
- 40,000-row cap, 13 curated tables.
