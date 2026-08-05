"""stridexz -- a table codec built from scratch to feed xz what xz wants.

Not Polypress. No parent search, no planar predictor, no finite differences.
The entire thesis is that *layout* is the lever: xz already has a very good
entropy stage, and the job of a table codec is to hand it bytes whose repeats
are long, regular, and close together.

Every idea is a flag on `encode`, so each one can be switched on alone and
measured. See `bench.py`. The stages, in the order they were added:

    (v0) baseline    column blocks, escaped, no delimiters
    pool             columns sharing a vocabulary made adjacent
    fixed            constant-width fields instead of separators
    planes           integer columns as fixed-width binary, byte planes split
    interleave       correlated column pairs alternated
    template         numbers split out of text, leaving an exact-repeat skeleton
    chunk            long columns cut so repeats stay at cheap distances
    tune             lc/lp/pb told what shape the data is

Everything round-trips exactly; `bench.py` verifies every cell before it
reports a size. A layout probe that does not round-trip is measuring a number
it is not entitled to.
"""
import json
import lzma

MAGIC = b"XZL1"
MAGIC_SPLIT = b"XZL2"

DEFAULTS = dict(
    dict=False,
    sort=False,
    zorder=False,
    mtf=False,
    pool=False,
    fixed=False,
    planes=False,
    interleave=False,
    template=False,
    chunk=0,
    tune=False,
    streams="one",
    stune=False,
    dictmb=0,
)

