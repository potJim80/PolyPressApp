# Codec improvements lab (2026-09-28)

`improve.py` measures three changes to the shipping codec end to end, each with
a decoder: a measured global row sort, first-appearance dictionary ids, and
derived columns (numbers in text cells that another column already holds).
Results: `OUT/results/improve-lab.md`. Derived columns win (−9.0% over 14
tables, −20% on chicago_crimes); the other two do not earn a place.

**Code retired 2026-09-29** with the Python codec it was a harness around
(`fast.py`). The script is in git at commit `c55f68f`; the results stay in
`OUT/results/improve-lab.md`.
