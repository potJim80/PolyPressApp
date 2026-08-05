/* Buffers, cells, CSV, liblzma and just enough JSON. Nothing codec-specific. */
#include "sxz.h"

#include <lzma.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/* ------------------------------------------------------------------ bytes */

void buf_init(Buf *b) { b->p = NULL; b->len = b->cap = 0; }

int buf_reserve(Buf *b, size_t need)
{
    if (b->cap - b->len >= need)
        return 0;
    size_t want = b->cap ? b->cap : 64;
    while (want - b->len < need) {
        if (want > (SIZE_MAX >> 1))
            return -1;
        /* Double while small, then grow by half. Doubling a 300 MB buffer
         * reserves 600 MB to hold 301, and on the payload buffer alone that
         * was hundreds of megabytes of resident set doing nothing. */
        want = want < (64u << 20) ? want << 1 : want + (want >> 1);
    }
    char *p = realloc(b->p, want);
    if (!p)
        return -1;
    b->p = p;
    b->cap = want;
    return 0;
}

int buf_put(Buf *b, const void *data, size_t n)
{
    if (n == 0)
        return 0;
    if (buf_reserve(b, n) != 0)
        return -1;
    memcpy(b->p + b->len, data, n);
    b->len += n;
    return 0;
}

int buf_putc(Buf *b, char c) { return buf_put(b, &c, 1); }
int buf_puts(Buf *b, const char *s) { return buf_put(b, s, strlen(s)); }

int buf_printf(Buf *b, const char *fmt, ...)
{
    char tmp[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < 0)
        return -1;
    if ((size_t)n < sizeof tmp)
        return buf_put(b, tmp, (size_t)n);

    char *big = malloc((size_t)n + 1);
    if (!big)
        return -1;
    va_start(ap, fmt);
    vsnprintf(big, (size_t)n + 1, fmt, ap);
    va_end(ap);
    int rc = buf_put(b, big, (size_t)n);
    free(big);
    return rc;
}

void buf_free(Buf *b)
{
    free(b->p);
    b->p = NULL;
    b->len = b->cap = 0;
}

/* ----------------------------------------------------------------- strings */

int str_eq(Str a, Str b)
{
    return a.n == b.n && (a.n == 0 || memcmp(a.s, b.s, a.n) == 0);
}

uint64_t str_hash(Str a)
{
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < a.n; i++) {
        h ^= (unsigned char)a.s[i];
        h *= 1099511628211ULL;
    }
    return h;
}

void strvec_init(StrVec *v) { v->v = NULL; v->n = v->cap = 0; }

int strvec_push(StrVec *v, Str s)
{
    if (v->n == v->cap) {
        size_t want = v->cap ? v->cap * 2 : 16;
        Str *p = realloc(v->v, want * sizeof *p);
        if (!p)
            return -1;
        v->v = p;
        v->cap = want;
    }
    v->v[v->n++] = s;
    return 0;
}

void strvec_free(StrVec *v) { free(v->v); v->v = NULL; v->n = v->cap = 0; }


/* ------------------------------------------------------------------ arena */

#define ARENA_BLOCK (1u << 20)

struct ArenaBlock {
    ArenaBlock *next;
    size_t used, cap;
    char data[];
};

void arena_init(Arena *a) { a->head = NULL; }

char *arena_alloc(Arena *a, size_t n)
{
    ArenaBlock *b = a->head;
    if (!b || b->cap - b->used < n) {
        size_t cap = n > ARENA_BLOCK ? n : ARENA_BLOCK;
        ArenaBlock *nb = malloc(sizeof *nb + cap);
        if (!nb)
            return NULL;
        nb->next = a->head;
        nb->used = 0;
        nb->cap = cap;
        a->head = nb;
        b = nb;
    }
    char *p = b->data + b->used;
    b->used += n;
    return p;
}

int arena_put(Arena *a, const void *data, size_t n, Str *out)
{
    char *p = arena_alloc(a, n ? n : 1);
    if (!p)
        return -1;
    if (n)
        memcpy(p, data, n);
    out->s = p;
    out->n = n;
    return 0;
}

void arena_free(Arena *a)
{
    for (ArenaBlock *b = a->head; b;) {
        ArenaBlock *next = b->next;
        free(b);
        b = next;
    }
    a->head = NULL;
}

