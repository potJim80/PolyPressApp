# 2026-08-07 — BUILD DAY

**New rule as of 2026-08-06: research days and build days alternate.**
Today is a build day. **Do not open a research agent. Do not run a web search.**
Everything below is already measured; the numbers are in
`memory/tests/TEST-20/results.txt` and `memory/tests/TEST-18/`.

If a question comes up that needs research, write it at the bottom of this file
under "Questions for the next research day" and keep building.

---

## The five rules that bind every task here

1. **Byte-identity.** Any format change lands in `polypress/fast.py` AND
   `csrc/ppz_encode.c` in the **same commit**. This roughly doubles every task
   below. It is why tasks are staged as *probe with a decoder* → *land in both*.
2. **Never worse, measured.** Every new encoding is a candidate the encoder
   *tries* and keeps only if it wins. Entropy nominates; compression decides.
3. **The decoder treats its input as hostile.** New stream types need new
   bounds checks and a `tests/test_lying_header.py` case.
4. **Verify before writing.** Both CLIs already do this; don't regress it.
5. **Negative results get written down** in the README table, not in a commit
   message.

## Where today's wins come from (recap, one line each)

- The gap to `kanzi -l9` is an **entropy-coding gap on the id stream**
  (1.15× the order-0 floor, median), not a modelling gap. TEST-20 §B.
- **Timestamp columns are currently `text`** and the codec does nothing with
  them. `usgs time` −24.9%, `austin rep_date_time` −25.1%. TEST-20 §E.
- **Byte-transposing fixed-width text** is a permutation, needs no parsing,
  and is worth −15.3% on `time`. TEST-20 §E.
- `place` carries **59% of usgs_quakes's gap** and is a *text* column of shape
  `<number> km <BEARING> of <PLACE>, <REGION>`. TEST-18.
- **`EX_MAX_FRACTION = 0.05` refuses every float column** in usgs_quakes, so
  the planar predictor cannot fire there at all. TEST-20 §C.

---

# TASK 1 — Order-0 coder for the id stream, gated per column
**Highest EV. 0–5% per table. Expect most of the day.**

`pack_ints` + `xz -9e` sits at 1.01–1.40× the order-0 entropy of the ids
(median 1.15). LAW 2's rANS/tANS engines land at 1.0002–1.0013× of that floor
and already exist.

**1.1** Lift the order-0 static rANS encoder + decoder out of
`memory/tests/TEST-2/` into a probe module under `memory/tests/TEST-21/`.
Do **not** put it in `polypress/` yet. Re-validate it against the entropy floor
on 20 columns before trusting it — TEST-2's own caveat is that the coders were
written from memory.

**1.2** Write `run_idcoder.py`: for every `dict` column in the plan of all 13
corpus tables, encode the id stream three ways —
`pack_ints+xz` (today), `rANS order-0`, `rANS order-1 adaptive` — **decode each
back and assert equality**. Charge the frequency table in full. Report per
column and per table.

**1.3 Acceptance gate.** The build lands only if the *per-column minimum*
beats `pack_ints+xz` by ≥1.5% aggregated over the 13 tables **and**
austin_incidents does not regress. Remember austin is **−7.8%** on this axis:
its `occ_date`/`rep_date` id streams are run-structured and xz beats H0 there.
A blind swap loses.

**1.4** If it passes: add a stream-type byte to the container, implement the
chooser in `fast.py` as a `_probe_len` comparison, port to `ppz_encode.c` +
`ppz_decode.c`, same commit. If it fails: write the negative result into the
README table and stop — that is a complete day's work either way.

**Trap to expect:** the chooser must not iterate a Python `set`, and any
tie must fall to a rule both languages can keep (lower index), per the 2026-07-31
entry in CLAUDE.md.

---

# TASK 2 — Timestamp columns
**−24.9% on one column of usgs_quakes = 3.4% of that archive.**

**2.1** Add `_datetime(cells)` beside `_numeric` / `_numeric_lenient`. It must
**re-render and require the original string**, exactly as `_exact_int` does.
Recognise, initially, only these three, all seen in the corpus:
`YYYY-MM-DDTHH:MM:SS.mmmZ`, `YYYY-MM-DDTHH:MM:SS.mmm`, `MM/DD/YYYY  HH:MM`
(note the **two spaces** in the third — it is literal in austin_incidents).
Anything that does not re-render is a positioned exception, same mechanism as
the numeric path.

