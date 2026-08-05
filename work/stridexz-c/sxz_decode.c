/* The decoder. It reads files other people made, so it treats every number
 * that comes off the wire as a lie until checked: lengths against the payload
 * that actually arrived, codes against the alphabet that actually exists, the
 * permutation against being a permutation. Corrupt input is refused; it never
 * crashes and never allocates on an attacker's say-so.
 *
 * Cells go straight into the table's Arena, which never moves what it has
 * handed out. An earlier version accumulated into a growable buffer and so had
 * to record every cell as an offset and fix the pointers up afterwards -- an
 * offset table of one entry per cell, 320 MB on a 20-million-cell table, whose
 * only purpose was surviving a realloc the arena simply never does.
 */
#include "sxz.h"

#include <stdlib.h>
#include <string.h>

static int fail(char **err, const char *msg)
{
    if (err && !*err)
        *err = strdup(msg);
    return -1;
}

static int64_t js_num(const Json *j, int64_t dflt)
{
    return (j && j->kind == JS_NUM) ? j->num : dflt;
}

/* ------------------------------------------------------------- unpackers */

static int unpack_esc(const char *b, size_t n, size_t nrows, Arena *arena,
                      StrVec *dst)
{
    size_t i = 0;
    Buf scratch;
    buf_init(&scratch);
    for (size_t r = 0; r < nrows; r++) {
        scratch.len = 0;
        while (i < n && b[i] != '\n') {
            char c = b[i];
            if (c == '\\' && i + 1 < n) {
                char nx = b[i + 1];
                if (buf_putc(&scratch, nx == 'n' ? '\n' : nx) != 0) goto bad;
                i += 2;
                continue;
            }
            if (buf_putc(&scratch, c) != 0) goto bad;
            i++;
        }
        Str s;
        if (arena_put(arena, scratch.p, scratch.len, &s) != 0) goto bad;
        if (strvec_push(dst, s) != 0) goto bad;
        if (i < n && b[i] == '\n')
            i++;
        else if (r + 1 < nrows)
            goto bad;                  /* fewer rows in the blob than claimed */
    }
    buf_free(&scratch);
    return 0;
bad:
    buf_free(&scratch);
    return -1;
}

static int unpack_fixed(const char *b, size_t n, size_t nrows, int w,
                        Arena *arena, StrVec *dst)
{
    if (w <= 0 || (size_t)w > 64)
        return -1;
    if (nrows && (size_t)w > n / nrows)
        return -1;
    if ((size_t)w * nrows != n)
        return -1;
    for (size_t r = 0; r < nrows; r++) {
        const char *rec = b + (size_t)w * r;
        size_t len = (size_t)w;
        while (len && rec[len - 1] == '\0')
            len--;
        Str s;
        if (arena_put(arena, rec, len, &s) != 0)
            return -1;
        if (strvec_push(dst, s) != 0)
            return -1;
    }
    return 0;
}

/* Undo the byte-plane split back into int64s. */
static int planes_to_i64(const char *b, size_t n, size_t nrows, int w,
                         int64_t **out)
{
    if (w != 1 && w != 2 && w != 4 && w != 8)
        return -1;
    if (nrows && (size_t)w > n / nrows)
        return -1;
    if ((size_t)w * nrows != n)
        return -1;
    int64_t *v = calloc(nrows ? nrows : 1, sizeof *v);
    if (!v)
        return -1;
    const unsigned char *u = (const unsigned char *)b;
    for (int p = 0; p < w; p++)
        for (size_t i = 0; i < nrows; i++)
            v[i] |= (int64_t)((uint64_t)u[(size_t)p * nrows + i] << (8 * p));
    if (w < 8) {
        uint64_t sign = 1ULL << (w * 8 - 1);
        uint64_t mask = ~0ULL << (w * 8);
        for (size_t i = 0; i < nrows; i++)
            if ((uint64_t)v[i] & sign)
                v[i] = (int64_t)((uint64_t)v[i] | mask);
    }
    *out = v;
    return 0;
}

