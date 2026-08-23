# Polypress — project brief

*A self-contained description of the project, written to be handed to someone
(or some model) who has never seen the repo and needs to build a website about
it. Every number here comes from measurements in the repo; nothing is
aspirational. Where a claim is limited or was withdrawn, that is said inline —
please keep those qualifiers on the site, they are the point.*

---

## 1. What it is, in one paragraph

**Polypress is a lossless compressor for data tables** — CSV, Parquet, and
anything else shaped like rows and columns. General-purpose compressors (zip,
gzip, xz, zstd) see a table as a stream of bytes. Columnar formats (Parquet,
ORC, Feather) see it as columns but compress each column on its own. Polypress
is built on the observation that **the columns of a real table are not
independent of each other** — `City` is almost determined by `Postal Code`,
`latitude` is repeated inside `location`, adjacent sensor columns hold nearly
the same number — and it exploits those relationships explicitly.

The output is a `.ppz` file. The original table comes back **exactly**: same
columns, same column order, same row order, every cell as the exact string it
was.

## 2. The three ideas it is built on

**1. Predict down a column.** Fit a low-degree polynomial to the last few
values in a column, extrapolate one step, store only the error. Because the fit
is re-centred at every cell, the coefficients never need to be stored — the
decoder already has the neighbours. Order-*k* extrapolation turns out to be
exactly the *k*-th finite difference, so this is one `np.diff` call.

**2. The same thing in two dimensions.** Where adjacent numeric columns are
*commensurable* — same decimal places, same magnitude — a cell is predicted
from its left, upper, and upper-left neighbours. This is what wins on
matrix-shaped tables (yield curves, sensor grids), and it is the one thing no
shipped columnar codec does: Gorilla, DoubleDelta, T64, zfp and fpzip all
predict down a single column. **The detection matters as much as the
predictor** — differencing `Model Year` against `Make` is meaningless, so
groups are formed only where the columns are genuinely comparable.

**3. Reorder the rows so a column collapses into runs.** This is the central
idea. Rather than *model* the fact that `City` depends on `Postal Code`, sort
the rows by the parent column: equal parents become adjacent, the child
collapses into long runs, and the entropy coder eats it. **The permutation is
free**, because the decoder has already rebuilt the parent column and can
recompute the same stable sort. Parents are picked by conditional entropy, then
confirmed by actually compressing both ways, and arranged into a tree so a
parent is always decoded before its children.

Two details that distinguish it from most of the prior work: **the ordering is
per-column, not global** (a 421-column table can carry hundreds of different
row orderings at once), and **the permutation is neither stored nor imposed on
the output** — you get your table back in its original row order.

## 3. The headline result: 500 datasets nobody chose

The obvious objection to any compression result is *"you picked the files"*,
and there is no way to answer that by picking more files. So the main benchmark
does not pick. It asks the Socrata open-data catalog — the index behind several
hundred government data portals — for its datasets **in descending order of
page views**, and takes the first 500 that survive four mechanical filters (the
CSV downloads; ≥2 columns and ≥20 rows; ≥50 KB; not a byte-identical duplicate
of one already taken). Rank order is public and fixed, so the list reproduces.
Every rejection is recorded with its reason.

That gives **500 tables, 3.57 GB of CSV, 14.3 million rows, 10,557 columns**,
measured against **17 competing codecs**:

| | result |
|---|---|
| Round-trips exactly | **500 of 500** |
| Smaller than the best of all 17 competitors | **478 of 500 (96%)** |
| Margin over the best other tool | median **1.25x**, best 2.73x, worst 0.65x |
| Whole corpus, aggregate | 222.8 MB vs 280.2 MB — **1.26x smaller** |
| Compression vs raw CSV | median **14.35x**, best 231.78x, worst 3.49x |
| Peak memory, whole sweep | 1,148 MB |

Head to head against each competitor individually:

| competitor | polypress wins | median margin |
|---|---|---|
| `xz -9e` | **500/500** | 1.30x |
| `zstd -22 ultra` | **500/500** | 1.39x |
| `gzip -9` | **500/500** | 2.24x |
| `lz4 -9` | **500/500** | 2.65x |
| `parquet+zstd` | 449/450 | 1.71x |
| `parquet+snappy` | **450/450** | 2.56x |
| `feather+zstd` | **450/450** | 3.36x |
| `orc+zstd` | 435/436 | 2.00x |
| `bzip2 -9` | 491/500 | 1.46x |
| `brotli -q 11` | 487/500 | 1.32x |

(Counts differ because pyarrow could not read every CSV: 50 files defeated its
Parquet/Feather reader, 64 its ORC writer.)

### Two things that must be said next to those numbers

**Parquet did not reproduce the data on 356 of the 500 tables.** Read with type
inference — the way a data engineer actually reads a CSV — Parquet returned the
exact printed text on only 81 of 500. Turning `"1.50"` into `1.5`, or `007`
into `7`, makes a smaller file for reasons that have nothing to do with
compression. Polypress guarantees the exact printed cell. So on 71% of the
corpus the Parquet rows above are *flattering to Parquet* — and it still loses
all but one of them.

