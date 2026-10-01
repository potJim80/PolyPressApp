/* Decoding a Polypress archive.
 *
 * Where a line here looks gratuitously specific -- the stable argsort, the
 * warm-start handling in undiff, the two string layouts -- it is the exact
 * inverse of a decision in ppz_encode.c, and the archive format depends on it.
 *
 * This reads files other people made, so every count, size and index in the
 * metadata is a CLAIM, checked against the bytes actually present before
 * anything is allocated or read from it. The metadata is JSON inside the
 * archive; a header can be perfectly well-formed and still lie. Every column
 * must also account for exactly `nrows` cells: a short column used to be
 * padded with empties, which turns a damaged archive into a table that looks
 * fine and is wrong. The tests in tests/t_hostile.c build such liars.
 */

#include "ppz.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ESCAPE 255

/* Far above any real table (a trillion rows), far below where size
 * arithmetic on it could overflow. */
#define MAX_ROWS ((uint64_t)1 << 40)

/* A count from the metadata: a non-negative integer no larger than `max`.
 * Absent is 0 when `absent_ok`. Anything else -- negative, fractional, a
 * string, too large -- is a lie, and the archive is refused. */
static int count_of(const Js *j, uint64_t max, int absent_ok, size_t *out)
{
    if (!j) { *out = 0; return absent_ok ? 0 : -1; }
    if (j->kind != JS_NUM || !j->is_int || j->inum < 0 || (uint64_t)j->inum > max)
        return -1;
    *out = (size_t)j->inum;
    return 0;
}

/* a*b, or -1 when it would not fit in a size_t */
static int mul_ok(size_t a, size_t b, size_t *out)
{
    if (a && b > SIZE_MAX / a) return -1;
    *out = a * b;
    return 0;
}

/* --------------------------------------------------------------- varints */

/* Mirror of fast.unpack_ints. `buf` is width byte, then n head bytes, then
 * the escaped tail at 4 or 8 bytes each. */
static int64_t *unpack_ints(const uint8_t *buf, size_t buflen, size_t n)
{
    if (n == 0) return calloc(1, sizeof(int64_t));
    if (buflen < 1 || n > buflen - 1) return NULL;
    int width = buf[0];
    const uint8_t *head = buf + 1;

    size_t nbig = 0;
    for (size_t i = 0; i < n; i++) if (head[i] == ESCAPE) nbig++;

    const uint8_t *tail = buf + 1 + n;
    size_t need = (width == 8 ? 8 : 4) * nbig;       /* nbig <= n < buflen */
    if (need > buflen - 1 - n) return NULL;

    int64_t *out = malloc(n * sizeof(int64_t));
    if (!out) return NULL;

    size_t t = 0;
    for (size_t i = 0; i < n; i++) {
        uint64_t u = head[i];
        if (u == ESCAPE) {
            if (width == 8) {
                uint64_t v;
                memcpy(&v, tail + 8 * t, 8);
                u = v;                       /* stored little-endian */
            } else {
                uint32_t v;
                memcpy(&v, tail + 4 * t, 4);
                u = v;
            }
            t++;
        }
        /* unzigzag: (u >> 1) ^ -(u & 1) */
        out[i] = (int64_t)(u >> 1) ^ -(int64_t)(u & 1);
    }
    return out;
}

/* ------------------------------------------------------------ stable sort */

typedef struct { int64_t v; size_t i; } KV;

static int kv_cmp(const void *a, const void *b)
{
    const KV *x = a, *y = b;
    if (x->v < y->v) return -1;
    if (x->v > y->v) return 1;
    /* Ties broken by original position. qsort is not stable, so the index is
     * folded into the comparison -- this reproduces numpy's
     * argsort(kind="stable"), which the encoder used to permute the column
     * and which the decoder must reproduce exactly or the rows come back
     * shuffled. */
    return x->i < y->i ? -1 : (x->i > y->i ? 1 : 0);
}

static size_t *stable_argsort(const int64_t *v, size_t n)
{
    KV *kv = malloc(n * sizeof(KV));
    size_t *out = malloc(n * sizeof(size_t));
    if (!kv || !out) { free(kv); free(out); return NULL; }
    for (size_t i = 0; i < n; i++) { kv[i].v = v[i]; kv[i].i = i; }
    qsort(kv, n, sizeof(KV), kv_cmp);
    for (size_t i = 0; i < n; i++) out[i] = kv[i].i;
    free(kv);
    return out;
}

/* ---------------------------------------------------------------- undiff */

/* Invert k-fold differencing. `warm` holds the first k ORIGINAL values, not
 * the differences, so each level's leading term is re-derived as the first
 * element of the j-th difference of warm -- same as fast._undiff. */
