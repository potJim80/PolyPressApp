# TEST-8 — store the dictionary IN the table

Run 2026-08-06, from THINKING-LOG Thought 11. 13 tables, 40,000 rows each,
every variant decoded and compared cell by cell before its bytes were counted,
plus 17 adversarial tables built to break the escaping.

**The proposal:** the dictionary need not be a block at the front of the file.
Walk left to right; the first time a value is seen, emit the value itself;
every later time, emit its id. The decoder rebuilds the table and the
dictionary in the same single pass, because by the time an id arrives the value
it names has already gone past.

## Result

| variant | total | vs `xz -9e` | vs B |
|---|---|---|---|
| `xz -9e` on the CSV | 11,184,465 | 1.000 | — |
| **B** out-of-band dictionary, row-major | 11,200,789 | 1.001 | 1.000 |
| **C** inline dictionary, row-major | 10,975,531 | 0.981 | **0.980** |
| **D** inline, single pass, no gate | 11,071,320 | 0.990 | 0.988 |
| **E** inline, single pass, causal gate | **10,835,749** | **0.969** | **0.967** |
| **F** inline, column-major | 11,304,151 | 1.011 | 1.009 |

**The inline dictionary is 2.0% smaller than the out-of-band one**, on 9 of 13
tables, and **E gives up the analysis pass entirely and is 3.3% smaller than B**
— same 0.969x that TEST-7 reached with a two-pass encoder and a shipped
alphabet block.

Per table:

| table | rows | cols | xz raw | B out-band | C inline | D 1pass | E 1p+gate | F col | C/B | E/xz |
|---|---|---|---|---|---|---|---|---|---|---|
| austin_incidents | 40000 | 19 | 726,865 | 691,010 | 693,603 | 718,942 | 694,330 | 625,493 | 1.004 | 0.955 |
| cdc_nndss | 40000 | 16 | 129,427 | 83,576 | 88,553 | 86,008 | 113,783 | **39,420** | 1.060 | 0.879 |
| chicago_crimes | 40000 | 22 | 1,220,318 | 1,184,047 | 1,177,392 | 1,214,407 | 1,204,434 | 1,298,269 | 0.994 | 0.987 |
| chicago_permits | 40000 | 116 | 3,893,471 | 4,037,527 | 3,963,770 | 3,973,116 | 3,925,049 | 4,222,386 | 0.982 | 1.008 |
| noaa_gsoy_ord | 69 | 102 | 6,966 | 7,756 | 7,517 | 7,662 | 7,676 | 6,368 | 0.969 | 1.102 |
| noaa_gsoy_sea | 79 | 106 | 6,857 | 7,872 | 7,350 | 7,523 | 7,521 | 6,354 | 0.934 | 1.097 |
| nyc_311 | 40000 | 44 | 1,824,441 | 1,814,040 | 1,733,885 | 1,763,355 | 1,648,084 | 2,086,042 | 0.956 | 0.903 |
| nyc_baby_names | 29685 | 6 | 104,382 | 70,442 | 72,842 | 72,834 | 93,726 | 61,152 | 1.034 | 0.898 |
| nyc_collisions | 40000 | 29 | 909,115 | 920,456 | 893,370 | 910,524 | 866,995 | 848,558 | 0.971 | 0.954 |
| seattle_fire911 | 40000 | 7 | 525,052 | 608,220 | 572,601 | 570,978 | 501,528 | 536,055 | 0.941 | 0.955 |
| usgs_quakes | 16190 | 22 | 643,133 | 643,179 | 637,666 | 627,417 | 631,779 | 518,385 | 0.991 | 0.982 |
| usgs_quakes_deep | 16707 | 22 | 622,584 | 617,391 | 615,308 | 612,518 | 614,461 | 494,217 | 0.997 | 0.987 |
| wa_ev_population | 40000 | 16 | 571,854 | 515,273 | 511,674 | 506,036 | 526,383 | 561,452 | 0.993 | 0.920 |

## What each variant isolates

B and C are identical in **every** respect except where the distinct strings
live — same column kinds, same 50% gate, same ids, same separators, same xz
settings. That pair alone answers the question asked. D and E then ask whether
the analysis pass can be dropped, which is the part of the idea that buys speed
rather than bytes.

- **D** decides a column's kind from its **first cell** and nothing else. A
  later non-numeric cell in a numeric column is an exception, marked and stored
  raw — the same shape `fast.py` uses.
- **E** adds a **self-synchronising gate**: after 64 rows, a dictionary column
  whose distinct/seen ratio exceeds 0.5 switches permanently to raw text. Both
  sides evaluate the rule at the top of each row from data already emitted or
  already decoded, so they cannot disagree. **This is the interesting part of
  the result** — the 50% gate, which TEST-7 measured as worth 8%, does not need
  a pre-pass or a header field. It can be re-derived by the decoder.

