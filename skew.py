"""The last legitimate improvement: realistic foreign-key distributions.

At warehouse scale 42% of the FD-aware side's per-row cost is the two foreign
key columns, and they are incompressible in the earlier tests only because I
sampled them uniformly at random. Real event logs are not uniform:

  skew       a few zip codes / airports account for most rows (power law)
  locality   consecutive events often share a location

Which side this favours is genuinely unclear a priori: skew makes all 19
dependent columns compressible for the baseline, but only 1 key column for
the FD-aware side. So it must be measured.

It also settles a design question. The surrogate numbering is ours to assign,
so the dimension permutation has two competing objectives:

  compressive order   sort the dimension so ITS columns compress
  popularity order    sort by descending frequency so hot keys get small codes

At scale the fact table dwarfs the dimension, so popularity order should win.
"""

from __future__ import annotations

import io
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

import real_fd as R

LEVEL = 19
N_ROWS = 600_000


def psize(cols, sort_by=None) -> int:
    table = pa.table({k: pa.array(R.typed(v)) if isinstance(v[0], str)
                      else pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=LEVEL,
                   use_dictionary=True)
    return buf.getbuffer().nbytes


def weights(n, rng, s=1.15):
    """Zipf-ish popularity assigned to dimension rows in random order."""
    w = [1.0 / ((i + 1) ** s) for i in range(n)]
    rng.shuffle(w)
    return w


def sample_keys(n_dim, n_rows, rng, mode, w):
    if mode == "uniform":
        return [rng.randrange(n_dim) for _ in range(n_rows)]
    picks = rng.choices(range(n_dim), weights=w, k=n_rows)
    if mode == "skew+locality":
        for i in range(1, n_rows):
            if rng.random() < 0.35:
                picks[i] = picks[i - 1]
    return picks


def popularity_order(n_dim, picks):
    counts = [0] * n_dim
    for p in picks:
        counts[p] += 1
    return sorted(range(n_dim), key=lambda i: -counts[i])


def main() -> None:
    geo = R.load_csv(os.path.join(R.SCRATCH, "geo-data.csv"), "zipcode")
    air = R.load_csv(os.path.join(R.SCRATCH, "airport-codes.csv"), "ident",
                     limit=60_000)
    dims = {"geo": (geo, "zipcode"), "air": (air, "ident")}

    rng0 = random.Random(11)
    pop_w = {label: weights(len(d[k]), rng0)
             for label, (d, k) in dims.items()}

    print("\n{:,} fact rows, real dimensions, zstd-{}".format(N_ROWS, LEVEL))
    header = ("  key distribution     dim order       industry      "
              "FD-aware      win")
    print("\n" + header)
    print("  " + "-" * (len(header) - 2))

    for mode in ("uniform", "skew", "skew+locality"):
        rng = random.Random(23)
        fact = {
            "ts": sorted(rng.randrange(1_700_000_000_000, 1_700_900_000_000)
                         for _ in range(N_ROWS)),
            "amount": [round(rng.uniform(1, 999), 2) for _ in range(N_ROWS)],
            "channel": [rng.choice(["web", "app", "store", "phone"])
                        for _ in range(N_ROWS)],
        }
        picks = {label: sample_keys(len(d[k]), N_ROWS, rng, mode, pop_w[label])
                 for label, (d, k) in dims.items()}

        full = dict(fact)
        for label, (dim, key) in dims.items():
            idx = picks[label]
            for name, values in dim.items():
                full["{}_{}".format(label, name)] = [values[i] for i in idx]
        industry = min(psize(full), psize(full, "air_ident"))

        for order_name in ("compressive", "popularity"):
            dim_bytes = 0
            fact_cols = dict(fact)
            for label, (dim, key) in dims.items():
                if order_name == "compressive":
                    perm = R.compressive_order(dim, key)
                else:
                    perm = popularity_order(len(dim[key]), picks[label])
                pdim = R.permute(dim, perm)
                rank = {old: new for new, old in enumerate(perm)}
                reduced, subdims, _ = R.factor_dimension(pdim, key)
                dim_bytes += psize({"{}_{}".format(label, n): v
                                    for n, v in reduced.items()})
                for sub in subdims:
                    dim_bytes += psize({"{}_{}".format(label, n): v
                                        for n, v in sub.items()})
                fact_cols["{}_id".format(label)] = [rank[i]
                                                    for i in picks[label]]
            fd_total = dim_bytes + psize(fact_cols)
            print("  {:<20} {:<14} {:>11,}  {:>11,}   {:>5.2f}x".format(
                mode if order_name == "compressive" else "",
                order_name, industry, fd_total, industry / fd_total))

    print("\n  'skew' is a power law over dimension rows; 'locality' repeats")
    print("  the previous row's key 35% of the time.")


if __name__ == "__main__":
    main()
