/* polypress -- the command line.
 *
 *     polypress compress data.csv              -> data.csv.ppz
 *     polypress restore  data.csv.ppz          -> data.csv
 *     polypress restore  data.csv.ppz -o x.json
 *     polypress info     data.csv.ppz
 *     polypress convert  data.tsv data.jsonl
 *
 * For a file too large to hold in memory, compress a block at a time with a
 * settable memory budget; restore and info recognise such archives by
 * themselves:
 *
 *     polypress stream-compress big.csv --budget 1.0
 *
 * Restoring writes whatever format the output extension asks for, so it
 * doubles as a converter. Compression decodes the archive and compares every
 * cell before anything is written.
 */

#include "ppz.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define PPZ_VERSION "0.3.0"

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

/* 1,234 B / 12.3 KB / 4.5 MB / 1.2 GB */
static const char *human(double n, char *out, size_t cap)
{
    const char *u[] = { "B", "KB", "MB", "GB" };
    int i = 0;
    while (n >= 1024.0 && i < 3) { n /= 1024.0; i++; }
    char digits[64];
    snprintf(digits, sizeof(digits), i ? "%.1f" : "%.0f", n);
    /* thousands separators on the integer part */
    char *dot = strchr(digits, '.');
    size_t ilen = dot ? (size_t)(dot - digits) : strlen(digits);
    char grouped[96];
    size_t g = 0;
    for (size_t k = 0; k < ilen; k++) {
        if (k && (ilen - k) % 3 == 0) grouped[g++] = ',';
        grouped[g++] = digits[k];
    }
    grouped[g] = 0;
    snprintf(out, cap, "%s%s %s", grouped, dot ? dot : "", u[i]);
    return out;
}

static const char *commas(unsigned long long v, char *out, size_t cap)
{
    char d[32];
    snprintf(d, sizeof(d), "%llu", v);
    size_t n = strlen(d), g = 0;
    for (size_t k = 0; k < n && g + 2 < cap; k++) {
        if (k && (n - k) % 3 == 0) out[g++] = ',';
        out[g++] = d[k];
    }
    out[g] = 0;
    return out;
}

static int read_file(const char *path, Buf *out, char *err, size_t cap)
{
    buf_init(out);
    struct stat st;
    if (stat(path, &st) == 0 && S_ISDIR(st.st_mode)) {
        snprintf(err, cap, "%s is a directory, not a file", path);
        return -1;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        if (errno == ENOENT) snprintf(err, cap, "no such file: %s", path);
        else if (errno == EACCES) snprintf(err, cap, "not allowed to read %s", path);
        else snprintf(err, cap, "cannot read %s: %s", path, strerror(errno));
        return -1;
    }
    uint8_t chunk[65536];
    size_t got;
    while ((got = fread(chunk, 1, sizeof(chunk), f)) > 0) buf_put(out, chunk, got);
    fclose(f);
    return 0;
}

static int magic_of(const char *path, char magic[4])
{
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t got = fread(magic, 1, 4, f);
    fclose(f);
    return got == 4 ? 0 : -1;
}

static int ends_with_ci(const char *s, const char *suf)
{
    size_t n = strlen(s), m = strlen(suf);
    if (n < m) return 0;
    for (size_t i = 0; i < m; i++) {
        char a = s[n - m + i], b = suf[i];
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (a != b) return 0;
    }
    return 1;
}

static void default_restore_name(const char *src, char *out, size_t cap)
{
    if (ends_with_ci(src, ".ppz") || ends_with_ci(src, ".tcz"))
        snprintf(out, cap, "%.*s", (int)(strlen(src) - 4), src);
    else
        snprintf(out, cap, "%s.csv", src);
    if (!strcmp(out, src)) snprintf(out, cap, "%s.restored.csv", src);
}

/* Is this file something we can honestly read as a table?
 *
 * A reader handed the wrong kind of file does not fail: it finds "rows" in
 * whatever bytes it is given. `compress data.parquet` once read the binary
 * as text, found 883 rows of 2 columns, verified that round trip perfectly,
 * wrote the archive, and restored a corrupt file. The verification compares
 * the parsed table with the decoded one, so it is downstream of the damage
 * and cannot see it. Refuse at the door instead: magic bytes for containers
 * that announce themselves, extensions for the formats this cannot read.
 *
 * Deliberately NOT a content sniff: a NUL byte is legal inside a CSV cell.
 * Returns NULL when the file is fine, otherwise the reason. */