/* ------------------------------------------------------------------ table */

void table_init(Table *t)
{
    strvec_init(&t->names);
    t->cols = NULL;
    t->ncols = t->nrows = 0;
    arena_init(&t->arena);
    buf_init(&t->source);
}

void table_free(Table *t)
{
    for (size_t i = 0; i < t->ncols; i++)
        strvec_free(&t->cols[i]);
    free(t->cols);
    strvec_free(&t->names);
    arena_free(&t->arena);
    buf_free(&t->source);
    t->cols = NULL;
    t->ncols = t->nrows = 0;
}

static int fail(char **err, const char *msg)
{
    if (err && !*err)
        *err = strdup(msg);
    return -1;
}

/* ------------------------------------------------------------- csv reader */

/* Refill the window: drop what has been consumed, then top up from the file.
 * This both MOVES and may REALLOCATE the buffer, so it must only ever be
 * called at a block boundary -- cells of the block just finished are dead by
 * then, and cells of the block about to be read do not exist yet. Calling it
 * mid-block would leave every cell already handed out pointing at freed or
 * shifted memory. */
static int csv_fill(CsvReader *r)
{
    if (r->pos) {
        memmove(r->win.p, r->win.p + r->pos, r->win.len - r->pos);
        r->win.len -= r->pos;
        r->pos = 0;
    }
    while (!r->eof && r->win.len < r->target) {
        size_t want = r->target - r->win.len;
        if (buf_reserve(&r->win, want) != 0)
            return -1;
        ssize_t got = read(r->fd, r->win.p + r->win.len, want);
        if (got < 0)
            return -1;
        if (got == 0) {
            r->eof = 1;
            break;
        }
        r->win.len += (size_t)got;
    }
    return 0;
}

/* Parse one field starting at *p. Unquoted fields point straight into the
 * window; quoted ones are unescaped into the arena. Returns 0 on success,
 * 1 if the field runs past the end of available data (caller must refill). */
static int csv_field(const char **p, const char *end, int eof, Arena *arena,
                     Buf *scratch, Str *out, int *end_of_row)
{
    const char *q = *p;
    if (q < end && *q == '"') {
        q++;
        scratch->len = 0;
        int closed = 0;
        while (q < end) {
            if (*q == '"') {
                if (q + 1 < end && q[1] == '"') {
                    if (buf_putc(scratch, '"') != 0) return -1;
                    q += 2;
                    continue;
                }
                if (q + 1 >= end && !eof)
                    return 1;              /* might be a doubled quote */
                q++;
                closed = 1;
                break;
            }
            if (buf_putc(scratch, *q++) != 0) return -1;
        }
        if (!closed && !eof)
            return 1;
        if (arena_put(arena, scratch->p, scratch->len, out) != 0) return -1;
    } else {
        const char *st = q;
        while (q < end && *q != ',' && *q != '\n' && *q != '\r')
            q++;
        if (q >= end && !eof)
            return 1;                      /* field may continue */
        out->s = st;
        out->n = (size_t)(q - st);
    }

    *end_of_row = 0;
    if (q >= end) {
        if (!eof)
            return 1;
        *end_of_row = 1;
    } else if (*q == ',') {
        q++;
    } else if (*q == '\r' || *q == '\n') {
        if (*q == '\r' && q + 1 >= end && !eof)
            return 1;
        if (*q == '\r' && q + 1 < end && q[1] == '\n')
            q++;
        q++;
        *end_of_row = 1;
    }
    *p = q;
    return 0;
}

