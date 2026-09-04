"""Build the standard benchmark suite: a fixed set of tables, small to large,
covering the shapes this codec wins on and the shapes it does not.

    python3 benchmarks/make_suite.py ../IN/suite            # build all of it
    python3 benchmarks/make_suite.py ../IN/suite --max-tier m   # skip the big
    python3 benchmarks/make_suite.py ../IN/suite --force     # rebuild in place

Why a fixed suite exists at all
-------------------------------
The 500-dataset Socrata sweep answers "does it win on a random government
table". It cannot answer "what shape is this codec good at", because every
dataset in it is the same genre and the sizes are whatever the portal served.
This suite is the other question, and it is built to be *read*: every file is
tagged with a tier and a list of forms, so a result can be grouped by shape
rather than averaged into one number that hides both the 3.7x and the 0.9x.

Three kinds of entry, and the distinction is load-bearing:

  real       a table someone else published, copied whole
  slice      the first N bytes of one of those, cut at a true row boundary
             (quotes tracked, so an embedded newline never splits a row)
  generated  deterministic synthetic data for a genre no file on disk covers

Slices are honest for comparison -- every codec is handed the identical file --
but they are not new evidence: four rungs of the `nndss` ladder are one table
measured four times, and the report must not count them as four wins. They are
in the suite to answer "does the ratio depend on size", which needs the same
table at several sizes and nothing else.

Everything generated is seeded from a constant, so the suite rebuilds byte for
byte on any machine with the same CPython. The manifest carries a sha256 per
file; if a number moves and the hash moved with it, the input changed, not the
codec.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IN = os.path.normpath(os.path.join(HERE, "..", "..", "IN"))

SEED = 20260828
TIERS = ["xs", "s", "m", "l"]
MB = 1_000_000


# ---------------------------------------------------------------------------
# raw CSV row iteration
#
# `csv.reader` would parse and `csv.writer` would re-quote, and re-quoting is
# not a copy -- CLAUDE.md has a whole section on how Python's writer differs
# from what is on disk. A slice must be the original bytes. So: walk the text
# and track whether we are inside a quoted field. A `""` escape toggles twice
# and lands back inside, which is exactly right.

def iter_raw_rows(text: str):
    start, inq, i, n = 0, False, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            inq = not inq
        elif c == "\n" and not inq:
            yield text[start:i + 1]
            start = i + 1
        i += 1
    if start < n:
        yield text[start:]


def slice_table(src: str, dst: str, budget: int, max_rows: int = 0) -> None:
    """First rows of `src` up to `budget` bytes, header always kept."""
    with open(src, "r", encoding="utf-8", errors="strict", newline="") as fh:
        text = fh.read(budget + 4 * MB)
    out, total, nrows = [], 0, 0
    for row in iter_raw_rows(text):
        if out and (total + len(row.encode()) > budget
                    or (max_rows and nrows > max_rows)):
            break
        out.append(row)
        total += len(row.encode())
        nrows += 1
    body = "".join(out)
    if not body.endswith("\n"):
        body += "\n"
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)


def copy_whole(src: str, dst: str) -> None:
    shutil.copyfile(src, dst)


# ---------------------------------------------------------------------------
# generators
#
# Each returns a list of rows (lists of str) plus a header. They are written
# with csv.writer because there is no original to preserve -- these files ARE
# the original.

def write_rows(dst: str, header, rows) -> None:
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _budget_rows(header, make_row, budget: int, rng):
    """Call make_row until the encoded CSV would pass `budget` bytes."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    rows, i = [], 0
    while buf.tell() < budget:
        r = make_row(i, rng)
        rows.append(r)
        w.writerow(r)
        i += 1
    return rows


