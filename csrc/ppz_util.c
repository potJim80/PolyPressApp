/* Buffers, the table, CSV, liblzma/libbz2 wrappers, and a small JSON parser.
 *
 * Everything here is infrastructure the codec sits on. The only parts with
 * opinions are the CSV reader (it must accept exactly what dtz accepts, or
 * the C and Python versions disagree about what the table even is) and the
 * lzma filter chain (it must be the exact chain fast.py uses, or the output
 * is not byte-identical).
 */

#include "ppz.h"

#include <bzlib.h>
#include <errno.h>
#include <lzma.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ bytes */

static void die_oom(void)
{
    fprintf(stderr, "polypress: out of memory\n");
    exit(1);
}

void buf_init(Buf *b) { b->data = NULL; b->len = b->cap = 0; }

void buf_free(Buf *b)
{
    free(b->data);
    buf_init(b);
}

void buf_need(Buf *b, size_t extra)
{
    if (b->len + extra <= b->cap) return;
    size_t cap = b->cap ? b->cap : 4096;
    while (cap < b->len + extra) {
        if (cap > (size_t)1 << 60) die_oom();
        cap *= 2;
    }
    uint8_t *p = realloc(b->data, cap);
    if (!p) die_oom();
    b->data = p;
    b->cap = cap;
}

void buf_put(Buf *b, const void *p, size_t n)
{
    if (!n) return;
    buf_need(b, n);
    memcpy(b->data + b->len, p, n);
    b->len += n;
}

void buf_putc(Buf *b, char c)
{
    buf_need(b, 1);
    b->data[b->len++] = (uint8_t)c;
}

/* ------------------------------------------------------------------ table */

void table_init(Table *t)
{
    memset(t, 0, sizeof(*t));
    buf_init(&t->arena);
}

void table_free(Table *t)
{
    for (size_t i = 0; i < t->ncols; i++) free(t->names[i]);
    free(t->names);
    free(t->cells);
    buf_free(&t->arena);
    memset(t, 0, sizeof(*t));
}

Str table_at(const Table *t, size_t row, size_t col)
{
    return t->cells[row * t->ncols + col];
}

/* ------------------------------------------------------------------- csv */

/* Cells are appended to the arena, which may realloc and move. So cells are
 * recorded as offsets during the parse and converted to pointers at the end.
 * Storing pointers directly and hoping the arena never grows is a bug that
 * only shows up on large files. */
typedef struct { size_t off, len; } Span;