static const char *input_refusal(const char *path)
{
    static const struct { const char *magic; size_t n; const char *what; } MAGIC[] = {
        { "PAR1",             4, "a Parquet file" },
        { "ORC",              3, "an ORC file" },
        { "ARROW1",           6, "an Arrow/Feather file" },
        { "PK\x03\x04",       4, "a zip archive (.xlsx and .ods are zips)" },
        { "\xd0\xcf\x11\xe0", 4, "an old-style Excel/Office file" },
        { "\x1f\x8b",         2, "a gzip-compressed file" },
        { "BZh",              3, "a bzip2-compressed file" },
        { "\xfd" "7zXZ",      6, "an xz-compressed file" },
        { "\x28\xb5\x2f\xfd", 4, "a zstd-compressed file" },
        { "SQLite format 3", 15, "an SQLite database" },
        { "PPZ1",             4, "a Polypress archive already" },
        { "PPZX",             4, "a Polypress archive already" },
        { "PPZB",             4, "a Polypress archive already" },
        { "PPZS",             4, "a Polypress archive already" },
        { "FAST",             4, "a Polypress archive already" },
    };
    static const struct { const char *ext; const char *what; } EXT[] = {
        { ".xlsx", "an Excel" }, { ".xls", "an Excel" }, { ".ods", "an OpenDocument" },
        { ".orc", "an ORC" }, { ".feather", "a Feather" }, { ".arrow", "an Arrow" },
        { ".dta", "a Stata" }, { ".sav", "an SPSS" }, { ".zsav", "an SPSS" },
        { ".por", "an SPSS" }, { ".sas7bdat", "a SAS" }, { ".xpt", "a SAS transport" },
        { ".rds", "an R" }, { ".rdata", "an R" },
    };
    static char msg[640];
    if (!strcmp(path, "-")) return NULL;

    /* the extension first: it names the format ("an Excel file"), where the
     * magic only knows the container ("a zip archive") */
    for (size_t i = 0; i < sizeof(EXT) / sizeof(EXT[0]); i++)
        if (ends_with_ci(path, EXT[i].ext)) {
            snprintf(msg, sizeof(msg),
                     "%s is %s file, which Polypress cannot read yet.\n"
                     "Export it as CSV first.", path, EXT[i].what);
            return msg;
        }
    FILE *f = fopen(path, "rb");
    if (f) {
        char head[16];
        size_t got = fread(head, 1, sizeof(head), f);
        fclose(f);
        for (size_t i = 0; i < sizeof(MAGIC) / sizeof(MAGIC[0]); i++)
            if (got >= MAGIC[i].n && !memcmp(head, MAGIC[i].magic, MAGIC[i].n)) {
                if (!strcmp(MAGIC[i].magic, "PAR1") && !strcmp(ppz_format_of(path), "parquet"))
                    return NULL;         /* table_read_any explains the bridge */
                snprintf(msg, sizeof(msg),
                         "%s looks like %s, not a table in text.\nReading it as "
                         "text would produce an archive that restores to a "
                         "corrupt file.", path, MAGIC[i].what);
                return msg;
            }
    }
    return NULL;
}

static int tables_equal(const Table *a, const Table *b)
{
    if (a->nrows != b->nrows || a->ncols != b->ncols) return 0;
    for (size_t j = 0; j < a->ncols; j++)
        if (strcmp(a->names[j], b->names[j])) return 0;
    for (size_t k = 0; k < a->nrows * a->ncols; k++) {
        Str x = a->cells[k], y = b->cells[k];
        if (x.n != y.n || (x.n && memcmp(x.p, y.p, x.n))) return 0;
    }
    return 1;
}

/* --------------------------------------------------------------- args */

typedef struct {
    const char *path, *path2, *out, *encoding;
    int no_verify, json, progress;
    double budget;
    long rows;
} Args;

