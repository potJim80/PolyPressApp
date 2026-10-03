/* Encoding a Polypress archive.
 *
 * Three ideas, chosen per column: predict down a column (differences),
 * predict across commensurable numeric columns (2D groups), and reorder rows
 * so a column collapses into runs (parents). Two rules keep the output
 * deterministic -- the same table always gives the same bytes, on any
 * machine and any number of threads:
 *
 *   Entropy scores are quantised to a ~1e-6 grid and compared as integers
 *   (score_of), so a choice never hangs on a float's last bit.
 *   Ties fall to the lowest column index.
 */

#include "ppz.h"

#include <math.h>
#include <stddef.h>
#include <stdint.h>
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

/* ------------------------------------------------------------------ varints */

static uint64_t zigzag(int64_t v)
{
    return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63);
}

/* Bytes pack_ints would write: one byte per value, plus a 4- or 8-byte tail
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
        /* unsigned: values reach +-2^62, and a difference of two of them
         * overflows int64. Wrapping is what the decoder undoes; signed
         * overflow would be undefined behaviour instead */
        for (size_t i = 0; i + 1 < len; i++)
            cur[i] = (int64_t)((uint64_t)cur[i + 1] - (uint64_t)cur[i]);
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

/* ------------------------------------------------------------------ strings */

static int str_cmp(const void *pa, const void *pb)
{
    const Str *a = pa, *b = pb;
    size_t n = a->n < b->n ? a->n : b->n;
    int r = n ? memcmp(a->p, b->p, n) : 0;
    if (r) return r;
    /* UTF-8 byte order is codepoint order; a prefix sorts first */
    return a->n < b->n ? -1 : (a->n > b->n ? 1 : 0);
}

static int str_eq(Str a, Str b)
{
    return a.n == b.n && (a.n == 0 || !memcmp(a.p, b.p, a.n));
}

/* --------------------------------------------------- sorting without qsort */

/* Until 2026-10-01 every sort here was qsort with a comparison function, and
 * sorting was 43% of the encoder's busy time on NEMSIS blocks. The values
 * being sorted are almost always small non-negative integers -- dictionary
 * ids -- so the sorts below are counting and radix sorts. Each produces
 * exactly the order the qsort did (the stable argsort included), so no
 * archive byte moved. */

/* perm[j] = the row that comes j-th in (v, row) order, v in 0..k-1: the
 * stable argsort (qsort on value, then row) gave, in O(n + k). */
static size_t *argsort_ids(const int64_t *v, size_t n, size_t k)
{
    size_t *perm = malloc((n ? n : 1) * sizeof(size_t));
    size_t *start = calloc(k + 1, sizeof(size_t));
    if (!perm || !start) { free(perm); free(start); return NULL; }
    for (size_t i = 0; i < n; i++) start[(size_t)v[i] + 1]++;
    for (size_t b = 0; b < k; b++) start[b + 1] += start[b];
    for (size_t i = 0; i < n; i++) perm[start[(size_t)v[i]]++] = i;
    free(start);
    return perm;
}

/* Sort non-negative integers ascending: counting when the range is small,
 * else LSD radix on 11-bit digits, only as many passes as the largest value
 * needs. Returns 0, or -1 (out of memory or a negative value: caller falls
 * back). */
static int sort_nonneg(int64_t *a, size_t n)
{
    if (n < 2) return 0;
    uint64_t mx = 0;
    for (size_t i = 0; i < n; i++) {
        if (a[i] < 0) return -1;
        if ((uint64_t)a[i] > mx) mx = (uint64_t)a[i];
    }
    if (mx < 2 * (uint64_t)n + 4096) {
        size_t *cnt = calloc((size_t)mx + 1, sizeof(size_t));
        if (!cnt) return -1;
        for (size_t i = 0; i < n; i++) cnt[a[i]]++;
        size_t o = 0;
        for (uint64_t v = 0; v <= mx; v++)
            for (size_t c = cnt[v]; c; c--) a[o++] = (int64_t)v;
        free(cnt);
        return 0;
    }
    int64_t *tmp = malloc(n * sizeof(int64_t));
    if (!tmp) return -1;
    int64_t *src = a, *dst = tmp;
    for (int shift = 0; shift < 64 && (mx >> shift); shift += 11) {
        size_t cnt[2049] = { 0 };
        for (size_t i = 0; i < n; i++) cnt[(((uint64_t)src[i] >> shift) & 2047) + 1]++;
        for (int b = 0; b < 2048; b++) cnt[b + 1] += cnt[b];
        for (size_t i = 0; i < n; i++) dst[cnt[((uint64_t)src[i] >> shift) & 2047]++] = src[i];
        int64_t *s = src; src = dst; dst = s;
    }
    if (src != a) memcpy(a, src, n * sizeof(int64_t));
    free(tmp);
    return 0;
}

/* ------------------------------------------------- dictionaries by hashing */

static uint64_t str_hash(const char *p, size_t n)
{
    uint64_t h = 0x9E3779B97F4A7C15ULL ^ n;
    while (n >= 8) {
        uint64_t w;
        memcpy(&w, p, 8);
        h = (h ^ w) * 0xBF58476D1CE4E5B9ULL;
        h ^= h >> 31;
        p += 8; n -= 8;
    }
    uint64_t w = 0;
    memcpy(&w, p, n);
    h = (h ^ w) * 0x94D049BB133111EBULL;
    return h ^ (h >> 29);
}

typedef struct { Str s; uint32_t e; } StrE;

static int stre_cmp(const void *a, const void *b)
{
    return str_cmp(&((const StrE *)a)->s, &((const StrE *)b)->s);
}

/* The distinct values among n cells (cell i at base[i * stride]) in sorted
 * order, and each cell's index in that order -- what sorting all n cells and
 * binary-searching each one used to give. Hashing finds the distinct values
 * in one pass; only they are sorted. Stops as soon as more than `cap`
 * distinct values appear (returns 0), because every caller only needs a
 * dictionary small enough to use. Returns 1 when built, -1 when out of
 * memory. *alpha and *ids are the caller's to free; ids may be NULL. */
