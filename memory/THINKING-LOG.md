# Thinking log

Mahdi thinks out loud; this file records it. **Recorded verbatim in substance,
not paraphrased into conclusions.** Feedback is only given when asked, and
when it is given it goes in a clearly marked block so the thinking and the
response never blur together.

Rules for this file:

- Thoughts are recorded even when they contradict a LAW. A law is a record of
  what was measured, not a filter on what may be thought.
- Nothing here is a finding. Findings live in `LAWS.md`, and only after
  `tests/` produces a measurement with a decoder.
- Ideas that get measured are linked to the test that measured them, so the
  log stays honest about which thoughts were paid for and which are still
  free.

---

## 2026-08-05 — the frame

**The goal: find a better way to compress data tables.**

### Thought 1 — the whole game is proximity

> To compress data tables, the best way is to find a way to group or feed
> similar patterns closer together to an **established** compressor. That has
> always been the idea to some extent.

Note the shape of this: the compressor is taken as given and is **not** the
thing being improved. The lever is what order the bytes arrive in. Matches
the frame in `READING-LIST.md` — every compressor is a MODEL plus a CODER,
and this is a statement that the work is all in the model.

Consistent with [[law-2]]: the coder is worth 0.01%, so improving it is not
where the room is.

### Thought 2 — column-major, not row-major

> The best way to do this is to feed down a column, not down a row. (right?)

Asked as a check. Answered — see the feedback block below.

> **FEEDBACK GIVEN (asked):** Yes, and it is the single largest layout effect
> measured in this repo. From the retired `stridexz` fork
> (`OUT/results/xzlab-ideas.txt`): all eight layout ideas together were worth
> **2.8%** over a column-blocks baseline, and *nearly the entire effect was
> the one act of writing columns contiguously*. Everything after that is
> small change by comparison.
>
> Not raised at the time, kept here for when it becomes relevant: there is a
> limit to how far "group similar things together" goes. Splitting **per
> column** costs 1-19% versus one stream, because the compressor's dictionary
> can no longer reach across columns; splitting **by kind** (binary units vs
> text units) is free at every scale, -1.15% to +0.41%
> (`OUT/results/streams-probe.txt`). So proximity helps until fragmentation
> starts costing more than adjacency buys.

### Thought 3 — chunking is the first problem, not a later one

> The issue is how to also do chunking alongside that. Because you need to
> read the entire n rows to go down a given column to the nth cell, the
> memory explodes. So there is firstly and foremostly a **chunking issue**.

The crux as stated: column-major output and row-major input are in direct
conflict. A CSV arrives one row at a time, but writing a column contiguously
means holding every cell of that column — so the whole table is resident
before the first byte can be emitted. Memory is O(table), not O(chunk).

Framing to preserve: he is calling this **primary**, not an implementation
detail to be handled after the compression scheme is chosen. That ordering is
the claim — that the chunking strategy constrains what compression scheme is
even available, rather than the other way round.

*Links held for when feedback is asked for, deliberately not raised:*
`polypress/stream.py` (the existing bounded-memory block variant); the
measured peak-memory rule `(input MB x 22) + 700` and the 2,300 MB peak on a
73.7 MB table; and the closing paragraph of `OUT/results/streams-probe.txt`,
which is about exactly this conflict.

### Thought 4 — is out-of-core columnisation a solved problem?

> So to figure out a way to reorganize a table by columns rather than rows,
> without holding everything in memory at once, is a major thing. Do any
> algorithms exist out there, any github repos, that do this very cheaply and
> effectively?

Direct research question, so it was answered — searched rather than recalled.

