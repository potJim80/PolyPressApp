#!/usr/bin/env python3
"""Pick 200 real single columns from the corpus, spanning kinds by quota.

Columns are classified by what they hold, not by their header, because
headers lie. The quota is what makes the summary readable -- taking the
first 200 columns off the corpus would be ~60% categorical codes and would
answer a question about one shape rather than about columns.
"""
from __future__ import annotations

import csv
import glob
import os
import re
import sys
from typing import Dict, List

csv.field_size_limit(10 ** 9)

INT = re.compile(r"^[+-]?\d+$")
FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}.*)?$|"
                  r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}([ T]\d{1,2}:\d{2}.*)?$")
ALPHA = re.compile(r"^[^\W\d_][\w .,'&/()-]*$", re.UNICODE)
HASDIGIT = re.compile(r"\d")
HASALPHA = re.compile(r"[^\W\d_]", re.UNICODE)

KINDS = ("int-only", "num-mixed", "date-time", "text-only",
         "text-mixed", "id-highcard", "blank-heavy")


def classify(values: List[str]) -> str:
    n = len(values)
    nonblank = [v for v in values if v != ""]
    if len(nonblank) < n * 0.5:
        return "blank-heavy"
    sample = nonblank if len(nonblank) <= 4000 else nonblank[::max(1, len(nonblank) // 4000)]
    m = len(sample)
    k = len(set(values))

    ints = sum(1 for v in sample if INT.match(v))
    floats = sum(1 for v in sample if FLOAT.match(v))
    dates = sum(1 for v in sample if DATE.match(v))

    if dates >= m * 0.95:
        return "date-time"
    if ints >= m * 0.98:
        # a pure-integer column that is also nearly unique is a key, and keys
        # behave nothing like measurements -- keep them apart
        return "id-highcard" if k > n * 0.9 and n > 1000 else "int-only"
    if floats >= m * 0.98:
        return "num-mixed"

    alpha = sum(1 for v in sample if ALPHA.match(v) and not HASDIGIT.search(v))
    mixed = sum(1 for v in sample if HASDIGIT.search(v) and HASALPHA.search(v))
    if k > n * 0.9 and n > 1000:
        return "id-highcard"
    if mixed >= m * 0.4:
        return "text-mixed"
    if alpha >= m * 0.8:
        return "text-only"
    return "text-mixed"


def read_columns(path: str, max_rows: int, max_bytes: int):
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rd = csv.reader(fh)
            hdr = next(rd)
            if not hdr or len(hdr) > 400:
                return []
            cols = [[] for _ in hdr]
            for i, row in enumerate(rd):
                if i >= max_rows:
                    break
                w = min(len(row), len(hdr))
                for j in range(w):
                    cols[j].append(row[j])
                for j in range(w, len(hdr)):
                    cols[j].append("")
    except (UnicodeDecodeError, StopIteration, csv.Error, OSError, ValueError):
        return []
    out = []
    base = os.path.basename(path).replace(".csv", "").replace("data_", "")
    for j, h in enumerate(hdr):
        v = cols[j]
        if len(v) < 2000:
            continue
        # a cell holding a newline would make the one-value-per-line form
        # ambiguous for case A; both cases must see the same table
        if any("\n" in s or "\r" in s for s in v[:5000]):
            continue
        nb = sum(len(s) for s in v) + len(v)
        if nb > max_bytes:
            keep = 0
            acc = 0
            for s in v:
                acc += len(s) + 1
                if acc > max_bytes:
                    break
                keep += 1
            if keep < 2000:
                continue
            v = v[:keep]
        if len(set(v)) == 1:
            continue          # constant: every scheme scores ~30 bytes
        out.append((f"{base[:26]}:{h[:18]}", v))
    return out


def harvest(pattern: str, target: int, per_file: int = 4,
            max_rows: int = 100_000, max_bytes: int = 900_000):
    paths = sorted(glob.glob(pattern))
    per_kind = target // len(KINDS) + 1
    got: Dict[str, list] = {k: [] for k in KINDS}
    total = 0
    for p in paths:
        if total >= target:
            break
        cands = read_columns(p, max_rows, max_bytes)
        taken = 0
        # widest kinds first so a file rich in one kind still yields the others
        cands.sort(key=lambda nv: len(set(nv[1])))
        for name, v in cands:
            if taken >= per_file or total >= target:
                break
            kind = classify(v)
            if len(got[kind]) >= per_kind:
                continue
            got[kind].append((name, kind, v))
            taken += 1
            total += 1
        sys.stderr.write(f"\r  scanned {os.path.basename(p)[:40]:<42} "
                         f"kept {total}/{target}")
        sys.stderr.flush()
    sys.stderr.write("\n")
    # a second pass fills any kind the corpus is thin in, ignoring the quota
    if total < target:
        for p in paths:
            if total >= target:
                break
            for name, v in read_columns(p, max_rows, max_bytes):
                if total >= target:
                    break
                kind = classify(v)
                if any(name == g[0] for g in got[kind]):
                    continue
                got[kind].append((name, kind, v))
                total += 1
    out = []
    for k in KINDS:
        out.extend(got[k])
    return out