static int build_dict(const Str *base, size_t stride, size_t n, size_t cap,
                      Str **alpha_out, size_t *u_out, int64_t **ids_out)
{
    *alpha_out = NULL;
    *u_out = 0;
    if (ids_out) *ids_out = NULL;
    size_t most = cap < n ? cap + 1 : n;           /* entries ever stored */
    size_t tsz = 16;
    while (tsz < 2 * most + 2) tsz <<= 1;
    uint32_t *slot = calloc(tsz, sizeof(uint32_t)); /* entry + 1, 0 empty */
    Str *ent = malloc((most ? most : 1) * sizeof(Str));
    uint32_t *eid = ids_out ? malloc((n ? n : 1) * sizeof(uint32_t)) : NULL;
    if (!slot || !ent || (ids_out && !eid)) {
        free(slot); free(ent); free(eid);
        return -1;
    }
    size_t u = 0;
    for (size_t i = 0; i < n; i++) {
        Str c = base[i * stride];
        size_t h = (size_t)str_hash(c.p, c.n) & (tsz - 1);
        for (;;) {
            uint32_t s = slot[h];
            if (!s) {
                if (u == cap) { free(slot); free(ent); free(eid); *u_out = cap + 1; return 0; }
                ent[u] = c;
                slot[h] = (uint32_t)(u + 1);
                if (eid) eid[i] = (uint32_t)u;
                u++;
                break;
            }
            if (str_eq(ent[s - 1], c)) {
                if (eid) eid[i] = s - 1;
                break;
            }
            h = (h + 1) & (tsz - 1);
        }
    }
    free(slot);
    StrE *se = malloc((u ? u : 1) * sizeof(StrE));
    Str *alpha = malloc((u ? u : 1) * sizeof(Str));
    uint32_t *rank = malloc((u ? u : 1) * sizeof(uint32_t));
    int64_t *ids = ids_out ? malloc((n ? n : 1) * sizeof(int64_t)) : NULL;
    if (!se || !alpha || !rank || (ids_out && !ids)) {
        free(se); free(alpha); free(rank); free(ids); free(ent); free(eid);
        return -1;
    }
    for (size_t e = 0; e < u; e++) { se[e].s = ent[e]; se[e].e = (uint32_t)e; }
    qsort(se, u, sizeof(StrE), stre_cmp);
    for (size_t r = 0; r < u; r++) { alpha[r] = se[r].s; rank[se[r].e] = (uint32_t)r; }
    if (ids)
        for (size_t i = 0; i < n; i++) ids[i] = rank[eid[i]];
    free(se); free(rank); free(ent); free(eid);
    *alpha_out = alpha;
    *u_out = u;
    if (ids_out) *ids_out = ids;
    return 1;
}

/* ------------------------------------------------------------------- numeric */

/* Optional sign, digits, optional fraction; the whole field must be
 * consumed. */
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
     * signed overflow is undefined and wraps negative in practice. */
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

/* A column that is numeric apart from a few cells that are not.
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
 * histogram a fixed array. */
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

#define NUMPAR_SAMPLE 20000           /* rows a nomination looks at */
static size_t num_probe(const int64_t *a, size_t n, const size_t *perm);
static uint64_t num_bits(int64_t *a, size_t n, int64_t *tmp);

/* Could storing this column as numbers possibly beat leaving it alone?
 *
 * One-sided by construction. The alternative is deliberately OVER-estimated --
 * it charges for the dictionary's alphabet and ignores the reorder parent that
 * would make the column cheaper still -- so `numeric >= estimate` implies
 * `numeric >= real`, and declining on that basis cannot discard a win. */

/* `p` is the column's own plan from the first pass -- its dictionary or
 * text form, the alternative -- and `plan` the rest of the table, whose
 * dictionary columns the numbers could be sorted by. */
static int lenient_promising(const Table *t, size_t col, const int64_t *a,
                             size_t n, const size_t *expos, size_t nex,
                             const ColPlan *plan, size_t nc)
{
    /* the numbers: in file order, or sorted by the dictionary column whose
     * order makes their differences cheapest (estimated on a sample, then
     * probed) -- whichever is smaller */
    size_t num = num_probe(a, n, NULL);
    size_t step = n / NUMPAR_SAMPLE + 1, sn = (n + step - 1) / step;
    int64_t *v = malloc((sn ? sn : 1) * sizeof(int64_t));
    int64_t *tmp = malloc((sn ? sn : 1) * sizeof(int64_t));
    int64_t *sid = malloc((sn ? sn : 1) * sizeof(int64_t));
    if (v && tmp && sid) {
        for (size_t i = 0; i < sn; i++) v[i] = a[i * step];
        uint64_t best = num_bits(v, sn, tmp);
        long bd = -1;
        for (size_t d = 0; d < nc; d++) {
            if (d == col || plan[d].kind != K_DICT || plan[d].nalpha < 2) continue;
            for (size_t i = 0; i < sn; i++) sid[i] = plan[d].ids[i * step];
            size_t *perm = argsort_ids(sid, sn, plan[d].nalpha);
            if (!perm) continue;
            for (size_t i = 0; i < sn; i++) v[i] = a[perm[i] * step];
            free(perm);
            uint64_t c = num_bits(v, sn, tmp);
            if (c < best) { best = c; bd = (long)d; }
        }
        if (bd >= 0) {
            size_t *perm = argsort_ids(plan[bd].ids, n, plan[bd].nalpha);
            size_t s2 = perm ? num_probe(a, n, perm) : (size_t)-1;
            if (s2 < num) num = s2;
            free(perm);
        }
    }
    free(v); free(tmp); free(sid);

    int64_t *gaps = malloc((nex ? nex : 1) * sizeof(int64_t));
    if (!gaps) return 1;
    size_t prev = 0;
    for (size_t i = 0; i < nex; i++) {
        gaps[i] = (int64_t)expos[i] - (int64_t)prev;
        prev = expos[i];
    }
    Buf pb;
    buf_init(&pb);
    pack_ints(gaps, nex, &pb);
    num += ppz_lzma_probe_len(pb.data, pb.len);
    buf_free(&pb);
    free(gaps);

    /* the alternative, from the first pass: dictionary ids and alphabet in
     * file order, or the cells as text */
    const ColPlan *p = &plan[col];
    size_t alt;
    if (p->kind == K_DICT) {
        int w = p->nalpha <= 256 ? 1 : (p->nalpha <= 65536 ? 2 : 4);
        Buf idb;
        buf_init(&idb);
        for (size_t i = 0; i < n; i++) {
            uint64_t x = (uint64_t)p->ids[i];
            for (int b = 0; b < w; b++) buf_putc(&idb, (char)((x >> (8 * b)) & 0xFF));
        }
        alt = ppz_lzma_probe_len(idb.data, idb.len);
        buf_free(&idb);
        Buf ab;
        buf_init(&ab);
        for (size_t i = 0; i < p->nalpha; i++) {
            if (i) buf_putc(&ab, '\n');
            buf_put(&ab, p->alpha[i].p, p->alpha[i].n);
        }
        alt += ppz_lzma_probe_len(ab.data, ab.len);
        buf_free(&ab);
    } else {
        Buf cb;
        buf_init(&cb);
        for (size_t i = 0; i < n; i++) {
            if (i) buf_putc(&cb, '\n');
            Str c = table_at(t, i, col);
            buf_put(&cb, c.p, c.n);
        }
        alt = ppz_lzma_probe_len(cb.data, cb.len);
        buf_free(&cb);
    }
    return num < alt;
}

/* Two passes. The first gives every column its strict form -- numeric,
 * dictionary or text -- and keeps the lenient numbers of nearly-numeric
 * columns aside. The second decides, per column, whether those numbers win:
 * it runs once every dictionary column is known, because sorting by one of
 * them is part of what numbers can do. */