static int parse_args(int argc, char **argv, Args *a, int want_two)
{
    memset(a, 0, sizeof(*a));
    a->budget = 1.0;
    for (int i = 0; i < argc; i++) {
        const char *s = argv[i];
        if ((!strcmp(s, "-o") || !strcmp(s, "--output")) && i + 1 < argc) a->out = argv[++i];
        else if (!strcmp(s, "--encoding") && i + 1 < argc) a->encoding = argv[++i];
        else if (!strcmp(s, "--budget") && i + 1 < argc) a->budget = strtod(argv[++i], NULL);
        else if (!strcmp(s, "--rows") && i + 1 < argc) a->rows = strtol(argv[++i], NULL, 10);
        else if (!strcmp(s, "--no-verify")) a->no_verify = 1;
        else if (!strcmp(s, "--json")) a->json = 1;
        else if (!strcmp(s, "--progress")) a->progress = 1;
        else if (s[0] == '-' && s[1]) {
            fprintf(stderr, "polypress: unknown option %s\n", s);
            return -1;
        }
        else if (!a->path) a->path = s;
        else if (want_two && !a->path2) a->path2 = s;
        else { fprintf(stderr, "polypress: unexpected argument %s\n", s); return -1; }
    }
    if (!a->path || (want_two && !a->path2)) return -1;
    if (a->budget <= 0) { fprintf(stderr, "polypress: --budget must be positive\n"); return -1; }
    if (a->rows < 0) { fprintf(stderr, "polypress: --rows must be positive\n"); return -1; }
    return 0;
}

/* ------------------------------------------------------------ commands */

static int cmd_compress(int argc, char **argv)
{
    Args a;
    if (parse_args(argc, argv, &a, 0)) {
        fprintf(stderr, "usage: polypress compress FILE [-o OUT] [--encoding ENC]\n");
        return 2;
    }
    char err[1024] = "";
    const char *why = input_refusal(a.path);
    if (why) { fprintf(stderr, "polypress: %s\n", why); return 1; }

    char dst[4096];
    if (a.out) snprintf(dst, sizeof(dst), "%s", a.out);
    else if (!strcmp(a.path, "-")) {
        fprintf(stderr, "polypress: reading standard input needs -o OUT\n");
        return 2;
    } else snprintf(dst, sizeof(dst), "%s.ppz", a.path);

    /* --progress: one line per stage on stderr, for the app to show */
    #define STAGE(x) do { if (a.progress) { fprintf(stderr, "stage %s\n", x); fflush(stderr); } } while (0)
    double t0 = now();
    Table t;
    STAGE("reading");
    if (table_read_any(&t, a.path, a.encoding, err, sizeof(err))) {
        fprintf(stderr, "polypress: %s\n", err);
        return 1;
    }
    Buf blob;
    buf_init(&blob);
    STAGE("compressing");
    if (ppz_encode(&t, &blob)) {
        fprintf(stderr, "polypress: encode failed\n");
        table_free(&t); buf_free(&blob);
        return 1;
    }
    double secs = now() - t0;

    if (!a.no_verify) {
        STAGE("verifying");
        Table back;
        int ok = ppz_decode(blob.data, blob.len, &back) == 0;
        if (ok) { ok = tables_equal(&t, &back); table_free(&back); }
        if (!ok) {
            fprintf(stderr, "polypress: verification FAILED -- nothing written\n");
            table_free(&t); buf_free(&blob);
            return 1;
        }
    }

    /* beside the name, then renamed: a stopped or failed write never
     * leaves a truncated archive under the name the user asked for */
    char tmp[4200];
    FILE *f = stdout;
    if (strcmp(dst, "-")) {
        snprintf(tmp, sizeof(tmp), "%s.partXXXXXX", dst);
        int fd = mkstemp(tmp);
        if (fd >= 0) {
            ppz_tmp_register(tmp);
            mode_t um = umask(0);
            umask(um);
            fchmod(fd, 0666 & ~um);
        }
        f = fd >= 0 ? fdopen(fd, "wb") : NULL;
    }
    if (!f) {
        fprintf(stderr, "polypress: cannot write %s: %s\n", dst, strerror(errno));
        table_free(&t); buf_free(&blob);
        return 1;
    }
    size_t w = fwrite(blob.data, 1, blob.len, f);
    int bad = (f == stdout) ? fflush(f) : fclose(f);
    if (f != stdout && !bad && w == blob.len && rename(tmp, dst)) bad = 1;
    if (f != stdout) ppz_tmp_forget(tmp);
    if (w != blob.len || bad) {
        fprintf(stderr, "polypress: cannot write %s\n", dst);
        if (f != stdout) unlink(tmp);
        table_free(&t); buf_free(&blob);
        return 1;
    }
    #undef STAGE

    struct stat st;
    double raw = (strcmp(a.path, "-") && !stat(a.path, &st)) ? (double)st.st_size : 0;
    char r1[32], h1[32], h2[32];
    FILE *msg = (f == stdout) ? stderr : stdout;
    if (raw > 0)
        fprintf(msg, "%s rows x %zu cols   %s -> %s   %.2fx   %.1f MB/s\n",
                commas(t.nrows, r1, sizeof(r1)), t.ncols, human(raw, h1, sizeof(h1)),
                human((double)blob.len, h2, sizeof(h2)),
                raw / (blob.len ? (double)blob.len : 1.0),
                raw / 1e6 / (secs > 1e-9 ? secs : 1e-9));
    else
        fprintf(msg, "%s rows x %zu cols   -> %s\n", commas(t.nrows, r1, sizeof(r1)),
                t.ncols, human((double)blob.len, h2, sizeof(h2)));
    fprintf(msg, "%s\n", dst);
    table_free(&t);
    buf_free(&blob);
    return 0;
}

