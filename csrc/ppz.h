/* Polypress in C -- shared declarations.
 *
 * This is the standalone binary, not the ctypes accelerator. tcz.c stays as
 * it is: it is loaded by caccel.py to speed up the Python codec, and it only
 * ever held four hot loops. This tree is the whole codec, so that a
 * researcher can be handed one file that runs with nothing installed. That
 * -- not speed -- is why it exists. Profiling put liblzma at 62-100% of
 * encode time, so C buys ~1.0-1.15x on the tables the compression win lives
 * on; what it buys is a program with no Python and no numpy behind it.
 *
 * The contract with the Python implementation is byte-identical output. The
 * Python version is the oracle, and every stage here is diffed against it.
 */

#ifndef PPZ_H
#define PPZ_H

#include <stddef.h>
#include <stdint.h>

/* ------------------------------------------------------------- containers */

#define PPZ_MAGIC      "PPZ1"   /* modelled encoding */
#define PPZ_MAGIC_V0   "FAST"   /* pre-rename archives still open */
#define PPZ_MAGIC_RAW_XZ "PPZX" /* whole table, plain xz */
#define PPZ_MAGIC_RAW_BZ "PPZB" /* whole table, plain bzip2 */

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

/* CSV. read_csv accepts what dtz accepts: ragged rows are padded or clipped
 * to the header width, which is the same rule the Python reader uses. */
int  table_read_csv(Table *t, const char *path);
int  table_write_csv(const Table *t, const char *path);
int  table_write_csv_buf(const Table *t, Buf *out);

/* ------------------------------------------------------------------- lzma */

/* Raw LZMA2 at preset 9|EXTREME -- the exact filter chain fast.py uses.
 * Verified byte-identical against Python's lzma module on liblzma 5.4.3 and
 * 5.8.3, which is what makes a byte-identical port possible at all. */
int ppz_lzma_compress(const uint8_t *in, size_t n, Buf *out);
int ppz_lzma_decompress(const uint8_t *in, size_t n, Buf *out);
int ppz_bz2_decompress(const uint8_t *in, size_t n, Buf *out);

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
    char   *str;       /* decoded, NUL-terminated */
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
void js_free(Js *j);
const Js *js_get(const Js *obj, const char *key);   /* NULL if absent */
long      js_int(const Js *j, long fallback);
int64_t   js_i64(const Js *j, int64_t fallback);

/* --------------------------------------------------------------- decoding */

/* Decode any Polypress container into `out`. Returns 0 on success. */
int ppz_decode(const uint8_t *blob, size_t n, Table *out);

/* --------------------------------------------------------------- encoding */

/* Encode a table into the modelled container. Byte-identical to fast.encode's
 * modelled path -- the plain-fallback candidates are the Python CLI's job for
 * now, so this always writes PPZ1. */
int ppz_encode(const Table *t, Buf *out);

#endif /* PPZ_H */
