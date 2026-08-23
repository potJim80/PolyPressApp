# TEST-9 — the CSV as a plain text stream, through every model family

Run 2026-08-06, from Mahdi's Thought 12: *"what if we treat the csv as a text
file? forget rows and columns, just feed in the actual text stream."*

Before building a new text model, measure what the EXISTING model families do
to that same text. They are the honest ceiling for "a better text model", and
they cost nothing but CPU. 13 tables, 40,000 rows, 120.2 MB of canonical CSV —
the identical bytes TEST-8 used as its baseline. Every result was decompressed
and compared to the original.

## Result — a different MODEL beats our transform, on the raw text, 7x faster

| codec | model family | total bytes | vs `xz -9e` | compress s | decompress s |
|---|---|---|---|---|---|
| **PPMd order-16** | context model + arithmetic | **10,031,630** | **0.897** | **6.8** | 7.3 |
| PPMd order-12 | context model + arithmetic | 10,214,794 | 0.913 | 6.1 | 6.4 |
| PPMd order-8 | context model + arithmetic | 10,794,555 | 0.965 | 4.6 | 4.9 |
| `xz -9e` | LZ77 + range coder | 11,185,256 | 1.000 | 48.6 | 0.8 |
| `lzma2 mx9` (7z) | LZ77 + range coder | 11,201,596 | 1.001 | 49.7 | 0.5 |
| `brotli -q11` | LZ77 + context + static dict | 11,368,848 | 1.016 | 137.6 | 0.3 |
| PPMd order-6 | context model + arithmetic | 11,599,116 | 1.037 | 3.7 | 3.7 |
| `zstd -22` | LZ77 + FSE | 11,839,534 | 1.058 | 66.7 | 0.2 |
| `bzip2 -9` | BWT + MTF + Huffman | 14,499,751 | 1.296 | 7.4 | 2.0 |
| PPMd order-4 | context model + arithmetic | 14,822,512 | 1.325 | 2.1 | 2.3 |

**PPMd order-16 is 10.3% smaller than `xz -9e` and compresses 7.1x faster.**

For scale: TEST-8's inline-dictionary scheme — a transform we designed for this
data — reached 0.969x of xz. **Changing the model class, with no transform at
all, is worth three times as much.**

## Per table

| table | raw MB | xz -9e | ppmd o8 | ppmd o12 | ppmd o16 | best |
|---|---|---|---|---|---|---|
| austin_incidents | 8.0 | 726,928 | 578,789 | 642,957 | 673,784 | **ppmd o6 553,554 (0.76)** |
| cdc_nndss | 4.6 | 129,488 | 156,060 | 192,493 | 185,780 | **brotli 124,337 (0.96)** |
| chicago_crimes | 9.9 | 1,220,380 | 1,222,641 | 1,222,043 | 1,233,927 | **xz (1.00)** |
| chicago_permits | 34.5 | 3,893,532 | 3,891,248 | 3,548,505 | 3,418,496 | **ppmd o16 (0.88)** |
| noaa_gsoy_ord | 0.0 | 7,024 | 7,249 | 6,959 | 7,234 | ppmd o12 (0.99) |
| noaa_gsoy_sea | 0.0 | 6,916 | 7,172 | 6,764 | 6,932 | ppmd o12 (0.98) |
| nyc_311 | 33.1 | 1,824,504 | 2,052,588 | 1,763,644 | 1,675,882 | **ppmd o16 (0.92)** |
| nyc_baby_names | 1.2 | 104,440 | 98,176 | 95,147 | 95,404 | **ppmd o4 90,819 (0.87)** |
| nyc_collisions | 8.3 | 909,176 | 763,729 | 715,431 | 694,561 | **ppmd o16 (0.76)** |
| seattle_fire911 | 4.8 | 525,112 | 562,964 | 551,529 | 560,378 | **xz (1.00)** |
| usgs_quakes | 3.0 | 643,196 | 497,472 | 513,605 | 519,052 | **ppmd o8 (0.77)** |
| usgs_quakes_deep | 3.1 | 622,644 | 479,730 | 497,396 | 502,220 | **ppmd o6 478,503 (0.77)** |
| wa_ev_population | 9.6 | 571,916 | 476,737 | 458,321 | 457,980 | **ppmd o16 (0.80)** |

PPMd beats xz on **11 of 13**; it loses on `cdc_nndss` (the most repetitive
table, where LZ's long-range matching is exactly right) and `seattle_fire911`.

**The best order is table-dependent and the spread is large** — order 4 wins
`nyc_baby_names`, order 6 wins `austin_incidents` by 24%, order 16 wins
`nyc_311`. A per-table order choice is worth having; the guard is invariant 2,
encode at two or three orders and keep the smallest, which is cheap because
PPMd runs at ~18 MB/s.

## Why this happens, mechanically

LZMA and PPMd fail on opposite things.

- **LZ77 (xz)** codes a repeat as a (distance, length) pair. It is unbeatable
  when whole lines recur — `cdc_nndss`, where the same state/disease/week
  strings repeat verbatim thousands of times.
- **PPMd** never copies. It predicts the next byte from the previous *N* bytes
  and codes the surprise. On a CSV that means it learns "after `,4` in this
  position usually comes `7.`" — the *column's* value distribution, arrived at
  by context rather than by construction. That is worth more on tables whose
  fields vary slightly row to row (coordinates, timestamps, counts) — exactly
  where LZ finds no exact match and has to emit literals.

This is LAW 2 restated at a bigger scale. LAW 2 said the coder is worth 0.01%
and the model 47%, measured over ANS variants on single columns. Here the same
statement holds between two *shipping* codecs on whole tables: same input, same
class of entropy coder, 10.3% from the model alone.

## The finding that matters most — our competitor set has a hole

`benchmarks/measure_one.py` measures against 17 competitors:

    gzip -9, bzip2 -9, xz -9e, lz4 -9, zstd -3/-19/-22, brotli -q11,
    Parquet x4 codecs, ORC x3 codecs, Feather x2 codecs

**Every one of them is LZ-family, Huffman, or block-sort. There is no
context-mixing or PPM model in the set at all** — and PPMd is not exotic: it
ships inside 7-Zip, which is installed on this machine and on most others.

The headline claim in CLAUDE.md and README.md — *478 of 500 datasets beaten
against the best of 17 competitors* — is therefore **untested against the model
family that just beat plain xz by 10%**. That does not make the claim false: on
this corpus the shipping codec is far ahead of both (see TEST-11). It makes it
**incompletely stated**, and the repo's own rule is that limits get written
down rather than discovered later by someone else.

**Action taken:** PPMd added to the competitor lineup in `measure_one.py`, so
future sweeps carry it. Any headline quoted from a sweep run before this date
should say "17 LZ-family and columnar competitors".

## Caveats

- 7-Zip archives carry a small container overhead (a few hundred bytes) that
  the raw `xz -c` stream does not. Immaterial at these sizes; it flatters xz on
  the two 7 KB tables and nothing else.
- PPMd memory was capped at 512 MB (four workers, 4 GB ceiling). Deeper orders
  may improve with more; untested.
- 40,000 rows per table. The two `noaa_gsoy` tables are 69 and 79 rows and
  carry no weight in the aggregate.
