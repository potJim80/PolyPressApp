# Working on Polypress

A lossless compressor for data tables. Read this before changing the codec —
most of it is hard-won and several items overturned an "obviously correct"
change.

## The one-line version

Three ideas: predict down a column (finite differences), predict from
left+above+diagonal on commensurable numeric columns (planar), and **reorder
rows so a column collapses into runs**. The third is the central idea of the
codec, but it is **prior art**, not novel: US 8,312,026 (Vo, AT&T, 2012, now
expired) discloses the whole of it, down to the measured-parent rule. Arrived
at here independently; claim withdrawn — see the prior-art note in README.md.

**Which idea fires depends on the data, and getting this wrong wastes days:**

- Smooth numeric / matrix-shaped tables (yield curves, sensor grids) → the
  planar predictor. This is the ~2x case, and as of 2026-07-29 it is finally
  backed by data in the repo: `benchmarks/fetch_matrix.py` pulls a Treasury
  yield curve and two sensor grids, giving 1.86x, 2.14x and 1.66x. Before
  that, the claim rested on data no benchmark here touched.
- **Survey and administrative data (NHANES, NEDS, disease surveillance) → the
  planar predictor does NOT apply** and the codec correctly refuses it: those
  columns are *codes*, not quantities. **The reordering does all the work.**
  Skip patterns ("Med_Given = No" blanks the whole medication branch) are what
  make survey data compress.

## Non-negotiable invariants

1. **The C encoder is byte-identical to the Python one.** Not equivalent —
   identical. `tests/test_cbin.py` enforces it, against `fast.encode` — which
   means the *container choice* is part of the guarantee, not just the bytes
   inside a container. Any format change must land in `polypress/fast.py` and
   `csrc/ppz_encode.c` in the *same commit*.
2. **Never worse.** A modelled encoding must beat the plain fallback, and a
   parent must beat no parent, *measured*, not assumed. This binds **both**
   implementations: the C encoder ran without the plain fallbacks until
   2026-07-27 and quietly wrote larger files than Python on any table where no
   trick fired. See "the measured/unmeasured trap" below.
3. **The decoder treats its input as hostile.** It reads files other people
   made. Corrupt input must be refused, never crash, never allocate unbounded.
4. **Verify before writing.** Both CLIs decode the blob and compare every cell
   before a file is created.
5. **Negative results get written down**, in the README table. Several are
   already there and they are the most valuable part of the document.

## Layout

```
polypress/     the codec. fast.py is the whole thing; dtz.py is table I/O;
               stream.py is the bounded-memory block variant; caccel.py+tcz.c
               is an optional ctypes accelerator (NOT the standalone binary)
csrc/          the standalone C binary: no Python, no numpy. ppz_encode.c and
               ppz_decode.c mirror fast.py step for step
tzip.py        shim -> polypress/cli.py (the `polypress` console script)
app/           the Mac app. build_app.sh, build_app.sh dmg
tests/         test_fast, test_dtz, test_stream, test_cbin, test_fuzz,
               test_hostile, test_encoding
benchmarks/    measure_one.py (one table, every competitor) + sweep.py (a
               corpus, one subprocess per table, resumable) + report.py
               (aggregate into the claims). fetch_socrata100.py pulls the
               unbiased 100; fetch_corpus.py + fetch_nhanes.py +
               fetch_matrix.py pull the curated sets; make_hostile.py
               generates adversarial tables
results/       sweep output. The .jsonl and the summary are committed; the
               downloaded CSV is not (see .gitignore)
attic/         superseded work kept for the record
```

**Branch `ondemand`** carries `polypress/ondemand.py` — a separate container
(`PPZO`) giving *column random access*: read one column without decoding the
table, by walking the parent chain (mean 2.78 hops on a 209-column survey).
Re-forked from master on 2026-07-28. It consumes `fast.py` through
`classify` / `pick_parents` / `pack_ints` / `diff_order`, so keep those
**semantics** stable or it breaks silently — and "silently" is literal. On
2026-07-29 `classify` gained the lenient numeric path without changing any
signature, and `ondemand.pack` had nowhere to store the exception cells, so it
wrote the forward-filled values and dropped the originals: a blank came back
as `0.09`, a `-0.0` as `0.19`. No error, a table that looks fine and is wrong.
`pack` now passes `lenient=False` explicitly. **After any change to those four
functions, merge master into `ondemand` and round-trip it** — no test on
either branch catches this. It has **no 2D groups and no text
reorder parents**, and costs ~15% size against the archival codec.

