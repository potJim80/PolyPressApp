# Polypress

A lossless compressor for data tables.

`polypress/fast.py` compresses data tables smaller than xz, zstd, brotli, Parquet, and
the specialised numeric codecs in ClickHouse — on every *real* table tested so
far — and encodes several times faster than the max-level general compressors.

It does **not** win on everything. A deliberately adversarial suite
(`benchmarks/make_hostile.py`) found four tables it loses on; they are listed
under [Where it loses](#where-it-loses), because a compressor whose failure
cases are unknown is a compressor nobody should trust with their data.

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

`tzip.py info` explains where the win comes from: **172 of the 200
dictionary columns were sorted by a parent**. Survey columns predict each
other heavily, and no columnar format exploits that — Parquet compresses
each column chunk independently.

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

```
polypress/      the codec: fast (single-shot), stream (bounded memory),
                dtz (table I/O), codec, caccel + tcz.c (C accelerator)
tzip.py         command line entry point
app/            the Mac app: gui.py, build_app.sh, make_icon.py
tests/          fidelity suites
benchmarks/     size and speed against real binaries
docs/           the results PDF and the script that generates it
attic/          superseded work, kept for the record
```

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

## Use

```bash
python3 tzip.py compress data.csv          # -> data.csv.ppz
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
./app/build_app.sh      # installs to ~/Applications/Polypress.app
open ~/Applications/Polypress.app
```

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

## Where it loses

Six real datasets is not a claim, and every one of them was a table this codec
was designed for. So `benchmarks/make_hostile.py` generates ten tables built to
break specific assumptions in it. Four of them do:

| hostile case | vs best other | what it breaks |
|---|---|---|
| `single_wide_row` | **1.11x LARGER** | 5,000 columns, 1 row. Per-column overhead with nothing to amortise it over — and encode collapses to **0.5 MB/s**, decode to 3.6 MB/s. |
| `high_precision` | **1.03x LARGER** | Decimal places that vary per row, so no fixed scale fits and the numeric parser cannot hold a column together. |
| `base64_blob` | **1.02x LARGER** | Already-compressed bytes. Nothing to find; we pay container overhead for the privilege. |
| `random_text` | **1.01x LARGER** | No structure at all. This is the floor, and near-parity here is the correct result. |

The six it still wins on are the informative half. `anticorrelated` — a
high-cardinality numeric column that a naive conditional-entropy score would
happily adopt as a parent — comes out **4.24x** ahead, which is the
Miller-Madow correction earning its place. `wide_random` (200 mutually
independent numeric columns) wins by only 1.02x, and that is the honest result:
the O(columns²) parent search does its maximum work for almost no reward.

Two things worth taking from this:

- **The losses are small and they are graceful.** Three of the four are within
  3% of the best alternative. Nothing here degrades catastrophically on size.
- **`single_wide_row` is a genuine defect, not a tie.** Losing 11% is
  survivable; encoding at 0.5 MB/s is not. Extremely wide, shallow tables are
  the one shape to fix.

Full numbers, every contender, in `benchmarks/hostile-results.txt`.

## Honest limitations

- **Pure Python + numpy + a small C library.** Encode is 9–30 MB/s. That
  beats `xz -9e` (5), `brotli -q 11` (1.1) and `zstd -22` (4), but it is
  nowhere near the fast tier — `zstd -3` encodes at 173 MB/s and always will.
- **The 2x cases are matrix-shaped tables.** The 1.3–1.5x cases are the more
  typical result.
- **Six real datasets.** Still not a claim. The hostile suite above covers the
  "tables that are hostile to it" half; what is still missing is *real* breadth
  — census panels, NOAA grids, genomics tables.
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

## What else is in here, and what it proved

These are kept because the negative results are the useful part.

| file | verdict |
|---|---|
| `attic/exact_interp.py` | **Exact polynomial interpolation cannot compress.** Storing the interpolating polynomial's coefficients costs *more* than the values, and gets worse with more points (6.8x worse at n=24). Interpolation is an invertible linear map — n values in, n coefficients out. |
| `attic/smart.py`, `attic/rc.py` | A working adaptive binary range coder with cross-column context modelling. **Superseded**: reordering plus xz beat it on both size and speed. Kept as the reference implementation. |
| `polypress/dtz.py` | The earlier "try every strategy and keep the smallest" container. Its apparent 1% win over `xz -9e` turned out to be **CSV quote-stripping, not compression** — feeding xz the same canonicalised bytes matched it to within 68 bytes. |
| `polypress/codec.py` | Rice coding and the original fixed-order predictors. The predictor idea survived into `fast.py`; Rice coding did not — it cannot spend fractional bits. |

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

`docs/Polypress-Results.pdf` is a four-page summary of every measurement here,
including a page stating plainly what is not done. Regenerate it with:

```bash
python3 docs/report.py docs/Polypress-Results.pdf
```

## Tests and benchmarks

```bash
python3 tests/test_fast.py            # 29 fidelity cases, C path and fallback
python3 tests/test_dtz.py             # 18 fidelity cases for the table I/O
python3 tests/test_stream.py          # 180 checks: block counts and every output format
python3 benchmarks/bench.py data.csv  # size and speed vs the binaries AND Parquet
python3 benchmarks/make_hostile.py d/ # generate the adversarial suite
python3 app/gui.py --selftest         # compile every AppleScript the app can emit
```

`bench.py` includes the Parquet lineup, because Parquet is the honest
competitor and a comparison that only beats gzip has not beaten anything anyone
uses. It also runs a fidelity check on every dataset and prints the verdict:
Parquet read with type inference can quietly turn `"1.50"` into `1.5`, and on
three of the ten hostile datasets it did exactly that. Read the size column
with that verdict in hand.

`bench.py` refuses inputs over 80 MB by default (`--max-mb` to override).
`fast.py` expands CSV roughly 8.5x into Python strings and benchmarking holds
an encoded and a decoded copy at once, so 80 MB is already about 1.4 GB
resident. Use `stream-compress` for anything larger.

`build_app.sh` runs `gui.py --selftest` and refuses to build if it fails. A
malformed AppleScript only surfaces when the user clicks something, so it is
checked at build time -- an earlier version shipped a file-type list joined
with spaces where AppleScript wanted commas, and the first click failed.

`test_fast.py` runs every case twice -- once through the C accelerator and
once through the numpy fallback -- because an accelerator that disagrees with
the reference is not an accelerator, it is a second codec. It caught three
real bugs: a 32-bit varint tail that truncated values past 2^31, newline-
joined text storage that split any cell containing a newline, and a division
by zero on an empty table.