static int64_t *undiff(int64_t *d, size_t dn, const int64_t *warm, int k,
                       size_t *out_n)
{
    int64_t *a = d;
    size_t n = dn;
    for (int j = k - 1; j >= 0; j--) {
        int64_t first;
        if (j == 0) {
            first = warm[0];
        } else {
            /* j-th finite difference of warm, first element.
             *
             * `len` used to be `k` unclamped while tmp is eight elements, so a
             * header claiming k = 2^20 walked a million entries off the end of
             * a stack array -- a segfault from a well-formed archive. The
             * caller now refuses k outside 0..4, which is all the encoder can
             * emit; this clamp is the second line of defence. */
            int64_t tmp[8];
            for (int i = 0; i <= k && i < 8; i++) tmp[i] = warm[i];
            int len = k < 8 ? k : 8;
            for (int r = 0; r < j; r++) {
                for (int i = 0; i < len - 1; i++)
                    tmp[i] = (int64_t)((uint64_t)tmp[i + 1] - (uint64_t)tmp[i]);
                len--;
            }
            first = tmp[0];
        }
        int64_t *b = malloc((n + 1) * sizeof(int64_t));
        if (!b) { free(a); return NULL; }
        b[0] = first;
        memcpy(b + 1, a, n * sizeof(int64_t));
        n++;
        /* unsigned: the encoder's differences wrap the same way, and signed
         * overflow would be undefined rather than merely wrong */
        uint64_t run = 0;
        for (size_t i = 0; i < n; i++) { run += (uint64_t)b[i]; b[i] = (int64_t)run; }
        free(a);
        a = b;
    }
    *out_n = n;
    return a;
}

/* --------------------------------------------------------------- format */

/* Scaled integers back to their printed text -- mirror of tcz.c fmt_fixed,
 * one value at a time so the caller can append into a growable buffer. */
static void fmt_fixed_one(Buf *b, int64_t v, int dec)
{
    char digits[24];
    if (v < 0) { buf_putc(b, '-'); v = -v; }
    int64_t scale = 1;
    for (int d = 0; d < dec; d++) scale *= 10;
    int64_t q = dec ? v / scale : v;
    int64_t r = dec ? v % scale : 0;

    int nd = 0;
    if (q == 0) digits[nd++] = '0';
    while (q > 0) { digits[nd++] = (char)('0' + (q % 10)); q /= 10; }
    while (nd > 0) buf_putc(b, digits[--nd]);

    if (dec) {
        buf_putc(b, '.');
        for (int d = dec - 1; d >= 0; d--) {
            int64_t p = 1;
            for (int e = 0; e < d; e++) p *= 10;
            buf_putc(b, (char)('0' + ((r / p) % 10)));
        }
    }
}

/* ---------------------------------------------------------------- decode */

typedef struct {
    Str   *cells;      /* nrows entries */
    Buf    store;      /* backing bytes when this column was formatted */
    int    owned;      /* whether store is in use */
} Col;

static int decode_fallback(const uint8_t *blob, size_t n, Table *out, int bz)
{
    Buf plain;
    buf_init(&plain);
    int r = bz ? ppz_bz2_decompress(blob + 4, n - 4, &plain)
               : ppz_lzma_decompress(blob + 4, n - 4, &plain);
    if (r) { buf_free(&plain); return -1; }

    /* The fallback stores canonical CSV; parse it where it lies. */
    int rc = table_parse_csv(out, plain.data, plain.len);
    buf_free(&plain);
    return rc;
}

/* Overwrite the cells a numeric column could not represent -- blanks, "-0.0",
 * a stray decimal count. The encoder filled those slots with a neighbouring
 * value so the column kept its full length, and stored the originals by
 * position and as text. Positions were delta-coded.
 *
 * Every bound is checked against the archive's own arrays rather than trusted,
 * because this reads files other people made. Mirrors fast._apply_exceptions.
 * Returns 0 on success. */
static int read_exceptions(const Js *sp, size_t nrows,
                           const uint8_t **cut, const size_t *cutlen,
                           size_t nbins, Str **sgroup, const size_t *sgroup_n,
                           size_t ngroups_s, size_t *bi, size_t *ti,
                           Str *cells)
{
    size_t nex;
    if (count_of(js_get(sp, "nex"), nrows, 1, &nex)) return -1;
    if (!nex) return 0;
    if (*bi >= nbins || *ti >= ngroups_s) return -1;

    int64_t *gaps = unpack_ints(cut[*bi], cutlen[*bi], nex);
    (*bi)++;
    if (!gaps) return -1;
    Str *vals = sgroup[*ti];
    size_t nvals = sgroup_n[*ti];
    (*ti)++;

    if (nvals != nex || !vals) { free(gaps); return -1; }
    uint64_t acc = 0;
    for (size_t i = 0; i < nex; i++) {
        if (gaps[i] < 0 || (uint64_t)gaps[i] >= nrows) { free(gaps); return -1; }
        acc += (uint64_t)gaps[i];
        if (acc >= nrows) { free(gaps); return -1; }
        cells[acc] = vals[i];
    }
    free(gaps);
    return 0;
}

/* Expand the references a PPZ2 archive stores in its derived columns (the
 * format is in ppz.h). Sources are never derived, so they are final here.
 * Everything is checked: a candidate list naming a column twice or a derived
 * column, a reference to a candidate that is not there, a source that is not
 * a number, a marker byte outside a reference -- none of them is something
 * the encoder writes, and each is refused. */
