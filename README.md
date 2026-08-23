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
output, which are not competitors):

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

So the guarantee is **"never worse than our own plain fallback"**, not "never
worse than any tool run on your original bytes". The gap is 0.1%–1.6% on nine
of 500 tables. It is stated here rather than left for someone else to find.

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

`tzip.py info` explains where the win comes from: **172 of the 200
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
                corpus/ corpus100/ corpus500/ corpus_matrix/ corpus_hostile/
                plus polypress-demo.csv and polypress-demo-hard.csv
OUT/results/    sweep output: the JSONL, summaries and CSVs that are the evidence
work/           everything below is inside work/ -- run commands from there
  polypress/    the codec: fast (single-shot), stream (bounded memory),
                dtz (table I/O), codec, caccel + tcz.c (C accelerator)
  csrc/         the standalone C binary -- reads archives with nothing installed
  tzip.py       command line entry point
  app/          the Mac app: gui.py, build_app.sh, make_icon.py
  tests/        fidelity suites
  benchmarks/   size and speed against real binaries
  docs/         the results PDF and the script that generates it
  attic/        superseded work, kept for the record
  pyproject.toml
```

## The standalone binary

```bash
./csrc/build.sh                         # -> csrc/polypress
./csrc/polypress compress data.csv      # no Python, no numpy
./csrc/polypress restore  data.csv.ppz
./csrc/polypress info     data.csv.ppz
```

**Using it needs nothing installed.** That is the point. The Python codec
needs Python 3.9+, numpy, and ideally a compiler; a researcher sent a `.ppz`
should not have to build an environment to open it.

**The C encoder is byte-identical to the Python one.** Not "equivalent" —
the same bytes, verified on every case in `tests/test_cbin.py` (39/39,
including 421-column real NHANES data), plus 1,450 randomly generated tables
through `tests/test_fuzz.py` across five seeds. The comparison is against
`fast.encode`, so the choice of *which* container to write is covered as well
as the bytes inside it. That is the strongest correctness
signal available: any divergence is a bug with a known location, and the
Python implementation stays usable as the oracle.

Getting there required three things that are not obvious, and each is
commented where it lives:

- **Summation order.** The parent search compares entropies, and `np.sum` is
  pairwise, not left-to-right. A different last bit flips a `>`, picks a
  different parent, and changes every byte after it. `pairwise_sum()`
  reproduces numpy's algorithm, block size and all.
- **Tie-breaking.** `pick_parents` used a Python `set`, so which of two
  equally-good parents won was an artefact of CPython's hash table. It is a
  list in both languages now — lowest column index wins. An encoder whose
  output can shift with an interpreter's internals is not one to build a
  format on, so this is a fix regardless of the port.
- **JSON.** The metadata is compared byte for byte, so the emitter matches
  `json.dumps(..., separators=(",",":"))` exactly, including `ensure_ascii`
  escaping and surrogate pairs above the BMP.

Speed is a side effect, not the reason. Compress is ~1.4x faster on NHANES,
restore ~1.3x. Profiling puts liblzma at 62–100% of encode time, so there was
never much to win: the Python around it was not the bottleneck.

`tests/test_cbin.py` runs the shared corpus plus cases that force each piece
of machinery — the parent permutation, the 2D group reconstruction, undiff at
orders 1 to 3, the 8-byte varint tail, and JSON escaping via non-ASCII column
names — and checks both directions: bytes out of the encoder, cells out of
the decoder. All three container types are covered.

**The C encoder now makes the same container choice too**, which it did not
until 2026-07-27. It used to always write the modelled container, so on a
table where none of the three tricks fired it produced a larger file than
`tzip.py compress` — never a *wrong* one, but bigger, and that broke the
"never worse" rule for anyone using the standalone binary. It now builds the
same canonical CSV, checks it round-trips, and takes the smallest of xz,
bzip2 and modelled. Across the fidelity case set that is **52.6% off in
total**, and up to **13x on a very small table** (163 bytes → 12), which is
exactly the "I tried it on a small file first" case a new user hits.

On real data it changes much less: of 15 real and adversarial tables, only two
moved at all (`high_precision` 3.3%, `base64_blob` 1.3%), because on real
tables a trick usually does fire. Both numbers are worth stating — the fix
matters for the guarantee and for first impressions, not for the headline
ratios.

The subtle part is that the fallback compresses the table re-serialised as
canonical CSV, so the C writer has to match Python's `csv.writer` byte for
byte — including that a bare `\r` is *not* quoted, that an empty field *is*
quoted when it is alone in its row, and that a NUL forces quoting. The first
of those makes Python's own CSV lossy for such a cell, so the candidate is
re-parsed and compared before it is allowed to win, and both implementations
then decline it and agree. `tests/test_cbin.py` pins nine such cases.

Needs `liblzma` and `libbz2` headers (`brew install xz`, or
`apt install liblzma-dev libbz2-dev`). The build script finds them via
pkg-config. liblzma 5.4.3 and 5.8.3 were both verified to emit byte-identical
output to Python's `lzma` module for this filter chain, which is what makes a
byte-identical port possible at all.

## The three ideas

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
pip install polypress          # the codec and the `polypress` command
pip install 'polypress[parquet]'   # add pyarrow, for reading/writing .parquet
```

numpy is the only hard requirement. The C accelerator compiles itself on first
import and falls back to numpy if there is no compiler, so it is never a
dependency. Or use it straight from a checkout with no install at all --
`python3 tzip.py ...` still works and calls the same code.

