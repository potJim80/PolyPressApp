"""The C binary must refuse what it cannot parse, at the door.

    python3 tests/test_input_guard.py

This is a regression suite for one bug, and the bug is worth restating because
its shape recurs in this repo.

`csrc/polypress compress` reads comma-separated text and nothing else, but the
CLI used to hand `table_read_csv` whatever file it was given. Pointed at a
Parquet file it read the *binary* as text, found 883 "rows" of 2 "columns",
encoded them, ran the round-trip verification, **passed it**, wrote the
archive, and restored to a corrupted file. Pointed at a TSV it found one
column per line -- nothing in a TSV is a comma -- and so disagreed with the
Python encoder about what the table even was, which is invariant 1 broken in
the one place no test looked.

The verification did not fail because it compares the parsed table against the
decoded table. Both agreed. The damage happened *before* either existed. This
repo has already paid for that lesson once, when `errors="replace"` turned
undecodable bytes into U+FFFD before the round-trip check ran and a latin-1
file lost every accent while reporting success. **A check downstream of the
damage cannot see the damage.**

So the fix is a refusal at the entrance, and this suite pins it:

  * a container that announces itself by magic bytes is refused
  * a format the Python CLI handles properly is refused with a pointer to it
  * nothing is written when a file is refused -- a refusal that leaves a
    half-archive behind is not a refusal
  * ordinary CSV is unaffected, including the awkward cells the byte-identity
    guarantee depends on

That last one is the real risk in a guard like this. The guard is deliberately
limited to magic numbers and file extensions, and does **not** sniff content,
because content rules are exactly where the two implementations drift apart --
and a guard that makes C refuse what Python accepts would destroy byte-identity
in the act of defending it.

A NUL byte is the case that makes the point, and checking it turned up a
divergence that was already there:

    Python  dtz.read_any on a CSV containing a NUL raises
            `_csv.Error: line contains NUL` and refuses the file.
    C       table_read_csv accepts it and compresses it.

So the two CLIs disagree about whether that file is readable at all. It is not
data loss -- the C archive restores to what C parsed -- and it is not invariant
1, which is about two encoders given the *same table*. It is recorded here
rather than fixed because changing either reader's mind about NUL is a format
decision, not a bug fix. Note the direction: a NUL *is* legal in a table cell
built in memory, where it forces quoting in the canonical CSV and
`test_cbin.py::check_canonical` pins that. Reading one back out of a file is
the part Python refuses.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BINARY = os.path.join(ROOT, "csrc", "polypress")

PLAIN_CSV = b"id,name,value\n1,alpha,10\n2,beta,20\n3,gamma,30\n"

# Files that must be refused, as (filename, first bytes, why it is here).
REFUSE = [
    ("data.parquet", b"PAR1\x15\x04\x15\x00\x15\x02" + b"\x00" * 64,
     "the file that started this: binary read as text, verified, corrupted"),
    ("data.orc", b"ORC\x00\x01\x02\x03" + b"\x00" * 64,
     "another columnar container"),
    ("data.feather", b"ARROW1\x00\x00" + b"\x00" * 64,
     "Arrow IPC"),
    ("book.xlsx", b"PK\x03\x04\x14\x00" + b"\x00" * 64,
     "xlsx is a zip, and a zip read as CSV is nonsense"),
    ("t.csv.gz", b"\x1f\x8b\x08\x00" + b"\x00" * 64,
     "already compressed"),
    ("t.csv.xz", b"\xfd7zXZ\x00" + b"\x00" * 64,
     "already compressed"),
    ("t.csv.zst", b"\x28\xb5\x2f\xfd" + b"\x00" * 64,
     "already compressed"),
    ("db.sqlite", b"SQLite format 3\x00" + b"\x00" * 64,
     "a database, not a table file"),
    ("again.ppz", b"PPZ1" + b"\x00" * 64,
     "compressing an archive again is always a mistake"),
    # Extension-only cases: plausible text, but a delimiter this binary does
    # not use. These are the invariant-1 half of the bug -- the C encoder
    # would have produced a different table from the Python one.
    ("data.tsv", b"id\tname\n1\talpha\n2\tbeta\n",
     "tabs, so C would find exactly one column per line"),
    ("data.psv", b"id|name\n1|alpha\n2|beta\n",
     "pipes, same problem"),
    ("data.json", b'[{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]\n',
     "the Python CLI reads this properly; C must not pretend to"),
    ("data.jsonl", b'{"id": 1}\n{"id": 2}\n',
     "same"),
    ("data.ndjson", b'{"id": 1}\n{"id": 2}\n',
     "same"),
]

# Ordinary CSV that must still be accepted. The awkward ones are here because
# a careless guard would reject them and quietly break byte-identity.
ACCEPT = [
    ("plain.csv", PLAIN_CSV, "the ordinary case"),
    ("quoted.csv", b'id,note\n1,"has, comma"\n2,"has ""quotes"""\n',
     "quoting and escapes"),
    ("newline.csv", b'id,note\n1,"line one\nline two"\n2,plain\n',
     "an embedded newline"),
    ("utf8.csv", "id,name\n1,café\n2,北京\n".encode("utf-8"),
     "non-ASCII text"),
    ("noext", PLAIN_CSV, "no extension at all is not a reason to refuse"),
    ("upper.CSV", PLAIN_CSV, "extension matching must not be case sensitive"),
]


