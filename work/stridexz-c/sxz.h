/* StrideXZ -- a table codec that arranges bytes so xz can copy them cheaply.
 *
 * The name is the thesis. xz pays for a copy by how far back it reaches, and
 * it keeps four "bookmarks" at the last four distances it used; a repeat that
 * lands on a bookmark costs a handful of bits where a fresh far-away one costs
 * forty. So the job of a table codec is not to compress -- xz is very good at
 * that -- but to hand xz a layout whose repeats are long, regular, and close
 * together. Everything here serves that: constant STRIDE, grouped
 * vocabularies, byte planes, and rows ordered so a column collapses into runs.
 *
 * This C implementation reads and writes the same container as the Python
 * reference in ../stridexz/. Neither is a translation of the other's bytes --
 * they are two implementations of one documented format, and each must be able
 * to decode the other's archives. tests/ checks exactly that.
 *
 * Only the transforms that SURVIVED measurement are here. Interleaving
 * correlated columns, templating numbers out of text, row chunking,
 * move-to-front and Z-ordering were all built, measured, and lost; the record
 * is in OUT/results/stridexz-ideas.txt. Porting them would be porting known
 * dead weight.
 */
#ifndef SXZ_H
#define SXZ_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define SXZ_MAGIC "XZL1"
#define SXZ_MAGIC_LEN 4

/* Refuse anything that would make an allocation the machine cannot serve.
 * The decoder reads files other people made; every count that comes off the
 * wire is checked against these before it is used to size anything. */
#define SXZ_MAX_COLS      1000000
#define SXZ_MAX_ROWS      2000000000ULL
#define SXZ_MAX_PAYLOAD   (8ULL << 30)

/* ------------------------------------------------------------------ bytes */

typedef struct {
    char  *p;
    size_t len;
    size_t cap;
} Buf;

void  buf_init(Buf *b);
int   buf_reserve(Buf *b, size_t need);
int   buf_put(Buf *b, const void *data, size_t n);
int   buf_putc(Buf *b, char c);
int   buf_puts(Buf *b, const char *s);
int   buf_printf(Buf *b, const char *fmt, ...);
/* NOTE: this zeroes len as well as freeing p. Capture the length first if you
 * are about to compare against it. */
void  buf_free(Buf *b);

/* ----------------------------------------------------------------- strings */

/* A cell. Explicitly counted, never NUL-terminated, because a CSV cell may
 * legitimately contain a NUL byte and truncating there would be data loss. */
typedef struct {
    const char *s;
    size_t      n;
} Str;

int  str_eq(Str a, Str b);
/* FNV-1a. Used only for hash tables that never affect output ordering --
 * see the note on determinism in sxz_encode.c. */
uint64_t str_hash(Str a);

typedef struct {
    Str   *v;
    size_t n, cap;
} StrVec;

void strvec_init(StrVec *v);
int  strvec_push(StrVec *v, Str s);
void strvec_free(StrVec *v);

/* ------------------------------------------------------------------ table */

/* A chain of fixed blocks. Unlike a Buf it NEVER moves what it has already
 * handed out, which is the whole reason it exists: cells can be pointed at the
 * moment they are produced instead of being recorded as offsets and fixed up
 * afterwards. The decoder used to carry an offset table of one entry per cell
 * -- 320 MB on a 20-million-cell table -- purely to survive a realloc. */
typedef struct ArenaBlock ArenaBlock;

typedef struct {
    ArenaBlock *head;
} Arena;

void  arena_init(Arena *a);
char *arena_alloc(Arena *a, size_t n);
int   arena_put(Arena *a, const void *data, size_t n, Str *out);
void  arena_free(Arena *a);

typedef struct {
    StrVec  names;     /* ncols column names */
    StrVec *cols;      /* ncols vectors, each nrows long: cols[c].v[r] */
    size_t  ncols;
    size_t  nrows;
    Arena   arena;     /* copied cells live here */
    Buf     source;    /* raw bytes owned by the table; unquoted cells point in */
} Table;

void table_init(Table *t);
void table_free(Table *t);

/* Read comma-separated text. Refuses anything it cannot parse rather than
 * guessing -- the C reader accepting a file the Python one rejects (or the
 * reverse) is a divergence that no round-trip check downstream can see. */
int  table_read_csv(const char *path, Table *out, char **err);
/* Write RFC4180-ish CSV: what `restore` produces. */
int  table_write_csv(const Table *t, FILE *fh);

/* -------------------------------------------------------------------- xz */

/* preset 9 | EXTREME, matching the Python reference. lc/lp/pb are the literal
 * context, literal position and position bits; passing -1 keeps the default.
 * The defaults are tuned for English prose, which packed table columns are
 * not, so the encoder stores which setting it used. */
typedef struct {
    int lc, lp, pb;
} XzTune;

extern const XzTune XZ_DEFAULT_TUNE;

int xz_compress(const char *data, size_t n, XzTune tune, Buf *out);
int xz_decompress(const char *data, size_t n, size_t limit, Buf *out);

/* Incremental compression. The point is that the encoder never holds the
 * uncompressed payload: column blobs are fed in as they are built and freed
 * immediately. The output is byte-for-byte what xz_compress would produce from
 * the concatenation -- same bytes, same filter, just handed over in
 * instalments -- so streaming costs nothing in compression. */
