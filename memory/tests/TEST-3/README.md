# TEST-3 — Huffman over VALUES against plain xz

Run 2026-08-05, on Mahdi's proposal: *forget the permutation and the pancake,
just Huffman-encode the columns; then no dictionary is needed other than the
Huffman tree.*

10 real columns from `IN/corpus500`, capped at 400 KB, all round-trip verified.

## Result

| variant | total | vs xz | beats xz |
|---|---|---|---|
| **A `xz -9e`** | **126,937** | **1.000** | — |
| `dict+xz` | 126,405 | **0.996** | **7/10** |
| `rANS-val` (same model, fractional bits) | 146,935 | 1.158 | 4/10 |
| `huff-val` | 154,294 | 1.216 | 2/10 |
| `huff-val-raw` (alphabet not xz'd) | 199,941 | 1.575 | 2/10 |
| `huff-byte` | 486,001 | 3.829 | 0/10 |

## Three things it settled

**1. The tree does NOT replace the dictionary.** A Huffman tree assigns a code
length per symbol; the decoder still has to be told what string each symbol
*is*. Leaving the alphabet uncompressed costs 1.216 → 1.575. On the
highest-cardinality column (`adopted`, k=7,646) the alphabet is **24,278 of
the 48,793 bytes** — half the container is the dictionary that was supposed to
have gone away.

**2. Huffman cannot spend less than one bit per symbol, and on skewed columns
that dominates everything.** Two `blank-heavy` columns:

| column | n | k | value entropy | huff-val | over floor |
|---|---|---|---|---|---|
| `settlement` | 38,734 | 2 | 172 B | 4,887 B | **28.4x** |
| `authority` | 38,734 | 7 | 79 B | 5,015 B | **63.3x** |

38,734 rows x 1 bit = 4,842 bytes, and no Huffman code can do better. rANS on
the identical model spends **234** and **258** bytes. This is the same defect
that retired Rice coding from `polypress/codec.py` — *it cannot spend
fractional bits* — and it is why the modern answer is arithmetic/ANS, not a
cleverer Huffman.

Away from that failure mode Huffman is fine: `huff-val / value entropy` is
1.00-1.18 on the other eight columns. The coder is not the problem; the
one-bit granularity is.

**3. The value floor is BELOW xz, so the idea has real room — nothing here
reaches it.**

| | bytes | vs xz |
|---|---|---|
| n·H(value) — the order-0 value floor | 105,103 | **0.828** |
| `xz -9e` | 126,937 | 1.000 |
| best implementation here (`dict+xz`) | 126,405 | 0.996 |

An order-0 model over *values* is worth 17% more than xz **in principle**, and
every implementation measured gives all of it back to container overhead.
That gap — 0.828 achievable vs 0.996 achieved — is the open item.

## Relation to the earlier laws

- LAW 1b said the win belongs to number-encoding, not sorting, and measured
  dictionary+codes at 0.86x on 66 columns. `dict+xz` here is 0.996x on 10 —
  a different, harder sample, so the two are not in conflict, but the 0.86x
  should not be quoted as general.
- LAW 2 said the coder is worth ~0.01% between ANS types. Huffman is **not**
  in that equivalence class: it is 5% worse than rANS on the same model here,
  and 28-63x worse in the skewed corner.

## Files

`huff.py` (canonical Huffman over bytes and values), `run_test3.py`,
`results.txt`. Run from `work/`:
`python3 ../memory/tests/TEST-3/run_test3.py`
