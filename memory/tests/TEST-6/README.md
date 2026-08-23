# TEST-6 — anchor rows plus differences

Run 2026-08-05, on top of TEST-5's row-major scheme. Proposal: keep a few
anchor cells holding the true encoded number, store every other cell as a
difference.

Differencing is **down the column**, not across the row — `zip` minus
`incident_type` means nothing. Anchors are whole rows at a fixed interval,
which makes them pay twice: they bound error propagation *and* they are
exactly where a chunk can start. 5 tables, 40k rows, every variant decoded and
compared before counting.

## Result

| table | xz raw | abs (control) | a=256 | a=1024 | delta-all | best/xz | polypress |
|---|---|---|---|---|---|---|---|
| cdc_nndss | 129,427 | 98,368 | 46,404 | 44,309 | **43,311** | **0.33** | 24,575 |
| nyc_baby_names | 104,382 | 68,081 | 65,159 | 64,503 | **64,335** | **0.62** | 55,062 |
| wa_ev_population | 571,854 | **502,531** | 688,447 | 688,161 | 688,002 | 0.88 | 327,609 |
| seattle_fire911 | 525,052 | **497,340** | 610,305 | 609,884 | 609,692 | 0.95 | 350,554 |
| usgs_quakes | 643,133 | **625,437** | 636,744 | 636,666 | 636,663 | 0.97 | 472,280 |
| **TOTAL** | 1,973,848 | 1,791,757 | 2,047,059 | 2,043,523 | 2,042,003 | **0.878** | 1,230,080 |

**Applied globally it is a NET LOSS** — every anchor variant totals worse than
the control. It helps enormously on 2 tables (`cdc_nndss` −55%) and hurts
badly on 3 (`wa_ev_population` +37%).

## The predictor is clustering, not column kind

A prediction was made before the run — that differencing would help numeric
columns and hurt dictionary ids — and it was **wrong**. `cdc_nndss` is the most
categorical table in the corpus and the biggest winner.

The real predictor is whether **the file already arrives row-clustered**.
`cdc_nndss` is sorted by state/year/week, so consecutive rows are nearly
identical and deltas collapse to runs of zeros. `wa_ev_population` is not
sorted, so deltas are noise where the original ids were at least repetitive.

Same property `probe_permfloor.py` found was worth 6x over the enumerative
floor: *real tables arrive clustered*.

## Anchor interval is nearly free

256 → 1024 → all-delta moves about 1%. **Anchors can be placed wherever
chunking wants them without paying for it** — the useful result for a
streaming design.

## Next

Per **column**, not per table. On `wa_ev_population` some columns almost
certainly want differencing even though the table does not. Invariant 2 at the
right granularity: encode both ways, keep the smaller. Not run.
