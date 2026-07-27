/* Encoding a Polypress archive, in C. Byte-identical to fast.py's encode().
 *
 * "Byte-identical" is the contract, not "equivalent", so this file is full of
 * places where the obvious C is not what is written. Three worth knowing
 * about before reading:
 *
 *   Summation order. The parent search compares entropies, and entropy is a
 *   sum of floats. np.sum is pairwise, not left-to-right, and the two give
 *   different last bits. A different last bit can flip a `>` comparison and
 *   therefore pick a different parent and therefore change every byte after
 *   it. pairwise_sum() below reproduces numpy's algorithm exactly, block size
 *   and all.
 *
 *   Tie-breaking. Equal gains are broken by lowest column index, matching the
 *   list iteration in pick_parents. (fast.py used a set there until this port
 *   forced the question; a set's order is a hash-table artefact, so the
 *   archive bytes depended on it. It is a list now, in both languages.)
 *
 *   JSON. The metadata is compared byte for byte, so this emits exactly what
 *   json.dumps(meta, separators=(",",":")) emits: no spaces, keys in
 *   insertion order, and ensure_ascii escaping of everything outside
 *   0x20..0x7e as \uXXXX with surrogate pairs above the BMP.
 */

#include "ppz.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ESCAPE      255
#define DICT_MAX    (1 << 16)
#define MI_SAMPLE   40000
#define MI_MIN_SAMPLE 1500
#define MI_BUDGET   150000000LL
#define MIN_2D_GROUP 3
#define MIN_ROWS_FOR_PARENTS 8
#define MIN_ROWS_FOR_2D 3
#define INT_LIMIT   (((int64_t)1) << 62)

/* ------------------------------------------------------- numpy pairwise sum */

/* numpy's pairwise summation, from loops.c.src. Reproduced because the
 * entropy comparisons in the parent search are sensitive to the last bit. */
#define PW_BLOCKSIZE 128

static double pairwise_sum(const double *a, size_t n)
{
    if (n < 8) {
        double res = 0.0;
        for (size_t i = 0; i < n; i++) res += a[i];
        return res;
    }
    if (n <= PW_BLOCKSIZE) {
        double r[8];
        size_t i;
        for (i = 0; i < 8; i++) r[i] = a[i];
        for (i = 8; i < n - (n % 8); i += 8) {
            r[0] += a[i + 0]; r[1] += a[i + 1];
            r[2] += a[i + 2]; r[3] += a[i + 3];
            r[4] += a[i + 4]; r[5] += a[i + 5];
            r[6] += a[i + 6]; r[7] += a[i + 7];
        }
        double res = ((r[0] + r[1]) + (r[2] + r[3]))
                   + ((r[4] + r[5]) + (r[6] + r[7]));
        for (; i < n; i++) res += a[i];
        return res;
    }
    size_t n2 = n / 2;
    n2 -= n2 % 8;
    return pairwise_sum(a, n2) + pairwise_sum(a + n2, n - n2);
}

/* ------------------------------------------------------------------ varints */

static uint64_t zigzag(int64_t v)
{
    return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63);
}

/* Mirror of fast.packed_len: one byte per value, plus a 4- or 8-byte tail
 * entry for each escape. The width is 8 only if some escaped value needs it. */
static size_t packed_len(const int64_t *a, size_t n)
{
    size_t esc = 0;
    int wide = 0;
    for (size_t i = 0; i < n; i++) {
        uint64_t u = zigzag(a[i]);
        if (u >= ESCAPE) {
            esc++;
            if (u >= ((uint64_t)1 << 32)) wide = 1;
        }
    }
    return 1 + n + (size_t)(wide ? 8 : 4) * esc;
}

static void pack_ints(const int64_t *a, size_t n, Buf *out)
{
    int wide = 0;
    for (size_t i = 0; i < n; i++) {
        uint64_t u = zigzag(a[i]);
        if (u >= ESCAPE && u >= ((uint64_t)1 << 32)) { wide = 1; break; }
    }
    buf_putc(out, wide ? 8 : 4);
    size_t head_at = out->len;
    buf_need(out, n);
    out->len += n;
    Buf tail;
    buf_init(&tail);
    for (size_t i = 0; i < n; i++) {
        uint64_t u = zigzag(a[i]);
        if (u < ESCAPE) {
            out->data[head_at + i] = (uint8_t)u;
        } else {
            out->data[head_at + i] = ESCAPE;
            if (wide) {
                buf_put(&tail, &u, 8);
            } else {
                uint32_t v = (uint32_t)u;
                buf_put(&tail, &v, 4);
            }
        }
    }
    buf_put(out, tail.data, tail.len);
    buf_free(&tail);
}

