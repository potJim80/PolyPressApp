/* Buffers, the table, the liblzma wrappers, a small JSON parser, and the
 * numbers derived columns are made of. Infrastructure the codec sits on. */

#include "ppz.h"

#include <errno.h>
#include <limits.h>
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

/* Make room for `extra` more bytes after b->len. Capacity starts at 4 KiB
 * and doubles, so a long run of appends costs amortized O(1) per byte; the
 * 2^60 ceiling stops the doubling before it can wrap size_t. */
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

/* ------------------------------------------------------ partial outputs */

#include <signal.h>
#include <unistd.h>

#define TMP_SLOTS 8
static char *volatile tmp_paths[TMP_SLOTS];

void ppz_tmp_register(const char *path)
{
    for (int i = 0; i < TMP_SLOTS; i++)
        if (!tmp_paths[i]) { tmp_paths[i] = strdup(path); return; }
}

void ppz_tmp_forget(const char *path)
{
    for (int i = 0; i < TMP_SLOTS; i++)
        if (tmp_paths[i] && !strcmp(tmp_paths[i], path)) {
            char *p = tmp_paths[i];
            tmp_paths[i] = NULL;
            free(p);
        }
}

/* unlink and _exit are async-signal-safe; nothing else is called here */
static void on_signal(int sig)
{
    for (int i = 0; i < TMP_SLOTS; i++)
        if (tmp_paths[i]) unlink(tmp_paths[i]);
    signal(sig, SIG_DFL);
    raise(sig);
}

void ppz_cleanup_on_signals(void)
{
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGHUP, on_signal);
    signal(SIGPIPE, on_signal);           /* a reader that went away */
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

/* ------------------------------------------------------------------- lzma */

/* Raw LZMA2 at preset 9e, streamed a megabyte at a time. Each run holds one
 * of the process-wide slots (ppz_thread.c), so however many blocks and
 * streams are being compressed at once, only ppz_workers() xz -9e working
 * sets exist at the same moment. */
#define CHUNK ((size_t)1 << 20)

int ppz_lzma_compress(const uint8_t *in, size_t n, Buf *out)
{
    return ppz_lzma_compress_as(in, n, out, PPZ_XZ_PLAIN);
}

/* LZMA's three layout settings per stream: literal context bits (lc),
 * literal and match position bits (lp, pb). The decoder needs none of
 * them -- LZMA2 carries them in its chunk headers -- so they are free to
 * choose. Measured
 * 2026-10-01 over the 39 suite tables' raw streams (xz -9e, total bytes):
 *   numbers  lc3 lp0 pb2 (xz default) 11,482,660 -> lc0 lp2 pb2 11,228,268 (-2.2%)
 *   text     lc3 lp0 pb2              10,189,881 -> lc4 lp0 pb1 10,177,320 (-0.1%)
 * The packed numbers are fixed-width, so position (lp/pb) predicts better
 * than the previous byte (lc). Best per table instead would add only 0.6%
 * on the numbers and needs a trial encode; splitting dictionary ids from
 * packed integers into two streams, 0.4%. Neither was worth it. */
int ppz_lzma_compress_as(const uint8_t *in, size_t n, Buf *out, PpzXz kind)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME)) return -1;
    if (kind == PPZ_XZ_INTS) { opt.lc = 0; opt.lp = 2; opt.pb = 2; }
    else if (kind == PPZ_XZ_TEXT) { opt.lc = 4; opt.lp = 0; opt.pb = 1; }
    lzma_filter filters[2] = {
        { LZMA_FILTER_LZMA2, &opt },
        { LZMA_VLI_UNKNOWN, NULL },
    };
    buf_free(out);
    ppz_slot_take();
    lzma_stream strm = LZMA_STREAM_INIT;
    int rc = -1;
    if (lzma_raw_encoder(&strm, filters) != LZMA_OK) goto done;
    size_t fed = 0;
    for (;;) {
        size_t piece = n - fed < CHUNK ? n - fed : CHUNK;
        strm.next_in = in + fed;
        strm.avail_in = piece;
        fed += piece;
        lzma_action act = fed < n ? LZMA_RUN : LZMA_FINISH;
        for (;;) {
            buf_need(out, CHUNK);
            strm.next_out = out->data + out->len;
            strm.avail_out = out->cap - out->len;
            size_t before = strm.avail_out;
            lzma_ret r = lzma_code(&strm, act);
            out->len += before - strm.avail_out;
            if (r == LZMA_STREAM_END) { rc = 0; goto done; }
            if (r != LZMA_OK) goto done;
            if (act == LZMA_RUN && strm.avail_in == 0) break;
        }
    }
