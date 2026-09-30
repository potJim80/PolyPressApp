# Improving the codec — three ideas, measured end to end

2026-09-28. Script: `work/lab/improve/improve.py`; raw numbers `improve-lab.json`. Each idea wraps or patches the shipping `fast.encode`, has its own decoder, and is round-trip checked (14 tables x 3 ideas, all exact). Lab only: fast.py and the C port are unchanged.

| table | PolyPress now | A: measured sort | B: first-appearance ids | C: derived columns |
|---|--:|--:|--:|--:|
| m_nndss_2mb | 12,078 | 16,509 (+36.7%) | 11,689 (-3.2%) | 11,995 (-0.7%) |
| m_survey_demo | 172,570 | 259,200 (+50.2%) | 173,148 (+0.3%) | n/a |
| s_shuffled_cats | 231,565 | 249,176 (+7.6%) | 232,126 (+0.2%) | n/a |
| s_permits_1k | 126,304 | 128,088 (+1.4%) | 128,981 (+2.1%) | 120,242 (-4.8%) |
| m_ecommerce | 478,863 | 562,922 (+17.6%) | 495,148 (+3.4%) | n/a |
| m_quakes | 472,280 | 501,282 (+6.1%) | 501,806 (+6.3%) | n/a |
| m_weather_hourly | 494,456 | 1,066,621 (+115.7%) | 494,471 (+0.0%) | n/a |
| s_treasury_yields | 33,778 | 73,317 (+117.1%) | 33,793 (+0.0%) | n/a |
| m_financial_shuffled | 688,065 | 602,155 (-12.5%) | 690,285 (+0.3%) | n/a |
| m_baby_names | 55,062 | 67,386 (+22.4%) | 55,316 (+0.5%) | n/a |
| l_chicago_permits | 2,520,529 | 2,490,631 (-1.2%) | 2,604,756 (+3.3%) | 2,361,177 (-6.3%) |
| l_chicago_crimes | 2,010,153 | 2,277,048 (+13.3%) | 2,286,460 (+13.7%) | 1,602,560 (-20.3%) |
| l_nyc_311 | 1,134,489 | 1,176,109 (+3.7%) | 1,204,514 (+6.2%) | 987,601 (-12.9%) |
| l_seattle_fire911 | 1,252,915 | 2,340,417 (+86.8%) | 1,505,949 (+20.2%) | 1,102,056 (-12.0%) |

Shipped the way the codec would ship it — `min(current, idea)` per file (invariant 2):

| idea | total | vs now | tables it wins |
|---|--:|--:|--:|
| PolyPress now | 9,683,107 | — | — |
| A: measured sort | 9,567,299 | -1.20% | 2/14 |
| B: first-appearance ids | 9,682,718 | -0.00% | 1/14 |
| C: derived columns | 8,812,270 | -8.99% | 6/14 |
| best of all three | 8,726,054 | -9.88% | |

## What it says

1. **C (derived columns) is the one to build.** −20.3% on chicago_crimes, −13.0% nyc_311, −12.0% seattle_fire911, −6.3% chicago_permits; −9.0% across all 14 tables as shipped. It is also *faster* than the current encode (36 s vs 49 s in total) because it hands the codec less text. What it found: `location`/`report_location` rebuilt from `latitude`+`longitude` (exact, or rounded to 12 decimals), fee columns that copy other fee columns, `date` sharing its year with `year`, `taxi_pick_up_location` repeating `incident_zip`. Backlog item 2's ceiling was 15–21% on geometry tables; this reaches it.
2. **A (measured sort) is a niche safety net.** Wins twice — m_financial_shuffled −12.5%, chicago_permits −1.2% — and loses up to +117% elsewhere, so only as a try-and-keep. Worth −1.2% in total, for a full second encode on every table.
3. **B (first-appearance ids) does not carry over.** One win (nndss −3.2%), losses to +20%; −0.004% as shipped. Sorted alphabets stay: they are what front-coding feeds on. The +7.3% from sortreg was specific to that codec.

## Before C ships

- Port to `fast.py` **and** `csrc/ppz_encode.c`/`ppz_decode.c` in one commit (invariant 1); a new spec kind rather than a wrapper.
- Rounding is `Decimal` ROUND_HALF_EVEN; the C port must reproduce it exactly — the trap list's lesson about floats applies. Every reference is verified at encode, so a disagreement can only lose bytes, never data, but it would break byte identity.
- Choices are made per file (min of with/without). Per-column measurement (some sources are coincidences: `contact_1_name` ← `street_number`) is the obvious refinement.
- Only numeric tokens are handled. Concatenated text keys (`full_name` = first + last) are the other half of backlog item 2.
