# TEST-7 — one global dictionary, and where the row-major scheme leaks

Run 2026-08-05. Two questions, both from Mahdi.

## Q1 — does the xz stream stay continuous if you feed it row by row?

**Yes, at a cost of 2 bytes in 104,380 — 0.002%.**

| | bytes |
|---|---|
| whole body, one `compress()` call | 104,380 |
| fed one row at a time, 29,685 separate calls | 104,382 |

LZMA's window persists across `LZMACompressor.compress()` calls, so row 1's
patterns are still matchable when row 29,685 arrives. **This is what makes the
row-major architecture work** — O(1) memory *and* full dictionary reach, which
is exactly what `stream.py` gives up. The only limit is the 64 MiB window.

## Q2 — one global dictionary instead of one per column?

Ids assigned in row-major first-encounter order, across all columns.

| variant | total | vs xz |
|---|---|---|
| global, everything dictionary-coded | 2,382,111 | 1.207 |
| global, numbers verbatim, ids sigil-marked | 2,112,228 | **1.070** |
| global, numbers verbatim, per-column kinds | 2,146,314 | 1.087 |
| **per-column dictionaries** | **1,912,249** | **0.969** |
| `xz -9e` | 1,973,848 | 1.000 |

**Not dictionary-encoding the numbers is worth 11%** (1.207 → 1.070).
`cdc_nndss` nearly halved.

**But the global dictionary still loses to per-column by 10%.** The sharing is
real and tiny — 3,628 global entries vs 3,721 per-column, 2.5% — and it is
swamped by id length. A per-column dictionary for `gndr` uses ids `0`-`1`, one
character; a global one puts the same values at arbitrary positions, so every
cell costs 4-5 digits. The body is far bigger than the dictionary, so it loses.

**Only per-column beats plain xz.**

Note the `sigil` variant beat `kinds` (1.070 vs 1.087) despite paying a byte
per id, because `kinds` decides per *column* with `all(NUM.match(...))` — one
non-numeric cell in 40,000 dumps the whole column into the dictionary. **The
all-or-nothing gate from CLAUDE.md, reappearing in a scheme built from scratch
the same afternoon.** Worth 1.6% here.

## Where the per-column scheme leaks — profiled

### Size

**The dictionary is 49% of the output on `seattle_fire911` (1.14x, the worst
table) and 8% on `wa_ev_population` (0.90x, a winner).** Cause: columns with
near-unique values get dictionary-coded anyway.

| `seattle_fire911` column | distinct / 40,000 rows |
|---|---|
| `incident_number` | **40,000** — every value unique |
| `datetime` | 36,122 |
| `address` | 16,603 |

Dictionary-coding a unique key is pure loss: every string stored *and* an id
pointing at it. The 50% gate fixes it, measured at 8%.

**The same point is stored three times in that file:**

```
latitude          '47.52331'
longitude         '-122.272834'
report_location   'POINT (-122.272834 47.52331)'
```

Identical precision. Per-column share: latitude 22%, longitude 22%,
report_location 22% — **66% of the table for one point's worth of
information.** This is the cross-column redundancy backlog item (40/100 tables,
best 47%), sitting in the middle of the worst benchmark table, unexploited.

`address` at 21% is `'4800 S Henderson St'` — backlog item 1, the
skeleton/number split. On the winner, `dol_vehicle_id` is 29%, a numeric unique
id with no differencing.

### Time

| stage | share |
|---|---|
| **xz body** | **73.5%** |
| emit tokens | 13.0% |
| build dict | 8.2% |
| xz dict | 2.9% |
| classify | 2.4% |

Three-quarters is xz, which TEST-4 says cannot be removed. The rest is linear
Python string work. **There is no O(columns²) term and no double encode** —
the contrast with `fast.encode`, and why this scheme runs 1.28-5.76x faster.
The time is already near the floor for this design; the size is what leaks.