/* k-fold finite difference, in place into a fresh array of length n-k */
static int64_t *diff_n(const int64_t *a, size_t n, int k, size_t *out_n)
{
    int64_t *cur = malloc((n ? n : 1) * sizeof(int64_t));
    if (!cur) return NULL;
    memcpy(cur, a, n * sizeof(int64_t));
    size_t len = n;
    for (int r = 0; r < k; r++) {
        if (len == 0) break;
        for (size_t i = 0; i + 1 < len; i++) cur[i] = cur[i + 1] - cur[i];
        len--;
    }
    *out_n = len;
    return cur;
}

static int diff_order(const int64_t *a, size_t n)
{
    size_t best_cost = packed_len(a, n);
    int best = 0;
    int hi = (int)(n >= 1 ? (n - 1) : 0);
    if (hi > 4) hi = 4;
    for (int k = 1; k <= hi; k++) {
        size_t dn = 0;
        int64_t *d = diff_n(a, n, k, &dn);
        if (!d) break;
        size_t c = packed_len(d, dn) + 8 * (size_t)k;
        free(d);
        if (c < best_cost) { best_cost = c; best = k; }
    }
    return best;
}

/* ------------------------------------------------------------ stable argsort */

typedef struct { int64_t v; size_t i; } KVI;

static int kvi_cmp(const void *a, const void *b)
{
    const KVI *x = a, *y = b;
    if (x->v < y->v) return -1;
    if (x->v > y->v) return 1;
    return x->i < y->i ? -1 : (x->i > y->i ? 1 : 0);
}

/* ------------------------------------------------------------------ strings */

static int str_cmp(const void *pa, const void *pb)
{
    const Str *a = pa, *b = pb;
    size_t n = a->n < b->n ? a->n : b->n;
    int r = n ? memcmp(a->p, b->p, n) : 0;
    if (r) return r;
    /* Python compares str by codepoint; UTF-8 byte order preserves codepoint
     * order, and a prefix sorts before the longer string. */
    return a->n < b->n ? -1 : (a->n > b->n ? 1 : 0);
}

static int str_eq(Str a, Str b)
{
    return a.n == b.n && (a.n == 0 || !memcmp(a.p, b.p, a.n));
}

static long str_bsearch(const Str *sorted, size_t n, Str key)
{
    size_t lo = 0, hi = n;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        int c = str_cmp(&sorted[mid], &key);
        if (c == 0) return (long)mid;
        if (c < 0) lo = mid + 1;
        else hi = mid;
    }
    return -1;
}

/* ------------------------------------------------------------------- numeric */

/* Same rules as tcz.c scan_decimals: optional sign, digits, optional
 * fraction; the whole field must be consumed. */
static int scan_decimals_cell(Str s, int *dec_out)
{
    size_t p = 0;
    int digits = 0, dec = 0;
    if (p < s.n && s.p[p] == '-') p++;
    while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') { p++; digits++; }
    if (p < s.n && s.p[p] == '.') {
        p++;
        while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') { p++; dec++; }
        if (dec == 0) return -1;
    }
    if (digits == 0 || p != s.n) return -1;
    *dec_out = dec;
    return 0;
}

static const int64_t POW10[19] = {
    1LL, 10LL, 100LL, 1000LL, 10000LL, 100000LL, 1000000LL, 10000000LL,
    100000000LL, 1000000000LL, 10000000000LL, 100000000000LL,
    1000000000000LL, 10000000000000LL, 100000000000000LL,
    1000000000000000LL, 10000000000000000LL, 100000000000000000LL,
    1000000000000000000LL
};

static int parse_fixed_cell(Str s, int dec, int64_t *out)
{
    size_t p = 0;
    int neg = 0, seen = 0, used = 0;
    int64_t v = 0;
    if (p < s.n && s.p[p] == '-') { neg = 1; p++; }
    while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') {
        v = v * 10 + (s.p[p] - '0');
        if (v >= INT_LIMIT) return -1;
        p++; seen = 1;
    }
    if (!seen) return -1;
    if (p < s.n && s.p[p] == '.') {
        p++;
        while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') {
            if (used < dec) {
                v = v * 10 + (s.p[p] - '0');
                if (v >= INT_LIMIT) return -1;
                used++;
            }
            p++;
        }
    }
    if (p != s.n) return -1;
    while (used < dec) {
        v *= 10;
        if (v >= INT_LIMIT) return -1;
        used++;
    }
    *out = neg ? -v : v;
    return 0;
}

static void fmt_fixed_one(Buf *b, int64_t v, int dec)
{
    char digits[24];
    if (v < 0) { buf_putc(b, '-'); v = -v; }
    int64_t scale = dec ? POW10[dec] : 1;
    int64_t q = dec ? v / scale : v;
    int64_t r = dec ? v % scale : 0;
    int nd = 0;
    if (q == 0) digits[nd++] = '0';
    while (q > 0) { digits[nd++] = (char)('0' + (q % 10)); q /= 10; }
    while (nd > 0) buf_putc(b, digits[--nd]);
    if (dec) {
        buf_putc(b, '.');
        for (int d = dec - 1; d >= 0; d--)
            buf_putc(b, (char)('0' + ((r / POW10[d]) % 10)));
    }
}