static ColPlan *classify(const Table *t)
{
    size_t nc = t->ncols, nr = t->nrows;
    ColPlan *plan = calloc(nc ? nc : 1, sizeof(ColPlan));
    int64_t **lax = calloc(nc ? nc : 1, sizeof(int64_t *));
    size_t **lexp = calloc(nc ? nc : 1, sizeof(size_t *));
    size_t *lnex = calloc(nc ? nc : 1, sizeof(size_t));
    int *ldec = calloc(nc ? nc : 1, sizeof(int));
    if (!plan || !lax || !lexp || !lnex || !ldec) {
        free(plan); free(lax); free(lexp); free(lnex); free(ldec);
        return NULL;
    }

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
        lax[j] = numeric_column_lenient(t, j, &ldec[j], &lexp[j], &lnex[j]);
        /* dictionary if the distinct count is small enough */
        size_t limit = nr > 2 ? nr : 2;
        size_t cap = limit / 2 < DICT_MAX ? limit / 2 : DICT_MAX;
        Str *alpha = NULL;
        int64_t *ids = NULL;
        size_t u = 0;
        int dict = build_dict(t->cells + j, nc, nr, cap, &alpha, &u, &ids);
        if (dict < 0) { plan_free(plan, nc); plan = NULL; break; }
        if (dict) {
            plan[j].kind = K_DICT;
            plan[j].alpha = alpha;
            plan[j].nalpha = u;
            plan[j].ids = ids;
        } else {
            plan[j].kind = K_TEXT;
        }
    }

    /* Nearly-numeric columns are numbers only when that could win for THIS
     * column. Until 2026-10-01 it was one verdict for the whole table, and
     * one promising column dragged every lenient column in (a CDC
     * vaccination table, 3.77 MB instead of 2.73); and the numbers were
     * judged in file order only, so a county-by-date table never saw its
     * counts sorted by county. */
    for (size_t j = 0; plan && j < nc; j++) {
        if (!lax[j]) continue;
        if (lenient_promising(t, j, lax[j], nr, lexp[j], lnex[j], plan, nc)) {
            free(plan[j].alpha); free(plan[j].ids);
            plan[j].alpha = NULL; plan[j].ids = NULL; plan[j].nalpha = 0;
            plan[j].kind = K_NUM;
            plan[j].ints = lax[j];
            plan[j].dec = ldec[j];
            plan[j].ex_pos = lexp[j];
            plan[j].nex = lnex[j];
            lax[j] = NULL; lexp[j] = NULL;
        }
    }
    for (size_t j = 0; j < nc; j++) { free(lax[j]); free(lexp[j]); }
    free(lax); free(lexp); free(lnex); free(ldec);
    return plan;
}

/* --------------------------------------------------------------- 2D groups */

typedef struct { size_t *pos; size_t n; } Group;

/* `a <= 8*b` without overflowing.
 *
 * This codec accepts magnitudes up to 2^62, and 8 * 2^62 does not fit in an
 * int64 -- the product wraps negative and the comparison silently says "not
 * commensurable" -- every group of large numeric columns was once refused
 * that way, for no visible reason. For positive integers, a <= 8b is exactly ceil(a/8) <= b. */
static int le_times8(int64_t a, int64_t b)
{
    return (a + 7) / 8 <= b;
}

static Group *find_2d_groups(ColPlan *plan, size_t nc, size_t nrows,
                             const long *nparent, size_t *ngroups)
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
        /* a column that compresses better sorted by a parent stays out:
         * a group is stored in file order */
        if (plan[p].kind != K_NUM || nrows == 0 || (nparent && nparent[p] >= 0)) {
            FLUSH(); continue;
        }
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

/* Entropy scores are COMPARED as integers, never as floats: quantised to a
 * ~1e-6 grid, so a parent choice depends on the part of the number that
 * carries information and never on rounding in the last bit. Ties fall to
 * the lower column index. Every score here is non-negative. */
#define SCORE_SCALE 1048576.0

static int64_t score_of(double x)
{
    return (int64_t)floor(x * SCORE_SCALE + 0.5);
}

static double counts_entropy(const int64_t *counts, size_t nc, size_t n)
{
    double s = 0.0;
    for (size_t i = 0; i < nc; i++) {
        double p = (double)counts[i] / (double)n;
        s += p * log2(p);
    }
    return -s;
}

/* Counts of each distinct value in `v`, ascending by value -- a fixed order,
 * because it is the summation order of the entropy. */
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
    if (sort_nonneg(s, n)) qsort(s, n, sizeof(int64_t), i64_cmp);
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

/* One dictionary parent, measured: column b's ids sorted by parent a must
 * compress smaller than in file order. A failed allocation keeps the parent,
 * as the serial loop did. */
typedef struct {
    const ColPlan *plan;
    size_t nrows, b, a;
    int    pays;
} Guard;

static void guard_task(void *arg)
{
    Guard *g = arg;
    const ColPlan *plan = g->plan;
    size_t nrows = g->nrows, b = g->b, a = g->a;
    g->pays = 1;
    size_t alen = plan[b].nalpha;
    int wb = alen <= 256 ? 1 : (alen <= 65536 ? 2 : 4);
    size_t *kv = argsort_ids(plan[a].ids, nrows, plan[a].nalpha);
    if (!kv) return;
    uint8_t *flat = malloc(nrows * (size_t)wb ? nrows * (size_t)wb : 1);
    uint8_t *perm = malloc(nrows * (size_t)wb ? nrows * (size_t)wb : 1);
    if (!flat || !perm) { free(kv); free(flat); free(perm); return; }
    for (size_t i = 0; i < nrows; i++) {
        uint32_t v0 = (uint32_t)plan[b].ids[i];
        uint32_t v1 = (uint32_t)plan[b].ids[kv[i]];
        memcpy(flat + i * (size_t)wb, &v0, (size_t)wb);
        memcpy(perm + i * (size_t)wb, &v1, (size_t)wb);
    }
    free(kv);
    size_t cost_perm = ppz_lzma_probe_len(perm, nrows * (size_t)wb);
    size_t cost_flat = ppz_lzma_probe_len(flat, nrows * (size_t)wb);
    free(flat); free(perm);
    g->pays = cost_perm < cost_flat;
}

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

    /* gain[b][a] for a != b, stored dense as a QUANTISED score; -1 means
     * "no usable gain". See score_of(). */
    int64_t *gain = malloc(nd * nd * sizeof(int64_t));
    if (!gain) { for (size_t i = 0; i < nd; i++) free(sample[i]);
                 free(sample); free(base); free(distinct); free(dict_pos);
                 free(parent); free(order); return R; }
    for (size_t i = 0; i < nd * nd; i++) gain[i] = -1;
    const int64_t min_gain = score_of(0.05);
    for (size_t ai = 0; ai < nd; ai++) {
        for (size_t bi = 0; bi < nd; bi++) {
            if (ai == bi) continue;
            int64_t g = score_of(base[bi] - cond_entropy_corrected(
                sample[ai], sample[bi], sn,
                (int64_t)plan[dict_pos[bi]].nalpha, base[ai], distinct[ai]));
            if (g > min_gain) gain[bi * nd + ai] = g;
        }
    }

    /* root = the lowest-entropy column; min() over a list takes the first */
    size_t root = 0;
    for (size_t i = 1; i < nd; i++)
        if (score_of(base[i]) < score_of(base[root])) root = i;

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
        int64_t bg = 0;
        /* iterate remaining in column order, and `a` in column order too --
         * strictly greater, so the first maximum wins */
        for (size_t ri = 0; ri < rn; ri++) {
            size_t b = remaining[ri];
            for (size_t a = 0; a < nd; a++) {
                if (a == b || !placed[a]) continue;
                int64_t g = gain[b * nd + a];
                if (g < 0) continue;
                if (bb < 0 || g > bg) { bb = (long)b; ba = (long)a; bg = g; }
            }
        }
        size_t chosen;
        if (bb < 0) {
            size_t mi = 0;
            for (size_t ri = 1; ri < rn; ri++)
                if (score_of(base[remaining[ri]])
                        < score_of(base[remaining[mi]])) mi = ri;
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
     * Entropy stays as the nominator; this checks the nomination pays.
     *
     * It cannot be skipped where entropy is confident: on the suite
     * (2026-10-02) it refused 71 of 360 nominees, 20 of them with a gain
     * above 3 bits. Each check is independent of the others, so they run
     * on the encoder's workers like the text-parent probes; the decisions,
     * and the bytes, are the serial ones. */
    Guard *gj = malloc((no ? no : 1) * sizeof(Guard));
    size_t ng = 0;
    for (size_t oi = 0; gj && oi < no; oi++) {
        size_t b = order[oi];
        if (parent[b] < 0) continue;
        gj[ng++] = (Guard){ plan, nrows, b, (size_t)parent[b], 0 };
    }
    if (gj) ppz_parallel(guard_task, gj, sizeof(Guard), ng);
    for (size_t k = 0; gj && k < ng; k++)
        if (!gj[k].pays) parent[gj[k].b] = -1;
    free(gj);

    for (size_t i = 0; i < nd; i++) free(sample[i]);
    free(sample); free(base); free(distinct);
    free(gain); free(placed); free(remaining);
    free(dict_pos);
    R.parent = parent; R.order = order; R.norder = no;
    return R;
}