int table_read_csv(Table *t, const char *path)
{
    FILE *f = strcmp(path, "-") ? fopen(path, "rb") : stdin;
    if (!f) return -1;

    Buf src;
    buf_init(&src);
    uint8_t chunk[65536];
    size_t got;
    while ((got = fread(chunk, 1, sizeof(chunk), f)) > 0) buf_put(&src, chunk, got);
    if (f != stdin) fclose(f);

    table_init(t);

    Span  *spans = NULL;
    size_t nspans = 0, spancap = 0;
    size_t fields_this_row = 0, width = 0, nrows = 0;
    int    in_quotes = 0, field_open = 0;
    size_t fstart = 0;

    Buf cell;                      /* the field being assembled */
    buf_init(&cell);

    #define PUSH_FIELD()                                                     \
        do {                                                                 \
            if (nspans == spancap) {                                         \
                spancap = spancap ? spancap * 2 : 1024;                      \
                Span *np = realloc(spans, spancap * sizeof(Span));           \
                if (!np) die_oom();                                          \
                spans = np;                                                  \
            }                                                                \
            fstart = t->arena.len;                                           \
            buf_put(&t->arena, cell.data, cell.len);                         \
            spans[nspans].off = fstart;                                      \
            spans[nspans].len = cell.len;                                    \
            nspans++;                                                        \
            cell.len = 0;                                                    \
            fields_this_row++;                                               \
            field_open = 0;                                                  \
        } while (0)

    #define END_ROW()                                                        \
        do {                                                                 \
            if (width == 0) width = fields_this_row;                         \
            else {                                                           \
                while (fields_this_row < width) {                            \
                    /* ragged: pad, same rule as dtz */                      \
                    if (nspans == spancap) {                                 \
                        spancap = spancap ? spancap * 2 : 1024;              \
                        Span *np = realloc(spans, spancap * sizeof(Span));   \
                        if (!np) die_oom();                                  \
                        spans = np;                                          \
                    }                                                        \
                    spans[nspans].off = 0;                                   \
                    spans[nspans].len = 0;                                   \
                    nspans++; fields_this_row++;                             \
                }                                                            \
                while (fields_this_row > width) { nspans--; fields_this_row--; }\
            }                                                                \
            nrows++;                                                         \
            fields_this_row = 0;                                             \
        } while (0)

    for (size_t i = 0; i < src.len; i++) {
        char c = (char)src.data[i];
        if (in_quotes) {
            if (c == '"') {
                if (i + 1 < src.len && src.data[i + 1] == '"') {
                    buf_putc(&cell, '"');
                    i++;
                } else {
                    in_quotes = 0;
                }
            } else {
                buf_putc(&cell, c);
            }
            continue;
        }
        if (c == '"' && !field_open) { in_quotes = 1; field_open = 1; continue; }
        if (c == ',') { PUSH_FIELD(); continue; }
        if (c == '\r') {
            if (i + 1 < src.len && src.data[i + 1] == '\n') continue;
            PUSH_FIELD(); END_ROW(); continue;
        }
        if (c == '\n') { PUSH_FIELD(); END_ROW(); continue; }
        buf_putc(&cell, c);
        field_open = 1;
    }
    /* a final line with no terminator still counts */
    if (cell.len || field_open || fields_this_row) { PUSH_FIELD(); END_ROW(); }

    buf_free(&cell);
    buf_free(&src);

    if (nrows == 0 || width == 0) { free(spans); return 0; }

    t->ncols = width;
    t->nrows = nrows - 1;                       /* first row is the header */
    t->names = calloc(width, sizeof(char *));
    if (!t->names) die_oom();
    for (size_t j = 0; j < width; j++) {
        Span s = spans[j];
        char *nm = malloc(s.len + 1);
        if (!nm) die_oom();
        memcpy(nm, (char *)t->arena.data + s.off, s.len);
        nm[s.len] = 0;
        t->names[j] = nm;
    }

    t->cells = calloc(t->nrows * width ? t->nrows * width : 1, sizeof(Str));
    if (!t->cells) die_oom();
    for (size_t k = 0; k < t->nrows * width; k++) {
        Span s = spans[width + k];
        t->cells[k].p = (const char *)t->arena.data + s.off;
        t->cells[k].n = s.len;
    }
    free(spans);
    return 0;
}

static int csv_needs_quotes(Str s)
{
    for (size_t i = 0; i < s.n; i++) {
        char c = s.p[i];
        if (c == ',' || c == '"' || c == '\n' || c == '\r') return 1;
    }
    return 0;
}

static void csv_write_field(Buf *out, Str s)
{
    if (!csv_needs_quotes(s)) { buf_put(out, s.p, s.n); return; }
    buf_putc(out, '"');
    for (size_t i = 0; i < s.n; i++) {
        if (s.p[i] == '"') buf_putc(out, '"');
        buf_putc(out, s.p[i]);
    }
    buf_putc(out, '"');
}

int table_write_csv_buf(const Table *t, Buf *out)
{
    for (size_t j = 0; j < t->ncols; j++) {
        if (j) buf_putc(out, ',');
        Str s = { t->names[j], strlen(t->names[j]) };
        csv_write_field(out, s);
    }
    buf_putc(out, '\n');
    for (size_t i = 0; i < t->nrows; i++) {
        for (size_t j = 0; j < t->ncols; j++) {
            if (j) buf_putc(out, ',');
            csv_write_field(out, table_at(t, i, j));
        }
        buf_putc(out, '\n');
    }
    return 0;
}

int table_write_csv(const Table *t, const char *path)
{
    Buf out;
    buf_init(&out);
    table_write_csv_buf(t, &out);
    FILE *f = strcmp(path, "-") ? fopen(path, "wb") : stdout;
    if (!f) { buf_free(&out); return -1; }
    /* Capture the length before freeing: buf_free zeroes it, so comparing
     * against out.len afterwards reported failure on every successful write. */
    size_t want = out.len;
    size_t w = fwrite(out.data, 1, want, f);
    if (f != stdout) fclose(f);
    buf_free(&out);
    return w == want ? 0 : -1;
}

/* ------------------------------------------------------------------- lzma */

int ppz_lzma_compress(const uint8_t *in, size_t n, Buf *out)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME)) return -1;
    lzma_filter filters[2] = {
        { LZMA_FILTER_LZMA2, &opt },
        { LZMA_VLI_UNKNOWN, NULL },
    };
    size_t cap = n + n / 2 + 4096;
    buf_free(out);
    buf_need(out, cap);
    size_t pos = 0;
    if (lzma_raw_buffer_encode(filters, NULL, in, n, out->data, &pos, cap))
        return -1;
    out->len = pos;
    return 0;
}