def main() -> int:
    if not os.path.exists(BINARY):
        print("csrc/polypress not built -- run ./csrc/build.sh first.")
        return 0

    failures = []
    checks = 0

    with tempfile.TemporaryDirectory() as tmp:
        print("must be REFUSED, with nothing written:")
        for name, body, why in REFUSE:
            src = os.path.join(tmp, name)
            out = os.path.join(tmp, name + ".ppz")
            with open(src, "wb") as fh:
                fh.write(body)
            proc = subprocess.run([BINARY, "compress", src, "-o", out],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=120)
            checks += 1
            wrote = os.path.exists(out)
            if proc.returncode == 0:
                failures.append("{}: accepted, but must be refused ({})"
                                .format(name, why))
                print("  {:<16} *** ACCEPTED ***".format(name))
            elif wrote:
                failures.append("{}: refused but left {} behind"
                                .format(name, os.path.basename(out)))
                print("  {:<16} refused, but WROTE OUTPUT".format(name))
            else:
                # The message opens with the path, which here is a long temp
                # directory; show the part that says why.
                msg = proc.stderr.decode("utf-8", "replace").strip()
                first = msg.split("\n")[0]
                reason = first.split(name, 1)[-1].strip() or first
                print("  {:<16} refused:{}".format(name, reason[:54]))

        print("\nmust still be ACCEPTED:")
        for name, body, why in ACCEPT:
            src = os.path.join(tmp, name)
            out = os.path.join(tmp, name + ".ppz")
            with open(src, "wb") as fh:
                fh.write(body)
            proc = subprocess.run([BINARY, "compress", src, "-o", out],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=120)
            checks += 1
            if proc.returncode != 0 or not os.path.exists(out):
                failures.append("{}: refused, but must be accepted -- {}"
                                .format(name, why))
                print("  {:<16} *** REFUSED *** {}".format(
                    name, proc.stderr.decode("utf-8", "replace")[:50]))
                continue

            # And it has to still be the same archive Python would write.
            from polypress import dtz, fast
            table = dtz.read_any(src)
            py = fast.encode(table)
            c = open(out, "rb").read()
            if py != c:
                failures.append("{}: accepted but C and Python disagree "
                                "({:,} B vs {:,} B)".format(name, len(py),
                                                            len(c)))
                print("  {:<16} accepted, BYTES DIFFER".format(name))
            else:
                print("  {:<16} accepted, byte-identical to Python".format(name))

    print()
    if failures:
        print("{} of {} checks FAILED:".format(len(failures), checks))
        for f in failures:
            print("  " + f)
        return 1
    print("all {} checks passed: every unparseable input is refused with "
          "nothing written, and every real CSV still encodes byte-identically"
          .format(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
