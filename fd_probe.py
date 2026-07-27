"""Feasibility probe: can functional-dependency awareness beat Parquet + zstd?

The claim under test
--------------------
Parquet stores every column in its own chunk, so the compressor never sees two
columns in the same window. If column C is a *function* of column A, that
redundancy is structurally invisible to zstd at any level.

The obvious objection
---------------------
Sort by A and the dependent columns become long runs, which dictionary + RLE
crushes. If that recovers the win, the idea is dead.

So the probe uses a table with TWO independent correlation clusters. One sort
order can expose at most one of them. This is the realistic case: an event log
where rows carry both a location key and a device key.

Baselines are real pyarrow Parquet with zstd at max level, not a proxy.
"""

from __future__ import annotations

import io
import os
import random
import shutil
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

N_ROWS = 200_000
N_ZIP = 12_000
N_DEVICE = 6_000
ZSTD_LEVEL = 22

STATES = ["CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI"]
OSES = ["iOS 17.2", "iOS 16.6", "Android 14", "Android 13", "Windows 11",
        "macOS 14.2", "Linux"]
BROWSERS = ["Safari", "Chrome", "Firefox", "Edge", "Samsung Internet"]
EVENTS = ["view", "click", "scroll", "purchase", "add_to_cart", "search",
          "share", "login", "logout", "error"]


def build():
    rng = random.Random(42)

    # --- dimension 1: zip determines city, state, lat, lon -----------------
    zip_codes = rng.sample(range(10_000, 99_999), N_ZIP)
    zip_city, zip_state, zip_lat, zip_lon = {}, {}, {}, {}
    for z in zip_codes:
        zip_city[z] = "City_{}".format(rng.randrange(0, 8000))
        zip_state[z] = rng.choice(STATES)
        zip_lat[z] = round(rng.uniform(25.0, 49.0), 5)
        zip_lon[z] = round(rng.uniform(-124.0, -67.0), 5)

    # --- dimension 2: device determines os, browser, screen ----------------
    devices = ["dev_{:06x}".format(i) for i in range(N_DEVICE)]
    dev_os, dev_browser, dev_w, dev_h = {}, {}, {}, {}
    for d in devices:
        dev_os[d] = rng.choice(OSES)
        dev_browser[d] = rng.choice(BROWSERS)
        dev_w[d] = rng.choice([360, 375, 390, 414, 428, 768, 1080, 1440, 1920])
        dev_h[d] = rng.choice([640, 667, 736, 812, 844, 926, 1024, 1920, 2160])

    # --- fact rows, in timestamp order (the mandated order) ---------------
    ts, zips, devs, evts, durs = [], [], [], [], []
    clock = 1_700_000_000_000
    for _ in range(N_ROWS):
        clock += rng.randrange(1, 400)
        ts.append(clock)
        zips.append(rng.choice(zip_codes))
        devs.append(rng.choice(devices))
        evts.append(rng.choice(EVENTS))
        durs.append(rng.randrange(1, 30_000))

    full = {
        "ts": ts,
        "zip": zips,
        "city": [zip_city[z] for z in zips],
        "state": [zip_state[z] for z in zips],
        "lat": [zip_lat[z] for z in zips],
        "lon": [zip_lon[z] for z in zips],
        "device": devs,
        "os": [dev_os[d] for d in devs],
        "browser": [dev_browser[d] for d in devs],
        "screen_w": [dev_w[d] for d in devs],
        "screen_h": [dev_h[d] for d in devs],
        "event": evts,
        "duration_ms": durs,
    }

    zip_dim = {
        "zip": zip_codes,
        "city": [zip_city[z] for z in zip_codes],
        "state": [zip_state[z] for z in zip_codes],
        "lat": [zip_lat[z] for z in zip_codes],
        "lon": [zip_lon[z] for z in zip_codes],
    }
    dev_dim = {
        "device": devices,
        "os": [dev_os[d] for d in devices],
        "browser": [dev_browser[d] for d in devices],
        "screen_w": [dev_w[d] for d in devices],
        "screen_h": [dev_h[d] for d in devices],
    }
    return full, zip_dim, dev_dim


def parquet_size(cols, sort_by=None) -> int:
    table = pa.table({k: pa.array(v) for k, v in cols.items()})
    if sort_by:
        table = table.sort_by([(sort_by, "ascending")])
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd",
                   compression_level=ZSTD_LEVEL, use_dictionary=True)
    return buf.getbuffer().nbytes


def main() -> None:
    full, zip_dim, dev_dim = build()

    raw_csv = len(",".join(full.keys())) + 1
    for i in range(N_ROWS):
        raw_csv += sum(len(str(full[k][i])) for k in full) + len(full)

    base = parquet_size(full)
    by_zip = parquet_size(full, "zip")
    by_dev = parquet_size(full, "device")

    # normalized: fact table keeps only keys + independent measures
    fact = {k: full[k] for k in ("ts", "zip", "device", "event", "duration_ms")}
    norm = parquet_size(fact) + parquet_size(zip_dim) + parquet_size(dev_dim)
    norm_sorted = (parquet_size(fact, "zip")
                   + parquet_size(zip_dim) + parquet_size(dev_dim))

    results = [
        ("raw CSV (uncompressed)", raw_csv),
        ("Parquet+zstd22 (ts order)", base),
        ("Parquet+zstd22 sorted by zip", by_zip),
        ("Parquet+zstd22 sorted by device", by_dev),
        ("FD-normalized (ts order)", norm),
        ("FD-normalized + sorted fact", norm_sorted),
    ]
    best_industry = min(base, by_zip, by_dev)

    print("\n{:,} rows x {} columns".format(N_ROWS, len(full)))
    print("two functional-dependency clusters: zip->{city,state,lat,lon}, "
          "device->{os,browser,screen_w,screen_h}\n")
    for label, size in results:
        print("  {:<32} {:>10,} B   {:>6.2f}x vs CSV".format(
            label, size, raw_csv / size))

    print("\n  best industry-standard config : {:,} B".format(best_industry))
    print("  FD-aware best                : {:,} B".format(min(norm, norm_sorted)))
    print("  ratio                        : {:.2f}x smaller".format(
        best_industry / min(norm, norm_sorted)))


if __name__ == "__main__":
    main()
