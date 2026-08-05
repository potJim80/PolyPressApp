/* The encoder: choose a representation per column, choose an order, emit.
 *
 * Every choice here is MEASURED, not assumed. A transform that cannot be shown
 * smaller than the plain alternative does not get used, because the one thing
 * this codec must never do is make a file bigger than the obvious encoding of
 * it would have been. `xz_probe` (preset 1) ranks candidates cheaply; the real
 * entropy stage runs once at the end over whatever won.
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

const SxzOptions SXZ_DEFAULTS = {
    1, 1, 1, 1, 0, 1, { 4, -1, 0 }, 0
};

/* ------------------------------------------------------------- hash table */

/* Open addressing, power-of-two capacity. Used for cardinality counts and for
 * building dictionary alphabets. It never decides output ORDER -- alphabets
 * are emitted in first-appearance order and columns in index order -- because
 * iterating a hash table where the order reaches the output would make the
 * archive depend on the hash function. */

typedef struct {
    Str    key;
    int64_t val;
    int     used;
} HEnt;

typedef struct {
    HEnt  *e;
    size_t cap, n;
} HMap;

static int hmap_init(HMap *m, size_t hint)
{
    size_t cap = 16;
    while (cap < hint * 2)
        cap <<= 1;
    m->e = calloc(cap, sizeof *m->e);
    m->cap = cap;
    m->n = 0;
    return m->e ? 0 : -1;
}

static void hmap_free(HMap *m) { free(m->e); m->e = NULL; m->cap = m->n = 0; }

static HEnt *hmap_slot(HMap *m, Str key)
{
    size_t mask = m->cap - 1;
    size_t i = (size_t)str_hash(key) & mask;
    while (m->e[i].used && !str_eq(m->e[i].key, key))
        i = (i + 1) & mask;
    return &m->e[i];
}

static int hmap_grow(HMap *m)
{
    HEnt *old = m->e;
    size_t oldcap = m->cap;
    m->cap <<= 1;
    m->e = calloc(m->cap, sizeof *m->e);
    if (!m->e) {
        m->e = old;
        m->cap = oldcap;
        return -1;
    }
    m->n = 0;
    for (size_t i = 0; i < oldcap; i++) {
        if (!old[i].used)
            continue;
        HEnt *s = hmap_slot(m, old[i].key);
        *s = old[i];
        m->n++;
    }
    free(old);
    return 0;
}

/* Insert if absent. Returns the entry; *fresh tells you which happened. */
static HEnt *hmap_put(HMap *m, Str key, int *fresh)
{
    if ((m->n + 1) * 10 >= m->cap * 7 && hmap_grow(m) != 0)
        return NULL;
    HEnt *s = hmap_slot(m, key);
    *fresh = !s->used;
    if (!s->used) {
        s->used = 1;
        s->key = key;
        s->val = 0;
        m->n++;
    }
    return s;
}

static int hmap_has(const HMap *m, Str key)
{
    size_t mask = m->cap - 1;
    size_t i = (size_t)str_hash(key) & mask;
    while (m->e[i].used) {
        if (str_eq(m->e[i].key, key))
            return 1;
        i = (i + 1) & mask;
    }
    return 0;
}

/* ------------------------------------------------------------ cell access */

/* Rows may be viewed through a permutation without copying the table. */
static Str cell(const StrVec *c, const size_t *perm, size_t r)
{
    return c->v[perm ? perm[r] : r];
}

/* --------------------------------------------------------------- packing */