int csv_open(const char *path, CsvReader *r, char **err)
{
    memset(r, 0, sizeof *r);
    strvec_init(&r->names);
    buf_init(&r->win);
    r->fd = open(path, O_RDONLY);
    if (r->fd < 0)
        return fail(err, "cannot open input file");
    r->target = SXZ_WINDOW;
    if (csv_fill(r) != 0) {
        csv_close(r);
        return fail(err, "cannot read input file");
    }
    if (r->win.len == 0) {
        csv_close(r);
        return fail(err, "input file is empty");
    }

    const char *p = r->win.p, *end = r->win.p + r->win.len;
    if (r->win.len >= 3 && (unsigned char)p[0] == 0xEF &&
        (unsigned char)p[1] == 0xBB && (unsigned char)p[2] == 0xBF)
        p += 3;
    if (r->win.len >= 2 &&
        (((unsigned char)p[0] == 0xFF && (unsigned char)p[1] == 0xFE) ||
         ((unsigned char)p[0] == 0xFE && (unsigned char)p[1] == 0xFF))) {
        csv_close(r);
        return fail(err, "input looks like UTF-16; convert it to UTF-8 first");
    }

    /* Header names are copied out: everything else in the window is transient
     * and the names have to outlive every refill. */
    Arena tmp;
    arena_init(&tmp);
    Buf scratch;
    buf_init(&scratch);
    for (;;) {
        Str cell;
        int eor = 0;
        int rc = csv_field(&p, end, r->eof, &tmp, &scratch, &cell, &eor);
        if (rc != 0) {
            buf_free(&scratch);
            arena_free(&tmp);
            csv_close(r);
            return fail(err, rc > 0 ? "header row is longer than the read window"
                                    : "out of memory reading header");
        }
        char *cp = malloc(cell.n ? cell.n : 1);
        if (!cp || strvec_push(&r->names, (Str){ cp, cell.n }) != 0) {
            free(cp);
            buf_free(&scratch);
            arena_free(&tmp);
            csv_close(r);
            return fail(err, "out of memory");
        }
        memcpy(cp, cell.s, cell.n);
        if (eor)
            break;
    }
    buf_free(&scratch);
    arena_free(&tmp);

    r->ncols = r->names.n;
    if (r->ncols == 0 || r->ncols > SXZ_MAX_COLS) {
        csv_close(r);
        return fail(err, "header row is empty or absurdly wide");
    }
    r->pos = (size_t)(p - r->win.p);
    return 0;
}

void csv_close(CsvReader *r)
{
    for (size_t i = 0; i < r->names.n; i++)
        free((void *)r->names.v[i].s);
    strvec_free(&r->names);
    buf_free(&r->win);
    if (r->fd >= 0)
        close(r->fd);
    r->fd = -1;
}

int csv_read_block(CsvReader *r, size_t max, Table *out, size_t *got,
                   char **err)
{
    *got = 0;
    size_t width = r->ncols;

    if (csv_fill(r) != 0)
        return fail(err, "cannot read input file");

    if (out->ncols != width) {
        out->ncols = width;
        out->cols = calloc(width, sizeof *out->cols);
        if (!out->cols)
            return fail(err, "out of memory");
        size_t hint = max != (size_t)-1 && max < 1000000 ? max : 4096;
        for (size_t i = 0; i < width; i++) {
            strvec_init(&out->cols[i]);
            out->cols[i].v = malloc((hint ? hint : 1) * sizeof *out->cols[i].v);
            if (!out->cols[i].v)
                return fail(err, "out of memory");
            out->cols[i].cap = hint ? hint : 1;
        }
        for (size_t i = 0; i < width; i++)
            if (strvec_push(&out->names, r->names.v[i]) != 0)
                return fail(err, "out of memory");
    }
    for (size_t i = 0; i < width; i++)
        out->cols[i].n = 0;
    out->nrows = 0;

    StrVec row;
    strvec_init(&row);
    Buf scratch;
    buf_init(&scratch);
    static const Str empty = { "", 0 };
    const char *p = r->win.p + r->pos, *end = r->win.p + r->win.len;

    while (p < end && out->nrows < max) {
        const char *row_start = p;
        row.n = 0;
        int complete = 0, oom = 0;
        while (p < end) {
            Str cell;
            int eor = 0;
            int rc = csv_field(&p, end, r->eof, &out->arena, &scratch, &cell,
                               &eor);
            if (rc > 0)
                break;                    /* row continues past the window */
            if (rc < 0) { oom = 1; break; }
            if (strvec_push(&row, cell) != 0) { oom = 1; break; }
            if (eor) { complete = 1; break; }
        }
        if (oom)
            goto oom;
        if (!complete) {
            /* Leave the partial row for the next block. If a single row does
             * not fit the window at all, say so rather than loop forever. */
            if (row_start == r->win.p + r->pos && out->nrows == 0 && !r->eof) {
                strvec_free(&row);
                buf_free(&scratch);
                return fail(err, "a single row is larger than the read window");
            }
            p = row_start;
            break;
        }
        for (size_t i = 0; i < width; i++)
            if (strvec_push(&out->cols[i], i < row.n ? row.v[i] : empty) != 0)
                goto oom;
        out->nrows++;
    }

    strvec_free(&row);
    buf_free(&scratch);
    r->pos = (size_t)(p - r->win.p);
    *got = out->nrows;
    return 0;

oom:
    strvec_free(&row);
    buf_free(&scratch);
    return fail(err, "out of memory reading input");
}

