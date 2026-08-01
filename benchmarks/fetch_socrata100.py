"""Fetch an UNBIASED corpus of real open-data tables for benchmarking.

    python3 benchmarks/fetch_socrata100.py corpus100/ --count 100
    python3 benchmarks/fetch_socrata100.py corpus100/ --manifest-only

Why this exists
---------------
`fetch_corpus.py` pulls 13 datasets that were *chosen*, one at a time, by
someone who already knew what the codec was good at. That is fine for
debugging and worthless as evidence: the obvious objection to any compression
result is "you picked the files". There is no way to answer it by picking more
files.

So this does not pick. It asks the Socrata open-data catalog -- the index
behind several hundred government portals -- for its datasets **in descending
order of page views**, and takes the first N that survive a short list of
purely mechanical filters. Rank order is fixed and public, so anyone can
reproduce the same list. Nothing is skipped because of what is in it.

The filters, and all of them are about being a table at all, not about
content:

  * the CSV export has to actually download (many assets are maps, blobs,
    external links, or just broken)
  * at least 2 columns and at least 20 rows -- a one-column list is not a
    table and nothing here would mean anything on it
  * at least 50 KB, because below that the archive is mostly container
    overhead and every codec measures its own header
  * not a byte-identical duplicate of one already taken (the same dataset is
    published on more than one portal)

**Every rejection is recorded in the manifest with its reason.** A corpus that
silently drops what it cannot handle is the same cherry-picking with extra
steps. `--manifest-only` reprints the record without downloading.

Resuming
--------
The output filename is derived from catalog metadata alone, so it is known
*before* the download. If that file is already on disk it is measured in place
and not fetched again, and the manifest is rewritten after every accepted
dataset. Five hundred datasets is several gigabytes over a network; a run that
loses all of it to one dropped connection is not usable. `--refetch` forces
the download anyway.

Size handling
-------------
`$limit` caps the row count at the source. If the CSV still comes back over
`--max-mb`, it is truncated at a row boundary and the manifest says so. That
is honest because every codec in the comparison is then handed the identical
truncated file -- it changes which table is being measured, not who wins.
The ceiling exists because fast.py expands CSV about 8.5x into Python strings
and benchmarking holds an encoded and a decoded copy at once; see the peak
memory rule in CLAUDE.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CATALOG = "http://api.us.socrata.com/api/catalog/v1"
UA = {"User-Agent": "polypress-benchmark (open-data compression study)"}

MIN_BYTES = 50 * 1024
MIN_ROWS = 20
MIN_COLS = 2


class TooSlow(Exception):
    """The download was still going after its whole-transfer deadline."""


def _get(url: str, timeout: int = 120, deadline: float = 300.0) -> bytes:
    """Fetch a URL under BOTH a socket timeout and a total-transfer deadline.

    `timeout` alone is not enough. It is a per-read idle timeout, so a portal
    that keeps dribbling bytes never trips it: Chicago's Taxi Trips table --
    a billion rows, from which the portal has to materialise fifty thousand --
    blocked one run for over half an hour and stalled everything behind it.
    One pathological dataset must not be able to hold up a five-hundred
    dataset fetch, so the transfer is read in chunks against a wall clock.
    """
    req = urllib.request.Request(url, headers=UA)
    started = time.time()
    chunks, total = [], 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            if time.time() - started > deadline:
                raise TooSlow("still downloading after {:.0f}s ({:,} bytes)"
                              .format(time.time() - started, total))
            chunk = r.read(1 << 18)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    return b"".join(chunks)


def catalog_page(offset: int, limit: int = 100) -> list:
    """One page of the catalog, ordered by lifetime page views, descending."""
    q = urllib.parse.urlencode({
        "only": "dataset",
        "limit": limit,
        "offset": offset,
        "order": "page_views_total",
    })
    body = _get("{}?{}".format(CATALOG, q), timeout=120)
    return json.loads(body).get("results", [])


def slug(domain: str, name: str, ident: str) -> str:
    """A stable, filesystem-safe name that still says where it came from."""
    dom = domain.replace("www.", "").split(".")[0]
    keep = []
    for ch in name.lower():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "_":
            keep.append("_")
    base = "".join(keep).strip("_")[:40].strip("_")
    return "{}_{}_{}".format(dom, base or "dataset", ident)


def probe_shape(body: bytes):
    """(rows, cols) of a CSV body, or (0, 0) if it does not parse as one."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = body.decode("latin-1")
        except Exception:
            return 0, 0
    try:
        rdr = csv.reader(io.StringIO(text))
        header = next(rdr)
        n = sum(1 for _ in rdr)
    except Exception:
        return 0, 0
    return n, len(header)