## Running things

```bash
python3 tests/test_fast.py      # 0.4s
python3 tests/test_dtz.py       # 1.5s
python3 tests/test_stream.py    # 2.0s
python3 tests/test_cbin.py      # C must match Python byte for byte
python3 tests/test_fuzz.py      # random adversarial tables, both languages
python3 tests/test_hostile.py   # corrupt stream/ondemand archives, run in a
                                # memory-capped subprocess (invariant 3)
python3 tests/test_lying_header.py  # headers that are well-formed and LIE.
                                # Mutation fuzzing cannot build these, which is
                                # why three unchecked indices and a segfault
                                # survived every earlier pass
python3 tests/test_encoding.py  # BOMs, UTF-16, latin-1: read or refuse
python3 tests/test_input_guard.py   # the C binary must refuse what it cannot
                                # parse. It used to read a .parquet as text,
                                # verify it, and restore garbage
python3 tests/test_cbin_corpus.py corpus100/*.csv   # invariant 1 on real
                                # data, not constructed cases. Slow; needs the
                                # corpus downloaded. Run before releasing
python3 app/gui.py --selftest   # compiles every AppleScript AND runs the
                                # whole menu headless (26 checks). This is
                                # the build gate in app/build_app.sh.
./csrc/build.sh                 # needs lzma.h: brew install xz
./app/build_app.sh dmg          # -> dist/Polypress.dmg
```

**Do not wrap the suites in a `python3 -c` subprocess loop with a long
timeout** — that has hung twice in this repo for reasons unrelated to the
tests. Run them directly.

```bash
python3 benchmarks/fetch_matrix.py corpus/   # yield curve + 2 sensor grids
```

Benchmarks, three steps:

```bash
python3 benchmarks/fetch_socrata100.py corpus100/ --count 100   # ~1.0 GB
python3 benchmarks/sweep.py corpus100/*.csv --out results/socrata100.jsonl
python3 benchmarks/report.py results/socrata100.jsonl --title "Socrata 100"
```

`sweep.py` runs **one subprocess per dataset**, so peak RSS is the largest
single table rather than the accumulated total, and it **skips datasets
already in the output** — a multi-hour sweep has to be safe to interrupt.
`--max-mb` truncates oversize inputs at a row boundary (every codec then gets
the identical file) and `--rss-abort` stops the run if a worker's *measured*
peak crosses the ceiling. Predict to schedule, measure to believe.

`benchmarks/bench.py` was retired to `attic/` — `measure_one.py` is a strict
superset (adds lz4, ORC, Feather, the like-for-like re-finish, and peak RSS).
Two scripts measuring the same thing differently is how the repo ends up
contradicting its own evidence.

**Peak memory, measured 2026-07-29 and the earlier rule corrected.** The old
estimate `(input MB x 8.5 x 2) + 700` **under-predicts by about 18%**: it put
the 73.7 MB `chicago_permits` at 1,952 MB and the real peak was **2,300 MB**.
Use `(input MB x 22) + 700` instead. Run the whole 13-dataset corpus in
phases, smallest first, so a memory problem on the largest file cannot take
the rest of the run with it — `benchmarks/` has no driver for this, it is a
few lines of shell. A full sweep of 26 datasets takes **19 minutes**, not the
~40 recorded earlier.

## The measured/unmeasured trap — read this before optimising

Three separate "obviously good" changes were built, measured, and reverted:

- **RLE over the text blob**: +0.3%. xz already subsumes it — RLE finds only
  adjacent repeats, LZMA finds them at any distance. The gain never comes from
  a better compressor on the same bytes; it comes from a better *order*.
- **Ragged-decimal numeric columns** (recovering `latitude`, which is thrown
  away because its decimal places vary per row): **9–23% worse**.
- **Raising the 50% dictionary threshold**: **4–14% worse**, despite dictionary
  encoding beating raw text on 9 of 10 individual columns.

The last two failed for the *same* reason, and it took a while to see it:

> `pick_text_parents` **measured** its choice and could decline.
> `pick_parents` (dictionary) **did not** — it trusted conditional entropy.
> So any change that moved a column out of `text` swapped a measured decision
> for an unmeasured one and lost. **The loss landed on a different column than
> the one being changed**, which is why per-column size probes never saw it.

Fixing that asymmetry (commit `20dd96e`) took **19.7% off `cdc_nndss`** — the
dataset the codec was already best at. The blind parent choices were not merely
useless, they were harmful.

**The lesson generalises: entropy is a good nominator and a bad decider.** Use
it to shortlist, then compress and compare. `_probe_len` / `_probe_bytes` exist
for exactly this.

## The all-or-nothing gate — the 2026-07-29 lesson

A screen that a column must pass *entirely* will be failed by one bad cell,
and the cost is the whole column.

- **Four blank cells in 72,048 cost 41.2% of the Treasury yield curve.** The
  numeric test was all or nothing, so those four dropped all eight rate
  columns to the dictionary path, and with no numeric columns the planar
  predictor had nothing to group.
- **One `-0.0` in 26,304 cells disqualified a temperature column** and, because
  that column sat in the middle, split a 21-column matrix into 18 and 3.
- Numeric columns now carry **exceptions**: unrepresentable cells are stored by
  position and as text, and their slots are **forward-filled**. Filling rather
  than dropping is load-bearing — equal column lengths are what let the planar
  predictor stack them, worth 18% on top of the 28% for being numeric at all.
- **It is guarded end to end**, because recovering these columns also moves
  them out of the dictionary path — the exact trade that made the reverted
  ragged-decimal work 9-23% worse. Four large corpus datasets have eligible
  columns and the guard refuses all four.

**When reading this codec, treat every all-or-nothing test as suspect and ask
what one anomalous cell costs.**

**The other two were then checked, and both stay** — the lesson did not
generalise, which is itself worth knowing:

- **The 50% dictionary threshold**: +0.09% at 0.6, +1.44% at 0.75, +2.45% at
  0.9. It also **disproves the standing theory** that raising it only failed
  because dictionary parents were unmeasured — `20dd96e` fixed that two
  sessions ago and the threshold still loses. Drop that theory. Per file it
  is mixed (nyc_311 gains 5.0% at 0.6, chicago_permits loses 1.8%), so the
  best threshold is file-dependent — but choosing per file means another
  double encode, and that is the most expensive thing in this codec.
- **The 2D exact-decimal rule**, relaxed to within one: **+0 bytes on every
  table**. On `weather_hourly` the looser rule forms 2 groups where strict
  forms 0, and the guard rejects both.

The numeric gate was special: the discarded column was large, smooth, and the
sole gateway to the planar predictor.

## Traps that have already bitten

- **`set --` in `app/build_app.sh`** clobbers the script's own arguments. The
  action is captured into `ACTION` at the top; keep it that way.
- **Signed overflow checks must come *before* the multiply.** `v >= LIMIT`
  after `v = v*10 + d` never fires — that shipped in `tcz.c` and made the
  accelerator disagree with the numpy fallback about which columns were
  numeric, so the same file compressed differently depending on whether a
  compiler was present.
- **`8 * b` overflows int64** at the 2^62 magnitudes this codec accepts. Python
  never had the bug; its ints are arbitrary precision.
- **A double holds 53 bits.** The metadata carries int64 warm-start values.
  Parse integer JSON tokens with `strtoll`, not `strtod`.
- **numpy's `np.sum` is pairwise, not left-to-right.** The C port reproduces it
  in `pairwise_sum()` because a different last bit flips a `>`, picks a
  different parent, and changes every byte after it.
- **Never iterate a Python `set` where the order affects output.** Tie-breaks
  used to depend on CPython's hash table, so archive bytes did too.
- **`buf_free` zeroes `len`** — capture the length before freeing if you are
  about to compare against it.
