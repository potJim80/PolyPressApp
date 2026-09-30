# Reordering lab — three new row/column orders vs PolyPress's parent sort

2026-09-28. Script: `work/lab/reorder/reorder.py`. Raw numbers: `reorder-lab.json`, `reorder-lab-split.json`.

Same back end for every method (cells as escaped text, column after column, one xz -9e stream), so only the ORDER differs. Stored-order methods pay for their permutation. **Every size is round-trip verified** (10 tables x 5 methods, all exact).

## Sizes (bytes)

| table | xz on file | PolyPress (full codec) | none | parent (PolyPress's order) | global sort | similarity chain | context sort |
|---|--:|--:|--:|--:|--:|--:|--:|
| m_nndss_2mb | 69,561 | 12,078 | 16,818 | **15,875** | 24,540 | 48,188 | 20,927 |
| m_survey_demo | 362,125 | 172,570 | 284,720 | **220,158** | 294,028 | 316,626 | 313,830 |
| s_shuffled_cats | 314,504 | 231,565 | **285,414** | 285,590 | 286,354 | 309,563 | 285,447 |
| s_permits_1k | 137,924 | 126,304 | 138,103 | 131,658 | 132,441 | **129,229** | 133,520 |
| m_ecommerce | 627,869 | 478,863 | 543,301 | **527,059** | 573,915 | 588,012 | 613,019 |
| m_quakes | 641,080 | 472,280 | 515,321 | **505,074** | 597,879 | 590,127 | 592,218 |
| m_weather_hourly | 1,078,572 | 494,456 | **767,953** | 767,981 | 1,319,533 | 1,287,671 | 1,271,224 |
| s_treasury_yields | 64,770 | 33,778 | **61,652** | 61,680 | 98,148 | 103,206 | 91,544 |
| m_financial_shuffled | 749,132 | 688,065 | 778,949 | 728,999 | **617,519** | 732,436 | 644,732 |
| m_baby_names | 105,705 | 55,062 | 68,518 | 67,002 | 78,934 | 102,762 | **66,829** |
| **total** | 4,151,242 | 2,765,021 | 3,460,749 | 3,311,076 | 4,023,291 | 4,207,820 | 4,033,290 |

## Change against no reordering (negative = smaller)

| table | parent | global | chain | context |
|---|--:|--:|--:|--:|
| m_nndss_2mb | -5.6% | +45.9% | +186.5% | +24.4% |
| m_survey_demo | -22.7% | +3.3% | +11.2% | +10.2% |
| s_shuffled_cats | +0.1% | +0.3% | +8.5% | +0.0% |
| s_permits_1k | -4.7% | -4.1% | -6.4% | -3.3% |
| m_ecommerce | -3.0% | +5.6% | +8.2% | +12.8% |
| m_quakes | -2.0% | +16.0% | +14.5% | +14.9% |
| m_weather_hourly | +0.0% | +71.8% | +67.7% | +65.5% |
| s_treasury_yields | +0.0% | +59.2% | +67.4% | +48.5% |
| m_financial_shuffled | -6.4% | -20.7% | -6.0% | -17.2% |
| m_baby_names | -2.2% | +15.2% | +50.0% | -2.5% |
| **total** | -4.3% | +16.3% | +21.6% | +16.5% |

## Stored orders: is the order bad, or just expensive?

`data` = the reordered cells alone, as if the permutation were free; `perm` = the stored permutation.

| table | rows | none | global data | global perm | chain data | chain perm |
|---|--:|--:|--:|--:|--:|--:|
| m_nndss_2mb | 14,618 | 16,818 | 23,011 (+37%) | 1,529 | 41,641 (+148%) | 6,547 |
| m_survey_demo | 40,000 | 284,720 | 214,711 (-25%) | 79,317 | 237,300 (-17%) | 79,326 |
| s_shuffled_cats | 40,000 | 285,414 | 207,006 (-27%) | 79,348 | 230,310 (-19%) | 79,253 |
| s_permits_1k | 1,059 | 138,103 | 130,843 (-5%) | 1,598 | 127,636 (-8%) | 1,593 |
| m_ecommerce | 27,584 | 543,301 | 520,609 (-4%) | 53,306 | 534,852 (-2%) | 53,160 |
| m_quakes | 16,190 | 515,321 | 568,207 (+10%) | 29,672 | 560,569 (+9%) | 29,558 |
| m_weather_hourly | 87,672 | 767,953 | 1,125,931 (+47%) | 193,602 | 1,110,684 (+45%) | 176,987 |
| s_treasury_yields | 9,006 | 61,652 | 84,605 (+37%) | 13,543 | 89,611 (+45%) | 13,595 |
| m_financial_shuffled | 40,175 | 778,949 | 537,818 (-31%) | 79,701 | 652,824 (-16%) | 79,612 |
| m_baby_names | 29,685 | 68,518 | 59,350 (-13%) | 19,584 | 72,859 (+6%) | 29,903 |

## What it says

1. **PolyPress's parent sort is still the best reordering here** — best or within 0.1% of best on 7 of 10, never more than 0.1% worse than doing nothing, -4.3% overall. None of the three new methods beats it in total; all three are *worse than no reordering* in total (+16% to +22%).
2. **A stored order costs log2(rows) bits per row, and it does not compress.** 40,000 rows → ~79 KB of permutation (15.9 bits/row; log2 40,000 = 15.3). On `m_survey_demo` and `s_shuffled_cats` the global sort is a genuinely better order (-25%, -27% on the data alone) and the permutation eats all of it. This is LAW 1 again: sorting moves information into the permutation, it does not remove it.
3. **Throwing away file order is expensive on its own.** Time-ordered tables (`weather_hourly`, `treasury_yields`, `quakes`) get 10-47% worse *even with a free permutation* — the file order was already the best order.
4. **The one real win is a shuffled file.** `m_financial_shuffled`: global sort 617,519 B beats the *full* PolyPress codec (688,065) by 10%. The file is a shuffled copy of `m_financial_sorted`, so the sort is undoing the shuffle and the permutation is the price of the shuffle. Tables exported in random or hash order would behave the same way.
5. **Context sort shows depth is the problem.** Sorting each column by *every* earlier column is free, yet worse than nothing on 7 of 10 (one by only 33 bytes): deep keys shatter the runs a single good key makes. PolyPress's parent sort is the same idea at depth 1 with a *measured* key.
6. **The chain is the worst of all**, even with a free permutation (`nndss` +148%): greedy nearest-neighbour steps look locally good and fragment the table globally.
7. **Reordering is not most of PolyPress's win.** On this back end the best reordering reaches 3.31 MB; the full codec reaches 2.77 MB. The remaining 16% is dictionary ids and numeric differences — the non-reordering tricks.

Worth building from this, if anything: a **measured** global sort (invariant 2 — keep it only when it wins end to end), for shuffled or hash-ordered exports. Not built.
