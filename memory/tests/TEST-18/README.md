# TEST-18 — which column carries the gap to kanzi?

Run 2026-08-06. TEST-14 measured `kanzi -l9` beating Polypress on 6 of 13
tables, worst `austin_incidents` at **0.799**. LAW 3 says the tables we lose
are the ones where row reordering does not fire — a statement about tables.
This asks the sharper question: **which columns?**

Two views per column: **isolated** (the column alone as a one-column table) and
**ablated** (the whole table minus that column). Isolated shows raw modelling
strength; ablated is the honest one, because a column cheap on its own may be
the parent that makes five others cheap.

## Result — it is not one column, and it is not a numeric-column effect

`austin_incidents`, 19 columns, 40,000 rows. Whole table: polypress **572,320**,
kanzi **457,416**.

| column | distinct | alone: pp | alone: kanzi | k/p | gap closed if removed |
|---|---|---|---|---|---|
| ucr_code | 256 | 33,790 | 28,143 | 0.833 | **42.5%** |
| census_block_group | 684 | 53,006 | 46,483 | 0.877 | **38.6%** |
| occ_time | 1,440 | 52,680 | 46,487 | 0.882 | 22.1% |
| crime_type | 286 | 34,113 | 34,191 | 1.002 | 15.8% |
| rep_date_time | 36,319 | 63,389 | 47,755 | 0.753 | 15.0% |
| rep_time | 1,440 | 51,175 | 46,279 | 0.904 | 14.5% |
| occ_date_time | 30,170 | 83,543 | 59,515 | **0.712** | 12.8% |
| family_violence | **2** | 2,773 | 2,074 | **0.748** | 0.9% |
| clearance_status | 4 | 7,765 | 6,086 | 0.784 | 1.2% |
| category_description | 8 | 10,631 | 8,818 | 0.829 | 0.5% |
| incident_report_number | 40,000 | 60,151 | 55,279 | 0.919 | −6.4% |
| clearance_date | 510 | 26,008 | 21,883 | 0.841 | −17.4% |
| rep_date | 213 | 4,789 | 3,558 | 0.743 | −26.4% |
| occ_date | 652 | 19,593 | 14,962 | 0.764 | **−33.5%** |

**kanzi beats us on 17 of 19 columns in isolation**, and the columns it wins
hardest are categorical, not numeric.

## The two findings

**1. `family_violence` has two distinct values in 40,000 rows and a context
mixer beats us by 25% on it.** There is no reordering to do and no cross-column
trick to find — it is a binary flag, and we spend a quarter more bits than
necessary. Same on `clearance_status` (4 values, 0.784) and
`category_description` (8 values, 0.829).

That is **LAW 2's known exception, unacted upon**: TEST-2 found the 20 of 200
columns where ANS beat xz were blank-heavy or tiny-alphabet — skewed small
alphabets, where LZ has nothing to match. It was filed as a curiosity. It is
now visibly costing a table.

**2. The negative numbers are the codec working.** Removing `occ_date` makes
our position **33.5% worse**; `rep_date` 26.4%, `clearance_date` 17.4%. Those
are the reorder anchors — the columns whose sort order collapses everything
else. **This is LAW 4's mechanism measured directly**: a column is worth its
own bytes plus its value as a parent, and here that second term is larger than
the first.

## What it says about `austin_incidents` specifically

The table decomposes into two already-filed findings rather than anything new:

- an entropy-coding gap on small-alphabet id streams (LAW 2's exception);
- several *independent* functional dependencies against LAW 1's single shared
  row order — which is why TEST-17 gained 8.7% here and nowhere else, and why
  candidate C1 (composite `lexsort` parents) is the fix rather than FD
  stripping.

## Caveats

- One table fully ablated. `usgs_quakes` was ablated by the research pass with
  the same method and found `place`, a **free-text** column, carrying 59% of
  that table's gap — the skeleton/number backlog item, not a float problem.
- "Gap closed" is `1 − (gap without column) / (gap with)`, so it is a share of
  a difference and is unstable when the gap is small. Read the ranking, not the
  percentages.
- kanzi at `-l9 -b 16m`, single thread.