/* Decimal text -> scaled integers, or NULL. Exactness is checked the way
 * caccel.parse_column checks it: format the parsed values back and compare
 * the bytes. That is what rejects leading zeros, "-0.0" and ragged decimals. */
static int64_t *numeric_column(const Table *t, size_t col, int *dec_out)
{
    size_t n = t->nrows;
    if (n == 0) return NULL;
    int dec = 0;
    for (size_t i = 0; i < n; i++) {
        int d = 0;
        if (scan_decimals_cell(table_at(t, i, col), &d)) return NULL;
        if (d > dec) dec = d;
    }
    if (dec > 18) return NULL;

    int64_t *out = malloc(n * sizeof(int64_t));
    if (!out) return NULL;
    for (size_t i = 0; i < n; i++) {
        if (parse_fixed_cell(table_at(t, i, col), dec, &out[i])) {
            free(out);
            return NULL;
        }
    }
    /* round-trip check */
    Buf chk;
    buf_init(&chk);
    for (size_t i = 0; i < n; i++) {
        Str s = table_at(t, i, col);
        size_t at = chk.len;
        fmt_fixed_one(&chk, out[i], dec);
        if (chk.len - at != s.n || memcmp(chk.data + at, s.p, s.n)) {
            buf_free(&chk);
            free(out);
            return NULL;
        }
        chk.len = at;
    }
    buf_free(&chk);
    *dec_out = dec;
    return out;
}

/* --------------------------------------------------------------------- plan */

typedef enum { K_NUM, K_DICT, K_TEXT } Kind;

typedef struct {
    Kind     kind;
    int64_t *ints;      /* K_NUM */
    int      dec;
    Str     *alpha;     /* K_DICT */
    size_t   nalpha;
    int64_t *ids;
    int      k;         /* diff order chosen for K_NUM */
    long     parent;    /* -1 none */
    int      in_group;
    int      group_idx;
} ColPlan;

static void plan_free(ColPlan *p, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        free(p[i].ints);
        free(p[i].alpha);
        free(p[i].ids);
    }
    free(p);
}

static ColPlan *classify(const Table *t)
{
    size_t nc = t->ncols, nr = t->nrows;
    ColPlan *plan = calloc(nc ? nc : 1, sizeof(ColPlan));
    if (!plan) return NULL;

    for (size_t j = 0; j < nc; j++) {
        plan[j].parent = -1;
        plan[j].group_idx = -1;
        int dec = 0;
        int64_t *ints = numeric_column(t, j, &dec);
        if (ints) {
            plan[j].kind = K_NUM;
            plan[j].ints = ints;
            plan[j].dec = dec;
            continue;
        }
        /* dictionary if the distinct count is small enough */
        Str *tmp = malloc((nr ? nr : 1) * sizeof(Str));
        if (!tmp) { plan_free(plan, nc); return NULL; }
        for (size_t i = 0; i < nr; i++) tmp[i] = table_at(t, i, j);
        qsort(tmp, nr, sizeof(Str), str_cmp);
        size_t u = 0;
        for (size_t i = 0; i < nr; i++)
            if (i == 0 || !str_eq(tmp[i], tmp[u - 1])) tmp[u++] = tmp[i];

        size_t limit = nr > 2 ? nr : 2;
        if (u <= DICT_MAX && u * 2 <= limit) {
            Str *alpha = malloc((u ? u : 1) * sizeof(Str));
            memcpy(alpha, tmp, u * sizeof(Str));
            int64_t *ids = malloc((nr ? nr : 1) * sizeof(int64_t));
            if (!alpha || !ids) { free(tmp); plan_free(plan, nc); return NULL; }
            for (size_t i = 0; i < nr; i++)
                ids[i] = str_bsearch(alpha, u, table_at(t, i, j));
            plan[j].kind = K_DICT;
            plan[j].alpha = alpha;
            plan[j].nalpha = u;
            plan[j].ids = ids;
        } else {
            plan[j].kind = K_TEXT;
        }
        free(tmp);
    }
    return plan;
}

/* --------------------------------------------------------------- 2D groups */

typedef struct { size_t *pos; size_t n; } Group;