static int cmd_restore(int argc, char **argv)
{
    Args a;
    if (parse_args(argc, argv, &a, 0)) {
        fprintf(stderr, "usage: polypress restore ARCHIVE [-o OUT]\n");
        return 2;
    }
    char dst[4096], err[1024] = "";
    if (a.out) snprintf(dst, sizeof(dst), "%s", a.out);
    else default_restore_name(a.path, dst, sizeof(dst));
    FILE *msg = strcmp(dst, "-") ? stdout : stderr;
    char r1[32], h1[32], h2[32];
    double t0 = now();

    char mg[4];
    if (!magic_of(a.path, mg) && !memcmp(mg, PPZ_MAGIC_STREAM, 4)) {
        StreamStats st;
        if (ppz_stream_restore(a.path, dst, &st, err, sizeof(err))) {
            fprintf(stderr, "polypress: %s\n", err);
            return 1;
        }
        double secs = now() - t0;
        fprintf(msg, "%s rows from %zu blocks   %s   %.1f MB/s\n",
                commas(st.rows, r1, sizeof(r1)), st.blocks,
                human((double)st.bytes, h1, sizeof(h1)),
                st.bytes / 1e6 / (secs > 1e-9 ? secs : 1e-9));
        fprintf(msg, "%s\n", dst);
        return 0;
    }

    Buf blob;
    if (read_file(a.path, &blob, err, sizeof(err))) { fprintf(stderr, "polypress: %s\n", err); return 1; }
    Table t;
    if (ppz_decode(blob.data, blob.len, &t)) {
        fprintf(stderr, "polypress: cannot read %s -- it is not a Polypress "
                "archive, or it is damaged.\n", a.path);
        buf_free(&blob);
        return 1;
    }
    size_t packed = blob.len;
    buf_free(&blob);
    if (table_write_any(&t, dst, err, sizeof(err))) {
        fprintf(stderr, "polypress: %s\n", err);
        table_free(&t);
        return 1;
    }
    double secs = now() - t0;
    struct stat st;
    double out = (strcmp(dst, "-") && !stat(dst, &st)) ? (double)st.st_size : 0;
    fprintf(msg, "%s rows x %zu cols   %s -> %s   %.1f MB/s\n",
            commas(t.nrows, r1, sizeof(r1)), t.ncols, human((double)packed, h1, sizeof(h1)),
            human(out, h2, sizeof(h2)), out / 1e6 / (secs > 1e-9 ? secs : 1e-9));
    fprintf(msg, "%s\n", dst);
    table_free(&t);
    return 0;
}

