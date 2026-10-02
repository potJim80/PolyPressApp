/* Bounded-memory compression for files larger than RAM.
 *
 * The single-shot path holds the whole table. This splits it into row blocks,
 * compresses each independently, and concatenates them behind an index, so
 * peak memory is one block, not one file, and it is settable.
 *
 * The cost is real: the cross-column reordering only sees correlations
 * *inside* a block, so smaller blocks compress slightly worse. Measure with
 * --rows before choosing a small one.
 *
 * Blocks are independent, so up to ppz_workers() of them are encoded at once
 * (each one serial inside), and the budget is split between them.
 */

#include "ppz.h"

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* Peak memory per block with verification on: a fixed part (the xz -9e
 * working set and friends) plus a multiple of the block's own bytes and
 * cells. Checked 2026-09-29 on the heaviest suite table, l_chicago_permits:
 * --budget 0.25 peaked at 242 MB and 0.5 at 497 MB; l_nndss_full 171 and 304,
 * l_weblog_big 176 and 206. Re-measure if the encoder's working set changes. */
#define FIXED_MB        160.0
#define PER_BYTE        14.0
#define PER_CELL        64.0
#define NO_VERIFY_SAVES 0.35     /* verification's share of the per-row cost */
#define MIN_ROWS        1000
#define MAX_ROWS        2000000
#define SAMPLE_ROWS     2000
#define DEFAULT_ROWS    100000   /* when the input cannot be sampled (stdin) */

static void seterr(char *err, size_t cap, const char *fmt, ...)
{
    if (!err || !cap) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
}

static int tables_equal(const Table *a, const Table *b)
{
    if (a->ncols != b->ncols || a->nrows != b->nrows) return 0;
    for (size_t j = 0; j < a->ncols; j++)
        if (strcmp(a->names[j], b->names[j])) return 0;
    size_t n = a->nrows * a->ncols;
    for (size_t k = 0; k < n; k++) {
        Str x = a->cells[k], y = b->cells[k];
        if (x.n != y.n || (x.n && memcmp(x.p, y.p, x.n))) return 0;
    }
    return 1;
}

/* Rows per block that keep peak memory near the budget, from the file's own
 * first rows: a 209-column survey row and a 5-column sensor row differ by two
 * orders of magnitude, so a fixed guess is wrong for one of them. */
static size_t plan_rows(const char *src, const char *encoding, double budget_gb,
                        int verify, char *err, size_t cap)
{
    if (!strcmp(src, "-")) return DEFAULT_ROWS;
    CsvIn *c = csv_open(src, encoding, 0, err, cap);
    if (!c) return 0;
    Table t;
    int r = csv_next(c, &t, SAMPLE_ROWS);
    if (r < 0) { seterr(err, cap, "%s", csv_error(c)); csv_close(c); return 0; }
    double bytes = 0, rows = r > 0 ? (double)t.nrows : 0, cols = r > 0 ? (double)t.ncols : 0;
    if (r > 0)
        for (size_t k = 0; k < t.nrows * t.ncols; k++) bytes += (double)t.cells[k].n + 1;
    if (r > 0) table_free(&t);
    csv_close(c);
    if (rows < 1) return MIN_ROWS;

    double per_row = PER_BYTE * (bytes / rows) + PER_CELL * cols;
    if (!verify) per_row *= 1.0 - NO_VERIFY_SAVES;
    double room = budget_gb * 1024.0 * 1024.0 * 1024.0 - FIXED_MB * 1024.0 * 1024.0;
    double n = room / per_row;
    if (n < MIN_ROWS) n = MIN_ROWS;
    if (n > MAX_ROWS) n = MAX_ROWS;
    return (size_t)n;
}

static int write_all(FILE *f, const void *p, size_t n)
{
    return fwrite(p, 1, n, f) == n ? 0 : -1;
}

/* One block's work: encode it, and check it decodes back to itself. Blocks
 * are independent, so several run at once; each is serial inside, because
 * the parallelism is here now and the memory budget is per block. */
typedef struct {
    Table t;
    Buf   blob;
    int   rc;                /* 0 ok, 1 encode failed, 2 verification failed */
    int   verify;
} BlockJob;

