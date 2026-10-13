# Polypress

A lossless compressor for data tables: CSV, TSV and other delimited text, JSON,
JSON Lines, Parquet, and Stata, SPSS and SAS files.

The table comes back exactly: the same columns in the same order, the same rows
in the same order, and every cell as the exact text it was. Before writing an
archive, Polypress decodes it and compares every cell.

## Why it is smaller

General-purpose compressors see a table as a stream of bytes. Columnar formats
compress each column on its own. Polypress uses the fact that the columns of a
real table depend on each other.

- **Rows are reordered by a parent column.** `City` is nearly determined by
  `Postal Code`. Sorting the rows by postal code puts equal cities next to each
  other, and the city column collapses into long runs. The order costs nothing
  to store: the decoder rebuilds the parent column first and repeats the same
  sort. Rows come back in their original order.
- **Numbers are predicted** from the values above them and from comparable
  neighbouring columns, and only the error is stored.
- **Copied values are stored once.** A number that also appears inside another
  column's text (`latitude` inside `location`) is not stored twice.
- The modelled columns are finished with xz.

The core idea is not new. US 8,312,026 B2 (Vo, AT&T, filed 2009) describes it.
It was arrived at here independently, and the patent has expired.

## Results

Measured on 1,000 government open-data tables chosen by a rule, not by hand:
the Socrata catalog's datasets in order of page views, keeping the first 1,000
that download as CSV and have at least 2 columns, 20 rows and 50 KB, with no
duplicates. That is 7.2 GB of CSV and 27.9 million rows. The 189 largest tables
were cut at a fixed size, and every tool got the same cut file.

Polypress 1.00 was compared with 22 other tools: xz, zstd, gzip, bzip2, brotli,
lz4, zip, 7-Zip (LZMA2 and PPMd), and Parquet, ORC and Feather with their
codecs.

| | |
|---|---|
| Restored exactly | 1,000 of 1,000 |
| Smallest of all 23 tools | 781 of 1,000 (78%) |
| All 1,000 together, against the best other tool on each table | 418 MB vs 486 MB (1.16x smaller) |
| Compression of the CSV | median 16x |

Head to head, Polypress is smaller than `xz -9e` on 990 of 1,000 tables,
`zstd -19` on 997, `gzip -9` on all 1,000, and `parquet+zstd` on 899 of 900.

**Where it loses.** It is not the smallest on 219 tables, mostly text-heavy
ones, and mostly to 7-Zip's PPMd. The worst result is 54% bigger than
`orc+zstd` on one table.

**Speed.** On an Apple M4 MacBook Air, it compresses at about 18 MB/s and
decompresses at about 150 MB/s (`xz -9e`: 4 and 199). That is one timing pass,
so read it as an order of magnitude.

**Parquet and exact text.** Parquet, read with type inference, changed the
printed text on 702 of the tables (`1.50` became `1.5`). Part of a Parquet size
win there is lost formatting, not compression.

Every table in the benchmark is government open data, so there is no reason to
assume the results carry over to other kinds of data. Nobody outside the
project has used it yet.

## Build

You need a C compiler. The xz and ReadStat sources are included, so nothing else
has to be installed.

```sh
sh work/csrc/build.sh                              # -> work/csrc/polypress
PREFIX=/usr/local sh work/csrc/build.sh install    # put it on your PATH
```

- **Mac app:** `sh work/app/build_app.sh` builds `Polypress.app` into
  `~/Applications` (macOS 11 or later, Xcode command line tools).
- **Parquet** goes through `work/py/parquet.py`, which needs Python 3 and
  `pyarrow`.

## Use

```sh
polypress compress data.csv              # -> data.csv.ppz
polypress restore  data.csv.ppz          # -> data.csv
polypress info     data.csv.ppz          # what is inside
polypress convert  data.csv data.json    # one table format to another
polypress stream-compress big.csv        # for files bigger than memory
```

- **Exactly means the table.** Column names, order, rows and every cell come
  back exactly. A CSV file may not come back byte for byte, because quoting and
  line endings can be written more than one way.
- **Stata, SPSS and SAS files** are kept byte for byte, labels included. When
  one is converted to another format, Polypress says what the conversion leaves
  out.
- **Text encoding is never guessed.** UTF-8, or a file with a byte-order mark,
  is read as such. Anything else is refused with the bad byte named. Name the
  encoding with `--encoding latin-1` (or `cp1252`, `utf-16`, ...).

## Licence

MIT. The bundled xz (0BSD) and ReadStat (MIT) keep their own licences, in their
folders under `work/csrc/`.
