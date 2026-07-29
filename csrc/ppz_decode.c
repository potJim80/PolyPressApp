/* Decoding a Polypress archive, in C.
 *
 * This mirrors fast.py's decode() step for step. Where a line here looks
 * gratuitously specific -- the stable argsort, the warm-start handling in
 * undiff, the two different string layouts -- it is matching a decision made
 * on the Python side, and the comment says which.
 *
 * Decode came first in the port because it is what a researcher handed a
 * .ppz actually needs, and because it can be verified immediately: every
 * archive the Python encoder produces is a test case with a known answer.
 */

#include "ppz.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ESCAPE 255

/* --------------------------------------------------------------- varints */

/* Mirror of fast.unpack_ints. `buf` is width byte, then n head bytes, then
 * the escaped tail at 4 or 8 bytes each. */
static int64_t *unpack_ints(const uint8_t *buf, size_t buflen, size_t n)
{
    if (n == 0) return calloc(1, sizeof(int64_t));
    if (buflen < 1 + n) return NULL;
    int width = buf[0];
    const uint8_t *head = buf + 1;

    size_t nbig = 0;
    for (size_t i = 0; i < n; i++) if (head[i] == ESCAPE) nbig++;

    const uint8_t *tail = buf + 1 + n;
    size_t need = (width == 8 ? 8 : 4) * nbig;
    if (buflen < 1 + n + need) return NULL;

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
            /* j-th finite difference of warm, first element */
            int64_t tmp[8];
            for (int i = 0; i <= k && i < 8; i++) tmp[i] = warm[i];
            int len = k;
            for (int r = 0; r < j; r++) {
                for (int i = 0; i < len - 1; i++) tmp[i] = tmp[i + 1] - tmp[i];
                len--;
            }
            first = tmp[0];
        }
        int64_t *b = malloc((n + 1) * sizeof(int64_t));
        if (!b) { free(a); return NULL; }
        b[0] = first;
        memcpy(b + 1, a, n * sizeof(int64_t));
        n++;
        int64_t run = 0;
        for (size_t i = 0; i < n; i++) { run += b[i]; b[i] = run; }
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

    /* The fallback stores canonical CSV, so reuse the CSV reader via a temp
     * file-free path: write to a memory buffer and parse it. */
    char tmpl[] = "/tmp/ppzfbXXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) { buf_free(&plain); return -1; }
    FILE *f = fdopen(fd, "wb");
    fwrite(plain.data, 1, plain.len, f);
    fclose(f);
    buf_free(&plain);
    int rc = table_read_csv(out, tmpl);
    remove(tmpl);
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
    size_t nex = (size_t)js_int(js_get(sp, "nex"), 0);
    if (!nex) return 0;
    if (nex > nrows || *bi >= nbins || *ti >= ngroups_s) return -1;

    int64_t *gaps = unpack_ints(cut[*bi], cutlen[*bi], nex);
    (*bi)++;
    if (!gaps) return -1;
    Str *vals = sgroup[*ti];
    size_t nvals = sgroup_n[*ti];
    (*ti)++;

    int64_t acc = 0;
    for (size_t i = 0; i < nex; i++) {
        acc += gaps[i];
        if (acc < 0 || (size_t)acc >= nrows || i >= nvals || !vals) {
            free(gaps);
            return -1;
        }
        cells[acc] = vals[i];
    }
    free(gaps);
    return 0;
}

