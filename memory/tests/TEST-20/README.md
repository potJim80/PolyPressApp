# TEST-20 — the specialised model families, measured on our own data

Run 2026-08-06, the research half of the day that TEST-18 opened. TEST-18 asked
*which column* carries the gap to kanzi. This asks *what model would close it*,
and takes the answer from measurement rather than from the papers, because the
papers do not measure the thing we need.

Full numbers in `results.txt`. Scripts: `cmsplit.py` (transform vs mixer),
`entropy.py` (order-0 floors), `ragged.py` (classify plan + decimal splits),
`alp_exact.py` (ALP made text-exact), `tstamp.py` (timestamp forms),
`roworder.py` (Hilbert/Morton), `ablate.py` (leave-one-out, overlaps TEST-18 —
it was written before TEST-18 landed; keep TEST-18's as the record).

## The four findings

### 1. kanzi's advantage is TPAQX alone. There is nothing in its front end.

`-t None -e TPAQX` equals `-l9` within 0.05% on both whole tables and on 11 of
12 columns. The EXE+RLT+TEXT+UTF+DNA stack is worth **−0.5% on usgs_quakes**.
LAW 3b for the fourth time, from the other side: transform and model do not
stack, and here the model is the whole thing.

Note the control: kanzi's own `CM` — a context model *without* the mixture and
the match model — is **2.2–4.3× worse than xz**. "A context model" is not the
answer. The mixture of many models is.

### 2. The gap is an ENTROPY-CODING gap, not a modelling gap.

TPAQX sits at **1.00–1.09× the order-0 entropy of the column's values** on
every medium-cardinality column. We sit at **1.07–1.54×**. Measured directly on
the id stream, `pack_ints` + `xz -9e` is **1.01–1.40× H0, median 1.15**.

LAW 2 said an order-0 rANS lands at exactly 1.00× the floor and LAW 2b said no
ANS beats xz *on a column as text*. Nobody had asked the third question: what
does xz do on the **id stream**, where there is no text to match. It sits 15%
above a floor we already know how to reach.

**But it is not universal, and the counter-example is the point.** Aggregated
over all dict columns: nyc_collisions **+5.3%**, usgs_quakes_deep +3.0%,
chicago_permits +2.4%, usgs_quakes +0.7%, **austin_incidents −7.8%** — there
the id streams are run-structured and xz eats the runs far *below* H0. Any
build must choose per column and measure, exactly as invariant 2 requires.

### 3. The ragged-decimal problem dissolves, and the codec still loses.

On this corpus **the printed cell is the shortest round-trip decimal of its
double** — 16,190 of 16,190 on every clean float column in usgs_quakes, zero
cells needing trailing zeros. So the decimal count is a *function* of the
double: **exact-double implies exact-text**, and the format-descriptor stream
that made the earlier ragged-decimal attempt 9–23% worse does not need to
exist. That is worth knowing whatever gets built.

It is still not enough. Charged honestly — per-1024-vector exponent, decode,
print with the shortest printer, require the original string, exceptions
positioned — **ALP is 2.3% worse than Polypress on usgs_quakes and 24% worse on
nyc_collisions**, and loses to the bare context mixer on all 12 columns.

The two mechanisms inside ALP that *are* worth having are separable from it:
- **Per-vector exponent, not per column.** Column-wide is 37% *worse* than
  xz-on-text; per-1024 is 8–24% *better*. That single scope choice is the
  difference, and it explains the reverted result.
- **The patch list** — positioned exceptions — which we already built.

**Gorilla / Chimp / Chimp128 / Elf / Patas are dead**, not marginal: XOR with
the previous value is **worse than not compressing at all** on 6 of 8 columns
(133,564 vs 129,520 raw doubles on `latitude`). Consecutive table rows are
unrelated observations.

**pcodec** is the one float-domain codec that is competitive — 0.968 against us
on usgs_quakes's 12 numeric columns, winning 8 of 12 — and it has one
catastrophic mode: **2.3–2.5× worse than us on nyc_collisions coordinates**,
because 5/6/7 ragged decimals defeat its scale detection and leave ~52
near-random mantissa bits. Which gives the law-shaped statement:

> **For high-precision ragged decimals the printed text is a strictly better
> representation than the double.** Any scheme that parses to `double` first
> has already discarded the redundancy that was paying. This is why zfp/fpzip
> lossless are not worth investigating either.

### 4. Spatial row ordering does not pay, and Hilbert is not even the best key.

Hilbert(lat,lon) costs **+1.8%** and Morton **+1.7%** once the permutation is
charged. On the coordinate pair alone Hilbert gains 29% — and **plain
sort-by-latitude gains more** (70,698 vs 74,569). LAW 1's reason: the
permutation is paid once whatever the key, so the right key is the one that
collapses the most bytes, not the one with the best 2D locality. Coding the
pair *as a pair* (interleaved on one line) is worse than two independent
columns in all four row orders.

## Also established, for the numeric gate

`fast.classify` refuses **every float column** in usgs_quakes — `latitude`,
`longitude`, `depth` to `text`; `dmin`, `rms`, `horizontalError`, `depthError`,
`magError` to `dict` with alphabets up to 6,853 over 16,190 rows. Only the
three integer columns are numeric and they are not adjacent, so
`find_2d_groups` returns nothing and **the planar predictor cannot fire on this
table at all**.

Cause is `EX_MAX_FRACTION = 0.05` against exception rates of 9.9–11.8%. These
are not four bad cells; they are a fat tail, refused by exactly the
all-or-nothing shape CLAUDE.md already warns about — applied this time to the
screen that was built to fix all-or-nothing screens.

## Caveats

- Column measurements are **standalone**, one column as a one-column table. In
  the full encode these columns get reorder parents, so the standalone numbers
  bound the *entropy-coder* question, not the whole-codec question.
- No decoder was written for the ALP, pcodec or transpose forms — sizes are
  from encoders whose inverse was verified per cell (shortest-repr equality),
  not from a round-tripped archive. **Nothing here is a LAW until something is
  built with a decoder.**
- 40,000-row cap, 2 of 13 tables for the ablation, one corpus, all government
  open data.
- pcodec was measured through its Python API at level 12; its container
  overhead is included, ours is not.
- The float-codec and geospatial literature passes did not complete and the
  session's web-search budget ran out. The Chimp/Elf/Patas verdict above is
  from the mechanism plus our Gorilla measurement, not from their papers.
