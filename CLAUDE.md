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
benchmarks/    bench.py is the one to use; fetch_corpus.py + fetch_nhanes.py +
               fetch_matrix.py download real data; make_hostile.py generates
               adversarial tables
attic/         superseded work kept for the record
```

**Branch `ondemand`** carries `polypress/ondemand.py` — a separate container
(`PPZO`) giving *column random access*: read one column without decoding the
table, by walking the parent chain (mean 2.78 hops on a 209-column survey).
Re-forked from master on 2026-07-28. It consumes `fast.py` through
`classify` / `pick_parents` / `pack_ints` / `diff_order`, so keep those
signatures stable or it breaks silently. It has **no 2D groups and no text
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
python3 tests/test_encoding.py  # BOMs, UTF-16, latin-1: read or refuse
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

Benchmarks: `python3 benchmarks/bench.py --reps 1 corpus/*.csv`. Use
`--reps 1` for a corpus run; 3 passes triples a run already dominated by
`brotli -q 11` at ~1 MB/s. Memory ceiling is 80 MB of CSV per file because
`fast.py` expands CSV ~8.5x into Python strings and benchmarking holds an
encoded and a decoded copy at once.

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
what one anomalous cell costs.** The same shape may still be lurking in the
50% dictionary threshold and in the commensurability screen.

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
2. **Cross-column redundancy.** `chicago_permits` stores the same point *five
   times* — `xcoordinate`, `ycoordinate`, `latitude`, `longitude`, and
   `location = "POINT (-87.62 41.89)"` — at different precisions. No general
   compressor can see through it; a table codec can. Most speculative, biggest
   ceiling.
3. ~~**The C encoder never tries the plain fallbacks.**~~ **DONE** — it now
   builds the same canonical CSV, round-trip checks it, and picks the smallest
   of xz / bzip2 / modelled, exactly as `fast.encode` does. Worth 52.6% across
   the fidelity case set and up to 13x on a very small table; on real tables
   only 2 of 15 changed at all, because a trick usually fires. The trap here is
   that the canonical CSV must match Python's `csv.writer` byte for byte —
   see "traps that have already bitten".
4. **Encode is O(columns²)** in the parent search. 1.9 MB/s on 421 columns.

## Honest status

13 of 13 real datasets beaten. Median **1.31x**, worst **1.11x**, best
**3.70x**, measured 2026-07-27 and reproduced in
`benchmarks/corpus-results.txt`. Excellent on densely-coded administrative
data, marginal on numeric and text-heavy data. Nobody outside this project has
run it yet, and the `.dmg` is unsigned — Gatekeeper will call it damaged until
someone pays for a certificate.

Added 2026-07-29 and **not** yet folded into that corpus record:

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
