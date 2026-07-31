"""Turn sweep JSONL into the numbers a reader can check.

    python3 benchmarks/report.py results/socrata100.jsonl --title "Socrata 100"
    python3 benchmarks/report.py results/*.jsonl --csv results/all-results.csv

Three questions get answered, in this order, because they are three different
strengths of claim and conflating them is how compression results get
overstated:

  1. **Does it win against the best of everything else?** Not against the mean
     of the field, and not against gzip -- against whichever competitor was
     smallest on that particular table. This is the only honest headline,
     because a reader storing a table will use the best tool they have, not
     the average one.

  2. **How does it do against each competitor individually?** A tool can beat
     the best-of-field rarely and still be reliably better than the specific
     format someone actually uses. Parquet is the row that matters; nobody
     stores a survey as compressed CSV.

  3. **Does the win survive using the competitor's own entropy coder?**
     Polypress finishes with xz, and Parquet cannot use xz at all. So part of
     any margin is the finisher rather than the modelling. The re-finished
     columns answer the sharpest objection anyone will raise, and if they ever
     stop showing a win, the honest summary changes.

Losses are printed in full, never summarised away. A result file that lists
its failures is worth more than one that has none.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

# Everything polypress produces, so "the competition" can be defined by
# exclusion rather than by an allow-list that silently drops a new codec.
OURS = "polypress"


def load(paths):
    recs, bad = [], []
    for p in paths:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                (bad if "error" in rec else recs).append(rec)
    return recs, bad


def competitors(rec):
    return {k: v for k, v in rec["results"].items() if not k.startswith(OURS)}


def fmt_ratio(x):
    return "{:.2f}x".format(x)


def section(title):
    print("\n" + title)
    print("-" * len(title))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="report")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--title", default="Polypress benchmark")
    ap.add_argument("--csv", default=None,
                    help="also write one row per dataset per codec")
    ap.add_argument("--top", type=int, default=12,
                    help="how many best/worst datasets to list")
    a = ap.parse_args(argv)

    recs, bad = load(a.paths)
    if not recs:
        print("no usable records in {}".format(", ".join(a.paths)))
        return 1

    total_raw = sum(r["bytes"] for r in recs)
    print("=" * 74)
    print(a.title)
    print("=" * 74)
    print("{} datasets measured, {:,} bytes of CSV, {:,} rows, {} columns "
          "in total".format(len(recs), total_raw,
                            sum(r["rows"] for r in recs),
                            sum(r["cols"] for r in recs)))
    if bad:
        print("{} datasets failed to measure and are listed at the end"
              .format(len(bad)))
    trunc = [r for r in recs if r.get("truncated_from")]
    if trunc:
        print("{} truncated at the size ceiling (every codec got the same "
              "truncated file)".format(len(trunc)))
    rt = [r for r in recs if r.get("roundtrip") is False]
    print("round-trip: {} of {} datasets decoded back to the exact input"
          .format(len(recs) - len(rt), len(recs)))
    if rt:
        print("  *** ROUND-TRIP FAILURES: {} ***"
              .format(", ".join(r["file"] for r in rt)))
    peak = max(r.get("peak_mb") or 0 for r in recs)
    print("peak RSS across the whole sweep: {:.0f} MB".format(peak))

    # ---- 1. against the best of everything else ---------------------------
    section("1. Polypress vs the best competitor on each table")
    wins, margins, rows = 0, [], []
    for r in recs:
        comp = competitors(r)
        ours = r["results"][OURS]["bytes"]
        blabel, bbytes = min(((k, v["bytes"]) for k, v in comp.items()),
                             key=lambda kv: kv[1])
        margin = bbytes / ours
        margins.append(margin)
        rows.append((margin, r["file"], ours, blabel, bbytes,
                     r["bytes"] / ours, r["bytes"] / bbytes))
        if ours < bbytes:
            wins += 1
    print("wins {} of {} ({:.0f}%)".format(wins, len(recs),
                                           100.0 * wins / len(recs)))
    print("margin over the best other tool: median {}, best {}, worst {}"
          .format(fmt_ratio(statistics.median(margins)),
                  fmt_ratio(max(margins)), fmt_ratio(min(margins))))
    ours_total = sum(r["results"][OURS]["bytes"] for r in recs)
    best_total = sum(min(v["bytes"] for v in competitors(r).values())
                     for r in recs)
    print("whole corpus: {:,} B vs {:,} B, {} smaller in aggregate"
          .format(ours_total, best_total, fmt_ratio(best_total / ours_total)))
    print("compression ratio vs raw CSV: median {}, best {}, worst {}".format(
        fmt_ratio(statistics.median([x[5] for x in rows])),
        fmt_ratio(max(x[5] for x in rows)),
        fmt_ratio(min(x[5] for x in rows))))

    rows.sort(reverse=True)
    print("\n  strongest {}:".format(a.top))
    hdr = "  {:<50} {:>7} {:>7}  {}".format("dataset", "vs best", "vs csv",
                                            "best other")
    print(hdr)
    for m, f, ours, bl, bb, vc, _bc in rows[:a.top]:
        print("  {:<50} {:>7} {:>7}  {}".format(
            f[:50], fmt_ratio(m), fmt_ratio(vc), bl))
    print("\n  weakest {} (losses first):".format(a.top))
    print(hdr)
    for m, f, ours, bl, bb, vc, _bc in rows[-a.top:]:
        print("  {:<50} {:>7} {:>7}  {}".format(
            f[:50], fmt_ratio(m), fmt_ratio(vc), bl))

    losses = [x for x in rows if x[0] < 1.0]
    if losses:
        print("\n  every loss, in full ({} of {}):".format(len(losses),
                                                           len(rows)))
        for m, f, ours, bl, bb, vc, _bc in sorted(losses):
            print("  {:<50} lost to {:<16} by {:.1f}%".format(
                f[:50], bl, 100.0 * (ours - bb) / bb))

    # ---- 2. head to head against each competitor --------------------------
    section("2. Head to head, one competitor at a time")
    labels = sorted({k for r in recs for k in competitors(r)})
    print("  {:<18} {:>10} {:>10} {:>10} {:>10}".format(
        "competitor", "n", "ppz wins", "median", "worst"))
    for lab in labels:
        pairs = [(r["results"][lab]["bytes"], r["results"][OURS]["bytes"])
                 for r in recs if lab in r["results"]]
        if not pairs:
            continue
        ms = [t / o for t, o in pairs]
        w = sum(1 for t, o in pairs if o < t)
        print("  {:<18} {:>10} {:>10} {:>10} {:>10}".format(
            lab, len(pairs), "{}/{}".format(w, len(pairs)),
            fmt_ratio(statistics.median(ms)), fmt_ratio(min(ms))))

    # ---- 3. like for like --------------------------------------------------
    section("3. Like for like: the same finisher on both sides")
    print("Polypress finishes with xz. pyarrow answers 'Unsupported")
    print("compression: xz', so Parquet cannot. These rows strip that")
    print("advantage out by re-finishing the same modelled streams.")
    LIKE_FOR_LIKE = (
        ("polypress+zstd", ("parquet+zstd", "orc+zstd", "feather+zstd",
                            "zstd -22")),
        ("polypress+brotli", ("parquet+brotli", "brotli -q11")),
    )
    for ours_lab, rivals in LIKE_FOR_LIKE:
        for rival in rivals:
            pairs = [(r["results"][rival]["bytes"],
                      r["results"][ours_lab]["bytes"])
                     for r in recs
                     if rival in r["results"] and ours_lab in r["results"]]
            if not pairs:
                continue
            ms = [t / o for t, o in pairs]
            w = sum(1 for t, o in pairs if o < t)
            print("  {:<18} vs {:<16} {:>8} wins   median {:>7}  worst {:>7}"
                  .format(ours_lab, rival, "{}/{}".format(w, len(pairs)),
                          fmt_ratio(statistics.median(ms)),
                          fmt_ratio(min(ms))))

    # ---- fidelity ----------------------------------------------------------
    section("4. Did the columnar formats reproduce the data exactly?")
    ex = [r.get("parquet_exact") for r in recs]
    print("Parquet read with type inference gave back the exact printed text")
    print("on {} datasets, changed it on {}, and could not be checked on {}."
          .format(sum(1 for e in ex if e is True),
                  sum(1 for e in ex if e is False),
                  sum(1 for e in ex if e is None)))
    print("Where it changed the text, part of any Parquet size win is")
    print("discarded formatting rather than compression -- '1.50' becoming")
    print("1.5 is a smaller file for a reason that is not the codec.")
    inexact = [r["file"] for r in recs if r.get("parquet_exact") is False]
    if inexact:
        print("  changed: " + ", ".join(f[:40] for f in inexact[:20]))
        if len(inexact) > 20:
            print("  ...and {} more".format(len(inexact) - 20))

    # ---- speed -------------------------------------------------------------
    section("5. Speed, indicative only")
    print("One timing pass per codec, taken while the sweep had the machine")
    print("to itself but with no attempt to quiet it. Sizes are exact and")
    print("reproducible; these numbers are not. Read them for order of")
    print("magnitude and nothing finer.")
    print("  {:<18} {:>12} {:>12}".format("codec", "enc MB/s", "dec MB/s"))
    allc = sorted({k for r in recs for k in r["results"]})
    for lab in allc:
        e = [r["results"][lab]["enc_mbs"] for r in recs
             if lab in r["results"] and r["results"][lab]["enc_mbs"]]
        d = [r["results"][lab]["dec_mbs"] for r in recs
             if lab in r["results"] and r["results"][lab]["dec_mbs"]]
        if not e:
            continue
        print("  {:<18} {:>12.1f} {:>12.1f}".format(
            lab, statistics.median(e), statistics.median(d) if d else 0.0))

    # ---- failures ----------------------------------------------------------
    if bad:
        section("6. Datasets that could not be measured")
        for r in bad:
            print("  {:<50} {}".format(r["file"][:50], r["error"][:70]))

    if a.csv:
        import csv as _csv
        os.makedirs(os.path.dirname(os.path.abspath(a.csv)) or ".",
                    exist_ok=True)
        with open(a.csv, "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["dataset", "csv_bytes", "rows", "cols", "codec",
                        "bytes", "ratio_vs_csv", "enc_mbs", "dec_mbs"])
            for r in recs:
                for lab, v in sorted(r["results"].items()):
                    w.writerow([r["file"], r["bytes"], r["rows"], r["cols"],
                                lab, v["bytes"],
                                round(r["bytes"] / v["bytes"], 4),
                                v["enc_mbs"] and round(v["enc_mbs"], 2),
                                v["dec_mbs"] and round(v["dec_mbs"], 2)])
        print("\nwrote {}".format(a.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