> **ANSWER GIVEN (asked):** Yes. It is solved, named, and standard.
>
> - **The name is PAX** — Partition Attributes Across, Ailamaki, DeWitt, Hill,
>   Skounakis, VLDB 2001, CMU. Chunk the rows into a *row group*, then store
>   column-wise **inside** the chunk. Memory becomes O(row group), not
>   O(table). This is exactly the "chunking alongside column-major" problem,
>   and it is what Parquet, ORC and Arrow all do.
> - **The theory is closed.** Floyd (1972) gave optimal I/O bounds for
>   external transposition; Aggarwal & Vitter (1988) generalised them with
>   matching upper and lower bounds. The two optimal algorithms are variants
>   of **merge sort** and **distribution sort**. Cache-oblivious recursive
>   transposition also exists.
> - **Best reference implementation for the bounded-memory question
>   specifically:** Rust `parquet::arrow::ArrowWriter` — it exposes
>   `max_row_group_row_count`, `max_row_group_bytes`, and `memory_size()` /
>   `in_progress_size()` so the caller can flush early on a memory threshold.
>   That is the actual API for "columnar output under a memory ceiling".
> - **The known trap:** pyarrow's incremental `ParquetWriter` does *not* give
>   bounded memory by default (ARROW-10052) — it accumulates.
>
> **The parameter that matters is chunk size, and it is a direct dial between
> memory and ratio.** Already measured in this repo, so the curve is known:
> `turbo.py` splitting the string pile 1/2/4/8 ways costs 0 / +1.35% / +2.00%
> / +5.88%; `streams-probe.txt` shows the per-column penalty collapsing from
> 19.1% to 0.9% as the dictionary stops being able to span a column. Smaller
> chunks = less memory = worse compression, and the exchange rate is
> *table-dependent*.

### Thought 5 — "we tried this. What was the issue? Why can't you keep a dictionary across row groups?"

> Wait. I am not understanding. We tried doing this, but what was the issue?
> Why can't you maintain a dictionary across row groups?

> **ANSWER GIVEN (asked):** The premise is wrong in a useful way — **you can**
> keep the dictionary across row groups. That is not what broke.
>
> What we built is `polypress/stream.py`: row blocks, each compressed
> **independently**, concatenated behind an index. Its own header names the
> cost, and it is not the dictionary:
>
> > *"The cross-column reordering only sees correlations inside a block, so
> > smaller blocks compress slightly worse."*
>
> Three separate things are lost when a table is chunked, and they are not
> equally expensive:
>
> | lost | recoverable? | how |
> |---|---|---|
> | LZ77 sliding window | **yes, free** | one continuous xz stream; the window is bounded (64 MiB at -9) and does not grow with the file |
> | value→code dictionary | **yes, cheap** | keep one global map; costs O(distinct values), not O(rows) |
> | **the row permutation** | **no** | a global sort needs all rows; this is the codec's central idea and the only one that truly requires the whole table |
>
> So the binding constraint is **the sort, not the dictionary**. Chunking
> hurts this codec more than it hurts Parquet precisely because Parquet has
> no global reorder to lose.
>
> Why the standard formats don't share dictionaries across row groups: they
> are buying **row-group independence** — parallel readers, predicate
> pushdown, splitting one file across machines. Polypress is an archival
> codec that always decodes the whole file, so it is paying that price for a
> feature it does not use.
>
> **Gap found while answering:** the cost of blocking in `stream.py` is
> asserted ("slightly worse") and **never measured**. No number for it exists
> anywhere in the repo. By this project's own standard that is an unverified
> claim sitting in a shipped module.

### Thought 6 — "so the issue is row storage?"

> Okay. So the issue [is] row storage?

> **ANSWER GIVEN (asked):** Partly, and separating the two halves is the whole
> point — they have different fixes.
>
> | problem | caused by row storage? | fix | currency |
> |---|---|---|---|
> | **transposition** — must buffer every cell of a column before writing it contiguously | **yes** | spill each column to its own file in one streaming pass | disk I/O |
> | **the global sort** — the permutation needs every row of the parent column | **no** | external merge sort | disk passes |
>
> The second one is the important correction. **Even with perfectly
> column-major input, the sort would still need the whole column.** Sorting is
> globally dependent by nature, not by storage layout. So "row storage" is not
> the root cause — it only creates the first problem, which is the cheap one.
>
> **Consequence, and this reframes the whole thread:** neither problem is
> actually memory-bound once spilling is allowed. Aggarwal & Vitter's external
> merge sort sorts 50 GB in 1 GB of RAM. So the real trade is
> **memory vs disk passes**, *not* memory vs compression ratio — which is what
> `stream.py` currently trades, by chopping the table into independent blocks
> and giving up the global permutation to stay in RAM.
>
> Sketch of the out-of-core version, for the record: (1) stream the CSV once,
> spilling each column to its own file; (2) pick parents from statistics
> gathered in that pass; (3) external-sort row ids by the parent column;
> (4) apply the permutation to each column — this is the expensive step, a
> gather, and is exactly the distribution-sort variant in the Vitter survey;
> (5) feed the permuted columns into one continuous xz stream, so the
> dictionary spans everything and the chunking penalty is zero.