def gen_weblog(dst: str, budget: int, seed: int) -> None:
    """HTTP access log. Monotone timestamps, a few high-cardinality text
    columns, a small categorical set, and a heavy-tailed byte count. The
    genre where the win, if any, comes from the user-agent column."""
    rng = random.Random(seed)
    header = ["ts", "ip", "method", "path", "status", "bytes", "referrer",
              "user_agent", "latency_ms"]
    agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
        "Gecko) Chrome/122.0.0.0 Safari/537.36",
        "curl/8.4.0",
        "Googlebot/2.1 (+http://www.google.com/bot.html)",
    ]
    paths = ["/", "/index.html", "/api/v1/search", "/api/v1/items",
             "/static/app.js", "/static/app.css", "/login", "/logout",
             "/health", "/api/v1/orders", "/img/logo.png", "/favicon.ico"]
    methods = ["GET"] * 9 + ["POST", "HEAD", "PUT"]
    statuses = [200] * 17 + [304, 404, 500]
    refs = ["-", "https://example.com/", "https://www.google.com/",
            "https://news.ycombinator.com/", "https://t.co/abc123"]
    t = [0]

    def row(i, r):
        t[0] += r.randint(0, 3)
        secs = t[0]
        ts = "2026-03-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
            1 + (secs // 86400) % 28, (secs // 3600) % 24,
            (secs // 60) % 60, secs % 60)
        ip = "{}.{}.{}.{}".format(r.choice([10, 172, 192, 203]),
                                  r.randint(0, 255), r.randint(0, 255),
                                  r.randint(1, 254))
        p = r.choice(paths)
        if p.startswith("/api"):
            p += "?q={}&page={}".format(r.randint(1, 9999), r.randint(1, 50))
        return [ts, ip, r.choice(methods), p, r.choice(statuses),
                int(r.lognormvariate(7.5, 1.4)), r.choice(refs),
                r.choice(agents), round(r.lognormvariate(3.2, 0.9), 1)]

    write_rows(dst, header, _budget_rows(header, row, budget, rng))


def gen_financial(dst_sorted: str, dst_shuffled: str, budget: int,
                  seed: int) -> None:
    """Daily OHLCV panel, written twice: sorted by (ticker, date) and in a
    shuffled order. Same rows, same bytes, different order -- the cleanest
    available probe of how much of this codec is the row reordering."""
    rng = random.Random(seed)
    header = ["date", "ticker", "open", "high", "low", "close", "adj_close",
              "volume", "exchange", "sector"]
    tickers = ["AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA", "JPM",
               "V", "PG", "UNH", "HD", "MA", "XOM", "JNJ", "WMT", "CVX", "LLY",
               "ABBV", "PFE", "KO", "PEP", "MRK", "AVGO", "COST"]
    sectors = {t: rng.choice(["Technology", "Financials", "Health Care",
                              "Consumer Staples", "Energy",
                              "Consumer Discretionary"]) for t in tickers}
    exch = {t: rng.choice(["NASDAQ", "NYSE"]) for t in tickers}
    price = {t: rng.uniform(20, 400) for t in tickers}

    rows, day = [], 0
    est = len(",".join(header)) + 1
    while est < budget:
        y, m, d = 2020 + day // 252, 1 + (day % 252) // 21, 1 + (day % 21)
        date = "{}-{:02d}-{:02d}".format(y, m, d)
        for t in tickers:
            p = price[t] * (1.0 + rng.gauss(0, 0.014))
            price[t] = p
            o = round(p * (1 + rng.gauss(0, 0.003)), 2)
            c = round(p, 2)
            hi = round(max(o, c) * (1 + abs(rng.gauss(0, 0.004))), 2)
            lo = round(min(o, c) * (1 - abs(rng.gauss(0, 0.004))), 2)
            adj = round(c * 0.98, 2)
            vol = int(rng.lognormvariate(15.5, 0.7))
            r = [date, t, o, hi, lo, c, adj, vol, exch[t], sectors[t]]
            rows.append(r)
            est += sum(len(str(x)) for x in r) + len(r)
        day += 1

    rows.sort(key=lambda r: (r[1], r[0]))
    write_rows(dst_sorted, header, rows)
    shuf = list(rows)
    random.Random(seed + 1).shuffle(shuf)
    write_rows(dst_shuffled, header, shuf)


def gen_iot(dst: str, budget: int, seed: int) -> None:
    """Sensor stream: many devices, regular cadence, smooth random-walk
    floats, and a dropout that leaves blank cells. The blanks are the point --
    an all-or-nothing numeric gate loses a whole column to four of them."""
    rng = random.Random(seed)
    header = ["device_id", "ts", "temp_c", "humidity_pct", "pressure_hpa",
              "battery_pct", "rssi_dbm", "status"]
    ndev = 40
    state = [{"t": rng.uniform(15, 27), "h": rng.uniform(30, 70),
              "p": rng.uniform(985, 1025), "b": rng.uniform(60, 100)}
             for _ in range(ndev)]
    rows, tick = [], 0
    est = len(",".join(header)) + 1
    while est < budget:
        for d in range(ndev):
            s = state[d]
            s["t"] += rng.gauss(0, 0.08)
            s["h"] += rng.gauss(0, 0.20)
            s["p"] += rng.gauss(0, 0.05)
            s["b"] = max(0.0, s["b"] - 0.0009)
            secs = tick * 300
            ts = "2026-05-{:02d} {:02d}:{:02d}:00".format(
                1 + (secs // 86400) % 28, (secs // 3600) % 24, (secs // 60) % 60)
            drop = rng.random() < 0.004
            r = ["dev-{:04d}".format(d), ts,
                 "" if drop else round(s["t"], 2),
                 "" if drop else round(s["h"], 1),
                 round(s["p"], 2), round(s["b"], 3),
                 rng.randint(-95, -40),
                 "OK" if not drop else rng.choice(["DROPOUT", "TIMEOUT"])]
            rows.append(r)
            est += sum(len(str(x)) for x in r) + len(r)
        tick += 1
    write_rows(dst, header, rows)


def gen_ecommerce(dst: str, budget: int, seed: int) -> None:
    """Transactions with two kinds of redundancy on purpose: `order_id` is
    four other columns pasted together, and `line_total` is qty x unit_price.
    Both are the patterns the cross-column probe measured as the largest
    unclaimed win in the backlog, so the suite should contain a case where
    they are known to be present."""
    rng = random.Random(seed)
    header = ["order_id", "order_ts", "customer_id", "country", "city", "sku",
              "category", "qty", "unit_price", "line_total", "currency",
              "payment_method"]
    countries = [("US", "USD", ["Chicago", "Austin", "Seattle", "Denver"]),
                 ("GB", "GBP", ["London", "Leeds", "Bristol"]),
                 ("DE", "EUR", ["Berlin", "Munich", "Hamburg"]),
                 ("JP", "JPY", ["Tokyo", "Osaka"]),
                 ("BR", "BRL", ["Sao Paulo", "Recife"])]
    cats = ["Electronics", "Home & Kitchen", "Apparel", "Grocery", "Toys",
            "Books", "Sports", "Beauty"]
    pay = ["card", "card", "card", "paypal", "applepay", "invoice"]

    def row(i, r):
        cc, cur, cities = r.choice(countries)
        cust = r.randint(100000, 999999)
        day = 1 + i % 28
        ts = "2026-02-{:02d}T{:02d}:{:02d}:{:02d}".format(
            day, r.randint(0, 23), r.randint(0, 59), r.randint(0, 59))
        seq = i % 10000
        qty = r.choice([1, 1, 1, 2, 2, 3, 4, 6, 12])
        price = round(r.lognormvariate(2.6, 0.8), 2)
        total = round(qty * price, 2)
        oid = "{}-2026{:02d}-{}-{:04d}".format(cc, day, cust, seq)
        return [oid, ts, "CUST{}".format(cust), cc, r.choice(cities),
                "SKU-{:07d}".format(r.randint(0, 250000)), r.choice(cats),
                qty, price, total, cur, r.choice(pay)]

    write_rows(dst, header, _budget_rows(header, row, budget, rng))


def gen_genomic(dst: str, budget: int, seed: int) -> None:
    """Variant calls: a sorted integer position within each chromosome, tiny
    alphabet columns, and floats at two very different precisions."""
    rng = random.Random(seed)
    header = ["chrom", "pos", "variant_id", "ref", "alt", "qual", "filter",
              "allele_count", "allele_freq", "gene", "consequence"]
    bases = ["A", "C", "G", "T"]
    genes = ["BRCA1", "BRCA2", "TP53", "EGFR", "KRAS", "APOE", "CFTR", "MYC",
             "PTEN", "RB1", "ALK", "BRAF", "NRAS", "PIK3CA", "."]
    cons = ["missense_variant", "synonymous_variant", "intron_variant",
            "upstream_gene_variant", "stop_gained", "frameshift_variant",
            "3_prime_UTR_variant", "splice_region_variant"]
    filt = ["PASS"] * 8 + ["LowQual", "q10", "SnpCluster"]
    chroms = [str(i) for i in range(1, 23)] + ["X", "Y", "MT"]
    ci, pos = [0], [10000]

    def row(i, r):
        if r.random() < 0.002 and ci[0] < len(chroms) - 1:
            ci[0] += 1
            pos[0] = 10000
        pos[0] += r.randint(1, 4000)
        ref = r.choice(bases)
        return [chroms[ci[0]], pos[0],
                "rs{}".format(r.randint(1000, 99999999)), ref,
                r.choice([b for b in bases if b != ref]),
                round(r.uniform(3.0, 3000.0), 1), r.choice(filt),
                r.randint(1, 5000), round(r.betavariate(0.4, 8), 6),
                r.choice(genes), r.choice(cons)]

    write_rows(dst, header, _budget_rows(header, row, budget, rng))


def gen_multilingual(dst: str, budget: int, seed: int) -> None:
    """Non-ASCII UTF-8 across six scripts. Every other file in the suite is
    effectively Latin-1; this one costs multi-byte characters in the text
    blob and in the dictionary alphabets."""
    rng = random.Random(seed)
    header = ["record_id", "name", "city", "country", "script", "note",
              "amount", "recorded_at"]
    data = [
        ("Latin", ["María González", "Søren Kierkegaard", "Zoë Bäcker",
                   "François Dupont"], ["Zürich", "Malmö", "Kraków"], "CH"),
        ("Cyrillic", ["Иван Петров", "Наталья Смирнова", "Дмитрий Волков"],
         ["Москва", "Новосибирск", "Киев"], "RU"),
        ("Greek", ["Γιώργος Παπαδόπουλος", "Ελένη Νικολάου"],
         ["Αθήνα", "Θεσσαλονίκη"], "GR"),
        ("Arabic", ["محمد عبد الله", "فاطمة الزهراء", "أحمد حسن"],
         ["القاهرة", "الرياض", "دبي"], "EG"),
        ("Han", ["王小明", "李美玲", "张伟", "陈建国"],
         ["北京", "上海", "深圳", "台北"], "CN"),
        ("Devanagari", ["राजेश कुमार", "प्रिया शर्मा"], ["मुंबई", "दिल्ली"], "IN"),
    ]
    notes = ["确认收货", "доставка выполнена", "تم التسليم", "livré",
             "पुष्टि हो गई", "παραδόθηκε", ""]

    def row(i, r):
        script, names, cities, cc = r.choice(data)
        return ["REC-{:08d}".format(i), r.choice(names), r.choice(cities), cc,
                script, r.choice(notes), round(r.lognormvariate(5, 1.1), 2),
                "2026-01-{:02d}".format(1 + i % 28)]

    write_rows(dst, header, _budget_rows(header, row, budget, rng))


# ---------------------------------------------------------------------------
# the suite
#
# forms:  categorical text geo numeric matrix scientific ids timestamps
#         sparse wide narrow hostile unicode redundant sorted shuffled survey
#
# `ladder` marks the four rungs of the same table at four sizes. They share a
# source, so they are not four independent results.

C = os.path.join(IN, "corpus")
H = os.path.join(IN, "corpus_hostile")
M_ = os.path.join(IN, "corpus_matrix")

SUITE = [
    # name, tier, how, arg, forms, why
    ("xs_yields_400.csv", "xs", "slice", (os.path.join(M_, "treasury_yields.csv"), 22_000),
     ["numeric", "matrix", "timestamps"],
     "the planar predictor's own case, small enough that container overhead shows"),
    ("xs_survey_200.csv", "xs", "slice", (os.path.join(IN, "polypress-demo.csv"), 24_000),
     ["survey", "categorical", "sparse"],
     "skip-pattern survey data at a size where a header costs real percent"),
    ("xs_quakes_300.csv", "xs", "slice", (os.path.join(C, "usgs_quakes.csv"), 60_000),
     ["scientific", "geo", "numeric"],
     "float-heavy scientific table, small"),
    ("xs_nndss_500.csv", "xs", "slice", (os.path.join(C, "cdc_nndss.csv"), 70_000),
     ["categorical", "sparse", "ladder"],
     "ladder rung 1 -- the codec's best genre at 70 KB"),
    ("xs_noaa_gsoy_ord.csv", "xs", "real", os.path.join(C, "noaa_gsoy_ord.csv"),
     ["wide", "scientific", "sparse"],
     "102 columns, 70 rows: a real table that is wider than it is tall"),
    ("xs_wide_row.csv", "xs", "real", os.path.join(H, "single_wide_row.csv"),
     ["wide", "hostile"],
     "5,000 columns, one row -- the shape that aborted a sweep on ORC's RSS"),

    ("s_treasury_yields.csv", "s", "real", os.path.join(M_, "treasury_yields.csv"),
     ["numeric", "matrix", "timestamps"],
     "whole yield curve; the 1.86x matrix result came from this file"),
    ("s_permits_1k.csv", "s", "slice", (os.path.join(C, "chicago_permits.csv"), 950_000),
     ["text", "wide", "sparse", "geo"],
     "116 columns of which 8 are free text: the known worst case, small"),
    ("s_anticorrelated.csv", "s", "real", os.path.join(H, "anticorrelated.csv"),
     ["narrow", "hostile"],
     "two columns whose orders fight: sorting one scrambles the other"),
    ("s_high_precision.csv", "s", "real", os.path.join(H, "high_precision.csv"),
     ["numeric", "narrow", "hostile"],
     "floats with more digits than a delta can exploit"),
    ("s_mixed_types.csv", "s", "real", os.path.join(H, "mixed_types.csv"),
     ["narrow", "hostile", "sparse"],
     "one poisoned column: the all-or-nothing gate's test case"),
    ("s_shuffled_cats.csv", "s", "real", os.path.join(H, "shuffled_cats.csv"),
     ["categorical", "shuffled", "hostile"],
     "categorical columns in an order chosen to defeat reordering"),
    ("s_weblog.csv", "s", "gen", ("weblog", 600_000),
     ["text", "timestamps", "categorical", "ids"],
     "server log: no government portal genre covers it"),
    ("s_multilingual.csv", "s", "gen", ("multilingual", 550_000),
     ["unicode", "text", "categorical"],
     "six scripts of UTF-8; everything else in the suite is effectively Latin-1"),

    ("m_baby_names.csv", "m", "real", os.path.join(C, "nyc_baby_names.csv"),
     ["categorical", "numeric", "narrow"],
     "six columns, heavily repeated values, sorted by year"),
    ("m_quakes.csv", "m", "real", os.path.join(C, "usgs_quakes.csv"),
     ["scientific", "geo", "numeric", "timestamps"],
     "the full USGS extract: 22 columns of mostly floats"),
    ("m_weather_wide.csv", "m", "real", os.path.join(M_, "weather_wide.csv"),
     ["numeric", "matrix", "timestamps"],
     "25 commensurable temperature columns -- the 2D group's home ground"),
    ("m_weather_hourly.csv", "m", "real", os.path.join(M_, "weather_hourly.csv"),
     ["numeric", "matrix", "timestamps"],
     "hourly grid where one -0.0 once split the matrix in two"),
    ("m_survey_demo.csv", "m", "real", os.path.join(IN, "polypress-demo.csv"),
     ["survey", "categorical", "sparse"],
     "NEMSIS-shaped survey: skip patterns blank whole branches"),
    ("m_nndss_2mb.csv", "m", "slice", (os.path.join(C, "cdc_nndss.csv"), 2 * MB),
     ["categorical", "sparse", "ladder"],
     "ladder rung 2"),
    ("m_financial_sorted.csv", "m", "gen", ("financial_sorted", 3 * MB),
     ["numeric", "timestamps", "categorical", "sorted"],
     "OHLCV panel in its natural (ticker, date) order"),
    ("m_financial_shuffled.csv", "m", "gen", ("financial_shuffled", 3 * MB),
     ["numeric", "timestamps", "categorical", "shuffled"],
     "the same rows shuffled: the difference is what row order is worth"),
    ("m_ecommerce.csv", "m", "gen", ("ecommerce", 3 * MB),
     ["ids", "redundant", "categorical", "numeric"],
     "order_id is four columns pasted together and line_total = qty x price"),
    ("m_genomic.csv", "m", "gen", ("genomic", 2_500_000),
     ["scientific", "categorical", "numeric", "sorted"],
     "variant calls: tiny alphabets, a sorted integer, two float precisions"),
    ("m_uuid_keys.csv", "m", "real", os.path.join(H, "uuid_keys.csv"),
     ["ids", "narrow", "hostile"],
     "random UUIDs: nothing to model, and the codec must not make it worse"),
    ("m_random_floats.csv", "m", "real", os.path.join(H, "random_floats.csv"),
     ["numeric", "hostile"],
     "incompressible floats"),
    ("m_random_text.csv", "m", "real", os.path.join(H, "random_text.csv"),
     ["text", "hostile"],
     "incompressible text"),
    ("m_base64_blob.csv", "m", "real", os.path.join(H, "base64_blob.csv"),
     ["text", "narrow", "hostile"],
     "one column of base64: a single opaque blob per row"),
    ("m_wide_random.csv", "m", "real", os.path.join(H, "wide_random.csv"),
     ["wide", "numeric", "hostile"],
     "200 unrelated numeric columns: wide but with no 2D structure"),

    ("l_nndss_8mb.csv", "l", "slice", (os.path.join(C, "cdc_nndss.csv"), 8 * MB),
     ["categorical", "sparse", "ladder"],
     "ladder rung 3"),
    ("l_nndss_full.csv", "l", "real", os.path.join(C, "cdc_nndss.csv"),
     ["categorical", "sparse", "ladder"],
     "ladder rung 4 -- the 3.70x headline table, whole"),
    ("l_chicago_permits.csv", "l", "slice", (os.path.join(C, "chicago_permits.csv"), 28 * MB),
     ["text", "wide", "geo", "sparse"],
     "the worst real result on record (1.12x): 71% of the output is text blob"),
    ("l_nyc_311.csv", "l", "slice", (os.path.join(C, "nyc_311.csv"), 28 * MB),
     ["categorical", "text", "geo", "timestamps"],
     "44 mixed columns; the most-used open dataset in the world"),
    ("l_chicago_crimes.csv", "l", "slice", (os.path.join(C, "chicago_crimes.csv"), 28 * MB),
     ["categorical", "geo", "timestamps", "ids"],
     "coded administrative data with coordinates repeated as text"),
    ("l_wa_ev_population.csv", "l", "slice", (os.path.join(C, "wa_ev_population.csv"), 28 * MB),
     ["categorical", "ids", "geo"],
     "VIN prefixes and vehicle codes: high-cardinality identifiers"),
    ("l_seattle_fire911.csv", "l", "slice", (os.path.join(C, "seattle_fire911.csv"), 28 * MB),
     ["geo", "text", "timestamps", "narrow"],
     "seven columns storing the same point three times"),
    ("l_nyc_collisions.csv", "l", "slice", (os.path.join(C, "nyc_collisions.csv"), 28 * MB),
     ["geo", "categorical", "text", "redundant"],
     "lat, lon and 'POINT (lon lat)' side by side -- known cross-column case"),
    ("l_iot_sensor.csv", "l", "gen", ("iot", 10 * MB),
     ["numeric", "timestamps", "ids", "sparse"],
     "40 devices at a fixed cadence with dropouts that leave blank cells"),
    ("l_weblog_big.csv", "l", "gen", ("weblog_big", 10 * MB),
     ["text", "timestamps", "categorical", "ids"],
     "the log genre at a size where the text blob dominates"),
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def shape(path: str):
    with open(path, "r", encoding="utf-8", errors="strict", newline="") as fh:
        text = fh.read()
    rows = sum(1 for _ in iter_raw_rows(text))
    header = next(iter_raw_rows(text))
    cols = len(next(csv.reader(io.StringIO(header))))
    return max(0, rows - 1), cols


def build(outdir: str, max_tier: str, force: bool) -> int:
    os.makedirs(outdir, exist_ok=True)
    limit = TIERS.index(max_tier)
    missing_src, manifest = [], []
    fin = {}                        # the financial pair is built once for two

    for name, tier, how, arg, forms, why in SUITE:
        if TIERS.index(tier) > limit:
            continue
        dst = os.path.join(outdir, name)
        if os.path.exists(dst) and not force:
            pass
        elif how == "real":
            if not os.path.exists(arg):
                missing_src.append((name, arg))
                continue
            copy_whole(arg, dst)
        elif how == "slice":
            src, budget = arg
            if not os.path.exists(src):
                missing_src.append((name, src))
                continue
            slice_table(src, dst, budget)
        elif how == "gen":
            kind, budget = arg
            if kind == "weblog":
                gen_weblog(dst, budget, SEED)
            elif kind == "weblog_big":
                gen_weblog(dst, budget, SEED + 7)
            elif kind == "multilingual":
                gen_multilingual(dst, budget, SEED + 2)
            elif kind == "ecommerce":
                gen_ecommerce(dst, budget, SEED + 3)
            elif kind == "genomic":
                gen_genomic(dst, budget, SEED + 4)
            elif kind == "iot":
                gen_iot(dst, budget, SEED + 5)
            elif kind.startswith("financial"):
                if not fin:
                    a = os.path.join(outdir, "m_financial_sorted.csv")
                    b = os.path.join(outdir, "m_financial_shuffled.csv")
                    gen_financial(a, b, budget, SEED + 6)
                    fin["done"] = True
            else:
                raise SystemExit("unknown generator " + kind)

        if not os.path.exists(dst):
            missing_src.append((name, "not built"))
            continue
        nrows, ncols = shape(dst)
        manifest.append({
            "name": name, "tier": tier, "origin": how, "forms": forms,
            "why": why, "bytes": os.path.getsize(dst),
            "rows": nrows, "cols": ncols, "sha256": sha256(dst),
            "source": (arg if isinstance(arg, str)
                       else (os.path.basename(arg[0])
                             if how == "slice" else "generated:" + str(arg[0]))),
        })
        print("  {:<28} {:>4}  {:>12,} B  {:>7,} x {:<5} {}".format(
            name, tier, manifest[-1]["bytes"], nrows, ncols,
            ",".join(forms)), flush=True)

    mpath = os.path.join(outdir, "MANIFEST.json")
    with open(mpath, "w") as fh:
        json.dump({"seed": SEED, "files": manifest}, fh, indent=1)

    total = sum(m["bytes"] for m in manifest)
    print("\n{} files, {:,} bytes ({:.1f} MB) -> {}".format(
        len(manifest), total, total / 1e6, outdir))
    print("manifest: {}".format(mpath))
    if missing_src:
        print("\n{} entries could not be built -- fetch the corpora first "
              "(benchmarks/fetch_corpus.py, fetch_matrix.py, make_hostile.py):"
              .format(len(missing_src)))
        for name, src in missing_src:
            print("  {:<28} needs {}".format(name, src))
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_suite")
    ap.add_argument("outdir", nargs="?", default=os.path.join(IN, "suite"))
    ap.add_argument("--max-tier", choices=TIERS, default="l",
                    help="build up to this tier only (default l = everything)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild files that already exist")
    a = ap.parse_args(argv)
    return build(os.path.abspath(a.outdir), a.max_tier, a.force)


if __name__ == "__main__":
    sys.exit(main())
