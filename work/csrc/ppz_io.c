/* Tables in and out: delimited text, JSON, JSON Lines, and text encodings.
 *
 * The codec only ever sees a Table -- a header and rows of exact cell
 * strings. This file decides what table a file IS, and that decision is the
 * one place data can be lost before any check can see it. Twice in this
 * project a reader damaged the table first and the round-trip verification,
 * comparing the damaged table with its own decoding, passed:
 *
 *   errors="replace"   every undecodable byte became U+FFFD before the table
 *                      existed; a latin-1 file lost every accent and
 *                      "verified".
 *   row[:width]        rows wider than the header were silently cut; Romeo
 *                      and Juliet as raw text lost the tail of every line
 *                      with a comma in it.
 *
 * A check downstream of the damage cannot see the damage. So the rules here
 * are refusals, not repairs:
 *
 *   - Text is decoded strictly. A byte-order mark is honoured; anything else
 *     unmarked must be valid UTF-8 or it is refused with the byte and offset
 *     named. --encoding is the only override, and there is no guessing.
 *   - A row shorter than the header is padded with empty cells (CSV writers
 *     drop trailing empties all the time; nothing is lost). A row LONGER
 *     than the header is trimmed only when every extra cell is empty, and
 *     refused otherwise.
 *
 * CSV follows the common dialect: a quote opens a field only at its start,
 * "" inside quotes is a quote, CR, LF and CRLF all end a row outside quotes
 * and are kept inside them. The streaming reader hands out the file a block
 * of rows at a time, which is what lets `stream-compress` hold one block in
 * memory instead of the whole file.
 */

#include "ppz.h"

#include <errno.h>
#include <iconv.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define READ_CHUNK ((size_t)1 << 20)

static void seterr(char *err, size_t cap, const char *fmt, ...)
{
    if (!err || !cap) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
}

static void oom(void)
{
    fprintf(stderr, "polypress: out of memory\n");
    exit(1);
}

static const char *base_name(const char *path)
{
    const char *s = strrchr(path, '/');
    return s ? s + 1 : path;
}