**"You just picked a better final compressor" is answered.** Polypress finishes
with xz, and Parquet cannot use xz at all. So the same modelled streams were
re-finished with the competitor's own entropy coder: `polypress+zstd` beats
`parquet+zstd` 449/450 (median 1.57x), `polypress+brotli` beats
`parquet+brotli` 449/450 (median 1.63x). The margin is the modelling, not the
finisher.

## 4. Where it wins, and where it doesn't — measured, not guessed

**The win is decided by what fraction of the output is free text**, not by how
wide the table is:

| free-text share of output | win |
|---|---|
| 70.9% (`chicago_permits`, 116 columns) | 1.12x |
| 7.5% (`cdc_nndss`) | 3.70x |

- **Densely-coded administrative and survey data** — disease surveillance,
  health surveys, permit and incident logs — is where it is excellent (2–4x
  over the best competitor). These tables are made of *codes*, and the
  reordering trick does nearly all the work.
- **Matrix-shaped numeric tables** — a Treasury yield curve and two sensor
  grids — measure **1.86x, 2.14x, 1.66x**. This is the 2D predictor's own case.
- **Free-text-heavy tables** (addresses, names, descriptions) are the weak
  genre. There, LZ77 substring matching is already good and modelling adds
  little.

### It loses on 22 of 500, and all 22 are listed publicly

Twelve losses are to `brotli -q 11` (not one of the carried fallbacks). Nine
are to `bzip2 -9` run on the *original* file — Polypress's fallback compresses
the table re-rendered canonically, and on those tables normalising the quoting
removed redundancy the Burrows-Wheeler transform had been exploiting; the gap
is 0.1%–1.6%. That leaves **one genuine loss**,
`sars_cov_2_variant_proportions` at 0.65x to `orc+zstd`, and it is undiagnosed.

The honest form of the guarantee is: **never worse than our own plain
fallback**, not "never worse than any tool on your original bytes."

## 5. The engineering principles (these are the interesting part)

The project is unusually strict about evidence, and the site could reasonably
be built around this as much as the ratios.

- **Never worse, measured.** A modelled encoding must beat the plain fallback,
  and a parent column must beat no parent — *measured*, not assumed. Every
  guard works by encoding both ways and keeping the smaller.
- **Verify before writing.** Both command-line tools decode the compressed blob
  and compare every cell against the input *before* any file is created.
- **The decoder treats its input as hostile.** It reads files other people
  made. Corrupt input must be refused, never crash, never allocate unbounded.
  There is a dedicated test suite of deliberately lying headers.
- **The C encoder is byte-identical to the Python one** — not equivalent,
  identical. Verified on 108 real datasets.
- **Negative results get written down.** The README carries a table of
  everything that was tried and failed, and it is treated as the most valuable
  part of the document.
- **Entropy is a good nominator and a bad decider.** Use a cheap statistic to
  shortlist a choice, then actually compress and compare. Fixing one place
  where this rule was broken took 19.7% off the codec's best dataset.
- **Every all-or-nothing test is suspect.** Four blank cells in 72,048 cost
  41.2% of a Treasury yield curve, because a single failing cell disqualified a
  whole column and with it the whole 2D predictor. One `-0.0` in 26,304 cells
  split a 21-column matrix into 18 and 3.
- **A component probe may nominate; only a whole-file encode decides.** Four
  separate transforms measured +12.7%, −24.9%, 0.647x and −1.02% in isolation
  and three of the four made the *whole codec worse*. A column is not worth its
  own bytes — it is worth its own bytes plus its value as a sort parent for
  every other column.

## 6. What it is *not* — the withdrawn claim

An earlier version of the README claimed the reordering trick was novel. **That
claim is withdrawn.** A prior-art search found **US 8,312,026 B2** (Kiem-Phong
Vo, AT&T, filed 2009, granted 2012) which discloses the whole of it — including
the rule that a parent must be chosen by measured compressed size rather than
an entropy score, which this project learned the hard way. The predictors are
also prior art: the planar predictor is Lorenzo (2003, used in fpzip and SZ);
MED is JPEG-LS. Row reordering for compression is a studied problem (Lemire,
Kaser & Gutarra, ACM TODS 2012).

The design was reached here independently, without knowledge of any of it.
Independent convergence twenty years apart is decent evidence the design is
right; it is not evidence of priority. **Both relevant patents have expired**,
so there is no restriction on using the code.

**Do not let a website claim novelty for the core idea.** The honest framing is:
a careful, measured, verified implementation of a good idea, with a table-aware
front end, taken further and tested harder than the prior art was.

## 7. Honest limitations (keep these on the site)

