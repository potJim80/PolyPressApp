# TEST-1 — one column: plain xz, or sort-and-regress?

Run 2026-08-05. Produced **LAW 1** and **LAW 1b** (see `memory/LAWS.md`).

## The question

For a data table with n rows and **1 column**, which is smaller:

- **A** — plain `xz -9e` on the column as text, one value per line.
- **B** — number-encode every value, sort ascending, pancake equal values
  into runs (`A A A B B C` → `3A 2B C`), regress, store the differences —
  **plus** whatever it takes to put the rows back where they were.

## How it was run

- `law1.py` — the codecs. `run_law1.py` — the driver. `results.txt` — raw output.
- Run from `work/`: `python3 ../memory/tests/TEST-1/run_law1.py`
- **Every B variant has a real decoder and was decoded and compared value by
  value before its size was counted.** Nothing here is a size estimate.
- **B was given an oracle.** Where a part of B could be encoded several ways
  (dictionary as sorted text / regressed / delta-coded; run lengths plain or
  regressed; codes bit-packed or varint), B was charged the *smallest*. It
  could not achieve this without trying them all. B loses anyway.
- Every variant is a self-contained length-prefixed container, so no part of
  B is measured off the books.

## The four B variants

| variant | how the row order is stored |
|---|---|
| `B_raw` | one row number per row — the literal reading of the proposal |
| `B_gap` | stable sort leaves row numbers ascending inside a run, so store gaps — the best the row order can be coded |
| `B_min` | equal values are interchangeable, so only say *which value* each row holds, not which copy — the information-theoretic floor |
| `B_bag` | no row order at all. Not lossless for a table; measured only to show what the sort and the pancake actually buy |

`B_min` does not need the run lengths (they are derivable), so it is not
charged for them.

## Corpus

- **66 real columns** from the first 12 files of `IN/corpus100/`, capped at
  200k rows, chosen to span cardinality rather than taking the first columns.
- **10 synthetic controls**: constant, 2/10/1000 codes, zipf, all-distinct
  ids shuffled and sorted, a random walk, pre-grouped codes, a cyclic column.

## Result

| scheme | total vs `xz -9e` | columns won |
|---|---|---|
| `B_raw` | **1.29x** (worse) | 6/76 |
| `B_gap` | **1.03x** | 28/76 |
| `B_min` | **0.89x** | 65/76 |

**`B_gap` beat `B_min` on 0 of 76 columns.** The sort never paid for itself
on any column, real or synthetic.

Real columns only: `B_min` 0.86x, winning 57 of 66.

## What it means

See `memory/LAWS.md`. In short: sorting moves information into the
permutation rather than destroying it, and the permutation's floor is exactly
what plain order-0 coding costs — so the sort cannot come out ahead. The win
that showed up is entirely from step 1, number-encoding.
