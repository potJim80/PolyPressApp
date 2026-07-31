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
    /* Check before multiplying, not after. `v >= INT_LIMIT` following
     * `v = v*10 + d` never fires when the multiply already overflowed --
     * signed overflow is undefined and wraps negative in practice. Same fix
     * as tcz.c; the two must agree or the accelerated and unaccelerated
     * Python paths disagree about which columns are numeric. */
    #define ACC_DIGIT(d)                                                     \
        do {                                                                 \
            int dgt_ = (d);                                                  \
            if (v > (INT_LIMIT - 1 - dgt_) / 10) return -1;                  \
            v = v * 10 + dgt_;                                               \
        } while (0)

    if (p < s.n && s.p[p] == '-') { neg = 1; p++; }
    while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') {
        ACC_DIGIT(s.p[p] - '0');
        p++; seen = 1;
    }
    if (!seen) return -1;
    if (p < s.n && s.p[p] == '.') {
        p++;
        while (p < s.n && s.p[p] >= '0' && s.p[p] <= '9') {
            if (used < dec) {
                ACC_DIGIT(s.p[p] - '0');
                used++;
            }
            p++;
        }
    }
    if (p != s.n) return -1;
    while (used < dec) {
        ACC_DIGIT(0);
        used++;
    }
    #undef ACC_DIGIT
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

/* A column that is numeric apart from a few cells that are not. Mirrors
 * fast._numeric_lenient exactly.
 *
 * The numeric test is all or nothing, and that is expensive. The Treasury
 * yield curve contains four blank cells in 72,048 and those four drop all
 * eight rate columns to the dictionary path: 59,309 B instead of 34,891 B.
 * One "-0.0" in 26,304 cells disqualifies a temperature column the same way.
 *
 * Exception slots are FORWARD-FILLED from the previous good value so the
 * column keeps its full length and can still join a planar group -- worth
 * another 18% on the yield curve beyond the 28% for being numeric at all.
 * The fill values are never seen; the decoder overwrites those positions.
 *
 * The decimal count is the most common one, ties to the smaller, so the
 * choice cannot depend on iteration order. Cells with more than 18 decimals
 * are counted as exception candidates rather than binned, which keeps this
 * histogram a fixed array and keeps Python in step. */
#define EX_MAX_DEC 18
#define EX_FRAC_NUM 5
#define EX_FRAC_DEN 100

static int64_t *numeric_column_lenient(const Table *t, size_t col, int *dec_out,
                                       size_t **expos_out, size_t *nex_out)
{
    size_t n = t->nrows;
    *expos_out = NULL;
    *nex_out = 0;
    if (n == 0) return NULL;
    size_t limit = n * EX_FRAC_NUM / EX_FRAC_DEN;

    size_t counts[EX_MAX_DEC + 1];
    for (int i = 0; i <= EX_MAX_DEC; i++) counts[i] = 0;
    size_t bad = 0, any = 0;
    for (size_t i = 0; i < n; i++) {
        int d = 0;
        if (scan_decimals_cell(table_at(t, i, col), &d) || d > EX_MAX_DEC) {
            if (++bad > limit) return NULL;
            continue;
        }
        counts[d]++;
        any = 1;
    }
    if (!any) return NULL;
    int dec = 0;
    for (int d = 1; d <= EX_MAX_DEC; d++)
        if (counts[d] > counts[dec]) dec = d;   /* ties keep the smaller d */

    int64_t *out = malloc(n * sizeof(int64_t));
    char *isex = calloc(n ? n : 1, 1);
    size_t *expos = malloc((limit + 1) * sizeof(size_t));
    if (!out || !isex || !expos) { free(out); free(isex); free(expos);
                                   return NULL; }
    size_t nex = 0;
    Buf chk;
    buf_init(&chk);
    for (size_t i = 0; i < n; i++) {
        Str s = table_at(t, i, col);
        int64_t v = 0;
        int ok = parse_fixed_cell(s, dec, &v) == 0;
        if (ok) {
            size_t at = chk.len;
            fmt_fixed_one(&chk, v, dec);
            ok = (chk.len - at == s.n) && memcmp(chk.data + at, s.p, s.n) == 0;
            chk.len = at;
        }
        if (ok) {
            out[i] = v;
        } else {
            if (nex >= limit + 1) { ok = 0; goto too_many; }
            expos[nex++] = i;
            isex[i] = 1;
            if (nex > limit) goto too_many;
        }
    }
    buf_free(&chk);
    if (nex == 0) { free(out); free(isex); free(expos); return NULL; }

    /* forward fill, then patch a leading run with the first good value */
    int have = 0;
    int64_t fill = 0, first_good = 0;
    for (size_t i = 0; i < n; i++) {
        if (isex[i]) { if (have) out[i] = fill; }
        else { fill = out[i]; if (!have) { first_good = out[i]; have = 1; } }
    }
    if (!have) { free(out); free(isex); free(expos); return NULL; }
    for (size_t i = 0; i < n; i++) {
        if (!isex[i]) break;
        out[i] = first_good;
    }

    free(isex);
    *dec_out = dec;
    *expos_out = expos;
    *nex_out = nex;
    return out;

too_many:
    buf_free(&chk);
    free(out); free(isex); free(expos);
    return NULL;
}

/* --------------------------------------------------------------------- plan */

typedef enum { K_NUM, K_DICT, K_TEXT } Kind;

static int diff_order(const int64_t *a, size_t n);
static int64_t *diff_n(const int64_t *a, size_t n, int k, size_t *out_n);
static int str_cmp(const void *pa, const void *pb);
static int str_eq(Str a, Str b);
static long str_bsearch(const Str *sorted, size_t n, Str key);
static void pack_ints(const int64_t *a, size_t n, Buf *out);