For the standalone binary, which needs no Python at all, see
[The standalone binary](#the-standalone-binary).

## Use

```bash
polypress compress data.csv                # -> data.csv.ppz
python3 tzip.py compress data.csv          # identical, no install needed
python3 tzip.py restore  data.csv.ppz      # -> data.csv
python3 tzip.py restore  data.csv.ppz -o out.parquet
python3 tzip.py info     data.csv.ppz      # plan, shape, how much was reordered
```

Restoring writes whatever format the output extension asks for, so it doubles
as a converter.

For a file larger than RAM, the same three commands in a block-at-a-time form
with a settable memory budget:

```bash
python3 tzip.py stream-compress big.csv --budget 1.0   # ~1 GB peak
python3 tzip.py stream-restore  big.csv.ppz -o back.csv
python3 tzip.py stream-info     big.csv.ppz            # blocks and sizes
```

Blocks are compressed independently, so peak memory is one block rather than
one file. The cost is real: the cross-column reordering only sees correlations
*inside* a block, so smaller blocks compress slightly worse. Restoring honours
the output extension here too — `.parquet` output becomes one row group per
block, which keeps the write bounded as well.

Or the Mac app:

```bash
./app/build_app.sh          # installs to ~/Applications/Polypress.app
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

Three ways to use it:

- **launch it** — pick any table, get a `.ppz`
- **double-click a `.ppz`** — restores it; the bundle registers the extension
  (archives written before the rename, `.tcz`, still open)
- **drop files on the Dock icon** — same thing

Right-click the Dock icon → Options → Keep in Dock. Nothing is written until
the compressed blob has been decoded in memory and compared to the original.

The app is an AppleScript droplet rather than a shell wrapper for one
reason: only an applet receives the `on open` Apple Event Finder sends when
you double-click a document. A plain launcher never sees the path.

Needs Python 3.9+ and numpy. `cc` is optional — `caccel.py` compiles
`tcz.c` on first import and falls back to numpy if there is no compiler.

## Fidelity

The **logical table** round-trips exactly: column names, column order, row
order, and every cell as an exact string. Verified in memory on every
compress.

It does *not* promise byte-identical files, because CSV quoting and line
endings are not canonical. Read a file and write it back and you get an
equivalent table, not identical bytes.

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
`tests/test_encoding.py` pins all of it.

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

**The guarantee is: never worse than xz or bzip2 on any table**, because both
are carried as candidates and the smaller wins. As the section above records it
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

- **Pure Python + numpy + a small C library, and it sits in the slow tier.**
  Median across the 100 unselected datasets: **2.0 MB/s encode, 134 MB/s
  decode** — down from 3.6 MB/s encode before the fallback check became
  unconditional, which is what the "never worse" guarantee cost. For context,
  on the same corpus and the same machine, `xz -9e` is 3.9 / 203,
  `brotli -q 11` is 1.1 / 501, `zstd -22` is 2.8 / 871 and `zstd -3` is
  187 / 716. So encoding is now slower than `xz -9e` and roughly twice
  `brotli -q 11`, and two orders of magnitude off the fast tier, which is where
  it will stay. Note also that these are not measured on the same basis: the
  general-purpose tools are handed raw CSV bytes, while Parquet, ORC, Feather
  and Polypress are handed an already-parsed table and are not charged for
  reading the CSV.
- **Never-worse costs encode time, and the bill has gone up.** Every guard in
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
- **Bit-identity with numpy is not achievable, and the codec no longer depends
  on it.** `tests/test_cbin_corpus.py` — the first check of invariant 1 against
  real data rather than constructed cases — found three C/Python divergences.
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
- **Encode got slower to make "never worse" true.** Every table now builds the
  canonical CSV and checks the plain fallbacks against the modelled encoding.
  Measured before the abort-early cap: **+63% encode time**. That bought the
  guarantee — `xz`, `bzip2` and `zstd -22` all go to 100/100 — and it is the
  right trade, but it is a real cost and it is not yet reclaimed on the C side,
  which still compresses each candidate in full.
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
| `polypress/dtz.py` | The earlier "try every strategy and keep the smallest" container. Its apparent 1% win over `xz -9e` turned out to be **CSV quote-stripping, not compression** — feeding xz the same canonicalised bytes matched it to within 68 bytes. |
| `polypress/codec.py` | Rice coding and the original fixed-order predictors. The predictor idea survived into `fast.py`; Rice coding did not — it cannot spend fractional bits. |
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
python3 tests/test_fast.py            # 32 fidelity cases, C path and fallback
python3 tests/test_dtz.py             # 18 fidelity cases for the table I/O
python3 tests/test_encoding.py        # BOMs, UTF-16, latin-1: read or refuse
python3 tests/test_stream.py          # 180 checks: block counts and every output format
python3 tests/test_lying_header.py    # headers that are well-formed and dishonest
python3 tests/test_cbin.py            # the C binary must agree with Python on every case
python3 tests/test_fuzz.py [n] [seed] # random adversarial tables through both implementations
python3 tests/test_input_guard.py     # the C binary must refuse what it cannot parse
python3 benchmarks/measure_one.py f.csv  # one table vs all 20 competitors
python3 benchmarks/make_hostile.py d/ # generate the adversarial suite
python3 app/gui.py --selftest         # compile every AppleScript the app can emit
```

And before releasing anything, the two slow ones that need the corpus
downloaded:

```bash
python3 benchmarks/sweep.py ../IN/corpus100/*.csv --out ../OUT/results/socrata100.jsonl
python3 tests/test_cbin_corpus.py ../IN/corpus100/*.csv   # invariant 1 on real data
```

`measure_one.py` runs 20 competitors in three families — general purpose
(gzip, bzip2, xz, lz4, zstd at three levels, brotli), columnar files (Parquet
at four codecs, ORC at three, Feather at two), and Polypress including the
like-for-like re-finishes. Parquet and ORC are in the list because a comparison
that only beats gzip has not beaten anything anyone uses; nobody stores a
209-column survey as compressed CSV.

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