static int cmd_convert(int argc, char **argv)
{
    Args a;
    if (parse_args(argc, argv, &a, 1)) {
        fprintf(stderr, "usage: polypress convert IN OUT [--encoding ENC]\n");
        return 2;
    }
    char err[1024] = "";
    const char *why = input_refusal(a.path);
    if (why) { fprintf(stderr, "polypress: %s\n", why); return 1; }
    Table t;
    if (table_read_any(&t, a.path, a.encoding, err, sizeof(err))) {
        fprintf(stderr, "polypress: %s\n", err);
        return 1;
    }
    if (table_write_any(&t, a.path2, err, sizeof(err))) {
        fprintf(stderr, "polypress: %s\n", err);
        table_free(&t);
        return 1;
    }
    char r1[32];
    FILE *msg = strcmp(a.path2, "-") ? stdout : stderr;
    fprintf(msg, "%s rows x %zu cols\n%s\n", commas(t.nrows, r1, sizeof(r1)), t.ncols, a.path2);
    table_free(&t);
    return 0;
}

static double g_stream_t0;

/* On a terminal: one line, rewritten -- how far, how fast, how long left. */
static void stream_progress(size_t blk, unsigned long long rows, uint64_t out,
                            uint64_t in, uint64_t total)
{
    char r1[32], h1[32];
    double secs = now() - g_stream_t0;
    fprintf(stderr, "\r  %12s rows   %s", commas(rows, r1, sizeof(r1)),
            human((double)out, h1, sizeof(h1)));
    if (total && in) {
        double frac = in >= total ? 1.0 : (double)in / (double)total;
        double left = secs * (1.0 - frac) / (frac > 1e-9 ? frac : 1e-9);
        fprintf(stderr, "   %3.0f%%   %.0f min %02.0f s left   ", 100 * frac,
                left >= 60 ? (double)((long)left / 60) : 0.0, left - 60 * (double)((long)left / 60));
    }
    fflush(stderr);
    (void)blk;
}

/* --progress: machine-readable, for the app. */
static void stream_progress_machine(size_t blk, unsigned long long rows, uint64_t out,
                                    uint64_t in, uint64_t total)
{
    fprintf(stderr, "progress %llu %llu %llu %llu %zu\n", (unsigned long long)in,
            (unsigned long long)total, rows, (unsigned long long)out, blk);
    fflush(stderr);
}

static int cmd_stream_compress(int argc, char **argv)
{
    Args a;
    if (parse_args(argc, argv, &a, 0)) {
        fprintf(stderr, "usage: polypress stream-compress FILE [-o OUT] "
                "[--budget GB] [--rows N] [--encoding ENC]\n");
        return 2;
    }
    const char *why = input_refusal(a.path);
    if (why) { fprintf(stderr, "polypress: %s\n", why); return 1; }
    const char *fmt = ppz_format_of(a.path);
    if (!strcmp(fmt, "json") || !strcmp(fmt, "jsonl") || !strcmp(fmt, "parquet")) {
        fprintf(stderr, "polypress: stream-compress reads delimited text only; "
                "convert %s to CSV first\n", a.path);
        return 1;
    }
    char dst[4096], err[1024] = "";
    if (a.out) snprintf(dst, sizeof(dst), "%s", a.out);
    else if (!strcmp(a.path, "-")) {
        fprintf(stderr, "polypress: reading standard input needs -o OUT\n");
        return 2;
    } else snprintf(dst, sizeof(dst), "%s.ppz", a.path);

    double t0 = now();
    g_stream_t0 = t0;
    StreamStats st;
    int interactive = isatty(2) && !a.progress;
    if (a.progress) { fprintf(stderr, "stage compressing\n"); fflush(stderr); }
    if (ppz_stream_compress(a.path, dst, a.budget, (size_t)a.rows, !a.no_verify,
                            a.encoding, a.progress ? stream_progress_machine
                                        : interactive ? stream_progress : NULL,
                            &st, err, sizeof(err))) {
        if (interactive) fprintf(stderr, "\n");
        fprintf(stderr, "polypress: %s\n", err);
        return 1;
    }
    if (interactive) fprintf(stderr, "\r%90s\r", "");
    double secs = now() - t0;
    struct stat sb;
    double raw = (strcmp(a.path, "-") && !stat(a.path, &sb)) ? (double)sb.st_size : 0;
    char r1[32], r2[32], h1[32], h2[32];
    printf("%s rows in %zu blocks of %s   %s -> %s   %.2fx   %.1f MB/s\n",
           commas(st.rows, r1, sizeof(r1)), st.blocks,
           commas(st.rows_per_block, r2, sizeof(r2)), human(raw, h1, sizeof(h1)),
           human((double)st.bytes, h2, sizeof(h2)),
           raw / (st.bytes ? (double)st.bytes : 1.0),
           raw / 1e6 / (secs > 1e-9 ? secs : 1e-9));
    printf("%s\n", dst);
    return 0;
}

