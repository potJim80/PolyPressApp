"""Does the FD win scale with the fact-to-dimension ratio?

real_fd.py measured 1.21x, but with only 200k fact rows against 33k + 60k
dimension rows -- each dimension row shared by 3-6 fact rows. Real warehouse
fact tables are 100M-1B rows against dimensions that size, so the sharing
factor is 1000x+ and the fixed dimension cost amortizes to nothing.

The mechanism predicts the win GROWS with that ratio: FD-awareness deletes
19 per-row index arrays while the dimension tables stay a constant size.
This sweeps the fact size to test that prediction.

Uses zstd level 19 rather than 22 so the sweep is tractable; both sides get
the identical level, so the ratio is unaffected.
"""

from __future__ import annotations

import io
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

import real_fd as R

LEVEL = 19
SIZES = (200_000, 600_000, 1_800_000)


def psize(cols, sort_by=None) -> int:
    table = pa.table({k: pa.array(R.typed(v)) if isinstance(v[0], str)
                      else pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=LEVEL,
                   use_dictionary=True)
    return buf.getbuffer().nbytes


def main() -> None:
    geo = R.load_csv(os.path.join(R.SCRATCH, "geo-data.csv"), "zipcode")
    air = R.load_csv(os.path.join(R.SCRATCH, "airport-codes.csv"), "ident",
                     limit=60_000)
    dims = {"geo": (geo, "zipcode"), "air": (air, "ident")}

    # dimension side is built once -- it does not grow with the fact table
    dim_bytes = 0
    prepared = {}
    for label, (dim, key) in dims.items():
        perm = R.compressive_order(dim, key)
        pdim = R.permute(dim, perm)
        rank = {old: new for new, old in enumerate(perm)}
        reduced, subdims, _ = R.factor_dimension(pdim, key)
        size = psize({"{}_{}".format(label, n): v for n, v in reduced.items()})
        for sub in subdims:
            size += psize({"{}_{}".format(label, n): v for n, v in sub.items()})
        dim_bytes += size
        prepared[label] = (dim, key, rank)

    print("\nfixed dimension-side cost: {:,} B "
          "(does not grow with fact rows)".format(dim_bytes))
    print("\n   fact rows   share/dim   industry std      FD-aware      win")
    print("  " + "-" * 60)

    for n_rows in SIZES:
        rng = random.Random(5)
        fact = {
            "ts": sorted(rng.randrange(1_700_000_000_000, 1_700_900_000_000)
                         for _ in range(n_rows)),
            "amount": [round(rng.uniform(1, 999), 2) for _ in range(n_rows)],
            "channel": [rng.choice(["web", "app", "store", "phone"])
                        for _ in range(n_rows)],
        }
        picks = {label: [rng.randrange(len(d[k])) for _ in range(n_rows)]
                 for label, (d, k, _) in prepared.items()}

        full = dict(fact)
        for label, (dim, key, rank) in prepared.items():
            idx = picks[label]
            for name, values in dim.items():
                full["{}_{}".format(label, name)] = [values[i] for i in idx]

        industry = min(psize(full), psize(full, "air_ident"))

        fact_cols = dict(fact)
        for label, (dim, key, rank) in prepared.items():
            fact_cols["{}_id".format(label)] = [rank[i] for i in picks[label]]
        fd_total = dim_bytes + psize(fact_cols)

        share = n_rows / sum(len(d[k]) for d, k, _ in prepared.values())
        print("  {:>10,}   {:>8.1f}x   {:>12,}  {:>12,}   {:>5.2f}x".format(
            n_rows, share, industry, fd_total, industry / fd_total))

    print("\n  'share/dim' is fact rows per dimension row. Real warehouses "
          "run 1000x+.")


if __name__ == "__main__":
    main()