static Group *find_2d_groups(ColPlan *plan, size_t nc, size_t nrows,
                             size_t *ngroups)
{
    *ngroups = 0;
    if (nrows < MIN_ROWS_FOR_2D) return NULL;

    Group *out = calloc(nc ? nc : 1, sizeof(Group));
    size_t *run = malloc((nc ? nc : 1) * sizeof(size_t));
    if (!out || !run) { free(out); free(run); return NULL; }
    size_t rn = 0, gn = 0;

    #define FLUSH()                                                        \
        do {                                                               \
            if (rn >= MIN_2D_GROUP) {                                      \
                out[gn].pos = malloc(rn * sizeof(size_t));                 \
                memcpy(out[gn].pos, run, rn * sizeof(size_t));             \
                out[gn].n = rn;                                            \
                gn++;                                                      \
            }                                                              \
            rn = 0;                                                        \
        } while (0)

    for (size_t p = 0; p < nc; p++) {
        if (plan[p].kind != K_NUM || nrows == 0) { FLUSH(); continue; }
        if (rn == 0) { run[rn++] = p; continue; }
        ColPlan *prev = &plan[run[rn - 1]];
        int64_t hi_a = 0, hi_b = 0;
        for (size_t i = 0; i < nrows; i++) {
            int64_t v = prev->ints[i] < 0 ? -prev->ints[i] : prev->ints[i];
            if (v > hi_a) hi_a = v;
            int64_t w = plan[p].ints[i] < 0 ? -plan[p].ints[i] : plan[p].ints[i];
            if (w > hi_b) hi_b = w;
        }
        if (!hi_a) hi_a = 1;
        if (!hi_b) hi_b = 1;
        if (prev->dec == plan[p].dec && hi_a <= 8 * hi_b && hi_b <= 8 * hi_a) {
            run[rn++] = p;
        } else {
            FLUSH();
            run[rn++] = p;
        }
    }
    FLUSH();
    free(run);
    for (size_t g = 0; g < gn; g++)
        for (size_t i = 0; i < out[g].n; i++) {
            plan[out[g].pos[i]].in_group = 1;
            plan[out[g].pos[i]].group_idx = (int)g;
        }
    *ngroups = gn;
    return out;
}

/* ------------------------------------------------------------ parent search */

static double counts_entropy(const int64_t *counts, size_t nc, size_t n)
{
    double *t = malloc((nc ? nc : 1) * sizeof(double));
    if (!t) return 0.0;
    for (size_t i = 0; i < nc; i++) {
        double p = (double)counts[i] / (double)n;
        t[i] = p * log2(p);
    }
    double s = pairwise_sum(t, nc);
    free(t);
    return -s;
}

/* counts of each distinct value in `v`, as np.unique(return_counts) would --
 * i.e. ascending by value, which is the order np.bincount also yields for the
 * nonzero-count subset. Order only affects summation order, which is why it
 * has to match. */
static int i64_cmp(const void *a, const void *b)
{
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return x < y ? -1 : (x > y ? 1 : 0);
}

static int64_t *value_counts(const int64_t *v, size_t n, size_t *out_n)
{
    int64_t *s = malloc((n ? n : 1) * sizeof(int64_t));
    if (!s) return NULL;
    memcpy(s, v, n * sizeof(int64_t));
    qsort(s, n, sizeof(int64_t), i64_cmp);
    int64_t *c = malloc((n ? n : 1) * sizeof(int64_t));
    if (!c) { free(s); return NULL; }
    size_t k = 0;
    for (size_t i = 0; i < n; ) {
        size_t j = i;
        while (j < n && s[j] == s[i]) j++;
        c[k++] = (int64_t)(j - i);
        i = j;
    }
    free(s);
    *out_n = k;
    return c;
}

static double entropy_of(const int64_t *ys, size_t n)
{
    if (n == 0) return 0.0;
    size_t nc = 0;
    int64_t *c = value_counts(ys, n, &nc);
    if (!c) return 0.0;
    double h = counts_entropy(c, nc, n);
    free(c);
    return h;
}

static double cond_entropy_corrected(const int64_t *xs, const int64_t *ys,
                                     size_t n, int64_t ny)
{
    if (n == 0) return 0.0;
    int64_t *joint = malloc(n * sizeof(int64_t));
    if (!joint) return 0.0;
    for (size_t i = 0; i < n; i++) joint[i] = xs[i] * ny + ys[i];
    size_t jn = 0, mn = 0;
    int64_t *jc = value_counts(joint, n, &jn);
    int64_t *mc = value_counts(xs, n, &mn);
    free(joint);
    if (!jc || !mc) { free(jc); free(mc); return 0.0; }
    double h = counts_entropy(jc, jn, n) - counts_entropy(mc, mn, n);
    double corr = (double)((int64_t)jn - (int64_t)mn)
                / (2.0 * (double)n * log(2.0));
    free(jc); free(mc);
    return h + corr;
}

typedef struct { long *parent; size_t *order; size_t norder; } Parents;

