# Laws

Findings that govern what gets built. Each law is produced by a numbered test
in `memory/tests/`. A law stays here until a test overturns it — and if one
does, the overturning test gets a number too and both stay on the record.

Rule: **a law must come from a measurement with a decoder.** No law from
reasoning alone, no law from a per-column size probe that was never
round-tripped.

---

## LAW 1 — sorting is conservation, not compression

*From TEST-1, 2026-08-05. 76 columns, all round-trip verified.*

**On a single column, sorting can never win. This is arithmetic, not tuning.**

- `(sorted values, permutation)` is a bijection with the original column.
  Sorting does not delete information, it moves it into the permutation.
- The permutation's floor is `Σ cᵢ·log₂(n/cᵢ)` bits — **exactly `n·H(p)`**,
  which is what order-0 coding of the *unsorted* column already costs.
- The dictionary and the run lengths are then additive on top.
- Measured: the sorted form beat the unsorted form on **0 of 76 columns**.

### LAW 1b — the win belongs to number-encoding, not to sorting

- Dictionary + codes in original order beats `xz -9e` on **57 of 66** real
  columns, **0.86x** aggregate.
- Scheme B pushed to its own optimum *degenerates into* plain dictionary
  coding. The sort drops out of the answer.

### Consequences

- **Never sort one column on its own to compress it.**
- Sorting pays only when **one row order is shared across many columns**, so
  the permutation is paid once and divided by the column count. That is what
  Polypress already does, and it is the only reason the idea works at all.
- Consistent with `OUT/results/sortreg-summary.txt`, the multi-column version
  of the same probe (2.53x → 1.03x xz, never catching the codec).

### Known exceptions — where plain xz still beats number-encoding (10 of 76)

- **High-cardinality free text** (addresses, personal names). LZ77 exploits
  shared *substrings*; a dictionary treats each distinct string as an opaque
  symbol and throws that structure away. This is backlog item 1 in CLAUDE.md.
- **Columns already grouped by value.** xz's match finder eats the runs;
  no order-0 model can see them.

### Caveat on the margin

- The number-encoded variant sits at **1.20x** the order-0 floor and xz-on-text
  at **1.56x** (medians, real columns). Both are above the floor — the variant
  bit-packs and xz's the result rather than running an adaptive coder. So
  0.86x is a floor on its advantage, not a ceiling.

---

## LAW 2 — the coder is not the bottleneck; the model is

*From TEST-2, 2026-08-05. 200 real single columns, 8 coders, all round-trip
verified. Follows LAW 1, which left open whether a real entropy coder would
close the 1.20x gap to the order-0 floor. It does not.*

**Which entropy coder you pick is worth 0.01%. Which model you feed it is
worth 47%.**

| axis | ratio | wins |
|---|---|---|
| tANS vs rANS (table vs range form) | 1.0001 | 34/200 |
| uABS vs rABS (uniform vs range, binary) | 1.0001 | 0/200 |
| rANS x4 vs rANS (interleaving) | 1.0002 | 0/200 |
| adaptive bit-tree vs static byte | 0.899 | 91/200 |
| **order 1 vs order 0** | **0.571** | **192/200** |

All four ANS types are different machines for spending the same number of
bits. Interleaving is `+12 bytes` flat — a speed arrangement, not a type.

### LAW 2b — no ANS type beats xz on a column, at any order

| coder | vs `xz -9e` | wins | size / order-0 floor |
|---|---|---|---|
| rABS order-1 adaptive | 2.18x | 7/200 | 0.57 |
| rANS / tANS order-1 static | 2.43x | 20/200 | 0.61 |
| rANS / tANS order-0 static | 4.25x | 4/200 | **1.00** |
| `xz -9e` | 1.00x | — | **0.14** |

Order-0 rANS lands at exactly **1.00x the order-0 entropy** — it is doing its
job perfectly. That is the point: xz sits at **0.14x**, seven times *below*
the order-0 floor, because LZ77 is not an order-0 model. `"CHICAGO"` repeated
40,000 times is one match to xz and 40,000 independent draws to any order-0
coder.

### Consequences

- **Stop looking for a better entropy coder.** Same conclusion as
  `OUT/results/sortreg-summary.txt` finding 6, reached from the opposite
  direction and on 200 columns instead of 9.
- **A Markov model is the lever, and most of it is already answered.** Order-1
  over bytes is a first-order Markov chain — it is the 47% above.
  Cross-column context modelling was built (`attic/smart.py`) and beaten by
  reordering, 2,328 B vs 6,901 B. `pick_parents` already scores conditional
  entropy, then acts on it by sorting, because a permutation is free and a
  transition table is not.
- **Where ANS does win is where LZ has nothing to match** (20/200): 11 are
  `blank-heavy`, 4 are fixed-point `num-mixed` like `mta_tax` at 0.53x.
  Skewed small alphabets, no repeated substrings — pure order-0 territory.

### Caveat on provenance

The coders were written from memory, not from reference implementations. The
uABS encode was recalled **wrong** and re-derived. Engines were validated
against the order-0 entropy floor (1.0002–1.0013x), which rules out the
failure mode that could change the conclusion; the frequency-table container
is mine and penalises static variants on small columns only. Full detail in
`memory/tests/TEST-2/README.md`.

### Open — the TEST-3 candidate

Order-1 over a column's **values** (not bytes) measured **0.17x of the
enumerative floor** on cdc_nndss, two of four columns at exactly **0**
(`probe_permfloor.py`, 2026-08-04). That ceiling charges nothing for the
transition table. Nobody has built the version that pays for it.