int ppz_decode(const uint8_t *blob, size_t n, Table *out)
{
    if (n < 4) return -1;
    if (!memcmp(blob, PPZ_MAGIC_RAW_XZ, 4)) return decode_fallback(blob, n, out, 0);
    if (!memcmp(blob, PPZ_MAGIC_RAW_BZ, 4)) return decode_fallback(blob, n, out, 1);
    if (memcmp(blob, PPZ_MAGIC, 4) && memcmp(blob, PPZ_MAGIC_V0, 4)) return -1;
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

    size_t nrows = (size_t)js_int(js_get(meta, "nrows"), 0);
    size_t ncols = jcols->count;
    size_t nlen  = (size_t)js_int(js_get(meta, "nlenbins"), 0);

    /* slice the concatenated binary payload */
    size_t nbins = jbins->count;
    const uint8_t **cut = calloc(nbins ? nbins : 1, sizeof(uint8_t *));
    size_t *cutlen = calloc(nbins ? nbins : 1, sizeof(size_t));
    if (!cut || !cutlen) goto fail_meta;
    {
        size_t at = 0;
        for (size_t i = 0; i < nbins; i++) {
            size_t sz = (size_t)js_int(&jbins->items[i], 0);
            if (at + sz > rawb.len) goto fail_cuts;
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
    if (!sgroup || !sgroup_n) goto fail_cuts;
    {
        size_t at = 0, li = 0;
        size_t first_len_bin = nbins - nlen;
        for (size_t g = 0; g < ngroups_s; g++) {
            const Js *m = &jsmeta->items[g];
            size_t cnt = (size_t)js_int(js_get(m, "n"), 0);
            size_t nb  = (size_t)js_int(js_get(m, "b"), 0);
            int    nl  = (int)js_int(js_get(m, "nl"), 1);
            const char *chunk = (const char *)txtb.data + at;
            if (at + nb > txtb.len) goto fail_sgroup;
            at += nb;
            sgroup_n[g] = cnt;
            if (cnt == 0) { sgroup[g] = NULL; continue; }
            Str *arr = malloc(cnt * sizeof(Str));
            if (!arr) goto fail_sgroup;
            if (nl) {
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
                size_t pos = 0;
                for (size_t k = 0; k < cnt; k++) {
                    arr[k].p = chunk + pos;
                    arr[k].n = (size_t)lens[k];
                    pos += (size_t)lens[k];
                }
                free(lens);
            }
            sgroup[g] = arr;
        }
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
        size_t pos = (size_t)js_int(&jorder->items[oi], 0);
        if (pos >= ncols) goto fail_ids;
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
        for (size_t i = 0; i < nrows; i++) {
            if (alpha_n == 0) { cells[i].p = ""; cells[i].n = 0; continue; }
            size_t k = (size_t)ids[i];
            if (k >= alpha_n) k = 0;
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
            Str *cells = malloc((nrows ? nrows : 1) * sizeof(Str));
            if (!cells) goto fail_ids;
            for (size_t i = 0; i < nrows; i++) {
                if (i < cnt) cells[i] = src[i];
                else { cells[i].p = ""; cells[i].n = 0; }
            }
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
            int k = (int)js_int(js_get(sp, "k"), 0);
            int dec = (int)js_int(js_get(sp, "dec"), 0);
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
            Str *cells = malloc((nrows ? nrows : 1) * sizeof(Str));
            if (!cells) { free(d); goto fail_ids; }
            Buf *store = &cols[pos].store;
            size_t *offs = malloc((nrows ? nrows : 1) * sizeof(size_t));
            size_t *lens = malloc((nrows ? nrows : 1) * sizeof(size_t));
            if (!offs || !lens) { free(offs); free(lens); free(cells); free(d); goto fail_ids; }
            for (size_t i = 0; i < nrows; i++) {
                offs[i] = store->len;
                fmt_fixed_one(store, i < an ? d[i] : 0, dec);
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
            size_t nex = (size_t)js_int(js_get(sp, "nex"), 0);
            if (nex) {
                if (nex > nrows || bi >= nbins || ti >= ngroups_s)
                    goto fail_ids;
                int64_t *gaps = unpack_ints(cut[bi], cutlen[bi], nex);
                bi++;
                if (!gaps) goto fail_ids;
                int64_t acc = 0;
                for (size_t i = 0; i < nex; i++) {
                    acc += gaps[i];
                    if (acc < 0 || (size_t)acc >= nrows) { free(gaps); goto fail_ids; }
                    gaps[i] = acc;
                }
                grp_ex_pos[pos] = gaps;
                grp_ex_val[pos] = sgroup[ti];
                grp_ex_n[pos] = nex < sgroup_n[ti] ? nex : sgroup_n[ti];
                ti++;
            }
        }
    }

    /* 2D groups: rebuild the matrix by cumulative sums, then format */
    for (size_t gi = 0; gi < jgroups->count; gi++) {
        const Js *g = &jgroups->items[gi];
        size_t w = g->count;
        if (w == 0 || nrows == 0) continue;
        size_t n_side = w + (nrows - 1);
        size_t total = n_side + (nrows - 1) * (w - 1);
        if (bi >= nbins) goto fail_ids;
        int64_t *flat = unpack_ints(cut[bi], cutlen[bi], total);
        bi++;
        if (!flat) goto fail_ids;

        /* M[0] = row0; column 0 of the rest = col0; interior = D.
         * Rebuild D1 by cumsum along axis 1, then M by cumsum along axis 0 --
         * the exact inverse of the encoder's np.diff twice. */
        int64_t *M = malloc(nrows * w * sizeof(int64_t));
        if (!M) { free(flat); goto fail_ids; }
        for (size_t c = 0; c < w; c++) M[c] = flat[c];
        for (size_t i = 1; i < nrows; i++) {
            int64_t run = flat[w + (i - 1)];
            M[i * w + 0] = run;
            for (size_t c = 1; c < w; c++) {
                run += flat[n_side + (i - 1) * (w - 1) + (c - 1)];
                M[i * w + c] = run;
            }
        }
        for (size_t c = 0; c < w; c++) {
            int64_t run = M[c];
            for (size_t i = 1; i < nrows; i++) {
                run += M[i * w + c];
                M[i * w + c] = run;
            }
        }
        free(flat);

        for (size_t c = 0; c < w; c++) {
            size_t pos = (size_t)js_int(&g->items[c], 0);
            if (pos >= ncols) continue;
            int dec = (int)js_int(js_get(&jcols->items[pos], "dec"), 0);
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
            for (size_t e = 0; e < grp_ex_n[pos]; e++) {
                size_t p = (size_t)grp_ex_pos[pos][e];
                if (p < nrows) cells[p] = grp_ex_val[pos][e];
            }
            free(cols[pos].cells);
            cols[pos].cells = cells;
            cols[pos].owned = 1;
        }
        free(M);
    }

    /* ------------------------------------------------ assemble the table */
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
            if (cols[j].cells)
                for (size_t i = 0; i < nrows; i++) total += cols[j].cells[i].n;
        buf_need(&out->arena, total + 1);
        out->cells = calloc(nrows * ncols ? nrows * ncols : 1, sizeof(Str));
        if (!out->cells) goto fail_ids;
        for (size_t i = 0; i < nrows; i++) {
            for (size_t j = 0; j < ncols; j++) {
                Str s = cols[j].cells ? cols[j].cells[i] : (Str){ "", 0 };
                size_t at = out->arena.len;
                buf_put(&out->arena, s.p, s.n);
                out->cells[i * ncols + j].p = (const char *)out->arena.data + at;
                out->cells[i * ncols + j].n = s.n;
            }
        }
    }

    for (size_t j = 0; j < ncols; j++) {
        free(cols[j].cells);
        buf_free(&cols[j].store);
        free(ids_by_pos[j]);
        free(grp_ex_pos[j]);
    }
    free(cols); free(ids_by_pos);
    free(grp_ex_pos); free(grp_ex_val); free(grp_ex_n);
    for (size_t g = 0; g < ngroups_s; g++) free(sgroup[g]);
    free(sgroup); free(sgroup_n);
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
    for (size_t g = 0; g < ngroups_s; g++) free(sgroup[g]);
    free(sgroup); free(sgroup_n);
fail_cuts:
    free(cut); free(cutlen);
fail_meta:
    js_free(meta);
fail:
    buf_free(&metab); buf_free(&rawb); buf_free(&txtb);
    return -1;
}