int table_read_csv(const char *path, Table *out, char **err)
{
    CsvReader r;
    if (csv_open(path, &r, err) != 0)
        return -1;
    /* Whole-table read: let the window be the entire file, so one block holds
     * everything and cells stay valid for the table's lifetime. */
    struct stat st;
    if (fstat(r.fd, &st) == 0 && st.st_size > 0)
        r.target = (size_t)st.st_size + 16;
    table_init(out);
    size_t got;
    if (csv_read_block(&r, (size_t)-1, out, &got, err) != 0) {
        csv_close(&r);
        table_free(out);
        return -1;
    }
    /* The table takes over the window; unquoted cells point into it. */
    out->source = r.win;
    buf_init(&r.win);
    for (size_t i = 0; i < out->ncols; i++) {
        Str nm;
        if (arena_put(&out->arena, out->names.v[i].s, out->names.v[i].n, &nm) != 0) {
            csv_close(&r);
            table_free(out);
            return fail(err, "out of memory");
        }
        out->names.v[i] = nm;
    }
    csv_close(&r);
    return 0;
}

static int csv_needs_quote(Str s)
{
    for (size_t i = 0; i < s.n; i++) {
        char c = s.s[i];
        if (c == ',' || c == '"' || c == '\n' || c == '\r')
            return 1;
    }
    return 0;
}

static int csv_write_field(Str s, FILE *fh)
{
    if (!csv_needs_quote(s)) {
        if (s.n && fwrite(s.s, 1, s.n, fh) != s.n)
            return -1;
        return 0;
    }
    if (fputc('"', fh) == EOF)
        return -1;
    for (size_t i = 0; i < s.n; i++) {
        if (s.s[i] == '"' && fputc('"', fh) == EOF)
            return -1;
        if (fputc(s.s[i], fh) == EOF)
            return -1;
    }
    return fputc('"', fh) == EOF ? -1 : 0;
}

int table_write_csv(const Table *t, FILE *fh)
{
    for (size_t c = 0; c < t->ncols; c++) {
        if (c && fputc(',', fh) == EOF)
            return -1;
        if (csv_write_field(t->names.v[c], fh) != 0)
            return -1;
    }
    if (fputc('\n', fh) == EOF)
        return -1;
    for (size_t r = 0; r < t->nrows; r++) {
        for (size_t c = 0; c < t->ncols; c++) {
            if (c && fputc(',', fh) == EOF)
                return -1;
            if (csv_write_field(t->cols[c].v[r], fh) != 0)
                return -1;
        }
        if (fputc('\n', fh) == EOF)
            return -1;
    }
    return 0;
}

/* -------------------------------------------------------------------- xz */

const XzTune XZ_DEFAULT_TUNE = { -1, -1, -1 };

static void tune_filter(lzma_options_lzma *o, XzTune t)
{
    if (t.lc >= 0) o->lc = (uint32_t)t.lc;
    if (t.lp >= 0) o->lp = (uint32_t)t.lp;
    if (t.pb >= 0) o->pb = (uint32_t)t.pb;
}

static int xz_run(const char *data, size_t n, uint32_t preset, XzTune tune,
                  Buf *out)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, preset))
        return -1;
    tune_filter(&opt, tune);

    lzma_filter filters[2];
    filters[0].id = LZMA_FILTER_LZMA2;
    filters[0].options = &opt;
    filters[1].id = LZMA_VLI_UNKNOWN;
    filters[1].options = NULL;

    lzma_stream strm = LZMA_STREAM_INIT;
    if (lzma_stream_encoder(&strm, filters, LZMA_CHECK_CRC64) != LZMA_OK)
        return -1;

    /* lzma_stream_buffer_bound is the documented worst case; using it means
     * one allocation and no incremental regrow. */
    size_t bound = lzma_stream_buffer_bound(n) + 128;
    if (buf_reserve(out, bound) != 0) {
        lzma_end(&strm);
        return -1;
    }
    strm.next_in = (const uint8_t *)data;
    strm.avail_in = n;
    strm.next_out = (uint8_t *)out->p + out->len;
    strm.avail_out = out->cap - out->len;

    lzma_ret rc = lzma_code(&strm, LZMA_FINISH);
    size_t produced = (out->cap - out->len) - strm.avail_out;
    lzma_end(&strm);
    if (rc != LZMA_STREAM_END)
        return -1;
    out->len += produced;
    return 0;
}