static Parents pick_parents(ColPlan *plan, size_t nc, size_t nrows)
{
    Parents R = { NULL, NULL, 0 };
    size_t *dict_pos = malloc((nc ? nc : 1) * sizeof(size_t));
    long *parent = malloc((nc ? nc : 1) * sizeof(long));
    size_t *order = malloc((nc ? nc : 1) * sizeof(size_t));
    if (!dict_pos || !parent || !order) {
        free(dict_pos); free(parent); free(order);
        return R;
    }
    for (size_t i = 0; i < nc; i++) parent[i] = -1;
    size_t nd = 0;
    for (size_t p = 0; p < nc; p++)
        if (plan[p].kind == K_DICT) dict_pos[nd++] = p;

    if (nrows < MIN_ROWS_FOR_PARENTS || nd < 2) {
        for (size_t i = 0; i < nd; i++) order[i] = dict_pos[i];
        free(dict_pos);
        R.parent = parent; R.order = order; R.norder = nd;
        return R;
    }

    long long npairs = (long long)nd * (long long)(nd - 1);
    if (npairs < 1) npairs = 1;
    long long rows = MI_BUDGET / npairs;
    if (rows < MI_MIN_SAMPLE) rows = MI_MIN_SAMPLE;
    if (rows > MI_SAMPLE) rows = MI_SAMPLE;
    size_t step = nrows / (size_t)rows;
    if (step < 1) step = 1;
    size_t sn = (nrows + step - 1) / step;

    int64_t **sample = calloc(nd, sizeof(int64_t *));
    double  *base    = calloc(nd, sizeof(double));
    if (!sample || !base) { free(sample); free(base); free(dict_pos);
                            free(parent); free(order); return R; }
    for (size_t i = 0; i < nd; i++) {
        sample[i] = malloc((sn ? sn : 1) * sizeof(int64_t));
        size_t k = 0;
        for (size_t r = 0; r < nrows; r += step) sample[i][k++] = plan[dict_pos[i]].ids[r];
        base[i] = entropy_of(sample[i], sn);
    }

    /* gain[b][a] for a != b, stored dense; -1 means "no usable gain" */
    double *gain = malloc(nd * nd * sizeof(double));
    if (!gain) { for (size_t i = 0; i < nd; i++) free(sample[i]);
                 free(sample); free(base); free(dict_pos); free(parent);
                 free(order); return R; }
    for (size_t i = 0; i < nd * nd; i++) gain[i] = -1.0;
    for (size_t ai = 0; ai < nd; ai++) {
        for (size_t bi = 0; bi < nd; bi++) {
            if (ai == bi) continue;
            double g = base[bi] - cond_entropy_corrected(
                sample[ai], sample[bi], sn,
                (int64_t)plan[dict_pos[bi]].nalpha);
            if (g > 0.05) gain[bi * nd + ai] = g;
        }
    }

    /* root = the lowest-entropy column; min() over a list takes the first */
    size_t root = 0;
    for (size_t i = 1; i < nd; i++) if (base[i] < base[root]) root = i;

    char *placed = calloc(nd, 1);
    size_t *remaining = malloc(nd * sizeof(size_t));
    size_t rn = 0;
    for (size_t i = 0; i < nd; i++) if (i != root) remaining[rn++] = i;

    placed[root] = 1;
    order[0] = dict_pos[root];
    parent[dict_pos[root]] = -1;
    size_t no = 1;

    while (rn) {
        long bb = -1, ba = -1;
        double bg = 0.0;
        /* iterate remaining in column order, and `a` in column order too --
         * strictly greater, so the first maximum wins */
        for (size_t ri = 0; ri < rn; ri++) {
            size_t b = remaining[ri];
            for (size_t a = 0; a < nd; a++) {
                if (a == b || !placed[a]) continue;
                double g = gain[b * nd + a];
                if (g < 0.0) continue;
                if (bb < 0 || g > bg) { bb = (long)b; ba = (long)a; bg = g; }
            }
        }
        size_t chosen;
        if (bb < 0) {
            size_t mi = 0;
            for (size_t ri = 1; ri < rn; ri++)
                if (base[remaining[ri]] < base[remaining[mi]]) mi = ri;
            chosen = remaining[mi];
            parent[dict_pos[chosen]] = -1;
        } else {
            chosen = (size_t)bb;
            parent[dict_pos[chosen]] = (long)dict_pos[ba];
        }
        placed[chosen] = 1;
        order[no++] = dict_pos[chosen];
        for (size_t ri = 0; ri < rn; ri++)
            if (remaining[ri] == chosen) {
                memmove(remaining + ri, remaining + ri + 1,
                        (rn - ri - 1) * sizeof(size_t));
                rn--;
                break;
            }
    }

    for (size_t i = 0; i < nd; i++) free(sample[i]);
    free(sample); free(base); free(gain); free(placed); free(remaining);
    free(dict_pos);
    R.parent = parent; R.order = order; R.norder = no;
    return R;
}