static void block_task(void *arg)
{
    BlockJob *j = arg;
    /* Serial for this block only. The caller's own thread runs blocks too
     * (ppz_parallel puts it to work), so the flag is put back afterwards:
     * left set, it made every batch after the first run one block at a
     * time -- NEMSIS (1,475 blocks) used 0.76 of a core for 19.5 minutes. */
    int was = ppz_serial();
    ppz_set_serial(1);
    buf_init(&j->blob);
    j->rc = 0;
    if (ppz_encode(&j->t, &j->blob)) j->rc = 1;
    else if (j->verify) {
        Table back;
        int ok = ppz_decode(j->blob.data, j->blob.len, &back) == 0;
        if (ok) { ok = tables_equal(&j->t, &back); table_free(&back); }
        if (!ok) j->rc = 2;
    }
    ppz_set_serial(was);
}

/* Compressing while the file is read. One thread reads blocks, `par`
 * workers encode them, and the caller writes them out in order -- so the
 * reader never waits for a batch to finish and a slow block holds up only
 * the writer. Until 2026-10-01 it went in rounds: read four blocks, encode
 * the four, write, repeat.
 *
 * A block lives in one slot from read to write. Its state only moves
 * forward (empty -> read -> busy -> done -> empty), and each step belongs to
 * one party: the reader fills empty slots, a worker takes read ones, the
 * writer empties done ones in sequence. Blocks are cut and encoded exactly as
 * before, so the archive's bytes do not depend on any of this. */
enum { SL_EMPTY, SL_READ, SL_BUSY, SL_DONE };

typedef struct {
    BlockJob job;
    int      state;
    uint64_t in_bytes;       /* of the input, read when this block was */
} Slot;

typedef struct {
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    Slot   *slot;
    size_t  nslot;
    size_t  nread;           /* blocks read, which is the next block's number */
    size_t  nclaimed;        /* the next block a worker takes */
    int     eof;             /* the reader is finished, for any reason */
    int     stop;            /* give up: the writer failed */
    int     read_failed;
    char    rerr[512];
    CsvIn  *c;
    size_t  rows;
    int     verify;
} Pipe;

static void *reader_main(void *arg)
{
    Pipe *p = arg;
    for (;;) {
        pthread_mutex_lock(&p->mu);
        Slot *s = &p->slot[p->nread % p->nslot];
        while (!p->stop && s->state != SL_EMPTY) pthread_cond_wait(&p->cv, &p->mu);
        int stop = p->stop;
        pthread_mutex_unlock(&p->mu);
        if (stop) break;
        /* the slot is empty, so it is the reader's until marked read */
        int r = csv_next(p->c, &s->job.t, p->rows);
        pthread_mutex_lock(&p->mu);
        if (r <= 0) {
            if (r < 0) {
                p->read_failed = 1;
                snprintf(p->rerr, sizeof(p->rerr), "%s", csv_error(p->c));
            }
            p->eof = 1;
            pthread_cond_broadcast(&p->cv);
            pthread_mutex_unlock(&p->mu);
            break;
        }
        s->job.verify = p->verify;
        s->in_bytes = csv_bytes_read(p->c);
        s->state = SL_READ;
        p->nread++;
        pthread_cond_broadcast(&p->cv);
        pthread_mutex_unlock(&p->mu);
    }
    pthread_mutex_lock(&p->mu);
    p->eof = 1;
    pthread_cond_broadcast(&p->cv);
    pthread_mutex_unlock(&p->mu);
    return NULL;
}

static void *worker_main(void *arg)
{
    Pipe *p = arg;
    for (;;) {
        pthread_mutex_lock(&p->mu);
        while (!p->stop && p->nclaimed >= p->nread && !p->eof)
            pthread_cond_wait(&p->cv, &p->mu);
        if (p->stop || p->nclaimed >= p->nread) {   /* stopped, or all done */
            pthread_mutex_unlock(&p->mu);
            break;
        }
        Slot *s = &p->slot[p->nclaimed++ % p->nslot];
        s->state = SL_BUSY;
        pthread_mutex_unlock(&p->mu);
        block_task(&s->job);
        pthread_mutex_lock(&p->mu);
        s->state = SL_DONE;
        pthread_cond_broadcast(&p->cv);
        pthread_mutex_unlock(&p->mu);
    }
    return NULL;
}