/* A decoder reads files other people made, so both of these treat their input
 * as hostile. The earlier versions grew a buffer and retried on ANY failure,
 * which meant a corrupt archive -- which never decodes at any size -- doubled
 * its way to a terabyte allocation. Fuzzing found it on 132 of 199 mutated
 * inputs. Two changes fix it for good: decode incrementally so the output
 * buffer only grows when bytes were actually produced, and stop the moment
 * the library reports corruption rather than assuming more room would help.
 *
 * PPZ_MAX_PLAIN also bounds a decompression bomb -- a small archive that
 * legitimately decodes to something enormous. 4 GB is far above any table
 * this codec is meant for and far below "swap the machine".
 */
#define PPZ_MAX_PLAIN ((size_t)4 << 30)

int ppz_lzma_decompress(const uint8_t *in, size_t n, Buf *out)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME)) return -1;
    lzma_filter filters[2] = {
        { LZMA_FILTER_LZMA2, &opt },
        { LZMA_VLI_UNKNOWN, NULL },
    };
    lzma_stream strm = LZMA_STREAM_INIT;
    if (lzma_raw_decoder(&strm, filters) != LZMA_OK) return -1;

    buf_free(out);
    size_t chunk = n * 2 + 65536;
    if (chunk > (size_t)8 << 20) chunk = (size_t)8 << 20;

    strm.next_in = in;
    strm.avail_in = n;
    int rc = -1;
    for (;;) {
        if (out->len + chunk > PPZ_MAX_PLAIN) goto done;
        buf_need(out, chunk);
        strm.next_out = out->data + out->len;
        strm.avail_out = chunk;
        size_t before = strm.avail_out;
        lzma_ret r = lzma_code(&strm, strm.avail_in ? LZMA_RUN : LZMA_FINISH);
        out->len += before - strm.avail_out;
        if (r == LZMA_STREAM_END) { rc = 0; goto done; }
        if (r != LZMA_OK) goto done;          /* corrupt: do not grow, stop */
        if (strm.avail_in == 0 && before == strm.avail_out) {
            /* input exhausted and nothing more is coming out */
            rc = 0;
            goto done;
        }
    }
done:
    lzma_end(&strm);
    if (rc) buf_free(out);
    return rc;
}

int ppz_bz2_decompress(const uint8_t *in, size_t n, Buf *out)
{
    bz_stream strm;
    memset(&strm, 0, sizeof(strm));
    if (BZ2_bzDecompressInit(&strm, 0, 0) != BZ_OK) return -1;

    buf_free(out);
    size_t chunk = n * 2 + 65536;
    if (chunk > (size_t)8 << 20) chunk = (size_t)8 << 20;

    strm.next_in = (char *)(uintptr_t)in;
    strm.avail_in = (unsigned int)n;
    int rc = -1;
    for (;;) {
        if (out->len + chunk > PPZ_MAX_PLAIN) goto done;
        buf_need(out, chunk);
        strm.next_out = (char *)out->data + out->len;
        strm.avail_out = (unsigned int)chunk;
        unsigned int before = strm.avail_out;
        int r = BZ2_bzDecompress(&strm);
        out->len += before - strm.avail_out;
        if (r == BZ_STREAM_END) { rc = 0; goto done; }
        if (r != BZ_OK) goto done;            /* corrupt: stop */
        if (strm.avail_in == 0 && before == strm.avail_out) goto done;
    }
done:
    BZ2_bzDecompressEnd(&strm);
    if (rc) buf_free(out);
    return rc;
}

/* ------------------------------------------------------------------- json */

typedef struct {
    const char *p;
    size_t      n, i;
    int         bad;
} Jp;