/* Lower-cased extension including the dot, or "" -- ".CSV" is ".csv". */
static void ext_of(const char *path, char *out, size_t cap)
{
    out[0] = 0;
    const char *b = base_name(path);
    const char *dot = strrchr(b, '.');
    if (!dot || dot == b) return;
    size_t i = 0;
    for (; dot[i] && i + 1 < cap; i++) {
        char c = dot[i];
        out[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    out[i] = 0;
}

const char *ppz_format_of(const char *path)
{
    if (!strcmp(path, "-")) return "csv";
    char e[16];
    ext_of(path, e, sizeof(e));
    if (!strcmp(e, ".tsv") || !strcmp(e, ".tab")) return "tsv";
    if (!strcmp(e, ".psv")) return "psv";
    if (!strcmp(e, ".json")) return "json";
    if (!strcmp(e, ".jsonl") || !strcmp(e, ".ndjson")) return "jsonl";
    if (!strcmp(e, ".parquet") || !strcmp(e, ".pq")) return "parquet";
    if (!strcmp(e, ".csv")) return "csv";
    return "text";                   /* .txt, .dat, anything: delimiter sniffed */
}

/* ================================================================ source */

/* Bytes in, validated UTF-8 out. */
typedef struct {
    FILE     *f;
    int       own;
    int       conv;          /* 1: through iconv, 0: validate UTF-8 in place */
    iconv_t   cd;
    uint8_t  *raw;
    size_t    rlen, rpos, rcap;
    uint64_t  roff;          /* file offset of raw[0] */
    int       eof;           /* nothing more to read from f */
    char      name[256];
    char      enc[64];       /* for messages: "UTF-8", "LATIN-1", ... */
} Src;

static int src_fill(Src *s, size_t want)
{
    if (s->eof) return 0;
    if (s->rpos) {
        memmove(s->raw, s->raw + s->rpos, s->rlen - s->rpos);
        s->roff += s->rpos;
        s->rlen -= s->rpos;
        s->rpos = 0;
    }
    if (s->rcap - s->rlen < want) {
        size_t cap = s->rlen + want;
        uint8_t *p = realloc(s->raw, cap);
        if (!p) oom();
        s->raw = p;
        s->rcap = cap;
    }
    size_t got = fread(s->raw + s->rlen, 1, s->rcap - s->rlen, s->f);
    s->rlen += got;
    if (got == 0) s->eof = 1;
    return (int)got;
}

static void refuse_bytes(Src *s, uint64_t off, char *err, size_t cap)
{
    unsigned byte = off >= s->roff && off - s->roff < s->rlen
                  ? s->raw[off - s->roff] : 0;
    seterr(err, cap,
           "%s is not valid %s -- byte 0x%02X at offset %llu cannot be "
           "decoded.\nIf you know the file's encoding, name it: "
           "--encoding latin-1 (or cp1252, utf-16, ...).",
           s->name, s->enc, byte, (unsigned long long)off);
}

/* Length of the valid UTF-8 prefix of p[0..n). *bad is set when an invalid
 * sequence starts at the returned position; otherwise the remainder (if any)
 * is an incomplete sequence that more input may finish. Strict in the same
 * way Python's decoder is: no overlongs, no surrogates, nothing above
 * U+10FFFF. */
static size_t utf8_valid(const uint8_t *p, size_t n, int *bad)
{
    size_t i = 0;
    *bad = 0;
    while (i < n) {
        uint8_t c = p[i];
        if (c < 0x80) { i++; continue; }
        size_t need;
        uint8_t lo = 0x80, hi = 0xBF;
        if (c >= 0xC2 && c <= 0xDF) need = 1;
        else if (c == 0xE0) { need = 2; lo = 0xA0; }
        else if (c >= 0xE1 && c <= 0xEC) need = 2;
        else if (c == 0xED) { need = 2; hi = 0x9F; }
        else if (c >= 0xEE && c <= 0xEF) need = 2;
        else if (c == 0xF0) { need = 3; lo = 0x90; }
        else if (c >= 0xF1 && c <= 0xF3) need = 3;
        else if (c == 0xF4) { need = 3; hi = 0x8F; }
        else { *bad = 1; return i; }
        for (size_t k = 1; k <= need; k++) {
            if (i + k >= n) return i;             /* incomplete: wait */
            uint8_t x = p[i + k];
            uint8_t l = k == 1 ? lo : 0x80, h = k == 1 ? hi : 0xBF;
            if (x < l || x > h) { *bad = 1; return i; }
        }
        i += need + 1;
    }
    return i;
}

/* Append decoded UTF-8 to `out`. Returns bytes appended (0 at the end of the
 * input) or -1 with `err` set. */
static long src_read(Src *s, Buf *out, char *err, size_t cap)
{
    for (;;) {
        if (s->rpos == s->rlen) {
            if (s->eof || !src_fill(s, READ_CHUNK)) return 0;
        }
        size_t avail = s->rlen - s->rpos;
        if (!s->conv) {
            int bad;
            size_t ok = utf8_valid(s->raw + s->rpos, avail, &bad);
            if (bad) { refuse_bytes(s, s->roff + s->rpos + ok, err, cap); return -1; }
            if (ok) {
                buf_put(out, s->raw + s->rpos, ok);
                s->rpos += ok;
                return (long)ok;
            }
            /* only an incomplete sequence is left: it needs more bytes */
            if (s->eof || !src_fill(s, READ_CHUNK)) {
                refuse_bytes(s, s->roff + s->rpos, err, cap);
                return -1;
            }
            continue;
        }
        buf_need(out, avail * 4 + 16);
        char *in = (char *)s->raw + s->rpos;
        size_t inleft = avail;
        char *op = (char *)out->data + out->len;
        size_t outleft = out->cap - out->len;
        size_t r = iconv(s->cd, &in, &inleft, &op, &outleft);
        int e = errno;
        size_t made = (out->cap - out->len) - outleft;
        out->len += made;
        s->rpos += avail - inleft;
        if (made) return (long)made;
        /* Nothing came out but the input was fine (a shift sequence, say):
         * that is not the end of the file -- go round for more. Returning 0
         * here would read as EOF and silently cut the table short. */
        if (r != (size_t)-1) continue;
        if (e == EILSEQ) { refuse_bytes(s, s->roff + s->rpos, err, cap); return -1; }
        if (e == EINVAL) {                         /* incomplete at the end */
            if (s->eof || !src_fill(s, READ_CHUNK)) {
                refuse_bytes(s, s->roff + s->rpos, err, cap);
                return -1;
            }
            continue;
        }
        if (e == E2BIG) continue;
        seterr(err, cap, "%s: cannot decode as %s", s->name, s->enc);
        return -1;
    }
}

static void src_close(Src *s)
{
    if (s->conv) iconv_close(s->cd);
    if (s->own && s->f) fclose(s->f);
    free(s->raw);
    memset(s, 0, sizeof(*s));
}

/* Python-style names people will actually type, mapped to iconv's. */
static const char *iconv_name(const char *want, char *buf, size_t cap)
{
    size_t i = 0;
    for (; want[i] && i + 1 < cap; i++) {
        char c = want[i];
        if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
        if (c == '_') c = '-';
        buf[i] = c;
    }
    buf[i] = 0;
    if (!strcmp(buf, "LATIN-1") || !strcmp(buf, "LATIN1") || !strcmp(buf, "L1")
        || !strcmp(buf, "ISO8859-1") || !strcmp(buf, "ISO-8859-1"))
        return "ISO-8859-1";
    if (!strcmp(buf, "WINDOWS-1252") || !strcmp(buf, "CP-1252")) return "CP1252";
    return buf;
}

static int is_utf8_name(const char *e)
{
    char b[32];
    iconv_name(e, b, sizeof(b));
    return !strcmp(b, "UTF-8") || !strcmp(b, "UTF8") || !strcmp(b, "UTF-8-SIG")
        || !strcmp(b, "U8");
}

static int src_open(Src *s, const char *path, const char *encoding,
                    char *err, size_t cap)
{
    memset(s, 0, sizeof(*s));
    snprintf(s->name, sizeof(s->name), "%s",
             strcmp(path, "-") ? base_name(path) : "standard input");
    if (!strcmp(path, "-")) s->f = stdin;
    else {
        struct stat st;
        if (stat(path, &st) == 0 && S_ISDIR(st.st_mode)) {
            seterr(err, cap, "%s is a directory, not a file", path);
            return -1;
        }
        s->f = fopen(path, "rb");
        if (!s->f) {
            if (errno == ENOENT) seterr(err, cap, "no such file: %s", path);
            else if (errno == EACCES) seterr(err, cap, "not allowed to read %s", path);
            else seterr(err, cap, "cannot read %s: %s", path, strerror(errno));
            return -1;
        }
        s->own = 1;
    }
    /* the first bytes, for the byte-order mark */
    while (s->rlen < 4 && !s->eof) src_fill(s, READ_CHUNK);
    const uint8_t *h = s->raw;
    size_t hn = s->rlen;

    /* Longest first: the UTF-32LE mark starts with the UTF-16LE one, so
     * testing 16 first would read a UTF-32 file as UTF-16 nonsense. */
    const char *from = NULL;
    size_t skip = 0;
    if (hn >= 4 && !memcmp(h, "\xFF\xFE\0\0", 4)) { from = "UTF-32LE"; skip = 4; }
    else if (hn >= 4 && !memcmp(h, "\0\0\xFE\xFF", 4)) { from = "UTF-32BE"; skip = 4; }
    else if (hn >= 3 && !memcmp(h, "\xEF\xBB\xBF", 3)) { from = NULL; skip = 3; }
    else if (hn >= 2 && !memcmp(h, "\xFF\xFE", 2)) { from = "UTF-16LE"; skip = 2; }
    else if (hn >= 2 && !memcmp(h, "\xFE\xFF", 2)) { from = "UTF-16BE"; skip = 2; }

    char nb[64];
    if (encoding && *encoding) {
        const char *want = iconv_name(encoding, nb, sizeof(nb));
        snprintf(s->enc, sizeof(s->enc), "%s", want);
        if (is_utf8_name(encoding)) {
            from = NULL;
            skip = (hn >= 3 && !memcmp(h, "\xEF\xBB\xBF", 3)) ? 3 : 0;
        } else if (!strcmp(want, "UTF-16") || !strcmp(want, "UTF-32")) {
            /* the mark says which byte order; without one, little-endian */
            int is16 = !strcmp(want, "UTF-16");
            if (!from || (is16 && skip != 2) || (!is16 && skip != 4)) {
                from = is16 ? "UTF-16LE" : "UTF-32LE";
                skip = 0;
            }
        } else {
            from = want;
            skip = 0;
        }
    } else {
        snprintf(s->enc, sizeof(s->enc), "%s", from ? from : "UTF-8");
    }
    s->rpos = skip < hn ? skip : hn;
    if (from) {
        s->cd = iconv_open("UTF-8", from);
        if (s->cd == (iconv_t)-1) {
            seterr(err, cap, "unknown encoding: %s", encoding ? encoding : from);
            if (s->own) fclose(s->f);
            free(s->raw);
            memset(s, 0, sizeof(*s));
            return -1;
        }
        s->conv = 1;
    }
    return 0;
}

/* ================================================================== csv */

typedef struct { size_t off, len; } Span;

struct CsvIn {
    Src     src;
    char    delim;
    Buf     in;              /* decoded text */
    size_t  pos;             /* start of the next record in `in` */
    int     src_done;
    size_t  want;            /* how much to read at the next refill */
    char  **names;
    size_t  width;
    int     have_header;
    int     finished;
    int     gave_block;
    unsigned long long record;   /* records read, the header counting as 1 */
    char    err[640];
};

enum { REC_OK, REC_MORE, REC_EOF };

static void span_push(Span **sp, size_t *n, size_t *cap, size_t off, size_t len)
{
    if (*n == *cap) {
        *cap = *cap ? *cap * 2 : 4096;
        Span *p = realloc(*sp, *cap * sizeof(Span));
        if (!p) oom();
        *sp = p;
    }
    (*sp)[*n].off = off;
    (*sp)[*n].len = len;
    (*n)++;
}

/* One record from `d[*pos..n)`: cells appended to `arena`, their spans to
 * `sp`. REC_MORE means the buffer ended mid-record and more input may follow;
 * the caller rolls back and retries with more. */
static int parse_record(const uint8_t *d, size_t n, size_t *pos, int eof,
                        char delim, Buf *arena,
                        Span **sp, size_t *nsp, size_t *capsp, size_t *nf)
{
    size_t i = *pos;
    *nf = 0;
    if (i >= n) return eof ? REC_EOF : REC_MORE;
    size_t fstart = arena->len;
    #define PUSH() do { span_push(sp, nsp, capsp, fstart, arena->len - fstart); \
                        (*nf)++; fstart = arena->len; } while (0)
    int field_open = 0;
    for (;;) {
        if (i >= n) {
            if (!eof) return REC_MORE;
            PUSH();                      /* a last line with no terminator */
            *pos = i;
            return REC_OK;
        }
        uint8_t ch = d[i];
        if (ch == '"' && !field_open) {
            i++;
            field_open = 1;
            for (;;) {
                const uint8_t *q = i < n ? memchr(d + i, '"', n - i) : NULL;
                if (!q) {
                    if (!eof) return REC_MORE;
                    /* unterminated quote: the rest of the file is the cell */
                    buf_put(arena, d + i, n - i);
                    i = n;
                    break;
                }
                size_t k = (size_t)(q - d);
                buf_put(arena, d + i, k - i);
                if (k + 1 >= n) {
                    if (!eof) return REC_MORE;
                    i = k + 1;
                    break;
                }
                if (d[k + 1] == '"') { buf_putc(arena, '"'); i = k + 2; continue; }
                i = k + 1;               /* the closing quote */
                break;
            }
            continue;
        }
        if (ch == (uint8_t)delim) { PUSH(); field_open = 0; i++; continue; }
        if (ch == '\n') { PUSH(); *pos = i + 1; return REC_OK; }
        if (ch == '\r') {
            if (i + 1 >= n && !eof) return REC_MORE;   /* maybe CRLF */
            PUSH();
            *pos = (i + 1 < n && d[i + 1] == '\n') ? i + 2 : i + 1;
            return REC_OK;
        }
        size_t k = i;
        while (k < n) {
            uint8_t x = d[k];
            if (x == (uint8_t)delim || x == '\n' || x == '\r') break;
            k++;
        }
        buf_put(arena, d + i, k - i);
        i = k;
        field_open = 1;
    }
    #undef PUSH
}

/* Pick the delimiter from the header line: whichever of , TAB ; | occurs most
 * outside quotes, ties in that order, comma when none does. Only the header
 * is read, so a tab inside a data value cannot be mistaken for it. */
static char sniff_delim(const uint8_t *d, size_t n)
{
    const char cand[4] = { ',', '\t', ';', '|' };
    size_t cnt[4] = { 0, 0, 0, 0 };
    int inq = 0;
    for (size_t i = 0; i < n; i++) {
        uint8_t c = d[i];
        if (c == '"') { inq = !inq; continue; }
        if (inq) continue;
        if (c == '\n' || c == '\r') break;
        for (int k = 0; k < 4; k++) if (c == (uint8_t)cand[k]) cnt[k]++;
    }
    int best = 0;
    for (int k = 1; k < 4; k++) if (cnt[k] > cnt[best]) best = k;
    return cnt[best] ? cand[best] : ',';
}

/* More decoded text into c->in, dropping what has been consumed. */
static int csv_refill(CsvIn *c)
{
    if (c->pos) {
        memmove(c->in.data, c->in.data + c->pos, c->in.len - c->pos);
        c->in.len -= c->pos;
        c->pos = 0;
    }
    size_t target = c->in.len + c->want;
    while (c->in.len < target && !c->src_done) {
        long got = src_read(&c->src, &c->in, c->err, sizeof(c->err));
        if (got < 0) return -1;
        if (got == 0) c->src_done = 1;
    }
    return 0;
}

CsvIn *csv_open(const char *path, const char *encoding, char delim,
                char *err, size_t cap)
{
    CsvIn *c = calloc(1, sizeof(CsvIn));
    if (!c) oom();
    if (src_open(&c->src, path, encoding, err, cap)) { free(c); return NULL; }
    buf_init(&c->in);
    c->want = READ_CHUNK;
    if (csv_refill(c)) {
        seterr(err, cap, "%s", c->err);
        csv_close(c);
        return NULL;
    }
    if (!delim) {
        const char *f = ppz_format_of(path);
        if (!strcmp(f, "tsv")) delim = '\t';
        else if (!strcmp(f, "psv")) delim = '|';
        else if (!strcmp(f, "csv")) delim = ',';
        else delim = sniff_delim(c->in.data, c->in.len);
    }
    c->delim = delim;
    return c;
}

const char *csv_error(const CsvIn *c) { return c->err; }
size_t csv_width(const CsvIn *c) { return c->width; }
uint64_t csv_bytes_read(const CsvIn *c) { return c->src.roff + c->src.rlen; }
char csv_delim(const CsvIn *c) { return c->delim; }

void csv_close(CsvIn *c)
{
    if (!c) return;
    src_close(&c->src);
    buf_free(&c->in);
    for (size_t j = 0; j < c->width; j++) free(c->names[j]);
    free(c->names);
    free(c);
}

/* The next block of up to `max_rows` rows (0: all of them). Returns 1 with a
 * block in `t`, 0 when the file is exhausted, -1 on error (csv_error). A file
 * with a header and no rows yields one empty block, so it still has columns. */
int csv_next(CsvIn *c, Table *t, size_t max_rows)
{
    table_init(t);
    if (c->finished) return 0;
    buf_need(&t->arena, 1);

    Span *sp = NULL;
    size_t nsp = 0, capsp = 0, nrows = 0;
    Buf hdr;
    buf_init(&hdr);

    for (;;) {
        if (max_rows && nrows >= max_rows) break;
        size_t save_arena = t->arena.len, save_nsp = nsp, nf = 0;
        size_t p = c->pos;
        Buf *arena = c->have_header ? &t->arena : &hdr;
        int r = parse_record(c->in.data, c->in.len, &p, c->src_done, c->delim,
                             arena, &sp, &nsp, &capsp, &nf);
        if (r == REC_MORE) {
            arena->len = save_arena;
            if (!c->have_header) hdr.len = 0;
            nsp = save_nsp;
            /* a record longer than one read: read at least that much again,
             * so one huge quoted cell costs O(n log n), not O(n^2) */
            size_t partial = c->in.len - c->pos;
            if (partial > c->want) c->want = partial;
            if (csv_refill(c)) goto fail;
            continue;
        }
        if (r == REC_EOF) { c->finished = 1; break; }
        c->pos = p;
        c->record++;

        if (!c->have_header) {
            c->width = nf;
            c->names = calloc(nf ? nf : 1, sizeof(char *));
            if (!c->names) oom();
            for (size_t j = 0; j < nf; j++) {
                Span s = sp[save_nsp + j];
                char *nm = malloc(s.len + 1);
                if (!nm) oom();
                memcpy(nm, hdr.data + s.off, s.len);
                nm[s.len] = 0;
                c->names[j] = nm;
            }
            nsp = save_nsp;
            c->have_header = 1;
            buf_free(&hdr);
            continue;
        }
        if (nf < c->width) {
            for (; nf < c->width; nf++) span_push(&sp, &nsp, &capsp, 0, 0);
        } else if (nf > c->width) {
            for (size_t j = c->width; j < nf; j++) {
                if (sp[save_nsp + j].len) {
                    snprintf(c->err, sizeof(c->err),
                             "row %llu of %s has %zu cells but the header has "
                             "%zu, and cell %zu is not empty.\nPolypress will "
                             "not drop data to make the table rectangular. Fix "
                             "the row or the header and try again.",
                             c->record, c->src.name, nf, c->width, j + 1);
                    goto fail;
                }
            }
            nsp = save_nsp + c->width;
        }
        nrows++;
        /* consumed text can go once the buffer is mostly spent */
        if (c->pos > (c->in.len >> 1) && c->pos > READ_CHUNK) {
            memmove(c->in.data, c->in.data + c->pos, c->in.len - c->pos);
            c->in.len -= c->pos;
            c->pos = 0;
        }
    }
    buf_free(&hdr);

    if (!c->have_header) { free(sp); c->finished = 1; return c->gave_block ? 0 : (c->gave_block = 1, 1); }
    if (nrows == 0 && c->gave_block) { free(sp); return 0; }

    t->ncols = c->width;
    t->nrows = nrows;
    t->names = calloc(c->width ? c->width : 1, sizeof(char *));
    if (!t->names) oom();
    for (size_t j = 0; j < c->width; j++) {
        t->names[j] = strdup(c->names[j]);
        if (!t->names[j]) oom();
    }
    /* Span and Str are both two words: convert in place */
    Str *cells = (Str *)sp;
    if (!cells) { cells = calloc(1, sizeof(Str)); if (!cells) oom(); }
    for (size_t k = 0; k < nsp; k++) {
        size_t off = sp[k].off, len = sp[k].len;
        cells[k].p = (const char *)t->arena.data + off;
        cells[k].n = len;
    }
    t->cells = cells;
    c->gave_block = 1;
    return 1;

fail:
    buf_free(&hdr);
    free(sp);
    table_free(t);
    return -1;
}

/* The in-memory parse the encoder uses to check its canonical CSV comes back.
 * Same state machine, comma only, no decoding (the bytes are its own). */
int table_parse_csv(Table *t, const uint8_t *data, size_t n)
{
    table_init(t);
    buf_need(&t->arena, 1);
    Span *sp = NULL;
    size_t nsp = 0, capsp = 0, nf = 0, pos = 0, width = 0, nrows = 0;
    Buf hdr;
    buf_init(&hdr);
    int have_header = 0;
    for (;;) {
        size_t before = nsp;
        int r = parse_record(data, n, &pos, 1, ',', have_header ? &t->arena : &hdr,
                             &sp, &nsp, &capsp, &nf);
        if (r != REC_OK) break;
        if (!have_header) {
            width = nf;
            t->names = calloc(nf ? nf : 1, sizeof(char *));
            if (!t->names) oom();
            for (size_t j = 0; j < nf; j++) {
                char *nm = malloc(sp[j].len + 1);
                if (!nm) oom();
                memcpy(nm, hdr.data + sp[j].off, sp[j].len);
                nm[sp[j].len] = 0;
                t->names[j] = nm;
            }
            t->ncols = width;
            nsp = 0;
            have_header = 1;
            continue;
        }
        if (nf < width) for (; nf < width; nf++) span_push(&sp, &nsp, &capsp, 0, 0);
        else if (nf > width) {
            for (size_t j = width; j < nf; j++)
                if (sp[before + j].len) { buf_free(&hdr); free(sp); table_free(t); return -1; }
            nsp = before + width;
        }
        nrows++;
    }
    buf_free(&hdr);
    t->nrows = nrows;
    Str *cells = (Str *)sp;
    if (!cells) { cells = calloc(1, sizeof(Str)); if (!cells) oom(); }
    for (size_t k = 0; k < nsp; k++) {
        size_t off = sp[k].off, len = sp[k].len;
        cells[k].p = (const char *)t->arena.data + off;
        cells[k].n = len;
    }
    t->cells = cells;
    return 0;
}

/* ================================================================= json */

/* Column names to indices, first appearance wins the position. */
typedef struct {
    char  **keys;
    size_t *idx;
    size_t  cap, n;
    char  **names;           /* by index */
    size_t  ncap;
} NameMap;

static uint64_t fnv(const char *s)
{
    uint64_t h = 1469598103934665603ULL;
    while (*s) { h ^= (uint8_t)*s++; h *= 1099511628211ULL; }
    return h;
}

static size_t map_get(NameMap *m, const char *key)
{
    if (m->n * 2 + 2 > m->cap) {
        size_t nc = m->cap ? m->cap * 2 : 64;
        char **nk = calloc(nc, sizeof(char *));
        size_t *ni = calloc(nc, sizeof(size_t));
        if (!nk || !ni) oom();
        for (size_t i = 0; i < m->cap; i++) {
            if (!m->keys[i]) continue;
            size_t h = (size_t)fnv(m->keys[i]) & (nc - 1);
            while (nk[h]) h = (h + 1) & (nc - 1);
            nk[h] = m->keys[i];
            ni[h] = m->idx[i];
        }
        free(m->keys); free(m->idx);
        m->keys = nk; m->idx = ni; m->cap = nc;
    }
    size_t h = (size_t)fnv(key) & (m->cap - 1);
    while (m->keys[h]) {
        if (!strcmp(m->keys[h], key)) return m->idx[h];
        h = (h + 1) & (m->cap - 1);
    }
    char *k = strdup(key);
    if (!k) oom();
    m->keys[h] = k;
    m->idx[h] = m->n;
    if (m->n == m->ncap) {
        m->ncap = m->ncap ? m->ncap * 2 : 16;
        char **nn = realloc(m->names, m->ncap * sizeof(char *));
        if (!nn) oom();
        m->names = nn;
    }
    m->names[m->n] = k;
    return m->n++;
}

static void map_free(NameMap *m)
{
    for (size_t i = 0; i < m->n; i++) free(m->names[i]);
    free(m->names); free(m->keys); free(m->idx);
    memset(m, 0, sizeof(*m));
}

/* JSON string escaping for output: quote, backslash and control characters
 * only; everything else, non-ASCII included, is written as it is. */
void ppz_json_str(Buf *b, const char *s, size_t n)
{
    static const char hex[] = "0123456789abcdef";
    buf_putc(b, '"');
    size_t run = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 0x20 && c != '"' && c != '\\') continue;
        buf_put(b, s + run, i - run);
        run = i + 1;
        switch (c) {
        case '"':  buf_put(b, "\\\"", 2); break;
        case '\\': buf_put(b, "\\\\", 2); break;
        case '\n': buf_put(b, "\\n", 2); break;
        case '\r': buf_put(b, "\\r", 2); break;
        case '\t': buf_put(b, "\\t", 2); break;
        case '\b': buf_put(b, "\\b", 2); break;
        case '\f': buf_put(b, "\\f", 2); break;
        default: {
            char u[6] = { '\\', 'u', '0', '0', hex[c >> 4], hex[c & 15] };
            buf_put(b, u, 6);
        }
        }
    }
    buf_put(b, s + run, n - run);
    buf_putc(b, '"');
}

