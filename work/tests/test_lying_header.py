"""Archives whose header is well-formed and DISHONEST.

The corrupt-archive suites damage bytes: truncate, bit-flip, splice. That
finds decompressors that never terminate and buffers that grow toward a
terabyte, and it found several. What it structurally cannot produce is an
archive that decompresses cleanly, parses as valid JSON, and then *lies about
its own shape* -- claims more columns than it carries payloads for, names a
parent that does not exist, or declares more exception cells than there are
rows. A random mutation almost never lands on a coherent header.

That gap was not theoretical. Three unchecked indices survived every fuzzing
pass in this repo and were found by reading the code on 2026-07-29: the
dictionary walk indexed sgroup[] and cut[] by counters driven from the column
list, the text walk indexed sgroup[], and the 2D group loop indexed cut[] --
none of them checked against the arrays' real lengths. An archive naming more
columns than it has payloads read out of bounds.

So this builds the header deliberately. Take a valid archive, decompress its
metadata, edit the JSON into something a hostile party would write,
recompress, and require the decoder to refuse it -- in both implementations,
and without crashing or allocating without bound.

"Refused" here means: raises, or returns a table. It must NOT segfault, hang,
or come back with a table that silently differs in shape from what it claims.

    python3 tests/test_lying_header.py
"""

from __future__ import annotations

import copy
import json
import lzma
import os
import resource
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polypress import dtz, fast   # noqa: E402

MEM_CAP = 512 * 1024 * 1024
SELF = os.path.abspath(__file__)
CBIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "csrc", "polypress")


# ------------------------------------------------------------ the specimens

def base_table():
    """Wide enough to exercise dict, text, numeric, a 2D group and exceptions."""
    rows = []
    for i in range(120):
        rows.append([
            "{:05d}".format(90000 + i % 12),          # dict, a good parent
            "city{}".format(i % 12),                  # dict, child of it
            "note {} free text".format(i),            # text
            "" if i == 33 else str(i * 7),            # numeric with exceptions
            "{:.2f}".format(1.0 + i * 0.01),          # 2D group
            "{:.2f}".format(1.5 + i * 0.01),
            "{:.2f}".format(2.0 + i * 0.01),
        ])
    return dtz.Table(["zip", "city", "note", "n", "a", "b", "c"], rows)


def split(blob):
    ml = int.from_bytes(blob[4:8], "big")
    bl = int.from_bytes(blob[8:12], "big")
    tl = int.from_bytes(blob[12:16], "big")
    o = 16
    return (json.loads(lzma.decompress(blob[o:o + ml], **fast.XZ)),
            blob[o + ml:o + ml + bl], blob[o + ml + bl:o + ml + bl + tl])


def rebuild(meta, binz, txtz):
    mz = lzma.compress(json.dumps(meta, separators=(",", ":")).encode(),
                       **fast.XZ)
    return (fast.MAGIC + len(mz).to_bytes(4, "big")
            + len(binz).to_bytes(4, "big") + len(txtz).to_bytes(4, "big")
            + mz + binz + txtz)