/* Could storing this column as numbers possibly beat leaving it alone?
 * Mirrors fast._lenient_promising exactly -- it decides whether the whole-file
 * guard runs, so a disagreement here is a different archive.
 *
 * The guard costs a second full encode, and that second encode IS the cost:
 * the lenient scan is 0.04-0.09s where the extra encode is 0.9-1.4s. Four
 * large datasets used to encode twice, find nothing, and keep the first result.
 *
 * One-sided by construction. The alternative is deliberately OVER-estimated --
 * it charges for the dictionary's alphabet and ignores the reorder parent that
 * would make the column cheaper still -- so `numeric >= estimate` implies
 * `numeric >= real`, and declining on that basis cannot discard a win. */
static int lenient_promising(const Table *t, size_t col, const int64_t *a,
                             size_t n, const size_t *expos, size_t nex)
{
    int k = diff_order(a, n);
    size_t dn = 0;
    int64_t *d = k ? diff_n(a, n, k, &dn) : NULL;
    Buf pb;
    buf_init(&pb);
    pack_ints(k ? d : a, k ? dn : n, &pb);
    size_t num = ppz_lzma_probe_len(pb.data, pb.len);
    buf_free(&pb);
    free(d);

    int64_t *gaps = malloc((nex ? nex : 1) * sizeof(int64_t));
    if (!gaps) return 1;                 /* cannot screen -> let the guard run */
    size_t prev = 0;
    for (size_t i = 0; i < nex; i++) {
        gaps[i] = (int64_t)expos[i] - (int64_t)prev;
        prev = expos[i];
    }
    buf_init(&pb);
    pack_ints(gaps, nex, &pb);
    num += ppz_lzma_probe_len(pb.data, pb.len);
    buf_free(&pb);
    free(gaps);

    /* the alternative: the same distinct-count test classify() applies */
    Str *tmp = malloc((n ? n : 1) * sizeof(Str));
    if (!tmp) return 1;
    for (size_t i = 0; i < n; i++) tmp[i] = table_at(t, i, col);
    qsort(tmp, n, sizeof(Str), str_cmp);
    size_t u = 0;
    for (size_t i = 0; i < n; i++)
        if (i == 0 || !str_eq(tmp[i], tmp[u - 1])) tmp[u++] = tmp[i];

    size_t limit = n > 2 ? n : 2;
    size_t alt;
    if (u <= DICT_MAX && u * 2 <= limit) {
        int w = u <= 256 ? 1 : (u <= 65536 ? 2 : 4);
        Buf idb;
        buf_init(&idb);
        for (size_t i = 0; i < n; i++) {
            long id = str_bsearch(tmp, u, table_at(t, i, col));
            uint64_t v = (uint64_t)(id < 0 ? 0 : id);
            for (int b = 0; b < w; b++)
                buf_putc(&idb, (char)((v >> (8 * b)) & 0xFF));
        }
        alt = ppz_lzma_probe_len(idb.data, idb.len);
        buf_free(&idb);
        Buf ab;
        buf_init(&ab);
        for (size_t i = 0; i < u; i++) {
            if (i) buf_putc(&ab, '\n');
            buf_put(&ab, tmp[i].p, tmp[i].n);
        }
        alt += ppz_lzma_probe_len(ab.data, ab.len);
        buf_free(&ab);
    } else {
        Buf cb;
        buf_init(&cb);
        for (size_t i = 0; i < n; i++) {
            if (i) buf_putc(&cb, '\n');
            Str s = table_at(t, i, col);
            buf_put(&cb, s.p, s.n);
        }
        alt = ppz_lzma_probe_len(cb.data, cb.len);
        buf_free(&cb);
    }
    free(tmp);
    return num < alt;
}

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
    size_t  *ex_pos;    /* rows this numeric column could not represent */
    size_t   nex;       /* the strings themselves are read back from the table */
    int      ex_prom;   /* could the lenient form possibly win? nominator only */
} ColPlan;

static void plan_free(ColPlan *p, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        free(p[i].ints);
        free(p[i].alpha);
        free(p[i].ids);
        free(p[i].ex_pos);
    }
    free(p);
}