int xz_compress(const char *data, size_t n, XzTune tune, Buf *out)
{
    return xz_run(data, n, 9 | LZMA_PRESET_EXTREME, tune, out);
}

/* ---------------------------------------------------- incremental encoder */

struct XzEnc {
    lzma_stream       strm;
    lzma_options_lzma opt;
    Buf               out;
    int               dead;
};

XzEnc *xz_enc_begin(XzTune tune)
{
    XzEnc *e = calloc(1, sizeof *e);
    if (!e)
        return NULL;
    lzma_stream init = LZMA_STREAM_INIT;
    e->strm = init;
    buf_init(&e->out);
    if (lzma_lzma_preset(&e->opt, 9 | LZMA_PRESET_EXTREME)) {
        free(e);
        return NULL;
    }
    tune_filter(&e->opt, tune);

    /* The filter array points at e->opt, which must outlive this call --
     * which is why the options live in the struct, not on the stack. */
    lzma_filter filters[2];
    filters[0].id = LZMA_FILTER_LZMA2;
    filters[0].options = &e->opt;
    filters[1].id = LZMA_VLI_UNKNOWN;
    filters[1].options = NULL;

    if (lzma_stream_encoder(&e->strm, filters, LZMA_CHECK_CRC64) != LZMA_OK) {
        free(e);
        return NULL;
    }
    return e;
}

static int xz_enc_pump(XzEnc *e, lzma_action action)
{
    char chunk[65536];
    for (;;) {
        e->strm.next_out = (uint8_t *)chunk;
        e->strm.avail_out = sizeof chunk;
        lzma_ret rc = lzma_code(&e->strm, action);
        size_t produced = sizeof chunk - e->strm.avail_out;
        if (produced && buf_put(&e->out, chunk, produced) != 0)
            return -1;
        if (rc == LZMA_STREAM_END)
            return 1;
        if (rc != LZMA_OK)
            return -1;
        if (action == LZMA_RUN && e->strm.avail_in == 0)
            return 0;
        if (action == LZMA_FINISH && produced == 0 && e->strm.avail_in == 0)
            return -1;                  /* no progress; refuse to spin */
    }
}

int xz_enc_write(XzEnc *e, const char *data, size_t n)
{
    if (!e || e->dead)
        return -1;
    if (n == 0)
        return 0;
    e->strm.next_in = (const uint8_t *)data;
    e->strm.avail_in = n;
    if (xz_enc_pump(e, LZMA_RUN) < 0) {
        e->dead = 1;
        return -1;
    }
    return 0;
}

int xz_enc_finish(XzEnc *e, Buf *out)
{
    if (!e)
        return -1;
    int rc = -1;
    if (!e->dead) {
        e->strm.next_in = NULL;
        e->strm.avail_in = 0;
        rc = xz_enc_pump(e, LZMA_FINISH) == 1 ? 0 : -1;
    }
    if (rc == 0)
        rc = buf_put(out, e->out.p, e->out.len);
    lzma_end(&e->strm);
    buf_free(&e->out);
    free(e);
    return rc;
}

void xz_enc_abort(XzEnc *e)
{
    if (!e)
        return;
    lzma_end(&e->strm);
    buf_free(&e->out);
    free(e);
}

size_t xz_probe(const char *data, size_t n)
{
    Buf tmp;
    buf_init(&tmp);
    if (xz_run(data, n, 1, XZ_DEFAULT_TUNE, &tmp) != 0) {
        buf_free(&tmp);
        return (size_t)-1;
    }
    size_t len = tmp.len;   /* capture before buf_free zeroes it */
    buf_free(&tmp);
    return len;
}

