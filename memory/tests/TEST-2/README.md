# TEST-2 — one column: plain xz, or an ANS entropy coder?

Run 2026-08-05. Follows TEST-1, which produced LAW 1 and left one question
open: the number-encoded variant sat **1.20x above the order-0 floor** and
xz-on-text at 1.56x, because that variant bit-packed its codes and xz'd the
result instead of running a real entropy coder. This test runs the real
entropy coder.

## The question

For a data table with n rows and **1 column**:

- **A** — plain `xz -9e` on the column as text, one value per line.
- **B** — an ANS entropy coder on the same bytes.

## Provenance of the coders — read this before quoting any number

**The coders were written from memory, not taken from a repository.** No
reference implementation was fetched, consulted, or vendored. What that means
per coder:

| coder | origin | outcome |
|---|---|---|
| rANS | ryg's `rans_byte.h` (Fabian Giesen, public domain), recalled | correct |
| tANS | Collet's FSE — spread step, `deltaNbBits`/`deltaFindState` | correct; the `f == 1` special case reconstructed by reasoning |
| rABS | derived, not recalled — rANS on a two-symbol alphabet | correct |
| uABS | Duda's uABS, recalled | **wrong**, off by one on every state; re-derived from the decode map and brute-forced |
| bit tree + `p += (PONE-p)>>5` | LZMA's literal coder / CABAC, recalled | correct |
| `normalise()` | written here, ad hoc | **not** FSE's `FSE_normalizeCount` |

The uABS failure is the calibration for the rest: recall was confidently
wrong, and only a brute-force check of the inverse over 12 probabilities and
5,200 states caught it. `test_ansfam.py` round-trips all eight coders on 28
adversarial inputs, including the 255-symbol case that forces a normalised
frequency of 1 through tANS's special path.

**Round-tripping proves correctness, not efficiency.** A coder can be a
perfect bijection and still waste bits. The check that rules that out is
payload against the order-0 entropy floor — and because the floor is computed
from the *true* counts while the payload uses the *normalised* ones, it tests
`normalise()` at the same time:

| input | n | floor | rANS payload | tANS payload |
|---|---|---|---|---|
| uniform random, 256 symbols | 60,000 | 59,977 | 1.0002x | 1.0003x |
| skewed 90/10 | 60,000 | 3,494 | 1.0010x | 1.0013x |
| zipf-ish, 40 symbols | 60,000 | 19,093 | 1.0004x | 1.0006x |
| csv-ish text | 47,368 | 21,150 | 1.0002x | 1.0002x |

The engines are sound. **Two things are still mine and still suboptimal**, and
both bias the same way:

- the frequency table is varint + xz (144 B for 256 symbols); FSE's real
  header is a packed bitstream, perhaps half that
- `tableLog` is pinned at 12, where FSE derives it from input size

Both penalise the **static** variants on **small** columns and nowhere else.
So the small-column rows below measure this container as much as they measure
ANS, and are labelled accordingly. The headline is not exposed: no plausible
header fix moves an aggregate of 2.89x.

## How it was run

- `rans.py` — rANS. `ansfam.py` — tANS, rABS, uABS, interleaved rANS.
  `test2.py` — the variants. `harvest.py` — corpus selection.
  `run_test2.py` / `run_test2b.py` — the drivers.
- Run from `work/`: `python3 ../memory/tests/TEST-2/run_test2.py`
- **Every variant was decoded and compared value by value before its size was
  counted.** Nothing here is a size estimate.
- Every size is a **whole self-contained container**. The frequency table,
  the dictionary and the lengths are all charged. An entropy coder that
  reports only its payload is measuring nothing.

## Corpus

**200 real single columns** from `IN/corpus500`, classified by content rather
than by header — headers lie — and taken to a quota per kind, because the
first 200 columns off the corpus would be ~60% categorical codes and would
answer a question about one shape.

| kind | columns |
|---|---|
| `int-only`, `num-mixed`, `date-time`, `text-only`, `text-mixed`, `id-highcard`, `blank-heavy` | 29 each (26 for `id-highcard`) |

Capped at 100k rows and 900 KB per column in TEST-2; **150 KB in TEST-2b**,
because the binary coders run at 0.25 MB/s in Python. Totals are therefore
not comparable between the two runs. Ratios are.

## Result — TEST-2, rANS against xz, 200 columns, 80.9 MB

| variant | total | vs xz | columns won |
|---|---|---|---|
| `xz -9e` | 7,206,941 | 1.00x | — |
| rANS order-0 | 36,140,838 | 5.01x | 3/200 |
| rANS order-1 | 20,860,279 | 2.89x | 20/200 |
| dictionary + xz | 7,194,659 | **0.998x** | — |
| dictionary + rANS | 10,420,751 | 1.45x | — |

**ANS on the raw column text is not close.** Order-0 rANS lands at exactly
1.00x the order-0 floor — it is doing its job perfectly — and that is the
problem: the order-0 floor of a text column is 5x worse than xz.

By kind, best ANS against xz:

| kind | ANS/xz | ANS wins |
|---|---|---|
| `text-mixed` | 8.85x | 1/29 |
| `text-only` | 5.41x | 2/29 |
| `date-time` | 5.13x | 0/29 |
| `int-only` | 5.05x | 2/29 |
| `blank-heavy` | 2.01x | **12/29** |
| `id-highcard` | 1.93x | 0/26 |
| `num-mixed` | 1.39x | 3/29 |

## What it means

**The coder was never the bottleneck; the model was.** This is the same
finding as `OUT/results/sortreg-summary.txt` item 6, reached from the
opposite direction and on 200 columns instead of 9.

