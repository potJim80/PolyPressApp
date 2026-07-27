"""Random-table fuzzing of the Python codec and the C binary.

    python3 tests/test_fuzz.py [count] [seed]

The hand-written suites cover the cases someone thought of. This covers the
ones nobody did: tables assembled from a grab-bag of nasty cell generators --
values at the int64 boundaries, ragged decimals, embedded quotes and
newlines, non-BMP characters, empty and whitespace-only cells, duplicate and
unnameable column headers.

Two things are checked. The logical table must round-trip exactly, which is
the only thing the format actually promises. And where the CSV survives a
write/read cycle, the C encoder must produce byte-identical output to the
Python one -- a difference there means the two implementations disagree about
the plan, not merely about formatting.

It has already earned its place. In one run it found three real bugs:

  * the C JSON parser stored every number as a double, so int64 warm-start
    values above 2^53 silently rounded -- 74884171959489212 decoded as
    ...216. A decoder returning wrong numbers rather than failing.
  * find_2d_groups in C tested `a <= 8*b`, which overflows int64 at the
    magnitudes this codec accepts. The product wrapped negative, every group
    of large numeric columns was refused, and the archive came out bigger
    than Python's for no visible reason.
  * tcz.c checked for overflow *after* multiplying, so INT64_MIN was accepted
    as numeric despite a documented limit of 2^62 -- while the numpy fallback
    rejected it. The same file compressed differently depending on whether a
    compiler was available.

Default 250 tables keeps it to a few seconds; pass a larger count for a
longer soak.
"""
import os, random, string, subprocess, sys, tempfile, csv, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polypress import dtz, fast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(ROOT, 'csrc', 'polypress')

def gen_cell(rnd, mode):
    if mode == 'int':      return str(rnd.randint(-10**rnd.randint(1, 18), 10**rnd.randint(1, 18)))
    if mode == 'dec':      return "{:.{}f}".format(rnd.uniform(-1e6, 1e6), rnd.randint(0, 9))
    if mode == 'smallint': return str(rnd.randint(0, 5))
    if mode == 'cat':      return rnd.choice(['a', 'bb', 'ccc', '', ' ', 'ZZ'])
    if mode == 'text':     return ''.join(rnd.choice(string.printable) for _ in range(rnd.randint(0, 20)))
    if mode == 'nasty':    return rnd.choice(['', ' ', '\n', '\r\n', '"', ',', '\t', 'a"b,c\nd',
                                              'é', '中', '🎉', '\x00' if rnd.random() < .1 else 'x',
                                              '007', '-0.0', '1.', '.5', '+3', 'NaN', 'inf',
                                              str(2**63), str(-2**63), '0'*30])
    if mode == 'blank':    return ''
    if mode == 'big':      return str(rnd.randint(0, 2**62 - 1))
    return 'x'

MODES = ['int', 'dec', 'smallint', 'cat', 'text', 'nasty', 'blank', 'big']

def gen_table(rnd):
    nc = rnd.randint(1, 8)
    nr = rnd.choice([0, 1, 2, 3, 5, 9, 40, 200])
    modes = [rnd.choice(MODES) for _ in range(nc)]
    names = []
    for j in range(nc):
        if rnd.random() < 0.15:
            names.append(rnd.choice(['é', 'a b', 'a,b', 'a"b', '', 'x\ty', '🎉', 'dup']))
        else:
            names.append('c%d' % j)
    rows = [[gen_cell(rnd, modes[j]) for j in range(nc)] for _ in range(nr)]
    return dtz.Table(names, rows)

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    rnd = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    tmp = tempfile.mkdtemp(prefix='ppzfuzz-')
    py_fail, c_fail, c_diff, checked_c = [], [], [], 0
    for i in range(n):
        t = gen_table(rnd)
        # 1. Python round-trip
        try:
            blob = fast.encode(t)
            back = fast.decode(blob)
            if back.columns != t.columns or back.rows != t.rows:
                py_fail.append((i, 'mismatch', t.columns, len(t.rows)))
                continue
        except Exception:
            py_fail.append((i, traceback.format_exc().strip().splitlines()[-1], t.columns, len(t.rows)))
            continue
        # 2. C encoder byte-identity, only where CSV survives a write/read
        src = os.path.join(tmp, 'f%d.csv' % i)
        with open(src, 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh, lineterminator='\n')
            w.writerow(t.columns); w.writerows(t.rows)
        try:
            if dtz.read_any(src).rows != t.rows: continue
            if dtz.read_any(src).columns != t.columns: continue
        except Exception:
            continue
        if not os.path.exists(BINARY):
            continue
        checked_c += 1
        arc = os.path.join(tmp, 'f%d.ppz' % i)
        p = subprocess.run([BINARY, 'compress', src, '-o', arc], capture_output=True, timeout=120)
        if p.returncode != 0:
            c_fail.append((i, p.returncode, p.stderr.decode()[:120])); continue
        want, _ = fast._encode_plan(t)
        if open(arc, 'rb').read() != want:
            c_diff.append((i, len(t.rows), len(t.columns)))
    print('%d random tables' % n)
    print('  python round-trip failures: %d' % len(py_fail))
    for f in py_fail[:6]: print('   ', f)
    if not os.path.exists(BINARY):
        print('  C binary not built -- skipped (build with ./csrc/build.sh)')
    print('  C compress failures: %d  (of %d comparable)' % (len(c_fail), checked_c))
    for f in c_fail[:6]: print('   ', f)
    print('  C byte differences: %d' % len(c_diff))
    for f in c_diff[:6]: print('   ', f)
    return 1 if (py_fail or c_fail or c_diff) else 0

sys.exit(main())