/* A nested value, compact, keys in the order written. */
static void json_compact(Buf *b, const Js *v)
{
    switch (v->kind) {
    case JS_NULL: buf_put(b, "null", 4); break;
    case JS_BOOL: v->boolean ? buf_put(b, "true", 4) : buf_put(b, "false", 5); break;
    case JS_NUM:  buf_put(b, v->str, v->len); break;
    case JS_STR:  ppz_json_str(b, v->str ? v->str : "", v->str ? v->len : 0); break;
    case JS_ARR:
        buf_putc(b, '[');
        for (size_t i = 0; i < v->count; i++) {
            if (i) buf_putc(b, ',');
            json_compact(b, &v->items[i]);
        }
        buf_putc(b, ']');
        break;
    case JS_OBJ:
        buf_putc(b, '{');
        for (size_t i = 0; i < v->count; i++) {
            if (i) buf_putc(b, ',');
            const char *k = v->keys[i] ? v->keys[i] : "";
            ppz_json_str(b, k, strlen(k));
            buf_putc(b, ':');
            json_compact(b, &v->items[i]);
        }
        buf_putc(b, '}');
        break;
    }
}

/* A value as a cell: strings as they are, numbers as written, true/false,
 * null as empty, anything nested as compact JSON. */
static void cell_of(Buf *arena, const Js *v)
{
    switch (v->kind) {
    case JS_NULL: break;
    case JS_BOOL: v->boolean ? buf_put(arena, "true", 4) : buf_put(arena, "false", 5); break;
    case JS_NUM:  buf_put(arena, v->str, v->len); break;
    case JS_STR:  if (v->str) buf_put(arena, v->str, v->len); break;
    default:      json_compact(arena, v); break;
    }
}

