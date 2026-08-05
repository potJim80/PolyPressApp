"""Measure the CEILING of the cross-column redundancy idea, before building it.

    python3 benchmarks/probe_cross_column.py corpus100/*.csv --out results/cross-column.jsonl

The idea, backlog item 2 in CLAUDE.md: `chicago_permits` stores the same point
five times -- `xcoordinate`, `ycoordinate`, `latitude`, `longitude`, and
`location = "POINT (-87.62 41.89)"`. No general compressor can see through
that. A table codec could: store one copy and a recipe.

The parent search already handles one kind of redundancy -- a *categorical*
functional dependency (`zip` -> `city`) shows up as low conditional entropy and
gets a parent. So the only genuinely new ground is the redundancy the parent
search is blind to:

  * **embedding** -- one column's text contains another column's text, at a
    different format ("POINT (-87.62 41.89)" contains "-87.62")
  * **rounded embedding** -- the same, but at a different *precision*, which is
    what the real data actually does and what a plain substring test misses:
    `longitude` is `-87.67584459801843` while `location` is
    `POINT (-87.675844598018 42.018631130261)`, twelve decimals not fourteen
  * **affine** -- two numeric columns are the same quantity in different units
    or projections (state-plane feet vs degrees)
  * **duplicate** -- the columns are simply equal, cell for cell

What this measures, and what it deliberately does not
-----------------------------------------------------
It does **not** build a predictor. It finds derivable columns mechanically,
then encodes the table twice: whole, and with every derivable column
**deleted**. The difference is the **upper bound** on what a perfect
implementation of this idea could ever save on this table -- a real one still
has to store the recipe and every cell the recipe gets wrong, so it can only
do worse than this number.

That is the point. An upper bound is the cheap way to kill an idea. If the
ceiling is small, the idea is dead and no amount of clever implementation
rescues it. CLAUDE.md's standing rule is that entropy is a good nominator and a
bad decider; the same applies to intuition about redundancy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polypress import dtz, fast  # noqa: E402

SCREEN_ROWS = 60       # cheap first pass over every column pair
CONFIRM_ROWS = 400     # survivors are re-checked on more rows
MIN_HIT_RATE = 0.98    # a rule has to hold on nearly every row to count
MIN_SRC_LEN = 4        # a 1-3 char cell is inside everything by accident
MIN_SRC_DISTINCT = 8   # a near-constant column is not evidence of anything
MAX_COLS_PAIRWISE = 250


def sample_indices(n: int, k: int):
    if n <= k:
        return list(range(n))
    step = n / float(k)
    return sorted({int(i * step) for i in range(k)})


def as_float(cell: str):
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def embed_rate(dst, src, idx) -> float:
    """Fraction of sampled rows where src's cell sits inside dst's cell."""
    hits = seen = 0
    for i in idx:
        s = src[i]
        if len(s) < MIN_SRC_LEN:
            continue
        seen += 1
        if s in dst[i]:
            hits += 1
    if seen < max(8, len(idx) // 4):
        return 0.0
    return hits / float(seen)


NUM_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _tokens(cell: str):
    return NUM_TOKEN.findall(cell)


def _rounds_to(x: float, token: str) -> bool:
    """Is `token` the number x printed at the token's own precision?

    Tolerant of both rounding and truncation, because publishers do both, and
    this is an upper-bound probe -- a false positive here can only make the
    ceiling look *better* than it is, which is the safe direction for an
    argument that ends in "not worth building".
    """
    dot = token.find(".")
    d = len(token) - dot - 1 if dot >= 0 else 0
    try:
        t = float(token)
    except ValueError:
        return False
    return abs(t - x) <= 10.0 ** (-d) * 1.0000001


def embed_numeric_rate(dst, src, idx):
    """(rate, slot) for `dst` carrying `src`'s value at some fixed token slot.

    A plain substring test misses this: the same quantity is routinely
    republished at a different number of decimal places. The slot is pinned so
    that a cell full of numbers cannot match by accident on a different one
    each row.
    """
    votes = {}
    rows = []
    for i in idx:
        x = as_float(src[i])
        if x is None:
            continue
        toks = _tokens(dst[i])
        if not toks or len(toks) > 12:
            continue
        rows.append((x, toks))
        for k, t in enumerate(toks):
            if _rounds_to(x, t):
                votes[k] = votes.get(k, 0) + 1
    if not votes or len(rows) < max(8, len(idx) // 4):
        return 0.0, -1
    slot = max(votes, key=lambda k: votes[k])
    ok = sum(1 for x, toks in rows
             if slot < len(toks) and _rounds_to(x, toks[slot]))
    return ok / float(len(rows)), slot


def affine_rate(dst, src, idx) -> float:
    """Fraction of sampled rows fitting dst = a*src + b for one fixed a, b."""
    pts = []
    for i in idx:
        x, y = as_float(src[i]), as_float(dst[i])
        if x is None or y is None:
            continue
        pts.append((x, y))
    if len(pts) < max(8, len(idx) // 4):
        return 0.0
    xs = sorted(pts)
    lo, hi = xs[0], xs[-1]
    if hi[0] - lo[0] == 0:
        return 0.0
    a = (hi[1] - lo[1]) / (hi[0] - lo[0])
    b = lo[1] - a * lo[0]
    if a == 0:
        return 0.0
    scale = max(abs(p[1]) for p in pts) or 1.0
    tol = 1e-9 * scale
    ok = sum(1 for x, y in pts if abs(a * x + b - y) <= tol)
    return ok / float(len(pts))


def distinct_enough(col, idx) -> bool:
    return len({col[i] for i in idx}) >= MIN_SRC_DISTINCT


def find_derivable(table):
    """[{col, kind, source}] for every column another column can reconstruct.

    Only the *new* kinds are looked for. A categorical functional dependency is
    already exploited by pick_parents, so counting it here would credit this
    idea with a win the codec already banks.
    """
    nrow, ncol = table.shape
    if ncol < 2 or ncol > MAX_COLS_PAIRWISE or nrow < 20:
        return []
    cols = [table.column(j) for j in range(ncol)]
    screen = sample_indices(nrow, SCREEN_ROWS)
    confirm = sample_indices(nrow, CONFIRM_ROWS)

    usable_src = [distinct_enough(c, screen) for c in cols]
    numeric = [sum(1 for i in screen if as_float(c[i]) is not None)
               >= 0.9 * len(screen) for c in cols]

    found = []
    derived = set()
    for d in range(ncol):
        whole, parts = None, []
        for s in range(ncol):
            # A source that is itself being removed reconstructs nothing.
            # Without this, two columns that each derive the other are both
            # dropped and the "ceiling" counts bytes that are actually lost.
            if s == d or s in derived or not usable_src[s]:
                continue
            if cols[d] == cols[s]:
                whole = ("duplicate", s, -1)
                break
            if numeric[d] and numeric[s]:
                if affine_rate(cols[d], cols[s], screen) >= MIN_HIT_RATE \
                        and affine_rate(cols[d], cols[s], confirm) >= MIN_HIT_RATE:
                    whole = ("affine", s, -1)
                    break
            if embed_rate(cols[d], cols[s], screen) >= MIN_HIT_RATE \
                    and embed_rate(cols[d], cols[s], confirm) >= MIN_HIT_RATE:
                parts.append(("embed", s, -1))
                continue
            if numeric[s] and not numeric[d]:
                r, slot = embed_numeric_rate(cols[d], cols[s], screen)
                if r >= MIN_HIT_RATE:
                    r2, slot2 = embed_numeric_rate(cols[d], cols[s], confirm)
                    if r2 >= MIN_HIT_RATE and slot2 == slot \
                            and slot not in [p[2] for p in parts]:
                        parts.append(("embed_rounded", s, slot))
            if len(parts) >= 4:
                break
        for kind, s, slot in ([whole] if whole else parts):
            derived.add(d)
            found.append({"col": d, "name": table.columns[d],
                          "kind": kind, "source": s,
                          "source_name": table.columns[s], "slot": slot,
                          "whole": whole is not None})
    return found


def residual_table(table, found):
    """The table with only the redundant *part* of each column removed.

    Deleting a whole column would be the wrong measurement. A house number is
    derivable from a `street_number` column, but `"1234 N HALSTED ST"` still
    holds the street name -- delete the column and the "saving" is mostly
    information a real implementation would still have to store. So a column
    reconstructible in full (an exact duplicate, or an affine rescaling) is
    dropped, and a column that merely *contains* another is kept with the
    matched text cut out. What remains is a genuine upper bound: a perfect
    predictor also has to store its recipe, which this ignores.
    """
    ncol = len(table.columns)
    drop = {f["col"] for f in f_whole(found)}
    per_col = {}
    for f in found:
        if not f["whole"]:
            per_col.setdefault(f["col"], []).append(f)

    keep = [j for j in range(ncol) if j not in drop]
    rows = []
    for row in table.rows:
        out = []
        for j in keep:
            cell = row[j]
            for f in per_col.get(j, ()):
                src = row[f["source"]]
                if f["kind"] == "embed":
                    if len(src) >= MIN_SRC_LEN and src in cell:
                        cell = cell.replace(src, "", 1)
                else:
                    toks = _tokens(cell)
                    if f["slot"] < len(toks):
                        t = toks[f["slot"]]
                        x = as_float(src)
                        if x is not None and _rounds_to(x, t):
                            cell = cell.replace(t, "", 1)
            out.append(cell)
        rows.append(out)
    return dtz.Table([table.columns[j] for j in keep], rows)


def f_whole(found):
    return [f for f in found if f["whole"]]


def probe(path: str, max_bytes: int) -> dict:
    size = os.path.getsize(path)
    rec = {"file": os.path.basename(path), "bytes": size}
    table = dtz.read_any(path).normalise()
    if size > max_bytes:
        cut = int(len(table.rows) * max_bytes / float(size))
        table = dtz.Table(table.columns, table.rows[:max(20, cut)])
        rec["truncated_to_rows"] = len(table.rows)
    rec["rows"], rec["cols"] = table.shape

    t0 = time.time()
    found = find_derivable(table)
    rec["detect_s"] = round(time.time() - t0, 2)
    rec["derivable"] = found
    if not found:
        return rec

    # Only tables with a candidate pay for the two encodes.
    whole = len(fast.encode(table))
    without = len(fast.encode(residual_table(table, found)))
    rec["archive_bytes"] = whole
    rec["archive_without"] = without
    rec["ceiling_bytes"] = whole - without
    rec["ceiling_pct"] = round(100.0 * (whole - without) / whole, 2)
    rec["cols_dropped"] = len(f_whole(found))
    rec["cols_stripped"] = len({f["col"] for f in found if not f["whole"]})
    return rec


def summarise(path: str) -> int:
    """Print the finding from a JSONL the probe already wrote."""
    recs = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    ok = [r for r in recs if "error" not in r]
    hits = [r for r in ok if r.get("derivable")]
    if not hits:
        print("no derivable columns in {} datasets".format(len(ok)))
        return 0

    pcts = sorted(r["ceiling_pct"] for r in hits)
    tot_c = sum(r["ceiling_bytes"] for r in hits)
    tot_a = sum(r["archive_bytes"] for r in hits)
    mid = pcts[len(pcts) // 2]

    print("CROSS-COLUMN REDUNDANCY -- measured ceiling")
    print("=" * 62)
    print("datasets probed              : {}".format(len(ok)))
    print("with a derivable column      : {}".format(len(hits)))
    print("aggregate ceiling over those : {:,} of {:,} archive bytes"
          " = {:.2f}%".format(tot_c, tot_a, 100.0 * tot_c / tot_a))
    print("per-dataset ceiling, of those: min {:.2f}%  median {:.2f}%"
          "  max {:.2f}%".format(pcts[0], mid, pcts[-1]))
    print()
    print("Corpus-wide the share is strictly LOWER than the figure above: the")
    print("{} datasets with no derivable column gain nothing and still count"
          .format(len(ok) - len(hits)))
    print("in the denominator.")
    print()
    print("how many of the {} probed clear a bar".format(len(ok)))
    for bar in (1, 2, 5, 10, 20):
        n = sum(1 for x in pcts if x >= bar)
        print("   >= {:>2}%  {:>3} datasets  ({:.0f}% of the corpus)"
              .format(bar, n, 100.0 * n / len(ok)))

    print()
    print("largest ceilings")
    for r in sorted(hits, key=lambda r: -r["ceiling_pct"])[:10]:
        print("   {:>6.2f}%  {:>9,} B  {}".format(
            r["ceiling_pct"], r["ceiling_bytes"], r["file"][:50]))

    neg = [r for r in hits if r["ceiling_pct"] < 0]
    print()
    print("tables where removing the redundancy made the archive BIGGER: {}"
          .format(len(neg)))
    for r in sorted(neg, key=lambda r: r["ceiling_pct"])[:6]:
        rel = ", ".join("{} <- {}".format(f["name"], f["source_name"])
                        for f in r["derivable"][:2])
        print("   {:>6.2f}%  {:<44} {}".format(
            r["ceiling_pct"], r["file"][:44], rel[:60]))
    print()
    print("These are the reason a never-worse guard is mandatory rather than")
    print("optional: the relation is real but coding it costs more than it")
    print("saves, and only a measurement can tell which case you are in.")

    kinds = {}
    for r in hits:
        for f in r["derivable"]:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print()
    print("relations found, by kind")
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print("   {:>4}  {}".format(v, k))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="probe_cross_column")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--out", default=None, help="append JSONL here")
    ap.add_argument("--summary", default=None,
                    help="print the finding from an existing JSONL and exit")
    ap.add_argument("--max-mb", type=float, default=12.0,
                    help="truncate by rows above this, to bound peak memory")
    a = ap.parse_args(argv)
    if a.summary:
        return summarise(a.summary)
    max_bytes = int(a.max_mb * 1e6)

    done = set()
    if a.out and os.path.exists(a.out):
        with open(a.out) as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["file"])
                except Exception:
                    pass

    hits = tot_ceiling = tot_archive = 0
    paths = [p for p in sorted(a.paths) if os.path.basename(p) not in done]
    for i, path in enumerate(paths, 1):
        base = os.path.basename(path)
        print("[{:>3}/{}] {:<52} ".format(i, len(paths), base[:52]),
              end="", flush=True)
        try:
            rec = probe(path, max_bytes)
        except Exception as exc:
            rec = {"file": base, "error": "{}: {}".format(
                type(exc).__name__, str(exc)[:140])}
        if a.out:
            with open(a.out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        if "error" in rec:
            print("ERROR {}".format(rec["error"][:60]), flush=True)
        elif not rec.get("derivable"):
            print("-", flush=True)
        else:
            hits += 1
            tot_ceiling += rec["ceiling_bytes"]
            tot_archive += rec["archive_bytes"]
            kinds = ",".join(sorted({f["kind"] for f in rec["derivable"]}))
            print("{} derivable ({})  ceiling {:>9,} B  {:>6.2f}%".format(
                len(rec["derivable"]), kinds,
                rec["ceiling_bytes"], rec["ceiling_pct"]), flush=True)

    print("\n{} of {} tables have a derivable column".format(hits, len(paths)))
    if tot_archive:
        print("aggregate ceiling: {:,} of {:,} archive bytes = {:.2f}%"
              .format(tot_ceiling, tot_archive,
                      100.0 * tot_ceiling / tot_archive))
    return 0


if __name__ == "__main__":
    sys.exit(main())