### Thought 7 — two ideas about the parent, both self-rejected

> Polypress row reordering works because of picking a parent column, right?
> So I have 2 ideas.
>
> **Idea 1:** create a new column that literally just stores the row numbers.
> Well actually, that wouldn't work because all the columns are moving
> differently.
>
> **Idea 2:** choose one number-encoded column that is closest to actual row
> numbers to be the parent, and then store deviations. But again every column
> would need a different parent. So never mind on that as well.

Both abandoned mid-thought. Recording the assessment because the two
rejections are not equally sound.

**Idea 1 — correctly rejected, and there is a measurement.** Storing explicit
row numbers is exactly TEST-1's `B_raw` variant, the literal reading of the
sort proposal. It was the **worst** of the four B variants: **1.29x** vs xz,
winning 6/76. LAW 1 says why — an explicit permutation costs `n*H(p)` bits,
which is the entire content of the column. His instinct matches the data.

**Idea 2 — rejected on a premise that is FALSE.** The stated blocker is "every
column would need a different parent". That is not a blocker; it is the
existing design. `pick_parents` returns a *per-column* parent and a
topological `order`, and `_encode_plan` permutes each column by *its own*
parent's argsort:

```python
for pos in order:
    par = parent.get(pos)
    if par is not None:
        perm = np.argsort(plan[par]["ids"], kind="stable")
        ids = ids[perm]
```

Every one of those permutations is free, because the decoder rebuilds the
parent first and recomputes the same stable argsort. That is what the tree and
the topological order exist for. So idea 2 is not blocked by what he thinks
blocks it.

**And there is something genuinely new left inside idea 2.** "The column
closest to actual row numbers" is a *different selection criterion* from the
one in use. `pick_parents` scores **conditional entropy** — how well a parent
predicts a child. It has no notion of whether a column is already monotonic
with the file's existing row order. A near-sorted column means the file
already arrives clustered by it, which is precisely the property
`probe_permfloor.py` found was worth 6x over the enumerative floor. Choosing a
parent for *monotonicity with row order* rather than for predictiveness is not
something this codec has ever tried.

### Thought 8 — fork xz so it stops rediscovering our patterns

> It's open source right? Why don't we create our own xz fork that works for
> us? Integrated with our pre-planning and organization, so firstly it doesn't
> rediscover our patterns that we made for it, and secondly so we can have
> more control over its sometimes stupidity.

> **ANSWER GIVEN (asked):** Argued against, and **TEST-4 then removed the
> technical premise entirely** — xz is not rediscovering anything cheaply
> replaceable; direct coding is 1.77x worse.
>
> The second motivation was already refuted by this repo's own data:
> `streams-probe.txt` — *"PER-STREAM ENTROPY TUNING IS WORTH NOTHING...
> 0.01–0.03% at every one of 18 points. Drop that theory."* The `lc`/`lp`/`pb`
> knobs exist and buy nothing.
>
> Three further reasons: a fork breaks invariant 1 on contact (Python's `lzma`
> binds to *system* liblzma, so byte-identity depends on both sides calling the
> same one); xz is expensive rather than stupid — optimal parsing genuinely
> finds near-optimal parses; and a fork means owning an entropy coder forever,
> which after CVE-2024-3094 is a claim that has to be defended to adopters.
>
> The salvageable version: **not a fork of xz, but a small LZ that only has to
> handle the alphabet stream** — a fraction of the bytes. Conditional on TEST-4
> saying the structured streams don't need LZ. **It said the opposite.**

### Thought 9 — "if I solve chunking, row indexing and xz, is it the best?"

