# TEST-13 — the alphabet's order, and a claim cut to a quarter of its size

Run 2026-08-06. Checks the design pass's largest claim with a real decoder.

**The claim:** the distinct strings are ~60% of the compressed output and are
shipped in first-appearance order; sorting them and front-coding is worth
~21% of the whole file. It came from a size probe with no decoder — the same
standing as TEST-4's probes, which undercharge containers and bias toward the
challenger.

**The verdict: the ratio was right, the term it multiplies was four times too
big.** Sorting is worth **3.4%**, not 21%.

## Result

| variant | total | vs `xz -9e` | vs B |
|---|---|---|---|
| `xz -9e` on the CSV | 11,184,465 | 1.000 | — |
| **B** alphabet block, appearance ids | 11,201,075 | 1.001 | 1.000 |
| **C** inline dictionary (TEST-8) | 10,975,531 | 0.981 | 0.980 |
| **Bs** alphabet block, **sorted**, rank ids | 10,891,103 | 0.974 | 0.972 |
| **Bsf** sorted + **front-coded** | **10,820,789** | **0.967** | **0.966** |

And the alphabet block measured on its own, which is where the claim went
wrong:

| alphabet block alone | bytes | ratio |
|---|---|---|
| appearance order | 1,434,274 | 1.000 |
| sorted | 998,508 | 0.696 |
| sorted + front-coded | 928,189 | **0.647** |

The predicted ratio was 0.628 and the measured one is 0.647 — close. But the
block is **12.8% of the baseline (1,434,274 / 11,184,465), not 60%.** The
probe's 6,739,572-byte figure is not the alphabet as this codec ships it.
21% predicted, 3.4% delivered, and the whole gap is the size of the term.

**Rule this restates:** a probe that measures a component in isolation must
also measure that component's *share*, or its ratio means nothing. Same family
as the measured/unmeasured trap in CLAUDE.md.

## Per table — sorting helps and hurts, and the reason was predicted

| table | Bsf/B | alphabet app → front |
|---|---|---|
| nyc_311 | **0.909** | 279,930 → 146,822 |
| seattle_fire911 | 0.922 | 246,669 → 146,334 |
| wa_ev_population | 0.941 | 39,368 → 16,690 |
| nyc_collisions | 0.964 | 142,118 → 77,776 |
| noaa_gsoy_sea | 0.972 | 1,515 → 1,123 |
| usgs_quakes | 0.978 | 30,275 → 10,637 |
| chicago_permits | 0.979 | 596,648 → 475,528 |
| austin_incidents | 1.001 | 6,801 → 4,510 |
| **nyc_baby_names** | **1.075** | 11,489 → 7,122 |
| **cdc_nndss** | **1.098** | 5,624 → 4,213 |

**The alphabet shrinks on every single table, and the total still gets worse
on two.** That is the id trade, and it was predicted before the run: appearance
ids are Zipf-friendly — the value you meet first is usually a common one, so
frequent values get short ids and the body compresses well. Rank ids scatter
frequent values across the id space. On `cdc_nndss` the alphabet drops 1,411
bytes and the body gains 9,609.

A measured per-table gate (invariant 2: encode both ways, keep the smaller)
totals 10,806,460 — **0.13% better than always sorting.** Both losers are
small tables, so the gate barely pays. If it is built, build it because it
cannot lose, not because it wins much.

## Where this leaves the two schemes

    Bsf   sorted + front-coded alphabet block   0.967   two passes, block header
    E     inline dictionary, causal gate        0.969   one pass, no alphabet

**A dead heat.** The two-pass form with a sorted block and the single-pass
form with no block at all land within 0.2% of each other. They cannot be
combined — an inline dictionary is *defined* by the order values arrive in,
which is appearance order. Choosing between them is a choice about
architecture (streaming, memory, random access), not about size.

## Caveats

- 40,000 rows, 13 tables, the same corpus and canonical CSV as TEST-8/9/11.
- Front coding is applied to every dictionary column unconditionally here. In
  `fast.py` it sits behind `_choose_front`, a measured gate; a gated version
  would be no worse.
- Ids are decimal ASCII in all variants, as in TEST-8, so the id-length effect
  is real but its magnitude is framing-dependent.
- The selftest covers 16 adversarial tables plus 200 randomised front-coding
  cases, including embedded separators, escapes and non-ASCII.