done:
    lzma_end(&strm);
    ppz_slot_give();
    if (rc) buf_free(out);
    return rc;
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

size_t ppz_lzma_probe_len(const uint8_t *in, size_t n)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 1)) return (size_t)-1;
    lzma_filter filters[2] = {
        { LZMA_FILTER_LZMA2, &opt },
        { LZMA_VLI_UNKNOWN, NULL },
    };
    size_t cap = n + n / 2 + 4096;
    uint8_t *tmp = malloc(cap);
    if (!tmp) return (size_t)-1;
    size_t pos = 0;
    lzma_ret r = lzma_raw_buffer_encode(filters, NULL, in, n, tmp, &pos, cap);
    free(tmp);
    return r == LZMA_OK ? pos : (size_t)-1;
}

/* The same raw LZMA2 9e as ppz_lzma_compress_as, for input that does not
 * fit in memory: `src` fills a buffer (returns bytes, 0 at the end, or
 * (size_t)-1 on error) and every piece of output goes to `sink`. */
int ppz_xz_stream(PpzXz kind, size_t (*src)(uint8_t *, size_t, void *), void *sctx,
                  int (*sink)(const uint8_t *, size_t, void *), void *kctx, uint64_t *out_len)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME)) return -1;
    if (kind == PPZ_XZ_INTS) { opt.lc = 0; opt.lp = 2; opt.pb = 2; }
    else if (kind == PPZ_XZ_TEXT) { opt.lc = 4; opt.lp = 0; opt.pb = 1; }
    lzma_filter filters[2] = { { LZMA_FILTER_LZMA2, &opt }, { LZMA_VLI_UNKNOWN, NULL } };
    uint8_t *in = malloc(CHUNK), *out = malloc(CHUNK);
    lzma_stream strm = LZMA_STREAM_INIT;
    int rc = -1;
    uint64_t total = 0;
    ppz_slot_take();
    if (!in || !out || lzma_raw_encoder(&strm, filters) != LZMA_OK) goto done;
    lzma_action act = LZMA_RUN;
    for (;;) {
        if (strm.avail_in == 0 && act == LZMA_RUN) {
            size_t got = src(in, CHUNK, sctx);
            if (got == (size_t)-1) goto done;
            strm.next_in = in;
            strm.avail_in = got;
            if (!got) act = LZMA_FINISH;
        }
        strm.next_out = out;
        strm.avail_out = CHUNK;
        lzma_ret r = lzma_code(&strm, act);
        size_t made = CHUNK - strm.avail_out;
        if (made && sink(out, made, kctx)) goto done;
        total += made;
        if (r == LZMA_STREAM_END) { rc = 0; break; }
        if (r != LZMA_OK) goto done;
    }
done:
    lzma_end(&strm);
    ppz_slot_give();
    free(in); free(out);
    if (out_len) *out_len = total;
    return rc;
}

/* Decode a raw stream piece by piece into `sink`, refusing -- as
 * ppz_lzma_decompress does -- corrupt or cut-short input, and output past
 * `limit` bytes. */