- **Finding leaks on this machine has exactly one working recipe.** ASan's
  leak detector does not run on Apple Silicon, and ASan and `leaks` cannot be
  combined — `leaks` refuses to inspect a process using a malloc replacement
  ("target process is using Address Sanitizer"). What works: a plain
  `-O0 -g` build, then `MallocStackLogging=1 leaks --atExit -- ./binary ...`,
  which names the allocating source line. That is how the exception-path leak
  in `ppz_decode` was found; ASan had reported the same run clean.
  Compare a real file against a three-row one — a leak that appears only on
  the real file is data-dependent and therefore yours, not startup noise.
- **A `goto` cleanup label is not the success path.** The decoder's exception
  arrays were freed at `fail_ids:` and nowhere else, so every *successful*
  decode of an archive with a 2D group leaked them. Adding a resource means
  editing both exits.
- **`errors="replace"` made the round-trip check unable to see corruption.**
  The readers opened every file that way, so an undecodable byte became
  U+FFFD *before* the table existed. Verification then compared the decoded
  table against the already-corrupted one and passed — a latin-1 file lost
  every accent and reported success. **A check downstream of the damage cannot
  see the damage.** Decoding is strict now; a BOM is honoured, anything else
  is refused, and `--encoding` is the only override. Do not reintroduce a
  guess: chardet-style sniffing is the same bug with better odds.
- **The same shape again, found 2026-07-30: `csrc/polypress compress` accepted
  any file.** It reads comma-separated text and nothing else, but the CLI
  handed `table_read_csv` whatever it was given. On a `.parquet` it read the
  **binary as text**, found 883 "rows" of 2 "columns", encoded them, **passed
  the round-trip verification**, wrote the archive, and restored a corrupt
  file. On a `.tsv` it found one column per line — nothing in a TSV is a comma
  — so it disagreed with the Python encoder about what the table was, which is
  invariant 1 broken where no test looked. Invariant 4's check compares the
  *parsed* table with the *decoded* one; both agreed, because the damage
  happened before either existed. **Refuse at the door — `input_refusal()` in
  `ppz_main.c`, pinned by `tests/test_input_guard.py`.** The guard is magic
  bytes and file extensions only, deliberately **not** a content sniff: a rule
  that makes C refuse what Python accepts breaks byte-identity in the act of
  defending it.
- **"Skip what is already chosen" is not the same as "never consider".** The C
  front-coding search looped `for (g = 0; g < nsg; g++)` and skipped anything
  already in `bestf`; Python loops `for i in range(ndict, len(groups))`. Those
  look equivalent and are not — **when the all-alphabets candidate loses,
  `bestf` is empty, so C could front-code a single dictionary alphabet and
  Python never can.** Found 2026-07-30 by `tests/test_cbin_corpus.py` on a
  Colombian pharmaceutical register whose 26 string groups were *all*
  alphabets: Python had zero candidates, C picked group 6, and the archives
  differed by 41 bytes (C 0.13% smaller). Both decoded correctly and each read
  the other's output — it was never a data bug — but invariant 1 is
  byte-identity, and "the C port is slightly better here" is precisely the
  silent divergence that guarantee forbids. The loop now starts at `P.norder`.
  **`tests/test_cbin.py` passed throughout**; only real data with that shape
  exposed it.
- **>>> FIXED 2026-07-31, and the lesson is the fix. Bit-identity with numpy
  is NOT achievable, so nothing may depend on the last bit. <<<**
  `tests/test_cbin_corpus.py` found 2 of 122 datasets where C and Python
  disagreed, both a column pair holding the same information twice
  (`condition`/`icd10_codes`; `longitude`/`location` = `"POINT (lon lat)"`), so
  the entropies tied and the two implementations ordered them oppositely.

  Chasing it established something worth keeping: **`pairwise_sum()` faithfully
  reproduces numpy's documented scalar algorithm, and on a 23-bin marginal the
  two agree to the last bit — but on a 13,147-bin joint histogram `np.sum` does
  not match numpy's own documented algorithm**, because it takes a SIMD
  reduction whose grouping depends on the CPU's vector width. No portable C can
  match that, and two numpy builds on different hardware need not match either.

  So the fix is not a better `pairwise_sum`. **Entropy scores are quantised to
  a ~1e-6 grid by `_score()` / `score_of()` and compared as int64** — in
  `pick_parents`, the 0.05 nomination floor, the root choice, and
  `pick_text_parents`. Ties fall to the lower column index, a rule both sides
  can actually keep. **Do not reintroduce a float comparison on an entropy
  score anywhere in the parent search.**