/* -------------------------------------------------------- text reordering */

#define TEXT_PARENT_CANDIDATES 5

typedef struct { int64_t g; size_t dp; } Cand;   /* g is a quantised score_of() */

static int cand_cmp(const void *a, const void *b)
{
    const Cand *x = a, *y = b;
    /* by descending gain, then ascending column index */
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
 * they dominate the datasets this codec does worst on. Two stages: conditional
 * entropy only NOMINATES candidates, and the winner is decided by actually
 * compressing. Trusting the score alone made one real dataset 66% larger --
 * entropy measures how often the parent pins the exact value, which is not
 * what shrinks text, and reordering also destroys whatever useful order the
 * file already had. */
/* One measured candidate: column `tp` in its own order (dp < 0) or sorted by
 * dictionary column `dp`. Every probe is independent of every other, so they
 * all run together; the fold afterwards is the serial one. */
typedef struct {
    const Table   *t;
    const ColPlan *plan;
    size_t         nrows, tp;
    long           dp;
    size_t         cost;
} Probe;

static void probe_task(void *arg)
{
    Probe *pr = arg;
    size_t nrows = pr->nrows;
    if (pr->dp < 0) { pr->cost = probe_order(pr->t, pr->tp, NULL, nrows); return; }
    size_t *perm = argsort_ids(pr->plan[pr->dp].ids, nrows, pr->plan[pr->dp].nalpha);
    if (!perm) { pr->cost = (size_t)-1; return; }
    pr->cost = probe_order(pr->t, pr->tp, perm, nrows);
    free(perm);
}

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

    /* the shortlist: per text column, up to TEXT_PARENT_CANDIDATES parents */
    Probe *probes = malloc(nt * (TEXT_PARENT_CANDIDATES + 1) * sizeof(Probe));
    size_t nprobes = 0;
    int64_t **dsample = calloc(nd, sizeof(int64_t *));
    double  *dbase = calloc(nd, sizeof(double));
    size_t  *ddist = calloc(nd, sizeof(size_t));
    if (!probes || !dsample || !dbase || !ddist) goto done;
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
        Str *srt = NULL;
        int64_t *ids = NULL;
        size_t u = 0;
        if (build_dict(t->cells + tp, t->ncols * step, sn, SIZE_MAX, &srt, &u, &ids) != 1)
            break;
        size_t tdist = 0;
        double base_t = entropy_of(ids, sn, &tdist);

        Cand *cands = malloc(nd * sizeof(Cand));
        size_t ncand = 0;
        const int64_t min_gain_t = score_of(0.05);
        for (size_t di = 0; di < nd; di++) {
            int64_t g = score_of(base_t - cond_entropy_corrected(
                dsample[di], ids, sn, (int64_t)u, dbase[di], ddist[di]));
            if (g > min_gain_t) { cands[ncand].g = g; cands[ncand].dp = di; ncand++; }
        }
        free(srt); free(ids);
        if (!ncand) { free(cands); continue; }
        qsort(cands, ncand, sizeof(Cand), cand_cmp);

        /* Conditional entropy only NOMINATES; the decision is measured. It
         * measures how often the parent pins the exact value, which is not
         * what shrinks text, and reordering also destroys whatever useful
         * order the file already had. Trusting the score alone made one
         * real dataset 66% larger. */
        size_t take = ncand < TEXT_PARENT_CANDIDATES ? ncand : TEXT_PARENT_CANDIDATES;
        probes[nprobes++] = (Probe){ t, plan, nrows, tp, -1, 0 };
        for (size_t c = 0; c < take; c++)
            probes[nprobes++] = (Probe){ t, plan, nrows, tp, (long)dict_pos[cands[c].dp], 0 };
        free(cands);
    }

    ppz_parallel(probe_task, probes, sizeof(Probe), nprobes);

    /* the serial fold: own order first, then the candidates in rank order,
     * each kept only if strictly smaller */
    for (size_t i = 0; i < nprobes; ) {
        size_t tp = probes[i].tp, best_cost = probes[i].cost;
        long best = -1;
        size_t j = i + 1;
        for (; j < nprobes && probes[j].dp >= 0 && probes[j].tp == tp; j++)
            if (probes[j].cost < best_cost) { best_cost = probes[j].cost; best = probes[j].dp; }
        tparent[tp] = best;
        i = j;
    }

done:
    if (dsample) for (size_t i = 0; i < nd; i++) free(dsample[i]);
    free(dsample); free(dbase); free(ddist); free(probes);
    free(dict_pos); free(text_pos);
}

/* ------------------------------------------------------ numeric reordering */

/* Numeric columns can be stored sorted by a dictionary column too. Panel
 * data -- many places, each measured over time -- is the case it is for:
 * sorted by county, a county's counts sit next to each other and their
 * differences shrink. Measured 2026-10-01 on the CDC vaccination table
 * (3,300 counties x dates, 67 numeric columns): the numeric payload went
 * from 4.14 MB in file order to 1.18 MB sorted by county.
 *
 * Nomination is an estimate with no compression at all: the bits the
 * column's differences (order 0-2) would need in each order. The nominee is
 * then measured -- preset-1 probe of the packed column, sorted against file
 * order -- and kept only if strictly smaller. Wide tables nominate on a
 * sample of rows. Runs before the 2D groups form; a column given a parent
 * here is kept out of them. */

#define NUMPAR_BUDGET 300000000ULL     /* rows x candidate pairs, before sampling */

static uint64_t bit_cost(const int64_t *a, size_t n)
{
    uint64_t bits = 0;
    for (size_t i = 0; i < n; i++) {
        uint64_t u = zigzag(a[i]);
        bits += u ? 64 - (uint64_t)__builtin_clzll(u) : 0;
    }
    return bits + n;
}