/* --------------------------------------------------------------------- json */

/* Exactly json.dumps' ensure_ascii escaping: anything outside 0x20..0x7e, plus
 * quote and backslash, becomes an escape; non-BMP becomes a surrogate pair. */
static void json_str(Buf *b, const char *s, size_t n)
{
    buf_putc(b, '"');
    for (size_t i = 0; i < n; ) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"')  { buf_put(b, "\\\"", 2); i++; continue; }
        if (c == '\\') { buf_put(b, "\\\\", 2); i++; continue; }
        if (c == '\n') { buf_put(b, "\\n", 2); i++; continue; }
        if (c == '\t') { buf_put(b, "\\t", 2); i++; continue; }
        if (c == '\r') { buf_put(b, "\\r", 2); i++; continue; }
        if (c == '\b') { buf_put(b, "\\b", 2); i++; continue; }
        if (c == '\f') { buf_put(b, "\\f", 2); i++; continue; }
        if (c >= 0x20 && c <= 0x7e) { buf_putc(b, (char)c); i++; continue; }
        if (c < 0x20) {
            char tmp[8];
            snprintf(tmp, sizeof(tmp), "\\u%04x", c);
            buf_put(b, tmp, 6);
            i++;
            continue;
        }
        /* decode one UTF-8 sequence */
        unsigned cp = 0;
        int len = 1;
        if ((c & 0xE0) == 0xC0) { cp = c & 0x1Fu; len = 2; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0Fu; len = 3; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07u; len = 4; }
        else { cp = c; len = 1; }
        if (i + (size_t)len > n) { len = 1; cp = c; }
        for (int k = 1; k < len; k++)
            cp = (cp << 6) | ((unsigned char)s[i + k] & 0x3Fu);
        i += (size_t)len;
        char tmp[16];
        if (cp >= 0x10000) {
            unsigned v = cp - 0x10000;
            snprintf(tmp, sizeof(tmp), "\\u%04x\\u%04x",
                     0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF));
            buf_put(b, tmp, 12);
        } else {
            snprintf(tmp, sizeof(tmp), "\\u%04x", cp);
            buf_put(b, tmp, 6);
        }
    }
    buf_putc(b, '"');
}

static void json_int(Buf *b, long long v)
{
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%lld", v);
    buf_put(b, tmp, (size_t)n);
}

/* ------------------------------------------------------------------ encode */

typedef struct { size_t n, b; int nl; } SMeta;

int ppz_encode(const Table *t, Buf *out);