**2.2** Produce int64 (epoch ms, or minutes where that is the resolution), then
reuse the existing `diff_order` / `pack_ints` machinery. **Byte-aligned frame
of reference, not sub-byte bit packing** — see TASK 6's note.

**2.3 Gate it per column, measured.** This is not optional:
- `usgs time` −24.9%, `austin rep_date_time` −25.1%, `occ_date_time` −11.7%
- `usgs updated` +8.5% **worse** (non-monotone)
- `austin occ_date` +12% to +58% **worse** (652 distinct, runny, LZ eats it)

**2.4 Do NOT implement delta-of-delta.** Measured worse than plain delta on all
four columns that parse (TEST-20 §E). Gorilla's buckets were fitted to
Facebook's production sample.

**2.5** Round-trip test with a hostile case: a datetime column containing a
leap-second-looking `:60`, a year 0001, a year 9999, and an empty cell.

---

# TASK 3 — Byte-transpose for fixed-width text columns
**Cheapest thing on the list. ~15 lines each side.**

For a `text` column whose cells all have the same length, emit
`all char[0], all char[1], …` instead of the cells in order. It is a
permutation, so it is exactly invertible, needs no parsing, and has no
exception path at all.

Measured: `usgs time` 64,947 → **54,995 (−15.3%)**, `austin rep_date_time`
63,294 → **56,823 (−10.2%)**; but `usgs updated` **+39.7%** and `austin
occ_date` **+6.8%**. So: try it, compress both, keep the smaller. One flag bit.

Do this **after** TASK 2, and measure them **composed** — TASK 2 may take the
same columns, and the two must not both be charged for the same win.

---

# TASK 4 — Per-vector exponent for the numeric gate
**Structural: unblocks the planar predictor on a whole genre.**

`EX_MAX_FRACTION = 0.05` refuses every float column in usgs_quakes at 9.9–11.8%
exceptions, so the table has 3 numeric columns, none adjacent, and
`find_2d_groups` returns nothing.

**4.1** Change the decimal-count decision in `_numeric_lenient` from
**one count per column** to **one count per 1024-row vector**, with the
exception list still global. ALP's measurement and ours agree on why: per
column is 37% *worse* than xz-on-text, per 1024 is 8–24% *better*.

**4.2** Exploit the free descriptor. On this corpus the printed cell is the
shortest round-trip decimal of its double on 16,190/16,190 cells, so a value
that round-trips through `(significand, e)` reproduces its text with **no
descriptor stored**. Verify that property per cell at encode time; anything
that fails is an exception. This is what the 2026-07-30 attempt was missing.

**4.3 The trap is the same one that made ragged decimals 9–23% worse**, and it
is not in this column: recovering these columns moves them **out of the `dict`
path**, where they currently get measured reorder parents. The whole-file guard
must run. `_lenient_promising` will need its estimate re-derived for the
per-vector case — a one-sided screen that over-estimates the alternative, as
documented.

**4.4** Report explicitly whether `find_2d_groups` now forms a group on
usgs_quakes / usgs_quakes_deep / nyc_collisions, and what the planar predictor
is worth there. That is the actual prize; the per-column bytes are secondary.

---

# TASK 5 — `place`-style skeleton + number split (backlog item 1)
**Largest single gap carrier measured: 59% of usgs_quakes's 33,588 B.**
**Stretch — start only if 1–4 are done or blocked.**

`"108 km SE of Kuril'sk, Russia"` → skeleton `"# km SE of Kuril'sk, Russia"`
(10,257 distinct → far fewer → `dict` → **gets a reorder parent**) + `108`
(numeric → delta). CLAUDE.md has had this as backlog item 1 since before there
was a number on it.