static void jp_ws(Jp *j)
{
    while (j->i < j->n) {
        char c = j->p[j->i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') j->i++;
        else break;
    }
}

static Js *js_new(JsKind k)
{
    Js *j = calloc(1, sizeof(Js));
    if (!j) die_oom();
    j->kind = k;
    return j;
}

static Js *jp_value(Jp *j);

static char *jp_string_raw(Jp *j)
{
    if (j->i >= j->n || j->p[j->i] != '"') { j->bad = 1; return NULL; }
    j->i++;
    Buf b;
    buf_init(&b);
    while (j->i < j->n && j->p[j->i] != '"') {
        char c = j->p[j->i++];
        if (c != '\\') { buf_putc(&b, c); continue; }
        if (j->i >= j->n) { j->bad = 1; break; }
        char e = j->p[j->i++];
        switch (e) {
        case 'n': buf_putc(&b, '\n'); break;
        case 't': buf_putc(&b, '\t'); break;
        case 'r': buf_putc(&b, '\r'); break;
        case 'b': buf_putc(&b, '\b'); break;
        case 'f': buf_putc(&b, '\f'); break;
        case '/': buf_putc(&b, '/');  break;
        case '"': buf_putc(&b, '"');  break;
        case '\\': buf_putc(&b, '\\'); break;
        case 'u': {
            if (j->i + 4 > j->n) { j->bad = 1; break; }
            unsigned cp = 0;
            for (int k = 0; k < 4; k++) {
                char h = j->p[j->i + k];
                cp <<= 4;
                if (h >= '0' && h <= '9') cp |= (unsigned)(h - '0');
                else if (h >= 'a' && h <= 'f') cp |= (unsigned)(h - 'a' + 10);
                else if (h >= 'A' && h <= 'F') cp |= (unsigned)(h - 'A' + 10);
                else { j->bad = 1; break; }
            }
            j->i += 4;
            /* surrogate pair -- Python's json escapes non-BMP this way */
            if (cp >= 0xD800 && cp <= 0xDBFF && j->i + 6 <= j->n &&
                j->p[j->i] == '\\' && j->p[j->i + 1] == 'u') {
                unsigned lo = 0;
                for (int k = 0; k < 4; k++) {
                    char h = j->p[j->i + 2 + k];
                    lo <<= 4;
                    if (h >= '0' && h <= '9') lo |= (unsigned)(h - '0');
                    else if (h >= 'a' && h <= 'f') lo |= (unsigned)(h - 'a' + 10);
                    else if (h >= 'A' && h <= 'F') lo |= (unsigned)(h - 'A' + 10);
                }
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    j->i += 6;
                }
            }
            /* encode as UTF-8 */
            if (cp < 0x80) buf_putc(&b, (char)cp);
            else if (cp < 0x800) {
                buf_putc(&b, (char)(0xC0 | (cp >> 6)));
                buf_putc(&b, (char)(0x80 | (cp & 0x3F)));
            } else if (cp < 0x10000) {
                buf_putc(&b, (char)(0xE0 | (cp >> 12)));
                buf_putc(&b, (char)(0x80 | ((cp >> 6) & 0x3F)));
                buf_putc(&b, (char)(0x80 | (cp & 0x3F)));
            } else {
                buf_putc(&b, (char)(0xF0 | (cp >> 18)));
                buf_putc(&b, (char)(0x80 | ((cp >> 12) & 0x3F)));
                buf_putc(&b, (char)(0x80 | ((cp >> 6) & 0x3F)));
                buf_putc(&b, (char)(0x80 | (cp & 0x3F)));
            }
            break;
        }
        default: j->bad = 1; break;
        }
    }
    if (j->i >= j->n) { j->bad = 1; buf_free(&b); return NULL; }
    j->i++;                                   /* closing quote */
    buf_putc(&b, 0);
    return (char *)b.data;
}