/* the cheaper of differencing orders 0..2, in estimated bits */
static uint64_t num_bits(int64_t *a, size_t n, int64_t *tmp)
{
    uint64_t best = bit_cost(a, n);
    memcpy(tmp, a, n * sizeof(int64_t));
    size_t len = n;
    for (int k = 1; k <= 2 && len > 1; k++) {
        for (size_t i = 0; i + 1 < len; i++)
            tmp[i] = (int64_t)((uint64_t)tmp[i + 1] - (uint64_t)tmp[i]);
        len--;
        uint64_t c = bit_cost(tmp, len) + 64 * (uint64_t)k;
        if (c < best) best = c;
    }
    return best;
}

/* the column packed as the payload would pack it, sorted by `perm` or not */
static size_t num_probe(const int64_t *a, size_t n, const size_t *perm)
{
    int64_t *v = malloc((n ? n : 1) * sizeof(int64_t));
    if (!v) return (size_t)-1;
    for (size_t i = 0; i < n; i++) v[i] = a[perm ? perm[i] : i];
    int k = diff_order(v, n);
    size_t dn = 0;
    int64_t *d = k ? diff_n(v, n, k, &dn) : NULL;
    Buf b;
    buf_init(&b);
    pack_ints(k ? d : v, k ? dn : n, &b);
    size_t got = ppz_lzma_probe_len(b.data, b.len);
    buf_free(&b);
    free(d); free(v);
    return got;
}

static void pick_num_parents(const ColPlan *plan, size_t nc, size_t nr, long *nparent)
{
    for (size_t i = 0; i < nc; i++) nparent[i] = -1;
    if (nr < MIN_ROWS_FOR_PARENTS) return;
    size_t nd = 0, nn = 0;
    for (size_t p = 0; p < nc; p++) {
        if (plan[p].kind == K_DICT && plan[p].nalpha > 1) nd++;
        else if (plan[p].kind == K_NUM && !plan[p].in_group) nn++;
    }
    if (!nd || !nn) return;

    /* nominate on every `step`-th row when the full search is too big */
    size_t step = 1;
    while ((uint64_t)nd * nn * (nr / step) > NUMPAR_BUDGET) step++;
    size_t sn = (nr + step - 1) / step;
    uint64_t *base = calloc(nc, sizeof(uint64_t)), *best = calloc(nc, sizeof(uint64_t));
    int64_t *v = malloc((sn ? sn : 1) * sizeof(int64_t));
    int64_t *tmp = malloc((sn ? sn : 1) * sizeof(int64_t));
    int64_t *sid = malloc((sn ? sn : 1) * sizeof(int64_t));
    if (!base || !best || !v || !tmp || !sid) goto out;
    for (size_t p = 0; p < nc; p++) {
        if (plan[p].kind != K_NUM || plan[p].in_group) continue;
        for (size_t i = 0; i < sn; i++) v[i] = plan[p].ints[i * step];
        base[p] = best[p] = num_bits(v, sn, tmp);
    }
    for (size_t d = 0; d < nc; d++) {
        if (plan[d].kind != K_DICT || plan[d].nalpha < 2) continue;
        for (size_t i = 0; i < sn; i++) sid[i] = plan[d].ids[i * step];
        size_t *perm = argsort_ids(sid, sn, plan[d].nalpha);
        if (!perm) continue;
        for (size_t p = 0; p < nc; p++) {
            if (plan[p].kind != K_NUM || plan[p].in_group) continue;
            for (size_t i = 0; i < sn; i++) v[i] = plan[p].ints[perm[i] * step];
            uint64_t c = num_bits(v, sn, tmp);
            if (c < best[p]) { best[p] = c; nparent[p] = (long)d; }
        }
        free(perm);
    }
    /* measured, on every row: the nominee must actually compress smaller */
    for (size_t p = 0; p < nc; p++) {
        if (nparent[p] < 0) continue;
        const ColPlan *par = &plan[nparent[p]];
        size_t *perm = argsort_ids(par->ids, nr, par->nalpha);
        size_t with = perm ? num_probe(plan[p].ints, nr, perm) : (size_t)-1;
        size_t without = num_probe(plan[p].ints, nr, NULL);
        free(perm);
        if (with >= without) nparent[p] = -1;
    }
out:
    free(base); free(best); free(v); free(tmp); free(sid);
}

/* --------------------------------------------------------------------- json */

/* ASCII-only JSON strings: anything outside 0x20..0x7e, plus
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

/* The text pile, built and compressed; the uncompressed pile is dropped as
 * soon as it is compressed. */
typedef struct {
    Str                 **sg;
    const size_t         *sgn;
    size_t                nsg;
    const unsigned char  *has_nl;
    unsigned char        *front;
    SMeta                *sm;
    Buf                   z;
    int                   ok;
} PileJob;

static void build_pile(Str **sg, const size_t *sgn, size_t nsg,
                       const unsigned char *has_nl,
                       const unsigned char *front, Buf *txt, SMeta *sm);

static void pile_task(void *arg)
{
    PileJob *j = arg;
    Buf txt;
    buf_init(&txt);
    build_pile(j->sg, j->sgn, j->nsg, j->has_nl, j->front, &txt, j->sm);
    j->ok = !ppz_lzma_compress_as(txt.data, txt.len, &j->z, PPZ_XZ_TEXT);
    buf_free(&txt);
}

typedef struct { const Buf *in; Buf *out; int ok; } BinJob;

static void bins_task(void *arg)
{
    BinJob *j = arg;
    j->ok = !ppz_lzma_compress_as(j->in->data, j->in->len, j->out, PPZ_XZ_INTS);
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
    }
}

/* Are all the column names valid UTF-8? They travel in the JSON metadata,
 * and JSON cannot carry a stray byte: the escaper would turn 0xFF into
 * \u00ff, which comes back as C3 BF -- a renamed column. Tables read from
 * files never have such names (the readers validate UTF-8); a program using
 * this as a library can. */
static int names_are_utf8(const Table *t)
{
    for (size_t j = 0; j < t->ncols; j++) {
        const unsigned char *p = (const unsigned char *)t->names[j];
        while (*p) {
            unsigned c = *p;
            size_t need = c < 0x80 ? 0 : (c >= 0xC2 && c <= 0xDF) ? 1
                        : (c >= 0xE0 && c <= 0xEF) ? 2 : (c >= 0xF0 && c <= 0xF4) ? 3 : 9;
            if (need == 9) return 0;
            unsigned lo = 0x80, hi = 0xBF;
            if (c == 0xE0) lo = 0xA0; else if (c == 0xED) hi = 0x9F;
            else if (c == 0xF0) lo = 0x90; else if (c == 0xF4) hi = 0x8F;
            for (size_t k = 1; k <= need; k++) {
                unsigned x = p[k];
                if (x < (k == 1 ? lo : 0x80) || x > (k == 1 ? hi : 0xBF)) return 0;
            }
            p += need + 1;
        }
    }
    return 1;
}

