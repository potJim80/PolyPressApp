"""Improved FD codec, measured on REAL string functional dependencies.

Earlier probes used data I generated, so any tuning risked optimising against
my own generator's quirks. This uses two real public tables as dimensions:

  geo-data.csv       zipcode -> city, county, state, state_abbr, state_fips
                     (real cardinalities, and a real transitive chain:
                      state -> state_abbr -> state_fips are mutually 1:1)
  airport-codes.csv  ident -> name, type, municipality, iso_region, ...
                     (real high-cardinality names, real nulls)

A denormalized "one big table" event log is built by sampling both dimensions,
which is the shape warehouses actually store.

Four improvements over the naive normalize-and-write from fd_probe_wide.py:

  A  integer surrogate keys        fact stores a dim row index, not the string
  B  free dimension permutation    the surrogate numbering is MINE to choose,
                                   so the dim table's row order costs nothing
                                   and can be sorted for compressibility
  C  recursive FD factoring        apply FD detection *inside* the dimension
                                   table, collapsing transitive chains
                                   (listed as future work by Corra)
  D  fact ordering variants        timestamp order vs surrogate-key order

Baseline is the best of several real Parquet+zstd-22 configurations.
"""

from __future__ import annotations

import csv
import io
import os
import random
from typing import Dict, List, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-ideas-"
           "txt-compressor/8538d1a4-f39e-4ba7-9aaa-f726442dd7c1/scratchpad")
N_ROWS = 200_000
ZSTD = 22


# --------------------------------------------------------------- loading

def load_csv(path: str, key: str, limit: int = None):
    """Load a CSV into column arrays, deduplicated on `key`."""
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        names = [n for n in reader.fieldnames if n]
        seen = set()
        cols = {n: [] for n in names}
        for row in reader:
            k = row.get(key) or ""
            if not k or k in seen:
                continue
            seen.add(k)
            for n in names:
                cols[n].append(row.get(n) or "")
            if limit and len(seen) >= limit:
                break
    return cols


def typed(values: Sequence[str]):
    """Infer int/float/string so Parquet gets a fair shot at every column."""
    try:
        return [int(v) for v in values]
    except (ValueError, TypeError):
        pass
    try:
        out = [float(v) for v in values]
        return out
    except (ValueError, TypeError):
        return list(values)