typedef struct {
    Span  *sp;               /* nrows * ncols, grown as columns appear */
    size_t nrows, ncols, rowcap;
} Grid;

/* Records -> table. `recs` are objects; columns are the union of their keys
 * in first-seen order, a missing key is an empty cell, and a key repeated in
 * one record keeps its last value. */
static int table_from_records(Table *t, const Js *const *recs, size_t n,
                              char *err, size_t cap, const char *name)
{
    NameMap m;
    memset(&m, 0, sizeof(m));
    for (size_t r = 0; r < n; r++) {
        if (recs[r]->kind != JS_OBJ) {
            seterr(err, cap, "%s: record %zu is not an object, so this is not "
                   "a table", name, r + 1);
            map_free(&m);
            return -1;
        }
        for (size_t k = 0; k < recs[r]->count; k++)
            map_get(&m, recs[r]->keys[k] ? recs[r]->keys[k] : "");
    }
    table_init(t);
    buf_need(&t->arena, 1);
    size_t w = m.n;
    Span *sp = calloc(n * w ? n * w : 1, sizeof(Span));
    if (!sp) oom();
    for (size_t r = 0; r < n; r++) {
        for (size_t k = 0; k < recs[r]->count; k++) {
            size_t j = map_get(&m, recs[r]->keys[k] ? recs[r]->keys[k] : "");
            size_t at = t->arena.len;
            cell_of(&t->arena, &recs[r]->items[k]);
            sp[r * w + j].off = at;
            sp[r * w + j].len = t->arena.len - at;
        }
    }
    t->ncols = w;
    t->nrows = n;
    t->names = calloc(w ? w : 1, sizeof(char *));
    if (!t->names) oom();
    for (size_t j = 0; j < w; j++) { t->names[j] = strdup(m.names[j]); if (!t->names[j]) oom(); }
    Str *cells = (Str *)sp;
    for (size_t k = 0; k < n * w; k++) {
        size_t off = sp[k].off, len = sp[k].len;
        cells[k].p = (const char *)t->arena.data + off;
        cells[k].n = len;
    }
    t->cells = cells;
    map_free(&m);
    return 0;
}

