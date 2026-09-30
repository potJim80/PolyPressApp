# Benchmark protocol

How this project measures itself. The rules are here so that a number in the
README can be traced to a command, and so that two runs a month apart are
comparable. **If a run departs from this document, the departure gets written
down next to the number, not left for the reader to discover.**

## 1. What each benchmark answers

Three corpora, three different questions. They are not interchangeable and
their totals are not comparable to each other.

| corpus | question | size | run time |
|---|---|---|---|
| **suite** (`IN/suite/`, 39 files) | *What shapes is this codec good at?* | 267 MB | ~30 min |
| **socrata100** (`IN/corpus100/`) | *Does it win on a random government table?* | 1.0 GB | ~2 h |
| **socrata500** (`IN/corpus500/`) | The same, at a size where 22 losses can be decomposed | 4.0 GB | ~10 h |

The suite is the one to run after a codec change. It is tagged by shape, so it
answers *where* a change helped, which an aggregate ratio over 500 government
tables cannot. The Socrata sweeps are the headline claims and are re-run before
a release, not after every commit.

## 2. The lineup

Every table is measured against **22 competitors** in four families. A lineup
of one idea is not a lineup — that is why PPMd (a context model) and the
archivers are in it, not just the LZ family.

| family | entries |
|---|---|
| general purpose | `gzip -9`, `bzip2 -9`, `xz -9e`, `lz4 -9`, `zstd -3`, `zstd -19`, `zstd -22`, `brotli -q11` |
| archivers | `zip -9`, `7z LZMA2 -mx9`, `7z PPMd` at orders 6, 12, 16 |
| columnar files | Parquet at snappy / gzip-9 / brotli-11 / zstd-22, ORC at snappy / zlib / zstd, Feather at lz4 / zstd |
| this codec | `polypress` (the C program, `csrc/polypress`, run as its own process with `PPZ_THREADS=1`), and `polypress+zstd` / `polypress+brotli` (re-finished, see §5) |

Rules for the lineup:

- **Everyone gets the identical input file.** Not "the same data" — the same
  bytes. Where the suite truncates a table, it is truncated once and every
  codec is handed the result.
- **Container overhead counts against the tool that has it.** `zip` and `7z`
  carry a file name and a central directory; the piped tools do not. This
  costs them a few hundred bytes on a 20 KB table and it stays counted,
  because a reader who zips a CSV gets a zip, not a raw deflate stream.
- **A tool that cannot run on a table sits that table out, and the report says
  so** (the `n` column). ORC is skipped above 1,000 columns — it buffers about
  half a megabyte per column and peaked at 2,363 MB on the 78 KB
  `xs_wide_row.csv`. Silently averaging over the tables a format managed is
  how a format gets flattered.
- **Adding a competitor is a change to this file.** New entries go in
  `measure_one.py` and are listed above in the same commit.

Deliberately not in the lineup: anything not installable as one command
(`zpaq`, `cmix`, `bsc`), and any tool that is lossy on the printed text.

## 3. Rules of measurement

1. **One subprocess per dataset.** Peak RSS is then the largest single table,
   not the accumulated total, and a crash costs one dataset rather than the
   run.
2. **Round-trip or the result is void.** Both CLIs decode and compare every
   cell before a file is written; the sweep records `roundtrip` per dataset and
   the report prints failures at the top. **A size with a failed round trip is
   not a result.**
3. **Peak RSS is measured, never estimated.** The estimate in CLAUDE.md is only
   used to decide what to start. `--rss-abort` stops a run on a measured
   breach.
4. **Sizes are exact and reproducible; times are not.** One timing pass per
   codec (`REPS=1`), taken on a machine that was quiet but not quiesced. Read
   the times for order of magnitude and nothing finer. Raise `--reps` when
   measuring a single file.
5. **The timings are not on a common basis, and that matters more than the
   noise.** The general-purpose tools are handed raw CSV bytes, because
   compressing a CSV is what they do. Parquet, ORC, Feather and Polypress are
   handed an already-parsed table, so none of them is charged for reading the
   CSV. Compare the columnar rows to the polypress row; compare either to gzip
   only as a rough guide.
6. **Environment is pinned.** Every numeric library to one thread
   (`OMP_NUM_THREADS=1` and friends), `PYTHONHASHSEED=0`, `nice -n 5`, no other
   heavy process on the machine. Threads change timings, and multi-threaded
   `zstd`/`7z` change *sizes*.
7. **Parquet's fidelity is checked, not assumed.** The typed columnar path is
   read back and compared against the printed text. On the Socrata 500 it
   failed to reproduce it on **356 of 500**. Where it changed the text, part of
   any Parquet size win is discarded formatting rather than compression —
   `"1.50"` becoming `1.5` is a smaller file for a reason that is not the
   codec. **Always quote that fraction next to a Parquet comparison.**

## 4. The suite

`benchmarks/make_suite.py` builds `IN/suite/` and a `MANIFEST.json` carrying,
per file: tier, forms, origin, source, rows, columns, bytes, sha256.

**Tiers** are size, and they exist because container overhead and model warm-up
are only visible at the small end:

| tier | size | n |
|---|---|---|
| `xs` | < 100 KB | 6 |
| `s` | 100 KB – 1.3 MB | 8 |
| `m` | 1.3 – 6 MB | 15 |
| `l` | 6 – 28 MB | 10 |

**Forms** are shape, and a file carries several: `categorical`, `text`, `geo`,
`numeric`, `matrix`, `scientific`, `ids`, `timestamps`, `sparse`, `survey`,
`wide`, `narrow`, `unicode`, `redundant`, `sorted`, `shuffled`, `hostile`,
`ladder`. The report groups by these. **Never publish an aggregate over the
suite without the breakdown** — 3.7x on coded categorical data and 0.9x on
random floats average to a number true of neither.

**Origins**, and the distinction is load-bearing:

- `real` — a published table, copied whole.
- `slice` — the first N bytes of one, cut at a true row boundary with quotes
  tracked, so an embedded newline never splits a row.
- `generated` — deterministic synthetic data for a genre no file on disk
  covers: server logs, an OHLCV panel, an IoT stream, e-commerce transactions,
  variant calls, and multilingual UTF-8. Seeded from one constant, so the
  suite rebuilds byte for byte.

Two entries are deliberate experiments rather than samples:

- **`m_financial_sorted` / `m_financial_shuffled`** are the same rows in two
  orders. The difference between them is what row order alone is worth.
- **`m_ecommerce`** contains both patterns the cross-column probe measured as
  the largest unclaimed win: a concatenated key (`order_id`) and a derived
  column (`line_total` = qty × price).

**The ladder is one table, not four results.** `xs_nndss_500`, `m_nndss_2mb`,
`l_nndss_8mb` and `l_nndss_full` are `cdc_nndss` at 70 KB, 2 MB, 8 MB and
20.8 MB. They answer "does the ratio depend on size" and must not be counted as
four independent wins. The report prints them as their own section.

**A changed hash means the input changed, not the codec.** If a number moves,
check the manifest before blaming a commit.

## 5. Reporting rules

`benchmarks/report.py` prints five sections, in this order, because they are
five different strengths of claim:

1. **Against the best competitor on that table** — not against the mean of the
   field and not against gzip. A reader will use the best tool they have.
2. **Head to head against each competitor.** Parquet is the row that matters;
   nobody stores a survey as compressed CSV.
3. **Like for like.** Polypress finishes with xz and Parquet cannot use xz at
   all (`pyarrow` answers `Unsupported compression: xz`), so part of any margin
   is the finisher. The `polypress+zstd` / `polypress+brotli` rows re-finish the
   *same modelled streams* with the competitor's own coder. **If the win ever
   stops surviving that, the honest summary changes.**
4. **Parquet fidelity**, per §3.7.
5. **Speed**, with the caveats of §3.4 and §3.5.

Then, with `--manifest`, the by-tier and by-form breakdown and the ladder.

And two standing rules:

- **Every loss is printed in full, never summarised away.** A result file that
  lists its failures is worth more than one that has none.
- **Negative results get written down** — in the README table and in
  `OUT/results/`. Several of the most valuable entries in this repo are things
  that did not work.

## 6. Running it

```bash
cd work
python3 benchmarks/make_suite.py ../IN/suite          # idempotent; --force to rebuild
./benchmarks/run_suite.sh                             # sweep + report, pinned env
```

or by hand:

```bash
python3 benchmarks/sweep.py ../IN/suite/*.csv \
        --out ../OUT/results/suite-v1.jsonl --max-mb 30
python3 benchmarks/report.py ../OUT/results/suite-v1.jsonl \
        --manifest ../IN/suite/MANIFEST.json \
        --title "Polypress suite v1" \
        --csv ../OUT/results/suite-v1-results.csv
```

The sweep **skips datasets already in the output file**, so an interrupted run
resumes. To force a re-measure, delete the `.jsonl` — do not edit it by hand.

Single file, with real timings:

```bash
python3 benchmarks/measure_one.py ../IN/suite/l_nndss_full.csv --reps 3
```

## 7. What invalidates a result

- A round-trip failure anywhere in the run.
- A `sha256` in the manifest that no longer matches the file on disk.
- A codec change landing between the sweep and the report. **Regenerate the
  results file whenever the codec changes; never hand-patch a table from a
  commit message.** Doing that once left five of thirteen README rows wrong.
- Comparing byte totals across sweeps with different `--max-mb` caps. The
  100-corpus capped at 28 MB and the 500-corpus at 16 MB; **their absolute
  totals are not comparable, only their ratios are.**

## 8. Known non-comparabilities, stated up front

- **"Never worse" means never worse than our own plain fallback**, which
  compresses the table re-rendered through `csv.writer`. It does not mean never
  worse than any tool run on your original bytes. On the Socrata 500, nine
  losses were to `bzip2 -9` on the original file, where normalising the quoting
  removed redundancy the BWT was exploiting: the canonical CSV was 79 KB
  *smaller as text* and still compressed 117 bytes *worse*.
- **`brotli -q11` is not a carried fallback**, so a loss to it is a loss to a
  tool this codec never runs. Twelve of the 500 were that.
- **Every dataset in both Socrata sweeps is a government open-data table.**
  Breadth of genre is exactly what the suite is for, and even the suite's
  non-government entries are generated rather than found.
