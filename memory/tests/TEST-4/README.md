# TEST-4 — is xz redoing work the model already did?

Run 2026-08-05. **Hypothesis refuted.** Recorded because a closed direction is
worth more than an open one, and this one looked obviously right.

## The hypothesis

Polypress sorts a column *specifically* to turn it into `3 3 3 3 3 ...`, then
hands the bytes to xz, which spends optimal-parsing effort finding runs the
encoder deliberately created and already knows the position of. If that is
waste, the structured streams could be coded directly — RLE plus an entropy
coder, one linear pass, no match finder — at similar size and far less work,
leaving xz only for the alphabet.

It would have meant the speed/ratio tradeoff was engineering, not physics.

## Result — 13 tables, 41.7 MB of binary payload

| table | bins raw | **xz** | rANS0 | rANS1 | RLE+rANS | best/xz |
|---|---|---|---|---|---|---|
| cdc_nndss | 3,000,077 | **86,547** | 1,225,615 | 557,969 | 701,656 | **6.45x** |
| seattle_fire911 | 1,800,000 | **672,225** | 1,723,171 | 1,564,449 | 1,723,133 | 2.33x |
| chicago_permits | 8,000,000 | **1,523,631** | 4,145,665 | 3,021,727 | 3,292,064 | 1.98x |
| nyc_311 | 3,830,920 | **971,573** | 2,402,413 | 1,781,540 | 1,741,487 | 1.79x |
| nyc_collisions | 6,392,410 | **1,657,924** | 3,642,629 | 2,928,537 | 3,254,229 | 1.77x |
| austin_incidents | 6,092,574 | **1,676,939** | 3,541,230 | 2,880,975 | 3,300,650 | 1.72x |
| chicago_crimes | 5,007,179 | **1,714,119** | 3,437,444 | 2,716,660 | 2,870,262 | 1.58x |
| wa_ev_population | 6,693,427 | **2,434,327** | 4,827,809 | 3,626,148 | 3,939,003 | 1.49x |
| nyc_baby_names | 208,080 | **47,287** | 125,806 | 70,337 | 69,728 | 1.47x |
| usgs_quakes_deep | 342,493 | **128,402** | 220,453 | 181,298 | 182,435 | 1.41x |
| usgs_quakes | 348,827 | **169,973** | 263,936 | 235,653 | 227,467 | 1.34x |
| noaa_gsoy_ord | 4,846 | **1,323** | 2,389 | 2,656 | 1,589 | 1.20x |
| noaa_gsoy_sea | 6,560 | **1,354** | 2,454 | 2,265 | 1,548 | 1.14x |
| **TOTAL** | **41,727,393** | **11,085,624** | 25,561,014 | 19,570,214 | 21,305,251 | **1.77x** |

**Direct coding is 1.77x worse, and not close to 1.00 on a single table.**

## Why the hypothesis was wrong

RLE sees only *adjacent identical bytes*. xz sees three things it structurally
cannot:

- **Cross-column matches.** The binary payload is every column concatenated,
  and a 64 MiB window spans all of them. Columns correlate with each other.
- **Runs of runs.** When a parent's pattern repeats, the child's whole run
  *sequence* repeats — a second-order structure RLE has no representation for.
- **Long-range repetition.** On `cdc_nndss` xz reaches **34.7x** on the binary
  payload alone (3,000,077 → 86,547). That is not run-length territory.

**The tell:** the worst case for direct coding is `cdc_nndss`, the table the
codec is *best* at. The more the model succeeds, the more long-range structure
it creates, and the more xz earns its keep. Exactly backwards.

## The timing kills the other half

xz spends **6.70s on the binary payload and 18.70s on the text pile** — bins
are **26%** of xz's time. Removing xz there entirely would win back a quarter
of a cost that is not the dominant one anyway. **The encoder's time is in the
text pile**, and above that, in the parent search and the double-encode guards.

## Honest limit of this probe

**This is a size probe, not a decoder-verified measurement.** `rans0` is
round-trip verified in TEST-2, but the RLE variant was never decoded, and none
of the direct schemes were charged for a container. So the direct side is
*undercharged* here — a real implementation could only be larger. Since the
result is a refutation, that bias is in the safe direction; if the direct
schemes had won, this would need rebuilding with decoders before it counted.

## What it closes

**"Replace or route around xz" is dead.** Third independent measurement
pointing the same way, after `stridexz` (1.44x worse) and `probe_sortreg`
(1.03x at best, never caught the codec). It also removes the main technical
argument for forking xz.

The speed gap must come from elsewhere: the O(columns²) parent search, the
double-encode guards, Python→C (turbo got 2.84x), and the argsort dtype
candidate in `THINKING-LOG.md`.
