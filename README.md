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

### Is the win the modelling, or just a better final compressor?

A fair objection: Parquet **cannot use xz at all**. Its options are snappy,
gzip, brotli, zstd and lz4 — asking pyarrow for xz returns
`Unsupported compression: xz`. So some of the margin above might be nothing
more than a better finisher.

It is not. Re-finishing Polypress with the *same* codec Parquet is using, and
comparing like for like across 18 tables:

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
python3 benchmarks/bench.py nhanes_real.csv
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
python3 benchmarks/fetch_corpus.py corpus/
python3 benchmarks/bench.py --reps 1 corpus/*.csv
```

| dataset | rows x cols | vs best other | was |
|---|---|---|---|
| CDC notifiable disease | 150,000 x 16 | **3.70x** `parquet+brotli` | 2.97x |
| Seattle fire 911 | 200,000 x 7 | **1.77x** `xz -9e` | 1.77x |
| WA EV population | 200,000 x 16 | **1.75x** `xz -9e` | 1.74x |
| Austin 311 | 150,000 x 19 | **1.61x** `xz -9e` | 1.40x |
| NYC collisions | 150,000 x 29 | **1.44x** `xz -9e` | 1.37x |
| Chicago crimes | 150,000 x 22 | **1.44x** `xz -9e` | 1.12x |
| NYC 311 | 60,000 x 44 | **1.31x** `xz -9e` | 1.14x |
| NYC baby names | 29,685 x 6 | **1.26x** `parquet+brotli` | 1.17x |
| USGS earthquakes 2023 | 16,190 x 22 | **1.25x** `bzip2 -9` | 1.19x |
| USGS earthquakes 21-22 | 16,707 x 22 | 1.21x `bzip2 -9` | 1.20x |
| Chicago permits | 80,000 x 116 | **1.12x** `xz -9e` | 1.03x |
| NOAA climate, SEA | 79 x 106 | **1.11x** `bzip2 -9` | 1.10x |
| NOAA climate, ORD | 69 x 102 | **1.11x** `brotli -q 11` | 1.10x |

Median **1.31x**, worst case **1.11x**, best **3.70x** -- up from 1.19x / 1.03x
/ 2.97x. Every row above is reproduced in `benchmarks/corpus-results.txt`,
which is regenerated from the command in this section rather than edited by
hand; an earlier version of this table was patched per-dataset after a codec
change and drifted from the results file on five of the thirteen rows.
Two changes account for the gain, and both are the same idea: text columns
started taking a reorder parent, and then parent choices stopped being taken on
trust. Dictionary parents were being picked by conditional entropy and used
without checking; making that decision measurable took another fifth off
`cdc_nndss` alone, which was already the best result here.

**13 of 13 wins, but read the spread, not the headline.** Median 1.31x. Only
six datasets clear 1.4x. Two independent confirmations are worth noting: WA
EV population came out at 1.75x against 1.73x in the curated table above,
measured a year apart from a fresh download, and Parquet failed the
exact-text check on 8 of the 13 — so its column is flattered on most rows.

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
python3 benchmarks/fetch_matrix.py corpus/
python3 benchmarks/bench.py --reps 1 corpus/treasury_yields.csv \
    corpus/weather_hourly.csv corpus/weather_wide.csv
```

| dataset | shape | ours | best other | win |
|---|---|---|---|---|
| Treasury yield curve 1990-2025 | 9,006 x 9 | 34,891 | 64,836 `xz -9e` | **1.86x** |
| Weather, 10 sensors hourly x 10y | 87,672 x 11 | 503,460 | 1,078,640 `xz -9e` | **2.14x** |
| Hourly temperature, 24 cities | 26,304 x 25 | 414,471 | 687,390 `bzip2 -9` | **1.66x** |

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

```
polypress/      the codec: fast (single-shot), stream (bounded memory),
                dtz (table I/O), codec, caccel + tcz.c (C accelerator)
csrc/           the standalone C binary -- reads archives with nothing installed
tzip.py         command line entry point
app/            the Mac app: gui.py, build_app.sh, make_icon.py
tests/          fidelity suites
benchmarks/     size and speed against real binaries
docs/           the results PDF and the script that generates it
attic/          superseded work, kept for the record
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

Six real datasets is not a claim, and every one of them was a table this codec
was designed for. So `benchmarks/make_hostile.py` generates ten tables built to
break specific assumptions in it. Four of them did. Here is what happened next:

| hostile case | was | now | what changed |
|---|---|---|---|
| `single_wide_row` | 1.11x LARGER | **1.17x smaller** | The planar predictor was forming "groups" on a 1-row table — pure bookkeeping that reduced nothing. It is now refused below 3 rows. |
| `high_precision` | 1.03x LARGER | **tie, 4 bytes** | bzip2 fallback wins outright; the 4 bytes are the container magic. |
| `base64_blob` | 1.02x LARGER | **0.6% behind** | bzip2 fallback. Only brotli still beats it. |
| `random_text` | 1.01x LARGER | **0.8% behind** | Unchanged — it already beat both xz and bzip2; only brotli is ahead. |

**The guarantee is now: never worse than xz or bzip2 on any table**, because
both are carried as candidates and the smaller wins. It is not "never worse
than anything" — brotli still takes two of these by under 1%, on data that is
incompressible by construction. Carrying a brotli candidate would mean a
non-stdlib dependency and linking libbrotli into the C port, which is a bad
trade for 0.8% on random noise. That is a decision, not an oversight.

Two costs, stated plainly:

- **Unstructured tables now encode 3–5x slower.** When none of the three ideas
  fire, the fallbacks run, and they are a second and third pass over the data.
  `shuffled_cats` went from 12.5 to 2.6 MB/s. Tables where any trick fires —
  which is every real dataset here — are untouched, because the fallback is
  skipped entirely.
- **The fallback is refused if it cannot round-trip.** Canonical CSV is not
  lossless for every conceivable cell, so a candidate is parsed back and
  compared before it is allowed to win. Losing on size beats corrupting data.

The six it wins on are the informative half. `anticorrelated` — a
high-cardinality numeric column that a naive conditional-entropy score would
happily adopt as a parent — comes out **4.24x** ahead, which is the
Miller-Madow correction earning its place. `wide_random` (200 mutually
independent numeric columns) wins by only 1.02x, and that is the honest result:
the O(columns²) parent search does its maximum work for almost no reward.

Full numbers, every contender, in `benchmarks/hostile-results.txt`.

## Honest limitations

- **Pure Python + numpy + a small C library.** Encode is 9–30 MB/s. That
  beats `xz -9e` (5), `brotli -q 11` (1.1) and `zstd -22` (4), but it is
  nowhere near the fast tier — `zstd -3` encodes at 173 MB/s and always will.
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

`docs/Polypress-Results.pdf` is a four-page summary of every measurement here,
including a page stating plainly what is not done. Regenerate it with:

```bash
python3 docs/report.py docs/Polypress-Results.pdf
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