static int unpack_intp(const char *b, size_t n, size_t nrows, int w,
                       Arena *arena, StrVec *dst)
{
    int64_t *v;
    if (planes_to_i64(b, n, nrows, w, &v) != 0)
        return -1;
    char tmp[24];
    for (size_t r = 0; r < nrows; r++) {
        int len = snprintf(tmp, sizeof tmp, "%lld", (long long)v[r]);
        Str s;
        if (len < 0 || arena_put(arena, tmp, (size_t)len, &s) != 0 ||
            strvec_push(dst, s) != 0) {
            free(v);
            return -1;
        }
    }
    free(v);
    return 0;
}

static int unpack_dict(const char *b, size_t n, size_t nrows, int w,
                       int64_t nsym, Arena *arena, StrVec *dst)
{
    if (n < 4 || nsym < 1 || (uint64_t)nsym > SXZ_MAX_ROWS)
        return -1;
    size_t alen = (unsigned char)b[0] | ((size_t)(unsigned char)b[1] << 8) |
                  ((size_t)(unsigned char)b[2] << 16) |
                  ((size_t)(unsigned char)b[3] << 24);
    if (alen > n - 4)
        return -1;

    /* The alphabet is escaped text, exactly like an "esc" column. It goes into
     * the table's own arena and every row REFERENCES it, so a value repeated
     * ten thousand times is stored once rather than ten thousand times. */
    StrVec alphabet;
    strvec_init(&alphabet);
    if (unpack_esc(b + 4, alen, (size_t)nsym, arena, &alphabet) != 0 ||
        alphabet.n != (size_t)nsym) {
        strvec_free(&alphabet);
        return -1;
    }

    int64_t *codes;
    if (planes_to_i64(b + 4 + alen, n - 4 - alen, nrows, w, &codes) != 0) {
        strvec_free(&alphabet);
        return -1;
    }
    for (size_t r = 0; r < nrows; r++) {
        if (codes[r] < 0 || codes[r] >= nsym) {
            free(codes);
            strvec_free(&alphabet);
            return -1;                       /* code outside the alphabet */
        }
        if (strvec_push(dst, alphabet.v[codes[r]]) != 0) {
            free(codes);
            strvec_free(&alphabet);
            return -1;
        }
    }
    free(codes);
    strvec_free(&alphabet);
    return 0;
}

int sxz_unpack_unit(const char *kind, int w, int64_t nsym, const char *blob,
                    size_t n, size_t nrows, Arena *arena, StrVec *dst)
{
    if (!strcmp(kind, "esc"))   return unpack_esc(blob, n, nrows, arena, dst);
    if (!strcmp(kind, "fixed")) return unpack_fixed(blob, n, nrows, w, arena, dst);
    if (!strcmp(kind, "intp"))  return unpack_intp(blob, n, nrows, w, arena, dst);
    if (!strcmp(kind, "dict"))
        return unpack_dict(blob, n, nrows, w, nsym, arena, dst);
    return -1;
}

/* ------------------------------------------------------------------ main */

