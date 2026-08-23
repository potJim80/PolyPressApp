# TEST-14 — kanzi under this repo's own harness

Run 2026-08-06. The research pass measured kanzi `-l9` at 25.3% below `xz -9e`
and beating Polypress on 7 of 13 tables, on files truncated to 10 MB with
Polypress driven outside this harness. This re-runs it on the standard
40,000-row canonical CSV, with `fast.encode` / `fast.decode` and every kanzi
archive decompressed and compared.

kanzi `-l9` is `EXE+RLT+TEXT+UTF+DNA & TPAQX` — a word-dictionary transform in
front of a PAQ-family context mixer. Apache-2.0, no dependencies, built from
source on this machine in under a minute.

## Result — the claim holds in shape, and the margin is uncomfortable

| | total | vs `xz -9e` | vs polypress | encode |
|---|---|---|---|---|
| `xz -9e` | 11,184,465 | 1.000 | 1.283 | 32.0 s |
| kanzi `-l7` | 10,644,453 | 0.952 | 1.221 | |
| **kanzi `-l9`** | **8,999,740** | **0.805** | 1.033 | **14.8 s** |
| **Polypress** | **8,715,379** | **0.779** | 1.000 | 100.4 s |
| min(polypress, kanzi) per table | 8,482,445 | 0.758 | 0.973 | |

**Polypress is 3.2% ahead overall and loses 6 of 13 tables, at 6.8x the encode
time.**

| table | kanzi -l9 | polypress | knz/pp |
|---|---|---|---|
| austin_incidents | 457,416 | 572,320 | **0.799** |
| noaa_gsoy_sea | 5,791 | 6,521 | 0.888 |
| usgs_quakes_deep | 416,236 | 466,369 | 0.893 |
| noaa_gsoy_ord | 5,967 | 6,545 | 0.912 |
| usgs_quakes | 438,692 | 472,280 | 0.929 |
| nyc_collisions | 641,332 | 674,333 | 0.951 |
| nyc_baby_names | 55,151 | 55,062 | 1.002 |
| chicago_permits | 3,517,357 | 3,462,565 | 1.016 |
| chicago_crimes | 989,300 | 940,201 | 1.052 |
| seattle_fire911 | 385,052 | 350,554 | 1.098 |
| nyc_311 | 1,580,190 | 1,356,445 | 1.165 |
| wa_ev_population | 430,604 | 327,609 | 1.314 |
| cdc_nndss | 76,652 | 24,575 | **3.119** |

**The split is LAW 3 again, in a third independent measurement.** kanzi wins
the numeric and smooth tables where reordering does not fire; Polypress wins
where it does, by up to 3.1x. A context mixer with no idea the file is a table
takes six of our thirteen.

## Composed with TEST-15

Cross-column stripping does not just add — it **changes which codec wins**.
`nyc_collisions` is a kanzi win at 641,332 against our 674,333; with the
cross-column transform we take it at 561,077.

| best-of, per table | total | vs xz | vs polypress |
|---|---|---|---|
| Polypress alone | 8,715,379 | 0.779 | — |
| + cross-column where it fires | 8,336,228 | 0.745 | −4.3% |
| + kanzi where it still wins | **8,136,564** | **0.727** | **−6.6%** |

## What to do with it

As a **measured fallback candidate** alongside xz and bzip2 it can only help —
invariant 2 holds by construction. The costs are real and belong in the same
sentence as the gain:

- a bundled third-party C++17 binary in the `.dmg`;
- byte-identity (invariant 1) would then depend on the Python encoder and
  `ppz_encode.c` agreeing about a third tool's output, so it is a same-commit
  change to both;
- ~15 s and ~1 GB per 35 MB table.

Validate against the 500-corpus before believing the 2.7%.

## Flaw in this run

The `bzip3` column reported 0 bytes on every table — the wrapper looked for the
output in the wrong place (the CLI writes `<name>.bz3`). That column is
meaningless here and was dropped from the tables above. It does not touch the
kanzi or Polypress numbers, which round-tripped exact.

Other caveats: 40,000-row cap; kanzi at `-b 16m`, which the research pass
measured as costing ~5% against `-b 32m` in exchange for ~90 MB less RSS; one
corpus, all government open data.
