# dtz — a table compressor that picks the best strategy

Give it a data table in almost any form. It tries every encoding validated in
this project plus the standard general-purpose compressors, verifies each one
round-trips, keeps the smallest, and writes a self-describing container.

It is **not** a new compression breakthrough. The value is selection: the
standard tools are among the candidates, so the output is never worse than the
best of them, and on some tables is much better.

## Install

Needs Python 3.9+. Optional but recommended:

```bash
python3 -m pip install pyarrow zstandard
```

Without them the Parquet and zstd strategies are skipped; everything else works.

## Use

```bash
python3 dtz.py compress   data.csv -o data.dtz     # compress
python3 dtz.py decompress data.dtz -o out.csv      # restore
python3 dtz.py inspect    data.dtz                 # what's inside
python3 dtz.py bench      data.csv                 # every strategy's size
```

Add `--unordered` to `compress`/`bench` to allow row reordering. That
compresses better but **discards the original row order**, so only use it when
the table is a set of records rather than a sequence.

The output extension chooses the format, so this converts as it decompresses:

```bash
python3 dtz.py decompress data.dtz -o out.parquet
```

**Input:** `.csv .tsv .psv .txt .dat .json .jsonl .ndjson .parquet`
**Output:** `.csv .tsv .json .jsonl .parquet`

## Fidelity contract

dtz preserves the **logical table** exactly: column names, column order, row
order, and every cell as an exact string. Round-trip is verified in memory on
every compress; it refuses to write output if verification fails.

It does *not* promise to reproduce an input file byte-for-byte, because CSV
quoting, line endings, and whitespace conventions are not canonical. Read a
file and write it back and you get an equivalent table, not identical bytes.

Some output formats cannot hold some tables — duplicate column names in JSON or
Parquet, or headers for a zero-row `.jsonl`. Those raise `FormatLimit` and
write nothing rather than silently corrupting. `.csv`, `.tsv` and `.dtz` have
no such limits.

## Strategies

| strategy | what it does |
|---|---|
| `csv.xz` / `csv.zstd` / `csv.bz2` | canonical CSV through a general compressor |
| `colmajor.xz` | transpose to column-major text, then xz |
| `poly` / `poly.xz` | per-column polynomial predictor + Rice coding (`codec.py`) |
| `parquet.zstd` | typed Parquet, fixed-point ints where exact, zstd-22 |
| `fd.parquet.zstd` | detect exact functional dependencies, normalise, Parquet |
| `sorted.*` | as above but rows sorted first — `--unordered` only |

## What actually wins, on real data

```
steam.csv          (400 x 5,  smooth scientific)   poly.xz        23.94x
geo.tsv            (20k x 6,  real string FDs)     colmajor.xz     8.21x
airport-codes.csv  (86k x 13, real, quoted, nulls) colmajor.xz     4.78x
```

Two honest observations from building this:

- `colmajor.xz` — transpose, then xz — wins on most real tables. It is about
  fifteen lines. Layout matters far more than clever encoding.
- `fd.parquet.zstd`, the most sophisticated strategy here, has never won a
  benchmark in this project. Measured against best-tuned Parquet+zstd on real
  data it reaches roughly 1.35x, and its core mechanism was already published
  in 2024 (Corra, arXiv 2403.17229). It is kept because selection costs nothing
  and it may win on a wide denormalized table with high-cardinality string
  dependents.

`poly` earns its place on smooth numeric tables and nowhere else, which is
exactly what the theory predicts: it wins when a column's high-order finite
differences decay.

## Tests

```bash
python3 test_dtz.py
```

18 fidelity cases — embedded delimiters, quotes, newlines, unicode, ragged
rows, duplicate headers, leading zeros, integers beyond int64, degenerate
shapes. Every strategy must round-trip every case exactly.

## Container format

```
"DTZ1" | u16 name_len | strategy_name | u32 meta_len | xz(json metadata) | payload
```

Versioned by magic. The metadata is xz'd JSON, so a container is inspectable
without special tooling and new strategies can be added without breaking old
files.