- An order-0 byte model cannot see repetition *across* values. `"CHICAGO"`
  appearing 40,000 times is 40,000 independent draws to it, and seven bytes
  of entropy each time. LZ77 sees one match.
- The gap closes exactly where the model gets better, not where the coder
  gets better: order-1 halves the deficit (5.01x → 2.89x), and swapping the
  coder for a different ANS type moves it by ~0.1%.
- **Where ANS wins is where LZ has nothing to match**: 12 of 29 wins are
  `blank-heavy` columns, whose content is a skewed two-or-three-symbol
  alphabet with no repeated substrings worth a match. That is the order-0
  case, and there ANS is at the floor while xz pays for match machinery.

## Result — TEST-2b, every ANS type, 200 columns, 24.0 MB

Columns capped at 150 KB here, not 900 KB, because the binary coders run at
0.25 MB/s in Python. **Totals are not comparable with the run above; ratios
are.**

| coder | ANS type | alphabet | model | total | vs xz | wins | ÷floor |
|---|---|---|---|---|---|---|---|
| **A `xz -9e`** | — | — | LZ77 + range coder | **2,376,632** | **1.00x** | — | 0.14 |
| B7 rABS-o1 | range binary | bit tree | order-1 adaptive | 5,183,698 | 2.18x | 7/200 | 0.57 |
| B2 rANS-o1 | range | byte | order-1 static | 5,764,871 | 2.43x | 20/200 | 0.61 |
| B4 tANS-o1 | table (FSE) | byte | order-1 static | 5,765,352 | 2.43x | 20/200 | 0.61 |
| B6 rABS-o0 | range binary | bit tree | order-0 adaptive | 9,800,235 | 4.12x | 1/200 | 1.02 |
| B8 uABS-o0 | uniform binary | bit tree | order-0 adaptive | 9,800,839 | 4.12x | 1/200 | 1.02 |
| B1 rANS-o0 | range | byte | order-0 static | 10,096,177 | 4.25x | 4/200 | 1.00 |
| B3 tANS-o0 | table (FSE) | byte | order-0 static | 10,096,699 | 4.25x | 4/200 | 1.00 |
| B5 rANSx4-o0 | range, 4 lanes | byte | order-0 static | 10,098,289 | 4.25x | 4/200 | 1.00 |

What each axis is worth:

| comparison | ratio | wins | meaning |
|---|---|---|---|
| tANS-o0 / rANS-o0 | 1.0001 | 34/200 | table vs range form: **nothing** |
| uABS-o0 / rABS-o0 | 1.0001 | 0/200 | uniform vs range binary: **nothing** |
| rANSx4-o0 / rANS-o0 | 1.0002 | 0/200 | interleaving: **nothing** (speed only) |
| rABS-o0 / rANS-o0 | 0.971 | 71/200 | adaptive bit-tree vs static byte |
| rABS-o1 / rANS-o1 | 0.899 | 91/200 | same, order 1 |
| **rANS-o1 / rANS-o0** | **0.571** | **192/200** | **order 1 vs order 0** |
| **rABS-o1 / rABS-o0** | **0.529** | **195/200** | **same, adaptive** |

By kind, best ANS of any type:

| kind | cols | xz | rANS-o1 | rABS-o1 | best B/xz |
|---|---|---|---|---|---|
| date-time | 29 | 206,517 | 972,562 | 970,993 | 4.55x |
| text-mixed | 29 | 194,213 | 1,064,367 | 850,539 | 4.26x |
| text-only | 29 | 162,433 | 632,112 | 659,912 | 3.76x |
| int-only | 29 | 114,444 | 567,465 | 251,514 | 2.17x |
| blank-heavy | 29 | 90,486 | 161,784 | 177,881 | 1.78x |
| id-highcard | 26 | 783,748 | 1,344,028 | 1,238,477 | 1.56x |
| num-mixed | 29 | 824,791 | 1,022,553 | 1,034,382 | 1.22x |
| **ALL** | **200** | **2,376,632** | **5,764,871** | **5,183,698** | **2.11x** |

**The small-column row in `results-family.txt` measures this container, not
ANS.** Only 8 columns are under 20 KB, and my varint+xz frequency header
(144 B for 256 symbols) plus the pinned `tableLog=12` inflate the static
variants there specifically. Do not read it as a property of ANS.

## Not tested here

- **Arithmetic / range coding as a standalone case.** `xz` contains one, and
  `rABS-o1` is its structural twin, but no isolated range coder was run over
  these 200 columns. Older evidence only: `attic/rc.py` (one dataset),
  `probe_sortreg.py` (9 tables).
- **PPM** — variable-order Markov. On the reading list, never built.
- **Context mixing** (PAQ/lpaq/zpaq/cmix) — never built.

## TEST-3 candidate

**Order-1 over a column's *values*, with the transition table charged.**
`probe_permfloor.py` (2026-08-04) measured `n·H(value | value in the previous
row)` at **0.17x of the enumerative floor** on cdc_nndss, with `states` and
`sort_order` at exactly **0** — perfectly predicted by the previous row. It
also beats Cover's enumerative code by 6x, because that floor assumes
exchangeable rows and real tables arrive clustered.

That number charges nothing for the k x k transition table, so it is a
ceiling, not an achievable size — entropy nominates, it does not decide. The
untried work is the version that pays for the table. Given two columns came
out at exactly 0, the matrix is likely sparse enough to be worth it on
clustered administrative data, which is the genre this codec is already best
at.

A second, cheaper candidate: TEST-1's `B_min` re-run with a real entropy
coder rather than the bit-pack + xz oracle. TEST-2's `D-ans` row (dictionary
codes handed to rANS lost to the same codes handed to xz, 1.45x) predicts it
loses — the code stream still carries LZ-visible runs an order-0 coder cannot
see.