> **ANSWER GIVEN (asked):** No. One of the three was already closed by TEST-4,
> and the other two **remove limitations rather than add wins** — a codec that
> handles 50 GB and one that handles 2 GB compress the same table to the same
> size.
>
> "Row indexing", clarified to mean *storing the permutation cheaply*, turned
> out to be **already solved**: it costs zero bytes, because the decoder
> recomputes the argsort from a parent it has already rebuilt. What is actually
> paid is not bytes but a **restriction** — only permutations that are argsorts
> of columns already stored, roughly one choice per column out of `n!`.

### Thought 10 — forget columns; number-encode row by row into one xz stream

> What if, instead of doing the columnar compression, we just forget that. If
> you dictionary encode to numbers, xz doesn't know the difference, just the
> numbers. So just go row by row, from left to right, and encode every
> non-number into a number, and just send that row to xz, then dict encode next
> row, send that to the same xz stream. You can chunk by row.

**Built and measured — TEST-5, TEST-6, TEST-7.** This is the most productive
idea in the log so far. It produced a working "fast tier" the codec did not
have, in one message.

- **1.28x–5.76x faster than `fast.encode`**, at 3.8–10.8 MB/s against its
  1.4–3.4. No parent search, no double encode, no O(columns²).
- **0.969x plain xz** with per-column dictionaries, the 50% gate and varint
  ids. Started at 1.08x before those two fixes.
- Single pass, O(1) memory, chunkable by row, one continuous xz stream —
  everything `stream.py` fails to deliver at once.
- Still ~1.4x behind `fast.encode` on size. That gap is the parent sort, which
  the scheme deliberately gives up.

**Follow-ups he proposed, all measured:** anchor differencing (TEST-6 —
helps 55% on clustered tables, hurts 37% on unclustered, must be a per-column
measured choice); one global dictionary instead of per-column (TEST-7 —
elegant, and costs 10%, because global ids get long).

**And he was right about the xz stream:** feeding it one row at a time costs
2 bytes in 104,380. The window persists; the dictionary spans the file.

*(open thread — thinking in progress)*

### Thought 11 — store the dictionary IN the table; build it while decoding

> Basically, if you number encode things, xz doesn't really know the
> difference. So number encoding row by row and feeding to xz vs. column by
> column should in theory not really matter, except for the fact that
> typically, you see the same or similar things in columns. And also,
> dictionary building is expensive. So what if you just store the dictionary
> IN the table itself? So the first instance of a new non-number item gets
> code = "item". You pass that into xz. Then when decoding, first you do the
> xz decompression, and as you go you build the dictionary from the table as
> you go and replace the number encoded values.

Three claims in one thought, worth keeping separate:

1. xz cannot tell an id from a number, so **layout should not matter in
   theory** — and the reason it does in practice is that a column repeats
   itself and a row does not.
2. **Dictionary building is expensive**, and it is a separate cost from the
   bytes it produces.
3. The dictionary need not be a block at all. **First occurrence emits the
   literal; every later occurrence emits its id; the decoder rebuilds the
   table and the dictionary in the same left-to-right pass.**

Asked for a test. → **TEST-8, run the same day. It works.**

- **Inline dictionary is 2.0% smaller than the out-of-band block** on the
  13-table corpus, 9 of 13 tables, at identical column kinds, gate and ids.
- **The gate does not need a pre-pass either.** A self-synchronising 50% rule
  the decoder re-derives from what it has already decoded gives **0.969x xz**
  in **one pass**, 3.3% under the two-pass out-of-band form.
- Wins on high-cardinality tables (`noaa_gsoy_sea` 0.934, `seattle_fire911`
  0.941), loses on low-cardinality ones (`cdc_nndss` 1.060) — contiguity again:
  a small dense alphabet compresses better collected than scattered.
- Claim 1 is the one the data contradicts: layout matters **up to 2.25x, in
  both directions** (`cdc_nndss` 39,420 column-major vs 88,553 row-major;
  `nyc_311` the other way).
- Claim 2 is not visible at this scale — inlining is free on time (1.04x), and
  dictionary building was already 8.2% of encode against xz's 73.5%.

Full record in `tests/TEST-8/`.