def truncate_rows(body: bytes, max_bytes: int) -> bytes:
    """Cut to the last whole line that fits. Keeps the header."""
    if len(body) <= max_bytes:
        return body
    cut = body.rfind(b"\n", 0, max_bytes)
    return body[:cut + 1] if cut > 0 else body[:max_bytes]


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="fetch_socrata100")
    ap.add_argument("outdir", nargs="?", default="corpus100")
    ap.add_argument("--count", type=int, default=100,
                    help="how many usable datasets to collect (default 100)")
    ap.add_argument("--rows", type=int, default=50000,
                    help="$limit sent to the portal (default 50000)")
    ap.add_argument("--max-mb", type=float, default=30.0,
                    help="truncate anything larger, at a row boundary")
    ap.add_argument("--scan-limit", type=int, default=1200,
                    help="give up after considering this many catalog entries")
    ap.add_argument("--manifest-only", action="store_true")
    ap.add_argument("--refetch", action="store_true",
                    help="download again even if the file is already on disk")
    a = ap.parse_args(argv[1:])

    os.makedirs(a.outdir, exist_ok=True)
    mpath = os.path.join(a.outdir, "manifest.json")

    if a.manifest_only:
        if not os.path.exists(mpath):
            print("no manifest at {}".format(mpath))
            return 1
        report(json.load(open(mpath)))
        return 0

    max_bytes = int(a.max_mb * 1e6)
    taken, rejected, seen_digest = [], [], {}
    offset, considered = 0, 0

    def write_manifest() -> dict:
        m = {
            "source": "Socrata open-data catalog, ordered by page_views_total",
            "catalog_url": CATALOG,
            "requested": a.count,
            "row_limit": a.rows,
            "max_mb": a.max_mb,
            "considered": considered,
            "taken": taken,
            "rejected": rejected,
        }
        with open(mpath + ".part", "w") as fh:
            json.dump(m, fh, indent=1)
        os.replace(mpath + ".part", mpath)
        return m

    while len(taken) < a.count and considered < a.scan_limit:
        try:
            page = catalog_page(offset)
        except Exception as exc:
            sys.stderr.write("catalog page at offset {} failed: {}\n"
                             .format(offset, exc))
            break
        if not page:
            break
        offset += len(page)

        for entry in page:
            if len(taken) >= a.count:
                break
            considered += 1
            res = entry.get("resource", {})
            ident = res.get("id", "")
            name = res.get("name", "")
            domain = entry.get("metadata", {}).get("domain", "")
            views = res.get("page_views", {}).get("page_views_total", 0)
            rank = considered
            rec = {"rank": rank, "id": ident, "name": name,
                   "domain": domain, "page_views": views}

            if not ident or not domain:
                rec["reason"] = "catalog entry has no id or domain"
                rejected.append(rec)
                continue

            url = "https://{}/resource/{}.csv?$limit={}".format(
                domain, ident, a.rows)
            sys.stderr.write("[{:>4}] {:<28} {:<38} ".format(
                rank, domain[:28], name[:38]))
            sys.stderr.flush()

            # The name depends only on catalog metadata, so an interrupted run
            # can be resumed without refetching. The bytes then go through the
            # identical filters below, so a resumed record is the same record.
            fname = slug(domain, name, ident) + ".csv"
            fpath = os.path.join(a.outdir, fname)
            cached = os.path.exists(fpath) and not a.refetch
            if cached:
                with open(fpath, "rb") as fh:
                    body = fh.read()
            else:
                try:
                    body = _get(url)
                except Exception as exc:
                    rec["reason"] = "download failed: {}".format(
                        str(exc)[:80])
                    rejected.append(rec)
                    sys.stderr.write("SKIP (download)\n")
                    continue

            if len(body) < MIN_BYTES:
                rec["reason"] = "only {} bytes, under the {} KB floor".format(
                    len(body), MIN_BYTES // 1024)
                rejected.append(rec)
                sys.stderr.write("SKIP (tiny)\n")
                continue

            body = truncate_rows(body, max_bytes)
            nrow, ncol = probe_shape(body)
            if ncol < MIN_COLS or nrow < MIN_ROWS:
                rec["reason"] = "not a table: {} rows x {} columns".format(
                    nrow, ncol)
                rejected.append(rec)
                sys.stderr.write("SKIP (shape)\n")
                continue

            digest = (len(body), body[:4096])
            if digest in seen_digest:
                rec["reason"] = "duplicate of {}".format(seen_digest[digest])
                rejected.append(rec)
                sys.stderr.write("SKIP (duplicate)\n")
                continue

            if not cached:
                # Written under a temporary name and renamed, so an interrupted
                # run never leaves a half file that the next run would trust.
                with open(fpath + ".part", "wb") as fh:
                    fh.write(body)
                os.replace(fpath + ".part", fpath)
            seen_digest[digest] = fname
            rec.update({"file": fname, "bytes": len(body),
                        "rows": nrow, "cols": ncol,
                        "truncated": len(body) >= max_bytes - 4096,
                        "url": url})
            taken.append(rec)
            write_manifest()
            sys.stderr.write("{}  {:>10,} B  {:>7,}x{:<4}\n".format(
                "cached" if cached else "ok    ", len(body), nrow, ncol))

    report(write_manifest())
    return 0


def report(m) -> None:
    taken, rejected = m["taken"], m["rejected"]
    print("\n{} datasets kept out of {} catalog entries considered"
          .format(len(taken), m["considered"]))
    if taken:
        total = sum(t["bytes"] for t in taken)
        print("{:,} bytes total, median {:,} B, largest {:,} B".format(
            total,
            sorted(t["bytes"] for t in taken)[len(taken) // 2],
            max(t["bytes"] for t in taken)))
        print("{} distinct portals, {} truncated at the size ceiling".format(
            len(set(t["domain"] for t in taken)),
            sum(1 for t in taken if t.get("truncated"))))
    counts = {}
    for r in rejected:
        key = r["reason"].split(":")[0].split(",")[0]
        counts[key] = counts.get(key, 0) + 1
    if counts:
        print("\nrejected, by reason:")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print("  {:>4}  {}".format(v, k))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
