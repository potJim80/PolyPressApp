# TEST-11 — the shipping codec against the model family we never measured

Run 2026-08-06, forced by TEST-9: PPMd beat `xz -9e` by 10.3% on the raw CSV
text, and no context-mixing model has ever been in this project's competitor
set. One question had to be answered before anything else was built — **is
PPMd a threat to Polypress, or only to the streaming scheme?**

13 tables, 40,000 rows, the same canonical CSV as TEST-8/9. `fast.encode`
verified by `fast.decode`; every PPMd archive extracted and compared.

## Result — no. Polypress wins every table.

| | total bytes | vs `xz -9e` |
|---|---|---|
| `xz -9e` | 11,184,465 | 1.000 |
| PPMd order-8 | 10,794,243 | 0.965 |
| PPMd order-12 | 10,214,482 | 0.913 |
| PPMd order-16 | 10,219,144 | 0.914 |
| **best PPMd order per table** (an oracle — free choice) | 9,971,109 | 0.892 |
| **Polypress** | **8,715,379** | **0.779** |

**Polypress is 12.6% smaller than a per-table PPMd oracle, and wins 13 of 13.**

| table | xz -9e | best PPMd | polypress | pp/best |
|---|---|---|---|---|
| cdc_nndss | 129,427 | 156,036 | 24,575 | **0.157** |
| nyc_baby_names | 104,382 | 95,123 | 55,062 | **0.579** |
| seattle_fire911 | 525,052 | 551,505 | 350,554 | **0.636** |
| wa_ev_population | 571,854 | 457,956 | 327,609 | 0.715 |
| chicago_crimes | 1,220,318 | 1,222,019 | 940,201 | 0.769 |
| nyc_311 | 1,824,441 | 1,675,858 | 1,356,445 | 0.809 |
| noaa_gsoy_ord | 6,966 | 6,935 | 6,545 | 0.944 |
| usgs_quakes | 643,133 | 497,448 | 472,280 | 0.949 |
| noaa_gsoy_sea | 6,857 | 6,740 | 6,521 | 0.968 |
| nyc_collisions | 909,115 | 694,537 | 674,333 | 0.971 |
| usgs_quakes_deep | 622,584 | 479,706 | 466,369 | 0.972 |
| chicago_permits | 3,893,471 | 3,548,481 | 3,462,565 | 0.976 |
| austin_incidents | 726,865 | 578,765 | 572,320 | 0.989 |

## Read the margin, not just the sign

The win is **not uniform, and the shape of it is the finding**. On the five
tables where row reordering fires — `cdc_nndss`, `nyc_baby_names`,
`seattle_fire911`, `wa_ev_population`, `chicago_crimes` — Polypress is 1.3x to
6.4x ahead and PPMd is not close. On the other eight it is **1–5%**, and on
`austin_incidents` it is 1.1%.

Those eight are the numeric and text-heavy tables: exactly the genre CLAUDE.md
already calls "marginal". **A model with no idea that the file is a table gets
within 1% of us there**, which says the modelling on those tables is doing
almost nothing that a good byte-level context model does not already do.

## The cost side, which is not in our favour

    polypress encode   100.3 s
    xz -9e             32.0 s
    PPMd (per order)   ~7 s

**PPMd is roughly 14x faster than Polypress for 12.6% more bytes**, and 4.6x
faster than xz for 10.8% fewer. That is a real operating point and this project
has nothing on that curve — `fast.encode` is 1.9 MB/s and the streaming tier
(TEST-5/8) is ~4 MB/s but only reaches 0.969x.

## What this changes

1. **The headline survives.** Polypress beats PPMd on every table in this
   corpus, so adding PPM to the lineup does not overturn "478 of 500".
2. **The claim must still be restated.** It was measured against 17
   competitors that are *all* LZ-family, Huffman or block-sort. PPMd is now in
   `benchmarks/measure_one.py` so future sweeps carry it; anything quoted from
   an earlier sweep should say so.
3. **Where we are weak is now precisely located.** Not "numeric and text-heavy
   data" in the abstract — the eight tables where a context model ties us.
   That is where the next model has to earn its keep.

## Caveats

- "best PPMd" is an oracle over three orders picked per table after the fact.
  A shipping PPMd would pick one order or pay to try several; either is worse
  than this row. The comparison is deliberately generous to PPMd.
- PPMd memory capped at 256 MB (session budget). Deeper orders may improve.
- 7-Zip archives carry a few hundred bytes of container that the polypress
  blob does not. Immaterial except on the two 7 KB tables.
- 40,000-row cap, one corpus, all government open data.
