# Polypress

**A lossless compressor for data tables.** CSV, Parquet, and anything else
shaped like rows and columns.

General-purpose compressors see a table as a stream of bytes. Columnar formats
see it as columns, and compress each one on its own. Polypress is built on the
fact that **the columns of a real table are not independent of each other** —
`City` is nearly determined by `Postal Code`, `latitude` is repeated inside
`location`, adjacent sensor columns hold nearly the same number — and models
those relationships directly.

The original table comes back **exactly**: same columns, same column order,
same row order, every cell as the exact string it was.

```sh
pip install polypress
polypress compress data.csv        # -> data.csv.ppz
polypress restore  data.csv.ppz    # -> data.csv
polypress info     data.csv.ppz    # plan, shape, how much was reordered
```

## The benchmark, and how it was chosen

The obvious objection to any compression result is *"you picked the files"*, so
the main benchmark does not pick. It asks the Socrata open-data catalog for its
datasets **in descending order of page views** and takes the first 500 that
pass four mechanical filters (the CSV downloads; at least 2 columns and 20
rows; at least 50 KB; not a duplicate). Rank order is public and fixed, so the
list reproduces, and every rejection is recorded with its reason.

**500 tables, 3.57 GB of CSV, 14.3 million rows, 10,557 columns**, against 17
competing codecs:

| | |
|---|---|
| Round-trips exactly | **500 of 500** |
| Smaller than the best of all 17 competitors | **478 of 500** |
| Margin over the best other tool | median **1.25x**, best 2.73x, worst 0.65x |
| Compression vs raw CSV | median **14.35x** |

Head to head: it beats `xz -9e`, `zstd -22`, `gzip -9` and `lz4 -9` on **all
500**; `parquet+zstd` on 449 of 450; `bzip2 -9` on 491 of 500; `brotli -q 11`
on 487 of 500.

**Two things belong next to those numbers.** Parquet, read with type inference,
reproduced the exact printed text on only 81 of the 500 tables — turning
`"1.50"` into `1.5` makes a smaller file for reasons that have nothing to do
with compression, and Polypress guarantees the printed cell. And the margin is
not a better final compressor: re-finishing the same modelled streams with the
competitor's own entropy coder still wins 449 of 450 against `parquet+zstd`.

## How it works

1. **Predict down a column.** Fit a low-degree polynomial to the last few
   values, extrapolate one step, store the error. Re-centring the fit at every
   cell means the coefficients never have to be stored — order-*k*
   extrapolation is exactly the *k*-th finite difference.
2. **The same thing in two dimensions.** Where adjacent numeric columns are
   *commensurable*, a cell is predicted from its left, upper and upper-left
   neighbours. This is what wins on matrix-shaped tables, and no shipped
   columnar codec does it — Gorilla, DoubleDelta, T64, zfp and fpzip all
   predict down a single column.
3. **Reorder the rows so a column collapses into runs.** Rather than model that
   `City` depends on `Postal Code`, sort by the parent: equal parents become
   adjacent and the child collapses. **The permutation is free** — the decoder
   has already rebuilt the parent and recomputes the same stable sort. Your
   table comes back in its original row order.

## What it is honest about

- **It is slow to compress.** Median 2.0 MB/s encode, 134 MB/s decode. The
  never-worse guarantee is what costs it: eligible tables are encoded twice and
  the smaller result wins.
- **The core idea is not novel.** US 8,312,026 B2 (Vo, AT&T, filed 2009)
  discloses it. It was arrived at here independently and the novelty claim was
  withdrawn. Both relevant patents have expired.
- **The 2x results are matrix-shaped tables.** 1.3–1.5x is typical, and
  text-heavy tables are the weak genre.
- **All 22 losses out of 500 are published**, including the one genuine
  undiagnosed loss.
- **Every benchmark dataset is a government open-data table.** There is no
  reason to assume the result transfers to other genres.
- **Nobody outside the project has run it.**

## Requirements

Python 3.9+ and numpy. A C compiler is optional — the accelerator compiles
itself on first import and falls back to numpy without one. `pyarrow` is
needed only for `.parquet` input and output: `pip install 'polypress[parquet]'`.

## More

Source, tests, the full benchmark harness and the committed sweep results:
**https://github.com/potJim80/PolyPressApp** — including `memory/LAWS.md`, the
findings that govern what gets built, and the README section listing everything
that was tried and failed.

MIT licensed.