typedef struct XzEnc XzEnc;
XzEnc *xz_enc_begin(XzTune tune);
int    xz_enc_write(XzEnc *e, const char *data, size_t n);
/* Finishes the stream, appends it to `out`, and frees the encoder. */
int    xz_enc_finish(XzEnc *e, Buf *out);
void   xz_enc_abort(XzEnc *e);
/* preset 1, used only to rank candidate encodings against each other.
 * Returns the compressed size, or (size_t)-1 on failure. */
size_t xz_probe(const char *data, size_t n);

/* ------------------------------------------------------------------- json */

/* A deliberately small parser for one known schema. It exists because the
 * container header is JSON and both implementations must read it; it is not
 * general-purpose and does not try to be. */
typedef enum {
    JS_NULL, JS_BOOL, JS_NUM, JS_STR, JS_ARR, JS_OBJ
} JsonKind;

typedef struct Json Json;
struct Json {
    JsonKind kind;
    int      boolean;
    int64_t  num;
    Str      str;          /* points into the decoded-string arena */
    Json   **items;        /* JS_ARR and JS_OBJ values */
    Str     *keys;         /* JS_OBJ keys */
    size_t   count;
};

typedef struct {
    Json  *root;
    void  *pool;           /* opaque; freed by json_free */
} JsonDoc;

int   json_parse(const char *text, size_t n, JsonDoc *out, char **err);
void  json_free(JsonDoc *d);
const Json *json_get(const Json *obj, const char *key);
/* Emit a JSON string with ensure_ascii semantics, so the header bytes match
 * what Python's json.dumps(..., ensure_ascii=True) produces. */
int   json_emit_string(Buf *b, Str s);

/* ---------------------------------------------------------------- codec */

typedef struct {
    int use_pool;
    int use_fixed;
    int use_planes;
    int use_dict;
    int use_sort;
    int guard;         /* try with and without the row sort, keep the smaller */
    XzTune tune;
    /* 0 = read the whole table at once. Otherwise compress this many rows at
     * a time, so peak memory is one block rather than one file. Column-major
     * layout cannot stream the way xz does -- you cannot finish a column until
     * you have seen every row -- so row groups are the only way off the
     * "whole table in RAM" floor. They cost a little compression, because the
     * column ordering and the row reorder then only see one block at a time. */
    size_t block_rows;
} SxzOptions;

extern const SxzOptions SXZ_DEFAULTS;

int sxz_encode(const Table *t, SxzOptions opt, Buf *out, char **err);
/* Row-group encode straight from a file, never holding more than one block. */
int sxz_encode_file(const char *path, SxzOptions opt, Buf *out,
                    size_t *nrows_out, size_t *ncols_out, char **err);
int sxz_decode(const char *blob, size_t n, Table *out, char **err);

/* Decode ONE unit's blob back into cells. Exposed so the encoder can verify a
 * row group against its source the moment it is built, while the source rows
 * are still in hand -- under --rows the whole table is never resident, so
 * there is nothing left to compare against afterwards. Sharing this with the
 * decoder is the point: a verifier with its own copy of the unpacking logic
 * would agree with the encoder's bugs. */
int sxz_unpack_unit(const char *kind, int w, int64_t nsym, const char *blob,
                    size_t n, size_t nrows, Arena *arena, StrVec *dst);

/* -------------------------------------------------------------- csv reader */

/* Incremental reader over an mmap'd CSV. `table_read_csv` is this with one
 * unbounded block. */
/* A bounded read window, NOT an mmap.
 *
 * Mapping the file and letting unquoted cells point into it is elegant and
 * costs no copy -- and it makes peak RSS track the SIZE OF THE FILE, because
 * every page touched stays resident. Measured: a 1.36 GB input peaked at
 * 2.64 GB even with row groups, and madvise(MADV_DONTNEED) did not reclaim it
 * on macOS. A fixed window makes the number honest: memory is the window plus
 * one block, whatever the file weighs. */
typedef struct {
    int     fd;
    Buf     win;         /* the window; cells point into it */
    size_t  pos;         /* parse position within win */
    int     eof;
    size_t  target;      /* how much to keep buffered */
    StrVec  names;
    size_t  ncols;
} CsvReader;

/* Bytes buffered at once in the streaming path. Rows must fit within it. */
#define SXZ_WINDOW (48u << 20)

int  csv_open(const char *path, CsvReader *r, char **err);
/* Reads up to `max` rows into `out` (which must be table_init'd; its arena and
 * column vectors are reused). Sets *got to the number actually read; 0 means
 * end of file. */
int  csv_read_block(CsvReader *r, size_t max, Table *out, size_t *got,
                    char **err);
void csv_close(CsvReader *r);

/* --------------------------------------------------------------- layout */

/* Column order: vocabulary-sharing columns made adjacent, so the second one
 * copies from the first at a short distance instead of re-establishing the
 * same alphabet from scratch megabytes away. Fills order[0..ncols-1]. */
void layout_pool_order(const Table *t, size_t *order);
/* Row order: lexicographic by every column, lowest cardinality first. Sorting
 * by one column collapses that column and shuffles the rest, which is why the
 * caller must measure and be willing to decline. Fills perm[0..nrows-1] with
 * the original row index of each new row. */
void layout_sort_rows(const Table *t, size_t *perm);

#endif /* SXZ_H */
