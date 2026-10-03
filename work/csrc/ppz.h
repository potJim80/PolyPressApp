/* Polypress -- shared declarations.
 *
 * The codec, the table readers and writers, the streaming container and the
 * command line, with nothing but liblzma behind it. tests/ checks it against
 * its contract: every table round-trips cell for cell, hostile archives are
 * refused, and the threaded encoder writes exactly the bytes the serial one
 * does.
 */

#ifndef PPZ_H
#define PPZ_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

/* ------------------------------------------------------------- containers */

/* The one archive format: "PPZ2", then the compressed lengths of the
 * metadata, binary and text streams (4 bytes each, big-endian), then the
 * three raw LZMA2 streams. Every earlier format (FAST,
 * PPZ1, PPZX, PPZB) was dropped on 2026-10-01 -- nothing was ever stored in
 * them -- so builds from before then refuse these archives outright. */
#define PPZ_MAGIC      "PPZ2"

/* ------------------------------------------------------------------ bytes */

typedef struct {
    uint8_t *data;
    size_t   len;
    size_t   cap;
} Buf;

void  buf_init(Buf *b);
void  buf_free(Buf *b);
void  buf_need(Buf *b, size_t extra);
void  buf_put(Buf *b, const void *p, size_t n);
void  buf_putc(Buf *b, char c);

/* A borrowed slice of bytes. Cells point into one big arena rather than
 * being individually allocated: a 421-column survey has millions of cells,
 * and one malloc each is both slow and a fragmentation disaster. */
typedef struct {
    const char *p;
    size_t      n;
} Str;

/* ------------------------------------------------------------------ table */

typedef struct {
    char  **names;     /* column names, NUL-terminated, owned */
    size_t  ncols;
    size_t  nrows;
    Str    *cells;     /* row-major, nrows * ncols, pointing into `arena` */
    Buf     arena;     /* backing store for every cell */
} Table;

void   table_init(Table *t);
void   table_free(Table *t);
Str    table_at(const Table *t, size_t row, size_t col);

/* ------------------------------------------------------------- table I/O */

/* ppz_io.c. Every function that can fail fills `err` with a message meant
 * for the person at the keyboard. */

/* "csv", "tsv", "psv", "json", "jsonl", "parquet", or "text" (delimiter
 * sniffed from the header), from the extension. "-" is csv. */
const char *ppz_format_of(const char *path);

/* A delimited file a block of rows at a time. encoding NULL: a byte-order
 * mark, else strict UTF-8. delim 0: from the extension, else sniffed. */
typedef struct CsvIn CsvIn;
CsvIn      *csv_open(const char *path, const char *encoding, char delim,
                     char *err, size_t cap);
int         csv_next(CsvIn *c, Table *t, size_t max_rows); /* 1 block, 0 end, -1 */
const char *csv_error(const CsvIn *c);
size_t      csv_width(const CsvIn *c);
uint64_t    csv_bytes_read(const CsvIn *c);   /* of the file, so far */
char        csv_delim(const CsvIn *c);
void        csv_close(CsvIn *c);

/* A whole table from any readable format, chosen by extension. */
int table_read_any(Table *t, const char *path, const char *encoding,
                   char *err, size_t cap);

/* Output in the format the extension names ("-" is CSV on stdout), fed a
 * block at a time. The file appears under its name only when writer_close
 * succeeds; ok=0 abandons it. */
typedef struct Writer Writer;
Writer *writer_open(const char *path, char *const *names, size_t ncols,
                    char *err, size_t cap);
int     writer_rows(Writer *w, const Table *t);
int     writer_close(Writer *w, int ok, char *err, size_t cap);
int     table_write_any(const Table *t, const char *path, char *err, size_t cap);

/* A JSON string: quote, backslash and control characters escaped, UTF-8 as is. */
void    ppz_json_str(Buf *b, const char *s, size_t n);

/* ------------------------------------------------------------- streaming */

/* ppz_stream.c: files bigger than memory, one block of rows at a time.
 *
 *   "PPZS" 0 0 0 0 | block | block | ... | header | header length (8, BE)
 *
 * Each block is a complete single-shot archive; the header is raw LZMA2 over
 * {"columns":[...],"nrows":N,"rows_per_block":R,"blocks":[sizes]}. */
#define PPZ_MAGIC_STREAM "PPZS"