static int pack_esc(const StrVec *c, const size_t *perm, size_t nrows, Buf *out)
{
    for (size_t r = 0; r < nrows; r++) {
        if (r && buf_putc(out, '\n') != 0)
            return -1;
        Str s = cell(c, perm, r);
        for (size_t i = 0; i < s.n; i++) {
            char ch = s.s[i];
            if (ch == '\\') {
                if (buf_puts(out, "\\\\") != 0) return -1;
            } else if (ch == '\n') {
                if (buf_puts(out, "\\n") != 0) return -1;
            } else if (buf_putc(out, ch) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

/* Constant-width fields: pay padding bytes now so every repeat afterwards
 * sits at the same distance and lands on one of xz's cheap bookmarks. */
static int fixed_plan(const StrVec *c, const size_t *perm, size_t nrows)
{
    if (!nrows)
        return -1;
    size_t maxw = 0, total = 0;
    for (size_t r = 0; r < nrows; r++) {
        Str s = cell(c, perm, r);
        if (s.n > maxw)
            maxw = s.n;
        total += s.n;
        if (memchr(s.s, '\0', s.n))
            return -1;                 /* the pad byte must be unused */
    }
    if (maxw == 0 || maxw > 64)
        return -1;
    if (maxw * nrows > (total + nrows) * 2)
        return -1;                     /* padding would more than double it */
    return (int)maxw;
}

static int pack_fixed(const StrVec *c, const size_t *perm, size_t nrows,
                      int w, Buf *out)
{
    if (buf_reserve(out, (size_t)w * nrows) != 0)
        return -1;
    for (size_t r = 0; r < nrows; r++) {
        Str s = cell(c, perm, r);
        memcpy(out->p + out->len, s.s, s.n);
        memset(out->p + out->len + s.n, 0, (size_t)w - s.n);
        out->len += (size_t)w;
    }
    return 0;
}

/* Byte planes: all the high bytes, then all the low bytes. The high plane of
 * a real column is nearly all one value -- a long run at a perfectly regular
 * stride, which is the cheapest thing in the format. */
static int pack_planes(const int64_t *v, size_t n, int w, Buf *out)
{
    if (buf_reserve(out, (size_t)w * n) != 0)
        return -1;
    unsigned char *dst = (unsigned char *)out->p + out->len;
    for (int p = 0; p < w; p++)
        for (size_t i = 0; i < n; i++)
            dst[(size_t)p * n + i] =
                (unsigned char)((uint64_t)v[i] >> (8 * p));
    out->len += (size_t)w * n;
    return 0;
}

/* Strict on purpose: the cell text must be exactly what printing the parsed
 * integer gives back, so "007", "+3" and "-0" are refused rather than
 * silently normalised into something that will not round-trip. */
static int parse_i64(Str s, int64_t *out)
{
    size_t i = 0;
    int neg = 0;
    if (s.n == 0)
        return -1;
    if (s.s[0] == '-') {
        neg = 1;
        i = 1;
        if (s.n == 1)
            return -1;
    }
    if (s.n - i > 19)
        return -1;
    if (s.s[i] == '0' && s.n - i > 1)
        return -1;                          /* leading zero */
    if (neg && s.n - i == 1 && s.s[i] == '0')
        return -1;                          /* "-0" prints as "0" */
    uint64_t v = 0;
    for (; i < s.n; i++) {
        char c = s.s[i];
        if (c < '0' || c > '9')
            return -1;
        /* The check must come BEFORE the multiply: testing the result after
         * v = v*10 + d never fires, because the overflow has already wrapped. */
        if (v > (UINT64_MAX - 9) / 10)
            return -1;
        v = v * 10 + (uint64_t)(c - '0');
    }
    if (neg) {
        if (v > 9223372036854775808ULL)
            return -1;
        *out = (v == 9223372036854775808ULL) ? INT64_MIN : -(int64_t)v;
    } else {
        if (v > (uint64_t)INT64_MAX)
            return -1;
        *out = (int64_t)v;
    }
    return 0;
}

static int width_for(int64_t lo, int64_t hi)
{
    if (lo >= -128 && hi <= 127) return 1;
    if (lo >= -32768 && hi <= 32767) return 2;
    if (lo >= -2147483648LL && hi <= 2147483647LL) return 4;
    return 8;
}

/* --------------------------------------------------------------- planning */

typedef struct {
    const char *kind;      /* "esc" "fixed" "intp" "dict" "perm" */
    int         w;
    int64_t     nsym;
    size_t      col;
    size_t      len;      /* blob length, kept after the blob is freed */
    Buf         blob;
} Unit;

static int plan_dict(const StrVec *c, const size_t *perm, size_t nrows, Buf *out,
                     int64_t *nsym_out, int *w_out)
{
    if (nrows < 64)
        return -1;
    HMap seen;
    if (hmap_init(&seen, 1024) != 0)
        return -1;
    StrVec alphabet;
    strvec_init(&alphabet);
    int64_t *codes = malloc(nrows * sizeof *codes);
    if (!codes) {
        hmap_free(&seen);
        return -1;
    }
    for (size_t r = 0; r < nrows; r++) {
        Str s = cell(c, perm, r);
        int fresh;
        HEnt *e = hmap_put(&seen, s, &fresh);
        if (!e)
            goto fail;
        if (fresh) {
            e->val = (int64_t)alphabet.n;
            if (strvec_push(&alphabet, s) != 0)
                goto fail;
            /* Too many distinct values and the alphabet costs more than the
             * codes save. Bail early rather than build the whole thing. */
            if (alphabet.n * 2 > nrows)
                goto fail;
        }
        codes[r] = e->val;
    }
    if (alphabet.n < 2)
        goto fail;

    int w = alphabet.n <= 128 ? 1 : (alphabet.n <= 32768 ? 2 : 4);

    Buf abuf;
    buf_init(&abuf);
    for (size_t i = 0; i < alphabet.n; i++) {
        if (i && buf_putc(&abuf, '\n') != 0) { buf_free(&abuf); goto fail; }
        Str s = alphabet.v[i];
        for (size_t k = 0; k < s.n; k++) {
            char ch = s.s[k];
            int rc = (ch == '\\') ? buf_puts(&abuf, "\\\\")
                   : (ch == '\n') ? buf_puts(&abuf, "\\n")
                   : buf_putc(&abuf, ch);
            if (rc != 0) { buf_free(&abuf); goto fail; }
        }
    }
    unsigned char hdr[4];
    hdr[0] = (unsigned char)(abuf.len);
    hdr[1] = (unsigned char)(abuf.len >> 8);
    hdr[2] = (unsigned char)(abuf.len >> 16);
    hdr[3] = (unsigned char)(abuf.len >> 24);
    if (buf_put(out, hdr, 4) != 0 || buf_put(out, abuf.p, abuf.len) != 0 ||
        pack_planes(codes, nrows, w, out) != 0) {
        buf_free(&abuf);
        goto fail;
    }
    buf_free(&abuf);
    *nsym_out = (int64_t)alphabet.n;
    *w_out = w;
    free(codes);
    strvec_free(&alphabet);
    hmap_free(&seen);
    return 0;

fail:
    free(codes);
    strvec_free(&alphabet);
    hmap_free(&seen);
    return -1;
}

/* Build every representation this column is eligible for, probe each, keep
 * the smallest. Selectivity is the whole point: coding EVERY column lost on
 * 8 of 8 tables, coding only the ones that measure smaller wins. */
static int plan_column(const StrVec *c, const size_t *perm, size_t nrows,
                       SxzOptions opt, Unit *u)
{
    u->w = 0;
    u->nsym = 0;
    buf_init(&u->blob);

    /* Candidate 1: integers as byte planes. */
    if (opt.use_planes && nrows) {
        int64_t *v = malloc(nrows * sizeof *v);
        if (!v)
            return -1;
        int ok = 1;
        int64_t lo = 0, hi = 0;
        for (size_t r = 0; r < nrows; r++) {
            if (parse_i64(cell(c, perm, r), &v[r]) != 0) { ok = 0; break; }
            if (r == 0 || v[r] < lo) lo = v[r];
            if (r == 0 || v[r] > hi) hi = v[r];
        }
        if (ok) {
            int w = width_for(lo, hi);
            Buf b;
            buf_init(&b);
            if (pack_planes(v, nrows, w, &b) == 0) {
                free(v);
                u->kind = "intp";
                u->w = w;
                u->blob = b;
                goto have_base;
            }
            buf_free(&b);
        }
        free(v);
    }

    /* Candidate 2: constant width. */
    if (opt.use_fixed) {
        int w = fixed_plan(c, perm, nrows);
        if (w > 0) {
            Buf b;
            buf_init(&b);
            if (pack_fixed(c, perm, nrows, w, &b) == 0) {
                u->kind = "fixed";
                u->w = w;
                u->blob = b;
                goto have_base;
            }
            buf_free(&b);
        }
    }

    /* Candidate 3: plain escaped text, which always works. */
    {
        Buf b;
        buf_init(&b);
        if (pack_esc(c, perm, nrows, &b) != 0) {
            buf_free(&b);
            return -1;
        }
        u->kind = "esc";
        u->blob = b;
    }

have_base:
    if (!opt.use_dict)
        return 0;
    {
        Buf d;
        buf_init(&d);
        int64_t nsym;
        int dw;
        if (plan_dict(c, perm, nrows, &d, &nsym, &dw) == 0) {
            size_t a = xz_probe(u->blob.p, u->blob.len);
            size_t b = xz_probe(d.p, d.len);
            if (b != (size_t)-1 && (a == (size_t)-1 || b < a)) {
                buf_free(&u->blob);
                u->kind = "dict";
                u->w = dw;
                u->nsym = nsym;
                u->blob = d;
                return 0;
            }
            buf_free(&d);
        }
    }
    return 0;
}

/* ----------------------------------------------------------- the container */

typedef struct {
    size_t n;            /* rows in this block */
    size_t first_unit;   /* index into the flat unit array */
    size_t nunits;
} BlockMeta;

/* One definition of the header format, used by both the whole-table path and
 * the row-group path. Two copies of this would be two things to keep in sync,
 * and the format is exactly what must not drift. */
static int build_header(const StrVec *names, size_t ncols, size_t nrows,
                        int has_perm, const BlockMeta *blocks, size_t nblocks,
                        const Unit *units, Buf *h)
{
    if (buf_puts(h, "{\"columns\":[") != 0) return -1;
    for (size_t i = 0; i < ncols; i++) {
        if (i && buf_putc(h, ',')) return -1;
        if (json_emit_string(h, names->v[i]) != 0) return -1;
    }
    if (buf_printf(h, "],\"nrows\":%zu,\"perm\":%s,\"blocks\":[",
                   nrows, has_perm ? "true" : "false") != 0)
        return -1;
    for (size_t b = 0; b < nblocks; b++) {
        if (b && buf_putc(h, ',')) return -1;
        if (buf_printf(h, "{\"n\":%zu,\"units\":[", blocks[b].n) != 0) return -1;
        for (size_t k = 0; k < blocks[b].nunits; k++) {
            const Unit *u = &units[blocks[b].first_unit + k];
            if (k && buf_putc(h, ',')) return -1;
            if (buf_printf(h, "[\"%s\",", u->kind) != 0) return -1;
            if (!strcmp(u->kind, "esc")) {
                if (buf_puts(h, "{}") != 0) return -1;
            } else if (!strcmp(u->kind, "dict")) {
                if (buf_printf(h, "{\"w\":%d,\"n\":%lld}", u->w,
                               (long long)u->nsym) != 0) return -1;
            } else {
                if (buf_printf(h, "{\"w\":%d}", u->w) != 0) return -1;
            }
            if (u->col == (size_t)-1) {
                if (buf_puts(h, ",[]]") != 0) return -1;
            } else if (buf_printf(h, ",[%zu]]", u->col) != 0) {
                return -1;
            }
        }
        if (buf_puts(h, "],\"lens\":[") != 0) return -1;
        for (size_t k = 0; k < blocks[b].nunits; k++)
            if (buf_printf(h, "%s%zu", k ? "," : "",
                           units[blocks[b].first_unit + k].len) != 0)
                return -1;
        if (buf_puts(h, "]}") != 0) return -1;
    }
    return buf_puts(h, "]}");
}

/* Build every unit for one block: plan each column, spill its blob, keep only
 * the metadata. `lo` is the block's first GLOBAL row, which is what the
 * permutation records -- the decoder applies one permutation across the whole
 * table, so a per-block sort still has to name global destinations. */
static int verify_unit(const Unit *u, const StrVec *src, const size_t *perm,
                       size_t nrows)
{
    Arena a;
    arena_init(&a);
    StrVec got;
    strvec_init(&got);
    int ok = sxz_unpack_unit(u->kind, u->w, u->nsym, u->blob.p, u->blob.len,
                             nrows, &a, &got) == 0 && got.n == nrows;
    for (size_t r = 0; ok && r < nrows; r++)
        ok = str_eq(got.v[r], src->v[perm ? perm[r] : r]);
    strvec_free(&got);
    arena_free(&a);
    return ok;
}

static int plan_block(const Table *t, const size_t *order, const size_t *perm,
                      size_t lo, SxzOptions opt, FILE *spill, Unit *units)
{
    for (size_t k = 0; k < t->ncols; k++) {
        size_t ci = order[k];
        units[k].col = ci;
        if (plan_column(&t->cols[ci], perm, t->nrows, opt, &units[k]) != 0)
            return -1;
        /* Check it here, while the source rows are still in hand. Under
         * --rows they will be gone by the time the archive exists. */
        if (!verify_unit(&units[k], &t->cols[ci], perm, t->nrows)) {
            buf_free(&units[k].blob);
            return -1;
        }
        size_t n = units[k].blob.len;
        if (n && fwrite(units[k].blob.p, 1, n, spill) != n) {
            buf_free(&units[k].blob);
            return -1;
        }
        units[k].len = n;
        buf_free(&units[k].blob);
    }
    if (perm) {
        int64_t *v = malloc((t->nrows ? t->nrows : 1) * sizeof *v);
        if (!v)
            return -1;
        int64_t plo = 0, phi = 0;
        for (size_t r = 0; r < t->nrows; r++) {
            v[r] = (int64_t)(lo + perm[r]);
            if (r == 0 || v[r] < plo) plo = v[r];
            if (r == 0 || v[r] > phi) phi = v[r];
        }
        Unit *u = &units[t->ncols];
        u->kind = "perm";
        u->w = width_for(plo, phi);
        u->col = (size_t)-1;
        buf_init(&u->blob);
        int rc = pack_planes(v, t->nrows, u->w, &u->blob);
        free(v);
        if (rc != 0)
            return -1;
        if (u->blob.len && fwrite(u->blob.p, 1, u->blob.len, spill) != u->blob.len) {
            buf_free(&u->blob);
            return -1;
        }
        u->len = u->blob.len;
        buf_free(&u->blob);
    }
    return 0;
}

/* Build one unit at a time, spill each blob to a temporary file, free it, then
 * stream header-then-spill into the compressor.
 *
 * Holding every blob in memory at once is what made a 291 MB table peak at
 * 3.16 GB. The blobs must exist somewhere, because their LENGTHS go in the
 * header and the header goes first -- so this is a genuine two-pass, with disk
 * holding pass one and only a single column's worth of RAM live at any moment.
 * The bytes fed to xz are exactly the bytes the all-in-memory version fed it,
 * so the archive does not change: streaming buys memory and costs nothing. */
static int emit_units(const Table *t, const size_t *order, const size_t *perm,
                      SxzOptions opt, XzEnc *enc)
{
    size_t nunits = t->ncols + (perm ? 1 : 0);
    Unit *units = calloc(nunits, sizeof *units);   /* metadata only, no blobs */
    FILE *spill = tmpfile();
    if (!units || !spill) {
        free(units);
        if (spill)
            fclose(spill);
        return -1;
    }

    for (size_t k = 0; k < t->ncols; k++) {
        size_t ci = order[k];
        units[k].col = ci;
        if (plan_column(&t->cols[ci], perm, t->nrows, opt, &units[k]) != 0)
            goto fail;
        size_t n = units[k].blob.len;
        if (n && fwrite(units[k].blob.p, 1, n, spill) != n)
            goto fail;
        units[k].len = n;               /* capture before buf_free zeroes it */
        buf_free(&units[k].blob);
    }
    if (perm) {
        int64_t *v = malloc((t->nrows ? t->nrows : 1) * sizeof *v);
        if (!v)
            goto fail;
        int64_t lo = 0, hi = 0;
        for (size_t r = 0; r < t->nrows; r++) {
            v[r] = (int64_t)perm[r];
            if (r == 0 || v[r] < lo) lo = v[r];
            if (r == 0 || v[r] > hi) hi = v[r];
        }
        Unit *u = &units[t->ncols];
        u->kind = "perm";
        u->w = width_for(lo, hi);
        u->col = (size_t)-1;
        buf_init(&u->blob);
        int rc = pack_planes(v, t->nrows, u->w, &u->blob);
        free(v);
        if (rc != 0)
            goto fail;
        if (u->blob.len && fwrite(u->blob.p, 1, u->blob.len, spill) != u->blob.len)
            goto fail;
        u->len = u->blob.len;
        buf_free(&u->blob);
    }

    /* ---- header ---- */
    Buf h;
    buf_init(&h);
    {
        BlockMeta one = { t->nrows, 0, nunits };
        if (build_header(&t->names, t->ncols, t->nrows, perm != NULL,
                         &one, 1, units, &h) != 0)
            goto fail_h;
    }

    /* ---- stream it out ---- */
    unsigned char hl[4];
    hl[0] = (unsigned char)(h.len);
    hl[1] = (unsigned char)(h.len >> 8);
    hl[2] = (unsigned char)(h.len >> 16);
    hl[3] = (unsigned char)(h.len >> 24);
    if (xz_enc_write(enc, (const char *)hl, 4) != 0 ||
        xz_enc_write(enc, h.p, h.len) != 0)
        goto fail_h;

    rewind(spill);
    {
        char chunk[262144];
        size_t got;
        while ((got = fread(chunk, 1, sizeof chunk, spill)) > 0)
            if (xz_enc_write(enc, chunk, got) != 0)
                goto fail_h;
        if (ferror(spill))
            goto fail_h;
    }

    buf_free(&h);
    fclose(spill);
    free(units);
    return 0;

fail_h:
    buf_free(&h);
fail:
    for (size_t k = 0; k < nunits; k++)
        buf_free(&units[k].blob);
    fclose(spill);
    free(units);
    return -1;
}

static int encode_once(const Table *t, SxzOptions opt, const size_t *perm,
                       Buf *out)
{
    size_t *order = malloc((t->ncols ? t->ncols : 1) * sizeof *order);
    if (!order)
        return -1;
    if (opt.use_pool)
        layout_pool_order(t, order);
    else
        for (size_t i = 0; i < t->ncols; i++)
            order[i] = i;

    XzEnc *enc = xz_enc_begin(opt.tune);
    if (!enc) {
        free(order);
        return -1;
    }
    int rc = emit_units(t, order, perm, opt, enc);
    free(order);
    if (rc != 0) {
        xz_enc_abort(enc);
        return -1;
    }
    if (buf_put(out, SXZ_MAGIC, SXZ_MAGIC_LEN) != 0) {
        xz_enc_abort(enc);
        return -1;
    }
    return xz_enc_finish(enc, out);
}

int sxz_encode(const Table *t, SxzOptions opt, Buf *out, char **err)
{
    Buf plain;
    buf_init(&plain);
    if (encode_once(t, opt, NULL, &plain) != 0) {
        buf_free(&plain);
        if (err && !*err)
            *err = strdup("encode failed");
        return -1;
    }
    if (!opt.use_sort && !opt.guard) {
        *out = plain;
        return 0;
    }

    /* Sorting rows collapses ONE column into runs and shuffles every other.
     * Whether that is a net win is a property of the data, not of the idea:
     * measured over 21 big tables it wins on 15 and loses on 6, and the ones
     * it loses on are the ones that arrived already grouped. So encode both
     * ways and keep the smaller -- never worse, by construction. */
    size_t *perm = malloc((t->nrows ? t->nrows : 1) * sizeof *perm);
    if (!perm) {
        *out = plain;
        return 0;
    }
    layout_sort_rows(t, perm);

    Buf sorted;
    buf_init(&sorted);
    if (encode_once(t, opt, perm, &sorted) != 0) {
        buf_free(&sorted);
        free(perm);
        *out = plain;
        return 0;
    }
    free(perm);

    if (opt.guard && plain.len <= sorted.len) {
        buf_free(&sorted);
        *out = plain;
    } else {
        buf_free(&plain);
        *out = sorted;
    }
    return 0;
}

/* ------------------------------------------------------- row-group encode */

/* Read `block_rows` rows, encode them, discard, repeat. Peak memory becomes
 * one block plus liblzma's own state instead of the whole table.
 *
 * This is the only way off the "whole table in RAM" floor, because a
 * column-major codec cannot stream the way xz does: xz only ever needs the
 * last 64 MB, whereas finishing a COLUMN requires having seen every row. It
 * is what Parquet's row groups are for, and what Polypress's stream.py was
 * for. Two things get worse and both are worth stating plainly:
 *
 *   - the column pooling and the row reorder only see one block, so
 *     compression drops slightly (measured 0.3% at 50k rows, 2.4% at 10k);
 *   - the guard is off, because choosing between two encodings per block
 *     would mean encoding the file twice, and the file is the thing that does
 *     not fit. `--rows` therefore applies the row sort or does not, as asked.
 *
 * The blob spill still lands on DISK at roughly the size of the input, because
 * blob lengths go in the header and the header goes first. Disk, not RAM.
 */
int sxz_encode_file(const char *path, SxzOptions opt, Buf *out,
                    size_t *nrows_out, size_t *ncols_out, char **err)
{
    if (opt.block_rows == 0) {
        Table t;
        if (table_read_csv(path, &t, err) != 0)
            return -1;
        int rc = sxz_encode(&t, opt, out, err);
        if (nrows_out) *nrows_out = t.nrows;
        if (ncols_out) *ncols_out = t.ncols;
        table_free(&t);
        return rc;
    }

    CsvReader r;
    if (csv_open(path, &r, err) != 0)
        return -1;

    FILE *spill = tmpfile();
    XzEnc *enc = xz_enc_begin(opt.tune);
    Unit *units = NULL;
    size_t nunits = 0, unitcap = 0;
    BlockMeta *blocks = NULL;
    size_t nblocks = 0, blockcap = 0;
    size_t *order = malloc((r.ncols ? r.ncols : 1) * sizeof *order);
    size_t *perm = NULL;
    size_t total_rows = 0;
    Table blk;
    table_init(&blk);
    Buf h;
    buf_init(&h);

    if (!spill || !enc || !order) {
        fail(err, "out of memory");
        goto fail;
    }

    for (;;) {
        /* Cells of the previous block pointed into this arena; the block has
         * been spilled, so it is safe to drop the whole thing at once. */
        arena_free(&blk.arena);
        arena_init(&blk.arena);

        size_t got = 0;
        if (csv_read_block(&r, opt.block_rows, &blk, &got, err) != 0)
            goto fail;
        if (got == 0)
            break;

        if (opt.use_pool)
            layout_pool_order(&blk, order);
        else
            for (size_t i = 0; i < blk.ncols; i++)
                order[i] = i;

        const size_t *use_perm = NULL;
        if (opt.use_sort) {
            size_t *np = realloc(perm, got * sizeof *np);
            if (!np) { fail(err, "out of memory"); goto fail; }
            perm = np;
            layout_sort_rows(&blk, perm);
            use_perm = perm;
        }

        size_t need = blk.ncols + (use_perm ? 1 : 0);
        if (nunits + need > unitcap) {
            size_t want = unitcap ? unitcap * 2 : need * 4;
            while (want < nunits + need)
                want *= 2;
            Unit *nu = realloc(units, want * sizeof *nu);
            if (!nu) { fail(err, "out of memory"); goto fail; }
            units = nu;
            unitcap = want;
        }
        if (nblocks == blockcap) {
            size_t want = blockcap ? blockcap * 2 : 16;
            BlockMeta *nb = realloc(blocks, want * sizeof *nb);
            if (!nb) { fail(err, "out of memory"); goto fail; }
            blocks = nb;
            blockcap = want;
        }

        memset(units + nunits, 0, need * sizeof *units);
        if (plan_block(&blk, order, use_perm, total_rows, opt, spill,
                       units + nunits) != 0) {
            fail(err, "encode failed");
            goto fail;
        }
        blocks[nblocks].n = got;
        blocks[nblocks].first_unit = nunits;
        blocks[nblocks].nunits = need;
        nblocks++;
        nunits += need;
        total_rows += got;
    }

    if (nblocks == 0) {
        fail(err, "input has no data rows");
        goto fail;
    }

    if (build_header(&r.names, r.ncols, total_rows, opt.use_sort != 0,
                     blocks, nblocks, units, &h) != 0) {
        fail(err, "out of memory building header");
        goto fail;
    }

    unsigned char hl[4];
    hl[0] = (unsigned char)(h.len);
    hl[1] = (unsigned char)(h.len >> 8);
    hl[2] = (unsigned char)(h.len >> 16);
    hl[3] = (unsigned char)(h.len >> 24);
    if (buf_put(out, SXZ_MAGIC, SXZ_MAGIC_LEN) != 0 ||
        xz_enc_write(enc, (const char *)hl, 4) != 0 ||
        xz_enc_write(enc, h.p, h.len) != 0) {
        fail(err, "compression failed");
        goto fail;
    }
    rewind(spill);
    {
        char chunk[262144];
        size_t got;
        while ((got = fread(chunk, 1, sizeof chunk, spill)) > 0)
            if (xz_enc_write(enc, chunk, got) != 0) {
                fail(err, "compression failed");
                goto fail;
            }
        if (ferror(spill)) {
            fail(err, "temporary file read failed");
            goto fail;
        }
    }
    if (xz_enc_finish(enc, out) != 0) {
        enc = NULL;
        fail(err, "compression failed");
        goto fail;
    }
    enc = NULL;

    if (nrows_out) *nrows_out = total_rows;
    if (ncols_out) *ncols_out = r.ncols;
    buf_free(&h);
    free(units);
    free(blocks);
    free(order);
    free(perm);
    fclose(spill);
    table_free(&blk);
    csv_close(&r);
    return 0;

fail:
    buf_free(&h);
    free(units);
    free(blocks);
    free(order);
    free(perm);
    if (spill) fclose(spill);
    if (enc) xz_enc_abort(enc);
    table_free(&blk);
    csv_close(&r);
    return -1;
}

/* ---------------------------------------------------------------- layout */

/* Two columns drawing on the same vocabulary -- ten ICD-10 diagnosis slots, a
 * set of agency fields -- each pay to establish that alphabet separately when
 * they sit megabytes apart. Adjacent, the second copies from the first at a
 * short distance, which is several times cheaper. */

#define POOL_MIN_CARD 4
#define POOL_MAX_CARD 200000
#define POOL_OVERLAP_NUM 1      /* share >= 1/2 of the smaller vocabulary */
#define POOL_OVERLAP_DEN 2

void layout_pool_order(const Table *t, size_t *order)
{
    size_t n = t->ncols;
    for (size_t i = 0; i < n; i++)
        order[i] = i;
    if (n < 2 || t->nrows == 0)
        return;

    HMap *vocab = calloc(n, sizeof *vocab);
    int *live = calloc(n, sizeof *live);
    size_t *parent = malloc(n * sizeof *parent);
    if (!vocab || !live || !parent) {
        free(vocab); free(live); free(parent);
        return;
    }
    for (size_t i = 0; i < n; i++) {
        parent[i] = i;
        if (hmap_init(&vocab[i], 64) != 0)
            continue;
        for (size_t r = 0; r < t->nrows; r++) {
            int fresh;
            if (!hmap_put(&vocab[i], t->cols[i].v[r], &fresh))
                break;
            if (vocab[i].n > POOL_MAX_CARD)
                break;
        }
        live[i] = vocab[i].n >= POOL_MIN_CARD && vocab[i].n <= POOL_MAX_CARD;
    }

    for (size_t i = 0; i < n; i++) {
        if (!live[i])
            continue;
        for (size_t j = i + 1; j < n; j++) {
            if (!live[j])
                continue;
            size_t a = i, b = j;
            while (parent[a] != a) { parent[a] = parent[parent[a]]; a = parent[a]; }
            while (parent[b] != b) { parent[b] = parent[parent[b]]; b = parent[b]; }
            if (a == b)
                continue;
            const HMap *small = vocab[i].n <= vocab[j].n ? &vocab[i] : &vocab[j];
            const HMap *large = small == &vocab[i] ? &vocab[j] : &vocab[i];
            size_t shared = 0;
            for (size_t k = 0; k < small->cap; k++)
                if (small->e[k].used && hmap_has(large, small->e[k].key))
                    shared++;
            if (shared * POOL_OVERLAP_DEN >= small->n * POOL_OVERLAP_NUM)
                parent[b] = a;
        }
    }

    /* Emit groups in the order their first member appears, members in index
     * order, so a table with nothing to pool comes back untouched. */
    size_t *root_of = malloc(n * sizeof *root_of);
    size_t *first = malloc(n * sizeof *first);
    if (root_of && first) {
        size_t ngroups = 0;
        for (size_t i = 0; i < n; i++) {
            size_t a = i;
            while (parent[a] != a) { parent[a] = parent[parent[a]]; a = parent[a]; }
            root_of[i] = a;
        }
        size_t out = 0;
        for (size_t i = 0; i < n; i++) {
            int seen = 0;
            for (size_t g = 0; g < ngroups; g++)
                if (first[g] == root_of[i]) { seen = 1; break; }
            if (seen)
                continue;
            first[ngroups++] = root_of[i];
            for (size_t j = 0; j < n; j++)
                if (root_of[j] == root_of[i])
                    order[out++] = j;
        }
    }
    free(root_of);
    free(first);

    for (size_t i = 0; i < n; i++)
        hmap_free(&vocab[i]);
    free(vocab);
    free(live);
    free(parent);
}

/* Row order: lexicographic over every column, lowest cardinality first. */

static const Table *g_sort_table;
static size_t *g_sort_keys;

static int cmp_rows(const void *pa, const void *pb)
{
    size_t a = *(const size_t *)pa, b = *(const size_t *)pb;
    const Table *t = g_sort_table;
    for (size_t k = 0; k < t->ncols; k++) {
        size_t c = g_sort_keys[k];
        Str x = t->cols[c].v[a], y = t->cols[c].v[b];
        size_t m = x.n < y.n ? x.n : y.n;
        int rc = m ? memcmp(x.s, y.s, m) : 0;
        if (rc) return rc;
        if (x.n != y.n) return x.n < y.n ? -1 : 1;
    }
    /* Ties fall to the original row index: a rule both implementations can
     * keep, unlike whatever order a sort happens to leave equal keys in. */
    return a < b ? -1 : (a > b ? 1 : 0);
}

static int cmp_card(const void *pa, const void *pb)
{
    const int64_t *a = pa, *b = pb;
    if (a[0] != b[0]) return a[0] < b[0] ? -1 : 1;
    return a[1] < b[1] ? -1 : 1;
}

void layout_sort_rows(const Table *t, size_t *perm)
{
    for (size_t r = 0; r < t->nrows; r++)
        perm[r] = r;
    if (t->nrows < 2 || t->ncols == 0)
        return;

    int64_t *card = malloc(t->ncols * 2 * sizeof *card);
    size_t *keys = malloc(t->ncols * sizeof *keys);
    if (!card || !keys) {
        free(card); free(keys);
        return;
    }
    for (size_t c = 0; c < t->ncols; c++) {
        HMap m;
        if (hmap_init(&m, 64) != 0) {
            card[c * 2] = 0;
        } else {
            for (size_t r = 0; r < t->nrows; r++) {
                int fresh;
                if (!hmap_put(&m, t->cols[c].v[r], &fresh))
                    break;
            }
            card[c * 2] = (int64_t)m.n;
            hmap_free(&m);
        }
        card[c * 2 + 1] = (int64_t)c;
    }
    qsort(card, t->ncols, 2 * sizeof *card, cmp_card);
    for (size_t c = 0; c < t->ncols; c++)
        keys[c] = (size_t)card[c * 2 + 1];

    g_sort_table = t;
    g_sort_keys = keys;
    qsort(perm, t->nrows, sizeof *perm, cmp_rows);
    g_sort_table = NULL;
    g_sort_keys = NULL;

    free(card);
    free(keys);
}