static ColPlan *classify(const Table *t, int lenient)
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
        if (lenient) {
            size_t *expos = NULL, nex = 0;
            int64_t *lax = numeric_column_lenient(t, j, &dec, &expos, &nex);
            if (lax) {
                plan[j].kind = K_NUM;
                plan[j].ints = lax;
                plan[j].dec = dec;
                plan[j].ex_pos = expos;
                plan[j].nex = nex;
                plan[j].ex_prom = lenient_promising(t, j, lax, nr, expos, nex);
                continue;
            }
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

/* `a <= 8*b` without overflowing.
 *
 * This codec accepts magnitudes up to 2^62, and 8 * 2^62 does not fit in an
 * int64 -- the product wraps negative and the comparison silently says "not
 * commensurable". Python never had to think about it because its ints are
 * arbitrary precision, so the bug existed only on this side, and it cost
 * real compression: every group of large numeric columns was refused, and
 * the archive came out bigger than the Python one for no visible reason.
 * For positive integers, a <= 8b is exactly ceil(a/8) <= b. */
static int le_times8(int64_t a, int64_t b)
{
    return (a + 7) / 8 <= b;
}

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
        if (prev->dec == plan[p].dec &&
            le_times8(hi_a, hi_b) && le_times8(hi_b, hi_a)) {
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

/* H(Y) and the distinct count from one pass -- both are needed per column. */
static double entropy_of(const int64_t *ys, size_t n, size_t *distinct)
{
    if (n == 0) { if (distinct) *distinct = 0; return 0.0; }
    size_t nc = 0;
    int64_t *c = value_counts(ys, n, &nc);
    if (!c) { if (distinct) *distinct = 0; return 0.0; }
    double h = counts_entropy(c, nc, n);
    free(c);
    if (distinct) *distinct = nc;
    return h;
}

/* `hx` and `mx` describe X alone and are hoisted by the caller -- they used
 * to be recomputed for every Y, which on 421 columns meant 420 identical
 * recomputations per column. The arithmetic keeps the same shape so the
 * result is the same double, not merely the same number. */
static double cond_entropy_corrected(const int64_t *xs, const int64_t *ys,
                                     size_t n, int64_t ny,
                                     double hx, size_t mx)
{
    if (n == 0) return 0.0;
    int64_t *joint = malloc(n * sizeof(int64_t));
    if (!joint) return 0.0;
    for (size_t i = 0; i < n; i++) joint[i] = xs[i] * ny + ys[i];
    size_t jn = 0;
    int64_t *jc = value_counts(joint, n, &jn);
    free(joint);
    if (!jc) return 0.0;
    double h = counts_entropy(jc, jn, n) - hx;
    double corr = (double)((int64_t)jn - (int64_t)mx)
                / (2.0 * (double)n * log(2.0));
    free(jc);
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
    size_t  *distinct = calloc(nd, sizeof(size_t));
    if (!sample || !base || !distinct) {
        free(sample); free(base); free(distinct); free(dict_pos);
        free(parent); free(order); return R; }
    for (size_t i = 0; i < nd; i++) {
        sample[i] = malloc((sn ? sn : 1) * sizeof(int64_t));
        size_t k = 0;
        for (size_t r = 0; r < nrows; r += step) sample[i][k++] = plan[dict_pos[i]].ids[r];
        base[i] = entropy_of(sample[i], sn, &distinct[i]);
    }

    /* gain[b][a] for a != b, stored dense; -1 means "no usable gain" */
    double *gain = malloc(nd * nd * sizeof(double));
    if (!gain) { for (size_t i = 0; i < nd; i++) free(sample[i]);
                 free(sample); free(base); free(distinct); free(dict_pos);
                 free(parent); free(order); return R; }
    for (size_t i = 0; i < nd * nd; i++) gain[i] = -1.0;
    for (size_t ai = 0; ai < nd; ai++) {
        for (size_t bi = 0; bi < nd; bi++) {
            if (ai == bi) continue;
            double g = base[bi] - cond_entropy_corrected(
                sample[ai], sample[bi], sn,
                (int64_t)plan[dict_pos[bi]].nalpha, base[ai], distinct[ai]);
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

    /* Never-worse, the same guard text columns already had.
     *
     * Text parents were chosen by measurement and could decline; dictionary
     * parents were chosen by conditional entropy and taken on trust. That
     * asymmetry did real damage -- every experiment that moved a column out
     * of `text` traded a measured decision for an unmeasured one and lost,
     * and the loss landed on a different column than the one being changed.
     * Entropy stays as the nominator; this checks the nomination pays. */
    for (size_t oi = 0; oi < no; oi++) {
        size_t b = order[oi];
        long a = parent[b];
        if (a < 0) continue;
        size_t alen = plan[b].nalpha;
        int wb = alen <= 256 ? 1 : (alen <= 65536 ? 2 : 4);
        KVI *kv = malloc((nrows ? nrows : 1) * sizeof(KVI));
        int64_t *pv = plan[a].ids;
        if (!kv) continue;
        for (size_t i = 0; i < nrows; i++) { kv[i].v = pv[i]; kv[i].i = i; }
        qsort(kv, nrows, sizeof(KVI), kvi_cmp);
        uint8_t *flat = malloc(nrows * (size_t)wb ? nrows * (size_t)wb : 1);
        uint8_t *perm = malloc(nrows * (size_t)wb ? nrows * (size_t)wb : 1);
        if (!flat || !perm) { free(kv); free(flat); free(perm); continue; }
        for (size_t i = 0; i < nrows; i++) {
            uint32_t v0 = (uint32_t)plan[b].ids[i];
            uint32_t v1 = (uint32_t)plan[b].ids[kv[i].i];
            memcpy(flat + i * (size_t)wb, &v0, (size_t)wb);
            memcpy(perm + i * (size_t)wb, &v1, (size_t)wb);
        }
        free(kv);
        size_t cost_perm = ppz_lzma_probe_len(perm, nrows * (size_t)wb);
        size_t cost_flat = ppz_lzma_probe_len(flat, nrows * (size_t)wb);
        free(flat); free(perm);
        if (cost_perm >= cost_flat) parent[b] = -1;
    }

    for (size_t i = 0; i < nd; i++) free(sample[i]);
    free(sample); free(base); free(distinct);
    free(gain); free(placed); free(remaining);
    free(dict_pos);
    R.parent = parent; R.order = order; R.norder = no;
    return R;
}

/* -------------------------------------------------------- text reordering */

#define TEXT_PARENT_CANDIDATES 5

typedef struct { double g; size_t dp; } Cand;

static int cand_cmp(const void *a, const void *b)
{
    const Cand *x = a, *y = b;
    /* by descending gain, then ascending column index -- matching
     * ranked.sort(key=lambda r: (-r[0], r[1])) on the Python side */
    if (x->g > y->g) return -1;
    if (x->g < y->g) return 1;
    return x->dp < y->dp ? -1 : (x->dp > y->dp ? 1 : 0);
}

/* Compressed length of a column under a given row order, via the cheap probe.
 * `perm` may be NULL for the original order. */
static size_t probe_order(const Table *t, size_t col, const size_t *perm,
                          size_t nr)
{
    Buf b;
    buf_init(&b);
    for (size_t i = 0; i < nr; i++) {
        if (i) buf_putc(&b, '\n');
        Str s = table_at(t, perm ? perm[i] : i, col);
        buf_put(&b, s.p, s.n);
    }
    size_t got = ppz_lzma_probe_len(b.data, b.len);
    buf_free(&b);
    return got;
}

/* Choose a reorder parent for each text column.
 *
 * Text columns were the one place the reordering idea was never applied, and
 * they dominate the datasets this codec does worst on. Mirrors
 * fast.pick_text_parents exactly, including the two-stage shape: conditional
 * entropy only NOMINATES candidates, and the winner is decided by actually
 * compressing. Trusting the score alone made one real dataset 66% larger --
 * entropy measures how often the parent pins the exact value, which is not
 * what shrinks text, and reordering also destroys whatever useful order the
 * file already had. */
static void pick_text_parents(const Table *t, ColPlan *plan, size_t nc,
                              size_t nrows, long *tparent)
{
    for (size_t i = 0; i < nc; i++) tparent[i] = -1;

    size_t *dict_pos = malloc((nc ? nc : 1) * sizeof(size_t));
    size_t *text_pos = malloc((nc ? nc : 1) * sizeof(size_t));
    if (!dict_pos || !text_pos) { free(dict_pos); free(text_pos); return; }
    size_t nd = 0, nt = 0;
    for (size_t p = 0; p < nc; p++) {
        if (plan[p].kind == K_DICT) dict_pos[nd++] = p;
        else if (plan[p].kind == K_TEXT) text_pos[nt++] = p;
    }
    if (nrows < MIN_ROWS_FOR_PARENTS || !nd || !nt) {
        free(dict_pos); free(text_pos);
        return;
    }

    long long npairs = (long long)nt * (long long)nd;
    if (npairs < 1) npairs = 1;
    long long rows = MI_BUDGET / npairs;
    if (rows < MI_MIN_SAMPLE) rows = MI_MIN_SAMPLE;
    if (rows > MI_SAMPLE) rows = MI_SAMPLE;
    size_t step = nrows / (size_t)rows;
    if (step < 1) step = 1;
    size_t sn = (nrows + step - 1) / step;

    int64_t **dsample = calloc(nd, sizeof(int64_t *));
    double  *dbase = calloc(nd, sizeof(double));
    size_t  *ddist = calloc(nd, sizeof(size_t));
    if (!dsample || !dbase || !ddist) goto done;
    for (size_t i = 0; i < nd; i++) {
        dsample[i] = malloc((sn ? sn : 1) * sizeof(int64_t));
        size_t k = 0;
        for (size_t r = 0; r < nrows; r += step)
            dsample[i][k++] = plan[dict_pos[i]].ids[r];
        dbase[i] = entropy_of(dsample[i], sn, &ddist[i]);
    }

    for (size_t ti = 0; ti < nt; ti++) {
        size_t tp = text_pos[ti];

        /* factorise the sampled text column -- used only for scoring */
        Str *tmp = malloc((sn ? sn : 1) * sizeof(Str));
        if (!tmp) break;
        size_t k = 0;
        for (size_t r = 0; r < nrows; r += step) tmp[k++] = table_at(t, r, tp);
        Str *srt = malloc((sn ? sn : 1) * sizeof(Str));
        memcpy(srt, tmp, sn * sizeof(Str));
        qsort(srt, sn, sizeof(Str), str_cmp);
        size_t u = 0;
        for (size_t i = 0; i < sn; i++)
            if (i == 0 || !str_eq(srt[i], srt[u - 1])) srt[u++] = srt[i];
        int64_t *ids = malloc((sn ? sn : 1) * sizeof(int64_t));
        for (size_t i = 0; i < sn; i++) ids[i] = str_bsearch(srt, u, tmp[i]);
        size_t tdist = 0;
        double base_t = entropy_of(ids, sn, &tdist);

        Cand *cands = malloc(nd * sizeof(Cand));
        size_t ncand = 0;
        for (size_t di = 0; di < nd; di++) {
            double g = base_t - cond_entropy_corrected(
                dsample[di], ids, sn, (int64_t)u, dbase[di], ddist[di]);
            if (g > 0.05) { cands[ncand].g = g; cands[ncand].dp = di; ncand++; }
        }
        free(tmp); free(srt); free(ids);
        if (!ncand) { free(cands); continue; }
        qsort(cands, ncand, sizeof(Cand), cand_cmp);

        size_t best_cost = probe_order(t, tp, NULL, nrows);
        long best = -1;
        size_t take = ncand < TEXT_PARENT_CANDIDATES
                    ? ncand : TEXT_PARENT_CANDIDATES;
        for (size_t c = 0; c < take; c++) {
            size_t dp = dict_pos[cands[c].dp];
            KVI *kv = malloc((nrows ? nrows : 1) * sizeof(KVI));
            int64_t *pv = plan[dp].ids;
            for (size_t i = 0; i < nrows; i++) { kv[i].v = pv[i]; kv[i].i = i; }
            qsort(kv, nrows, sizeof(KVI), kvi_cmp);
            size_t *perm = malloc((nrows ? nrows : 1) * sizeof(size_t));
            for (size_t i = 0; i < nrows; i++) perm[i] = kv[i].i;
            free(kv);
            size_t cost = probe_order(t, tp, perm, nrows);
            free(perm);
            if (cost < best_cost) { best_cost = cost; best = (long)dp; }
        }
        free(cands);
        tparent[tp] = best;
    }

done:
    if (dsample) for (size_t i = 0; i < nd; i++) free(dsample[i]);
    free(dsample); free(dbase); free(ddist);
    free(dict_pos); free(text_pos);
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

typedef struct { size_t n, b; int nl; int fc; } SMeta;

/* Sampling cap and nomination floors -- must match fast.py exactly, since they
 * decide which groups get front-coded and therefore which archive is written. */
#define FC_PREFIX_CAP 4000
#define FC_MIN_BYTES  1024
#define FC_MIN_NUM    1
#define FC_MIN_DEN    4

/* Bytes shared with the previous word, and total bytes. Mirrors
 * fast._prefix_stats: byte comparisons, prefix capped at 255, sampled to
 * FC_PREFIX_CAP words. Returned as two integers rather than a ratio so both
 * implementations rank by cross-multiplying and never compare floats. */
static void prefix_stats(const Str *words, size_t count,
                         int64_t *shared, int64_t *total)
{
    *shared = 0;
    *total = 0;
    size_t n = count < FC_PREFIX_CAP ? count : FC_PREFIX_CAP;
    if (n < 2) return;
    Str prev = words[0];
    *total = (int64_t)prev.n;
    for (size_t i = 1; i < n; i++) {
        Str c = words[i];
        size_t m = prev.n < c.n ? prev.n : c.n;
        if (m > 255) m = 255;
        size_t k = 0;
        while (k < m && prev.p[k] == c.p[k]) k++;
        *shared += (int64_t)k;
        *total += (int64_t)c.n;
        prev = c;
    }
}

/* One group into the pile. `front` front-codes it; otherwise newline-joined,
 * or concatenated with an external length array when it contains a newline. */
static void pile_group(Buf *out, const Str *w, size_t count, int has_nl,
                       int front)
{
    if (has_nl) {
        for (size_t i = 0; i < count; i++) buf_put(out, w[i].p, w[i].n);
        return;
    }
    if (!front) {
        for (size_t i = 0; i < count; i++) {
            if (i) buf_putc(out, '\n');
            buf_put(out, w[i].p, w[i].n);
        }
        return;
    }
    for (size_t i = 0; i < count; i++) {
        size_t n = 0;
        if (i) {
            size_t m = w[i - 1].n < w[i].n ? w[i - 1].n : w[i].n;
            if (m > 255) m = 255;
            while (n < m && w[i - 1].p[n] == w[i].p[n]) n++;
        }
        buf_putc(out, (char)(unsigned char)n);
    }
    for (size_t i = 0; i < count; i++) {
        size_t n = 0;
        if (i) {
            size_t m = w[i - 1].n < w[i].n ? w[i - 1].n : w[i].n;
            if (m > 255) m = 255;
            while (n < m && w[i - 1].p[n] == w[i].p[n]) n++;
            buf_putc(out, '\n');
        }
        buf_put(out, w[i].p + n, w[i].n - n);
    }
}

static void build_pile(Str **sg, const size_t *sgn, size_t nsg,
                       const unsigned char *has_nl,
                       const unsigned char *front, Buf *txt, SMeta *sm)
{
    txt->len = 0;
    for (size_t g = 0; g < nsg; g++) {
        size_t at = txt->len;
        pile_group(txt, sg[g], sgn[g], has_nl[g], front[g]);
        sm[g].n = sgn[g];
        sm[g].b = txt->len - at;
        sm[g].nl = has_nl[g] ? 0 : 1;
        sm[g].fc = (!has_nl[g] && front[g]) ? 1 : 0;
    }
}

/* `use_2d` off skips the planar grouping entirely, which is safe because
 * classify() callocs the plan: in_group stays 0 and group_idx stays -1, so
 * every numeric column falls through to its own measured differencing order.
 * `ngroups_out` reports how many groups formed, so the caller knows whether
 * the second encode is worth running at all. */
static int encode_modelled(const Table *t, Buf *out, long *fired,
                           int use_2d, size_t *ngroups_out,
                           int use_lenient, size_t *nlax_out)
{
    if (fired) *fired = 0;
    if (ngroups_out) *ngroups_out = 0;
    if (nlax_out) *nlax_out = 0;
    size_t nc = t->ncols, nr = t->nrows;
    ColPlan *plan = classify(t, use_lenient);
    if (!plan) return -1;
    if (nlax_out) {
        /* only PROMISING columns are reported, because this is what decides
         * whether the caller pays for a second encode */
        size_t nl = 0;
        for (size_t j = 0; j < nc; j++) if (plan[j].nex && plan[j].ex_prom) nl++;
        *nlax_out = nl;
    }

    size_t ngroups = 0;
    Group *groups = use_2d ? find_2d_groups(plan, nc, nr, &ngroups) : NULL;
    if (ngroups_out) *ngroups_out = ngroups;
    Parents P = pick_parents(plan, nc, nr);
    if (!P.parent) { plan_free(plan, nc); return -1; }

    Buf bins;           /* concatenated binary payloads */
    buf_init(&bins);
    /* Three bins per column is the ceiling: a dictionary or numeric payload,
     * an exception-position bin, and a string-length bin. */
    size_t *binsz = malloc((nc * 3 + ngroups + 8) * sizeof(size_t));
    size_t nbins = 0;

    /* string groups, in the order the decoder expects them */
    Str **sg = calloc(nc * 3 + 8, sizeof(Str *));
    size_t *sgn = calloc(nc * 3 + 8, sizeof(size_t));
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
    long *tparent = malloc((nc ? nc : 1) * sizeof(long));
    for (size_t i = 0; i < nc; i++) tparent[i] = -1;
    pick_text_parents(t, plan, nc, nr, tparent);

    Str **text_cells = calloc(nc ? nc : 1, sizeof(Str *));
    Str **ex_cells = calloc(nc ? nc : 1, sizeof(Str *));
    for (size_t pos = 0; pos < nc; pos++) {
        ColPlan *c = &plan[pos];
        if (c->kind == K_TEXT) {
            Str *cells = malloc((nr ? nr : 1) * sizeof(Str));
            if (tparent[pos] >= 0) {
                /* same trick, same freeness: the decoder rebuilds the parent
                 * first and recomputes this stable argsort */
                KVI *kv = malloc((nr ? nr : 1) * sizeof(KVI));
                int64_t *pv = plan[tparent[pos]].ids;
                for (size_t i = 0; i < nr; i++) { kv[i].v = pv[i]; kv[i].i = i; }
                qsort(kv, nr, sizeof(KVI), kvi_cmp);
                for (size_t i = 0; i < nr; i++)
                    cells[i] = table_at(t, kv[i].i, pos);
                free(kv);
            } else {
                for (size_t i = 0; i < nr; i++) cells[i] = table_at(t, i, pos);
            }
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
        /* Exception cells, for grouped and ungrouped numeric columns alike,
         * emitted immediately after the column's own payload in BOTH streams
         * so the decoder recovers them at the same point in its own walk and
         * needs no index. Positions are delta-coded: they are sorted and
         * sparse, so the gaps pack far smaller than the indices. */
        if (c->kind == K_NUM && c->nex) {
            int64_t *gaps = malloc(c->nex * sizeof(int64_t));
            size_t prev = 0;
            for (size_t i = 0; i < c->nex; i++) {
                gaps[i] = (int64_t)c->ex_pos[i] - (int64_t)prev;
                prev = c->ex_pos[i];
            }
            size_t at = bins.len;
            pack_ints(gaps, c->nex, &bins);
            binsz[nbins++] = bins.len - at;
            free(gaps);

            Str *exv = malloc(c->nex * sizeof(Str));
            for (size_t i = 0; i < c->nex; i++)
                exv[i] = table_at(t, c->ex_pos[i], pos);
            ex_cells[pos] = exv;
            sg[nsg] = exv;
            sgn[nsg] = c->nex;
            nsg++;
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

    unsigned char *has_nl = calloc(nsg ? nsg : 1, 1);
    if (!has_nl) { buf_free(&lenbins); free(lenbinsz); free(smeta); }
    for (size_t g = 0; g < nsg && has_nl; g++)
        for (size_t i = 0; i < sgn[g]; i++)
            if (memchr(sg[g][i].p, '\n', sg[g][i].n)) { has_nl[g] = 1; break; }

    /* Length arrays exist only for groups that contain a newline, and such a
     * group is never front-coded, so they are identical for every candidate
     * layout and are built exactly once. */
    for (size_t g = 0; g < nsg && has_nl; g++) {
        if (!has_nl[g]) continue;
        int64_t *lens = malloc((sgn[g] ? sgn[g] : 1) * sizeof(int64_t));
        if (!lens) break;
        for (size_t i = 0; i < sgn[g]; i++) lens[i] = (int64_t)sg[g][i].n;
        size_t la = lenbins.len;
        pack_ints(lens, sgn[g], &lenbins);
        lenbinsz[nlenbins++] = lenbins.len - la;
        free(lens);
    }
    /* Front-coding the dictionary alphabets, measured. Mirrors fast.py: the
     * alphabets are the first P.norder string groups by construction and are
     * sorted, so neighbours share long prefixes. Worth -1.19% overall and
     * -7.39% on seattle_fire911, but it LOSES on four of thirteen datasets, so
     * the blob is built both ways and the smaller kept.
     *
     * The shared prefix is counted in BYTES and capped at 255. Python counts
     * bytes too, deliberately -- counting characters there would produce a
     * different archive for any word with a non-ASCII prefix.
     *
     * Cheap as guards go: this re-compresses only the text pile, where the
     * exceptions guard costs a second full encode. */
    /* Declared here rather than at the container, because the text pile is
     * compressed early: the metadata carries the chosen group sizes, so the
     * plain-versus-front-coded decision has to be made before meta is built. */
    Buf mz, bz, tz;
    buf_init(&mz); buf_init(&bz); buf_init(&tz);
    int rc = -1;

    /* Which groups to front-code, decided by compressing the WHOLE pile.
     * Mirrors fast._choose_front branch for branch, including the order the
     * candidates are tried in, because the choice selects the archive. */
    unsigned char *front = calloc(nsg ? nsg : 1, 1);
    unsigned char *trial = calloc(nsg ? nsg : 1, 1);
    unsigned char *bestf = calloc(nsg ? nsg : 1, 1);
    SMeta *smeta_t = calloc(nsg ? nsg : 1, sizeof(SMeta));
    Buf txt_t;
    buf_init(&txt_t);
    if (!front || !trial || !bestf || !smeta_t || !has_nl) {
        free(front); free(trial); free(bestf); free(smeta_t);
        buf_free(&txt_t); buf_free(&lenbins); free(lenbinsz); free(has_nl);
        goto done;
    }

    /* candidate 1: nothing front-coded */
    build_pile(sg, sgn, nsg, has_nl, front, &txt, smeta);
    if (ppz_lzma_compress(txt.data, txt.len, &tz)) {
        free(front); free(trial); free(bestf); free(smeta_t);
        buf_free(&txt_t); buf_free(&lenbins); free(lenbinsz); free(has_nl);
        goto done;
    }
    size_t best_z = tz.len;

    #define TAKE_TRIAL()                                                      \
        do {                                                                  \
            buf_free(&tz); buf_init(&tz);                                     \
            buf_put(&tz, tz2.data, tz2.len);                                  \
            best_z = tz2.len;                                                 \
            memcpy(bestf, trial, nsg);                                        \
            memcpy(smeta, smeta_t, nsg * sizeof(SMeta));                      \
        } while (0)

    /* candidate 2: every dictionary alphabet */
    if (P.norder > 0) {
        memset(trial, 0, nsg);
        for (size_t g = 0; g < P.norder && g < nsg; g++) trial[g] = 1;
        build_pile(sg, sgn, nsg, has_nl, trial, &txt_t, smeta_t);
        Buf tz2;
        buf_init(&tz2);
        if (!ppz_lzma_compress(txt_t.data, txt_t.len, &tz2) && tz2.len < best_z)
            TAKE_TRIAL();
        buf_free(&tz2);
    }

    /* candidate 3: the winner so far plus the single group with the most bytes
     * at stake. TEXT_FC_CANDIDATES is 1 because measurement said one trial
     * takes 96% of the available gain for a sixth of the time penalty. */
    {
        long pick = -1;
        int64_t best_stake = 0;
        /* Start at P.norder, NOT at 0. Python's loop is
         * `for i in range(ndict, len(groups))`, so a dictionary alphabet is
         * only ever front-coded as part of candidate 2 -- the all-or-nothing
         * block -- and never individually.
         *
         * This loop used to start at 0 and skip whatever was already in
         * `bestf`, which looks equivalent and is not: when candidate 2 loses,
         * `bestf` is empty, so the alphabets became eligible here and C could
         * front-code one on its own. Python cannot. Found 2026-07-30 by
         * tests/test_cbin_corpus.py on a Colombian pharmaceutical register
         * where all 26 string groups were alphabets: Python had zero
         * candidates and C picked group 6, giving a 41-byte smaller but
         * DIFFERENT archive. Both decoded correctly and each read the other's
         * output, so it was never a data bug -- but invariant 1 is
         * byte-identity, and "C is 0.13% better here" is exactly the kind of
         * silent divergence that guarantee exists to forbid. */
        for (size_t g = P.norder; g < nsg; g++) {
            if (bestf[g] || has_nl[g] || sgn[g] < 2) continue;
            int64_t sh = 0, tot = 0;
            prefix_stats(sg[g], sgn[g], &sh, &tot);
            if (sh <= 0 || tot <= 0) continue;
            if (sh * FC_MIN_DEN <= tot * FC_MIN_NUM) continue;
            size_t sampled = sgn[g] < FC_PREFIX_CAP ? sgn[g] : FC_PREFIX_CAP;
            int64_t stake = sh * (int64_t)sgn[g] / (int64_t)sampled;
            if (stake < FC_MIN_BYTES) continue;
            /* strictly greater keeps the lowest index on a tie, as Python's
             * sort by (-stake, index) does */
            if (stake > best_stake) { best_stake = stake; pick = (long)g; }
        }
        if (pick >= 0) {
            memcpy(trial, bestf, nsg);
            trial[pick] = 1;
            build_pile(sg, sgn, nsg, has_nl, trial, &txt_t, smeta_t);
            Buf tz2;
            buf_init(&tz2);
            if (!ppz_lzma_compress(txt_t.data, txt_t.len, &tz2)
                    && tz2.len < best_z)
                TAKE_TRIAL();
            buf_free(&tz2);
        }
    }
    #undef TAKE_TRIAL

    /* "exactly the alphabets" is the common case and the archive-level flag
     * spells it in 7 bytes rather than 7 per group. Same information, smaller
     * metadata; the decoder reads both spellings. */
    int use_fc = 0;
    if (P.norder > 0) {
        int all_alpha = 1;
        for (size_t g = 0; g < nsg; g++) {
            int want = (g < P.norder);
            if ((bestf[g] != 0) != want) { all_alpha = 0; break; }
        }
        use_fc = all_alpha;
    }
    for (size_t g = 0; g < nsg; g++) smeta[g].fc = use_fc ? 0 : bestf[g];

    free(front); free(trial); free(bestf); free(smeta_t);
    buf_free(&txt_t); free(has_nl);

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
            if (tparent[pos] >= 0) {
                buf_put(&meta, "{\"kind\":\"text\",\"parent\":", 24);
                json_int(&meta, (long long)tparent[pos]);
                buf_putc(&meta, '}');
            } else {
                buf_put(&meta, "{\"kind\":\"text\"}", 15);
            }
        } else if (c->in_group) {
            buf_put(&meta, "{\"kind\":\"grp\",\"dec\":", 20);
            json_int(&meta, c->dec);
            buf_put(&meta, ",\"g\":", 5);
            json_int(&meta, c->group_idx);
            /* "nex" is appended last because fast.py adds it to an already
             * built dict, and the metadata is compared byte for byte. */
            if (c->nex) {
                buf_put(&meta, ",\"nex\":", 7);
                json_int(&meta, (long long)c->nex);
            }
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
            buf_putc(&meta, ']');
            if (c->nex) {
                buf_put(&meta, ",\"nex\":", 7);
                json_int(&meta, (long long)c->nex);
            }
            buf_putc(&meta, '}');
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
        /* fast.py adds "fc" to an already-built dict, so it lands last */
        if (smeta[g].fc) buf_put(&meta, ",\"fc\":1", 7);
        buf_putc(&meta, '}');
    }
    buf_put(&meta, "],\"nlenbins\":", 13);
    json_int(&meta, (long long)nlenbins);
    /* fast.py sets meta["fc"] on an already-built dict, so it lands last and
     * the metadata is compared byte for byte */
    if (use_fc) buf_put(&meta, ",\"fc\":1", 7);
    buf_putc(&meta, '}');

    /* Did any of the three ideas actually do something? A parent-sorted
     * dictionary column, a numeric column whose differences packed smaller
     * than its values, or a 2D group. The same three counts fast.py sums --
     * note grouped numeric columns are excluded there because their spec is
     * "grp" rather than "num", and text parents are not counted at all. */
    if (fired) {
        long f = (long)ngroups;
        for (size_t pos = 0; pos < nc; pos++) {
            if (plan[pos].kind == K_DICT) {
                if (P.parent[pos] >= 0) f++;
            } else if (plan[pos].kind == K_NUM && !plan[pos].in_group
                       && plan[pos].k) {
                f++;
            }
        }
        *fired = f;
    }

    /* ------------------------------------------------------- container */
    if (ppz_lzma_compress(meta.data, meta.len, &mz)) goto done;
    if (ppz_lzma_compress(bins.data, bins.len, &bz)) goto done;
    /* tz is already the winner of the plain / front-coded comparison above */

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
    for (size_t j = 0; j < nc; j++) { free(text_cells[j]); free(ex_cells[j]); }
    free(text_cells); free(ex_cells); free(tparent);
    for (size_t g = 0; g < ngroups; g++) free(groups[g].pos);
    free(groups);
    free(P.parent); free(P.order);
    plan_free(plan, nc);
    return rc;
}

/* ------------------------------------------------------------- fallbacks */

/* Do the tables hold the same strings? Used to check that the canonical CSV
 * survives a round trip before the fallback built from it is allowed to win.
 * CSV quoting is not lossless for every conceivable cell -- a bare '\r' is
 * the case that actually occurs -- and a fallback that corrupts data would be
 * far worse than losing by 11%. */
static int tables_equal(const Table *a, const Table *b)
{
    if (a->ncols != b->ncols || a->nrows != b->nrows) return 0;
    for (size_t j = 0; j < a->ncols; j++)
        if (strcmp(a->names[j], b->names[j])) return 0;
    for (size_t i = 0; i < a->nrows; i++) {
        for (size_t j = 0; j < a->ncols; j++) {
            Str x = table_at(a, i, j), y = table_at(b, i, j);
            if (x.n != y.n || memcmp(x.p, y.p, x.n)) return 0;
        }
    }
    return 1;
}

/* Smallest plain-codec encoding of the whole table, if one beats `limit`.
 * Mirrors fast._raw_candidates: canonical CSV, round-trip checked, then xz and
 * bzip2, and the winner must be strictly smaller than the modelled container.
 * Returns 1 and fills `out` on success, 0 if nothing qualified. */
static int raw_candidates(const Table *t, size_t limit, Buf *out)
{
    Buf canon;
    buf_init(&canon);
    table_write_canonical(t, &canon);

    Table back;
    int ok = table_parse_csv(&back, canon.data, canon.len) == 0
             && tables_equal(t, &back);
    table_free(&back);
    if (!ok) { buf_free(&canon); return 0; }

    int found = 0;
    Buf cand;
    buf_init(&cand);
    const char *magics[2] = { PPZ_MAGIC_RAW_XZ, PPZ_MAGIC_RAW_BZ };
    for (int which = 0; which < 2; which++) {
        Buf z;
        buf_init(&z);
        int bad = which == 0 ? ppz_lzma_compress(canon.data, canon.len, &z)
                             : ppz_bz2_compress(canon.data, canon.len, &z);
        if (!bad && z.len + 4 < limit && (!found || z.len + 4 < cand.len)) {
            buf_free(&cand);
            buf_put(&cand, magics[which], 4);
            buf_put(&cand, z.data, z.len);
            found = 1;
        }
        buf_free(&z);
    }
    buf_free(&canon);

    if (found) { buf_free(out); buf_put(out, cand.data, cand.len); }
    buf_free(&cand);
    return found;
}

int ppz_encode_modelled(const Table *t, Buf *out, long *fired)
{
    return encode_modelled(t, out, fired, 1, NULL, 1, NULL);
}

/* Does this table have lenient columns at all, and could any of them win?
 * fast.encode classifies once up front to answer exactly this, because the
 * answer decides which plan is encoded -- so it has to be settled BEFORE any
 * encoding, not after, or the two implementations pick different containers. */
static void lenient_verdict(const Table *t, int *any_lax, int *promising)
{
    *any_lax = 0;
    *promising = 0;
    ColPlan *plan = classify(t, 1);
    if (!plan) return;
    for (size_t j = 0; j < t->ncols; j++) {
        if (plan[j].nex) {
            *any_lax = 1;
            if (plan[j].ex_prom) *promising = 1;
        }
    }
    plan_free(plan, t->ncols);
}

int ppz_encode(const Table *t, Buf *out)
{
    /* Numeric-with-exceptions, measured. Mirrors fast.encode branch for
     * branch. Recovering a column that is numeric apart from a few cells is
     * worth 41% of the Treasury yield curve, but it also moves a column out of
     * the dictionary path -- exactly the trade that made the reverted
     * ragged-decimal work 9-23% worse. So the old behaviour is encoded too and
     * kept if smaller: never-worse per file, not on average.
     *
     * The second encode is the expensive part, so it is nominated first. Three
     * cases, matching Python: no lenient columns at all (the two plans are
     * identical, encode once); lenient columns but none that could win (encode
     * the plain plan once); otherwise encode both and measure. */
    int any_lax = 0, promising = 0;
    lenient_verdict(t, &any_lax, &promising);
    int lenient = (!any_lax) || promising;

    long fired = 0;
    size_t ngroups = 0;
    if (encode_modelled(t, out, &fired, 1, &ngroups, lenient, NULL)) return -1;

    if (any_lax && promising) {
        Buf alt;
        buf_init(&alt);
        long alt_fired = 0;
        size_t alt_groups = 0;
        if (!encode_modelled(t, &alt, &alt_fired, 1, &alt_groups, 0, NULL)
                && alt.len < out->len) {
            buf_free(out);
            buf_put(out, alt.data, alt.len);
            fired = alt_fired;
            ngroups = alt_groups;
            lenient = 0;
        }
        buf_free(&alt);
    }

    /* The planar predictor, measured. Mirrors fast.encode exactly, and for the
     * same reason: of the three corpus tables where a group forms at all, two
     * came out LARGER for it. A grouped column loses its own measured
     * differencing order to one fixed scheme, so a group trades several
     * measured decisions for a single unmeasured one.
     *
     * End to end is the only check that works. Raw packed length shows -0.0%
     * on nyc_collisions where the real effect is +28.1%, and a per-group probe
     * says wide_random gains 1.4% where the file actually loses 0.69% -- the
     * group's bytes are compressed together with every other payload, so
     * nothing short of the whole container can see the result.
     *
     * Groups formed on 2 of 18 tables here, so the second encode is rare. */
    if (ngroups) {
        Buf alt;
        buf_init(&alt);
        long alt_fired = 0;
        if (!encode_modelled(t, &alt, &alt_fired, 0, NULL, lenient, NULL)
                && alt.len < out->len) {
            buf_free(out);
            buf_put(out, alt.data, alt.len);
            fired = alt_fired;
        }
        buf_free(&alt);
    }

    /* Only when none of the three tricks fired -- that is precisely the case
     * where this codec has degenerated into "split into columns, then xz" and
     * a different finisher may well beat it. Running the candidates
     * unconditionally would roughly double encode time for nothing. */
    if (fired) return 0;
    raw_candidates(t, out->len, out);
    return 0;
}