### Thought 12 — treat the CSV as a text file; pattern dictionary, then Huffman the dictionary

> What if we treat the csv as a text file? Forget rows and columns, just feed
> in the actual text stream. We could try some pattern dict encoding, and then
> huffman encode the dictionary? Idek.

And, as an example of the kind of adaptation wanted:

> Huffman but instead of individual characters it's whole words or something.

Asked for research agents and iteration, with a resource budget of 2 threads
and 3 GB, later clarified to include the agents themselves.

**Measured immediately — TEST-9, and it overturned the framing.** The idea's
premise (the text stream is worth attacking) is right; the proposed *mechanism*
is not where the money is.

- **PPMd order-16 on the raw CSV text is 0.897x of `xz -9e`, and 7.1x faster
  to compress** (6.8 s vs 48.6 s over 120 MB). No transform, no dictionary, no
  reordering. It wins on 11 of 13 tables.
- For scale: TEST-8's inline dictionary, a transform designed for this data,
  was 0.969x. **Changing the model class is worth three times as much as our
  best transform.**
- It loses on exactly the tables where LZ is the right tool — `cdc_nndss` and
  `seattle_fire911`, the most repetitive in the corpus, where whole lines
  recur and a (distance, length) pair beats any prediction.
- **The best order is table-dependent and the spread is large** (order 4 wins
  one table, order 6 another by 24%, order 16 the big ones). Cheap to choose
  per table by measuring, given PPMd runs at ~18 MB/s.

**The uncomfortable part, and the reason this is a finding rather than a
curiosity:** this project's 17 competitors are gzip, bzip2, xz, lz4, zstd x3,
brotli, Parquet x4, ORC x3, Feather x2 — **every one an LZ-family, Huffman or
block-sort codec. No context-mixing or PPM model has ever been in the set**,
and PPMd is not exotic; it ships inside 7-Zip. The 500-dataset headline is
therefore untested against the family that just beat plain xz by 10%.

Full record in `tests/TEST-9/`. Head-to-head against the shipping codec is
TEST-11, queued.

---

## Engineering candidates found in passing — not measured, not laws

Both fell out of answering questions rather than from a test. Recorded so they
are not lost, and flagged as unverified.

### C1 — composite (multi-key) sort parents are free and unimplemented

`pick_parents` returns `Dict[int, Optional[int]]`: exactly **one** parent per
column. `np.lexsort` appears nowhere in `fast.py`.

Sorting by `(A, then B)` costs **zero bytes** for the same reason sorting by
`A` does — the decoder holds both columns already. The single-parent search
structurally cannot see the case where `H(C|A)` and `H(C|B)` are both mediocre
while `H(C|A,B)` is near zero. That shape is common: `city` is poorly predicted
by `state` alone or `zip3` alone and nearly determined by the pair; survey data
is full of `(question_id, respondent_group)`.

Cost is **search, not storage**. Pairs are already O(columns²) — 38,220 on a
209-column survey; triples would be ~9M. Needs nomination (top-k single
parents, then try adding a second key), with the measured guard deciding, as
everywhere else in this codec.

### C2 — the argsort dtype leaves a ~45x speedup on the table

`classify` builds ids as `np.int64`. The narrow dtype is applied only when
*writing* the payload — `ids.astype(w).tobytes()` — which is **after** every
argsort. numpy's `kind="stable"` picks **radix sort** for 1–2 byte integers and
**timsort** otherwise, so the radix path is never taken even though most
columns have ≤256 distinct values.

Measured on synthetic arrays, permutation **bit-identical** either way (stable
is stable):

| distinct | rows | int64 | narrow | speedup |
|---|---|---|---|---|
| 200 | 2,000,000 | 112.0 ms | 2.4 ms | 45.8x |
| 5,000 | 2,000,000 | 181.6 ms | 4.6 ms | 39.7x |

Argsort runs in three hot places: `pick_parents` (the O(columns²) loop),
`_encode_plan`, and `decode`. **Not verified on the real encode path, and the C
encoder sorts separately.** Identical permutations mean the format is
untouched, so invariant 1 should be safe — but that is reasoning, not a test.

*(open thread — thinking in progress)*