- **It is slow to compress.** Median across 100 unselected datasets: **2.0 MB/s
  encode, 134 MB/s decode.** For comparison on the same machine: `xz -9e` is
  3.9/203 MB/s, `brotli -q 11` is 1.1/501, `zstd -22` is 2.8/871, `zstd -3` is
  187/716. Decompression is fast; compression is in the slow tier and will stay
  there. The "never worse" guarantee is what costs it — tables get encoded
  twice so the smaller result can win.
- **The parent search is O(columns²).** A 209-column table means 38,220 scored
  pairs. The worst case for speed is not a big file — it is a **60 KB** table
  with 106 columns and 79 rows, at 0.5 MB/s.
- **The 2x results are matrix-shaped tables. 1.3–1.5x is the typical result.**
- **Every benchmark dataset is a government open-data table**, because that is
  what the Socrata catalog indexes. Census microdata, NOAA grids, genomics and
  financial tick data are unmeasured, and there is no reason to assume the
  result transfers.
- **Nobody outside this project has run it.**
- The competitor set is now known to have had a hole: all 17 were LZ-family,
  Huffman or block-sort — no context-mixing model. PPMd was added later and
  Polypress still wins 13/13 against it, but on the seven tables where row
  reordering does not fire, **PPMd gets within 1–5%**. Sweeps run before
  2026-08-06 should be quoted as "17 LZ-family and columnar competitors".
- The Mac app is unsigned, so Gatekeeper warns on first open (right-click →
  Open clears it).

## 8. How you actually use it

```bash
pip install polypress                 # the codec and the `polypress` command
pip install 'polypress[parquet]'      # add pyarrow, for .parquet in/out

polypress compress data.csv           # -> data.csv.ppz
polypress restore  data.csv.ppz       # -> data.csv
polypress restore  data.csv.ppz -o out.parquet   # doubles as a converter
polypress info     data.csv.ppz       # plan, shape, how much was reordered
```

numpy is the only hard requirement. There is a C accelerator that compiles
itself on first import and silently falls back to numpy if there is no
compiler, so it is never a dependency.

**For files larger than RAM**, a block-at-a-time mode with a settable memory
budget (`--budget 1.0` for ~1 GB peak). Blocks compress independently, so peak
memory is one block rather than one file; the cost is that reordering only sees
correlations inside a block.

**There is also a standalone C binary** — no Python, no numpy — that produces
byte-identical output to the Python encoder.

**And a Mac app**: launch it and pick a table, double-click a `.ppz` to restore
it, or drop files on the Dock icon. Nothing is written until the compressed
blob has been decoded in memory and compared to the original.

## 9. The live direction (not shipped — describe as research if you use it)

- **Cross-column redundancy is the biggest open win.** One measured example:
  `chicago_permits` stores the same geographic point *five times* —
  `xcoordinate`, `ycoordinate`, `latitude`, `longitude`, and
  `location = "POINT (-87.62 41.89)"` — at different precisions. No general
  compressor can see through that; a table codec can. A ceiling probe on 100
  unselected tables found **40 of 100 have such a relationship, 9 clear 10%, 4
  clear 20%, best 47%**. A first real transform for the geometry case was built
  with a working decoder and measured **19.3% through Polypress against 9.9%
  through xz** on three tables — worth *twice as much* to a table codec,
  precisely because its columns are compressed apart and the duplicate is never
  adjacent to its source.
- **A fast tier.** A row-major single-pass streaming scheme (dictionary-encode
  cells as you sweep, feed every row into one shared xz stream) measures
  **0.97x plain xz at 1.3–5.8x the speed of the main encoder, in O(1) memory**.
  It gives up ~1.4x of size against the full codec — that gap *is* the parent
  sort — but it is chunkable and single-pass.
- **Timestamp columns are currently treated as plain text** and are worth
  −25% on the two tables measured.

---

## Notes for whoever builds the website

- **Lead with the 500-dataset table.** It is the strongest thing here and it is
  the one designed to survive a hostile reading. The methodology sentence —
  *"ranked by page views, first 500 that pass four mechanical filters, every
  rejection logged"* — is part of the claim, not a footnote.
- **The three ideas section is the explainer.** Idea 3 (reorder the rows, don't
  store the permutation) is the one that makes people understand it, and it
  visualises well: show a two-column table with a scattered `City` column, then
  the same table sorted by `Postal Code` with `City` collapsed into blocks.
- **Do not claim novelty for the core idea** (see §6), do not claim it beats
  everything (22 of 500 losses, all published), and do not hide the speed
  (§7). The project's whole character is that the limitations are stated before
  someone else finds them — a site that sands those off is misrepresenting it.
- Suggested tone: an engineering write-up, not a product launch. The audience
  most likely to care is people who already know what Parquet and zstd are.
- Good pull-quotes, all true and all load-bearing:
  - *"500 of 500 round-trip exactly."*
  - *"Parquet did not reproduce the printed data on 356 of the 500 tables."*
  - *"The permutation is free — the decoder recomputes it."*
  - *"Entropy is a good nominator and a bad decider."*
  - *"Negative results get written down."*
