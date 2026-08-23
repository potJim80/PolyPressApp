# TEST-5 — row-major number-encoding, one xz stream

Run 2026-08-05, on Mahdi's proposal: *forget columnar reorganisation. Walk row
by row, left to right; every non-numeric cell becomes a number from a growing
dictionary; numeric cells pass through verbatim. Feed each row into the SAME
xz stream. Chunk by row.*

13 tables, 60k rows each, all variants decoded and compared cell by cell.

## Variants

| | what |
|---|---|
| `xz raw` | plain `xz -9e` on the CSV (written with `csv.writer`, properly quoted) |
| **B row** | the proposal — number-encoded, row-major |
| C col | **identical ids**, transposed. The only difference from B is layout |
| polypress | `fast.encode` on the same rows |

## Size

| | total | vs xz | vs polypress |
|---|---|---|---|
| `xz raw` | 15,557,418 | 1.00 | |
| **B row-major** | **16,744,415** | **1.08** | **1.39** |
| C col-major | 16,712,175 | 1.07 | 1.39 |
| polypress | 12,033,070 | 0.77 | 1.00 |

**Loses to plain xz by 8%, and wins outright on 4 of 13** — best `cdc_nndss`
at **0.66x**, then `nyc_baby_names` 0.68x, `wa_ev_population` 0.90x.
Number-encoding roughly pays for itself; it does not clearly profit.

## Row-major vs column-major — read the median, not the total

The aggregate `B/C = 1.00` is **byte-weighted and misleading**. Per table,
**column-major wins on 9 of 13, median 1.15x**. The total only ties because
the four row-major wins land on the biggest outputs (`nyc_311`,
`chicago_permits`) while the largest column-major win — `cdc_nndss` at
**2.28x** — is on a small one.

Layout matters a great deal, in both directions, and which direction depends
on the table. It is not a wash.

## Speed — this is where the idea earns its place

| table | MB | B enc | B MB/s | polypress | pp MB/s | speedup |
|---|---|---|---|---|---|---|
| cdc_nndss | 6.9 | 1.79s | 3.83 | 2.30s | 2.98 | 1.28x |
| seattle_fire911 | 7.2 | 1.44s | 5.02 | 5.25s | 1.38 | **3.64x** |
| wa_ev_population | 14.4 | 1.33s | 10.84 | 4.23s | 3.40 | **3.19x** |
| nyc_311 | 49.5 | 6.30s | 7.86 | 36.30s | 1.36 | **5.76x** |

**1.28x–5.76x faster than Polypress, and on two tables faster than plain `xz`
on the raw CSV** — because the number-encoded body is far smaller than the
text it replaced, so the expensive stage has less to chew.

No parent search, no double-encode guard, no O(columns²).

## Verdict

This is **a different point on the speed/ratio curve, not a better codec**:
roughly **1.39x the size for ~3.5x the speed**, plus properties Polypress does
not have — single pass, O(1) memory, chunkable by row, one dictionary spanning
the whole file.

## Still open

- **The right comparison is `stream.py`, not `fast.encode`.** `stream.py`'s
  blocking cost has never been measured (its header only claims "slightly
  worse"). A scheme at 1.39x with O(1) memory may well beat a blocked codec at
  1.25x that still needs a whole block resident. Nobody knows yet.
- Chunking was argued, not exercised — no chunked encode was actually run.
- Decode was verified for correctness but never timed.