static int read_all(Src *s, Buf *out, char *err, size_t cap)
{
    for (;;) {
        long got = src_read(s, out, err, cap);
        if (got < 0) return -1;
        if (got == 0) return 0;
    }
}

static int read_json(Table *t, const char *path, const char *encoding,
                     int lines, char *err, size_t cap)
{
    Src s;
    if (src_open(&s, path, encoding, err, cap)) return -1;
    Buf text;
    buf_init(&text);
    int rc = read_all(&s, &text, err, cap);
    char name[256];
    snprintf(name, sizeof(name), "%s", s.name);
    src_close(&s);
    if (rc) { buf_free(&text); return -1; }

    const char *p = (const char *)text.data;
    size_t n = text.len;
    rc = -1;

    if (lines) {
        size_t cnt = 0, capr = 0;
        Js **recs = NULL;
        size_t line = 0;
        for (size_t i = 0; i < n; ) {
            const char *nl = memchr(p + i, '\n', n - i);
            size_t e = nl ? (size_t)(nl - p) : n;
            line++;
            size_t k = i;
            while (k < e && (p[k] == ' ' || p[k] == '\t' || p[k] == '\r')) k++;
            if (k < e) {
                size_t used = 0;
                Js *v = js_parse_prefix(p + i, e - i, &used);
                if (!v || used != e - i) {
                    js_free(v);
                    seterr(err, cap, "%s: line %zu is not valid JSON", name, line);
                    goto lines_done;
                }
                if (cnt == capr) {
                    capr = capr ? capr * 2 : 1024;
                    Js **nr = realloc(recs, capr * sizeof(Js *));
                    if (!nr) oom();
                    recs = nr;
                }
                recs[cnt++] = v;
            }
            i = e + 1;
        }
        rc = table_from_records(t, (const Js *const *)recs, cnt, err, cap, name);
    lines_done:
        for (size_t i = 0; i < cnt; i++) js_free(recs[i]);
        free(recs);
        buf_free(&text);
        return rc;
    }

    size_t used = 0;
    Js *v = js_parse_prefix(p, n, &used);
    if (!v || used != n) {
        js_free(v);
        buf_free(&text);
        seterr(err, cap, "%s is not valid JSON", name);
        return -1;
    }
    if (v->kind == JS_ARR) {
        const Js **recs = malloc((v->count ? v->count : 1) * sizeof(Js *));
        if (!recs) oom();
        for (size_t i = 0; i < v->count; i++) recs[i] = &v->items[i];
        rc = table_from_records(t, recs, v->count, err, cap, name);
        free(recs);
    } else if (v->kind == JS_OBJ) {
        /* column form: {"name": [values], ...} */
        size_t w = v->count, nr = 0;
        for (size_t j = 0; j < w; j++) {
            if (v->items[j].kind != JS_ARR) {
                seterr(err, cap, "%s: column %zu is not a list, so this is not "
                       "a table", name, j + 1);
                goto obj_done;
            }
            if (v->items[j].count > nr) nr = v->items[j].count;
        }
        table_init(t);
        buf_need(&t->arena, 1);
        Span *sp = calloc(nr * w ? nr * w : 1, sizeof(Span));
        if (!sp) oom();
        for (size_t j = 0; j < w; j++)
            for (size_t r = 0; r < v->items[j].count; r++) {
                size_t at = t->arena.len;
                cell_of(&t->arena, &v->items[j].items[r]);
                sp[r * w + j].off = at;
                sp[r * w + j].len = t->arena.len - at;
            }
        t->ncols = w;
        t->nrows = nr;
        t->names = calloc(w ? w : 1, sizeof(char *));
        if (!t->names) oom();
        for (size_t j = 0; j < w; j++) {
            t->names[j] = strdup(v->keys[j] ? v->keys[j] : "");
            if (!t->names[j]) oom();
        }
        Str *cells = (Str *)sp;
        for (size_t k = 0; k < nr * w; k++) {
            size_t off = sp[k].off, len = sp[k].len;
            cells[k].p = (const char *)t->arena.data + off;
            cells[k].n = len;
        }
        t->cells = cells;
        rc = 0;
    } else {
        seterr(err, cap, "%s holds a single value, not a table", name);
    }
obj_done:
    js_free(v);
    buf_free(&text);
    return rc;
}