---

## LAW 3 — the codec's win is the reordering, and only the reordering

*From TEST-9 and TEST-11, 2026-08-06. 13 tables, 40,000 rows, every result
decompressed and compared. The first measurement in this project against a
model family that is not LZ-family, Huffman or block-sort.*

**Against a per-table PPMd oracle, Polypress wins 13 of 13 — but the margin
splits the corpus in two, and the split is the law.**

| | pp / best PPMd | what fires |
|---|---|---|
| cdc_nndss | **0.157** | reordering |
| nyc_baby_names | 0.579 | reordering |
| seattle_fire911 | 0.636 | reordering |
| wa_ev_population | 0.715 | reordering |
| chicago_crimes | 0.769 | reordering |
| nyc_311 | 0.809 | partly |
| noaa_gsoy_ord | 0.944 | — |
| usgs_quakes | 0.949 | — |
| noaa_gsoy_sea | 0.968 | — |
| nyc_collisions | 0.971 | — |
| usgs_quakes_deep | 0.972 | — |
| chicago_permits | 0.976 | — |
| austin_incidents | **0.989** | — |

**On the seven tables where row reordering does not fire, a context model that
does not know the file is a table gets within 1–5% of us.** On
`austin_incidents` it is 1.1%. Everything this codec does on those tables —
the parent search, the planar predictor, the dictionary work, the double
encode, 100 seconds of it — is worth about as much as running `7zz -m0=PPMd`
for 7 seconds.

### LAW 3b — a different model class is worth more than a better transform

| | vs `xz -9e` |
|---|---|
| PPMd order-16 on the **raw CSV**, no transform | **0.897** |
| our best text-stream transform (TEST-8 E) with xz behind it | 0.969 |
| sorted + front-coded alphabet (TEST-13) | 0.967 |

Changing the model class beat every transform this project has built for the
streaming tier, by 3x, at 7x the speed. LAW 2 said the coder is worth 0.01%
and the model 47%, measured over ANS variants on single columns; LAW 3b is the
same statement between two shipping codecs on whole tables.

### But the transform and the better model do NOT stack

Feeding PPMd our transformed body is **worse** than feeding it the raw text,
and worse than feeding the transformed body to xz (TEST-10, partial:
`cdc_nndss` 114,615 with xz behind the transform, 163,186 with PPMd behind
it). A transform that removes redundancy hands a context model a stream whose
contexts it no longer recognises. The research pass measured the same shape
independently: a word-replacing transform aggregates to −1.02% in front of xz
and is *negative* in front of a context mixer.

### Consequences

- **Stop trying to improve the text-stream tier by transform.** Three
  transforms have now been measured there (inline dictionary +3.1%, sorted
  alphabet +3.4%, word-level −380%) against 10.3% for changing model class.
- **The competitor set was incomplete.** All 17 were LZ-family, Huffman or
  block-sort. PPMd is now in `benchmarks/measure_one.py`. Sweeps run before
  2026-08-06 should be quoted as "17 LZ-family and columnar competitors".
- **The next model has to earn its keep on the seven tables where we tie**, not
  on the five where reordering already wins by 1.3–6.4x.

---

## LAW 4 — measure the transform through the whole codec, never through a part

*From TEST-13, TEST-17, TEST-19 and the research passes, 2026-08-06. Four
independent transforms, four measurements, one shape.*

**A transform's gain measured on a column, on a block, or through `xz` does not
survive contact with the whole codec — and the sign can flip.**

| transform | measured in isolation | measured through `fast.encode` |
|---|---|---|
| functional dependencies (TEST-17) | **+12.7%** vs xz on `cdc_nndss` | **−4.8%** |
| structured text columns (TEST-19) | **−24.9%** vs xz on usgs `time` | **−3.7%** aggregate |
| sorted alphabet (TEST-13) | **0.647x** on the alphabet block | **+3.4%** on the file |
| word-replacing transform (research) | −1.02% vs xz | negative in front of a context model |

### Why, and it is one mechanism each time

**A column is not worth its own bytes. It is worth its own bytes plus its value
as a sort parent for every other column.** Remove it and the loss lands
somewhere else entirely, which is why a per-column probe cannot see it.
TEST-18 measured this directly from the other side: removing `occ_date` from
`austin_incidents` made our position against kanzi **33.5% worse**, because it
is the reorder anchor for the rest of the table.

For TEST-13 the same statement is arithmetic rather than structural: the
alphabet block is **12.8% of the file, not the 60% a component probe claimed**,
so a correct 0.647x ratio on it delivered 3.4%, not 21%.

### The rule

1. **A component probe may nominate. It may never decide.** Same as the
   entropy rule in CLAUDE.md, one level up: entropy nominates a parent, a
   component probe nominates a transform, and only a whole-file encode decides.
2. **A probe that measures a component must also measure that component's
   share**, or its ratio means nothing.
3. **The gate belongs on the whole table, both ways, measured.** TEST-19
   ungated loses 3.7%; gated on the whole table it cannot lose and still wins
   3.7-4.4% on the four tables where it helps. That costs a double encode,
   which is already the most expensive thing in this codec — so the honest
   statement of any such transform's value includes that cost.

### What this does NOT say

It does not say component probes are useless — `_probe_len` / `_probe_bytes`
exist and are correct for nomination. It says the decision they support must be
re-taken at the top. Every transform above would have shipped on its component
number, and three of the four would have made the codec worse.