int ppz_xz_unstream(const uint8_t *in, size_t n, uint64_t limit,
                    int (*sink)(const uint8_t *, size_t, void *), void *kctx, uint64_t *got)
{
    lzma_options_lzma opt;
    if (lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME)) return -1;
    lzma_filter filters[2] = { { LZMA_FILTER_LZMA2, &opt }, { LZMA_VLI_UNKNOWN, NULL } };
    lzma_stream strm = LZMA_STREAM_INIT;
    uint8_t *out = malloc(CHUNK);
    uint64_t total = 0;
    int rc = -1;
    if (!out || lzma_raw_decoder(&strm, filters) != LZMA_OK) goto done;
    strm.next_in = in;
    strm.avail_in = n;
    for (;;) {
        strm.next_out = out;
        strm.avail_out = CHUNK;
        lzma_ret r = lzma_code(&strm, strm.avail_in ? LZMA_RUN : LZMA_FINISH);
        size_t made = CHUNK - strm.avail_out;
        if (made) {
            if (total + made > limit) goto done;
            if (sink(out, made, kctx)) goto done;
            total += made;
        }
        if (r == LZMA_STREAM_END) { rc = 0; break; }
        if (r != LZMA_OK) goto done;
        if (strm.avail_in == 0 && !made) goto done;       /* cut short */
    }
done:
    lzma_end(&strm);
    free(out);
    if (got) *got = total;
    return rc;
}

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
            /* Input exhausted and nothing more coming out, but no end marker:
             * the stream was cut short. Every stream this program writes ends
             * with one, so this is a truncated file -- and treating it as
             * complete restored half a table and exited 0. */
            goto done;
        }
    }
done:
    lzma_end(&strm);
    if (rc) buf_free(out);
    return rc;
}

/* ------------------------------------------------------------------- json */

typedef struct {
    const char *p;
    size_t      n, i;
    int         bad;
    int         depth;
} Jp;

/* Nesting deeper than this is refused. The parser recurses, and 100,000
 * '[' overflowed the stack -- from a .json handed to `compress`, or from the
 * metadata of a hostile archive, which is JSON too. Real metadata is three
 * levels deep and real table JSON a handful. */
#define JS_MAX_DEPTH 512

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

static char *jp_string_raw(Jp *j, size_t *len_out)
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
    if (len_out) *len_out = b.len;            /* a \u0000 is a real byte */
    buf_putc(&b, 0);
    return (char *)b.data;
}

static Js *jp_value_inner(Jp *j);

static void js_token(Js *v, const char *start, const char *end)
{
    size_t tl = (size_t)(end - start);
    v->str = malloc(tl + 1);
    if (!v->str) die_oom();
    memcpy(v->str, start, tl);
    v->str[tl] = 0;
    v->len = tl;
}

static Js *jp_value(Jp *j)
{
    if (++j->depth > JS_MAX_DEPTH) { j->bad = 1; j->depth--; return NULL; }
    Js *v = jp_value_inner(j);
    j->depth--;
    return v;
}

static Js *jp_value_inner(Jp *j)
{
    jp_ws(j);
    if (j->i >= j->n) { j->bad = 1; return NULL; }
    char c = j->p[j->i];

    if (c == '"') {
        Js *v = js_new(JS_STR);
        v->str = jp_string_raw(j, &v->len);
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
            v->keys[v->count] = jp_string_raw(j, NULL);
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
        /* Copied out first: strtoll and strtod read until something stops
         * them, and this text is a slice of a buffer with no terminator, so
         * a number at its very end would be parsed from whatever memory lies
         * beyond. A number token is short; anything longer is refused. */
        char tok[80];
        size_t tl = 0;
        while (j->i + tl < j->n && tl < sizeof(tok) - 1) {
            char ch = j->p[j->i + tl];
            if ((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'z') ||
                (ch >= 'A' && ch <= 'Z') || ch == '+' || ch == '-' || ch == '.')
                tok[tl++] = ch;
            else break;
        }
        tok[tl] = 0;
        Js *v = js_new(JS_NUM);
        if (tl == 0 || tl == sizeof(tok) - 1) { j->bad = 1; v->kind = JS_NULL; return v; }

        const char *scan = tok;
        if (*scan == '-' || *scan == '+') scan++;
        int is_int = *scan >= '0' && *scan <= '9';
        while (*scan >= '0' && *scan <= '9') scan++;
        if (*scan == '.' || *scan == 'e' || *scan == 'E') is_int = 0;

        /* The token text is recorded from wherever the parse actually
         * stopped -- so "1.50" stays "1.50", and NaN or Infinity (which
         * strtod accepts, as Python's json does) come out as written. */
        char *end = NULL;
        if (is_int) {
            errno = 0;
            long long iv = strtoll(tok, &end, 10);
            if (end != tok && errno != ERANGE) {
                v->inum = (int64_t)iv;
                v->is_int = 1;
                v->num = (double)iv;
                js_token(v, tok, end);
                j->i += (size_t)(end - tok);
                return v;
            }
        }
        double d = strtod(tok, &end);
        if (!end || end == tok) { j->bad = 1; v->kind = JS_NULL; return v; }
        v->num = d;
        js_token(v, tok, end);
        j->i += (size_t)(end - tok);
        return v;
    }
}