int table_read_any(Table *t, const char *path, const char *encoding,
                   char *err, size_t cap)
{
    const char *f = ppz_format_of(path);
    if (!strcmp(f, "parquet")) {
        seterr(err, cap, "%s is Parquet, which needs Python to read.\n"
               "Convert it first:  python3 py/parquet.py to-csv %s | "
               "polypress compress - -o %s.ppz", path, path, path);
        return -1;
    }
    if (!strcmp(f, "json")) return read_json(t, path, encoding, 0, err, cap);
    if (!strcmp(f, "jsonl")) return read_json(t, path, encoding, 1, err, cap);

    CsvIn *c = csv_open(path, encoding, 0, err, cap);
    if (!c) return -1;
    int r = csv_next(c, t, 0);
    if (r < 0) { seterr(err, cap, "%s", c->err); csv_close(c); return -1; }
    if (r == 0) table_init(t);
    csv_close(c);
    return 0;
}

/* ============================================================== writers */

struct Writer {
    FILE  *f;
    char  *path;             /* final name */
    char  *tmp;              /* written here, renamed into place at the end */
    int    fmt;              /* 0 delimited, 1 json, 2 jsonl */
    char   delim;
    char **names;
    size_t ncols;
    unsigned long long rows;
    Buf    out;
};

