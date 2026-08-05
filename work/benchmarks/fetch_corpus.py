"""Fetch a corpus of real public datasets for benchmarking.

    python3 benchmarks/fetch_corpus.py corpus/
    python3 benchmarks/fetch_corpus.py corpus/ --list

Six curated datasets was never a claim, and ten synthetic adversarial ones
only prove where the codec breaks. This pulls real files, from real
government open-data portals, across deliberately different *shapes* --
because the thing that decides whether Polypress wins is shape, not subject:

  wide categorical   survey and administrative extracts, where the reordering
                     trick has parents to find. This is the codec's best case.
  narrow numeric     sensor and observation series, where the finite-difference
                     predictor works and the planar one may.
  mixed wide         incident and permit records: some codes, some free text,
                     some timestamps. The common real-world shape.
  text heavy         complaint and description fields, where there is little
                     to model and the result should be near parity.

Row limits are applied at the source where the portal supports it (Socrata
takes $limit), so nothing here needs the whole multi-gigabyte table. Sizes are
kept inside a 1-2 GB working budget: fast.py expands CSV roughly 8.5x into
Python strings, and bench.py holds an encoded and a decoded copy at once, so
its default ceiling is 80 MB of CSV.

Every entry records what it is expected to exercise. When a dataset behaves
unlike its shape predicts, that is the interesting result, not a mistake.
"""

from __future__ import annotations

import os
import sys
import urllib.request
import urllib.error

# (name, shape, url, note)
DATASETS = [
    ("nyc_collisions", "mixed wide",
     "https://data.cityofnewyork.us/resource/h9gi-nx95.csv?$limit=150000",
     "motor vehicle collisions: codes, streets, timestamps, lat/lon"),

    ("nyc_311", "text heavy",
     "https://data.cityofnewyork.us/resource/erm2-nwe9.csv?$limit=60000",
     "311 service requests: complaint text, agency codes, addresses"),

    ("seattle_fire911", "mixed wide",
     "https://data.seattle.gov/resource/kzjm-xkqj.csv?$limit=200000",
     "fire 911 calls: type text, address, lat/lon, datetime"),

    ("wa_ev_population", "wide categorical",
     "https://data.wa.gov/resource/f6w7-q2d2.csv?$limit=200000",
     "electric vehicle registrations -- the README's 1.73x dataset"),

    ("chicago_crimes", "wide categorical",
     "https://data.cityofchicago.org/resource/ijzp-q8t2.csv?$limit=150000",
     "crime incidents: heavy code hierarchies, strong column dependencies"),

    ("chicago_permits", "mixed wide",
     "https://data.cityofchicago.org/resource/ydr8-5enu.csv?$limit=80000",
     "building permits: many mostly-empty columns, classic skip-pattern shape"),

    ("austin_incidents", "mixed wide",
     "https://data.austintexas.gov/resource/fdj4-gpfu.csv?$limit=150000",
     "311 unified data"),

    # The USGS endpoint refuses any query matching more than 20,000 events,
    # so the magnitude floor is set to stay under it rather than the window
    # being widened.
    ("usgs_quakes", "narrow numeric",
     "https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv"
     "&starttime=2023-01-01&endtime=2023-12-31&minmagnitude=4.0",
     "earthquakes 2023: lat/lon/depth/magnitude, continuous numeric columns"),

    ("usgs_quakes_deep", "narrow numeric",
     "https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv"
     "&starttime=2021-01-01&endtime=2022-12-31&minmagnitude=4.5",
     "earthquakes 2021-22, higher floor: a second numeric shape"),

    ("noaa_gsoy_sea", "narrow numeric",
     "https://www.ncei.noaa.gov/data/global-summary-of-the-year/access/"
     "USW00024233.csv",
     "NOAA yearly climate summary, Seattle-Tacoma: many numeric columns"),

    ("noaa_gsoy_ord", "narrow numeric",
     "https://www.ncei.noaa.gov/data/global-summary-of-the-year/access/"
     "USW00094846.csv",
     "NOAA yearly climate summary, Chicago O'Hare"),

    ("nyc_baby_names", "wide categorical",
     "https://data.cityofnewyork.us/resource/25th-nujf.csv?$limit=60000",
     "small, highly repetitive: name/sex/ethnicity/rank"),

    ("cdc_nndss", "wide categorical",
     "https://data.cdc.gov/resource/x9gk-5huc.csv?$limit=150000",
     "notifiable disease surveillance: coded, many blanks"),
]


def fetch(entry, outdir: str) -> str:
    name, shape, url, note = entry
    dst = os.path.join(outdir, name + ".csv")
    if os.path.exists(dst) and os.path.getsize(dst) > 4096:
        return dst
    req = urllib.request.Request(url, headers={"User-Agent": "polypress-bench"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read()
    except Exception as exc:
        sys.stderr.write("  {:<20} FAILED: {}\n".format(name, exc))
        return ""
    if len(body) < 4096 or body[:1] == b"<":
        sys.stderr.write("  {:<20} FAILED: not a CSV body\n".format(name))
        return ""
    with open(dst, "wb") as fh:
        fh.write(body)
    return dst


def main(argv) -> int:
    if "--list" in argv:
        for name, shape, url, note in DATASETS:
            print("{:<20} {:<18} {}".format(name, shape, note))
        return 0
    outdir = argv[1] if len(argv) > 1 else "corpus"
    os.makedirs(outdir, exist_ok=True)
    got = []
    for entry in DATASETS:
        sys.stderr.write("fetching {}\n".format(entry[0]))
        p = fetch(entry, outdir)
        if p:
            got.append((entry, p))
    print("\n{:<20} {:<18} {:>12}  {}".format("name", "shape", "bytes", "note"))
    for (name, shape, _u, note), p in got:
        print("{:<20} {:<18} {:>12,}  {}".format(
            name, shape, os.path.getsize(p), note[:44]))
    print("\n{} of {} datasets, {:,} bytes total".format(
        len(got), len(DATASETS), sum(os.path.getsize(p) for _e, p in got)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