- **Known divergence, recorded not fixed:** a CSV containing a NUL byte is
  *refused* by Python (`_csv.Error: line contains NUL`) and *accepted* by the
  C reader. Not data loss and not invariant 1 — which is about two encoders
  given the same table — but the two CLIs disagree about whether that file is
  readable. Changing either reader's mind about NUL is a format decision.
- **Tkinter looks available on macOS and is not.** Apple's Tk 8.5.9 imports,
  constructs every `ttk` widget, and `destroy()`s cleanly — so a probe that
  builds widgets on a withdrawn window *passes*. It is **mapping** the window
  that wedges: one `update()` on a shown window never returns, with no error
  and no window. Re-verified 2026-07-28. This is why `app/gui.py` drives
  osascript instead; do not "improve" it to Tk on the strength of a widget
  probe.
- **Python's `csv.writer` is not the obvious CSV writer**, and the plain
  fallback compresses exactly its output, so `table_write_canonical` in
  `ppz_util.c` has to match it byte for byte. Three rules, all found by
  *testing* the Python writer rather than reading it:
  a bare `\r` is **not** quoted (it quotes on characters in the
  *lineterminator*, which here is `"\n"` alone); an empty field **is** quoted
  when it is the only field in its row; and a NUL byte forces quoting. Note
  the first makes Python's own CSV lossy for such a cell — which is why the
  fallback is round-trip checked before it may win, and why both
  implementations then refuse it and agree. `table_write_csv` (what `restore`
  writes) deliberately does **not** follow these rules; do not merge the two.
  `tests/test_cbin.py::check_canonical` pins all of it.

## Where the wins actually are

Measured: **the win is decided by what fraction of the output is the text
blob**, which gets the least modelling. Not by width — `chicago_permits` has
116 columns and was the *worst* result, because 8 free-text columns held most of
the bytes.

| text blob share | win |
|---|---|
| 70.9% (chicago_permits) | 1.12x |
| 7.5% (cdc_nndss) | 3.70x |

Remaining backlog, in value order — see the memory directory for detail:

1. **Numeric extraction from text.** `"1234 N HALSTED ST"` → skeleton
   `"# N HALSTED ST"` (low cardinality → dict → gets a parent) + number 1234
   (numeric → delta-coded). Unblocked now that dict parents are measured.
2. **Cross-column redundancy — MEASURED 2026-08-01, and it is alive.**
   `chicago_permits` stores the same point *five times* — `xcoordinate`,
   `ycoordinate`, `latitude`, `longitude`, and
   `location = "POINT (-87.62 41.89)"` — at different precisions. No general
   compressor can see through it; a table codec can.

   `benchmarks/probe_cross_column.py` measures the **ceiling** without building
   the predictor: detect derivable columns, then encode the table twice, whole
   and with the redundant *part* stripped. On the unselected 100:
   **40 of 100 tables have one, 9 clear 10%, 4 clear 20%, best 47.35%, median
   of those 40 is 3.42%.** Record in `results/cross-column-summary.txt`.

   Two narrow patterns do nearly all of it: **geometry republished as text**
   (`location`/`point`/WKT/GeoJSON from lat+lon, 15–21%) and **concatenated
   keys** (`row_id` = four columns pasted together, 47%; `full_name` = first +
   last, 19%). Build those two, not a general scheme.

   **Two traps, both already paid for.** A *substring* detector finds almost
   nothing — real duplication is at a different **precision**
   (`-87.67584459801843` vs `POINT (-87.675844598018 ...)`), and switching to
   numeric-token matching took `chicago_permits` from 0.49% to 5.47%. And
   **6 of the 40 get *worse*** when the redundancy is removed, worst −1.98%:
   the relation is real, coding it costs more than it saves. Invariant 2
   applies — measure, do not assume.
3. ~~**The C encoder never tries the plain fallbacks.**~~ **DONE** — it now
   builds the same canonical CSV, round-trip checks it, and picks the smallest
   of xz / bzip2 / modelled, exactly as `fast.encode` does. Worth 52.6% across
   the fidelity case set and up to 13x on a very small table; on real tables
   only 2 of 15 changed at all, because a trick usually fires. The trap here is
   that the canonical CSV must match Python's `csv.writer` byte for byte —
   see "traps that have already bitten".