/* ----------------------------------------------------------------- info */

static void json_kv_str(Buf *b, const char *k, const char *v, int *first)
{
    if (!*first) buf_put(b, ", ", 2);
    *first = 0;
    ppz_json_str(b, k, strlen(k));
    buf_put(b, ": ", 2);
    ppz_json_str(b, v, strlen(v));
}

static void json_kv_num(Buf *b, const char *k, unsigned long long v, int *first)
{
    if (!*first) buf_put(b, ", ", 2);
    *first = 0;
    ppz_json_str(b, k, strlen(k));
    char n[32];
    snprintf(n, sizeof(n), ": %llu", v);
    buf_put(b, n, strlen(n));
}

static int info_stream(const char *path, int as_json)
{
    char err[1024] = "";
    StreamInfo h;
    if (ppz_stream_info(path, &h, err, sizeof(err))) { fprintf(stderr, "polypress: %s\n", err); return 1; }
    char r1[32], r2[32], h1[32], h2[32], h3[32];
    if (as_json) {
        Buf b;
        buf_init(&b);
        int first = 1;
        buf_putc(&b, '{');
        json_kv_str(&b, "file", path, &first);
        json_kv_num(&b, "size", h.size, &first);
        json_kv_str(&b, "container", "stream", &first);
        json_kv_num(&b, "rows", h.nrows, &first);
        json_kv_num(&b, "columns", h.ncols, &first);
        json_kv_num(&b, "blocks", h.nblocks, &first);
        json_kv_num(&b, "rows_per_block", h.rows_per_block, &first);
        buf_put(&b, ", \"names\": [", 12);
        for (size_t j = 0; j < h.ncols; j++) {
            if (j) buf_put(&b, ", ", 2);
            ppz_json_str(&b, h.columns[j], strlen(h.columns[j]));
        }
        buf_put(&b, "]}\n", 3);
        fwrite(b.data, 1, b.len, stdout);
        buf_free(&b);
    } else {
        printf("file          %s\n", path);
        printf("size          %s\n", human((double)h.size, h1, sizeof(h1)));
        printf("container     streamed, one block at a time\n");
        printf("rows          %s\n", commas(h.nrows, r1, sizeof(r1)));
        printf("columns       %zu\n", h.ncols);
        printf("blocks        %zu of %s rows\n", h.nblocks, commas(h.rows_per_block, r2, sizeof(r2)));
        if (h.nblocks) {
            uint64_t *s = malloc(h.nblocks * sizeof(uint64_t));
            memcpy(s, h.blocks, h.nblocks * sizeof(uint64_t));
            for (size_t i = 1; i < h.nblocks; i++)      /* small n: insertion sort */
                for (size_t k = i; k > 0 && s[k - 1] > s[k]; k--) {
                    uint64_t x = s[k]; s[k] = s[k - 1]; s[k - 1] = x;
                }
            printf("block sizes   min %s  median %s  max %s\n",
                   human((double)s[0], h1, sizeof(h1)),
                   human((double)s[h.nblocks / 2], h2, sizeof(h2)),
                   human((double)s[h.nblocks - 1], h3, sizeof(h3)));
            free(s);
        }
    }
    ppz_stream_info_free(&h);
    return 0;
}