int xz_decompress(const char *data, size_t n, size_t limit, Buf *out)
{
    lzma_stream strm = LZMA_STREAM_INIT;
    /* A hostile archive must not be able to make us allocate without bound:
     * the memlimit and the output limit are both enforced. */
    if (lzma_stream_decoder(&strm, 512ULL << 20, LZMA_CONCATENATED) != LZMA_OK)
        return -1;

    strm.next_in = (const uint8_t *)data;
    strm.avail_in = n;

    char chunk[65536];
    for (;;) {
        strm.next_out = (uint8_t *)chunk;
        strm.avail_out = sizeof chunk;
        lzma_ret rc = lzma_code(&strm, LZMA_FINISH);
        size_t produced = sizeof chunk - strm.avail_out;
        if (produced) {
            if (out->len + produced > limit) {
                lzma_end(&strm);
                return -1;
            }
            if (buf_put(out, chunk, produced) != 0) {
                lzma_end(&strm);
                return -1;
            }
        }
        if (rc == LZMA_STREAM_END)
            break;
        if (rc != LZMA_OK) {
            lzma_end(&strm);
            return -1;
        }
        if (strm.avail_in == 0 && produced == 0) {
            lzma_end(&strm);
            return -1;                 /* truncated stream */
        }
    }
    lzma_end(&strm);
    return 0;
}

/* ------------------------------------------------------------------- json */

/* Nodes come from a chain of fixed blocks so their addresses stay put as more
 * are allocated -- a plain realloc'd array would invalidate every child
 * pointer already handed out. */
#define JSON_BLOCK 256

typedef struct JsonBlock {
    struct JsonBlock *next;
    size_t used;
    Json nodes[JSON_BLOCK];
} JsonBlock;

typedef struct {
    JsonBlock *blocks;
    Buf strings;          /* reserved once; never grows, so Str stay valid */
    Json **stack;
    size_t stack_cap;
} JsonPool;

typedef struct {
    const char *p, *end;
    JsonPool   *pool;
    char      **err;
} JsonParser;

static Json *json_alloc(JsonPool *pool)
{
    JsonBlock *b = pool->blocks;
    if (!b || b->used == JSON_BLOCK) {
        JsonBlock *nb = calloc(1, sizeof *nb);
        if (!nb)
            return NULL;
        nb->next = pool->blocks;
        pool->blocks = nb;
        b = nb;
    }
    return &b->nodes[b->used++];
}