int sxz_decode(const char *blob, size_t n, Table *out, char **err)
{
    JsonDoc doc;
    doc.root = NULL;
    doc.pool = NULL;
    Buf payload;
    buf_init(&payload);
    int64_t *permvals = NULL;
    size_t ncols = 0, nrows = 0;
    int has_perm = 0;

    table_init(out);
    if (n < SXZ_MAGIC_LEN + 1 || memcmp(blob, SXZ_MAGIC, SXZ_MAGIC_LEN))
        return fail(err, "not a StrideXZ archive");

    if (xz_decompress(blob + SXZ_MAGIC_LEN, n - SXZ_MAGIC_LEN,
                      SXZ_MAX_PAYLOAD, &payload) != 0) {
        buf_free(&payload);
        return fail(err, "archive body is not a valid xz stream");
    }
    if (payload.len < 4) {
        buf_free(&payload);
        return fail(err, "archive is truncated");
    }
    size_t hlen = (unsigned char)payload.p[0] |
                  ((size_t)(unsigned char)payload.p[1] << 8) |
                  ((size_t)(unsigned char)payload.p[2] << 16) |
                  ((size_t)(unsigned char)payload.p[3] << 24);
    if (hlen > payload.len - 4) {
        buf_free(&payload);
        return fail(err, "header length runs past the end of the archive");
    }
    if (json_parse(payload.p + 4, hlen, &doc, err) != 0) {
        buf_free(&payload);
        return -1;
    }

    {
        const Json *root = doc.root;
        const Json *jcols = json_get(root, "columns");
        const Json *jrows = json_get(root, "nrows");
        const Json *jblocks = json_get(root, "blocks");
        const Json *jperm = json_get(root, "perm");
        if (!jcols || jcols->kind != JS_ARR || !jrows || jrows->kind != JS_NUM ||
            !jblocks || jblocks->kind != JS_ARR)
            goto bad_header;

        ncols = jcols->count;
        int64_t nrows64 = jrows->num;
        if (ncols == 0 || ncols > SXZ_MAX_COLS || nrows64 < 0 ||
            (uint64_t)nrows64 > SXZ_MAX_ROWS)
            goto bad_header;
        nrows = (size_t)nrows64;
        has_perm = jperm && jperm->kind == JS_BOOL && jperm->boolean;

        out->ncols = ncols;
        out->nrows = nrows;
        out->cols = calloc(ncols, sizeof *out->cols);
        if (!out->cols)
            goto oom;
        for (size_t c = 0; c < ncols; c++) {
            strvec_init(&out->cols[c]);
            /* The row count is known exactly, so size the vector once rather
             * than letting it double its way there and keep the slack. */
            if (nrows) {
                out->cols[c].v = malloc(nrows * sizeof *out->cols[c].v);
                if (!out->cols[c].v)
                    goto oom;
                out->cols[c].cap = nrows;
            }
            if (jcols->items[c]->kind != JS_STR)
                goto bad_header;
            /* Names point into the JSON pool, which is freed below. Copy. */
            Str nm;
            if (arena_put(&out->arena, jcols->items[c]->str.s,
                          jcols->items[c]->str.n, &nm) != 0)
                goto oom;
            if (strvec_push(&out->names, nm) != 0)
                goto oom;
        }
        if (has_perm) {
            permvals = calloc(nrows ? nrows : 1, sizeof *permvals);
            if (!permvals)
                goto oom;
        }

        size_t pos = 4 + hlen;
        size_t seen_rows = 0, permcount = 0;
        for (size_t bi = 0; bi < jblocks->count; bi++) {
            const Json *blk = jblocks->items[bi];
            const Json *junits = json_get(blk, "units");
            const Json *jlens = json_get(blk, "lens");
            int64_t bn = js_num(json_get(blk, "n"), -1);
            if (!junits || junits->kind != JS_ARR || !jlens ||
                jlens->kind != JS_ARR || bn < 0 ||
                junits->count != jlens->count)
                goto bad_header;
            if ((uint64_t)bn > SXZ_MAX_ROWS || seen_rows + (size_t)bn > nrows)
                goto bad_header;
            size_t bnrows = (size_t)bn;

            for (size_t ui = 0; ui < junits->count; ui++) {
                const Json *u = junits->items[ui];
                if (u->kind != JS_ARR || u->count != 3 ||
                    u->items[0]->kind != JS_STR || u->items[2]->kind != JS_ARR)
                    goto bad_header;
                int64_t blen = js_num(jlens->items[ui], -1);
                if (blen < 0 || (uint64_t)blen > payload.len ||
                    pos + (size_t)blen > payload.len)
                    goto bad_header;
                const char *bp = payload.p + pos;
                size_t bl = (size_t)blen;
                pos += bl;

                Str kind = u->items[0]->str;
                const Json *params = u->items[1];
                int w = (int)js_num(json_get(params, "w"), 0);
                int64_t nsym = js_num(json_get(params, "n"), 0);
                const Json *idx = u->items[2];

                if (kind.n == 4 && !memcmp(kind.s, "perm", 4)) {
                    if (!has_perm || idx->count != 0)
                        goto bad_header;
                    int64_t *v;
                    if (planes_to_i64(bp, bl, bnrows, w, &v) != 0)
                        goto bad_stream;
                    if (permcount + bnrows > nrows) { free(v); goto bad_header; }
                    memcpy(permvals + permcount, v, bnrows * sizeof *v);
                    permcount += bnrows;
                    free(v);
                    continue;
                }

                if (idx->count != 1 || idx->items[0]->kind != JS_NUM)
                    goto bad_header;
                int64_t ci = idx->items[0]->num;
                if (ci < 0 || (size_t)ci >= ncols)
                    goto bad_header;
                size_t c = (size_t)ci;
                if (out->cols[c].n + bnrows > nrows)
                    goto bad_header;

                size_t before = out->cols[c].n;
                int rc;
                if (kind.n == 3 && !memcmp(kind.s, "esc", 3))
                    rc = unpack_esc(bp, bl, bnrows, &out->arena, &out->cols[c]);
                else if (kind.n == 5 && !memcmp(kind.s, "fixed", 5))
                    rc = unpack_fixed(bp, bl, bnrows, w, &out->arena,
                                      &out->cols[c]);
                else if (kind.n == 4 && !memcmp(kind.s, "intp", 4))
                    rc = unpack_intp(bp, bl, bnrows, w, &out->arena,
                                     &out->cols[c]);
                else if (kind.n == 4 && !memcmp(kind.s, "dict", 4))
                    rc = unpack_dict(bp, bl, bnrows, w, nsym, &out->arena,
                                     &out->cols[c]);
                else
                    goto bad_header;
                if (rc != 0 || out->cols[c].n - before != bnrows)
                    goto bad_stream;
            }
            seen_rows += bnrows;
        }
        if (seen_rows != nrows)
            goto bad_header;
        for (size_t c = 0; c < ncols; c++)
            if (out->cols[c].n != nrows)
                goto bad_header;
        if (has_perm && permcount != nrows)
            goto bad_header;
    }

    if (has_perm) {
        char *taken = calloc(nrows ? nrows : 1, 1);
        Str *slot = malloc((nrows ? nrows : 1) * sizeof *slot);
        if (!taken || !slot) {
            free(taken);
            free(slot);
            goto oom;
        }
        /* Validated up front, before anything is written: every destination is
         * in range and used exactly once. That is what makes the stored
         * permutation safe to index with rather than a way to walk off the
         * end of a column. */
        for (size_t r = 0; r < nrows; r++) {
            int64_t o = permvals[r];
            if (o < 0 || (size_t)o >= nrows || taken[o]) {
                free(taken);
                free(slot);
                goto bad_header;
            }
            taken[o] = 1;
        }
        for (size_t c = 0; c < ncols; c++) {
            for (size_t r = 0; r < nrows; r++)
                slot[permvals[r]] = out->cols[c].v[r];
            memcpy(out->cols[c].v, slot, nrows * sizeof *slot);
        }
        free(taken);
        free(slot);
    }

    free(permvals);
    json_free(&doc);
    buf_free(&payload);
    return 0;

oom:
    fail(err, "out of memory decoding archive");
    goto cleanup;
bad_header:
    fail(err, "archive header is inconsistent with its contents");
    goto cleanup;
bad_stream:
    fail(err, "archive column data is corrupt");
cleanup:
    free(permvals);
    json_free(&doc);
    buf_free(&payload);
    table_free(out);
    return -1;
}