Js *js_parse(const char *text, size_t len)
{
    size_t used;
    return js_parse_prefix(text, len, &used);
}

/* One value from the front of `text`; *used says where it ended, so a caller
 * can insist nothing but whitespace follows. */
Js *js_parse_prefix(const char *text, size_t len, size_t *used)
{
    Jp j = { text, len, 0, 0, 0 };
    Js *v = jp_value(&j);
    if (j.bad) { js_free(v); return NULL; }
    jp_ws(&j);
    *used = j.i;
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

/* ------------------------------------------------------ derived columns */

static int drv_digit(char c) { return c >= '0' && c <= '9'; }

size_t drv_token(const char *s, size_t n, size_t i)
{
    size_t j = i;
    if (j < n && s[j] == '-' && j + 1 < n && drv_digit(s[j + 1])) j++;
    if (j >= n || !drv_digit(s[j])) return 0;
    while (j < n && drv_digit(s[j])) j++;
    if (j + 1 < n && s[j] == '.' && drv_digit(s[j + 1])) {
        j++;
        while (j < n && drv_digit(s[j])) j++;
    }
    return j - i;
}

int drv_is_number(const char *s, size_t n)
{
    return n && drv_token(s, n, 0) == n;
}

int drv_decimals(const char *s, size_t n)
{
    const char *dot = memchr(s, '.', n);
    return dot ? (int)(n - (size_t)(dot - s) - 1) : 0;
}

size_t drv_round(const char *v, size_t n, int d, char *out)
{
    if (d < 0 || n > DRV_MAX_TOK || !drv_is_number(v, n)) return 0;
    int neg = v[0] == '-';
    const char *p = v + neg;
    size_t m = n - (size_t)neg;
    const char *dot = memchr(p, '.', m);
    if (!dot) return 0;
    size_t ni = (size_t)(dot - p), nf = m - ni - 1;
    if ((size_t)d >= nf) return 0;

    /* the kept digits, then the first dropped one decides */
    char dig[DRV_MAX_TOK + 1];
    size_t L = 0;
    for (size_t i = 0; i < ni; i++) dig[L++] = p[i];
    for (int i = 0; i < d; i++) dig[L++] = dot[1 + i];
    char next = dot[1 + d];
    int rest = 0;
    for (size_t i = (size_t)d + 1; i < nf; i++) if (dot[1 + i] != '0') { rest = 1; break; }
    int up = next > '5' || (next == '5' && (rest || ((dig[L - 1] - '0') & 1)));
    int carry = 0;
    if (up) {
        size_t i = L;
        carry = 1;
        while (carry && i > 0) {
            i--;
            if (dig[i] == '9') dig[i] = '0';
            else { dig[i]++; carry = 0; }
        }
    }
    size_t o = 0;
    if (neg) out[o++] = '-';
    if (carry) out[o++] = '1';
    for (size_t i = 0; i < ni; i++) out[o++] = dig[i];
    if (d) {
        out[o++] = '.';
        for (int i = 0; i < d; i++) out[o++] = dig[ni + (size_t)i];
    }
    return o;
}