static void delim_field(Buf *b, const char *s, size_t n, char delim)
{
    int q = 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (c == delim || c == '"' || c == '\n' || c == '\r') { q = 1; break; }
    }
    if (!q) { buf_put(b, s, n); return; }
    buf_putc(b, '"');
    size_t run = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] != '"') continue;
        buf_put(b, s + run, i + 1 - run);
        buf_putc(b, '"');
        run = i + 1;
    }
    buf_put(b, s + run, n - run);
    buf_putc(b, '"');
}

static int flush_out(Writer *w)
{
    if (!w->out.len) return 0;
    size_t n = w->out.len;
    size_t got = fwrite(w->out.data, 1, n, w->f);
    w->out.len = 0;
    return got == n ? 0 : -1;
}

Writer *writer_open(const char *path, char *const *names, size_t ncols,
                    char *err, size_t cap)
{
    const char *f = ppz_format_of(path);
    if (!strcmp(f, "parquet")) {
        seterr(err, cap, "writing Parquet needs Python:  polypress restore "
               "ARCHIVE -o - | python3 py/parquet.py from-csv - %s", path);
        return NULL;
    }
    Writer *w = calloc(1, sizeof(Writer));
    if (!w) oom();
    w->fmt = !strcmp(f, "json") ? 1 : !strcmp(f, "jsonl") ? 2 : 0;
    w->delim = !strcmp(f, "tsv") ? '\t' : !strcmp(f, "psv") ? '|' : ',';
    if (w->fmt) {
        /* a record cannot hold the same key twice */
        NameMap m;
        memset(&m, 0, sizeof(m));
        for (size_t j = 0; j < ncols; j++) {
            if (map_get(&m, names[j]) != j) {
                seterr(err, cap, "%s cannot represent the duplicate column name "
                       "\"%s\"; write .csv or .tsv instead",
                       w->fmt == 1 ? "JSON" : "JSON Lines", names[j]);
                map_free(&m);
                free(w);
                return NULL;
            }
        }
        map_free(&m);
    }
    w->ncols = ncols;
    w->names = calloc(ncols ? ncols : 1, sizeof(char *));
    if (!w->names) oom();
    for (size_t j = 0; j < ncols; j++) { w->names[j] = strdup(names[j]); if (!w->names[j]) oom(); }
    buf_init(&w->out);

    if (!strcmp(path, "-")) {
        w->f = stdout;
    } else {
        /* A restore that fails half way must not leave half a file under the
         * name the user asked for, so write beside it and rename at the end. */
        w->path = strdup(path);
        size_t n = strlen(path) + 16;
        w->tmp = malloc(n);
        if (!w->path || !w->tmp) oom();
        snprintf(w->tmp, n, "%s.partXXXXXX", path);
        int fd = mkstemp(w->tmp);
        if (fd < 0) {
            seterr(err, cap, "cannot write %s: %s", path, strerror(errno));
            free(w->path); free(w->tmp);
            for (size_t j = 0; j < ncols; j++) free(w->names[j]);
            free(w->names); free(w);
            return NULL;
        }
        mode_t um = umask(0);
        umask(um);
        fchmod(fd, 0666 & ~um);
        w->f = fdopen(fd, "wb");
        ppz_tmp_register(w->tmp);
    }
    if (w->fmt == 0 && ncols) {
        for (size_t j = 0; j < ncols; j++) {
            if (j) buf_putc(&w->out, w->delim);
            delim_field(&w->out, names[j], strlen(names[j]), w->delim);
        }
        buf_putc(&w->out, '\n');
    }
    return w;
}