typedef struct {
    unsigned long long rows;
    size_t   blocks;
    size_t   rows_per_block;
    uint64_t bytes;          /* output file size */
} StreamStats;

typedef struct {
    char   **columns;
    size_t   ncols;
    unsigned long long nrows;
    size_t   rows_per_block;
    size_t   nblocks;
    uint64_t *blocks;        /* each block's size */
    uint64_t size;           /* the whole file */
} StreamInfo;

/* After each batch of blocks: blocks and rows so far, archive bytes written,
 * input bytes read, and the input's size (0 when unknown, e.g. stdin). */
typedef void (*StreamProgress)(size_t blocks, unsigned long long rows,
                               uint64_t out_bytes, uint64_t in_bytes,
                               uint64_t in_total);

/* rows 0: sized from budget_gb and the file's own row width. */
int  ppz_stream_compress(const char *src, const char *dst, double budget_gb,
                         size_t rows, int verify, const char *encoding,
                         StreamProgress progress, StreamStats *st,
                         char *err, size_t cap);
int  ppz_stream_restore(const char *src, const char *dst, StreamStats *st,
                        char *err, size_t cap);
int  ppz_stream_info(const char *src, StreamInfo *info, char *err, size_t cap);
void ppz_stream_info_free(StreamInfo *info);

/* ------------------------------------------------------------------- lzma */

/* Raw LZMA2 at preset 9|EXTREME, the one compressor every stream uses,
 * with layout settings tuned to what the stream holds (see ppz_util.c). */
typedef enum { PPZ_XZ_PLAIN, PPZ_XZ_INTS, PPZ_XZ_TEXT } PpzXz;
int ppz_lzma_compress(const uint8_t *in, size_t n, Buf *out);   /* PLAIN */
int ppz_lzma_compress_as(const uint8_t *in, size_t n, Buf *out, PpzXz kind);

/* Compressed length at preset 1, used only to choose between two orderings of
 * the same column. Nothing it produces is stored; the real preset-9 stage is
 * far too slow to run as a decision procedure. */
size_t ppz_lzma_probe_len(const uint8_t *in, size_t n);
int ppz_lzma_decompress(const uint8_t *in, size_t n, Buf *out);

/* ------------------------------------------------------------------- json */

typedef enum {
    JS_NULL, JS_BOOL, JS_NUM, JS_STR, JS_ARR, JS_OBJ
} JsKind;

typedef struct Js Js;
struct Js {
    JsKind  kind;
    double  num;
    int64_t inum;      /* exact value when `is_int`; `num` cannot be trusted */
    int     is_int;    /* the token was a plain integer, parsed with strtoll */
    int     boolean;
    char   *str;       /* decoded, NUL-terminated; for JS_NUM, the token text */
    size_t  len;       /* bytes in str -- a \u0000 inside a string is data */
    Js     *items;     /* array elements / object values */
    char  **keys;      /* object keys */
    size_t  count;
};

/* A double holds only 53 bits of mantissa, and this metadata carries int64
 * warm-start values that routinely exceed that. Reading them back through a
 * double silently rounded 74884171959489212 to ...216 -- a decoder that
 * returns wrong numbers rather than failing. Integer tokens are therefore
 * kept exactly, and js_i64 is the accessor to use for anything that is a
 * value rather than a small count. */
Js  *js_parse(const char *text, size_t len);
Js  *js_parse_prefix(const char *text, size_t len, size_t *used);
void js_free(Js *j);
const Js *js_get(const Js *obj, const char *key);   /* NULL if absent */
long      js_int(const Js *j, long fallback);
int64_t   js_i64(const Js *j, int64_t fallback);

/* ------------------------------------------------------ partial outputs */

/* Outputs are written beside their name and renamed into place at the end.
 * A registered temp file is removed if the program is interrupted (Ctrl-C,
 * or the app's Stop), so a stopped run leaves nothing half-written behind. */
void ppz_tmp_register(const char *path);
void ppz_tmp_forget(const char *path);
void ppz_cleanup_on_signals(void);

/* ---------------------------------------------------------------- threads */

typedef void (*PpzTask)(void *arg);
typedef struct PpzBg PpzBg;