int ppz_stream_compress(const char *src, const char *dst, double budget_gb,
                        size_t rows, int verify, const char *encoding,
                        StreamProgress progress, StreamStats *st,
                        char *err, size_t cap)
{
    memset(st, 0, sizeof(*st));
    /* Blocks run `par` at a time, so each gets a share of the budget. */
    int par = ppz_workers();
    if (!rows) {
        rows = plan_rows(src, encoding, budget_gb / par, verify, err, cap);
        if (!rows) return -1;
    }
    CsvIn *c = csv_open(src, encoding, 0, err, cap);
    if (!c) return -1;
    struct stat sst;
    uint64_t in_total = (strcmp(src, "-") && !stat(src, &sst)) ? (uint64_t)sst.st_size : 0;

    size_t n = strlen(dst) + 16;
    char *tmp = malloc(n);
    if (!tmp) { csv_close(c); return -1; }
    snprintf(tmp, n, "%s.partXXXXXX", dst);
    int fd = mkstemp(tmp);
    if (fd < 0) {
        seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
        free(tmp); csv_close(c);
        return -1;
    }
    ppz_tmp_register(tmp);
    mode_t um = umask(0);
    umask(um);
    fchmod(fd, 0666 & ~um);
    FILE *out = fdopen(fd, "wb");

    int rc = -1;
    uint64_t *sizes = NULL;
    size_t nsizes = 0, capsz = 0;
    char **cols = NULL;
    size_t ncols = 0;
    uint64_t written = 8;
    Buf hdr, hz;
    buf_init(&hdr); buf_init(&hz);
    Pipe pp;
    memset(&pp, 0, sizeof(pp));
    pthread_mutex_init(&pp.mu, NULL);
    pthread_cond_init(&pp.cv, NULL);
    /* par encoding, one being read, one finished and waiting its turn */
    pp.nslot = (size_t)par + 2;
    pp.slot = calloc(pp.nslot, sizeof(Slot));
    pp.c = c;
    pp.rows = rows;
    pp.verify = verify;
    pthread_t rth, *wth = calloc((size_t)par, sizeof(pthread_t));
    int started = 0, reader_up = 0;
    if (!pp.slot || !wth) goto done;
    if (write_all(out, PPZ_MAGIC_STREAM "\0\0\0\0", 8)) goto io_fail;
    if (pthread_create(&rth, NULL, reader_main, &pp)) goto done;
    reader_up = 1;
    for (; started < par; started++)
        if (pthread_create(&wth[started], NULL, worker_main, &pp)) break;
    if (!started) goto done;

    double last_report = 0;
    for (size_t w = 0; ; w++) {
        Slot *s = &pp.slot[w % pp.nslot];
        pthread_mutex_lock(&pp.mu);
        while (!(w < pp.nread && s->state == SL_DONE) && !(pp.eof && w >= pp.nread))
            pthread_cond_wait(&pp.cv, &pp.mu);
        int finished = w >= pp.nread;          /* and so eof */
        pthread_mutex_unlock(&pp.mu);
        if (finished) {
            if (pp.read_failed) { seterr(err, cap, "%s", pp.rerr); goto done; }
            break;
        }
        BlockJob *j = &s->job;
        if (j->rc) {
            seterr(err, cap, j->rc == 1 ? "block %zu could not be encoded"
                                        : "block %zu failed verification -- nothing written",
                   nsizes + 1);
            goto done;
        }
        if (!cols) {
            ncols = j->t.ncols;
            cols = calloc(ncols ? ncols : 1, sizeof(char *));
            if (!cols) goto done;
            for (size_t k = 0; k < ncols; k++) cols[k] = strdup(j->t.names[k]);
        }
        if (write_all(out, j->blob.data, j->blob.len)) goto io_fail;
        if (nsizes == capsz) {
            capsz = capsz ? capsz * 2 : 64;
            uint64_t *ns = realloc(sizes, capsz * sizeof(uint64_t));
            if (!ns) goto done;
            sizes = ns;
        }
        sizes[nsizes++] = j->blob.len;
        written += j->blob.len;
        st->rows += j->t.nrows;
        uint64_t in_bytes = s->in_bytes;
        buf_free(&j->blob);
        table_free(&j->t);
        pthread_mutex_lock(&pp.mu);
        s->state = SL_EMPTY;
        pthread_cond_broadcast(&pp.cv);
        pthread_mutex_unlock(&pp.mu);
        /* a line four times a second is plenty for a progress bar */
        double now = ppz_now();
        if (progress && now - last_report >= 0.25) {
            progress(nsizes, st->rows, written, in_bytes, in_total);
            last_report = now;
        }
    }
    if (progress) progress(nsizes, st->rows, written, in_total ? in_total : 0, in_total);

    /* the index */
    buf_put(&hdr, "{\"columns\":[", 12);
    for (size_t j = 0; j < ncols; j++) {
        if (j) buf_putc(&hdr, ',');
        ppz_json_str(&hdr, cols[j], strlen(cols[j]));
    }
    char num[64];
    snprintf(num, sizeof(num), "],\"nrows\":%llu,\"rows_per_block\":%zu,\"blocks\":[",
             st->rows, rows);
    buf_put(&hdr, num, strlen(num));
    for (size_t i = 0; i < nsizes; i++) {
        snprintf(num, sizeof(num), i ? ",%llu" : "%llu", (unsigned long long)sizes[i]);
        buf_put(&hdr, num, strlen(num));
    }
    buf_put(&hdr, "]}", 2);
    if (ppz_lzma_compress(hdr.data, hdr.len, &hz)) goto done;
    uint8_t lenb[8];
    for (int i = 0; i < 8; i++) lenb[i] = (uint8_t)((uint64_t)hz.len >> (56 - 8 * i));
    if (write_all(out, hz.data, hz.len) || write_all(out, lenb, 8)) goto io_fail;
    written += hz.len + 8;
    if (fclose(out)) { out = NULL; goto io_fail; }
    out = NULL;
    if (rename(tmp, dst)) { seterr(err, cap, "cannot write %s: %s", dst, strerror(errno)); goto done; }
    st->blocks = nsizes;
    st->rows_per_block = rows;
    st->bytes = written;
    rc = 0;
    goto done;

io_fail:
    seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
done:
    /* stop the threads before anything they touch is freed */
    pthread_mutex_lock(&pp.mu);
    pp.stop = 1;
    pthread_cond_broadcast(&pp.cv);
    pthread_mutex_unlock(&pp.mu);
    if (reader_up) pthread_join(rth, NULL);
    for (int i = 0; i < started; i++) pthread_join(wth[i], NULL);
    for (size_t i = 0; pp.slot && i < pp.nslot; i++) {
        buf_free(&pp.slot[i].job.blob);
        table_free(&pp.slot[i].job.t);
    }
    free(pp.slot);
    free(wth);
    pthread_mutex_destroy(&pp.mu);
    pthread_cond_destroy(&pp.cv);
    if (out) fclose(out);
    if (rc) unlink(tmp);
    ppz_tmp_forget(tmp);
    free(tmp);
    csv_close(c);
    free(sizes);
    for (size_t j = 0; j < ncols; j++) free(cols[j]);
    free(cols);
    buf_free(&hdr); buf_free(&hz);
    return rc;
}