/* The archive for table `t`, from its column plan (which this frees). */
static int encode_modelled(const Table *t, Buf *out, ColPlan *plan,
                           const Buf *derive_meta)
{
    size_t nc = t->ncols, nr = t->nrows;
    double tr = ppz_now();
    /* Numeric parents first: a column that is better sorted by a parent
     * stays out of the 2D groups, which keep file order. Measured on the CDC
     * vaccination table: 2.50 MB with the groups taking those columns, 1.69
     * MB without (plain xz: 2.27). */
    long *nparent = malloc((nc ? nc : 1) * sizeof(long));
    if (nparent) pick_num_parents(plan, nc, nr, nparent);
    size_t ngroups = 0;
    Group *groups = find_2d_groups(plan, nc, nr, nparent, &ngroups);
    Parents P = pick_parents(plan, nc, nr);
    ppz_trace("2d groups + dictionary parents", tr); tr = ppz_now();
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
            /* stable argsort of the parent's ids, then ids = ids[perm] */
            size_t *kv = argsort_ids(plan[par].ids, nr, plan[par].nalpha);
            tmp = malloc((nr ? nr : 1) * sizeof(int64_t));
            /* out of memory: the rows stay unpermuted, the archive comes out
             * wrong, and verification refuses it -- never a silent loss */
            for (size_t i = 0; kv && tmp && i < nr; i++) tmp[i] = ids[kv[i]];
            if (!kv || !tmp) { free(tmp); tmp = NULL; }
            free(kv);
            if (tmp) ids = tmp;
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
    ppz_trace("dictionary payloads", tr); tr = ppz_now();
    pick_text_parents(t, plan, nc, nr, tparent);
    ppz_trace("text parents (probes)", tr); tr = ppz_now();

    Str **text_cells = calloc(nc ? nc : 1, sizeof(Str *));
    Str **ex_cells = calloc(nc ? nc : 1, sizeof(Str *));
    for (size_t pos = 0; pos < nc; pos++) {
        ColPlan *c = &plan[pos];
        if (c->kind == K_TEXT) {
            Str *cells = malloc((nr ? nr : 1) * sizeof(Str));
            if (tparent[pos] >= 0) {
                /* same trick, same freeness: the decoder rebuilds the parent
                 * first and recomputes this stable argsort */
                size_t *kv = argsort_ids(plan[tparent[pos]].ids, nr,
                                         plan[tparent[pos]].nalpha);
                for (size_t i = 0; i < nr; i++)       /* !kv: see above */
                    cells[i] = table_at(t, kv ? kv[i] : i, pos);
                free(kv);
            } else {
                for (size_t i = 0; i < nr; i++) cells[i] = table_at(t, i, pos);
            }
            text_cells[pos] = cells;
            sg[nsg] = cells;
            sgn[nsg] = nr;
            nsg++;
        } else if (c->kind == K_NUM && !c->in_group) {
            /* sorted by its numeric parent, if it has one: the values are
             * permuted in place, so the warm-start values in the metadata
             * are the sorted ones too */
            if (nparent && nparent[pos] >= 0) {
                size_t *kv = argsort_ids(plan[nparent[pos]].ids, nr, plan[nparent[pos]].nalpha);
                int64_t *v = malloc((nr ? nr : 1) * sizeof(int64_t));
                if (kv && v) {
                    for (size_t i = 0; i < nr; i++) v[i] = c->ints[kv[i]];
                    free(c->ints);
                    c->ints = v;
                } else {
                    free(v);
                    nparent[pos] = -1;
                }
                free(kv);
            }
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
        /* unsigned throughout, for the same reason as diff_n */
        for (size_t i = 1; i < nr; i++)
            flat[w + (i - 1)] = (int64_t)((uint64_t)plan[groups[g].pos[0]].ints[i]
                              - (uint64_t)plan[groups[g].pos[0]].ints[i - 1]);
        size_t at2 = w + (nr - 1);
        for (size_t i = 1; i < nr; i++)
            for (size_t c = 1; c < w; c++) {
                uint64_t d1 = (uint64_t)plan[groups[g].pos[c]].ints[i]
                            - (uint64_t)plan[groups[g].pos[c]].ints[i - 1];
                uint64_t d0 = (uint64_t)plan[groups[g].pos[c - 1]].ints[i]
                            - (uint64_t)plan[groups[g].pos[c - 1]].ints[i - 1];
                flat[at2 + (i - 1) * (w - 1) + (c - 1)] = (int64_t)(d1 - d0);
            }
        size_t at = bins.len;
        pack_ints(flat, total, &bins);
        binsz[nbins++] = bins.len - at;
        free(flat);
    }

    /* ------------------------------------------------- pack the strings */
    SMeta *smeta = calloc(nsg ? nsg : 1, sizeof(SMeta));
    Buf lenbins;
    buf_init(&lenbins);
    size_t *lenbinsz = malloc((nsg + 1) * sizeof(size_t));
    size_t nlenbins = 0;

    unsigned char *has_nl = calloc(nsg ? nsg : 1, 1);
    if (!has_nl) { buf_free(&lenbins); free(lenbinsz); lenbinsz = NULL; }
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
    /* One text pile, one layout, one compression. The dictionary alphabets
     * (the first P.norder groups, sorted by construction, so neighbours share
     * long prefixes) are front-coded; nothing else is. That was the best
     * single fixed rule on the suite -- the old encoder built up to four
     * layouts and kept the smallest, which bought ~0.5% for four -9e passes.
     * The binary payload is final before the pile is built, so the two are
     * compressed at the same time. */
    Buf mz, bz, tz;
    buf_init(&mz); buf_init(&bz); buf_init(&tz);
    int rc = -1;
    if (!has_nl) goto done;

    /* length arrays always go last in the binary payload */
    buf_put(&bins, lenbins.data, lenbins.len);
    for (size_t i = 0; i < nlenbins; i++) binsz[nbins++] = lenbinsz[i];
    buf_free(&lenbins);
    free(lenbinsz);
    BinJob bj = { &bins, &bz, 0 };
    PpzBg *bins_bg = ppz_bg_start(bins_task, &bj);

    PileJob pile;
    memset(&pile, 0, sizeof(pile));
    pile.sg = sg; pile.sgn = sgn; pile.nsg = nsg; pile.has_nl = has_nl;
    pile.front = calloc(nsg ? nsg : 1, 1);
    pile.sm = smeta;
    buf_init(&pile.z);
    if (pile.front) {
        for (size_t g = 0; g < P.norder && g < nsg; g++) pile.front[g] = 1;
        ppz_trace("numeric payloads + pile prep", tr); tr = ppz_now();
        pile_task(&pile);
        ppz_trace("text pile xz", tr); tr = ppz_now();
    }
    free(pile.front);
    tz = pile.z;
    int use_fc = P.norder > 0;          /* the alphabets are front-coded */

    free(has_nl);
    ppz_bg_join(bins_bg);
    ppz_trace("binary payload xz (waited for)", tr); tr = ppz_now();
    if (!pile.ok || !bj.ok) goto done;

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
            if (c->nex) {
                buf_put(&meta, ",\"nex\":", 7);
                json_int(&meta, (long long)c->nex);
            }
            buf_putc(&meta, '}');
        } else {
            /* "nump": a numeric column stored sorted by a dictionary
             * parent. A new kind rather than a new key, so a build that
             * predates it refuses the archive instead of misreading it */
            long np = nparent ? nparent[pos] : -1;
            buf_put(&meta, np >= 0 ? "{\"kind\":\"nump\",\"dec\":" : "{\"kind\":\"num\",\"dec\":",
                    np >= 0 ? 21 : 20);
            json_int(&meta, c->dec);
            buf_put(&meta, ",\"k\":", 5);
            json_int(&meta, c->k);
            buf_put(&meta, ",\"warm\":[", 9);
            for (int i = 0; i < c->k; i++) {
                if (i) buf_putc(&meta, ',');
                json_int(&meta, (long long)c->ints[i]);
            }
            buf_putc(&meta, ']');
            if (np >= 0) {
                buf_put(&meta, ",\"parent\":", 10);
                json_int(&meta, (long long)np);
            }
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
        buf_putc(&meta, '}');
    }
    buf_put(&meta, "],\"nlenbins\":", 13);
    json_int(&meta, (long long)nlenbins);
    if (use_fc) buf_put(&meta, ",\"fc\":1", 7);
    if (derive_meta) buf_put(&meta, derive_meta->data, derive_meta->len);
    buf_putc(&meta, '}');

    /* ------------------------------------------------------- container */
    if (ppz_lzma_compress(meta.data, meta.len, &mz)) goto done;

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
    buf_free(&meta); buf_free(&bins);
    free(smeta); free(binsz); free(sg); free(sgn);
    for (size_t j = 0; j < nc; j++) { free(text_cells[j]); free(ex_cells[j]); }
    free(text_cells); free(ex_cells); free(tparent); free(nparent);
    for (size_t g = 0; g < ngroups; g++) free(groups[g].pos);
    free(groups);
    free(P.parent); free(P.order);
    plan_free(plan, nc);
    return rc;
}

