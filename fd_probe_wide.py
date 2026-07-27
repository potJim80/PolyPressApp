"""Probe 2: the wide denormalized table ("one big table").

Probe 1 showed FD-awareness wins only 1.13x on a narrow log table, because
Parquet's dictionary encoding already flattens low-cardinality dependents.
The recoverable redundancy per row is bounded by sum(log2 k_i) over dependent
columns -- so the prize scales with how many HIGH-cardinality dependents exist.

That is precisely the modern warehouse anti-pattern: tables deliberately
denormalized to 50-200 columns so queries need no joins. dbt, Snowflake and
BigQuery all encourage it. Those tables are mostly dependent columns.

This probe measures the prize in that regime against real Parquet + zstd 22.
"""

from __future__ import annotations

import io
import random

import pyarrow as pa
import pyarrow.parquet as pq

N_ROWS = 200_000
N_CUST = 40_000
N_PROD = 4_000
ZSTD_LEVEL = 22

WORDS = ["alpha", "bravo", "delta", "vertex", "nimbus", "quartz", "lumen",
         "cobalt", "ember", "harbor", "juniper", "onyx", "prism", "solace",
         "tundra", "vellum", "zephyr", "cinder", "marrow", "plinth"]


def phrase(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def build():
    rng = random.Random(7)

    # customer dimension: 30 dependent attributes, mixed cardinality
    cust_ids = ["C{:08d}".format(i) for i in range(N_CUST)]
    cust_attrs = {}
    for j in range(30):
        if j < 8:                                   # high-cardinality text
            vals = {c: phrase(rng, 3) for c in cust_ids}
        elif j < 16:                                # high-cardinality numeric
            vals = {c: round(rng.uniform(0, 1e6), 3) for c in cust_ids}
        elif j < 24:                                # medium cardinality
            pool = [phrase(rng, 2) for _ in range(600)]
            vals = {c: rng.choice(pool) for c in cust_ids}
        else:                                       # low cardinality
            pool = [phrase(rng, 1) for _ in range(12)]
            vals = {c: rng.choice(pool) for c in cust_ids}
        cust_attrs["cust_attr_{:02d}".format(j)] = vals

    # product dimension: 20 dependent attributes
    prod_ids = ["P{:06d}".format(i) for i in range(N_PROD)]
    prod_attrs = {}
    for j in range(20):
        if j < 6:
            vals = {p: phrase(rng, 6) for p in prod_ids}     # descriptions
        elif j < 12:
            vals = {p: round(rng.uniform(0, 5000), 2) for p in prod_ids}
        else:
            pool = [phrase(rng, 2) for _ in range(80)]
            vals = {p: rng.choice(pool) for p in prod_ids}
        prod_attrs["prod_attr_{:02d}".format(j)] = vals

    cust_col = [rng.choice(cust_ids) for _ in range(N_ROWS)]
    prod_col = [rng.choice(prod_ids) for _ in range(N_ROWS)]

    fact = {
        "ts": sorted(rng.randrange(1_700_000_000_000, 1_700_900_000_000)
                     for _ in range(N_ROWS)),
        "cust_id": cust_col,
        "prod_id": prod_col,
        "qty": [rng.randrange(1, 20) for _ in range(N_ROWS)],
        "price_paid": [round(rng.uniform(1, 999), 2) for _ in range(N_ROWS)],
        "channel": [rng.choice(["web", "app", "store", "phone"])
                    for _ in range(N_ROWS)],
    }

    full = dict(fact)
    for name, mapping in cust_attrs.items():
        full[name] = [mapping[c] for c in cust_col]
    for name, mapping in prod_attrs.items():
        full[name] = [mapping[p] for p in prod_col]

    cust_dim = {"cust_id": cust_ids}
    for name, mapping in cust_attrs.items():
        cust_dim[name] = [mapping[c] for c in cust_ids]
    prod_dim = {"prod_id": prod_ids}
    for name, mapping in prod_attrs.items():
        prod_dim[name] = [mapping[p] for p in prod_ids]

    return full, fact, cust_dim, prod_dim


def parquet_size(cols, sort_by=None) -> int:
    table = pa.table({k: pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd",
                   compression_level=ZSTD_LEVEL, use_dictionary=True)
    return buf.getbuffer().nbytes


def main() -> None:
    full, fact, cust_dim, prod_dim = build()

    raw_csv = len(",".join(full.keys())) + 1
    for i in range(N_ROWS):
        raw_csv += sum(len(str(full[k][i])) for k in full) + len(full)

    base = parquet_size(full)
    by_cust = parquet_size(full, "cust_id")
    by_prod = parquet_size(full, "prod_id")
    norm = (parquet_size(fact)
            + parquet_size(cust_dim) + parquet_size(prod_dim))
    norm_sorted = (parquet_size(fact, "cust_id")
                   + parquet_size(cust_dim) + parquet_size(prod_dim))

    rows = [
        ("raw CSV (uncompressed)", raw_csv),
        ("Parquet+zstd22 (ts order)", base),
        ("Parquet+zstd22 sorted by cust", by_cust),
        ("Parquet+zstd22 sorted by prod", by_prod),
        ("FD-normalized", norm),
        ("FD-normalized + sorted fact", norm_sorted),
    ]
    best_industry = min(base, by_cust, by_prod)
    best_fd = min(norm, norm_sorted)

    print("\n{:,} rows x {} columns  (50 of them functionally dependent)".format(
        N_ROWS, len(full)))
    print("cust_id -> 30 attributes,  prod_id -> 20 attributes\n")
    for label, size in rows:
        print("  {:<32} {:>12,} B  {:>7.2f}x vs CSV".format(
            label, size, raw_csv / size))

    print("\n  best industry-standard config : {:>12,} B".format(best_industry))
    print("  FD-aware best                : {:>12,} B".format(best_fd))
    print("  ==> {:.2f}x smaller than the best industry config".format(
        best_industry / best_fd))


if __name__ == "__main__":
    main()
