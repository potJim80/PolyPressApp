# Polypress

A lossless compressor for data tables.

> **This repository holds two projects.** Polypress is the one documented
> below, at the repository root. [`handrail/`](handrail/) is the other — a
> native macOS app that writes R into an RStudio script — with its own README
> and its own history. It is developed in a separate repository and merged in
> here by [`scripts/sync-app.sh`](scripts/sync-app.sh), so **editing
> `handrail/` in this checkout is not how changes get made**: the next sync
> merges across them.

> **Repository layout changed on 2026-08-04.** The code now lives in `work/`,
> the corpora in `IN/`, and sweep output in `OUT/results/`. **Every command in
> this README is run from `work/`** — `cd work` first. That is why data paths
> below read `../IN/…` and `../OUT/…`. Full detail, including every file edited
> and how to undo it, is in [`memory/RESTRUCTURE-2026-08-04.md`](memory/RESTRUCTURE-2026-08-04.md).

> **One pass, since 2026-09-29 -- read this before any number below.** The
> encoder no longer tries a table several ways and keeps the smallest, and no
> longer checks the result against plain xz and bzip2 of the whole table.
> Mahdi's call, after a 6.8 GB table took over an hour: "one algorithm to
> reorder, one compression algorithm". Measured on the 39-table suite first:
> the trials and the check cost **2.3x the encode time (3x on one core, where
> the plain-xz check alone was 69%) for 2.5% smaller output**. So the
> **"never worse than plain xz" guarantee described below is retired**: on
> the suite the one-pass archive is still smaller than `xz -9e` on 38 of 39
> tables and 61 bytes (0.003%) bigger on the 39th, a table of random base64,
> but that is now a measurement, not a promise.
> The 500-table headline and the other sweep results below were produced by
> the earlier encoder, with the guarantee; they have not been re-run.
> Details: [One pass](#one-pass).

## The headline: 500 datasets nobody chose

The obvious objection to any compression result is **"you picked the files"**,
and there is no way to answer it by picking more files. So the main benchmark
does not pick.

`benchmarks/fetch_socrata100.py` asks the Socrata open-data catalog — the index
behind several hundred government portals — for its datasets **in descending
order of page views**, and takes the first N that survive four mechanical
filters: the CSV downloads, it has at least 2 columns and 20 rows, it is at
least 50 KB, and it is not a byte-identical duplicate of one already taken.
Rank order is public and fixed, so the list reproduces. Nothing is skipped for
what is in it, and **every rejection is recorded with its reason** in the
manifest.

That gives **500 tables, 3.57 GB of CSV, 14.3 million rows, 10,557 columns**,
measured against **17 competing codecs** (plus two re-finishes of our own
output, which are not competitors).

**That seventeen is the lineup as it stood in August 2026, and it is quoted
here unchanged rather than restated at today's count.** The lineup is **22**
now: `zip -9` and `7z` at LZMA2 and at PPMd orders 6/12/16 were added
afterwards, and PPMd in particular turns out to be the strongest rival this
codec has — see [the shape suite](#the-shape-suite-and-the-rival-that-actually-wins)
below. The 500-table numbers were not re-measured against it; they are a
17-competitor result and are labelled as one.

```bash
cd work
python3 benchmarks/fetch_socrata100.py ../IN/corpus500/ --count 500 --scan-limit 3000
./benchmarks/run_sweep_500.sh
```

| | result |
|---|---|
| **Round-trips exactly** | **500 of 500** |
| **Smaller than the best of all 17 competitors** | **478 of 500 (96%)** |
| Margin over the best other tool | median **1.25x**, best **2.73x**, worst 0.65x |
| Whole corpus, aggregate | 222,835,247 B vs 280,201,750 B — **1.26x smaller** |
| Compression vs raw CSV | median **14.35x**, best 231.78x, worst 3.49x |
| Peak memory, whole sweep | 1,148 MB |

Beaten by the *best of seventeen* on 478 tables out of 500 is the honest
headline, because someone storing a table uses the best tool they have, not
the average one. Against each competitor individually it is stronger:

| competitor | polypress wins | median | worst |
|---|---|---|---|
| `parquet+zstd` | **449/450** | 1.71x | 0.74x |
| `parquet+brotli` | **449/450** | 1.64x | 0.68x |
| `parquet+gzip` | **449/450** | 1.87x | 0.81x |
| `parquet+snappy` | **450/450** | 2.56x | 1.04x |
| `orc+zstd` | 435/436 | 2.00x | 0.65x |
| `orc+zlib` | 435/436 | 2.11x | 0.71x |
| `feather+zstd` | **450/450** | 3.36x | 1.34x |
| **`xz -9e`** | **500/500** | 1.30x | 1.00x |
| **`zstd -22 ultra`** | **500/500** | 1.39x | 1.02x |
| **`gzip -9`** | **500/500** | 2.24x | 1.12x |
| **`lz4 -9`** | **500/500** | 2.65x | 1.37x |
| `bzip2 -9` | 491/500 | 1.46x | 0.98x |
| `brotli -q 11` | 487/500 | 1.32x | 0.91x |

(The counts differ because pyarrow could not read every CSV: 50 files defeated
its Parquet/Feather reader and 64 its ORC writer. Those datasets keep their
general-purpose competitors and lose their columnar ones.)

**It beat plain `xz`, `zstd -22`, `gzip` and `lz4` on all 500** — that is the
"never worse" guarantee holding, and it only holds because a defect found by
the 100-dataset version of this corpus was fixed. All 22 losses are listed in
full under [Where it loses](#where-it-loses), because a compressor whose
failure cases are unknown is one nobody should trust with their data.

### The nine losses to `bzip2 -9`, and what the guarantee actually says

Worth being precise, because it looks like the guarantee failing and is not.
`bzip2` **is** one of the fallback candidates, and on those nine tables
Polypress did ship its own bzip2 fallback and did beat it. What beat *us* was
`bzip2` run on the **original file**, and the fallback compresses the table
re-rendered through Python's `csv.writer`.

On `data_current_sla_pending_licenses` the canonical rendering is 565,806 bytes
against the original's 645,280 — **79 KB smaller as text** — and yet
`bzip2(original)` is 107,692 against `bzip2(canonical)` 107,809. Normalising
the quoting removed redundancy the Burrows-Wheeler transform had been
exploiting.

So the guarantee was **"never worse than our own plain fallback"**, not "never
worse than any tool run on your original bytes". The gap was 0.1%–1.6% on nine
of 500 tables. (The fallback and the guarantee were both retired in 2026-09
for a one-pass encoder; this is kept as the record of what the guarantee
meant.)

### Parquet did not reproduce the data on 356 of the 500 tables

Worth stating before any size comparison. Parquet read with type inference —
the way a data engineer actually reads a CSV — gave back **the exact printed
text on only 81 of the 500 datasets**. It changed it on 356, and the check
could not run on 63.

Turning `"1.50"` into `1.5`, or `007` into `7`, makes a smaller file for a
reason that has nothing to do with compression. Polypress guarantees the exact
printed cell. So on **71% of this corpus** the Parquet columns above are
**flattering to Parquet**, and it still loses all but one of them.

### Is the win the modelling, or just a better final compressor?

The sharpest objection, and it deserves a direct answer. Polypress finishes
with xz, and **Parquet cannot use xz at all** — pyarrow answers
`Unsupported compression: xz`. Its options are snappy, gzip, brotli, zstd and
lz4. So part of the margin above could be nothing but a better finisher.

It is not. Re-finishing the *same modelled streams* with the competitor's own
entropy coder, across all 500 datasets:

| like for like | polypress wins | median | worst |
|---|---|---|---|
| `polypress+zstd` vs `parquet+zstd` | **449/450** | 1.57x | 0.74x |
| `polypress+zstd` vs `orc+zstd` | 430/436 | 1.83x | 0.66x |
| `polypress+zstd` vs `feather+zstd` | **450/450** | 3.02x | 1.24x |
| `polypress+brotli` vs `parquet+brotli` | **449/450** | 1.63x | 0.68x |
| `polypress+zstd` vs plain `zstd -22` | 490/500 | 1.27x | 0.96x |
| `polypress+brotli` vs plain `brotli -q 11` | 493/500 | 1.31x | 0.95x |

**Strip xz out entirely and the gap barely moves. The win is the modelling.**

Read the other way, this also prices the speed trade: finished with zstd
instead of xz, Polypress is a few percent larger and several times faster, and
still beats Parquet on every table.

### All four corpora together

The 500 unselected tables are the claim. The other three corpora exist to
attack it from directions the catalog cannot: a hand-picked set spanning
deliberately different *shapes*, the matrix-shaped tables the planar predictor
was built for, and ten tables written specifically to break it.

| corpus | datasets | wins | median margin | worst |
|---|---|---|---|---|
| **Socrata 500** (unselected) | 500 | **478 (96%)** | 1.25x | 0.65x |
| Curated (by shape) | 13 | **13** | 1.35x | 1.13x |
| Matrix-shaped | 3 | **3** | 1.92x | 1.67x |
| Adversarial (built to break it) | 10 | 5 | 1.00x | 0.92x |
| **all** | **526** | **499 (95%)** | **1.25x** | 0.65x |

**526 of 526 round-trip to the exact input.** Peak memory across the 500-table
sweep was 1,148 MB, with inputs truncated at 16 MB — lower than the earlier
28 MB ceiling, so the absolute byte totals here are **not** comparable with
those of the 100-table sweep, though every codec was still handed the identical
truncated file. The adversarial row is meant to be the bad one:
those tables are random text, UUIDs and base64 by construction, and a tie there
is the correct outcome.

Full records: `../OUT/results/socrata100-summary.txt`, `../OUT/results/curated13-summary.txt`,
`../OUT/results/matrix-summary.txt`, `../OUT/results/hostile-summary.txt`,
`../OUT/results/all-summary.txt`, and every individual measurement in
`../OUT/results/all-results.csv`.

---

## The shape suite, and the rival that actually wins

The 500 unselected tables answer *does it win*. They cannot answer *where*, and
an aggregate over 500 government tables hides the thing that matters most about
this codec: it is 2.3x on densely-coded categorical data and 1.00x on random
text. Those average to a number true of neither.

So there is a second corpus, small and **tagged**. `benchmarks/make_suite.py`
builds `IN/suite/` in 25 seconds out of the corpora already downloaded: **39
tables, 267 MB**, in four size tiers and tagged by *form* — categorical, text,
geo, matrix, scientific, ids, sparse, wide, narrow, unicode, redundant,
sorted/shuffled, hostile. It is seeded and sha256'd into a committed
`MANIFEST.json`, so the data itself stays out of git and a number is still
traceable to the exact bytes that produced it. This is the run to do after a
codec change; the Socrata sweeps take hours and cannot say where a change
helped.

```bash
cd work
./benchmarks/run_suite.sh    # build, sweep, report -- ~20 min
```

| | suite v2, 2026-10-01 | suite v1, 2026-08-28 |
|---|---|---|
| **Round-trips exactly** | **39 of 39** | 39 of 39 |
| **Smaller than the best of 22 competitors** | **30 of 39 (77%)** | 30 of 39 |
| Margin over the best other tool | median **1.17x**, best 4.22x, worst 0.91x | 1.16x, 4.22x, 0.91x |
| Whole suite, aggregate | 21,691,824 B vs 26,736,431 B — **1.23x smaller** | 22,306,557 B — 1.20x |
| Compression vs raw CSV | median **11.00x**, best 225.78x, worst 1.30x | 11.09x, 225.78x, 1.32x |
| Polypress encode time, whole suite, one thread | **35 s** | 152 s |

v1 was the Python encoder with its trial encodes and the never-worse-than-xz
check; v2 is the one-pass C program (2026-09-29) plus derived columns
(2026-10-01). One pass costs about 2.5% in size, derived columns take back
5.1%, and the encode is 4.3x faster. The geo tables gained most (median 1.13x
→ 1.30x over the best competitor); matrix lost a little (1.85x → 1.77x) to
one pass.

The breakdown is the point, not that line. **Never quote the aggregate without
it:**

| form | n | wins | median vs best | median vs CSV |
|---|---|---|---|---|
| matrix | 4 | **4/4** | **1.77x** | 10.66x |
| timestamps | 13 | 10/13 | 1.53x | 13.42x |
| geo | 9 | 8/9 | 1.30x | 15.40x |
| sparse | 11 | 10/11 | 1.20x | 17.32x |
| categorical | 19 | 16/19 | 1.20x | 19.32x |
| numeric | 15 | 12/15 | 1.16x | 6.26x |
| **text** | 10 | **5/10** | **1.01x** | 14.86x |
| **wide** | 5 | **3/5** | **1.04x** | 7.62x |
| **hostile** | 10 | **5/10** | **0.99x** | 2.50x |

A file carries several tags, so the rows overlap. And the four `nndss` rungs
are **one table at four sizes** — a scaling axis, not four wins; the report
prints them apart from everything else for exactly that reason.

That table is the codec in nine lines: **it wins on structure and ties on
entropy.**

### The rival is a context model, not a columnar format

Best non-Polypress result per table, over all 39:

| | | | |
|---|---|---|---|
| `7z ppmd` o6 / o12 / o16 | **8 + 7 + 6 = 21** | `xz -9e` | 3 |
| `orc+zstd` | 7 | `parquet+brotli` | 2 |
| `7z lzma2 -mx9` | 4 | `brotli -q11`, `bzip2 -9` | 1, 1 |

**Parquet at four codecs was never once the table to beat**, and seven of the
nine losses are to PPMd or ORC. Parquet is headlined above because it is what
people actually use; PPMd is what actually has to be beaten.

It is being measured at its ceiling. Probed on `l_nyc_311.csv` (28 MB):

```
order  2   6,577,792        mem   16m   1,790,664
order  6   1,970,162        mem   64m   1,519,719
order 16   1,393,015        mem  256m   1,393,015
order 32   1,393,480        mem 1024m   1,393,015
order 64   E_INVALIDARG  (7-Zip's PPMd caps at order 32)
```

Order saturates by 16 and is slightly *worse* at 32; memory saturates at
exactly the 256 MB the harness already hands it. There is no untested setting
where PPMd does better, which answers "but did you tune it".

**The obvious explanation of why is wrong**, and the wrong version is worth
writing down: a longer context does *not* reach the cell above. Sorted by row
width, the order-6 / order-16 ratio (>1 = order 16 wins) climbs with *width* —
0.78 at 10.9 B/row, 1.00 at 42.7, 1.19 at 245.1, **1.41 at 867.9**. At 868
bytes per row the cell above is 27x beyond PPMd's hard maximum of 32 bytes. A
longer order never reaches another row on any real table; it buys longer
phrases inside the current one.

Which gives the structural fact:

> **PPMd has a rich model with a ≤32-byte reach. LZMA has a 64 MB reach and an
> order-1 literal model.** Table redundancy is vertical, at a distance of one
> row width — inside LZMA's window and outside PPMd's context. Each of the
> field's two strongest general-purpose models holds exactly half of what a
> table needs, and they swap places by table shape. **Writing the columns
> contiguously is what removes the distance**, which is why this codec beats
> both where column structure is real (`nndss` 2.26x) and neither where it is
> not.

### Where the remaining losses come from: two thirds coder, one third model

The container is three xz sections — metadata, packed ints, text blob.
Re-finishing each with PPMd instead splits cleanly, and in opposite directions:

- **packed ints** — PPMd is *worse*: +27% (`nndss`), +18% (`nyc_311`), +9%
  (`permits`). It has no match model at all, and the redundancy in a
  byte-packed integer stream is periodic at the record stride, which is exactly
  what LZMA's match finder eats.
- **text blob** — PPMd is *better* nearly everywhere: −6.6% (`nndss`), −10.7%
  (`financial_shuffled`), −4.3% (`weblog`), −3.8% (`weblog_big`). Exception:
  `nyc_311`, +17%.

On `s_weblog` a 5.3% loss becomes 1.8% under a full PPMd re-finish. So roughly
**two thirds of that loss is our entropy coder and one third is PPMd's
whole-file context genuinely beating our column split.** That also corrects an
earlier finding recorded in this project — "our transform and PPMd do not
stack" was measured by swapping the coder for the *whole* archive. It holds for
the modelled numeric streams and fails for the residual text pile, which is
50–80% of the archive on precisely the tables we lose.

Full record: `../OUT/results/suite-v2-summary.txt` (all nine losses, the
per-competitor table, speeds), `../OUT/results/suite-v2-results.csv`
(v1: `suite-v1-*`),
`../OUT/results/competitor-ppmd.txt`. The measurement rules are in
`benchmarks/PROTOCOL.md`, which is the authority for any number quoted here.

---

## The older, hand-picked results

Everything below this line predates the unselected corpus and was chosen by
someone who already knew what the codec was good at. It is kept because the
per-shape detail is genuinely useful for understanding *why* the codec wins —
but the 100 datasets above are the evidence, and these are the illustration.

Measured against real binaries on real files from data.gov:

| dataset | shape | ours | best industry | win | enc MB/s | dec MB/s |
|---|---|---|---|---|---|---|
| Treasury yield curve | 7,003 x 8 | 21,968 | 44,148 `xz -9e` | **2.01x** | 21.5 | 90 |
| Treasury + dates | 7,003 x 9 | 26,542 | 50,572 `xz -9e` | **1.91x** | 20.1 | 89 |
| WA EV population | 289,564 x 16 | 2,258,862 | 3,917,084 `xz -9e` | **1.73x** | 30.5 | 121 |
| EPA supply-chain GHG | 18,288 x 8 | 79,009 | 116,900 `xz -9e` | **1.48x** | 14.5 | 178 |
| LA crime (200k slice) | 200,000 x 28 | 3,537,591 | 4,739,732 `xz -9e` | **1.34x** | 11.6 | 125 |
| NHAMCS survey (CDC) | 96,539 x 209 | 3,956,941 | 5,881,047 `parquet+brotli` | **1.49x** | 11.9 | 174 |

The NHAMCS row is the widest table tested — 209 columns, 200 of them
categorical — and gives the largest ratio by far. Full 278 MB file, every
contender measured:

| codec | bytes | ratio | time |
|---|---|---|---|
| **Polypress** | **3,956,941** | **73.65x** | 25s |
| `parquet+brotli` | 5,881,047 | 49.55x | 25s |
| `parquet+zstd` | 5,984,000 | 48.70x | 21s |
| `zstd --ultra -22` | 6,178,417 | 47.17x | 90s |
| `xz -9e` | 6,432,356 | 45.31x | 28s |
| `brotli -q 11` | 7,132,152 | 40.86x | 141s |
| `parquet+gzip` | 7,854,508 | 37.10x | 1s |
| `bzip2 -9` | 8,989,156 | 32.42x | 33s |
| `parquet+snappy` | 16,445,675 | 17.72x | 0s |
| `gzip -9` | 18,781,409 | 15.52x | 3s |

**1.49x smaller than Parquet**, which is the honest competitor here — nobody
stores a 209-column survey as compressed CSV. And the comparison is clean:
Parquet reproduced the printed text of all 209 columns exactly on this file,
so none of its size comes from discarding formatting.

### The same like-for-like question, on the 18 hand-picked tables

This is the earlier, smaller version of the measurement above — kept because it
prints the actual byte counts, which the 100-dataset summary aggregates away.
Re-finishing Polypress with the *same* codec Parquet is using, across 18
tables:

| dataset | ppz+zstd | parquet+zstd | ppz+brotli | parquet+brotli |
|---|---|---|---|---|
| `cdc_nndss` | 41,531 | 191,910 | 37,027 | 223,929 |
| `noaa_gsoy_sea` | 6,896 | 36,732 | 6,392 | 35,524 |
| `seattle_fire911` | 612,622 | 1,138,757 | 576,554 | 1,095,103 |
| `chicago_permits` | 918,844 | 1,353,332 | 864,525 | 1,312,856 |
| `wa_ev_population` | 272,923 | 528,746 | 254,441 | 502,133 |

**18 of 18 at zstd-22, 17 of 18 at brotli-11**, by margins from 1.06x to 5.6x.
The single loss is `mixed_types`, an adversarial table, by 11%. Strip xz out
entirely and the gap barely moves — the win is the modelling.

That measurement also prices the speed trade, since it is the same swap:
Polypress finished with zstd-22 instead of xz is **8% larger and several times
faster**, and still beats Parquet on every table. Nothing in the format
prevents offering that as a flag.

`polypress info` explains where the win comes from: **172 of the 200
dictionary columns were sorted by a parent**. Survey columns predict each
other heavily, and no columnar format exploits that — Parquet compresses
each column chunk independently.

### Reproducible on real survey data, from scratch

The NHAMCS file above is 278 MB and cannot be committed, so the survey claim
used to be unverifiable without a manual download. It no longer is:

```bash
python3 benchmarks/fetch_nhanes.py nhanes_real.csv   # real CDC microdata
python3 benchmarks/measure_one.py nhanes_real.csv --reps 3
```

That pulls 17 NHANES 2017-2018 questionnaire files from CDC, joins them on
respondent ID, and gives **9,254 rows x 421 columns** — wider than NHAMCS.
No credentials, no manual step. Measured 2026-07-27:

| codec | bytes | ratio |
|---|---|---|
| **Polypress** | **516,601** | **12.02x** |
| `xz -9e` | 707,784 | 8.77x |
| `bzip2 -9` | 712,418 | 8.72x |
| `brotli -q 11` | 717,274 | 8.66x |
| `parquet+brotli` | 1,003,563 | 6.19x |
| `parquet+zstd` | 1,049,961 | 5.91x |

**1.37x smaller than the best general compressor, 1.94x smaller than the best
Parquet** — and on this file Parquet did *not* reproduce the exact printed
text, so part of even that gap is discarded formatting rather than
compression.

Two things this exposes that the curated six did not. Encode runs at **1.9
MB/s** here, an order of magnitude off the headline figures, because 421
columns is 176,820 ordered pairs for the parent search — it was 1.3 MB/s
before H(X) was hoisted out of that loop, and the O(columns²) shape is still
there underneath. And the win is narrower than the 1.49x NHAMCS row — real
breadth moves numbers down, which is the point of measuring it.

### Thirteen real datasets, fetched and measured end to end

`benchmarks/fetch_corpus.py` pulls 13 real files from government open-data
portals — chosen across *shapes* rather than subjects, since shape is what
decides whether this codec wins. 353 MB, no credentials, one command:

```bash
python3 benchmarks/fetch_corpus.py ../IN/corpus/
python3 benchmarks/sweep.py ../IN/corpus/*.csv --out ../OUT/results/curated13.jsonl
python3 benchmarks/report.py ../OUT/results/curated13.jsonl
```

| dataset | rows x cols | vs best other | best other |
|---|---|---|---|
| CDC notifiable disease | 150,000 x 16 | **2.26x** | `orc+zstd` |
| Seattle fire 911 | 200,000 x 7 | **1.85x** | `xz -9e` |
| WA EV population | 146,961 x 16 \* | **1.74x** | `xz -9e` |
| Austin 311 | 150,000 x 19 | **1.61x** | `xz -9e` |
| NYC collisions | 150,000 x 29 | **1.46x** | `xz -9e` |
| Chicago crimes | 138,102 x 22 \* | **1.44x** | `xz -9e` |
| NYC 311 | 44,437 x 44 \* | **1.35x** | `xz -9e` |
| NYC baby names | 29,685 x 6 | **1.27x** | `parquet+brotli` |
| USGS earthquakes 2023 | 16,190 x 22 | **1.26x** | `bzip2 -9` |
| USGS earthquakes 21-22 | 16,707 x 22 | **1.22x** | `bzip2 -9` |
| Chicago permits | 28,332 x 116 \* | **1.15x** | `xz -9e` |
| NOAA climate, SEA | 79 x 106 | **1.12x** | `bzip2 -9` |
| NOAA climate, ORD | 69 x 102 | **1.12x** | `brotli -q11` |

\* truncated at a row boundary to stay inside a 2 GB memory ceiling — at
40 MB for the first three, and at 28 MB for `chicago_permits`, which peaked at
2,120 MB at 40 MB and had to come down. Every codec was handed the identical
truncated file, so the comparison on each row is exact.

**13 of 13, median 1.35x, worst 1.12x, best 2.26x.**
Regenerated 2026-07-30 from `../OUT/results/curated13.jsonl` by the command in this
section, never edited by hand — an earlier version of this table was patched
per-dataset after a codec change and drifted from the results file on five of
the thirteen rows.

**Read the spread, not the headline.** Only six datasets clear 1.4x.

The `cdc_nndss` row fell from a previously published **3.73x to 2.26x**, and
the reason is worth stating plainly because it looks like a regression and is
not one:

| | previous run | this run |
|---|---|---|
| polypress | 95,894 | **95,275** |
| `parquet+brotli` | 355,262 | 355,262 |
| `orc+zstd` | *not measured* | **215,688** |

The codec produced an almost identical archive and Parquet produced a
byte-identical one. What changed is that **ORC is in the lineup now and is a
much stronger competitor on that table than Parquet ever was.** The old 3.73x
was not wrong, it was measured against a weaker field. This is the direct cost
of having had 11 competitors instead of 20, and it is an argument for keeping
the lineup wide even when it makes the numbers smaller.

Parquet also failed the exact-text check on most of these rows, so its column
is flattered wherever it appears.

**Chicago permits is the informative one.** 116 columns, 106 of them
dictionary-encoded, 80 successfully sorted by a parent — the machinery fired
about as hard as it can — and the result is still only 1.12x, and was 1.03x
before text columns started taking a reorder parent. Nearly a tie either way.
The reason is that 8 free-text columns hold most of the bytes, and no amount of
cross-column modelling touches free text. **Width is not the predictor; the
fraction of the file that is modellable is.** A wide table dominated by a few
large text fields will tie, and saying "wide tables win" would have been the
wrong lesson to draw from NHAMCS.

Encode ranged from 28.7 MB/s down to 1.3 MB/s across the corpus, the low end
being the widest tables.

### Matrix-shaped tables, and the bug that was hiding them

Every dataset above is survey, administrative, incident or text-heavy data.
**None of them is the shape the planar predictor was built for**, and for a
long time that meant the ~2x planar claim rested on data no benchmark here
touched. `benchmarks/fetch_matrix.py` fixes that — three matrix-shaped tables,
no credentials, one command:

```bash
python3 benchmarks/fetch_matrix.py ../IN/corpus/
python3 benchmarks/measure_one.py ../IN/corpus/treasury_yields.csv \
    ../IN/corpus/weather_hourly.csv ../IN/corpus/weather_wide.csv
```

| dataset | shape | ours | best other | win |
|---|---|---|---|---|
| Treasury yield curve 1990-2025 | 9,006 x 9 | 33,778 | 64,836 `xz -9e` | **1.92x** |
| Weather, 10 sensors hourly x 10y | 87,672 x 11 | 494,456 | 1,078,640 `xz -9e` | **2.18x** |
| Hourly temperature, 24 cities | 26,304 x 25 | 412,140 | 687,390 `bzip2 -9` | **1.67x** |

**Getting this data in immediately exposed a real defect.** The Treasury yield
curve is the canonical matrix table, and it classified as *eight dictionary
columns and zero numeric ones* — so no group could form and the predictor never
ran. The cause was four blank cells out of 72,048. The numeric test was all or
nothing, so a single missing value discarded an entire column.

**Four cells were costing 41.2% of that file** (59,309 B where 34,891 B was
available). A second strain of the same fault: three of the 24 temperature
columns are refused despite uniform decimals and no blanks, because they
contain the cell `-0.0` — once, in one column's 26,304 cells. That refusal is
*correct*, since `-0.0` cannot be stored as the integer 0 and printed back
faithfully, but discarding the column for it is far too blunt, and it split a
21-column matrix into groups of 18 and 3.

A numeric column may now carry **exceptions**: cells it cannot represent are
kept by position and as text, and their slots are forward-filled from the
previous good value. Filling rather than dropping is the load-bearing choice —
it keeps every column the same length, which is what keeps the planar
predictor able to stack them. On the yield curve, becoming numeric at all is
worth 28% and the group on top of that another 18%.

**It is measured, because it is adjacent to an idea that already failed here.**
Recovering these columns also moves them out of the dictionary path, which is
exactly the trade that made the reverted ragged-decimal work 9–23% worse. So
the previous behaviour is encoded too and kept whenever it is smaller:

| dataset | before | after |
|---|---|---|
| `treasury_yields` | 59,309 | **34,891** (−41.2%) |
| `mixed_types` | 85,990 | 60,220 (−30.0%) |
| `weather_hourly` | 566,244 | 503,460 (−11.1%) |
| `weather_wide` | 431,381 | 414,471 (−3.9%) |
| every other table | — | **0.00%** |

Those exact zeroes are the guard working. `chicago_crimes`, `nyc_311`,
`nyc_collisions` and `wa_ev_population` all have eligible columns — 161 KB,
136 KB, 100 KB and 46 KB of them — and on every one the guard measured the
conversion and refused it. Without it this change would have lost on four of
the largest datasets here.

Against the *specialised* numeric codecs on the yield curve — the comparison
that actually matters, since general-purpose tools were never the competition
for numeric tables:

| codec | bytes |
|---|---|
| **ours** | **21,968** |
| ClickHouse `Delta + ZSTD(22)` | 43,375 |
| zfp int32 lossless + xz | 45,776 |
| ClickHouse `DoubleDelta + ZSTD(22)` | 49,647 |
| ClickHouse `T64 + ZSTD(22)` | 55,288 |
| ClickHouse `Gorilla + ZSTD(22)` | 141,642 |
| fpzip lossless (float64) | 255,606 |

## Layout

Since 2026-08-04 the repository follows a four-folder convention:
`memory/` (context and notes), `IN/` (inputs), `OUT/` (outputs), `work/` (code).

```
memory/         restructure log and working notes for future sessions
IN/             benchmark corpora -- all re-fetchable, none committed
OUT/results/    sweep output: the JSONL, summaries and CSVs that are the evidence
work/           everything below is inside work/ -- run commands from there
  csrc/         THE PROGRAM, in C: codec, table readers and writers,
                streaming container, threads, command line
  csrc/tests/   its test suite, also C
  py/           parquet.py -- the one format that needs Python (Arrow)
  app/          the Mac app: PolypressApp.swift + page/ (one window around the
                C program), build_app.sh
  benchmarks/   size and speed against every competitor (Python: they drive
                pyarrow, 7z, brotli... and time the C program as a process)
  docs/         the results PDF and the script that generates it
  lab/          experiments; not part of the program
```

## One pass

Since 2026-09-29 an encode is exactly this, once:

1. **Classify each column** -- dictionary (few distinct values), numeric
   (differences packed small, with exceptions for the odd blank or "-0.0"),
   or text. Whether a column that is numeric apart from a few cells is stored
   as numbers is decided by a cheap one-sided screen, not by encoding twice.
2. **Reorder**: each dictionary column is sorted by the parent column that
   best predicts it (conditional entropy, with a fast preset-1 probe as the
   check), and each text column by the dictionary column a probe says shrinks
   it. The decoder rebuilds the parent first and recomputes the same sort, so
   the order is free.
3. **Predict**: numeric columns differenced to their best order; adjacent
   commensurable numeric columns predicted in 2D.
4. **Compress**: three streams (metadata, binary payloads, text pile), each
   through `xz -9e` once. xz was measured against the alternatives on the
   modelled streams: brotli -11 is 1% smaller and 6.5x slower, bzip3 2%
   bigger, bzip2 7%, zstd -19/-22 11% bigger. Keeping xz also means every
   archive ever written still opens.

What was removed, and what it cost, on the 39-table suite:

| | total size | encode time |
|---|---|---|
| before: trials + the plain-xz/bzip2 check | 1.000 | 72 s |
| without the check only | 1.002 | 48 s |
| **one pass (now)** | **1.025** | **31 s** |
| one pass with no reordering at all | 1.20 | 28 s |
| plain `xz -9e` on the CSV | 1.35 | -- |

Taking the reordering out makes the one-pass output 17% bigger on this suite
-- the cross-column idea is real -- where the trials were worth 2.5%. On one core (what the big-file mode used to
run on) the gap was 3x: 9.0 s against 2.5 s on 200,000 rows of NEMSIS.
Tables that came out more than 3% bigger than before: `chicago_crimes`
+11.8%, `nyc_311` +6.9%, `chicago_permits` +4.8%, and five within 3.3-3.7%.

**Big files** are compressed a block at a time, now with up to four blocks
side by side (they are independent), and the program reports how far it has
got (`--progress`, which the app turns into a bar and a time estimate).

## The program is C

**Since 2026-09-29 Polypress is one C program** (`work/csrc/`). Until then
there were two implementations: the Python codec (`polypress/fast.py` and
friends, numpy behind it) and a C port that had to match it byte for byte,
checked by a test that ran both. The Python side was retired -- deleted, and
recoverable from git at commit `c55f68f` -- so there is one codec, one set of
readers, one command line. The archive format did not change: every `.ppz`
written before opens exactly as it did, and the C encoder still writes the
same bytes it wrote when it was the port (checked on all 39 suite tables
before the Python went).

```bash
./csrc/build.sh                              # -> csrc/polypress
./csrc/polypress compress data.csv           # -> data.csv.ppz
./csrc/polypress restore  data.csv.ppz       # -> data.csv
./csrc/polypress info     data.csv.ppz       # plan, shape, what was reordered
```

**Using it needs nothing installed.** liblzma is linked into the binary;
iconv ships with macOS and every Linux. Building needs the liblzma headers
(`brew install xz`, or `apt install liblzma-dev`).

**What is still Python, and why:**

| part | why it stays Python |
|---|---|
| `py/parquet.py` | Parquet is a binary container with its own encodings; reading it properly means the Arrow library, whose C API is a far heavier dependency than this whole program. So Parquet is converted at the edge: `python3 py/parquet.py to-csv x.parquet \| polypress compress - -o x.ppz`. |
| `benchmarks/` | They drive the competitors -- pyarrow's Parquet/ORC/Feather, 7z, brotli -- and time the C program as one more subprocess. |

**Threads.** 72-92% of encode time is xz trial compressions -- candidate
layouts that are compressed, measured and compared. The trials of one
decision are independent, so they run at the same time (up to 4, or
`PPZ_THREADS`), and every decision still folds its sizes in the original
order with the original strict `<`, so **the archive is the same bytes with
one thread or four**. On the suite: 29 small and medium tables 63 s -> 14 s,
the ten large ones 170 s -> 56 s. The price is memory -- several xz -9e
working sets at once, about (input MB x 50) + 100 at peak -- so streaming
mode runs on one thread and `PPZ_THREADS=1` does the same anywhere.

## ## The three ideas

**1. Local function building.** Fit a low-degree polynomial to the last few
values in a column, extrapolate one step, store only the error. Because the
fit is re-centred at every cell, the coefficients never have to be stored —
the decoder already has the neighbours. Order-*k* extrapolation turns out to
be exactly the *k*-th finite difference, so this is one `np.diff` call.

**2. The same thing in two dimensions.** Where adjacent numeric columns are
*commensurable* — same decimal places, same magnitude — a cell is predicted
from its left, upper, and upper-left neighbours. This is what wins on
matrix-shaped tables, and it is the one thing no shipped columnar codec does:
Gorilla, DoubleDelta, T64, zfp and fpzip all predict down a single column.
The detection matters as much as the predictor. Differencing `Model Year`
against `Make` is meaningless, so groups are only formed where the columns
are genuinely comparable.

**3. Cross-column structure by reordering.** Table columns are not
independent — `City` is nearly determined by `Postal Code`. Rather than model
that, sort the rows by the parent column: equal parents become adjacent, the
child collapses into long runs, and xz eats it. **The permutation is free**,
because the decoder has already rebuilt the parent and recomputes the same
stable argsort. Parents are chosen by conditional entropy with a Miller-Madow
correction, arranged into a tree so a parent is always decoded first.

On 40k EV rows this beat an adaptive context-modelling range coder
2,328 B vs 6,901 B — and it is far faster, because the heavy lifting moves
into C instead of a Python loop.

## Install

```bash
cd work && ./csrc/build.sh                                  # -> csrc/polypress
PREFIX=/usr/local ./csrc/build.sh install                   # put it on PATH
```

The PyPI package (`pip install polypress`, last release 0.2.1) was the
Python codec. It is retired with it: 0.2.1 stays on PyPI and still works,
but new versions are the C program.

## Use

```bash
polypress compress data.csv                 # -> data.csv.ppz
polypress compress data.tsv -o d.ppz        # TSV, PSV, .txt (delimiter sniffed),
polypress compress data.json                #   JSON records or columns, JSON Lines
polypress compress old.csv --encoding latin-1
polypress restore  data.csv.ppz             # -> data.csv
polypress restore  data.csv.ppz -o out.json # the extension picks the format
polypress convert  data.tsv data.jsonl      # a plain format change, no archive
polypress info     data.csv.ppz [--json]
```

`-` is standard input or output as CSV, which is how Parquet gets in and out:

```bash
python3 py/parquet.py to-csv data.parquet | polypress compress - -o data.ppz
polypress restore data.ppz -o - | python3 py/parquet.py from-csv - out.parquet
```

For a file larger than RAM, compress a block at a time with a settable
memory budget; `restore` and `info` recognise the result by themselves:

```bash
polypress stream-compress big.csv --budget 1.0     # ~1 GB peak
polypress restore big.csv.ppz -o back.csv
polypress info    big.csv.ppz                      # blocks and sizes
```

Blocks are compressed independently, so peak memory is one block rather than
one file. The cost is real: the cross-column reordering only sees correlations
*inside* a block, so smaller blocks compress slightly worse.

Or the Mac app:

```bash
./app/build_app.sh          # builds the program, then ~/Applications/Polypress.app
./app/build_app.sh dmg      # also writes dist/Polypress.dmg to hand to someone
open ~/Applications/Polypress.app
```

The `dmg` target produces a disk image with the app, an Applications symlink to
drag it onto, and a plain-language note. **It is unsigned**, so the first time
anyone opens it macOS will claim it is from an unidentified developer or even
that it is damaged. That is Gatekeeper's response to every app without a paid
Apple Developer certificate, not a fault in the build; right-click → Open → Open
once and it is fine thereafter. The note in the image says exactly that, because
a download that appears broken on first launch is a download nobody uses.

It is one quiet window (Swift + a bundled page, since 2026-09-29; it was a
chain of AppleScript dialogs before):

- **drop a table** on it — compressed into a `.ppz` beside the original,
  verified cell for cell. Big files go a block at a time automatically.
- **drop a `.ppz`** — restored beside itself, never over an existing file
- **hover a line** — "show" in Finder, and "as csv tsv json jsonl parquet" to
  write the same table in another format
- **click a file's name** — rows, columns, and what the codec did with them
- **double-click a `.ppz`** in Finder, or drop files on the Dock icon

The C program ships inside the bundle, so the app needs nothing installed;
Parquet is offered only when the system Python has pyarrow.
`Polypress --selftest` runs every action on generated files without opening
a window, and `build_app.sh` refuses to install a build that fails it.

## Fidelity

The **logical table** round-trips exactly: column names, column order, row
order, and every cell as an exact string. Verified in memory on every
compress.

It does *not* promise byte-identical files, because CSV quoting and line
endings are not canonical. Read a file and write it back and you get an
equivalent table, not identical bytes.

**A row is never cut to fit.** A row shorter than the header is padded with
empty cells -- CSV writers drop trailing empties all the time, and nothing is
lost. A row *longer* than the header used to be trimmed silently, and the
round-trip check could not see it, because it compared the already-trimmed
table with its own decoding (found 2026-09-15: Romeo and Juliet as raw text
lost the tail of every line with a comma in it). Now the extra cells are
dropped only when they are all empty, and otherwise the file is refused with
the row named.

**Text encoding is read or refused, never guessed.** A byte-order mark is
honoured, so the UTF-8-with-BOM and UTF-16 files Excel produces are read
correctly. An unmarked file is decoded as strict UTF-8, and one that is not
UTF-8 is refused with the offending byte named:

```
polypress: survey.csv is not valid UTF-8 -- byte 0xFC at offset 11 cannot be decoded.
If you know the file's encoding, name it: --encoding latin-1 (or cp1252, utf-16, ...).
```

`--encoding` is the only way to override it, because guessing is what
corrupts data. This was worth fixing properly: the readers used to open every
file with `errors="replace"`, which silently turns an undecodable byte into
U+FFFD. A latin-1 file lost every accented character and reported success —
and *the round-trip check could not catch it*, because the table was already
wrong before it was encoded, so the check compared a corrupted table against
itself. Three of six test encodings destroyed data that way.
`csrc/tests/` pins all of it.

## Where it loses

### The 22 losses out of 500, in full

| dataset | lost to | by |
|---|---|---|
| `sars_cov_2_variant_proportions` | `orc+zstd` | **53.1%** |
| `energy_star_certified_smart_thermostats` | `brotli -q 11` | 10.1% |
| `open_meetings` | `brotli -q 11` | 8.1% |
| `maryland_port_administration_general_cargo` | `brotli -q 11` | 7.1% |
| `new_york_state_budget_vetoes_2013_14` | `brotli -q 11` | 6.4% |
| `missouri_river_water_trail_access_points` | `brotli -q 11` | 4.9% |
| `covid_19_vaccination_trends_in_the_united_states` | `brotli -q 11` | 3.9% |
| `covid_19_vaccinations_in_the_united_states` | `brotli -q 11` | 3.4% |
| `salary_steps_by_job_classification` | `brotli -q 11` | 2.4% |
| `2015_street_tree_census_tree_data` | `brotli -q 11` | 2.3% |
| `listado_de_medicamentos_en_venta_libre` | `bzip2 -9` | 1.6% |
| `county_clerk_license_information` | `brotli -q 11` | 1.0% |
| `naloxone365_nj_free_naloxone_at_pharmacies` | `bzip2 -9` | 0.9% |
| `medical_examiner_unidentified_persons` | `brotli -q 11` | 0.7% |
| `american_rescue_plan_arp_rural_payments` | `bzip2 -9` | 0.6% |
| `energy_star_certified_residential_clothes_washers` | `brotli -q 11` | 0.5% |
| `tca_all_approved_grants_fy25` | `bzip2 -9` | 0.4% |
| `csric_best_practices` | `bzip2 -9` | 0.3% |
| `tca_all_approved_grants_fy24` | `bzip2 -9` | 0.2% |
| `current_sla_pending_licenses` | `bzip2 -9` | 0.1% |
| `presubmission_community_meetings` | `bzip2 -9` | 0.1% |
| `provider_relief_fund_covid_19_high_impact` | `bzip2 -9` | 0.1% |

**Twelve of the 22 are to `brotli -q 11`, which is not carried as a fallback
candidate** — see the note at the end of this section. **Nine are to `bzip2 -9`
run on the original file rather than on the canonical rendering**, explained
above under the guarantee. That leaves **one** real loss.

`sars_cov_2_variant_proportions` is that one, and it is the worst result in the
corpus at 0.65x. `orc+zstd` beats it by 53%. This is the shape the codec is
weakest on and it has not been diagnosed.

### The 24.9% loss that used to be here was a bug, and it is fixed

Worth keeping in the record, because the corpus earned its keep by finding it.

The codec carries plain `xz` and `bzip2` over canonical CSV as fallback
candidates, and the rule is that the modelled encoding only wins if it is
*measured* smaller. It was not measured. `fast.py` built the fallbacks **only
when none of the three modelling tricks fired**, on the theory that if any
trick fired the modelled output wins by a margin no general compressor closes.

That theory was wrong. It had been checked against 18 tables and held; against
100 unselected ones it failed on 3. On the COVID vaccination table (50,000 x 80)
one trick fired, so the fallbacks were skipped:

```
what encode() shipped          :    2,830,752 B   (container PPZ1)
the xz fallback it never ran   :    2,210,086 B
```

**28.1% larger than a fallback the codec already implements and simply did not
try.** This is the failure mode `CLAUDE.md` calls *the measured/unmeasured
trap*: entropy is a good nominator and a bad decider.

Fixed 2026-07-31. The candidates are now always considered, and the three
affected datasets improved by **21.9%**, 1.4% and 0.9%. `xz -9e`, `bzip2 -9`
and `zstd -22` all went to **100/100**.

Two things about the fix are worth stating. It is affordable because the
compressors are handed the size they must beat and abandon a hopeless candidate
part-way — compressed output only grows, so that cannot change which candidate
wins. And a cheaper alternative was **measured and rejected**: a preset-1 LZMA
probe as nominator is only sound above a 3.0x threshold (worst observed ratio
2.825), which fires on 19 of 22 datasets and saves almost nothing over simply
always checking. See `benchmarks/probe_fallback_gate.py`.

**The test suite had been agreeing with the bug.** `tests/test_fast.py`
asserted that "a structured table must NOT pay for the fallback" — the same
assumption `encode()` was making, so it could never have caught the defect. On
its own 2,000-row zip/city table the modelled container is 294 B and plain xz
is **122 B, 2.4x smaller**, and a trick fired. The test now requires the
smaller of the two.

### The adversarial suite

A hundred unselected datasets answers "you picked the files", but every one of
them is still a table someone thought worth publishing — none was written to
attack this codec. So `benchmarks/make_hostile.py` generates ten that were:
random text, UUID keys, base64 blobs, 200 mutually independent numeric columns,
a single 5,000-column row. **This is the corpus that is supposed to hurt**, and
a tie on it is the correct outcome, not a disappointment.

**5 of 10 won**, measured 2026-07-30. Every loss, in full:

| hostile case | lost to | by |
|---|---|---|
| `random_text` | `orc+zstd` | 9.0% |
| `uuid_keys` | `orc+zstd` | 3.1% |
| `base64_blob` | `orc+zstd` | 2.6% |
| `wide_random` | `orc+zlib` | 0.1% |
| `high_precision` | `bzip2 -9` | 0.0% (a tie; the difference is container magic) |

**Every one of those losses is to ORC, and that is new.** In earlier runs these
tables lost to `brotli -q 11` by under 1%, and the README concluded that
carrying a brotli candidate was not worth the dependency. Adding ORC to the
lineup changed the answer: on high-cardinality and incompressible data — random
text, UUID keys, base64 blobs — **ORC is a materially stronger competitor than
anything previously measured here**, and the gap is 9% rather than 0.8%.

This is the same lesson as the `cdc_nndss` row further up. A benchmark lineup
that is too small does not produce wrong numbers, it produces flattering ones.

**The guarantee was: never worse than xz or bzip2 on any table** (retired
2026-09-29, see [One pass](#one-pass)), because both were carried as
candidates and the smaller won. As the section above records it
did not hold until 2026-07-31; it now does, and the head-to-head table shows it
— `xz -9e` and `bzip2 -9` are both **100/100** across the unselected corpus.

It was never "never worse than anything" in any case. The fallbacks carried are
xz and bzip2, both stdlib; adding a brotli candidate would mean a non-stdlib
dependency and linking libbrotli into the C port. That was judged a bad trade
when the margin at stake was 0.8% on random noise — **but ORC now takes
`random_text` by 9.0%, so that judgement was made against a field that was too
small and is worth revisiting.** It is still a decision rather than an
oversight, unlike the gate above.

Two costs, stated plainly:

- **Every table now pays for the fallback check, and that is the price of the
  guarantee.** Measured before the abort-early cap was added: **+63% encode
  time** across 22 datasets. It also costs memory — building the canonical CSV
  and parsing it back is roughly another copy of the table, which took
  `chicago_permits` at 40 MB from a 1,965 MB peak to 2,120 MB. The benchmark
  ceiling dropped from 40 MB to 28 MB per input as a result.
- **The fallback is refused if it cannot round-trip.** Canonical CSV is not
  lossless for every conceivable cell, so a candidate is parsed back and
  compared before it is allowed to win. Losing on size beats corrupting data.

The five it wins on are the informative half. `anticorrelated` — a
high-cardinality numeric column that a naive conditional-entropy score would
happily adopt as a parent — comes out **4.24x** ahead, which is the
Miller-Madow correction earning its place. At the other end, `wide_random` (200
mutually independent numeric columns) now finishes **0.1% behind `orc+zlib`**,
and that is the honest result: the O(columns²) parent search does its maximum
work for no reward at all.

`single_wide_row` is worth one line for a reason that is not about ratio. It is
78 KB — one row, 5,000 columns — and Polypress encodes it in 115 MB of RAM
while **pyarrow's ORC writer needs 2,363 MB** on the same file, enough to abort
a sweep on its memory ceiling. ORC buffers roughly half a megabyte per column.
`measure_one.py` therefore skips ORC above 1,000 columns and records that it
did.

Full numbers, every contender, in `../OUT/results/hostile-summary.txt`.

## Honest limitations

- **It sits in the slow tier.** Median across the 100 unselected datasets,
  measured on the Python codec: **2.0 MB/s encode, 134 MB/s decode**. For
  context, on the same corpus and machine, `xz -9e` is 3.9 / 203,
  `brotli -q 11` is 1.1 / 501, `zstd -22` is 2.8 / 871 and `zstd -3` is
  187 / 716. The C program with threads is roughly 3-4x faster than that on
  the suite (170 s -> 56 s on the ten large tables), but the sweep has not
  been re-run on it yet, so the median above is the last *measured* one.
  Either way it is two orders of magnitude off the fast tier, where it will
  stay. These are not measured on the same basis: the general-purpose tools
  are handed raw CSV bytes, while Parquet, ORC, Feather and Polypress read the
  table themselves.
- **(Historical -- the encoder is one pass since 2026-09-29.) Never-worse
  cost encode time, and the bill had gone up.** Every guard in
  here works by encoding the table both ways and keeping the smaller, so a
  table eligible for a guard is encoded twice. Measured across 21 tables, the
  2026-07-29 changes made encode **1.86x slower for 1.66% smaller output**.
  Tables with nothing to recover are untouched (`shuffled_cats` 1.00x,
  `wide_random` 1.00x, `cdc_nndss` 1.03x), but four large datasets pay ~2.2x
  for **zero** gain: they have eligible columns, so the guard runs and then
  correctly declines. Making the guard cheaper without weakening it is open
  work; the guarantee is not negotiable, the price of it is.
- **The 2x cases are matrix-shaped tables.** The 1.3–1.5x cases are the more
  typical result.
- **Bit-identity with numpy was not achievable, and the codec does not depend
  on it** (a lesson from the port, kept because it shaped the format).
  `tests/test_cbin_corpus.py` — the first check of C against Python on
  real data rather than constructed cases — found three divergences.
  After the fixes it reports **108 of 108 datasets byte-identical**, with both
  decoders reproducing every table.
  Chasing the last two established why: `pairwise_sum()` in the C port
  reproduces numpy's *documented scalar* algorithm exactly and agrees to the
  last bit on a 23-bin marginal, but on a **13,147-bin joint histogram
  `np.sum` does not match numpy's own documented algorithm** — it takes a SIMD
  reduction whose grouping depends on the CPU's vector width. No portable C can
  match that, and two numpy builds on different hardware need not match each
  other. Entropy scores are therefore quantised to a ~1e-6 grid and compared as
  integers, with ties falling to the lower column index. The last bit never
  carried information; letting it choose a parent decided every byte after it.
- **(Historical, see One pass.) Encode got slower to make "never worse" true.** Every table builds the
  canonical CSV and checks the plain fallbacks against the modelled encoding.
  Measured before the abort-early cap: **+63% encode time**. That bought the
  guarantee — `xz`, `bzip2` and `zstd -22` all go to 100/100 — and it is the
  right trade. The fallbacks now run alongside the modelled encode and stop
  the moment they cannot win, which reclaims most of it.
- **Breadth is no longer the gap it was, but the corpus is still one genre.**
  100 unselected tables answers "you picked the files", and it does not answer
  "you picked the *kind* of file". Every one of them is a government
  open-data table, because that is what the Socrata catalog indexes. Census
  microdata, NOAA grids, genomics and financial tick data are all still
  unmeasured, and there is no reason to assume this result transfers to them.
- **A short, very wide table is the worst case for speed, and it is not
  obvious.** The slowest encode across 26 datasets is not a big file — it is
  `noaa_gsoy_sea`, **60 KB, at 0.5 MB/s**. 106 columns and 79 rows: the parent
  search is quadratic in the column count and there are nowhere near enough
  rows to amortise it. That is two orders of magnitude slower than
  `cdc_nndss` at 31.5 MB/s, for a 1.12x win.
- **Parent search is O(columns^2).** Every ordered pair of dictionary columns
  is scored, so a 209-column table means 38,220 pairs. The sample depth is
  traded against the pair count (`MI_BUDGET`) to keep that bounded; without
  it, encoding the NHAMCS file took two and a half minutes instead of 25
  seconds. A genuinely wide table -- thousands of columns -- would need a
  smarter candidate search, not a smaller sample.
- **The predictors are prior art.** The planar predictor is Lorenzo
  (Ibarria et al., 2003, used in fpzip and SZ); MED is JPEG-LS. What is not
  standard is the table-specific front end: commensurable-group detection and
  the reordering trick.
- **The reordering trick is not unprecedented either, and an earlier version
  of this README claimed novelty for it that it does not have.** Reordering
  rows to compress better is a studied problem — Lemire, Kaser and Gutarra,
  *Reordering Rows for Better Compression: Beyond the Lexicographic Order*,
  ACM TODS 37(3), 2012, and column-store sorted projections before it.
  Exploiting functional dependencies between columns appears in database
  patents. "SortComp" sorts each column and compresses the sorted table
  alongside an explicit permutation table.

  Two things here differ from *those particular* references, and they used to
  be described in this README as the novelty. **(1) The ordering is
  per-column, not global** — every
  dictionary column is stored under a permutation derived from *its own*
  parent, so a 421-column table can carry hundreds of different orderings at
  once, where the prior work picks one order for the whole table. **(2) The
  permutation is neither stored nor imposed on the output** — SortComp stores
  it, global-sort approaches change the row order you get back. Here the
  decoder re-derives each permutation from a parent it has already rebuilt,
  and the restored table is in the original row order, cell for cell.

  **That combination is anticipated in full, and the claim was wrong.** A
  prior-art search on 2026-07-28 found US 8,312,026 B2, "Compressing massive
  relational data", Kiem-Phong Vo, assigned to AT&T Intellectual Property I,
  L.P., filed 2009-12-22 and granted 2012-11-13. It discloses every element
  above. It defines the transform of a field by the unique stable
  lexicographic argsort of its predictor field; it notes that the topological
  order guarantees the predictor is already inverted when it is needed, so the
  permutation is never stored; its claim 1 recites an optimum branching of the
  dependency graph plus a topological sort — the parent tree; it requires that
  a compressed file "always decompress into its exact original state"; and it
  scores a candidate predictor by the *compressed size* of the transformed
  field. Earlier still, by the same author: B. D. Vo and K.-P. Vo, "Using
  column dependency to compress tables", DCC 2004, pp. 92–101, and
  "Compressing table data with column dependency", *Theoretical Computer
  Science* 387(3):273–283, 2007, which US 8,312,026 says it generalises.
  Neither paper has been read here — both are paywalled and could not be
  obtained. The per-column half is separately claimed in US 8,108,361 B2
  (Netz, Petculescu and Crivat, Microsoft, priority 2008-07-31, granted
  2012-01-31), whose claim 9 covers rearranging the row sequence of one column
  only, in a dictionary-encoded columnar compressor, for the same reason —
  though it stores the reordering as per-column metadata rather than
  re-deriving it.

  This codec reached the same design without knowing of any of that, including
  the rule that a parent must be chosen by measured compressed size and not by
  an entropy score — which took commit `20dd96e` and 19.7% off `cdc_nndss` to
  learn. Independent convergence on the same design, twenty years apart, is
  reasonable evidence the design is right. It is not evidence of priority, and
  the earlier claim is withdrawn.

  One thing the search did turn up that is worth recording: **almost everyone
  else stores the permutation.** Amazon's US 11,422,805 B2 ("Sorting by
  permutation", 2022) is argsort one column and use it to order another, but
  its claim 1 requires the permutation be kept in a separate mapping table;
  Oracle's US 7,103,608 puts it in the block header; SortComp keeps a
  permutation table. Vo is the only exception found. So the "don't store it,
  re-derive it from a parent the decoder already has" step is rare and it is
  correct — it is just not first.

  Both patents have expired — US 8,312,026 lapsed 2024-11-13 for unpaid
  maintenance fees, and US 8,108,361 has likewise expired — so there is no
  restriction on using this code.

## What else is in here, and what it proved

These are kept because the negative results are the useful part.

| file | verdict |
|---|---|
| `attic/exact_interp.py` | **Exact polynomial interpolation cannot compress.** Storing the interpolating polynomial's coefficients costs *more* than the values, and gets worse with more points (6.8x worse at n=24). Interpolation is an invertible linear map — n values in, n coefficients out. |
| `attic/smart.py`, `attic/rc.py` | A working adaptive binary range coder with cross-column context modelling. **Superseded**: reordering plus xz beat it on both size and speed. Kept as the reference implementation. |
| `polypress/dtz.py` (deleted 2026-09-29, in git at `c55f68f`) | The earlier "try every strategy and keep the smallest" container. Its apparent 1% win over `xz -9e` turned out to be **CSV quote-stripping, not compression** — feeding xz the same canonicalised bytes matched it to within 68 bytes. |
| `polypress/codec.py` (deleted 2026-09-29, in git at `c55f68f`) | Rice coding and the original fixed-order predictors. The predictor idea survived into the codec; Rice coding did not — it cannot spend fractional bits. |
| `polypress/turbo.py` (deleted 2026-09-29, in git at `c55f68f`) | A speed fork with independently compressed column streams (container `PPZT`), so each column had a size of its own and decisions could be local. Cheap for binary payloads (+0.2–5%), expensive for text (+13% where text columns resemble each other). The threaded encoder got the speed without changing the format, so the fork was retired rather than ported. |
| ragged-decimal numeric columns | **Tried, measured, reverted.** `latitude` is rejected as numeric because its decimal places vary row to row (11–15) and it has 1,153 blanks — and since every modern language prints floats at shortest-round-trip precision, *every* real float column is ragged. Recovering them by storing a per-cell decimal count and a null mask looked obviously right and made things **9–23% worse**. Two reasons. Scaling to the column's maximum decimal count inflates every value to ~4x10^16, so consecutive differences need 8 bytes each — worse than the 18 characters of text xz already handles well. And more importantly, moving a column out of `text` removes it from the text-reordering machinery: `chicago_crimes` gains 19.3% from a text parent, and converting those columns to numbers gave all of it back. A per-column size probe cannot see that, because the loss lands somewhere else. |

The exploratory scripts from the functional-dependency work (`fd_probe*.py`,
`real_fd.py`, `gap_probe.py`, `skew.py`, `scaling.py`, `sensitivity.py`,
`fair_fight.py`, `explain.py`, `benchmark.py`) were removed once that line was
measured out; they are in git history if ever needed.

Two claims in an earlier version of this README were wrong and are worth
recording: `colmajor.xz` did **not** "win on most real tables" (it lost on
both tables larger than 86k rows), and the strategy-selection container was
**not** "never worse than the best standard tool" — it had no brotli
candidate, and brotli beat it outright.

## The results write-up

`docs/Polypress-Results.pdf` is a summary of every measurement here, including
a page stating plainly what is not done. **It is generated from the sweep
output, not written from it** — every figure in it is computed from the JSONL,
so it cannot drift from the evidence the way the previous version did. That one
carried each number as a literal and was still asserting "beats every
compressor tested, on every dataset tested" two sessions after that stopped
being true.

Regenerate it whenever the codec or the corpus changes:

```bash
python3 docs/report.py docs/Polypress-Results.pdf ../OUT/results/socrata500.jsonl
```

## Tests and benchmarks

```bash
./csrc/tests/run.sh                    # the whole suite, in C, about 30 s:
                                       # codec round trips + fuzzing, threads vs
                                       # serial bytes, readers and encodings,
                                       # streaming, hostile and lying archives
                                       # (each in a watched child), the CLI
SANITIZE=1 ./csrc/tests/run.sh         # the same under AddressSanitizer
./app/build_app.sh                     # runs `Polypress --selftest` as its gate
python3 benchmarks/measure_one.py f.csv  # one table vs all 22 competitors
python3 benchmarks/make_hostile.py d/ # generate the adversarial suite
```

And before releasing anything, the slow one that needs the corpus
downloaded:

```bash
python3 benchmarks/sweep.py ../IN/corpus100/*.csv --out ../OUT/results/socrata100.jsonl
```

`measure_one.py` runs 22 competitors in four families — general purpose
(gzip, bzip2, xz, lz4, zstd at three levels, brotli), archivers (`zip -9`,
`7z` at LZMA2 -mx9 and at PPMd orders 6/12/16), columnar files (Parquet at four
codecs, ORC at three, Feather at two), and Polypress including the
like-for-like re-finishes. The archivers pay for their own container — a zip's
local header and central directory, a 7z's coder description — and that counts
against them on purpose, because a reader who zips a file gets a zip.
`benchmarks/PROTOCOL.md` is the authority on the lineup and the measurement
rules; read it before producing a number.

Parquet and ORC are in the list because a comparison that only beats gzip has
not beaten anything anyone uses; nobody stores a 209-column survey as
compressed CSV. PPMd is in it because every other entry was LZ-family, Huffman
or block-sort, and a lineup of one idea is not a lineup — on the shape suite it
is the strongest rival in the field.

It also runs the fidelity check on every dataset and prints the verdict.
Parquet read with type inference quietly turns `"1.50"` into `1.5`, and on
**77 of the 100 unselected datasets it did exactly that**. Read every Parquet
size with that verdict in hand.

`sweep.py` runs **one subprocess per dataset**, so peak RSS is the largest
single table rather than the accumulated total, and it skips datasets already
present in the output — a multi-hour sweep has to be safe to interrupt.
`--max-mb` truncates oversize inputs at a row boundary, which is honest because
every codec is then handed the identical file. `--rss-abort` stops the run on a
*measured* peak, not an estimated one: the estimate in `CLAUDE.md` was itself
wrong by 18% once. The full 100-dataset sweep took 59 minutes and peaked at
1,706 MB.

`benchmarks/bench.py` and `bench_gov.py` were retired to `attic/`.
`measure_one.py` is a strict superset of the former, and two scripts measuring
the same thing differently is how a repo ends up contradicting its own
evidence.

`test_input_guard.py` exists because `csrc/polypress compress` used to accept
any file at all. Pointed at a `.parquet` it read the **binary as text**, found
883 "rows" of 2 "columns", encoded them, **passed its own round-trip
verification**, wrote the archive, and restored a corrupt file. The
verification compares the parsed table with the decoded table, so it sits
downstream of the misparse and structurally cannot see it — the same shape as
the `errors="replace"` bug below it in this file. The guard screens on magic
bytes and extensions only, never on content: a rule that made C refuse what
Python accepts would break byte-identity in the act of defending it.

`build_app.sh` runs `gui.py --selftest` and refuses to build if it fails. A
malformed AppleScript only surfaces when the user clicks something, so it is
checked at build time -- an earlier version shipped a file-type list joined
with spaces where AppleScript wanted commas, and the first click failed.

`test_lying_header.py` covers what mutation fuzzing structurally cannot. The
corrupt-archive suites damage bytes, and a random mutation essentially never
produces an archive that decompresses cleanly, parses as valid JSON, and then
*lies about its own shape*. That gap was real: three unchecked array indices
survived every fuzzing pass in this repo and had to be found by reading. So
this suite builds the header deliberately — 27 specimens claiming ghost
columns, parents that do not exist, row counts larger than the file, exception
counts larger than the table. **It found a segfault on its first run**: a
header claiming a differencing order of 2^20 walked a million entries off the
end of an eight-element stack array in the C decoder. Python refused all 27
before the fix; only C crashed, which is the usual shape here.

`test_fuzz.py` builds tables out of deliberately awful cells -- int64
boundaries, ragged decimals, embedded quotes and newlines, non-BMP characters,
empty headers -- and checks both that the table round-trips and that the two
implementations emit the same bytes. It found three real bugs on its first
run: a JSON parser storing int64 values in a double (74884171959489212 decoded
as ...216), an `a <= 8*b` test that overflowed int64 and silently refused
every large numeric group, and an overflow check in `tcz.c` placed *after* the
multiply, so `INT64_MIN` was accepted as numeric while the numpy fallback
rejected it -- the same file compressing differently depending on whether a
compiler was present.

The C binary is also fuzzed against corrupt input, because reading an archive
means reading a file somebody else made. That found the worst bug of the
lot: both decompression wrappers grew their buffer and retried on *any*
failure, and corrupt data never decodes at any size, so they doubled from
64 KB toward a terabyte. 132 of 199 mutated inputs hit it.

`test_fast.py` runs every case twice -- once through the C accelerator and
once through the numpy fallback -- because an accelerator that disagrees with
the reference is not an accelerator, it is a second codec. It caught three
real bugs: a 32-bit varint tail that truncated values past 2^31, newline-
joined text storage that split any cell containing a newline, and a division
by zero on an empty table.
