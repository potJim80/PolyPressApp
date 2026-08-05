"""Column ordering. Which columns should sit next to each other, and why.

xz pays for a copy by how far back it reaches. Two columns drawing on the same
vocabulary -- ten ICD-10 diagnosis slots, a set of "agency" fields, start and
end dates -- are each paying to establish that vocabulary separately when they
sit megabytes apart. Put them adjacent and the second one copies from the
first at a short distance, which is several times cheaper.
"""

# A column with more distinct values than this is skipped: building the
# intersection is not free and a near-unique column has no vocabulary to share.
MAX_CARD = 200_000

# Share this fraction of the smaller column's vocabulary and they are pooled.
OVERLAP = 0.5

# Below this many distinct values, two columns overlapping is meaningless --
# every yes/no column in the table would pool with every other one.
MIN_CARD = 4


def _vocabs(cols, min_card):
    out = []
    for c in cols:
        s = set(c)
        out.append(s if min_card <= len(s) <= MAX_CARD else None)
    return out


def zorder_rows(cols, max_keys=4, max_card=4096):
    """Row order that gives EVERY key column partial grouping, not one column
    all of it.

    Sorting by a single column collapses that column into perfect runs and
    shuffles the other forty -- measured, and it is why a plain sort wins on
    three tables and loses on two. Z-ordering (Morton order) is the standard
    fix, the same one Delta Lake's ZORDER BY and BigQuery clustering use:
    rank each key column's values, then interleave the BITS of those ranks.
    Rows close in any single key stay close in the file.

    Interleaving needs numbers, which is why this can only run on ranked
    codes -- you cannot interleave the bits of "CHICAGO POLICE DEPT".
    """
    n = len(cols[0]) if cols and cols[0] else 0
    if n == 0:
        return None

    cand = []
    for i, c in enumerate(cols):
        card = len(set(c))
        if 1 < card <= max_card:
            cand.append((card, i))
    if len(cand) < 2:
        return None
    cand.sort()
    keys = [i for _, i in cand[:max_keys]]

    ranks, widths = [], []
    for i in keys:
        order = {v: r for r, v in enumerate(sorted(set(cols[i])))}
        ranks.append([order[v] for v in cols[i]])
        widths.append(max(len(order) - 1, 1).bit_length())

    bits = max(widths)
    k = len(keys)
    morton = [0] * n
    for r in range(n):
        key = 0
        for b in range(bits - 1, -1, -1):
            for j in range(k):
                key = (key << 1) | ((ranks[j][r] >> b) & 1)
        morton[r] = key
    return sorted(range(n), key=lambda r: (morton[r], r))


def find_pairs(cols, order, int_plan):
    """Integer columns that move together -> {i: j}, each index used once.

    "Move together" is measured, not guessed: the difference between the two
    columns has to take far fewer distinct values than the columns themselves.
    A row_id and its copy, a start and end date, an x and a y from the same
    survey point all pass; two unrelated counters do not.
    """
    plans = {}
    for i in order:
        p = int_plan(cols[i])
        if p is not None:
            plans[i] = p

    ids = [i for i in order if i in plans]
    n = len(cols[0]) if cols and cols[0] else 0
    if n == 0:
        return {}
    ceiling = max(4, n // 20)          # difference must be this near-constant

    taken, pairs = set(), {}
    for a_pos, i in enumerate(ids):
        if i in taken:
            continue
        wi, vi = plans[i]
        best, best_card = None, None
        for j in ids[a_pos + 1:]:
            if j in taken:
                continue
            wj, vj = plans[j]
            if wj != wi:               # interleaving needs one stride
                continue
            card = len({a - b for a, b in zip(vi, vj)})
            if card > ceiling:
                continue
            if best_card is None or card < best_card:
                best, best_card = j, card
        if best is not None:
            pairs[i] = best
            taken.add(i)
            taken.add(best)
    return pairs


def pool_order(cols, min_card=MIN_CARD):
    """-> a permutation of column indices, vocabulary-sharing ones adjacent.

    Groups are emitted in the order their first member appears, and members
    keep their original relative order, so a table with nothing to pool comes
    back exactly as it went in.
    """
    vocab = _vocabs(cols, min_card)
    n = len(cols)
    group = list(range(n))          # union-find parent

    def find(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(n):
        if vocab[i] is None:
            continue
        for j in range(i + 1, n):
            if vocab[j] is None:
                continue
            a, b = find(i), find(j)
            if a == b:
                continue
            small = min(len(vocab[i]), len(vocab[j]))
            if len(vocab[i] & vocab[j]) / small >= OVERLAP:
                group[b] = a

    buckets, seen = [], {}
    for i in range(n):
        root = find(i)
        if root not in seen:
            seen[root] = len(buckets)
            buckets.append([])
        buckets[seen[root]].append(i)
    return [i for bucket in buckets for i in bucket]
