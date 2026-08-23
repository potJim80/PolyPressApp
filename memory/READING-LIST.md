# Compression reading list

Search terms for building the fundamentals, grouped and roughly in learning
order. Written 2026-08-05.

**The frame that makes all of it click:**

> Every compressor = a **MODEL** that predicts the next symbol + a **CODER**
> that spends `−log₂(p)` bits on it. Huffman is a *coder*. LZ77 is a *model*.
> Almost every name below is one or the other, and every real format is a
> specific pairing of the two.

---

## 1. Coders — how to spend fractional bits

- **Shannon source coding theorem** — the floor. `H = −Σ p log₂ p`.
- **Kraft–McMillan inequality** — why prefix codes and probabilities are the
  same thing.
- **Arithmetic coding** — beats Huffman because it isn't stuck at whole bits.
- **Range coding** — arithmetic coding on integers; what LZMA actually uses.
- **Asymmetric Numeral Systems (ANS)** — Jarek Duda, ~2014. Arithmetic-coding
  ratios at Huffman speed. Variants **tANS / rANS**. The biggest change in the
  field in 30 years.
- **Finite State Entropy (FSE)** — the tANS implementation inside zstd.
- **CABAC** — adaptive *binary* arithmetic coding, from H.264/H.265. Same
  family as LZMA's coder.
- **Golomb–Rice codes**, **Elias gamma/delta**, **varint / LEB128** — cheap
  codes for small integers.

## 2. Models — LZ, the dictionary family

- **LZ77** (sliding window) vs **LZ78 / LZW** — the fork the field descends from.
- **DEFLATE** — LZ77 + Huffman. gzip, zip, PNG.
- **LZMA / LZMA2** — what **xz** is: LZ77 + binary range coder + literal
  context modeling + **optimal parsing**. Slow because it searches for the
  *best* parse, not the first.
- **Brotli** — LZ77 + Huffman + context modeling + a built-in 120 KB **static
  dictionary** of common web text.
- **Zstandard** — LZ77 + FSE/tANS + huff0; trained **dictionaries** for small
  inputs.
- **LZ4 / Snappy** — LZ77 with *no entropy stage*. Speed only. Why
  Parquet+snappy is a weak baseline.
- **Optimal parsing**, **lazy matching**, **match finders** (hash chains,
  binary trees, suffix automata) — where LZ encoders spend their time.

## 3. Transforms — reorder before you code

- **Burrows–Wheeler Transform (BWT)** + **Move-to-Front** + RLE → **bzip2**.
  Directly relevant: the reason bzip2 sometimes beats Polypress on
  unnormalized text (see the 9 losses in CLAUDE.md's honest-status section).
- **Suffix arrays** — how BWT gets computed.
- **Delta encoding**, **Frame of Reference (FOR)**, **PFOR / PFOR-delta**,
  **Simple-8b** — integer-sequence coding, the columnar workhorses.

## 4. Strong modeling — the top of the leaderboard

- **PPM** (Prediction by Partial Matching) — predict from the last k symbols.
- **Context mixing (CM)** — blend many models. **PAQ**, **lpaq**, **zpaq**,
  **cmix**.
- **Hutter Prize**, **Large Text Compression Benchmark** (Matt Mahoney).
- **nncp / ts_zip** — neural compressors; current record holders, unusably slow.

## 5. Columnar / table formats — the actual domain

- **Parquet encodings**: `RLE_DICTIONARY`, `DELTA_BINARY_PACKED`,
  `DELTA_BYTE_ARRAY` (front coding), `BYTE_STREAM_SPLIT`. Then a general codec
  on top. Search each individually — this is the competitor.
- **Dremel record shredding** — definition/repetition levels; how Parquet
  stores nested data.
- **ORC RLE v2** — `SHORT_REPEAT` / `DIRECT` / `PATCHED_BASE` / `DELTA`.
- **Abadi, "Integrating Compression and Execution in Column-Oriented Database
  Systems"** — the founding paper.
- **BtrBlocks**, **FastLanes**, **Data Blocks** — modern research on exactly
  this problem.
- **Apache Arrow / Feather** — in-memory columnar, mostly *not* compression.
- **Roaring bitmaps** — compressed sets, everywhere in analytics.

## 6. Numeric and float-specific

- **Gorilla encoding** (Facebook) — delta-of-delta timestamps + XOR'd floats.
  The time-series standard.
- **ALP** (Adaptive Lossless floating-Point), **Chimp**, **Pcodec**, **FPC** —
  modern float codecs.
- **Byte stream split** / **shuffle filter** (HDF5) / **bitshuffle** /
  **Blosc** — split floats into byte planes so like bytes sit together. Cheap,
  big wins, directly applicable to the matrix case.

## 7. Theory worth the detour

- **Kolmogorov complexity** — the uncomputable ideal.
- **Minimum Description Length (MDL)** — model selection as compression.
- **Cross-entropy / KL divergence** — literally "extra bits paid for a wrong
  model".
- **Rate–distortion theory** — the lossy side.

---

## If you only read four things

1. Matt Mahoney, **"Data Compression Explained"** — free, best single text.
2. Duda's **ANS** paper (or Yann Collet's zstd blog posts, gentler).
3. Abadi's **column-store compression** paper.
4. The **Parquet encodings spec** — short, and it is the competitor's source
   code in prose.