int    ppz_nthreads(void);            /* 1 when serial, else ppz_workers() */
int    ppz_workers(void);             /* PPZ_THREADS, else min(4, cores) */
void   ppz_set_serial(int on);        /* force one thread (streaming mode) */
int    ppz_serial(void);
double ppz_now(void);
void   ppz_trace(const char *what, double since);   /* PPZ_TRACE=1 */
void   ppz_slot_take(void);           /* bound the concurrent xz -9e runs */
void   ppz_slot_give(void);
/* fn(args + i*argsize) for i < n, concurrently; returns when all are done.
 * Leaf work only: a task must not wait on another task. */
void   ppz_parallel(PpzTask fn, void *args, size_t argsize, size_t n);
/* fn(arg) on its own thread now (inline at join time when serial). */
PpzBg *ppz_bg_start(PpzTask fn, void *arg);
void   ppz_bg_join(PpzBg *b);

/* -------------------------------------------------------- derived columns */

/* Numbers inside a text cell that another column of the same row already
 * holds -- `location = "POINT (lon lat)"` beside `longitude` and `latitude`,
 * a date beside its `year` -- are stored as references to that column:
 *
 *     \x01<k>:\x02       an exact copy of candidate column k
 *     \x01<k>:<d>\x02    candidate k rounded to d decimals, half to even
 *
 * A number is `-?[0-9]+(\.[0-9]+)?`, read left to right; one longer than
 * DRV_MAX_TOK bytes is never referenced, which also bounds how far a hostile
 * archive can make a cell grow. Archives that use this carry
 * "derive": [[column, [candidates...]], ...] in their metadata. */
#define DRV_MAX_TOK   64
#define DRV_MAX_CANDS 4

size_t drv_token(const char *s, size_t n, size_t i);     /* length at i, or 0 */
int    drv_is_number(const char *s, size_t n);           /* exactly one token */
int    drv_decimals(const char *s, size_t n);            /* digits after '.' */
/* v (a token with a '.' and more than d decimals, at most DRV_MAX_TOK long)
 * rounded to d decimals into out (DRV_MAX_TOK + 2 bytes). Length, or 0. */
size_t drv_round(const char *v, size_t n, int d, char *out);

/* --------------------------------------------- Stata, SPSS and SAS files */

/* ppz_stat.c, through the ReadStat library in readstat/. An archive of one
 * holds the original bytes and a JSON schema of its labels, formats and
 * notes; table formats are translations of it, and say what they drop. */

/* "dta", "sav", "zsav", "por", "sas7bdat", "xpt" from the extension, else NULL */
const char *ppz_stat_format(const char *path);
const char *ppz_stat_name(const char *fmt);        /* "Stata", ... or NULL */

/* What a text copy of the file cannot hold, counted while reading it. */
typedef struct {
    size_t var_labels, value_labels, user_missing, measures, notes;
    size_t dates, other_dates;      /* columns: written as ISO / as numbers */
    size_t tagged, tagged_cols;     /* .a-.z cells, and the columns they are in */
    int    file_label;
} StatLoss;

/* The file in data[0..n) as a table of text cells: numbers shortest
 * round-trip, dates as ISO text, system missing empty, tagged missing ".a".
 * `schema` (may be NULL) receives the JSON schema, `loss` the counts. */
int  stat_read(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
               Table *t, Buf *schema, StatLoss *loss, char *err, size_t cap);
void stat_loss_print(FILE *f, const StatLoss *l, const char *src_fmt, const char *dst);

int  ppz_encode_original(const uint8_t *data, size_t n, const char *fmt,
                         const char *encoding, const Buf *schema, Buf *out);
/* 1: an archive of an original file (orig filled when non-NULL, fmt set,
 * *meta the parsed metadata when meta is non-NULL), 0: a table archive,
 * -1: not an archive or damaged. */
int  ppz_original(const uint8_t *blob, size_t n, Buf *orig, char *fmt, size_t fcap,
                  Js **meta);

/* --------------------------------------------------------------- decoding */

/* Decode an archive into `out`. Returns 0 on success. */
int ppz_decode(const uint8_t *blob, size_t n, Table *out);

/* --------------------------------------------------------------- encoding */

/* Encode a table, in one pass (see ppz_encode in ppz_encode.c). Column
 * names must be UTF-8 -- the readers guarantee it -- or this returns -1. */
int ppz_encode(const Table *t, Buf *out);

#endif /* PPZ_H */