static Js *jp_value(Jp *j)
{
    jp_ws(j);
    if (j->i >= j->n) { j->bad = 1; return NULL; }
    char c = j->p[j->i];

    if (c == '"') {
        Js *v = js_new(JS_STR);
        v->str = jp_string_raw(j);
        return v;
    }
    if (c == '[') {
        j->i++;
        Js *v = js_new(JS_ARR);
        size_t cap = 0;
        jp_ws(j);
        if (j->i < j->n && j->p[j->i] == ']') { j->i++; return v; }
        for (;;) {
            if (v->count == cap) {
                cap = cap ? cap * 2 : 8;
                Js *np = realloc(v->items, cap * sizeof(Js));
                if (!np) die_oom();
                v->items = np;
            }
            Js *e = jp_value(j);
            if (j->bad) return v;
            v->items[v->count++] = *e;
            free(e);
            jp_ws(j);
            if (j->i < j->n && j->p[j->i] == ',') { j->i++; continue; }
            if (j->i < j->n && j->p[j->i] == ']') { j->i++; break; }
            j->bad = 1;
            break;
        }
        return v;
    }
    if (c == '{') {
        j->i++;
        Js *v = js_new(JS_OBJ);
        size_t cap = 0;
        jp_ws(j);
        if (j->i < j->n && j->p[j->i] == '}') { j->i++; return v; }
        for (;;) {
            if (v->count == cap) {
                cap = cap ? cap * 2 : 8;
                Js *np = realloc(v->items, cap * sizeof(Js));
                if (!np) die_oom();
                v->items = np;
                char **nk = realloc(v->keys, cap * sizeof(char *));
                if (!nk) die_oom();
                v->keys = nk;
            }
            jp_ws(j);
            v->keys[v->count] = jp_string_raw(j);
            if (j->bad) return v;
            jp_ws(j);
            if (j->i >= j->n || j->p[j->i] != ':') { j->bad = 1; return v; }
            j->i++;
            Js *e = jp_value(j);
            if (j->bad) return v;
            v->items[v->count] = *e;
            free(e);
            v->count++;
            jp_ws(j);
            if (j->i < j->n && j->p[j->i] == ',') { j->i++; continue; }
            if (j->i < j->n && j->p[j->i] == '}') { j->i++; break; }
            j->bad = 1;
            break;
        }
        return v;
    }
    if (c == 't' && j->i + 4 <= j->n && !memcmp(j->p + j->i, "true", 4)) {
        j->i += 4;
        Js *v = js_new(JS_BOOL);
        v->boolean = 1;
        return v;
    }
    if (c == 'f' && j->i + 5 <= j->n && !memcmp(j->p + j->i, "false", 5)) {
        j->i += 5;
        return js_new(JS_BOOL);
    }
    if (c == 'n' && j->i + 4 <= j->n && !memcmp(j->p + j->i, "null", 4)) {
        j->i += 4;
        return js_new(JS_NULL);
    }
    {
        /* Integers are kept exactly. A double carries 53 bits of mantissa and
         * this metadata holds int64 warm-start values well above that, so
         * going through a double silently rounds them -- which decodes to a
         * wrong number rather than an error. Only fall back to strtod for
         * tokens that are genuinely not integers. */
        const char *start = j->p + j->i;
        const char *scan = start;
        const char *end_lim = j->p + j->n;
        if (scan < end_lim && (*scan == '-' || *scan == '+')) scan++;
        int is_int = scan < end_lim && *scan >= '0' && *scan <= '9';
        while (scan < end_lim && *scan >= '0' && *scan <= '9') scan++;
        if (scan < end_lim && (*scan == '.' || *scan == 'e' || *scan == 'E'))
            is_int = 0;

        char *end = NULL;
        Js *v = js_new(JS_NUM);
        if (is_int) {
            errno = 0;
            long long iv = strtoll(start, &end, 10);
            if (end != start && errno != ERANGE) {
                j->i = (size_t)(end - j->p);
                v->inum = (int64_t)iv;
                v->is_int = 1;
                v->num = (double)iv;
                return v;
            }
        }
        double d = strtod(start, &end);
        if (!end || end == start) { j->bad = 1; v->kind = JS_NULL; return v; }
        j->i = (size_t)(end - j->p);
        v->num = d;
        return v;
    }
}

Js *js_parse(const char *text, size_t len)
{
    Jp j = { text, len, 0, 0 };
    Js *v = jp_value(&j);
    if (j.bad) { js_free(v); return NULL; }
    return v;
}

static void js_clear(Js *j)
{
    if (!j) return;
    free(j->str);
    for (size_t i = 0; i < j->count; i++) {
        js_clear(&j->items[i]);
        if (j->keys) free(j->keys[i]);
    }
    free(j->items);
    free(j->keys);
}

void js_free(Js *j)
{
    if (!j) return;
    js_clear(j);
    free(j);
}

const Js *js_get(const Js *obj, const char *key)
{
    if (!obj || obj->kind != JS_OBJ) return NULL;
    for (size_t i = 0; i < obj->count; i++)
        if (obj->keys[i] && !strcmp(obj->keys[i], key)) return &obj->items[i];
    return NULL;
}

long js_int(const Js *j, long fallback)
{
    if (!j) return fallback;
    if (j->kind == JS_NUM) return j->is_int ? (long)j->inum : (long)j->num;
    if (j->kind == JS_BOOL) return j->boolean;
    return fallback;
}

int64_t js_i64(const Js *j, int64_t fallback)
{
    if (!j) return fallback;
    if (j->kind == JS_NUM) return j->is_int ? j->inum : (int64_t)j->num;
    if (j->kind == JS_BOOL) return j->boolean;
    return fallback;
}