static int cmd_info(int argc, char **argv)
{
    Args a;
    if (parse_args(argc, argv, &a, 0)) {
        fprintf(stderr, "usage: polypress info ARCHIVE [--json]\n");
        return 2;
    }
    char err[1024] = "";
    char mg[4];
    if (!magic_of(a.path, mg) && !memcmp(mg, PPZ_MAGIC_STREAM, 4)) return info_stream(a.path, a.json);

    Buf blob;
    if (read_file(a.path, &blob, err, sizeof(err))) { fprintf(stderr, "polypress: %s\n", err); return 1; }
    const char *kind = NULL;
    if (blob.len >= 4) {
        if (!memcmp(blob.data, PPZ_MAGIC, 4))             kind = "modelled";
        else if (!memcmp(blob.data, PPZ_MAGIC_V0, 4))     kind = "modelled (written by an older version)";
        else if (!memcmp(blob.data, PPZ_MAGIC_RAW_XZ, 4)) kind = "plain xz -- no modelling helped on this table";
        else if (!memcmp(blob.data, PPZ_MAGIC_RAW_BZ, 4)) kind = "plain bzip2 -- no modelling helped on this table";
    }
    Table t;
    if (!kind || ppz_decode(blob.data, blob.len, &t)) {
        fprintf(stderr, "polypress: cannot read %s -- it is not a Polypress "
                "archive, or it is damaged.\n", a.path);
        buf_free(&blob);
        return 1;
    }

    /* the plan, from the metadata of a modelled archive */
    size_t nk[4] = { 0, 0, 0, 0 };            /* dict, num, text, grp */
    const char *kn[4] = { "dict", "num", "text", "grp" };
    size_t reordered = 0;
    Js *meta = NULL;
    if (!memcmp(blob.data, PPZ_MAGIC, 4) || !memcmp(blob.data, PPZ_MAGIC_V0, 4)) {
        size_t ml = ((size_t)blob.data[4] << 24) | ((size_t)blob.data[5] << 16) |
                    ((size_t)blob.data[6] << 8) | blob.data[7];
        Buf mb;
        buf_init(&mb);
        if (16 + ml <= blob.len && !ppz_lzma_decompress(blob.data + 16, ml, &mb))
            meta = js_parse((const char *)mb.data, mb.len);
        buf_free(&mb);
        const Js *cols = js_get(meta, "cols");
        for (size_t i = 0; cols && cols->kind == JS_ARR && i < cols->count; i++) {
            const Js *k = js_get(&cols->items[i], "kind");
            if (!k || k->kind != JS_STR) continue;
            for (int q = 0; q < 4; q++) if (!strcmp(k->str, kn[q])) nk[q]++;
            const Js *p = js_get(&cols->items[i], "parent");
            if (p && p->kind == JS_NUM) reordered++;
        }
    }
    const Js *groups = js_get(meta, "groups");
    char r1[32], h1[32];

    if (a.json) {
        Buf b;
        buf_init(&b);
        int first = 1;
        buf_putc(&b, '{');
        json_kv_str(&b, "file", a.path, &first);
        json_kv_num(&b, "size", blob.len, &first);
        json_kv_str(&b, "container", meta ? "modelled" : (blob.data[3] == 'X' ? "xz" : "bzip2"), &first);
        json_kv_num(&b, "rows", t.nrows, &first);
        json_kv_num(&b, "columns", t.ncols, &first);
        buf_put(&b, ", \"plan\": {", 11);
        int f2 = 1;
        for (int q = 0; q < 4; q++) if (nk[q]) json_kv_num(&b, kn[q], nk[q], &f2);
        buf_putc(&b, '}');
        json_kv_num(&b, "reordered", reordered, &first);
        buf_put(&b, ", \"groups\": [", 13);
        for (size_t g = 0; groups && groups->kind == JS_ARR && g < groups->count; g++) {
            if (g) buf_put(&b, ", ", 2);
            buf_putc(&b, '[');
            const Js *gg = &groups->items[g];
            for (size_t i = 0; gg->kind == JS_ARR && i < gg->count; i++) {
                long c = js_int(&gg->items[i], -1);
                if (c < 0 || (size_t)c >= t.ncols) continue;
                if (i) buf_put(&b, ", ", 2);
                ppz_json_str(&b, t.names[c], strlen(t.names[c]));
            }
            buf_putc(&b, ']');
        }
        buf_put(&b, "], \"names\": [", 13);
        for (size_t j = 0; j < t.ncols; j++) {
            if (j) buf_put(&b, ", ", 2);
            ppz_json_str(&b, t.names[j], strlen(t.names[j]));
        }
        buf_put(&b, "]}\n", 3);
        fwrite(b.data, 1, b.len, stdout);
        buf_free(&b);
    } else {
        printf("file        %s\n", a.path);
        printf("size        %s\n", human((double)blob.len, h1, sizeof(h1)));
        printf("container   %s\n", kind);
        printf("rows        %s\n", commas(t.nrows, r1, sizeof(r1)));
        printf("columns     %zu\n", t.ncols);
        if (meta) {
            printf("plan        ");
            int any = 0;
            for (int q = 0; q < 4; q++)
                if (nk[q]) { printf("%s%zu %s", any ? ", " : "", nk[q], kn[q]); any = 1; }
            printf("\n");
            if (groups && groups->kind == JS_ARR && groups->count) {
                printf("2D groups   ");
                for (size_t g = 0; g < groups->count; g++) {
                    const Js *gg = &groups->items[g];
                    if (g) printf("; ");
                    for (size_t i = 0; gg->kind == JS_ARR && i < gg->count; i++) {
                        long c = js_int(&gg->items[i], -1);
                        if (c >= 0 && (size_t)c < t.ncols) printf("%s%s", i ? ", " : "", t.names[c]);
                    }
                }
                printf("\n");
            }
            printf("reordered   %zu columns sorted by a parent\n", reordered);
        }
    }
    js_free(meta);
    table_free(&t);
    buf_free(&blob);
    return 0;
}