int ppz_encode(const Table *t, Buf *out)
{
    size_t nc = t->ncols, nr = t->nrows;
    ColPlan *plan = classify(t);
    if (!plan) return -1;

    size_t ngroups = 0;
    Group *groups = find_2d_groups(plan, nc, nr, &ngroups);
    Parents P = pick_parents(plan, nc, nr);
    if (!P.parent) { plan_free(plan, nc); return -1; }

    Buf bins;           /* concatenated binary payloads */
    buf_init(&bins);
    size_t *binsz = malloc((nc * 2 + ngroups + 8) * sizeof(size_t));
    size_t nbins = 0;

    /* string groups, in the order the decoder expects them */
    Str **sg = calloc(nc * 2 + 8, sizeof(Str *));
    size_t *sgn = calloc(nc * 2 + 8, sizeof(size_t));
    size_t nsg = 0;

    /* dictionary columns, in parent-before-child order */
    for (size_t oi = 0; oi < P.norder; oi++) {
        size_t pos = P.order[oi];
        ColPlan *c = &plan[pos];
        int64_t *ids = c->ids;
        int64_t *tmp = NULL;
        long par = P.parent[pos];
        if (par >= 0) {
            /* stable argsort of the parent's ids, then ids = ids[perm].
             * Stability is folded into the comparison as a tiebreak on the
             * original index, so qsort -- which is not stable -- still
             * reproduces numpy's argsort(kind="stable") exactly. */
            KVI *kv = malloc((nr ? nr : 1) * sizeof(KVI));
            int64_t *pv = plan[par].ids;
            for (size_t i = 0; i < nr; i++) { kv[i].v = pv[i]; kv[i].i = i; }
            qsort(kv, nr, sizeof(KVI), kvi_cmp);
            tmp = malloc((nr ? nr : 1) * sizeof(int64_t));
            for (size_t i = 0; i < nr; i++) tmp[i] = ids[kv[i].i];
            free(kv);
            ids = tmp;
        }
        int wb = c->nalpha <= 256 ? 1 : (c->nalpha <= 65536 ? 2 : 4);
        size_t at = bins.len;
        for (size_t i = 0; i < nr; i++) {
            uint32_t v = (uint32_t)ids[i];
            buf_put(&bins, &v, (size_t)wb);
        }
        binsz[nbins++] = bins.len - at;
        free(tmp);
        sg[nsg] = c->alpha;
        sgn[nsg] = c->nalpha;
        nsg++;
        plan[pos].k = wb;                 /* stash width for the metadata */
    }

    /* text columns and standalone numeric columns, in positional order */
    Str **text_cells = calloc(nc ? nc : 1, sizeof(Str *));
    for (size_t pos = 0; pos < nc; pos++) {
        ColPlan *c = &plan[pos];
        if (c->kind == K_TEXT) {
            Str *cells = malloc((nr ? nr : 1) * sizeof(Str));
            for (size_t i = 0; i < nr; i++) cells[i] = table_at(t, i, pos);
            text_cells[pos] = cells;
            sg[nsg] = cells;
            sgn[nsg] = nr;
            nsg++;
        } else if (c->kind == K_NUM && !c->in_group) {
            int k = diff_order(c->ints, nr);
            c->k = k;
            size_t dn = 0;
            int64_t *d = k ? diff_n(c->ints, nr, k, &dn) : NULL;
            size_t at = bins.len;
            pack_ints(k ? d : c->ints, k ? dn : nr, &bins);
            binsz[nbins++] = bins.len - at;
            free(d);
        }
    }

    /* 2D groups */
    for (size_t g = 0; g < ngroups; g++) {
        size_t w = groups[g].n;
        size_t total = w + (nr - 1) + (nr - 1) * (w - 1);
        int64_t *flat = malloc(total * sizeof(int64_t));
        /* M[0] row, then first-column differences, then the double difference */
        for (size_t c = 0; c < w; c++) flat[c] = plan[groups[g].pos[c]].ints[0];
        for (size_t i = 1; i < nr; i++)
            flat[w + (i - 1)] = plan[groups[g].pos[0]].ints[i]
                              - plan[groups[g].pos[0]].ints[i - 1];
        size_t at2 = w + (nr - 1);
        for (size_t i = 1; i < nr; i++)
            for (size_t c = 1; c < w; c++) {
                int64_t d1 = plan[groups[g].pos[c]].ints[i]
                           - plan[groups[g].pos[c]].ints[i - 1];
                int64_t d0 = plan[groups[g].pos[c - 1]].ints[i]
                           - plan[groups[g].pos[c - 1]].ints[i - 1];
                flat[at2 + (i - 1) * (w - 1) + (c - 1)] = d1 - d0;
            }
        size_t at = bins.len;
        pack_ints(flat, total, &bins);
        binsz[nbins++] = bins.len - at;
        free(flat);
    }

    /* ------------------------------------------------- pack the strings */
    Buf txt;
    buf_init(&txt);
    SMeta *smeta = calloc(nsg ? nsg : 1, sizeof(SMeta));
    Buf lenbins;
    buf_init(&lenbins);
    size_t *lenbinsz = malloc((nsg + 1) * sizeof(size_t));
    size_t nlenbins = 0;

    for (size_t g = 0; g < nsg; g++) {
        int has_nl = 0;
        for (size_t i = 0; i < sgn[g]; i++)
            if (memchr(sg[g][i].p, '\n', sg[g][i].n)) { has_nl = 1; break; }
        size_t at = txt.len;
        if (has_nl) {
            int64_t *lens = malloc((sgn[g] ? sgn[g] : 1) * sizeof(int64_t));
            for (size_t i = 0; i < sgn[g]; i++) {
                buf_put(&txt, sg[g][i].p, sg[g][i].n);
                lens[i] = (int64_t)sg[g][i].n;
            }
            size_t la = lenbins.len;
            pack_ints(lens, sgn[g], &lenbins);
            lenbinsz[nlenbins++] = lenbins.len - la;
            free(lens);
            smeta[g].nl = 0;
        } else {
            for (size_t i = 0; i < sgn[g]; i++) {
                if (i) buf_putc(&txt, '\n');
                buf_put(&txt, sg[g][i].p, sg[g][i].n);
            }
            smeta[g].nl = 1;
        }
        smeta[g].n = sgn[g];
        smeta[g].b = txt.len - at;
    }
    /* length arrays always go last in the binary payload */
    buf_put(&bins, lenbins.data, lenbins.len);
    for (size_t i = 0; i < nlenbins; i++) binsz[nbins++] = lenbinsz[i];
    buf_free(&lenbins);
    free(lenbinsz);

    /* ------------------------------------------------------- metadata */
    Buf meta;
    buf_init(&meta);
    buf_put(&meta, "{\"columns\":[", 12);
    for (size_t j = 0; j < nc; j++) {
        if (j) buf_putc(&meta, ',');
        json_str(&meta, t->names[j], strlen(t->names[j]));
    }
    buf_put(&meta, "],\"nrows\":", 10);
    json_int(&meta, (long long)nr);
    buf_put(&meta, ",\"cols\":[", 9);
    for (size_t pos = 0; pos < nc; pos++) {
        if (pos) buf_putc(&meta, ',');
        ColPlan *c = &plan[pos];
        if (c->kind == K_DICT) {
            buf_put(&meta, "{\"kind\":\"dict\",\"n\":", 19);
            json_int(&meta, (long long)c->nalpha);
            buf_put(&meta, ",\"w\":\"<u", 8);
            json_int(&meta, c->k);
            buf_put(&meta, "\",\"parent\":", 11);
            if (P.parent[pos] < 0) buf_put(&meta, "null", 4);
            else json_int(&meta, (long long)P.parent[pos]);
            buf_putc(&meta, '}');
        } else if (c->kind == K_TEXT) {
            buf_put(&meta, "{\"kind\":\"text\"}", 15);
        } else if (c->in_group) {
            buf_put(&meta, "{\"kind\":\"grp\",\"dec\":", 20);
            json_int(&meta, c->dec);
            buf_put(&meta, ",\"g\":", 5);
            json_int(&meta, c->group_idx);
            buf_putc(&meta, '}');
        } else {
            buf_put(&meta, "{\"kind\":\"num\",\"dec\":", 20);
            json_int(&meta, c->dec);
            buf_put(&meta, ",\"k\":", 5);
            json_int(&meta, c->k);
            buf_put(&meta, ",\"warm\":[", 9);
            for (int i = 0; i < c->k; i++) {
                if (i) buf_putc(&meta, ',');
                json_int(&meta, (long long)c->ints[i]);
            }
            buf_put(&meta, "]}", 2);
        }
    }
    buf_put(&meta, "],\"groups\":[", 12);
    for (size_t g = 0; g < ngroups; g++) {
        if (g) buf_putc(&meta, ',');
        buf_putc(&meta, '[');
        for (size_t i = 0; i < groups[g].n; i++) {
            if (i) buf_putc(&meta, ',');
            json_int(&meta, (long long)groups[g].pos[i]);
        }
        buf_putc(&meta, ']');
    }
    buf_put(&meta, "],\"order\":[", 11);
    for (size_t i = 0; i < P.norder; i++) {
        if (i) buf_putc(&meta, ',');
        json_int(&meta, (long long)P.order[i]);
    }
    buf_put(&meta, "],\"bins\":[", 10);
    for (size_t i = 0; i < nbins; i++) {
        if (i) buf_putc(&meta, ',');
        json_int(&meta, (long long)binsz[i]);
    }
    buf_put(&meta, "],\"smeta\":[", 11);
    for (size_t g = 0; g < nsg; g++) {
        if (g) buf_putc(&meta, ',');
        buf_put(&meta, "{\"n\":", 5);
        json_int(&meta, (long long)smeta[g].n);
        buf_put(&meta, ",\"b\":", 5);
        json_int(&meta, (long long)smeta[g].b);
        buf_put(&meta, ",\"nl\":", 6);
        buf_put(&meta, smeta[g].nl ? "true" : "false", smeta[g].nl ? 4 : 5);
        buf_putc(&meta, '}');
    }
    buf_put(&meta, "],\"nlenbins\":", 13);
    json_int(&meta, (long long)nlenbins);
    buf_putc(&meta, '}');

    /* ------------------------------------------------------- container */
    Buf mz, bz, tz;
    buf_init(&mz); buf_init(&bz); buf_init(&tz);
    int rc = -1;
    if (ppz_lzma_compress(meta.data, meta.len, &mz)) goto done;
    if (ppz_lzma_compress(bins.data, bins.len, &bz)) goto done;
    if (ppz_lzma_compress(txt.data, txt.len, &tz)) goto done;

    buf_free(out);
    buf_put(out, PPZ_MAGIC, 4);
    for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((mz.len >> s) & 0xFF));
    for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((bz.len >> s) & 0xFF));
    for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((tz.len >> s) & 0xFF));
    buf_put(out, mz.data, mz.len);
    buf_put(out, bz.data, bz.len);
    buf_put(out, tz.data, tz.len);
    rc = 0;

done:
    buf_free(&mz); buf_free(&bz); buf_free(&tz);
    buf_free(&meta); buf_free(&txt); buf_free(&bins);
    free(smeta); free(binsz); free(sg); free(sgn);
    for (size_t j = 0; j < nc; j++) free(text_cells[j]);
    free(text_cells);
    for (size_t g = 0; g < ngroups; g++) free(groups[g].pos);
    free(groups);
    free(P.parent); free(P.order);
    plan_free(plan, nc);
    return rc;
}