int writer_rows(Writer *w, const Table *t)
{
    for (size_t i = 0; i < t->nrows; i++) {
        if (w->fmt == 0) {
            if (!w->ncols) continue;
            for (size_t j = 0; j < w->ncols; j++) {
                if (j) buf_putc(&w->out, w->delim);
                Str s = table_at(t, i, j);
                delim_field(&w->out, s.p, s.n, w->delim);
            }
            buf_putc(&w->out, '\n');
        } else {
            int js = w->fmt == 1;
            if (js) buf_put(&w->out, w->rows ? ",\n " : "[\n ", w->rows ? 3 : 3);
            buf_putc(&w->out, '{');
            for (size_t j = 0; j < w->ncols; j++) {
                if (j) buf_put(&w->out, js ? ", " : ",", js ? 2 : 1);
                ppz_json_str(&w->out, w->names[j], strlen(w->names[j]));
                buf_put(&w->out, js ? ": " : ":", js ? 2 : 1);
                Str s = table_at(t, i, j);
                ppz_json_str(&w->out, s.p, s.n);
            }
            buf_putc(&w->out, '}');
            if (!js) buf_putc(&w->out, '\n');
        }
        w->rows++;
        if (w->out.len >= READ_CHUNK && flush_out(w)) return -1;
    }
    return flush_out(w);
}

int writer_close(Writer *w, int ok, char *err, size_t cap)
{
    if (!w) return -1;
    int rc = ok ? 0 : -1;
    if (ok && w->fmt == 1) {
        if (w->rows) buf_put(&w->out, "\n]\n", 3);
        else {
            /* a list of records cannot carry column names; the column form
             * can, and the reader understands both */
            buf_putc(&w->out, '{');
            for (size_t j = 0; j < w->ncols; j++) {
                if (j) buf_put(&w->out, ", ", 2);
                ppz_json_str(&w->out, w->names[j], strlen(w->names[j]));
                buf_put(&w->out, ": []", 4);
            }
            buf_put(&w->out, "}\n", 2);
        }
    }
    if (ok && w->fmt == 2 && !w->rows && w->ncols) {
        seterr(err, cap, "JSON Lines cannot carry column names for a table with "
               "no rows; write .csv or .json instead");
        rc = -1;
    }
    if (rc == 0 && flush_out(w)) { seterr(err, cap, "write failed: %s", strerror(errno)); rc = -1; }
    if (w->f == stdout) {
        if (fflush(stdout)) rc = -1;
    } else if (w->f) {
        if (fclose(w->f)) { if (!rc) seterr(err, cap, "write failed: %s", strerror(errno)); rc = -1; }
        if (rc == 0) {
            if (rename(w->tmp, w->path)) {
                seterr(err, cap, "cannot write %s: %s", w->path, strerror(errno));
                rc = -1;
            }
        }
        if (rc) unlink(w->tmp);
        ppz_tmp_forget(w->tmp);
    }
    for (size_t j = 0; j < w->ncols; j++) free(w->names[j]);
    free(w->names); free(w->path); free(w->tmp);
    buf_free(&w->out);
    free(w);
    return rc;
}

int table_write_any(const Table *t, const char *path, char *err, size_t cap)
{
    Writer *w = writer_open(path, t->names, t->ncols, err, cap);
    if (!w) return -1;
    if (writer_rows(w, t)) {
        seterr(err, cap, "write failed: %s", strerror(errno));
        writer_close(w, 0, NULL, 0);
        return -1;
    }
    return writer_close(w, 1, err, cap);
}
