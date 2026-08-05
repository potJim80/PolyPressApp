#!/usr/bin/env python3
"""How big is the table once it is in a database you can actually query?

The standing benchmark has 17 competitors and not one of them is a database.
That is the wrong gap to have, because the question a researcher asks is not
"how small is the archive" but "how big is the thing I can run SQL against".

    python3 stridexz/bench_sql.py out.jsonl file1.csv file2.csv ...

Measured per table, on the same 16 MB row-capped slice the stridexz sweep used:

    duckdb          native .duckdb storage, its own compression
    sqlite          plain .db, every column TEXT
    sqlite VACUUM   the same after VACUUM, which is the honest floor
    sqlite+xz       the vacuumed .db through xz -- an archive, not queryable

A database file is NOT an archive: it carries page structure and stays
queryable, which an archive does not. Quote that next to every number here.
Both engines are round-trip checked by copying the table back out and
comparing every cell; a failure is reported, never hidden.
"""
import json
import lzma
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz                        # noqa: E402

SCRATCH = os.environ.get(
    "STRIDEXZ_SCRATCH",
    "/tmp/claude-501/-Users-mahdiakbarin-Desktop-polypress/"
    "dac04072-98f5-4d6e-809b-d797450daa39/scratchpad")


def truncate(path, max_mb):
    """The identical slice the stridexz sweep saw."""
    cap = int(max_mb * 1024 * 1024)
    out = os.path.join(SCRATCH, "sqlcut.csv")
    with open(path, "rb") as fh:
        data = fh.read(cap)
    if os.path.getsize(path) > cap:
        data = data[:data.rfind(b"\n") + 1]
    with open(out, "wb") as fh:
        fh.write(data)
    return out, os.path.getsize(path) > cap


def rm(*paths):
    for p in paths:
        for suffix in ("", "-wal", "-shm", ".wal"):
            try:
                os.unlink(p + suffix)
            except OSError:
                pass


def cells_match(t, other) -> bool:
    """Compare content, not column names -- engines rename duplicates."""
    return t.rows == other.rows


def run_duckdb(csv_path, t):
    db = os.path.join(SCRATCH, "bench.duckdb")
    out = os.path.join(SCRATCH, "duck_out.csv")
    rm(db, out)
    sql = (
        f"CREATE TABLE t AS SELECT * FROM read_csv('{csv_path}', "
        f"all_varchar=true, header=true, sample_size=-1, ignore_errors=false);"
        f"CHECKPOINT;"
    )
    t0 = time.perf_counter()
    r = subprocess.run(["duckdb", db, "-c", sql],
                       capture_output=True, text=True)
    secs = time.perf_counter() - t0
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:200]}
    size = os.path.getsize(db)
    subprocess.run(["duckdb", db, "-c",
                    f"COPY t TO '{out}' (HEADER, FORMAT CSV);"],
                   capture_output=True, text=True)
    ok = False
    try:
        ok = cells_match(t, dtz.read_any(out).normalise())
    except Exception:                                   # noqa: BLE001
        ok = False
    rm(db, out)
    return {"bytes": size, "secs": round(secs, 2), "ok": ok}


def run_sqlite(csv_path, t):
    db = os.path.join(SCRATCH, "bench.sqlite")
    out = os.path.join(SCRATCH, "sqlite_out.csv")
    rm(db, out)
    script = f".mode csv\n.import '{csv_path}' t\n"
    t0 = time.perf_counter()
    r = subprocess.run(["sqlite3", db], input=script,
                       capture_output=True, text=True)
    secs = time.perf_counter() - t0
    if r.returncode != 0 or not os.path.exists(db):
        return {"error": (r.stderr or r.stdout).strip()[:200]}, None, None
    plain = os.path.getsize(db)

    t1 = time.perf_counter()
    subprocess.run(["sqlite3", db, "VACUUM;"], capture_output=True, text=True)
    vac_secs = secs + time.perf_counter() - t1
    vacuumed = os.path.getsize(db)

    exp = f".headers on\n.mode csv\n.output '{out}'\nSELECT * FROM t;\n"
    subprocess.run(["sqlite3", db], input=exp, capture_output=True, text=True)
    ok = False
    try:
        ok = cells_match(t, dtz.read_any(out).normalise())
    except Exception:                                   # noqa: BLE001
        ok = False

    with open(db, "rb") as fh:
        raw = fh.read()
    t2 = time.perf_counter()
    xzd = len(lzma.compress(raw, format=lzma.FORMAT_XZ,
                            filters=[{"id": lzma.FILTER_LZMA2,
                                      "preset": 9 | lzma.PRESET_EXTREME}]))
    xz_secs = vac_secs + time.perf_counter() - t2
    rm(db, out)
    return ({"bytes": plain, "secs": round(secs, 2), "ok": ok},
            {"bytes": vacuumed, "secs": round(vac_secs, 2), "ok": ok},
            {"bytes": xzd, "secs": round(xz_secs, 2), "ok": ok})


def main():
    out_path = sys.argv[1]
    max_mb = float(os.environ.get("PROBE_MAX_MB", "16"))
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(l)["file"] for l in open(out_path) if l.strip()}

    for path in sys.argv[2:]:
        base = os.path.basename(path)
        if base in done:
            print(f"skip {base}")
            continue
        cut, truncated = truncate(path, max_mb)
        t = dtz.read_any(cut).normalise()
        res = {"duckdb": run_duckdb(cut, t)}
        s_plain, s_vac, s_xz = run_sqlite(cut, t)
        res["sqlite"] = s_plain
        if s_vac:
            res["sqlite VACUUM"] = s_vac
            res["sqlite+xz"] = s_xz

        row = {"file": base, "rows": t.shape[0], "cols": t.shape[1],
               "csv": os.path.getsize(cut), "truncated": truncated,
               "results": res}
        with open(out_path, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        summary = "  ".join(
            f"{k}={v.get('bytes', 'ERR'):,}{'' if v.get('ok') else '!'}"
            if isinstance(v.get("bytes"), int) else f"{k}=ERR"
            for k, v in res.items())
        print(f"{base[:44]:<46} {summary}", flush=True)
        os.unlink(cut)


if __name__ == "__main__":
    main()
