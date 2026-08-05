"""Fetch matrix-shaped tables -- the shape the planar predictor was built for.

    python3 benchmarks/fetch_matrix.py corpus/
    python3 benchmarks/fetch_matrix.py corpus/ --list

WHY THIS EXISTS
---------------
The 13-dataset corpus covers survey, administrative, incident and text-heavy
tables, and the planar (2D) predictor fires on almost none of them: 3 of 24
tables in the 2026-07-28 sweep, and every one of those by accident -- random
floats, a wide random table, a collisions file. Not one is the shape the
predictor exists for.

So the README's ~2x planar claim rests on data no benchmark in this repo
touches, and when the 2D grouping was finally measured end to end it came out
HARMFUL on two of the three tables it fired on. That is not evidence the
predictor is bad. It is evidence it has never been tested on its own case.

A matrix-shaped table is one where adjacent numeric columns are commensurable:
the same quantity, the same units, the same decimal places, similar magnitude,
and smoothly varying down the rows. A yield curve is the canonical example --
thirteen interest rates for thirteen maturities, quoted daily, each one close
to its neighbour in both directions. A weather station's hourly readings are
the other classic: a genuine sensor grid.

Both are small (a few MB) and need no credentials.

  treasury_yields  ~9,000 x 14   daily US Treasury par yield curve, 1990-2025.
                                 The README's 2.01x dataset, from source.
                                 Commensurable by construction: every column
                                 is a percentage rate to 2 decimals.
  weather_hourly  ~87,000 x 11   hourly reanalysis for one point, ten sensor
                                 channels over ten years. Mixed units, so only
                                 SOME neighbouring columns are commensurable --
                                 which is the more honest test of whether the
                                 grouping rule can tell them apart.
  weather_wide     ~26,000 x 25  hourly temperature at 24 European cities.
                                 Every column is the same quantity in the same
                                 units: the strongest possible planar case,
                                 and a check on whether the 8x magnitude rule
                                 and the adjacency rule get in the way.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import urllib.error
import urllib.request

TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/daily-treasury-rates.csv/{y}/all"
            "?type=daily_treasury_yield_curve&field_tdr_date_value={y}"
            "&page&_format=csv")

OPEN_METEO = ("https://archive-api.open-meteo.com/v1/archive"
              "?latitude={lat}&longitude={lon}"
              "&start_date={start}&end_date={end}&hourly={vars}&format=csv")

HOURLY_VARS = ("temperature_2m,relative_humidity_2m,dew_point_2m,"
               "apparent_temperature,pressure_msl,surface_pressure,"
               "cloud_cover,wind_speed_10m,wind_direction_10m,"
               "soil_temperature_0_to_7cm")

# 24 European cities, far enough apart to differ and close enough to correlate
CITIES = [
    ("berlin", 52.52, 13.41), ("hamburg", 53.55, 9.99),
    ("munich", 48.14, 11.58), ("cologne", 50.94, 6.96),
    ("frankfurt", 50.11, 8.68), ("stuttgart", 48.78, 9.18),
    ("vienna", 48.21, 16.37), ("prague", 50.08, 14.44),
    ("warsaw", 52.23, 21.01), ("budapest", 47.50, 19.04),
    ("amsterdam", 52.37, 4.90), ("brussels", 50.85, 4.35),
    ("paris", 48.86, 2.35), ("lyon", 45.76, 4.84),
    ("zurich", 47.38, 8.54), ("milan", 45.46, 9.19),
    ("rome", 41.90, 12.50), ("madrid", 40.42, -3.70),
    ("barcelona", 41.39, 2.17), ("lisbon", 38.72, -9.14),
    ("copenhagen", 55.68, 12.57), ("stockholm", 59.33, 18.07),
    ("oslo", 59.91, 10.75), ("helsinki", 60.17, 24.94),
]


def get(url: str, timeout: int = 120) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "polypress/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return fh.read().decode("utf-8")


def write_csv(path: str, header, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def fetch_treasury(dest: str) -> str:
    """Daily par yield curve, every year, joined on the maturities common
    to all of them.

    The set of quoted maturities changed over the period -- 1 Mo appeared in
    2001, 2 Mo in 2018, 4 Mo in 2022, and 30 Yr was discontinued 2002-2006 --
    so a union would be riddled with blanks, and a column with blanks is not
    numeric to this codec. The intersection keeps the table genuinely
    matrix-shaped, which is the property under test.
    """
    per_year = []
    common = None
    for y in range(1990, 2026):
        try:
            text = get(TREASURY.format(y=y))
        except (urllib.error.URLError, OSError) as exc:
            print("  {}: {}".format(y, exc), file=sys.stderr)
            continue
        rows = list(csv.reader(io.StringIO(text)))
        if len(rows) < 2:
            continue
        head = [h.strip() for h in rows[0]]
        per_year.append((head, rows[1:]))
        cols = set(head)
        common = cols if common is None else (common & cols)
        sys.stdout.write("\r  treasury {} ({} rows)   ".format(y, len(rows) - 1))
        sys.stdout.flush()
    print()
    if not per_year or not common:
        raise RuntimeError("no treasury data fetched")

    keep = [c for c in per_year[0][0] if c in common]
    out = []
    for head, rows in per_year:
        idx = [head.index(c) for c in keep]
        for r in rows:
            if len(r) < len(head):
                continue
            out.append([r[i].strip() for i in idx])

    # ascending by date, which is how anyone would actually hold this file
    def key(r):
        try:
            m, d, y = r[0].split("/")
            return (int(y), int(m), int(d))
        except Exception:
            return (0, 0, 0)

    out.sort(key=key)
    path = os.path.join(dest, "treasury_yields.csv")
    write_csv(path, keep, out)
    return path


def _meteo_rows(text: str):
    """Open-Meteo prefixes two metadata lines before the real header."""
    rows = list(csv.reader(io.StringIO(text)))
    for i, r in enumerate(rows):
        if r and r[0] == "time":
            return rows[i], rows[i + 1:]
    raise RuntimeError("unexpected Open-Meteo layout")


def fetch_weather_hourly(dest: str) -> str:
    text = get(OPEN_METEO.format(lat=52.52, lon=13.41, start="2015-01-01",
                                 end="2024-12-31", vars=HOURLY_VARS))
    head, rows = _meteo_rows(text)
    path = os.path.join(dest, "weather_hourly.csv")
    write_csv(path, head, rows)
    return path


def fetch_weather_wide(dest: str) -> str:
    """One quantity, 24 places, one column each. The purest planar case."""
    times = None
    cols = []
    for name, lat, lon in CITIES:
        text = get(OPEN_METEO.format(lat=lat, lon=lon, start="2022-01-01",
                                     end="2024-12-31", vars="temperature_2m"))
        head, rows = _meteo_rows(text)
        if times is None:
            times = [r[0] for r in rows]
        cols.append((name, [r[1] if len(r) > 1 else "" for r in rows]))
        sys.stdout.write("\r  weather_wide {}   ".format(name))
        sys.stdout.flush()
    print()
    n = min(len(times), min(len(c[1]) for c in cols))
    header = ["time"] + [c[0] for c in cols]
    out = [[times[i]] + [c[1][i] for c in cols] for i in range(n)]
    path = os.path.join(dest, "weather_wide.csv")
    write_csv(path, header, out)
    return path


JOBS = [
    ("treasury_yields", "matrix, dense", fetch_treasury,
     "daily par yield curve 1990-2025: every column a rate in percent"),
    ("weather_hourly", "matrix, mixed units", fetch_weather_hourly,
     "10 sensor channels, hourly, 10 years -- only some columns commensurable"),
    ("weather_wide", "matrix, pure", fetch_weather_wide,
     "hourly temperature at 24 cities: one quantity, one unit, 24 columns"),
]


def main(argv) -> int:
    if "--list" in argv:
        for name, shape, _fn, note in JOBS:
            print("{:18} {:22} {}".format(name, shape, note))
        return 0
    if not argv:
        print(__doc__.strip().split("\n")[2].strip(), file=sys.stderr)
        return 2
    dest = argv[0]
    os.makedirs(dest, exist_ok=True)
    for name, shape, fn, note in JOBS:
        target = os.path.join(dest, name + ".csv")
        if os.path.exists(target):
            print("{:18} already present".format(name))
            continue
        print("{:18} {}".format(name, note))
        try:
            path = fn(dest)
        except Exception as exc:
            print("  FAILED: {}".format(exc), file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as fh:
            nrows = sum(1 for _ in fh) - 1
        print("  -> {}  {:,} rows  {:,} B".format(
            os.path.basename(path), nrows, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
