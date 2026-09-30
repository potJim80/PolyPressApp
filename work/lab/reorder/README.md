# Reordering lab (2026-09-28)

`reorder.py` tests three completely different ways to order a table before xz —
a global lexicographic row sort, a greedy nearest-neighbour row chain, and a
per-column context sort — against PolyPress's own parent sort and no
reordering, all on one plain text back end so only the order differs.

Results and conclusions: `OUT/results/reorder-lab.md`. Short version: the
parent sort stays best; every stored permutation costs ~log2(rows) bits a row
and eats the gain; the only win is undoing a shuffled file.

    cd work && python3 lab/reorder/reorder.py ../IN/suite/*.csv --json out.json

**Code retired 2026-09-29** with the Python codec it was a harness around
(`fast.py`). The script is in git at commit `c55f68f`; the results stay in
`OUT/results/reorder-lab.md`.