_XZ = dict(format=lzma.FORMAT_XZ,
           filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def _xz(data: bytes, tune=None, dict_size=0) -> bytes:
    if not tune and not dict_size:
        return lzma.compress(data, **_XZ)
    f = {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}
    if tune:
        f.update(tune)
    if dict_size:
        f["dict_size"] = dict_size
    return lzma.compress(data, format=lzma.FORMAT_XZ, filters=[f])


def _unxz(data: bytes) -> bytes:
    return lzma.decompress(data)


# ------------------------------------------------------- split-stream finish
# The whole table normally goes into ONE xz stream, so xz can copy between
# adjacent columns -- which is where `pool` earns its keep. `streams` splits
# that finish instead, to measure what the split costs (or buys).
#
#   one     the original: header + every blob, one stream          [XZL1]
#   percol  one stream per column unit                             [XZL2]
#   bykind  two streams, binary units and text units               [XZL2]
#
# Split streams use FORMAT_RAW, not FORMAT_XZ: a .xz container is ~60 bytes of
# header and CRC footer, and 116 of those on chicago_permits is 7 KB of pure
# accounting that would land on the split variants alone and contaminate the
# comparison. Raw LZMA2 self-terminates, so the length in the header is all
# the framing needed.
#
# dict_size is capped to the smallest power of two that covers the data. A
# dictionary cannot reference bytes the input does not have, so this changes
# no output byte -- it only stops liblzma allocating 64 MB per column.

_BINARY_KINDS = frozenset({"intp", "dict", "pair", "perm"})
_MAX_DICT = 1 << 26          # 64 MB, what preset 9 asks for
_HDR_DICT = 1 << 16


def _dict_size_for(n: int) -> int:
    d = 4096
    while d < n and d < _MAX_DICT:
        d <<= 1
    return d


def _raw_filters(tune, dict_size):
    f = {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}
    if tune:
        f.update(tune)
    f["dict_size"] = dict_size
    return [f]


def _raw(data: bytes, tune, dict_size) -> bytes:
    return lzma.compress(data, format=lzma.FORMAT_RAW,
                         filters=_raw_filters(tune, dict_size))


def _unraw(data: bytes, tune, dict_size) -> bytes:
    return lzma.decompress(data, format=lzma.FORMAT_RAW,
                           filters=_raw_filters(tune, dict_size))


# ------------------------------------------------------------------ escaping
# A column block is its cells joined by \n. A cell may itself contain \n or a
# backslash, so both are escaped. Real cells almost never do, so this costs
# nothing on real data and keeps the format honest on the ones that do.

def _esc(s: str) -> str:
    if "\\" in s or "\n" in s:
        return s.replace("\\", "\\\\").replace("\n", "\\n")
    return s


def _unesc(s: str) -> str:
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            out.append("\n" if nxt == "n" else nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _pack_esc(cells) -> bytes:
    return "\n".join(_esc(c) for c in cells).encode("utf-8", "surrogatepass")


def _unpack_esc(blob: bytes, nrows: int):
    if nrows == 0:
        return []
    text = blob.decode("utf-8", "surrogatepass")
    return [_unesc(p) for p in text.split("\n")]


# --------------------------------------------------------------- fixed width
# Idea 1: constant-width fields. Costs padding bytes before compression and
# can still win after, because every repeat then sits at the same distance and
# lands on one of xz's four cheap bookmarks.

def _fixed_plan(cells):
    """-> width, or None if fixed width is not usable/sensible here."""
    if not cells:
        return None
    widths = [len(c.encode("utf-8", "surrogatepass")) for c in cells]
    w = max(widths)
    if w == 0 or w > 64:
        return None
    total_esc = sum(widths) + len(cells)          # +1 separator each
    if w * len(cells) > total_esc * 2:            # padding more than doubles it
        return None
    for c in cells:                               # pad byte must be unused
        if "\x00" in c:
            return None
    return w


def _pack_fixed(cells, w: int) -> bytes:
    out = bytearray()
    for c in cells:
        b = c.encode("utf-8", "surrogatepass")
        out += b + b"\x00" * (w - len(b))
    return bytes(out)


def _unpack_fixed(blob: bytes, nrows: int, w: int):
    return [blob[i * w:(i + 1) * w].rstrip(b"\x00").decode("utf-8", "surrogatepass")
            for i in range(nrows)]


# ----------------------------------------------------------- integer planes
# Idea 4: an integer column as fixed-width binary, then split into byte planes
# -- all the high bytes, then all the low bytes. The high plane of a real
# column is nearly all one value, which is a long run at a perfectly regular
# stride: the cheapest thing in the format.
#
# Eligibility is strict on purpose. The cell text must be exactly what str()
# of the parsed integer gives back, so "007", "+3", " 5" and "" are all
# refused rather than silently normalised. Lossless first.

_WIDTHS = (1, 2, 4, 8)


def _int_plan(cells):
    """-> (width, values) or None."""
    if not cells:
        return None
    values = []
    for c in cells:
        if not c or len(c) > 19:
            return None
        body = c[1:] if c[0] == "-" else c
        if not body.isdigit():
            return None
        v = int(c)
        if str(v) != c:                 # leading zeros, "+1", "-0" -- refuse
            return None
        values.append(v)
    lo, hi = min(values), max(values)
    for w in _WIDTHS:
        bits = w * 8 - 1
        if -(1 << bits) <= lo and hi < (1 << bits):
            return w, values
    return None


def _pack_int_planes(values, w: int) -> bytes:
    raw = b"".join(v.to_bytes(w, "little", signed=True) for v in values)
    if w == 1:
        return raw
    return b"".join(raw[p::w] for p in range(w))


def _unpack_int_planes(blob: bytes, nrows: int, w: int):
    if w == 1:
        raw = blob
    else:
        planes = [blob[p * nrows:(p + 1) * nrows] for p in range(w)]
        raw = bytearray(nrows * w)
        for p, plane in enumerate(planes):
            raw[p::w] = plane
        raw = bytes(raw)
    return [str(int.from_bytes(raw[i * w:(i + 1) * w], "little", signed=True))
            for i in range(nrows)]


# -------------------------------------------------------------- templating
# Idea 6: turn near-repeats into exact repeats. "1234 N HALSTED ST" and
# "1236 N HALSTED ST" share nothing xz can copy cheaply once the digits differ
# early. Pull every run of digits out and the skeleton " N HALSTED ST" becomes
# an exact repeat, thousands of times over, while the digits go into their own
# stream where they sit next to other digits.
#
# The digit runs are kept as text, so "007" survives as "007".

_DIGIT_SLOT = "\x01"
_NUM_SEP = "\x02"


def _split_digits(cell: str):
    skel, nums, run = [], [], []
    for ch in cell:
        if ch.isascii() and ch.isdigit():
            run.append(ch)
        else:
            if run:
                nums.append("".join(run))
                skel.append(_DIGIT_SLOT)
                run = []
            skel.append(ch)
    if run:
        nums.append("".join(run))
        skel.append(_DIGIT_SLOT)
    return "".join(skel), nums


def _rejoin(skel: str, nums) -> str:
    out, k = [], 0
    for ch in skel:
        if ch == _DIGIT_SLOT:
            out.append(nums[k])
            k += 1
        else:
            out.append(ch)
    return "".join(out)


def _template_plan(cells):
    """-> (skeletons, numbers) if templating creates real repeats, else None."""
    if len(cells) < 64:
        return None
    for c in cells:                       # our two markers must not occur
        if _DIGIT_SLOT in c or _NUM_SEP in c:
            return None
    skels, nums, with_digits = [], [], 0
    for c in cells:
        s, n = _split_digits(c)
        skels.append(s)
        nums.append(n)
        if n:
            with_digits += 1
    if with_digits < len(cells) * 0.3:    # barely any digits -- nothing to win
        return None
    distinct_cells = len(set(cells))
    if distinct_cells < 32:               # already low cardinality; leave it
        return None
    if len(set(skels)) > distinct_cells * 0.5:
        return None                       # skeletons not repetitive enough
    return skels, nums


def _pack_template(skels, nums) -> bytes:
    a = _pack_esc(skels)
    b = "\n".join(_NUM_SEP.join(n) for n in nums).encode("utf-8", "surrogatepass")
    return len(a).to_bytes(4, "little") + a + b


def _unpack_template(blob: bytes, nrows: int):
    alen = int.from_bytes(blob[:4], "little")
    if alen > len(blob) - 4:
        raise ValueError("template skeleton length past end of blob")
    skels = _unpack_esc(blob[4:4 + alen], nrows)
    text = blob[4 + alen:].decode("utf-8", "surrogatepass")
    rows = text.split("\n") if nrows else []
    if len(skels) != nrows or len(rows) != nrows:
        raise ValueError("template stream row count disagrees with header")
    return [_rejoin(s, r.split(_NUM_SEP) if r else [])
            for s, r in zip(skels, rows)]


# -------------------------------------------------------------- dictionary
# Replace each distinct value with a code. On its own this earns very little:
# xz already codes a repeated string as a copy reference, which IS a code, so
# an explicit one gains nothing on the repeat and adds a dictionary to store.
# (Measured: OUT/results/numberise-probe.txt, lost on 8 of 8 tables when
# applied blindly to every column.)
#
# It is here for two reasons. First, applied *selectively* -- only where a
# probe says it pays -- it is a different proposition from applied always.
# Second, codes are what make Z-ordering possible at all: you cannot interleave
# the bits of "CHICAGO POLICE DEPT", only the bits of the code standing for it.

def _probe(blob: bytes) -> int:
    """Cheap size estimate. Preset 1 ranks candidates the way preset 9 does."""
    return len(lzma.compress(blob, format=lzma.FORMAT_XZ,
                             filters=[{"id": lzma.FILTER_LZMA2, "preset": 1}]))


def _dict_plan(cells):
    """-> (width, codes, alphabet) or None."""
    n = len(cells)
    if n < 64:
        return None
    order, index = [], {}
    for c in cells:
        if c not in index:
            index[c] = len(order)
            order.append(c)
            if len(order) * 2 > n:        # too many distinct values to pay off
                return None
    codes = [index[c] for c in cells]
    # _pack_int_planes is signed, so the usable range of a w-byte code is
    # 2^(8w-1), not 2^(8w). Getting this wrong overflows on the 129th symbol.
    w = 1 if len(order) <= 128 else (2 if len(order) <= 32768 else 4)
    return w, codes, order


def _pack_dict(codes, alphabet, w: int) -> bytes:
    a = _pack_esc(alphabet)
    return (len(a).to_bytes(4, "little") + a
            + _pack_int_planes(codes, w))


def _unpack_dict(blob: bytes, nrows: int, w: int, nsym: int):
    alen = int.from_bytes(blob[:4], "little")
    if alen > len(blob) - 4:
        raise ValueError("dictionary length past end of blob")
    alphabet = _unpack_esc(blob[4:4 + alen], nsym)
    if len(alphabet) != nsym:
        raise ValueError("dictionary holds the wrong number of symbols")
    codes = _unpack_int_planes(blob[4 + alen:], nrows, w)
    out = []
    for c in codes:
        k = int(c)
        if not 0 <= k < nsym:
            raise ValueError("dictionary code out of range")
        out.append(alphabet[k])
    return out


# ------------------------------------------------------------------ encoding

def _plan_column(cells, opt):
    """Pick a representation for one column. Returns (kind, params, blob)."""
    if opt["planes"]:
        plan = _int_plan(cells)
        if plan is not None:
            w, values = plan
            return "intp", {"w": w}, _pack_int_planes(values, w)
    if opt["fixed"]:
        w = _fixed_plan(cells)
        if w is not None:
            return "fixed", {"w": w}, _pack_fixed(cells, w)
    if opt["template"]:
        plan = _template_plan(cells)
        if plan is not None:
            return "tmpl", {}, _pack_template(*plan)
    return "esc", {}, _pack_esc(cells)


# ------------------------------------------------------------ move-to-front
# From bzip2's pipeline, and the sideways answer to the whole reordering
# question: instead of moving rows so equal values become adjacent, re-code
# each cell as "how many distinct values ago did I last see this one".
# A clustered column becomes a run of zeros WITHOUT any row being moved, so
# nothing else in the table gets shuffled and no permutation has to be stored.
#
# Capped at 256 symbols: the recode is a linear scan of the symbol list per
# cell, so a large alphabet makes this quadratic for a benefit that is
# concentrated in small ones anyway.

MTF_MAX_SYMBOLS = 256


def _mtf_plan(cells):
    """-> (ranks, alphabet) or None."""
    if len(cells) < 64:
        return None
    alphabet = []
    seen = set()
    for c in cells:
        if c not in seen:
            seen.add(c)
            alphabet.append(c)
            if len(alphabet) > MTF_MAX_SYMBOLS:
                return None
    if len(alphabet) < 2:
        return None
    live = list(alphabet)
    ranks = []
    for c in cells:
        i = live.index(c)
        ranks.append(i)
        if i:
            live.insert(0, live.pop(i))
    return ranks, alphabet


def _pack_mtf(ranks, alphabet) -> bytes:
    a = _pack_esc(alphabet)
    return (len(a).to_bytes(4, "little") + a
            + bytes(ranks))


def _unpack_mtf(blob: bytes, nrows: int, nsym: int):
    alen = int.from_bytes(blob[:4], "little")
    if alen > len(blob) - 4:
        raise ValueError("mtf alphabet length past end of blob")
    alphabet = _unpack_esc(blob[4:4 + alen], nsym)
    if len(alphabet) != nsym:
        raise ValueError("mtf alphabet holds the wrong number of symbols")
    ranks = blob[4 + alen:]
    if len(ranks) != nrows:
        raise ValueError("mtf rank count disagrees with the header")
    live = list(alphabet)
    out = []
    for i in ranks:
        if i >= len(live):
            raise ValueError("mtf rank out of range")
        c = live[i]
        out.append(c)
        if i:
            live.insert(0, live.pop(i))
    return out


def _plan_column_measured(cells, opt):
    """As _plan_column, but let a dictionary compete -- and only win if a
    probe says it is smaller. Selective, not blanket."""
    kind, params, blob = _plan_column(cells, opt)
    if not (opt["dict"] or opt["mtf"]):
        return kind, params, blob
    best = (_probe(blob), kind, params, blob)
    plan = _dict_plan(cells)
    if plan is not None:
        w, codes, alphabet = plan
        cand = _pack_dict(codes, alphabet, w)
        best = min(best, (_probe(cand), "dict",
                          {"w": w, "n": len(alphabet)}, cand),
                   key=lambda t: t[0])
    if opt["mtf"]:
        plan = _mtf_plan(cells)
        if plan is not None:
            ranks, alphabet = plan
            cand = _pack_mtf(ranks, alphabet)
            best = min(best, (_probe(cand), "mtf",
                              {"n": len(alphabet)}, cand),
                       key=lambda t: t[0])
    return best[1], best[2], best[3]


def _decode_column(kind, params, blob, nrows):
    if kind == "mtf":
        return _unpack_mtf(blob, nrows, params["n"])
    if kind == "dict":
        return _unpack_dict(blob, nrows, params["w"], params["n"])
    if kind == "tmpl":
        return _unpack_template(blob, nrows)
    if kind == "intp":
        return _unpack_int_planes(blob, nrows, params["w"])
    if kind == "fixed":
        return _unpack_fixed(blob, nrows, params["w"])
    return _unpack_esc(blob, nrows)


def _pack_pair(va, vb, w: int) -> bytes:
    """Idea 5: two columns that move together, planes interleaved.

    Plane p of A is written immediately before plane p of B, so B's high plane
    -- almost always the same near-constant run as A's -- is a copy from a few
    thousand bytes back rather than from the far side of a column.
    """
    ra = b"".join(v.to_bytes(w, "little", signed=True) for v in va)
    rb = b"".join(v.to_bytes(w, "little", signed=True) for v in vb)
    out = bytearray()
    for p in range(w):
        out += ra[p::w]
        out += rb[p::w]
    return bytes(out)


def _unpack_pair(blob: bytes, nrows: int, w: int):
    ba, bb = bytearray(nrows * w), bytearray(nrows * w)
    pos = 0
    for p in range(w):
        ba[p::w] = blob[pos:pos + nrows]
        pos += nrows
        bb[p::w] = blob[pos:pos + nrows]
        pos += nrows
    def unpack(raw):
        return [str(int.from_bytes(raw[i * w:(i + 1) * w], "little", signed=True))
                for i in range(nrows)]
    return unpack(bytes(ba)), unpack(bytes(bb))


def encode(table, **opts) -> bytes:
    o = dict(DEFAULTS)
    o.update(opts)
    ncols = len(table.columns)
    nrows = len(table.rows)
    cols = [table.column(i) for i in range(ncols)]

    # Row order first: everything downstream sees the reordered columns, and
    # the permutation rides along as one more integer column so the original
    # order comes back exactly.
    perm = None
    if o["zorder"] or o["sort"]:
        if o["zorder"]:
            from stridexz.layout import zorder_rows
            idx = zorder_rows(cols)
        else:
            keys = [i for _, i in sorted(
                (len(set(cols[i])), i) for i in range(ncols))]
            idx = sorted(range(nrows), key=lambda r: [cols[c][r] for c in keys])
        if idx is not None:
            cols = [[c[i] for i in idx] for c in cols]
            perm = idx

    order = list(range(ncols))
    if o["pool"]:
        from stridexz.layout import pool_order
        order = pool_order(cols, min_card=int(o["pool"]) if o["pool"] is not True else 4)

    pairs = {}
    if o["interleave"]:
        from stridexz.layout import find_pairs
        pairs = find_pairs(cols, order, _int_plan)

    # Idea 7: cut the table into row blocks so a column's own repeats, and its
    # neighbours', stay within cheap-bookmark distance instead of being a whole
    # blob apart. chunk == 0 means one block, the whole table.
    step = o["chunk"] or max(nrows, 1)
    bounds = [(s, min(s + step, nrows)) for s in range(0, max(nrows, 1), step)]

    blocks, blobs, kinds = [], [], []
    for lo, hi in bounds:
        # A unit is one blob and the column indices it restores.
        units, done = [], set()
        for i in order:
            if i in done:
                continue
            j = pairs.get(i)
            if j is not None and j not in done:
                wa, va = _int_plan(cols[i][lo:hi])
                _, vb = _int_plan(cols[j][lo:hi])
                units.append(("pair", {"w": wa}, [i, j], _pack_pair(va, vb, wa)))
                done.add(i)
                done.add(j)
                continue
            k, p, b = _plan_column_measured(cols[i][lo:hi], o)
            units.append((k, p, [i], b))
            done.add(i)
        if perm is not None:
            pw, pv = _int_plan([str(v) for v in perm[lo:hi]])
            units.append(("perm", {"w": pw}, [], _pack_int_planes(pv, pw)))
        blocks.append({"n": hi - lo,
                       "units": [[k, p, idx] for k, p, idx, _ in units],
                       "lens": [len(b) for _, _, _, b in units]})
        blobs.extend(b for _, _, _, b in units)
        kinds.extend(k for k, _, _, _ in units)

    head = {
        "columns": table.columns,
        "nrows": nrows,
        "perm": perm is not None,
        "blocks": blocks,
    }

    # A shrunken dictionary stands in for a table too big to fit one. What
    # decides whether xz can reach across a column boundary is the ratio of
    # column size to window, so squeezing the window is the same experiment as
    # growing the table, and costs a thousandth of the machine time.
    cap = int(o["dictmb"] * 1024 * 1024) if o["dictmb"] else 0

    if o["streams"] == "one":
        header = json.dumps(head, ensure_ascii=True,
                            separators=(",", ":")).encode("ascii")
        payload = len(header).to_bytes(4, "little") + header + b"".join(blobs)
        return MAGIC + _xz(payload, o["tune"] or None, cap)

    # --- split finish -------------------------------------------------------
    if o["streams"] == "percol":
        groups = [[i] for i in range(len(blobs))]
    elif o["streams"] == "bykind":
        binary = [i for i, k in enumerate(kinds) if k in _BINARY_KINDS]
        text = [i for i, k in enumerate(kinds) if k not in _BINARY_KINDS]
        groups = [g for g in (binary, text) if g]
    else:
        raise ValueError(f"unknown streams mode {o['streams']!r}")

    def tune_for(group):
        if not o["stune"]:
            return o["tune"] or None
        # A byte plane is stride-1 within its plane and carries no text
        # structure, so it wants no literal context; text keeps v7c's lc=4.
        if all(kinds[i] in _BINARY_KINDS for i in group):
            return {"lc": 0, "pb": 0}
        return {"lc": 4, "pb": 0}

    gmeta, streams = [], []
    for g in groups:
        data = b"".join(blobs[i] for i in g)
        tune = tune_for(g)
        ds = _dict_size_for(len(data))
        if cap:
            ds = max(4096, min(ds, cap))
        c = _raw(data, tune, ds)
        gmeta.append({"u": g, "c": len(c), "d": ds, "t": tune or {}})
        streams.append(c)

    head["groups"] = gmeta
    header = json.dumps(head, ensure_ascii=True,
                        separators=(",", ":")).encode("ascii")
    hc = _raw(header, None, _HDR_DICT)
    return (MAGIC_SPLIT + len(hc).to_bytes(4, "little") + hc
            + b"".join(streams))


def _read_one(blob: bytes):
    """[XZL1] -> (head, flat list of unit blobs)."""
    payload = _unxz(blob[4:])
    hlen = int.from_bytes(payload[:4], "little")
    if hlen > len(payload) - 4:
        raise ValueError("header length past end of stream")
    head = json.loads(payload[4:4 + hlen].decode("ascii"))
    blobs, pos = [], 4 + hlen
    for block in head["blocks"]:
        for size in block["lens"]:
            if pos + size > len(payload):
                raise ValueError("column blob past end of stream")
            blobs.append(payload[pos:pos + size])
            pos += size
    return head, blobs


def _read_split(blob: bytes):
    """[XZL2] -> (head, flat list of unit blobs)."""
    hclen = int.from_bytes(blob[4:8], "little")
    if hclen > len(blob) - 8:
        raise ValueError("header length past end of stream")
    head = json.loads(_unraw(blob[8:8 + hclen], None, _HDR_DICT).decode("ascii"))
    lens = [size for block in head["blocks"] for size in block["lens"]]
    blobs = [None] * len(lens)
    pos = 8 + hclen
    for meta in head.get("groups", []):
        size = meta["c"]
        if pos + size > len(blob):
            raise ValueError("group stream past end of stream")
        data = _unraw(blob[pos:pos + size], meta["t"] or None, meta["d"])
        pos += size
        at = 0
        for i in meta["u"]:
            if not 0 <= i < len(lens):
                raise ValueError("group names a unit that does not exist")
            if blobs[i] is not None:
                raise ValueError("two groups claim the same unit")
            if at + lens[i] > len(data):
                raise ValueError("group stream is shorter than its units")
            blobs[i] = data[at:at + lens[i]]
            at += lens[i]
        if at != len(data):
            raise ValueError("group stream is longer than its units")
    if any(b is None for b in blobs):
        raise ValueError("no group carries every unit")
    return head, blobs


def decode(blob: bytes):
    from polypress.dtz import Table
    if blob[:4] == MAGIC:
        head, blobs = _read_one(blob)
    elif blob[:4] == MAGIC_SPLIT:
        head, blobs = _read_split(blob)
    else:
        raise ValueError("not an stridexz stream")
    nrows = head["nrows"]

    columns = [[] for _ in head["columns"]]
    permvals = []
    seen = 0
    at = 0
    for block in head["blocks"]:
        n = block["n"]
        seen += n
        if seen > nrows:
            raise ValueError("blocks hold more rows than the header claims")
        for slot, (kind, params, idx) in enumerate(block["units"]):
            blk = blobs[at]
            at += 1
            if kind == "perm":
                permvals.extend(int(v) for v in
                                _unpack_int_planes(blk, n, params["w"]))
            elif kind == "pair":
                a, b = _unpack_pair(blk, n, params["w"])
                columns[idx[0]].extend(a)
                columns[idx[1]].extend(b)
            else:
                columns[idx[0]].extend(_decode_column(kind, params, blk, n))
    if seen != nrows:
        raise ValueError("blocks hold fewer rows than the header claims")

    rows = [[columns[c][r] for c in range(len(columns))] for r in range(nrows)]
    if head.get("perm"):
        if len(permvals) != nrows:
            raise ValueError("permutation length disagrees with the header")
        out = [None] * nrows
        for new, original in enumerate(permvals):
            if not 0 <= original < nrows or out[original] is not None:
                raise ValueError("permutation is not a permutation")
            out[original] = rows[new]
        rows = out
    return Table(list(head["columns"]), rows)
