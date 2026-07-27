# A lossless compressor for data tables

`fast.py` compresses data tables smaller than xz, zstd, brotli, Parquet, and
the specialised numeric codecs in ClickHouse — on every table tested so far —
and encodes several times faster than the max-level general compressors.

Measured against real binaries on real files from data.gov:

| dataset | shape | ours | best industry | win | enc MB/s | dec MB/s |
|---|---|---|---|---|---|---|
| Treasury yield curve | 7,003 x 8 | 21,968 | 44,148 `xz -9e` | **2.01x** | 21.5 | 90 |
| Treasury + dates | 7,003 x 9 | 26,542 | 50,572 `xz -9e` | **1.91x** | 20.1 | 89 |
| WA EV population | 289,564 x 16 | 2,258,862 | 3,917,084 `xz -9e` | **1.73x** | 30.5 | 121 |
| EPA supply-chain GHG | 18,288 x 8 | 79,009 | 116,900 `xz -9e` | **1.48x** | 14.5 | 178 |
| LA crime (200k slice) | 200,000 x 28 | 3,537,591 | 4,739,732 `xz -9e` | **1.34x** | 11.6 | 125 |
| NHAMCS survey (CDC) | 96,539 x 209 | 3,956,941 | see below | **73.65x raw** | 11.9 | 174 |

The NHAMCS row is the widest table tested — 209 columns, 200 of them
categorical — and gives the largest ratio by far. Its industry comparison was
measured on a 5,000-row slice rather than the full 278 MB file:

| codec | slice bytes | ratio |
|---|---|---|
| **ours** | **234,208** | **64.79x** |
| `xz -9e` | 355,280 | 42.71x |
| `brotli -q 11` | 370,934 | 40.91x |
| `zstd -19` | 383,027 | 39.62x |
| `bzip2 -9` | 416,249 | 36.46x |

so **1.52x smaller than the best general-purpose tool** on the shape this
codec is built for: many correlated categorical columns.

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
python3 -c "
import dtz, fast
t = dtz.read_any('data.csv')
open('data.tcz','wb').write(fast.encode(t))
"
```

Or the Mac app:

```bash
./build_app.sh          # installs to ~/Applications/TableZip.app
open ~/Applications/TableZip.app
```

Right-click its Dock icon → Options → Keep in Dock. Pick a file, get a
`.tcz`; pick a `.tcz`, get the table back. Nothing is written until the
compressed blob has been decoded in memory and compared to the original.

Needs Python 3.9+ and numpy. `cc` is optional — `caccel.py` compiles
`tcz.c` on first import and falls back to numpy if there is no compiler.

## Fidelity

The **logical table** round-trips exactly: column names, column order, row
order, and every cell as an exact string. Verified in memory on every
compress.

It does *not* promise byte-identical files, because CSV quoting and line
endings are not canonical. Read a file and write it back and you get an
equivalent table, not identical bytes.

## Honest limitations

- **Pure Python + numpy + a small C library.** Encode is 9–30 MB/s. That
  beats `xz -9e` (5), `brotli -q 11` (1.1) and `zstd -22` (4), but it is
  nowhere near the fast tier — `zstd -3` encodes at 173 MB/s and always will.
- **The 2x cases are matrix-shaped tables.** The 1.3–1.5x cases are the more
  typical result.
- **Six datasets.** Not yet a claim. It needs census panels, NOAA grids, and
  tables that are hostile to it.
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
| `exact_interp.py` | **Exact polynomial interpolation cannot compress.** Storing the interpolating polynomial's coefficients costs *more* than the values, and gets worse with more points (6.8x worse at n=24). Interpolation is an invertible linear map — n values in, n coefficients out. |
| `smart.py`, `rc.py` | A working adaptive binary range coder with cross-column context modelling. **Superseded**: reordering plus xz beat it on both size and speed. Kept as the reference implementation. |
| `dtz.py` | The earlier "try every strategy and keep the smallest" container. Its apparent 1% win over `xz -9e` turned out to be **CSV quote-stripping, not compression** — feeding xz the same canonicalised bytes matched it to within 68 bytes. |
| `codec.py` | Rice coding and the original fixed-order predictors. The predictor idea survived into `fast.py`; Rice coding did not — it cannot spend fractional bits. |

The exploratory scripts from the functional-dependency work (`fd_probe*.py`,
`real_fd.py`, `gap_probe.py`, `skew.py`, `scaling.py`, `sensitivity.py`,
`fair_fight.py`, `explain.py`, `benchmark.py`) were removed once that line was
measured out; they are in git history if ever needed.

Two claims in an earlier version of this README were wrong and are worth
recording: `colmajor.xz` did **not** "win on most real tables" (it lost on
both tables larger than 86k rows), and the strategy-selection container was
**not** "never worse than the best standard tool" — it had no brotli
candidate, and brotli beat it outright.

## Tests and benchmarks

```bash
python3 test_fast.py                 # 29 fidelity cases, C path and fallback
python3 test_dtz.py                  # 18 fidelity cases for the table I/O
python3 bench.py data.csv            # size and speed, both directions
python3 bench_gov.py data.csv        # the older dtz strategy comparison
python3 gui.py --selftest            # compile every AppleScript the app can emit
```

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