static int derive_restore(Table *t, const Js *jd)
{
    size_t nc = t->ncols, nr = t->nrows;
    if (!jd || jd->kind != JS_ARR || jd->count == 0 || jd->count > nc) return -1;
    size_t nd = jd->count;
    size_t *dcol = calloc(nd, sizeof(size_t));
    size_t (*cand)[DRV_MAX_CANDS] = calloc(nd, sizeof(*cand));
    size_t *ncand = calloc(nd, sizeof(size_t));
    unsigned char *role = calloc(nc ? nc : 1, 1);     /* 1 derived, 2 source */
    size_t *roff = NULL, *rlen = NULL;
    Buf side;
    buf_init(&side);
    int rc = -1;
    if (!dcol || !cand || !ncand || !role) goto out;
    for (size_t e = 0; e < nd; e++) {
        const Js *it = &jd->items[e];
        if (it->kind != JS_ARR || it->count != 2) goto out;
        const Js *jt = &it->items[0], *jc = &it->items[1];
        if (jt->kind != JS_NUM || !jt->is_int || jt->inum < 0 || (uint64_t)jt->inum >= nc)
            goto out;
        if (jc->kind != JS_ARR || jc->count == 0 || jc->count > DRV_MAX_CANDS) goto out;
        dcol[e] = (size_t)jt->inum;
        if (role[dcol[e]]) goto out;
        role[dcol[e]] = 1;
        ncand[e] = jc->count;
        for (size_t k = 0; k < jc->count; k++) {
            const Js *x = &jc->items[k];
            if (x->kind != JS_NUM || !x->is_int || x->inum < 0 || (uint64_t)x->inum >= nc)
                goto out;
            cand[e][k] = (size_t)x->inum;
        }
    }
    for (size_t e = 0; e < nd; e++)
        for (size_t k = 0; k < ncand[e]; k++) {
            if (role[cand[e][k]] == 1) goto out;      /* derived, or itself */
            role[cand[e][k]] = 2;
        }

    roff = malloc((nr * nd ? nr * nd : 1) * sizeof(size_t));
    rlen = malloc((nr * nd ? nr * nd : 1) * sizeof(size_t));
    if (!roff || !rlen) goto out;
    for (size_t i = 0; i < nr; i++) {
        for (size_t e = 0; e < nd; e++) {
            Str c = table_at(t, i, dcol[e]);
            roff[i * nd + e] = SIZE_MAX;
            if (!memchr(c.p, '\x01', c.n)) {
                if (memchr(c.p, '\x02', c.n)) goto out;
                continue;
            }
            size_t at = side.len;
            for (size_t p = 0; p < c.n; ) {
                char ch = c.p[p];
                if (ch == '\x02') goto out;
                if (ch != '\x01') { buf_putc(&side, ch); p++; continue; }
                /* \x01 <k> : [<d>] \x02 */
                p++;
                if (p >= c.n || c.p[p] < '0' || c.p[p] > '9') goto out;
                size_t k = (size_t)(c.p[p++] - '0');
                if (k >= ncand[e] || p >= c.n || c.p[p++] != ':') goto out;
                int d = -1;
                while (p < c.n && c.p[p] >= '0' && c.p[p] <= '9') {
                    d = (d < 0 ? 0 : d) * 10 + (c.p[p++] - '0');
                    if (d > DRV_MAX_TOK) goto out;
                }
                if (p >= c.n || c.p[p++] != '\x02') goto out;
                Str v = table_at(t, i, cand[e][k]);
                if (v.n > DRV_MAX_TOK || !drv_is_number(v.p, v.n)) goto out;
                if (d < 0) {
                    buf_put(&side, v.p, v.n);
                } else {
                    char r[DRV_MAX_TOK + 2];
                    size_t rn = drv_round(v.p, v.n, d, r);
                    if (!rn) goto out;
                    buf_put(&side, r, rn);
                }
            }
            roff[i * nd + e] = at;
            rlen[i * nd + e] = side.len - at;
        }
    }

    /* one new arena: every cell, expanded ones from `side` */
    {
        size_t total = side.len;
        for (size_t i = 0; i < nr * nc; i++) total += t->cells[i].n;
        Buf na;
        buf_init(&na);
        buf_need(&na, total + 1);
        size_t *col_e = malloc((nc ? nc : 1) * sizeof(size_t));
        if (!col_e) goto out;
        for (size_t j = 0; j < nc; j++) col_e[j] = SIZE_MAX;
        for (size_t e = 0; e < nd; e++) col_e[dcol[e]] = e;
        for (size_t i = 0; i < nr; i++)
            for (size_t j = 0; j < nc; j++) {
                Str *c = &t->cells[i * nc + j];
                size_t e = col_e[j];
                size_t at = na.len;
                if (e != SIZE_MAX && roff[i * nd + e] != SIZE_MAX) {
                    buf_put(&na, side.data + roff[i * nd + e], rlen[i * nd + e]);
                    c->n = rlen[i * nd + e];
                } else {
                    buf_put(&na, c->p, c->n);
                }
                c->p = (const char *)(uintptr_t)at;     /* fixed below */
            }
        for (size_t i = 0; i < nr * nc; i++)
            t->cells[i].p = (const char *)na.data + (uintptr_t)t->cells[i].p;
        free(col_e);
        buf_free(&t->arena);
        t->arena = na;
    }
    rc = 0;
out:
    free(dcol); free(cand); free(ncand); free(role); free(roff); free(rlen);
    buf_free(&side);
    return rc;
}