/* ------------------------------------------------------------ reading */

void ppz_stream_info_free(StreamInfo *h)
{
    for (size_t j = 0; j < h->ncols; j++) free(h->columns[j]);
    free(h->columns);
    free(h->blocks);
    memset(h, 0, sizeof(*h));
}

/* The index, treated as hostile: every size is checked against the file
 * before anything is allocated from it. */
static int read_index(FILE *f, const char *src, StreamInfo *h, char *err, size_t cap)
{
    memset(h, 0, sizeof(*h));
    #define BAD() do { seterr(err, cap, "cannot read %s -- it is not a " \
                       "Polypress archive, or it is damaged.", src); goto fail; } while (0)
    Buf hb, hj;
    buf_init(&hb); buf_init(&hj);
    Js *j = NULL;
    struct stat stt;
    if (fstat(fileno(f), &stt)) BAD();
    uint64_t size = (uint64_t)stt.st_size;
    uint8_t head[8], tail[8];
    if (size < 16 || fseeko(f, 0, SEEK_SET) || fread(head, 1, 8, f) != 8
        || memcmp(head, PPZ_MAGIC_STREAM, 4)) BAD();
    if (fseeko(f, -8, SEEK_END) || fread(tail, 1, 8, f) != 8) BAD();
    uint64_t hl = 0;
    for (int i = 0; i < 8; i++) hl = (hl << 8) | tail[i];
    if (hl > size - 16) BAD();
    if (fseeko(f, (off_t)(size - 8 - hl), SEEK_SET)) BAD();
    buf_need(&hb, hl ? hl : 1);
    if (fread(hb.data, 1, hl, f) != hl) BAD();
    hb.len = hl;
    if (ppz_lzma_decompress(hb.data, hb.len, &hj)) BAD();
    j = js_parse((const char *)hj.data, hj.len);
    const Js *jc = js_get(j, "columns"), *jb = js_get(j, "blocks");
    const Js *jn = js_get(j, "nrows"), *jr = js_get(j, "rows_per_block");
    if (!jc || jc->kind != JS_ARR || !jb || jb->kind != JS_ARR || !jn || jn->kind != JS_NUM) BAD();
    h->ncols = jc->count;
    h->columns = calloc(h->ncols ? h->ncols : 1, sizeof(char *));
    if (!h->columns) BAD();
    for (size_t i = 0; i < h->ncols; i++) {
        if (jc->items[i].kind != JS_STR || !jc->items[i].str) BAD();
        h->columns[i] = strdup(jc->items[i].str);
    }
    h->nblocks = jb->count;
    h->blocks = calloc(h->nblocks ? h->nblocks : 1, sizeof(uint64_t));
    if (!h->blocks) BAD();
    uint64_t total = 0, room = size - 16 - hl;
    for (size_t i = 0; i < h->nblocks; i++) {
        int64_t v = js_i64(&jb->items[i], -1);
        if (jb->items[i].kind != JS_NUM || v < 0 || (uint64_t)v > room - total) BAD();
        h->blocks[i] = (uint64_t)v;
        total += (uint64_t)v;
    }
    int64_t nr = js_i64(jn, -1);
    if (nr < 0) BAD();
    h->nrows = (unsigned long long)nr;
    h->rows_per_block = (size_t)js_int(jr, 0);
    h->size = size;
    js_free(j);
    buf_free(&hb); buf_free(&hj);
    return 0;