def psize(cols: Dict[str, list], sort_by: str = None, level: int = ZSTD) -> int:
    if not cols:
        return 0
    table = pa.table({k: pa.array(typed(v)) if isinstance(v[0], str)
                      else pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=level,
                   use_dictionary=True)
    return buf.getbuffer().nbytes


# ----------------------------------------------------- FD detection (C)

def cardinality(col: Sequence) -> int:
    return len(set(col))


def determines(a: Sequence, b: Sequence) -> bool:
    """True if a -> b is an exact functional dependency."""
    mapping = {}
    for x, y in zip(a, b):
        prev = mapping.get(x, y)
        if prev != y:
            return False
        mapping[x] = y
    return True


def factor_dimension(dim: Dict[str, list], key: str):
    """Split `dim` into a reduced dimension plus sub-dimensions, by finding
    for each column the lowest-cardinality other column that determines it.

    This is exact-FD discovery by hash probe -- the operation TANE/HyFD do
    efficiently, and which regression-based approaches cannot express for
    string columns.
    """
    names = [n for n in dim if n != key]
    cards = {n: cardinality(dim[n]) for n in names}
    order = sorted(names, key=lambda n: cards[n])

    owner: Dict[str, str] = {}       # column -> its determinant
    for b in order:
        for a in order:
            if a == b or a in owner:
                continue             # never chain onto a moved column
            if cards[a] > cards[b]:
                continue
            if cards[a] == cards[b] and a > b:
                continue             # break 1:1 ties deterministically
            if determines(dim[a], dim[b]):
                owner[b] = a
                break

    reduced = {key: dim[key]}
    for n in names:
        if n not in owner:
            reduced[n] = dim[n]

    subdims = []
    by_determinant: Dict[str, List[str]] = {}
    for b, a in owner.items():
        by_determinant.setdefault(a, []).append(b)
    for a, deps in by_determinant.items():
        rows = {}
        for i, kv in enumerate(dim[a]):
            if kv not in rows:
                rows[kv] = [dim[d][i] for d in deps]
        sub = {a: list(rows.keys())}
        for j, d in enumerate(deps):
            sub[d] = [v[j] for v in rows.values()]
        subdims.append(sub)
    return reduced, subdims, owner


# ------------------------------------------- free dim permutation (B)

def compressive_order(dim: Dict[str, list], key: str) -> List[int]:
    """Row permutation for the dimension table.

    The surrogate key numbering is ours to assign, so the dimension's row
    order carries no information and is free to choose. Sorting by ascending
    cardinality turns the low-cardinality columns into long runs, and each
    subsequent column into runs within those.
    """
    names = [n for n in dim if n != key]
    names.sort(key=lambda n: cardinality(dim[n]))
    n_rows = len(dim[key])
    return sorted(range(n_rows), key=lambda i: tuple(dim[c][i] for c in names))


def permute(dim: Dict[str, list], perm: Sequence[int]) -> Dict[str, list]:
    return {k: [v[i] for i in perm] for k, v in dim.items()}


# ------------------------------------------------------------ pipeline

def build_fact(dims, n_rows: int, seed: int = 5):
    rng = random.Random(seed)
    fact = {
        "ts": sorted(rng.randrange(1_700_000_000_000, 1_700_900_000_000)
                     for _ in range(n_rows)),
        "amount": [round(rng.uniform(1, 999), 2) for _ in range(n_rows)],
        "channel": [rng.choice(["web", "app", "store", "phone"])
                    for _ in range(n_rows)],
    }
    picks = {}
    for label, (dim, key) in dims.items():
        n = len(dim[key])
        picks[label] = [rng.randrange(n) for _ in range(n_rows)]
    return fact, picks


def main() -> None:
    geo = load_csv(os.path.join(SCRATCH, "geo-data.csv"), "zipcode")
    air = load_csv(os.path.join(SCRATCH, "airport-codes.csv"), "ident",
                   limit=60_000)

    print("\nreal dimensions loaded")
    print("  geo-data     {:>7,} zipcodes  x {} columns".format(
        len(geo["zipcode"]), len(geo)))
    print("  airport      {:>7,} idents    x {} columns".format(
        len(air["ident"]), len(air)))

    dims = {"geo": (geo, "zipcode"), "air": (air, "ident")}
    fact, picks = build_fact(dims, N_ROWS)

    # ---- the denormalized table a warehouse would actually store ----
    full = dict(fact)
    for label, (dim, key) in dims.items():
        idx = picks[label]
        for name, values in dim.items():
            col = "{}_{}".format(label, name)
            full[col] = [values[i] for i in idx]

    n_dep = len(full) - len(fact)
    print("\ndenormalized table: {:,} rows x {} columns "
          "({} functionally dependent)".format(N_ROWS, len(full), n_dep))

    raw = sum(len(str(full[k][i])) + 1 for k in full for i in range(0, N_ROWS, 97))
    raw = raw * 97  # sampled estimate of CSV bytes

    base_plain = psize(full)
    base_geo = psize(full, "geo_zipcode")
    base_air = psize(full, "air_ident")
    industry = min(base_plain, base_geo, base_air)

    print("\nbaselines (real Parquet + zstd-22)")
    print("  unsorted                       {:>11,} B".format(base_plain))
    print("  sorted by geo_zipcode          {:>11,} B".format(base_geo))
    print("  sorted by air_ident            {:>11,} B".format(base_air))
    print("  best industry config           {:>11,} B".format(industry))

    # ---- naive FD-aware: normalize, string keys (fd_probe_wide.py) ----
    naive = 0
    naive_fact = dict(fact)
    for label, (dim, key) in dims.items():
        naive_fact["{}_{}".format(label, key)] = [
            dim[key][i] for i in picks[label]]
        naive += psize({("{}_{}".format(label, n)): v for n, v in dim.items()})
    naive += psize(naive_fact)

    # ---- improved: A + B + C, in both fact orderings (D) ----
    def improved(order_by=None):
        total = 0
        detail = []
        fact_cols = dict(fact)
        for label, (dim, key) in dims.items():
            perm = compressive_order(dim, key)                     # B
            pdim = permute(dim, perm)
            rank = {old: new for new, old in enumerate(perm)}
            fact_cols["{}_id".format(label)] = [rank[i]
                                                for i in picks[label]]  # A
            reduced, subdims, owner = factor_dimension(pdim, key)   # C
            size = psize({("{}_{}".format(label, n)): v
                          for n, v in reduced.items()})
            for sub in subdims:
                size += psize({("{}_{}".format(label, n)): v
                               for n, v in sub.items()})
            total += size
            detail.append((label, len(reduced) - 1, len(subdims), owner, size))
        total += psize(fact_cols, sort_by=order_by)
        return total, detail

    imp_ts, detail = improved()
    imp_key, _ = improved("geo_id")

    print("\nFD-aware encodings")
    print("  naive normalize (string keys)   {:>11,} B   {:>5.2f}x".format(
        naive, industry / naive))
    print("  improved, fact in ts order      {:>11,} B   {:>5.2f}x".format(
        imp_ts, industry / imp_ts))
    print("  improved, fact sorted by geo_id {:>11,} B   {:>5.2f}x".format(
        imp_key, industry / imp_key))

    best = min(imp_ts, imp_key)
    print("\n  gain from the improvements alone: {:.2f}x over naive".format(
        naive / best))
    print("  final vs best industry config   : {:.2f}x".format(industry / best))

    print("\n  transitive FDs found inside the real dimensions:")
    for label, n_kept, n_sub, owner, size in detail:
        print("    {:<6} {} columns kept, {} sub-dimension(s)".format(
            label, n_kept, n_sub))
        for dep, det in sorted(owner.items()):
            print("      {:<22} <- determined by {}".format(dep, det))


if __name__ == "__main__":
    main()