/* -------------------------------------------------------- derived columns */

/* Numbers in text that another column already holds become references (the
 * format is in ppz.h). Measured in the improvements lab, 2026-09-28, before
 * a line of this was written: -20% chicago_crimes, -13% nyc_311, -12%
 * seattle_fire911, -9.0% over 14 tables, and the encode got faster because
 * the codec is handed less text. It found location = POINT(lon lat) rebuilt
 * from longitude + latitude (exact, or rounded), fee columns copying other
 * fee columns, a date sharing its year with `year`.
 *
 * The choice is made on a sample and never trial-encoded (one pass): column
 * j is a candidate for column t when j explains a number in t on at least
 * half the sampled rows where t has one. A source is never itself derived,
 * so the decoder always expands from finished values. */

#define DRV_SAMPLE     500
#define DRV_MAX_COLS   2048      /* the hit matrix is cols^2 x 2 bytes */

typedef struct { Str key; uint32_t col; size_t off; } DrvKey;

static int drvkey_cmp(const void *a, const void *b)
{
    const DrvKey *x = a, *y = b;
    if (x->key.n != y->key.n) return x->key.n < y->key.n ? -1 : 1;
    int c = memcmp(x->key.p, y->key.p, x->key.n);
    if (c) return c;
    return x->col < y->col ? -1 : x->col > y->col;
}

static int u32_cmp(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return x < y ? -1 : x > y;
}

/* The reference for one number in row `row`, or 0. Exact copies first, then
 * roundings; the first candidate that reproduces the number wins. */
static size_t drv_ref_for(const Table *t, size_t row, const uint32_t *cands,
                          size_t nc, const char *tok, size_t tn, char *ref)
{
    for (size_t k = 0; k < nc; k++) {
        Str v = table_at(t, row, cands[k]);
        if (v.n == tn && !memcmp(v.p, tok, tn))
            return (size_t)snprintf(ref, 16, "\x01%zu:\x02", k);
    }
    int d = drv_decimals(tok, tn);
    char r[DRV_MAX_TOK + 2];
    for (size_t k = 0; k < nc; k++) {
        Str v = table_at(t, row, cands[k]);
        if (v.n > DRV_MAX_TOK || drv_decimals(v.p, v.n) <= d) continue;
        size_t rn = drv_round(v.p, v.n, d, r);
        if (rn == tn && !memcmp(r, tok, tn))
            return (size_t)snprintf(ref, 16, "\x01%zu:%d\x02", k, d);
    }
    return 0;
}

/* Choose the derived columns and write the rewritten table into `out`, which
 * borrows every untouched cell from `t` (so `t` must outlive it) and the
 * "derive" metadata into `meta`. Returns the number of derived columns. */