static void json_skip_ws(JsonParser *ps)
{
    while (ps->p < ps->end &&
           (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r'))
        ps->p++;
}

static int json_utf8(Buf *b, unsigned long cp)
{
    if (cp < 0x80) {
        b->p[b->len++] = (char)cp;
    } else if (cp < 0x800) {
        b->p[b->len++] = (char)(0xC0 | (cp >> 6));
        b->p[b->len++] = (char)(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        b->p[b->len++] = (char)(0xE0 | (cp >> 12));
        b->p[b->len++] = (char)(0x80 | ((cp >> 6) & 0x3F));
        b->p[b->len++] = (char)(0x80 | (cp & 0x3F));
    } else {
        b->p[b->len++] = (char)(0xF0 | (cp >> 18));
        b->p[b->len++] = (char)(0x80 | ((cp >> 12) & 0x3F));
        b->p[b->len++] = (char)(0x80 | ((cp >> 6) & 0x3F));
        b->p[b->len++] = (char)(0x80 | (cp & 0x3F));
    }
    return 0;
}

static int json_hex4(JsonParser *ps, unsigned long *out)
{
    if (ps->end - ps->p < 4)
        return -1;
    unsigned long v = 0;
    for (int i = 0; i < 4; i++) {
        char c = *ps->p++;
        v <<= 4;
        if (c >= '0' && c <= '9') v |= (unsigned long)(c - '0');
        else if (c >= 'a' && c <= 'f') v |= (unsigned long)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') v |= (unsigned long)(c - 'A' + 10);
        else return -1;
    }
    *out = v;
    return 0;
}

static int json_string(JsonParser *ps, Str *out)
{
    if (ps->p >= ps->end || *ps->p != '"')
        return -1;
    ps->p++;
    Buf *sb = &ps->pool->strings;
    size_t start = sb->len;
    while (ps->p < ps->end && *ps->p != '"') {
        if (*ps->p != '\\') {
            sb->p[sb->len++] = *ps->p++;
            continue;
        }
        ps->p++;
        if (ps->p >= ps->end)
            return -1;
        char c = *ps->p++;
        switch (c) {
        case '"': case '\\': case '/': sb->p[sb->len++] = c; break;
        case 'b': sb->p[sb->len++] = '\b'; break;
        case 'f': sb->p[sb->len++] = '\f'; break;
        case 'n': sb->p[sb->len++] = '\n'; break;
        case 'r': sb->p[sb->len++] = '\r'; break;
        case 't': sb->p[sb->len++] = '\t'; break;
        case 'u': {
            unsigned long cp;
            if (json_hex4(ps, &cp) != 0)
                return -1;
            if (cp >= 0xD800 && cp <= 0xDBFF && ps->end - ps->p >= 6 &&
                ps->p[0] == '\\' && ps->p[1] == 'u') {
                const char *save = ps->p;
                ps->p += 2;
                unsigned long lo;
                if (json_hex4(ps, &lo) == 0 && lo >= 0xDC00 && lo <= 0xDFFF)
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                else
                    ps->p = save;
            }
            json_utf8(sb, cp);
            break;
        }
        default:
            return -1;
        }
    }
    if (ps->p >= ps->end)
        return -1;
    ps->p++;
    out->s = sb->p + start;
    out->n = sb->len - start;
    return 0;
}

static Json *json_value(JsonParser *ps);

static Json *json_container(JsonParser *ps, int is_obj)
{
    Json *node = json_alloc(ps->pool);
    if (!node)
        return NULL;
    node->kind = is_obj ? JS_OBJ : JS_ARR;
    ps->p++;                                    /* consume { or [ */

    size_t cap = 8;
    node->items = malloc(cap * sizeof *node->items);
    node->keys = is_obj ? malloc(cap * sizeof *node->keys) : NULL;
    if (!node->items || (is_obj && !node->keys))
        return NULL;

    json_skip_ws(ps);
    if (ps->p < ps->end && *ps->p == (is_obj ? '}' : ']')) {
        ps->p++;
        return node;
    }
    for (;;) {
        if (node->count == cap) {
            cap *= 2;
            Json **it = realloc(node->items, cap * sizeof *it);
            if (!it)
                return NULL;
            node->items = it;
            if (is_obj) {
                Str *k = realloc(node->keys, cap * sizeof *k);
                if (!k)
                    return NULL;
                node->keys = k;
            }
        }
        json_skip_ws(ps);
        if (is_obj) {
            if (json_string(ps, &node->keys[node->count]) != 0)
                return NULL;
            json_skip_ws(ps);
            if (ps->p >= ps->end || *ps->p != ':')
                return NULL;
            ps->p++;
        }
        Json *child = json_value(ps);
        if (!child)
            return NULL;
        node->items[node->count++] = child;
        json_skip_ws(ps);
        if (ps->p >= ps->end)
            return NULL;
        if (*ps->p == ',') {
            ps->p++;
            continue;
        }
        if (*ps->p == (is_obj ? '}' : ']')) {
            ps->p++;
            return node;
        }
        return NULL;
    }
}

static Json *json_value(JsonParser *ps)
{
    json_skip_ws(ps);
    if (ps->p >= ps->end)
        return NULL;
    char c = *ps->p;
    if (c == '{' || c == '[')
        return json_container(ps, c == '{');

    Json *node = json_alloc(ps->pool);
    if (!node)
        return NULL;
    if (c == '"') {
        node->kind = JS_STR;
        return json_string(ps, &node->str) == 0 ? node : NULL;
    }
    if (c == 't' && ps->end - ps->p >= 4 && !memcmp(ps->p, "true", 4)) {
        ps->p += 4;
        node->kind = JS_BOOL;
        node->boolean = 1;
        return node;
    }
    if (c == 'f' && ps->end - ps->p >= 5 && !memcmp(ps->p, "false", 5)) {
        ps->p += 5;
        node->kind = JS_BOOL;
        return node;
    }
    if (c == 'n' && ps->end - ps->p >= 4 && !memcmp(ps->p, "null", 4)) {
        ps->p += 4;
        node->kind = JS_NULL;
        return node;
    }
    if (c == '-' || (c >= '0' && c <= '9')) {
        /* strtoll, never strtod: a double holds 53 bits and this header can
         * carry int64 magnitudes. Parsing through a double silently rounds. */
        char tmp[32];
        size_t i = 0;
        while (ps->p < ps->end && i + 1 < sizeof tmp &&
               (*ps->p == '-' || *ps->p == '+' || (*ps->p >= '0' && *ps->p <= '9')))
            tmp[i++] = *ps->p++;
        tmp[i] = '\0';
        char *endp = NULL;
        node->kind = JS_NUM;
        node->num = strtoll(tmp, &endp, 10);
        return (endp && *endp == '\0' && i) ? node : NULL;
    }
    return NULL;
}

int json_parse(const char *text, size_t n, JsonDoc *out, char **err)
{
    JsonPool *pool = calloc(1, sizeof *pool);
    if (!pool)
        return fail(err, "out of memory");
    buf_init(&pool->strings);
    /* A decoded string is never longer than its escaped form, so reserving
     * the input length once means this buffer never moves. */
    if (buf_reserve(&pool->strings, n + 1) != 0) {
        free(pool);
        return fail(err, "out of memory");
    }
    JsonParser ps = { text, text + n, pool, err };
    Json *root = json_value(&ps);
    if (!root) {
        out->root = NULL;
        out->pool = pool;
        json_free(out);
        return fail(err, "archive header is not valid JSON");
    }
    out->root = root;
    out->pool = pool;
    return 0;
}

void json_free(JsonDoc *d)
{
    JsonPool *pool = d->pool;
    if (!pool)
        return;
    for (JsonBlock *b = pool->blocks; b;) {
        for (size_t i = 0; i < b->used; i++) {
            free(b->nodes[i].items);
            free(b->nodes[i].keys);
        }
        JsonBlock *next = b->next;
        free(b);
        b = next;
    }
    buf_free(&pool->strings);
    free(pool->stack);
    free(pool);
    d->pool = NULL;
    d->root = NULL;
}

const Json *json_get(const Json *obj, const char *key)
{
    if (!obj || obj->kind != JS_OBJ)
        return NULL;
    size_t klen = strlen(key);
    for (size_t i = 0; i < obj->count; i++)
        if (obj->keys[i].n == klen && !memcmp(obj->keys[i].s, key, klen))
            return obj->items[i];
    return NULL;
}

int json_emit_string(Buf *b, Str s)
{
    if (buf_putc(b, '"') != 0)
        return -1;
    for (size_t i = 0; i < s.n;) {
        unsigned char c = (unsigned char)s.s[i];
        if (c == '"' || c == '\\') {
            if (buf_putc(b, '\\') || buf_putc(b, (char)c))
                return -1;
            i++;
        } else if (c == '\n') {
            if (buf_puts(b, "\\n")) return -1; i++;
        } else if (c == '\r') {
            if (buf_puts(b, "\\r")) return -1; i++;
        } else if (c == '\t') {
            if (buf_puts(b, "\\t")) return -1; i++;
        } else if (c < 0x20) {
            if (buf_printf(b, "\\u%04x", c)) return -1; i++;
        } else if (c < 0x80) {
            if (buf_putc(b, (char)c)) return -1; i++;
        } else {
            /* ensure_ascii: decode the UTF-8 sequence and emit \uXXXX, with a
             * surrogate pair above the BMP, exactly as Python's json does. */
            unsigned long cp = 0;
            size_t need = 0;
            if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; need = 1; }
            else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; need = 2; }
            else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; need = 3; }
            else { cp = c; need = 0; }
            if (i + need >= s.n + 0 && need > 0 && i + need > s.n - 1) {
                /* truncated sequence: emit the raw byte escaped */
                if (buf_printf(b, "\\u%04x", c)) return -1;
                i++;
                continue;
            }
            for (size_t k = 1; k <= need; k++)
                cp = (cp << 6) | ((unsigned char)s.s[i + k] & 0x3F);
            i += need + 1;
            if (cp >= 0x10000) {
                unsigned long v = cp - 0x10000;
                if (buf_printf(b, "\\u%04x\\u%04x",
                               (unsigned)(0xD800 + (v >> 10)),
                               (unsigned)(0xDC00 + (v & 0x3FF))))
                    return -1;
            } else {
                if (buf_printf(b, "\\u%04x", (unsigned)cp))
                    return -1;
            }
        }
    }
    return buf_putc(b, '"');
}
