"""Probe 4: how much of the FD prize lies OUTSIDE the published work?

Literature check (2026-07-27) found the core idea is taken:
  Corra   (Liu, Stoian, van Renen, Kipf, arXiv 2403.17229, 2024)
          -- hierarchical encoding = dimension array indexed by a reference
             column's dictionary codes, plus outlier lists. Manual/UDF-driven.
  C3      -- automates Corra, linear regression on numeric column PAIRS only.
  Virtual (Stoian et al., arXiv 2410.14066, 2024)
          -- automates discovery via K-regression + L0 sparsity, whole-file
             evaluation over 103 data.gov tables, best reported saving 40%.

Virtual states it handles NUMERIC columns exclusively -- linear regression
cannot encode a string column. So the open question is how much of the win
comes from high-cardinality STRING/categorical dependents.

This probe splits the dependent columns by type and measures the FD win in
each regime. It also reports Parquet+Snappy alongside Parquet+zstd22, because
Virtual benchmarks against Snappy and that inflates any reported gain.
"""

from __future__ import annotations

import io
import random

import pyarrow as pa
import pyarrow.parquet as pq

N_ROWS = 200_000
N_CUST = 40_000
WORDS = ["alpha", "bravo", "delta", "vertex", "nimbus", "quartz", "lumen",
         "cobalt", "ember", "harbor", "juniper", "onyx", "prism", "solace",
         "tundra", "vellum", "zephyr", "cinder", "marrow", "plinth"]


def phrase(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def psize(cols, compression, level=None, sort_by=None) -> int:
    table = pa.table({k: pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    kw = {"compression": compression, "use_dictionary": True}
    if level is not None:
        kw["compression_level"] = level
    pq.write_table(table, buf, **kw)
    return buf.getbuffer().nbytes


def make(kind, n_attrs=24):
    """Build a wide table whose dependent columns are all of one `kind`."""
    rng = random.Random(7)
    cust_ids = ["C{:08d}".format(i) for i in range(N_CUST)]

    attrs = {}
    for j in range(n_attrs):
        if kind == "numeric":
            vals = {c: round(rng.uniform(0, 1e6), 3) for c in cust_ids}
        elif kind == "string_high":
            vals = {c: phrase(rng, 3) for c in cust_ids}
        elif kind == "string_low":
            pool = [phrase(rng, 2) for _ in range(600)]
            vals = {c: rng.choice(pool) for c in cust_ids}
        else:
            raise ValueError(kind)
        attrs["attr_{:02d}".format(j)] = vals

    cust_col = [rng.choice(cust_ids) for _ in range(N_ROWS)]
    fact = {
        "ts": sorted(rng.randrange(1_700_000_000_000, 1_700_900_000_000)
                     for _ in range(N_ROWS)),
        "cust_id": cust_col,
        "qty": [rng.randrange(1, 20) for _ in range(N_ROWS)],
    }
    full = dict(fact)
    for name, mapping in attrs.items():
        full[name] = [mapping[c] for c in cust_col]

    dim = {"cust_id": cust_ids}
    for name, mapping in attrs.items():
        dim[name] = [mapping[c] for c in cust_ids]
    return full, fact, dim


def main() -> None:
    print("\n{:,} rows, 24 dependent columns of a single type".format(N_ROWS))
    print("all dependents are exact functions of cust_id (40,000 distinct)\n")
    header = ("  dependent type    Snappy base    zstd22 base    "
              "FD-aware   vs Snappy  vs zstd22")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for kind in ("numeric", "string_low", "string_high"):
        full, fact, dim = make(kind)
        snappy = min(psize(full, "snappy"), psize(full, "snappy", sort_by="cust_id"))
        zstd = min(psize(full, "zstd", 22),
                   psize(full, "zstd", 22, sort_by="cust_id"))
        fd = psize(fact, "zstd", 22) + psize(dim, "zstd", 22)
        print("  {:<16} {:>11,}    {:>11,}    {:>9,}   {:>7.2f}x   {:>7.2f}x".format(
            kind, snappy, zstd, fd, snappy / fd, zstd / fd))

    print("\n  'numeric' is the regime Virtual/C3 already cover.")
    print("  'string_*' is the regime they explicitly exclude.")
    print("  Note how much larger any win looks against Snappy than zstd22.")


if __name__ == "__main__":
    main()