fail:
    js_free(j);
    buf_free(&hb); buf_free(&hj);
    ppz_stream_info_free(h);
    return -1;
    #undef BAD
}

int ppz_stream_info(const char *src, StreamInfo *h, char *err, size_t cap)
{
    FILE *f = fopen(src, "rb");
    if (!f) { seterr(err, cap, "cannot read %s: %s", src, strerror(errno)); return -1; }
    int rc = read_index(f, src, h, err, cap);
    fclose(f);
    return rc;
}

int ppz_stream_restore(const char *src, const char *dst, StreamStats *st,
                       char *err, size_t cap)
{
    memset(st, 0, sizeof(*st));
    FILE *f = fopen(src, "rb");
    if (!f) { seterr(err, cap, "cannot read %s: %s", src, strerror(errno)); return -1; }
    StreamInfo h;
    if (read_index(f, src, &h, err, cap)) { fclose(f); return -1; }
    Writer *w = writer_open(dst, h.columns, h.ncols, err, cap);
    if (!w) { ppz_stream_info_free(&h); fclose(f); return -1; }

    int ok = 1;
    Buf blob;
    buf_init(&blob);
    if (fseeko(f, 8, SEEK_SET)) ok = 0;
    for (size_t i = 0; ok && i < h.nblocks; i++) {
        buf_free(&blob);
        buf_need(&blob, h.blocks[i] ? h.blocks[i] : 1);
        if (fread(blob.data, 1, h.blocks[i], f) != h.blocks[i]) { ok = 0; break; }
        blob.len = h.blocks[i];
        Table t;
        if (ppz_decode(blob.data, blob.len, &t)) { ok = 0; break; }
        /* every block must be the table the index describes */
        int same = t.ncols == h.ncols;
        for (size_t j = 0; same && j < t.ncols; j++)
            if (strcmp(t.names[j], h.columns[j])) same = 0;
        if (!same || writer_rows(w, &t)) { table_free(&t); ok = 0; break; }
        st->rows += t.nrows;
        table_free(&t);
    }
    buf_free(&blob);
    fclose(f);
    if (!ok) {
        seterr(err, cap, "cannot read %s -- it is not a Polypress archive, or "
               "it is damaged.", src);
        writer_close(w, 0, NULL, 0);
        ppz_stream_info_free(&h);
        return -1;
    }
    st->blocks = h.nblocks;
    st->rows_per_block = h.rows_per_block;
    ppz_stream_info_free(&h);
    if (writer_close(w, 1, err, cap)) return -1;
    struct stat so;
    if (strcmp(dst, "-") && !stat(dst, &so)) st->bytes = (uint64_t)so.st_size;
    return 0;
}