**5.1** Tokenise on runs of digits only. One skeleton stream, one integer
stream per digit-run slot (cap at, say, 4 slots; overflow → the column is
refused).
**5.2** The split is only legal if `rebuild(skeleton, numbers) == original` for
every cell. No exceptions mechanism in v1 — refuse the column instead.
**5.3** Measure against the whole-table archive, not the column. The win is
supposed to come from the skeleton reaching the `dict` path and acquiring a
parent, which a per-column probe cannot see (this is the exact failure mode
CLAUDE.md describes: "the loss landed on a different column").
**5.4** Second target once it works: `chicago_permits` addresses
(`"1234 N HALSTED ST"`), which is where the idea came from.

---

# TASK 6 — Two free experiments, run them while something else compiles

**6.1 `pb=0` on the LZMA filter** for numeric/packed streams. liblzma's
position-bits default is 2 (4-byte alignment); our packed streams are 1-, 2-,
3-byte aligned. `lzma.FORMAT_RAW` takes it in the filter dict. **This is a
parameter, not a format change** — but it *does* change output bytes, so it is
still a same-commit change to `ppz_encode.c`. Sweep `pb ∈ {0,1,2}` and
`lc/lp` with `lc+lp ≤ 4` on the 13 tables.

**6.2 Do NOT bit-pack below byte alignment anywhere.** The one controlled
measurement of our exact pipeline (Petri & Moffat, SPE 2018) has Simple-16 as
the best standalone code and the **worst input to xz — worse than raw
uint32**, because variable-width packing shifts identical values to arbitrary
bit phases. DuckDB #18984 is the same effect in production at 2.86×. If any
task above tempts you into sub-byte packing, that is the reason not to.

---

# TASK 7 — Housekeeping, 20 minutes, do it before committing anything

- `memory/tests/TEST-20/ablate.py` duplicates `TEST-18/run_ablate.py`. Delete
  the copy in TEST-20; TEST-18 is the record.
- Add a line to `memory/LAWS.md` under LAW 3 pointing at TEST-20 §B — **but do
  not write it as a law**. Nothing in TEST-20 was round-tripped through a
  decoder, and the rule is that a law needs one. If TASK 1 lands, TEST-21
  earns the law.
- `pcodec` was installed into a scratchpad `pylibs/` that will not survive the
  session. If TASK 1's numbers make it worth re-testing, `pip install --target`
  again; it is a single Rust wheel, arm64, no dependencies.

---

## Do NOT build any of these (measured dead, 2026-08-06)

| | why | where |
|---|---|---|
| Gorilla / Chimp / Chimp128 / Elf / Patas | XOR-with-previous is worse than **not compressing** on 6 of 8 columns | TEST-20 §D |
| Delta-of-delta timestamps | worse than plain delta on all 4 columns that parse | TEST-20 §E |
| ALP as its own encoding path | text-exact: 2.3% worse than us on usgs, 24% worse on nyc_collisions, wins 2 of 12 | TEST-20 §D |
| zfp / fpzip lossless | same double-domain failure as pcodec, without the binning | inferred from §D |
| Hilbert / Morton row order | +1.8% / +1.7% net of the permutation; plain sort-by-lat beats Hilbert on the pair itself | TEST-20 §F |
| Pair-aware (lat,lon) coding | interleaving is worse than two columns in all 4 row orders | TEST-20 §F |
| Simple8b / PFOR / FastPFOR / StreamVByte / DELTA_BINARY_PACKED | speed codecs; sub-byte packing is measured *harmful* in front of xz | TEST-20 §G |
| Anything from kanzi's front end | the whole transform stack is worth −0.5% on usgs_quakes | TEST-20 §A |

---

## Questions for the next research day (write here, do not chase today)

- Is `nyc_collisions` `sars_cov_2_variant_proportions`-style, i.e. does the
  0.65x loss to `orc+zstd` share the ragged-decimal-coordinate cause found in
  TEST-20 §D? Still undiagnosed since the 500-sweep.
- Does the order-0 slack (TEST-20 §B) survive on the columns **after** they get
  a reorder parent? Everything measured was standalone.
- Bit-plane transposition (bitshuffle) as opposed to byte-transposition — the
  literature says it is the one bit-level rearrangement that helps a downstream
  LZ. Not measured here.
- The float-codec and geospatial literature passes never completed
  (web-search budget exhausted 2026-08-06). Chimp/Elf/Patas were ruled out on
  mechanism + our Gorilla measurement, not from their papers.