int ppz_decode(const uint8_t *blob, size_t n, Table *out)
{
    if (n < 4) return -1;
    if (!memcmp(blob, PPZ_MAGIC_RAW_XZ, 4)) return decode_fallback(blob, n, out, 0);
    if (!memcmp(blob, PPZ_MAGIC_RAW_BZ, 4)) return decode_fallback(blob, n, out, 1);
    int derived = !memcmp(blob, PPZ_MAGIC_DERIVED, 4);
    if (memcmp(blob, PPZ_MAGIC, 4) && memcmp(blob, PPZ_MAGIC_V0, 4) && !derived)
        return -1;
    if (n < 16) return -1;

    size_t ml = ((size_t)blob[4] << 24) | ((size_t)blob[5] << 16) |
                ((size_t)blob[6] << 8) | blob[7];
    size_t bl = ((size_t)blob[8] << 24) | ((size_t)blob[9] << 16) |
                ((size_t)blob[10] << 8) | blob[11];
    size_t off = 16;
    if (off + ml + bl > n) return -1;

    Buf metab, rawb, txtb;
    buf_init(&metab); buf_init(&rawb); buf_init(&txtb);
    if (ppz_lzma_decompress(blob + off, ml, &metab)) goto fail;
    off += ml;
    if (ppz_lzma_decompress(blob + off, bl, &rawb)) goto fail;
    off += bl;
    if (ppz_lzma_decompress(blob + off, n - off, &txtb)) goto fail;

    Js *meta = js_parse((const char *)metab.data, metab.len);
    if (!meta) goto fail;

    const Js *jcolumns = js_get(meta, "columns");
    const Js *jcols    = js_get(meta, "cols");
    const Js *jbins    = js_get(meta, "bins");
    const Js *jsmeta   = js_get(meta, "smeta");
    const Js *jorder   = js_get(meta, "order");
    const Js *jgroups  = js_get(meta, "groups");
    if (!jcolumns || !jcols || !jbins || !jsmeta || !jorder || !jgroups)
        goto fail_meta;
    /* derived columns only in a PPZ2, and a PPZ2 always has them */
    const Js *jderive = js_get(meta, "derive");
    if (!jderive != !derived) goto fail_meta;

    size_t nrows, nlen;
    size_t ncols = jcols->count;
    size_t nbins = jbins->count;
    if (jcols->kind != JS_ARR || jbins->kind != JS_ARR || jsmeta->kind != JS_ARR
        || jorder->kind != JS_ARR || jgroups->kind != JS_ARR || jcolumns->kind != JS_ARR)
        goto fail_meta;
    if (count_of(js_get(meta, "nrows"), MAX_ROWS, 0, &nrows)) goto fail_meta;
    if (count_of(js_get(meta, "nlenbins"), nbins, 1, &nlen)) goto fail_meta;
    /* a table with no columns has no rows either; claiming 2^40 of them made
     * the assembly below loop for hours over nothing */
    if (ncols == 0 && nrows != 0) goto fail_meta;

    /* slice the concatenated binary payload */
    const uint8_t **cut = calloc(nbins ? nbins : 1, sizeof(uint8_t *));
    size_t *cutlen = calloc(nbins ? nbins : 1, sizeof(size_t));
    if (!cut || !cutlen) goto fail_meta;
    {
        size_t at = 0;
        for (size_t i = 0; i < nbins; i++) {
            size_t sz;
            if (count_of(&jbins->items[i], rawb.len, 0, &sz)) goto fail_cuts;
            if (sz > rawb.len - at) goto fail_cuts;
            cut[i] = rawb.data + at;
            cutlen[i] = sz;
            at += sz;
        }
    }

    /* ------------------------------------------------- string groups */
    /* Mirror of fast._unpack_strings. Two layouts: newline-joined (the usual
     * case) and explicit lengths (only when a cell contains a newline). */
    size_t ngroups_s = jsmeta->count;
    Str  **sgroup = calloc(ngroups_s ? ngroups_s : 1, sizeof(Str *));
    size_t *sgroup_n = calloc(ngroups_s ? ngroups_s : 1, sizeof(size_t));
    /* Front-coded words are rebuilt from a prefix plus a remainder, so unlike
     * every other group they are not slices into the text blob and need
     * storage of their own. One owned buffer per group keeps the pointers
     * stable -- appending them all into one arena would invalidate earlier
     * groups every time it grew. */
    uint8_t **fcown = calloc(ngroups_s ? ngroups_s : 1, sizeof(uint8_t *));
    if (!sgroup || !sgroup_n || !fcown) goto fail_cuts;
    /* the dictionary alphabets are the first `norder` groups, by construction */
    size_t norder = jorder->count;
    int fc_on = (int)js_int(js_get(meta, "fc"), 0);
    {
        size_t at = 0, li = 0;
        size_t first_len_bin = nbins - nlen;        /* nlen <= nbins, checked */
        for (size_t g = 0; g < ngroups_s; g++) {
            const Js *m = &jsmeta->items[g];
            size_t cnt, nb;
            if (count_of(js_get(m, "n"), MAX_ROWS, 0, &cnt)) goto fail_sgroup;
            if (count_of(js_get(m, "b"), txtb.len - at, 0, &nb)) goto fail_sgroup;
            int    nl  = (int)js_int(js_get(m, "nl"), 1);
            const char *chunk = (const char *)txtb.data + at;
            at += nb;
            /* The words have to be in the bytes: newline-joined needs n-1
             * newlines, front-coded a prefix byte each, and explicit lengths
             * a byte each in their length bin. So a count the bytes cannot
             * hold is refused here, before `cnt` sizes any allocation. */
            if (!nl) {
                if (li >= nlen || first_len_bin + li >= nbins) goto fail_sgroup;
                if (cnt > cutlen[first_len_bin + li]) goto fail_sgroup;
            } else if (cnt > nb + 1) goto fail_sgroup;
            sgroup_n[g] = cnt;
            if (cnt == 0) { sgroup[g] = NULL; if (!nl) li++; continue; }
            Str *arr = malloc(cnt * sizeof(Str));
            if (!arr) goto fail_sgroup;
            /* Front-coding is recorded per group. The archive-level "fc",
             * meaning "the first `norder` groups", is the older spelling and
             * is still honoured -- reading such an archive under the new rule
             * would return wrong strings rather than an error. */
            int fc_g = (int)js_int(js_get(m, "fc"), 0)
                       || (fc_on && g < norder);
            if (nl && fc_g) {
                /* cnt prefix-length bytes, then newline-joined remainders */
                if (nb < cnt) { free(arr); goto fail_sgroup; }
                const unsigned char *pl = (const unsigned char *)chunk;
                const char *rest = chunk + cnt;
                size_t restn = nb - cnt;
                size_t *offs = malloc(cnt * sizeof(size_t));
                size_t *wl = malloc(cnt * sizeof(size_t));
                if (!offs || !wl) { free(offs); free(wl); free(arr);
                                    goto fail_sgroup; }
                Buf ob, prevw;
                buf_init(&ob);
                buf_init(&prevw);
                size_t start = 0, k = 0;
                for (size_t i = 0; i <= restn && k < cnt; i++) {
                    if (i == restn || rest[i] == '\n') {
                        /* a crafted archive can claim a prefix longer than the
                         * previous word; clamp rather than read past it */
                        size_t sh = pl[k];
                        if (sh > prevw.len) sh = prevw.len;
                        offs[k] = ob.len;
                        buf_put(&ob, (const char *)prevw.data, sh);
                        buf_put(&ob, rest + start, i - start);
                        wl[k] = sh + (i - start);
                        prevw.len = 0;
                        buf_put(&prevw, (const char *)ob.data + offs[k], wl[k]);
                        k++;
                        start = i + 1;
                    }
                }
                while (k < cnt) { offs[k] = ob.len; wl[k] = 0; k++; }
                buf_free(&prevw);
                for (size_t j = 0; j < cnt; j++) {
                    arr[j].p = (const char *)ob.data + offs[j];
                    arr[j].n = wl[j];
                }
                fcown[g] = ob.data;      /* ownership moves to fcown */
                free(offs); free(wl);
            } else if (nl) {
                size_t start = 0, k = 0;
                for (size_t i = 0; i <= nb && k < cnt; i++) {
                    if (i == nb || chunk[i] == '\n') {
                        arr[k].p = chunk + start;
                        arr[k].n = i - start;
                        k++;
                        start = i + 1;
                    }
                }
                while (k < cnt) { arr[k].p = chunk + nb; arr[k].n = 0; k++; }
            } else {
                int64_t *lens = unpack_ints(cut[first_len_bin + li],
                                            cutlen[first_len_bin + li], cnt);
                li++;
                if (!lens) { free(arr); goto fail_sgroup; }
                /* every length inside the group's own bytes, and all of them
                 * accounting for exactly those bytes */
                size_t pos = 0;
                for (size_t k = 0; k < cnt; k++) {
                    if (lens[k] < 0 || (uint64_t)lens[k] > nb - pos) {
                        free(lens); free(arr); goto fail_sgroup;
                    }
                    arr[k].p = chunk + pos;
                    arr[k].n = (size_t)lens[k];
                    pos += (size_t)lens[k];
                }
                free(lens);
                if (pos != nb) { free(arr); goto fail_sgroup; }
            }
            sgroup[g] = arr;
        }
        if (li != nlen) goto fail_sgroup;          /* a length bin nobody used */
    }

    /* ----------------------------------------------------- columns */
    Col *cols = calloc(ncols ? ncols : 1, sizeof(Col));
    if (!cols) goto fail_sgroup;
    for (size_t j = 0; j < ncols; j++) buf_init(&cols[j].store);

    int64_t **ids_by_pos = calloc(ncols ? ncols : 1, sizeof(int64_t *));
    if (!ids_by_pos) goto fail_cols;

    /* exceptions belonging to grouped columns, held until the group is built */
    int64_t **grp_ex_pos = calloc(ncols ? ncols : 1, sizeof(int64_t *));
    Str     **grp_ex_val = calloc(ncols ? ncols : 1, sizeof(Str *));
    size_t   *grp_ex_n   = calloc(ncols ? ncols : 1, sizeof(size_t));
    if (!grp_ex_pos || !grp_ex_val || !grp_ex_n) goto fail_ids;

    size_t bi = 0, ti = 0;

    /* dictionary columns, in the order the encoder wrote them so a parent is
     * always rebuilt before its child */
    for (size_t oi = 0; oi < jorder->count; oi++) {
        size_t pos;
        if (count_of(&jorder->items[oi], ncols ? ncols - 1 : 0, 0, &pos) || pos >= ncols)
            goto fail_ids;
        if (cols[pos].cells) goto fail_ids;        /* listed twice */
        const Js *sp = &jcols->items[pos];
        /* A crafted archive can name more columns than it carries payloads
         * for; reading past these arrays would be an out-of-bounds read on a
         * file somebody else made. */
        if (ti >= ngroups_s || bi >= nbins) goto fail_ids;
        Str *alpha = sgroup[ti];
        size_t alpha_n = sgroup_n[ti];
        ti++;

        const Js *jw = js_get(sp, "w");
        int wbytes = 1;
        if (jw && jw->kind == JS_STR) {
            if (!strcmp(jw->str, "<u2")) wbytes = 2;
            else if (!strcmp(jw->str, "<u4")) wbytes = 4;
        }
        const uint8_t *src = cut[bi];
        size_t srclen = cutlen[bi];
        bi++;
        size_t have = srclen / (size_t)wbytes;
        if (have < nrows) goto fail_ids;

        int64_t *ids = malloc((nrows ? nrows : 1) * sizeof(int64_t));
        if (!ids) goto fail_ids;
        for (size_t i = 0; i < nrows; i++) {
            uint32_t v = 0;
            memcpy(&v, src + i * (size_t)wbytes, (size_t)wbytes);
            ids[i] = (int64_t)v;
        }

        const Js *jp = js_get(sp, "parent");
        if (jp && jp->kind == JS_NUM) {
            size_t par = (size_t)js_i64(jp, -1);
            if (par >= ncols || !ids_by_pos[par]) { free(ids); goto fail_ids; }
            size_t *perm = stable_argsort(ids_by_pos[par], nrows);
            if (!perm) { free(ids); goto fail_ids; }
            int64_t *fixed = malloc((nrows ? nrows : 1) * sizeof(int64_t));
            if (!fixed) { free(perm); free(ids); goto fail_ids; }
            /* encoder did ids = ids[perm]; invert with out[perm] = ids */
            for (size_t i = 0; i < nrows; i++) fixed[perm[i]] = ids[i];
            free(perm);
            free(ids);
            ids = fixed;
        }
        ids_by_pos[pos] = ids;

        Str *cells = malloc((nrows ? nrows : 1) * sizeof(Str));
        if (!cells) goto fail_ids;
        if (alpha_n == 0 && nrows) { free(cells); goto fail_ids; }
        for (size_t i = 0; i < nrows; i++) {
            size_t k = (size_t)ids[i];
            /* an id past the alphabet is not something the encoder writes */
            if (k >= alpha_n) { free(cells); goto fail_ids; }
            cells[i] = alpha[k];
        }
        cols[pos].cells = cells;
    }

    /* text and numeric columns, in positional order -- same as the encoder */
    for (size_t pos = 0; pos < ncols; pos++) {
        const Js *sp = &jcols->items[pos];
        const Js *jk = js_get(sp, "kind");
        if (!jk || jk->kind != JS_STR) goto fail_ids;

        /* An unrecognised column kind means this archive was written by a
         * newer encoder. Silently skipping it would leave that column empty
         * and hand back a table that looks fine and is wrong -- the worst
         * possible outcome for a decoder. Refuse the file instead. */
        if (strcmp(jk->str, "text") && strcmp(jk->str, "num") &&
            strcmp(jk->str, "dict") && strcmp(jk->str, "grp")) {
            fprintf(stderr, "polypress: archive uses column kind '%s', which "
                            "this build does not know -- refusing rather than "
                            "returning a wrong table\n", jk->str);
            goto fail_ids;
        }

        if (!strcmp(jk->str, "text")) {
            if (ti >= ngroups_s) goto fail_ids;
            Str *src = sgroup[ti];
            size_t cnt = sgroup_n[ti];
            ti++;
            if (cnt != nrows || cols[pos].cells) goto fail_ids;
            Str *cells = malloc((nrows ? nrows : 1) * sizeof(Str));
            if (!cells) goto fail_ids;
            for (size_t i = 0; i < nrows; i++) cells[i] = src[i];
            /* a text column may carry a parent, exactly like a dictionary
             * column; invert the same stable argsort */
            const Js *jtp = js_get(sp, "parent");
            if (jtp && jtp->kind == JS_NUM) {
                size_t par = (size_t)js_i64(jtp, -1);
                if (par >= ncols || !ids_by_pos[par]) { free(cells); goto fail_ids; }
                size_t *perm = stable_argsort(ids_by_pos[par], nrows);
                if (!perm) { free(cells); goto fail_ids; }
                Str *fixed = malloc((nrows ? nrows : 1) * sizeof(Str));
                if (!fixed) { free(perm); free(cells); goto fail_ids; }
                for (size_t i = 0; i < nrows; i++) fixed[perm[i]] = cells[i];
                free(perm); free(cells);
                cells = fixed;
            }
            cols[pos].cells = cells;
        } else if (!strcmp(jk->str, "num")) {
            size_t ks, decs;
            /* diff_order only ever emits 0..4, and fmt_fixed_one indexes a
             * 19-entry POW10 table. Anything outside those was not written by
             * this encoder, and trusting it reads off the end of an array. */
            if (count_of(js_get(sp, "k"), 4, 1, &ks) || count_of(js_get(sp, "dec"), 18, 1, &decs))
                goto fail_ids;
            if (cols[pos].cells) goto fail_ids;
            int k = (int)ks, dec = (int)decs;
            size_t want = nrows >= (size_t)k ? nrows - (size_t)k : 0;
            if (bi >= nbins) goto fail_ids;
            int64_t *d = unpack_ints(cut[bi], cutlen[bi], want);
            bi++;
            if (!d) goto fail_ids;
            size_t an = want;
            if (k) {
                const Js *jwarm = js_get(sp, "warm");
                int64_t warm[8] = { 0 };
                for (int i = 0; i < k && jwarm && (size_t)i < jwarm->count; i++)
                    warm[i] = js_i64(&jwarm->items[i], 0);
                d = undiff(d, want, warm, k, &an);
                if (!d) goto fail_ids;
            }
            if (an != nrows && nrows) { free(d); goto fail_ids; }
            Str *cells = malloc((nrows ? nrows : 1) * sizeof(Str));
            if (!cells) { free(d); goto fail_ids; }
            Buf *store = &cols[pos].store;
            size_t *offs = malloc((nrows ? nrows : 1) * sizeof(size_t));
            size_t *lens = malloc((nrows ? nrows : 1) * sizeof(size_t));
            if (!offs || !lens) { free(offs); free(lens); free(cells); free(d); goto fail_ids; }
            for (size_t i = 0; i < nrows; i++) {
                offs[i] = store->len;
                fmt_fixed_one(store, d[i], dec);
                lens[i] = store->len - offs[i];
            }
            for (size_t i = 0; i < nrows; i++) {
                cells[i].p = (const char *)store->data + offs[i];
                cells[i].n = lens[i];
            }
            free(offs); free(lens); free(d);
            /* Cells this column could not represent -- blanks, "-0.0", a
             * stray decimal count. They were stored by position and as text,
             * and overwrite the filled-in values the encoder put there. */
            if (read_exceptions(sp, nrows, cut, cutlen, nbins, sgroup,
                                sgroup_n, ngroups_s, &bi, &ti,
                                cells) != 0) {
                free(cells);
                cols[pos].cells = NULL;
                goto fail_ids;
            }
            cols[pos].cells = cells;
            cols[pos].owned = 1;
        } else if (!strcmp(jk->str, "grp")) {
            /* A grouped column's exceptions are read here, in the same walk
             * the encoder wrote them, but cannot be applied until the group
             * has been rebuilt below. */
            size_t nex;
            if (count_of(js_get(sp, "nex"), nrows, 1, &nex)) goto fail_ids;
            if (nex) {
                if (bi >= nbins || ti >= ngroups_s || grp_ex_pos[pos]) goto fail_ids;
                if (sgroup_n[ti] != nex) goto fail_ids;
                int64_t *gaps = unpack_ints(cut[bi], cutlen[bi], nex);
                bi++;
                if (!gaps) goto fail_ids;
                uint64_t acc = 0;
                for (size_t i = 0; i < nex; i++) {
                    if (gaps[i] < 0 || (uint64_t)gaps[i] >= nrows) { free(gaps); goto fail_ids; }
                    acc += (uint64_t)gaps[i];
                    if (acc >= nrows) { free(gaps); goto fail_ids; }
                    gaps[i] = (int64_t)acc;
                }
                grp_ex_pos[pos] = gaps;
                grp_ex_val[pos] = sgroup[ti];
                grp_ex_n[pos] = nex;
                ti++;
            }
        }
    }

    /* 2D groups: rebuild the matrix by cumulative sums, then format */
    for (size_t gi = 0; gi < jgroups->count; gi++) {
        const Js *g = &jgroups->items[gi];
        if (g->kind != JS_ARR) goto fail_ids;
        size_t w = g->count;
        if (w == 0 || nrows == 0) continue;
        if (w > ncols) goto fail_ids;
        size_t n_side = w + (nrows - 1), inner, total, cells_n;
        if (mul_ok(nrows - 1, w - 1, &inner) || inner > SIZE_MAX - n_side) goto fail_ids;
        total = n_side + inner;
        if (mul_ok(nrows, w, &cells_n) || cells_n > SIZE_MAX / sizeof(int64_t)) goto fail_ids;
        if (bi >= nbins) goto fail_ids;
        int64_t *flat = unpack_ints(cut[bi], cutlen[bi], total);
        bi++;
        if (!flat) goto fail_ids;

        /* M[0] = row0; column 0 of the rest = col0; interior = D.
         * Rebuild D1 by cumsum along axis 1, then M by cumsum along axis 0 --
         * the exact inverse of the encoder's np.diff twice. */
        int64_t *M = malloc(cells_n * sizeof(int64_t));
        if (!M) { free(flat); goto fail_ids; }
        for (size_t c = 0; c < w; c++) M[c] = flat[c];
        /* unsigned sums: wrap is defined, and matches the encoder's */
        for (size_t i = 1; i < nrows; i++) {
            uint64_t run = (uint64_t)flat[w + (i - 1)];
            M[i * w + 0] = (int64_t)run;
            for (size_t c = 1; c < w; c++) {
                run += (uint64_t)flat[n_side + (i - 1) * (w - 1) + (c - 1)];
                M[i * w + c] = (int64_t)run;
            }
        }
        for (size_t c = 0; c < w; c++) {
            uint64_t run = (uint64_t)M[c];
            for (size_t i = 1; i < nrows; i++) {
                run += (uint64_t)M[i * w + c];
                M[i * w + c] = (int64_t)run;
            }
        }
        free(flat);

        for (size_t c = 0; c < w; c++) {
            size_t pos, decs;
            if (count_of(&g->items[c], ncols - 1, 0, &pos)) { free(M); goto fail_ids; }
            const Js *gk = js_get(&jcols->items[pos], "kind");
            /* only a "grp" column belongs in a group, and only once */
            if (!gk || gk->kind != JS_STR || strcmp(gk->str, "grp") || cols[pos].cells)
                { free(M); goto fail_ids; }
            /* same POW10 bound as the ungrouped numeric path */
            if (count_of(js_get(&jcols->items[pos], "dec"), 18, 1, &decs)) { free(M); goto fail_ids; }
            int dec = (int)decs;
            Str *cells = malloc(nrows * sizeof(Str));
            size_t *offs = malloc(nrows * sizeof(size_t));
            size_t *lens = malloc(nrows * sizeof(size_t));
            if (!cells || !offs || !lens) { free(cells); free(offs); free(lens); free(M); goto fail_ids; }
            Buf *store = &cols[pos].store;
            for (size_t i = 0; i < nrows; i++) {
                offs[i] = store->len;
                fmt_fixed_one(store, M[i * w + c], dec);
                lens[i] = store->len - offs[i];
            }
            for (size_t i = 0; i < nrows; i++) {
                cells[i].p = (const char *)store->data + offs[i];
                cells[i].n = lens[i];
            }
            free(offs); free(lens);
            /* the group is rebuilt now, so the stashed exceptions can land */
            for (size_t e = 0; e < grp_ex_n[pos]; e++)
                cells[(size_t)grp_ex_pos[pos][e]] = grp_ex_val[pos][e];
            cols[pos].cells = cells;
            cols[pos].owned = 1;
        }
        free(M);
    }

    /* ------------------------------------------------ assemble the table */
    /* Every column must have been rebuilt, and every payload used. A column
     * nothing filled in (a "dict" missing from the order, a "grp" in no group)
     * or a leftover bin is an archive this encoder did not write. */
    for (size_t j = 0; j < ncols; j++) if (!cols[j].cells) goto fail_ids;
    if (bi + nlen != nbins || ti != ngroups_s) goto fail_ids;
    table_init(out);
    out->ncols = ncols;
    out->nrows = nrows;
    out->names = calloc(ncols ? ncols : 1, sizeof(char *));
    if (!out->names) goto fail_ids;
    for (size_t j = 0; j < ncols && j < jcolumns->count; j++) {
        const Js *nm = &jcolumns->items[j];
        const char *s = (nm->kind == JS_STR && nm->str) ? nm->str : "";
        out->names[j] = strdup(s);
    }
    for (size_t j = jcolumns->count; j < ncols; j++) out->names[j] = strdup("");

    {
        size_t total = 0;
        for (size_t j = 0; j < ncols; j++)
            for (size_t i = 0; i < nrows; i++) total += cols[j].cells[i].n;
        buf_need(&out->arena, total + 1);
        size_t ncell;
        if (mul_ok(nrows, ncols, &ncell)) goto fail_ids;
        out->cells = calloc(ncell ? ncell : 1, sizeof(Str));
        if (!out->cells) goto fail_ids;
        for (size_t i = 0; i < nrows; i++) {
            for (size_t j = 0; j < ncols; j++) {
                Str s = cols[j].cells[i];
                size_t at = out->arena.len;
                buf_put(&out->arena, s.p, s.n);
                out->cells[i * ncols + j].p = (const char *)out->arena.data + at;
                out->cells[i * ncols + j].n = s.n;
            }
        }
    }

    if (derived && derive_restore(out, jderive)) {
        table_free(out);
        goto fail_ids;
    }

    for (size_t j = 0; j < ncols; j++) {
        free(cols[j].cells);
        buf_free(&cols[j].store);
        free(ids_by_pos[j]);
        free(grp_ex_pos[j]);
    }
    free(cols); free(ids_by_pos);
    free(grp_ex_pos); free(grp_ex_val); free(grp_ex_n);
    for (size_t g = 0; g < ngroups_s; g++) { free(sgroup[g]); free(fcown[g]); }
    free(sgroup); free(sgroup_n); free(fcown);
    free(cut); free(cutlen);
    js_free(meta);
    buf_free(&metab); buf_free(&rawb); buf_free(&txtb);
    return 0;

fail_ids:
    for (size_t j = 0; j < ncols; j++) {
        free(cols[j].cells);
        buf_free(&cols[j].store);
        if (ids_by_pos) free(ids_by_pos[j]);
        if (grp_ex_pos) free(grp_ex_pos[j]);
    }
    free(ids_by_pos);
    free(grp_ex_pos); free(grp_ex_val); free(grp_ex_n);
fail_cols:
    free(cols);
fail_sgroup:
    for (size_t g = 0; g < ngroups_s; g++) {
        if (sgroup) free(sgroup[g]);
        if (fcown) free(fcown[g]);
    }
    free(sgroup); free(sgroup_n); free(fcown);
fail_cuts:
    free(cut); free(cutlen);
fail_meta:
    js_free(meta);
fail:
    buf_free(&metab); buf_free(&rawb); buf_free(&txtb);
    return -1;
}
