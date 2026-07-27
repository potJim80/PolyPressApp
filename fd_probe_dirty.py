"""Probe 3: does the win survive dirty functional dependencies?

Probe 2 got 1.97x over the best Parquet config, but every FD was perfect.
Real denormalized warehouse tables violate their FDs constantly:

  - nulls and typos in the dependent attributes
  - slowly-changing dimensions: a customer's address legitimately CHANGES,
    so cust_id -> address is not a function over the table's whole history

An FD-aware codec must therefore store the dimension table PLUS an exception
list for every row that disagrees with it. Exceptions cost bits. At some
violation rate the exception list eats the entire win.

This probe finds that break-even rate. If real-world rates sit above it, the
idea is dead and nothing more should be spent on it.
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


def parquet_size(cols, sort_by=None) -> int:
    if not cols or not next(iter(cols.values())):
        return 0
    table = pa.table({k: pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd",
                   compression_level=ZSTD_LEVEL, use_dictionary=True)
    return buf.getbuffer().nbytes


def build_dims(rng):
    cust_ids = ["C{:08d}".format(i) for i in range(N_CUST)]
    prod_ids = ["P{:06d}".format(i) for i in range(N_PROD)]

    cust_attrs = {}
    for j in range(30):
        if j < 8:
            vals = {c: phrase(rng, 3) for c in cust_ids}
        elif j < 16:
            vals = {c: round(rng.uniform(0, 1e6), 3) for c in cust_ids}
        elif j < 24:
            pool = [phrase(rng, 2) for _ in range(600)]
            vals = {c: rng.choice(pool) for c in cust_ids}
        else:
            pool = [phrase(rng, 1) for _ in range(12)]
            vals = {c: rng.choice(pool) for c in cust_ids}
        cust_attrs["cust_attr_{:02d}".format(j)] = vals

    prod_attrs = {}
    for j in range(20):
        if j < 6:
            vals = {p: phrase(rng, 6) for p in prod_ids}
        elif j < 12:
            vals = {p: round(rng.uniform(0, 5000), 2) for p in prod_ids}
        else:
            pool = [phrase(rng, 2) for _ in range(80)]
            vals = {p: rng.choice(pool) for p in prod_ids}
        prod_attrs["prod_attr_{:02d}".format(j)] = vals

    return cust_ids, prod_ids, cust_attrs, prod_attrs


def run(violation_rate, cust_ids, prod_ids, cust_attrs, prod_attrs):
    """Build a table where `violation_rate` of cells break their FD."""
    rng = random.Random(1234)

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
    exceptions = {}          # column -> (row indices, replacement values)

    for keycol, attrs, keys in (("cust_id", cust_attrs, cust_col),
                                ("prod_id", prod_attrs, prod_col)):
        for name, mapping in attrs.items():
            column, ex_idx, ex_val = [], [], []
            for i, k in enumerate(keys):
                value = mapping[k]
                if rng.random() < violation_rate:
                    # an SCD update or a data-entry error: value diverges
                    if isinstance(value, float):
                        value = round(value * rng.uniform(0.5, 1.5), 3)
                    else:
                        value = phrase(rng, 3)
                    ex_idx.append(i)
                    ex_val.append(value)
                column.append(value)
            full[name] = column
            if ex_idx:
                exceptions[name] = (ex_idx, ex_val)

    cust_dim = {"cust_id": cust_ids}
    for name, mapping in cust_attrs.items():
        cust_dim[name] = [mapping[c] for c in cust_ids]
    prod_dim = {"prod_id": prod_ids}
    for name, mapping in prod_attrs.items():
        prod_dim[name] = [mapping[p] for p in prod_ids]

    industry = min(parquet_size(full),
                   parquet_size(full, "cust_id"),
                   parquet_size(full, "prod_id"))

    fd_total = (parquet_size(fact)
                + parquet_size(cust_dim)
                + parquet_size(prod_dim))
    ex_bytes = 0
    for name, (idx, val) in exceptions.items():
        # delta-code the sparse row indices, then let zstd have both arrays
        deltas = [idx[0]] + [idx[i] - idx[i - 1] for i in range(1, len(idx))]
        ex_bytes += parquet_size({"i": deltas, "v": [str(v) for v in val]})
    fd_total += ex_bytes

    n_ex = sum(len(v[0]) for v in exceptions.values())
    return industry, fd_total, ex_bytes, n_ex


def main() -> None:
    rng = random.Random(7)
    cust_ids, prod_ids, cust_attrs, prod_attrs = build_dims(rng)

    print("\n200,000 rows x 56 columns, 50 functionally dependent")
    print("varying the fraction of dependent cells that VIOLATE their FD")
    print("(SCD updates, typos, nulls)\n")
    print("  violation   industry std    FD-aware     exceptions   win")
    print("  " + "-" * 62)

    for rate in (0.0, 0.001, 0.01, 0.05, 0.10, 0.25, 0.50):
        industry, fd, ex_bytes, n_ex = run(
            rate, cust_ids, prod_ids, cust_attrs, prod_attrs)
        verdict = "DEAD" if industry / fd < 1.05 else ""
        print("  {:>7.1%}   {:>12,}  {:>11,}   {:>11,}   {:>5.2f}x {}".format(
            rate, industry, fd, ex_bytes, industry / fd, verdict))

    print("\n  'exceptions' is the byte cost of the divergence lists alone.")


if __name__ == "__main__":
    main()