## Where inline wins and where it loses

**Wins on high-cardinality tables, loses on low-cardinality ones.**

| table | distinct strings | C/B |
|---|---|---|
| noaa_gsoy_sea (106 wide, 79 rows) | many, mostly-unique | 0.934 |
| seattle_fire911 | 16k–36k per column | 0.941 |
| nyc_311 | high | 0.956 |
| nyc_baby_names | ~2k total | 1.034 |
| cdc_nndss | ~3.6k total | 1.060 |

Mechanism, both directions:

- **Inline wins** when the alphabet is a large share of the output. A literal
  emitted in place sits next to the row it belongs to, so xz can match it
  against its neighbours; the block form also pays a second xz stream's
  startup and cannot share a window with the body.
- **Inline loses** when the alphabet is small and dense. Collected in a block,
  a few thousand short related strings sit adjacent and xz compresses them
  hard; inlining scatters them through the body, and each one arrives once,
  far from the others.

That is the same trade `stridexz` measured from the other side: **contiguity
is the whole effect**. Here it applies to the dictionary rather than to the
columns.

## Layout still matters, and it is bigger than the dictionary question

**F is worse in total and better on 6 of 13 tables, by very large margins.**
`cdc_nndss` is 39,420 column-major against 88,553 row-major — **2.25x**;
`usgs_quakes` 518,385 against 637,666. And it is worse on `nyc_311`
(2,086,042 vs 1,733,885) and `chicago_permits`.

So the first claim in the thought — *"row by row vs column by column should in
theory not really matter"* — is **false in practice by up to 2.25x, in both
directions**. The ids are the same numbers either way; what changes is which
ones land next to each other. Consistent with TEST-5, which measured B/C = 1.00
on aggregate while individual tables ranged 0.89–2.28.

## Cost

Encode time over the whole corpus:

| variant | seconds | vs B |
|---|---|---|
| F inline col | 18.0 | 0.80x |
| D 1-pass | 21.7 | 0.97x |
| B out-of-band | 22.4 | 1.00x |
| C inline | 23.3 | 1.04x |
| E 1-pass gate | 26.7 | 1.19x |

**Inlining the dictionary is free** (C is 1.04x of B, and D — which also drops
the analysis pass — is *faster* than B). E's 1.19x is the per-row gate loop
written in Python over every column of every row; it is not inherent, and it
buys the largest size win. The claim that *"dictionary building is expensive"*
is not visible at this scale: TEST-7 profiled dictionary building at 8.2% of
encode time and 73.5% in xz.

## Caveats

- Ids are **decimal ASCII**, not varint, in all variants — the body is text so
  the framing needs it. TEST-7's 0.969x used varint ids, so the numbers here
  and there are not directly comparable; B is the like-for-like baseline.
- **40,000 rows**, 13 tables, one corpus. The gate constants (0.5, 64 rows) are
  taken from TEST-6/7 and not tuned here.
- No differencing (TEST-6) and no cross-column work. This is the dictionary
  question alone.
- C and F assign ids in emission order, so the *same* scheme in the two layouts
  produces different ids. That is inherent to inlining, not a confound.

## Addendum — the straight comparison against plain xz

`time_vs_xz.py`, same corpus, same 40,000-row cap. Variant E (inline
dictionary, single pass, causal gate) against `xz -9e` on the canonical CSV.
Sizes are whole archives; times are wall clock on this machine, one thread.

| | plain `xz -9e` | variant E | E / xz |
|---|---|---|---|
| bytes | 11,184,465 | **10,835,749** | **0.969** — 3.1% smaller |
| encode | 39.4 s | **33.0 s** | **0.84** — 16% faster |
| decode | 0.41 s | 12.70 s | **31.2** — 31x slower |
| encode throughput | 3.05 MB/s | 3.65 MB/s | |
| decode throughput | 295 MB/s | 9.5 MB/s | |

Won on size on 9 of 13; lost on `chicago_permits` (+0.8%) and the two 69-row
`noaa_gsoy` tables (+10%), where the header is a large share of a 7 KB file.

**Encode is faster because xz is given less to chew on** — the dictionary-coded
body is a fraction of the CSV, and both sides call the same liblzma, so the
comparison is fair. **Decode is 31x slower because it is per-cell Python**: xz
hands back a block, while E walks every cell to rebuild the dictionary and
resolve ids. That gap is implementation, not design — `fast.py` vs `csrc/`
shows the same shape — but it is unmeasured in C and should be stated as
unknown rather than assumed small.