4. **Encode is O(columns²)** in the parent search. 1.9 MB/s on 421 columns.

## Honest status

**The headline is now 500 unselected datasets, measured 2026-08-01.** Taken
from the Socrata catalog in page-view order, not chosen: **478 of 500 beaten
against the best of 17 competitors** (the earlier README said 20 — it was
miscounting our own three rows as rivals), median margin **1.25x**, aggregate
**1.26x**, **500 of 500 round-trip exact**, peak RSS **1,148 MB**. Against
Parquet it is **449/450 at three codecs and 450/450 at snappy**, and still
449/450 when re-finished with Parquet's own codec. Full record in
`results/socrata500-summary.txt`, per-codec rows in
`results/socrata500-results.csv`. Rebuild with `./benchmarks/run_sweep_500.sh`.

**Parquet did not reproduce the exact printed text on 356 of the 500.** Always
quote that next to a Parquet size comparison.

**The 22 losses decompose, and only one is real.** Twelve are to `brotli -q 11`,
which is not a carried fallback. Nine are to `bzip2 -9` **run on the original
file**: our fallback compresses the table re-rendered through `csv.writer`, and
on those tables normalising the quoting removed redundancy the BWT was
exploiting — the canonical CSV is 79 KB *smaller as text* and still compresses
117 bytes *worse*. The guarantee is "never worse than our own plain fallback",
not "never worse than any tool on your original bytes"; say so. That leaves
**one** genuine loss, `sars_cov_2_variant_proportions` at **0.65x** to
`orc+zstd`, the worst result in the corpus and **undiagnosed**.

**At 500 datasets the sweep must be run by `benchmarks/run_sweep_500.sh`**, not
by hand: it pins every numeric library to one thread, runs `nice`, caps inputs
at 16 MB and re-passes to pick up datasets that arrive while it is running. The
16 MB cap is lower than the 100-corpus's 28 MB, so **absolute byte totals from
the two sweeps are not comparable** — ratios are.

**Two things the bigger corpus exposed that 18 datasets could not:**

1. ~~**The "never worse" guarantee does not hold.**~~ **FIXED 2026-07-31.** The
   plain fallbacks were only generated when no trick fired, so a table where
   one fired could lose to a fallback never run — 3 of 100, worst **28.1%**.
   They are now always considered; the Python compressors are capped at the
   size they must beat and abandon a hopeless candidate part-way, which cannot
   change the winner. A preset-1 probe as nominator was measured and rejected:
   sound only above a 3.0x threshold (worst observed ratio 2.825), which fires
   on 19 of 22 datasets and saves almost nothing. See
   `benchmarks/probe_fallback_gate.py`.
2. **The C binary compressed a `.parquet` as text** and its own verification
   passed, because the check is downstream of the misparse. Fixed; see the
   traps section.

Excellent on densely-coded administrative data, marginal on numeric and
text-heavy data. Every dataset is a government open-data table, so breadth of
*genre* is still unmeasured. Nobody outside this project has run it. The `.dmg`
now carries a valid ad-hoc signature — Gatekeeper still refuses it on first
open, but as an ordinary unsigned app, which right-click → Open clears, rather
than as "damaged".

Also measured 2026-07-29:

- **Three matrix-shaped datasets** via `fetch_matrix.py`: **1.86x**, **2.14x**,
  **1.66x**. These are the shape the planar predictor exists for and the first
  time it has been benchmarked on its own case.
- **Like for like against Parquet** — Polypress re-finished with Parquet's own
  codec wins **18/18 at zstd-22 and 17/18 at brotli-11**, margins to 5.6x.
  Parquet cannot use xz at all, so this forecloses the "you just picked a
  better finisher" objection. Worth leading with.
- **Encode is 1.86x slower** than before this session, for 1.66% smaller
  output. Four large datasets pay ~2.2x for zero gain. See the limitations
  section of the README; this is the open cost.

**Regenerate the results file whenever the codec changes; never hand-patch the
README table from a commit message.** Doing that after the parent-guard change
left five of thirteen rows wrong and the committed results file a whole commit
behind what the README claimed.