def lies(meta):
    """Every way a well-formed header can misdescribe the payloads."""
    out = []

    def add(name, fn):
        m = copy.deepcopy(meta)
        try:
            fn(m)
        except Exception:
            return
        out.append((name, m))

    add("extra_dict_column", lambda m: (
        m["cols"].append({"kind": "dict", "n": 3, "w": "<u1", "parent": None}),
        m["order"].append(len(m["cols"]) - 1),
        m["columns"].append("ghost")))
    add("extra_text_column", lambda m: (
        m["cols"].append({"kind": "text"}), m["columns"].append("ghost")))
    add("extra_num_column", lambda m: (
        m["cols"].append({"kind": "num", "dec": 0, "k": 0, "warm": []}),
        m["columns"].append("ghost")))
    add("extra_group", lambda m: m["groups"].append([0, 1, 2]))
    add("group_names_missing_column",
        lambda m: m["groups"].append([len(m["cols"]) + 40,
                                      len(m["cols"]) + 41,
                                      len(m["cols"]) + 42]))
    add("order_names_missing_column",
        lambda m: m["order"].append(len(m["cols"]) + 99))
    add("parent_out_of_range", lambda m: m["cols"][1].__setitem__(
        "parent", len(m["cols"]) + 50))
    add("parent_is_self", lambda m: m["cols"][1].__setitem__("parent", 1))
    add("nrows_huge", lambda m: m.__setitem__("nrows", 1 << 40))
    add("nrows_negative", lambda m: m.__setitem__("nrows", -5))
    add("nex_absurd", lambda m: [c.__setitem__("nex", 1 << 30)
                                 for c in m["cols"] if "nex" in c])
    add("nex_on_a_column_without_one",
        lambda m: [c.__setitem__("nex", 4) for c in m["cols"]
                   if c.get("kind") == "num"])
    add("nex_exceeds_rows", lambda m: [c.__setitem__("nex", m["nrows"] + 10)
                                       for c in m["cols"] if "nex" in c])
    add("nlenbins_huge", lambda m: m.__setitem__("nlenbins", 1 << 20))
    add("nlenbins_negative", lambda m: m.__setitem__("nlenbins", -3))
    add("bins_lengths_inflated",
        lambda m: m.__setitem__("bins", [x + 1000 for x in m["bins"]]))
    add("bins_list_truncated", lambda m: m.__setitem__("bins", m["bins"][:1]))
    add("bins_list_extended",
        lambda m: m.__setitem__("bins", m["bins"] + [64, 64, 64]))
    add("smeta_truncated", lambda m: m.__setitem__("smeta", m["smeta"][:1]))
    add("smeta_counts_inflated",
        lambda m: [s.__setitem__("n", s["n"] + 5000) for s in m["smeta"]])
    add("smeta_bytes_inflated",
        lambda m: [s.__setitem__("b", s["b"] + 5000) for s in m["smeta"]])
    add("warm_too_long", lambda m: [c.__setitem__("warm", [1] * 64)
                                    for c in m["cols"] if c.get("kind") == "num"])
    add("k_absurd", lambda m: [c.__setitem__("k", 1 << 20)
                               for c in m["cols"] if c.get("kind") == "num"])
    add("dec_absurd", lambda m: [c.__setitem__("dec", 4000)
                                 for c in m["cols"] if "dec" in c])
    add("width_lies", lambda m: [c.__setitem__("w", "<u4")
                                 for c in m["cols"] if c.get("kind") == "dict"])
    add("columns_list_short", lambda m: m.__setitem__("columns",
                                                      m["columns"][:2]))
    add("unknown_kind", lambda m: m["cols"][0].__setitem__("kind", "wormhole"))
    return out


# ----------------------------------------------------------------- probing

def probe(path: str) -> int:
    """0 = returned a table, 1 = refused cleanly, 2 = wrong exception type."""
    resource.setrlimit(resource.RLIMIT_AS, (MEM_CAP, MEM_CAP))
    blob = open(path, "rb").read()
    try:
        t = fast.decode(blob)
        # A table is an acceptable answer only if it is self-consistent.
        for r in t.rows:
            if len(r) != len(t.columns):
                return 2
        return 0
    except (ValueError, KeyError, IndexError, TypeError, OverflowError,
            MemoryError, EOFError, lzma.LZMAError, UnicodeDecodeError,
            ZeroDivisionError, AttributeError, struct_error()):
        return 1
    except RecursionError:
        return 1


def struct_error():
    import struct
    return struct.error


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--probe":
        sys.exit(probe(sys.argv[2]))

    table = base_table()
    blob = fast.encode(table)
    if blob[:4] != fast.MAGIC:
        print("base table did not produce a modelled container -- "
              "the specimens would be meaningless")
        return 1
    meta, binz, txtz = split(blob)
    specimens = lies(meta)
    print("{} lying headers, base archive {} B\n".format(len(specimens),
                                                         len(blob)))

    tmp = tempfile.mkdtemp(prefix="ppz_lie_")
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    bad = []
    cbad = []
    have_c = os.path.exists(CBIN)

    for name, m in specimens:
        path = os.path.join(tmp, name + ".ppz")
        with open(path, "wb") as fh:
            fh.write(rebuild(m, binz, txtz))

        r = subprocess.run([sys.executable, SELF, "--probe", path],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        code = r.returncode if r.returncode in (0, 1, 2) else 3
        counts[code] += 1
        if code in (2, 3):
            bad.append((name, code))

        if have_c:
            c = subprocess.run([CBIN, "restore", path, "-o",
                                os.path.join(tmp, name + ".csv")],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120)
            # any exit code is fine; a signal (negative) is not
            if c.returncode < 0:
                cbad.append((name, c.returncode))

    print("  returned a self-consistent table : {}".format(counts[0]))
    print("  refused cleanly                  : {}".format(counts[1]))
    print("  wrong exception / inconsistent   : {}".format(counts[2]))
    print("  crashed or hit the memory cap    : {}".format(counts[3]))
    if have_c:
        print("  C binary killed by a signal      : {}".format(len(cbad)))
    else:
        print("  C binary not built -- skipped")

    for name, code in bad:
        print("    PYTHON {} -> {}".format(name, code))
    for name, code in cbad:
        print("    C {} -> signal {}".format(name, -code))

    if bad or cbad:
        print("\nFAILED")
        return 1
    print("\nevery dishonest header was refused or answered consistently")
    return 0


if __name__ == "__main__":
    sys.exit(main())