static size_t derive_plan(const Table *t, Table *out, Buf *meta)
{
    size_t nc = t->ncols, nr = t->nrows;
    if (nr == 0 || nc < 2 || nc > DRV_MAX_COLS) return 0;

    /* a column already holding the marker bytes cannot carry references */
    unsigned char *skip = calloc(nc, 1);
    size_t *seen = calloc(nc, sizeof(size_t));
    uint16_t *hits = calloc(nc * nc, sizeof(uint16_t));
    if (!skip || !seen || !hits) { free(skip); free(seen); free(hits); return 0; }
    for (size_t i = 0; i < nr; i++)
        for (size_t j = 0; j < nc; j++) {
            if (skip[j]) continue;
            Str c = table_at(t, i, j);
            if (memchr(c.p, '\x01', c.n) || memchr(c.p, '\x02', c.n)) skip[j] = 1;
        }

    /* For each sampled row: every number another cell could supply -- the
     * cell itself, and each of its roundings -- sorted, so each number in a
     * cell is one binary search. */
    size_t step = nr / DRV_SAMPLE ? nr / DRV_SAMPLE : 1;
    DrvKey *keys = NULL;
    size_t kcap = 0;
    Buf rbuf;
    buf_init(&rbuf);
    uint32_t *used = malloc(64 * sizeof(uint32_t));
    size_t ucap = 64;
    for (size_t i = 0; i < nr && used; i += step) {
        /* rounded strings go into rbuf, which may move as it grows, so
         * their keys hold an offset until the row is done */
        size_t nk = 0;
        rbuf.len = 0;
        for (size_t j = 0; j < nc; j++) {
            Str c = table_at(t, i, j);
            if (c.n > DRV_MAX_TOK || !drv_is_number(c.p, c.n)) continue;
            int k = drv_decimals(c.p, c.n);
            if (nk + (size_t)k + 1 > kcap) {
                size_t nc2 = (nk + (size_t)k + 1) * 2;
                DrvKey *k2 = realloc(keys, nc2 * sizeof(DrvKey));
                if (!k2) { nk = 0; break; }
                keys = k2;
                kcap = nc2;
            }
            keys[nk++] = (DrvKey){ c, (uint32_t)j, 0 };
            for (int d = 0; d < k; d++) {
                char r[DRV_MAX_TOK + 2];
                size_t rn = drv_round(c.p, c.n, d, r);
                keys[nk++] = (DrvKey){ { NULL, rn }, (uint32_t)j, rbuf.len };
                buf_put(&rbuf, r, rn);
            }
        }
        for (size_t q = 0; q < nk; q++)
            if (!keys[q].key.p) keys[q].key.p = (const char *)rbuf.data + keys[q].off;
        if (nk) qsort(keys, nk, sizeof(DrvKey), drvkey_cmp);

        for (size_t tc = 0; tc < nc; tc++) {
            if (skip[tc]) continue;
            Str c = table_at(t, i, tc);
            size_t nu = 0;
            int any = 0;
            for (size_t p = 0; p < c.n; ) {
                size_t tn = drv_token(c.p, c.n, p);
                if (!tn) { p++; continue; }
                any = 1;
                if (tn <= DRV_MAX_TOK && nk) {
                    DrvKey probe = { { c.p + p, tn }, 0, 0 };
                    size_t lo = 0, hi = nk;       /* first key >= (tok, col 0) */
                    while (lo < hi) {
                        size_t mid = (lo + hi) / 2;
                        if (drvkey_cmp(&keys[mid], &probe) < 0) lo = mid + 1;
                        else hi = mid;
                    }
                    for (size_t q = lo; q < nk && keys[q].key.n == tn
                         && !memcmp(keys[q].key.p, c.p + p, tn); q++) {
                        if (keys[q].col == tc) continue;
                        if (nu == ucap) {
                            ucap *= 2;
                            uint32_t *nu2 = realloc(used, ucap * sizeof(uint32_t));
                            if (!nu2) { free(used); used = NULL; break; }
                            used = nu2;
                        }
                        used[nu++] = keys[q].col;
                    }
                    if (!used) break;
                }
                p += tn;
            }
            if (!used) break;
            if (!any) continue;
            seen[tc]++;
            qsort(used, nu, sizeof(uint32_t), u32_cmp);
            for (size_t q = 0; q < nu; q++)
                if (q == 0 || used[q] != used[q - 1]) hits[tc * nc + used[q]]++;
        }
    }
    free(keys);
    buf_free(&rbuf);
    int ok = used != NULL;
    free(used);

    /* choose, left to right */
    uint32_t (*cands)[DRV_MAX_CANDS] = calloc(nc, sizeof(*cands));
    size_t *ncand = calloc(nc, sizeof(size_t));
    unsigned char *banned = calloc(nc, 1);
    size_t nd = 0;
    if (!cands || !ncand || !banned) ok = 0;
    for (size_t tc = 0; ok && tc < nc; tc++) {
        if (banned[tc] || skip[tc] || !seen[tc]) continue;
        for (size_t j = 0; j < nc && ncand[tc] < DRV_MAX_CANDS; j++) {
            if (j == tc || banned[j] || ncand[j]) continue;
            if (2 * (size_t)hits[tc * nc + j] >= seen[tc])
                cands[tc][ncand[tc]++] = (uint32_t)j;
        }
        if (!ncand[tc]) continue;
        nd++;
        for (size_t k = 0; k < ncand[tc]; k++) banned[cands[tc][k]] = 1;
    }
    free(skip); free(seen); free(hits); free(banned);
    if (!ok || !nd) { free(cands); free(ncand); return 0; }

    /* rewrite the derived columns; every other cell is borrowed */
    table_init(out);
    out->ncols = nc;
    out->nrows = nr;
    out->names = calloc(nc, sizeof(char *));
    out->cells = malloc(nr * nc * sizeof(Str));
    size_t *roff = malloc(nr * nc * sizeof(size_t));
    if (!out->names || !out->cells || !roff) goto fail;
    for (size_t j = 0; j < nc; j++)
        if (!(out->names[j] = strdup(t->names[j]))) goto fail;
    memcpy(out->cells, t->cells, nr * nc * sizeof(Str));
    for (size_t i = 0; i < nr * nc; i++) roff[i] = SIZE_MAX;
    Buf cell;
    buf_init(&cell);
    for (size_t i = 0; i < nr; i++) {
        for (size_t tc = 0; tc < nc; tc++) {
            if (!ncand[tc]) continue;
            Str c = table_at(t, i, tc);
            cell.len = 0;
            size_t from = 0;
            int changed = 0;
            for (size_t p = 0; p < c.n; ) {
                size_t tn = drv_token(c.p, c.n, p);
                if (!tn) { p++; continue; }
                char ref[16];
                size_t rl = tn <= DRV_MAX_TOK
                    ? drv_ref_for(t, i, cands[tc], ncand[tc], c.p + p, tn, ref) : 0;
                if (rl) {
                    buf_put(&cell, c.p + from, p - from);
                    buf_put(&cell, ref, rl);
                    from = p + tn;
                    changed = 1;
                }
                p += tn;
            }
            if (!changed) continue;
            buf_put(&cell, c.p + from, c.n - from);
            roff[i * nc + tc] = out->arena.len;
            buf_put(&out->arena, cell.data, cell.len);
            out->cells[i * nc + tc].n = cell.len;
        }
    }
    buf_free(&cell);
    for (size_t i = 0; i < nr * nc; i++)
        if (roff[i] != SIZE_MAX)
            out->cells[i].p = (const char *)out->arena.data + roff[i];
    free(roff);

    buf_put(meta, ",\"derive\":[", 11);
    int first = 1;
    for (size_t tc = 0; tc < nc; tc++) {
        if (!ncand[tc]) continue;
        if (!first) buf_putc(meta, ',');
        first = 0;
        buf_putc(meta, '[');
        json_int(meta, (long long)tc);
        buf_put(meta, ",[", 2);
        for (size_t k = 0; k < ncand[tc]; k++) {
            if (k) buf_putc(meta, ',');
            json_int(meta, (long long)cands[tc][k]);
        }
        buf_put(meta, "]]", 2);
    }
    buf_putc(meta, ']');
    free(cands); free(ncand);
    return nd;

fail:
    free(roff);
    if (!out->names) out->ncols = 0;
    table_free(out);
    free(cands); free(ncand);
    return 0;
}

/* ------------------------------------------------------------ the encoder */

/* One pass. Mahdi's rule, 2026-09-29: "one algorithm to reorder, one
 * compression algorithm" -- no encoding the table two ways and keeping the
 * smaller, and no checking the result against plain xz afterwards.
 *
 * What that replaced, measured on the 39-table suite before it went: a
 * second full encode without the lenient columns, a third without the 2D
 * groups, up to four text-pile layouts, and xz + bzip2 of the whole table as
 * CSV to guarantee "never larger than plain xz". Together: 2.3x the time
 * (3x on one core, where the CSV check alone was 69%) for 2.5% smaller
 * output. The fixed rules below were each the best single choice measured:
 * lenient columns where the screen says they can win (per column), 2D
 * groups on, the
 * alphabets front-coded, text parents chosen by probe. Still smaller than
 * plain xz -9e on every suite table but one, where it ties.
 *
 * Derived columns (2026-10-01) run first, on a sample, as one more fixed
 * rule. Measured on the suite before shipping: -5.1% in
 * total, -19% chicago_crimes, -16% nyc_collisions, -12% nyc_311 and
 * seattle_fire911, 31 of 39 tables byte-identical, encode time down 5%.
 * The one loss is xs_noaa_gsoy_ord, +78 bytes, where *_ATTRIBUTES flag
 * columns match each other by coincidence.
 *
 * Column names must be UTF-8 (the JSON metadata carries them); the readers
 * guarantee it, so only a program using this as a library can be refused. */
int ppz_encode(const Table *t, Buf *out)
{
    if (!names_are_utf8(t)) return -1;
    double tr = ppz_now();
    Table dt;
    Buf dmeta;
    buf_init(&dmeta);
    size_t nd = derive_plan(t, &dt, &dmeta);
    const Table *src = nd ? &dt : t;
    ppz_trace("derived columns", tr);
    tr = ppz_now();
    ColPlan *plan = classify(src);
    ppz_trace("classify", tr);
    tr = ppz_now();
    int rc = plan ? encode_modelled(src, out, plan, nd ? &dmeta : NULL) : -1;
    ppz_trace("== encode", tr);
    if (nd) table_free(&dt);
    buf_free(&dmeta);
    return rc;
}