static void usage(FILE *f)
{
    fprintf(f,
        "polypress %s -- lossless compression for data tables\n\n"
        "  polypress compress FILE [-o OUT] [--encoding ENC]   table -> .ppz\n"
        "  polypress restore  ARCHIVE [-o OUT]                 .ppz -> table\n"
        "  polypress info     ARCHIVE [--json]                 what is inside\n"
        "  polypress convert  IN OUT [--encoding ENC]          table -> table\n"
        "  polypress stream-compress FILE [-o OUT] [--budget GB] [--rows N]\n"
        "                                    for files bigger than memory\n\n"
        "Reads CSV, TSV, PSV, other delimited text (delimiter from the header),\n"
        "JSON (records or columns) and JSON Lines. The output extension picks\n"
        "the format when restoring or converting; \"-\" is CSV on stdin/stdout.\n"
        "Compression decodes and compares every cell before writing anything.\n",
        PPZ_VERSION);
}

int main(int argc, char **argv)
{
    ppz_cleanup_on_signals();
    if (argc < 2) { usage(stderr); return 2; }
    const char *c = argv[1];
    if (!strcmp(c, "-h") || !strcmp(c, "--help") || !strcmp(c, "help")) { usage(stdout); return 0; }
    if (!strcmp(c, "--version") || !strcmp(c, "version")) { printf("polypress %s\n", PPZ_VERSION); return 0; }
    if (!strcmp(c, "compress"))        return cmd_compress(argc - 2, argv + 2);
    if (!strcmp(c, "restore"))         return cmd_restore(argc - 2, argv + 2);
    if (!strcmp(c, "info"))            return cmd_info(argc - 2, argv + 2);
    if (!strcmp(c, "convert"))         return cmd_convert(argc - 2, argv + 2);
    if (!strcmp(c, "stream-compress")) return cmd_stream_compress(argc - 2, argv + 2);
    /* kept so older scripts work: restore and info read stream archives */
    if (!strcmp(c, "stream-restore"))  return cmd_restore(argc - 2, argv + 2);
    if (!strcmp(c, "stream-info"))     return cmd_info(argc - 2, argv + 2);
    fprintf(stderr, "polypress: unknown command %s\n\n", c);
    usage(stderr);
    return 2;
}
