/* polypress -- the standalone binary.
 *
 *     polypress restore data.csv.ppz [-o out.csv]
 *     polypress info    data.csv.ppz
 *
 * `compress` is not here yet; the Python CLI still writes archives. This
 * binary exists so that reading one needs nothing installed, which is the
 * half of the distribution problem that blocks a researcher from opening a
 * file someone sent them.
 */

#include "ppz.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int read_file(const char *path, Buf *out)
{
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    buf_init(out);
    uint8_t chunk[65536];
    size_t got;
    while ((got = fread(chunk, 1, sizeof(chunk), f)) > 0) buf_put(out, chunk, got);
    fclose(f);
    return 0;
}

static void human(double n, char *out, size_t cap)
{
    const char *u[] = { "B", "KB", "MB", "GB" };
    int i = 0;
    while (n >= 1024.0 && i < 3) { n /= 1024.0; i++; }
    if (i == 0) snprintf(out, cap, "%.0f B", n);
    else snprintf(out, cap, "%.1f %s", n, u[i]);
}

static int cmd_restore(int argc, char **argv)
{
    const char *src = NULL, *dst = NULL;
    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "-o") && i + 1 < argc) { dst = argv[++i]; continue; }
        if (!src) src = argv[i];
    }
    if (!src) { fprintf(stderr, "usage: polypress restore FILE [-o OUT]\n"); return 2; }

    Buf blob;
    if (read_file(src, &blob)) { fprintf(stderr, "cannot read %s\n", src); return 1; }

    Table t;
    if (ppz_decode(blob.data, blob.len, &t)) {
        fprintf(stderr, "polypress: not a readable Polypress archive\n");
        buf_free(&blob);
        return 1;
    }
    buf_free(&blob);

    char derived[4096];
    if (!dst) {
        size_t n = strlen(src);
        if (n > 4 && (!strcmp(src + n - 4, ".ppz") || !strcmp(src + n - 4, ".tcz"))) {
            snprintf(derived, sizeof(derived), "%.*s", (int)(n - 4), src);
        } else {
            snprintf(derived, sizeof(derived), "%s.csv", src);
        }
        dst = derived;
    }
    if (table_write_csv(&t, dst)) {
        fprintf(stderr, "cannot write %s\n", dst);
        table_free(&t);
        return 1;
    }
    char hb[32];
    human((double)t.nrows, hb, sizeof(hb));
    fprintf(stdout, "%zu rows x %zu cols -> %s\n", t.nrows, t.ncols, dst);
    table_free(&t);
    return 0;
}

/* Is this file something we can honestly parse?
 *
 * This binary reads exactly one thing: comma-delimited text. `table_read_csv`
 * does not check, and until this guard existed `cmd_compress` handed it
 * whatever it was given. That was not a harmless misread -- it was silent
 * data loss, and the verify step below could not see it:
 *
 *   polypress compress data.parquet     read a BINARY file as text, found
 *                                       883 "rows" of 2 "columns", encoded
 *                                       them, verified that round-trip
 *                                       perfectly, wrote the archive, and
 *                                       restored to a corrupt file.
 *   polypress compress data.tsv         found one column per line, because
 *                                       nothing in a TSV is a comma, and
 *                                       disagreed with the Python encoder
 *                                       about the table -- which is
 *                                       invariant 1 broken in the one place
 *                                       no test looked.
 *
 * The verification is real but it compares the parsed table against the
 * decoded table, so it is downstream of the damage and cannot see it. This
 * repo has met that exact shape before, when `errors="replace"` corrupted a
 * file before the round-trip check ever ran. The lesson stands: a check
 * downstream of the damage cannot see the damage, so refuse at the door.
 *
 * Two signals, both cheap and both decisive:
 *
 *   magic bytes   a container that announces itself. Parquet is the one that
 *                 bit us; the rest are here because they are what people
 *                 actually have next to their CSVs.
 *   extension     formats the Python CLI reads properly and this one cannot,
 *                 so the answer is "use the other CLI", not "refuse".
 *
 * Deliberately NOT a check: a NUL byte, or anything else content-based. A NUL
 * is legal inside a CSV cell -- it forces quoting, which `table_write_canonical`
 * has to reproduce and `test_cbin.py::check_canonical` pins -- so rejecting on
 * it would make the C binary refuse files the Python one accepts, and break
 * the byte-identity guarantee in the course of protecting it.
 *
 * Returns NULL when the file is fine, otherwise the reason.
 */
static const char *input_refusal(const char *path)
{
    static const struct { const char *magic; size_t n; const char *what; } MAGIC[] = {
        { "PAR1",             4, "a Parquet file" },
        { "ORC",              3, "an ORC file" },
        { "ARROW1",           6, "an Arrow/Feather file" },
        { "PK\x03\x04",       4, "a zip archive (.xlsx and .ods are zips)" },
        { "\x1f\x8b",         2, "a gzip-compressed file" },
        { "BZh",              3, "a bzip2-compressed file" },
        { "\xfd" "7zXZ",      6, "an xz-compressed file" },
        { "\x28\xb5\x2f\xfd", 4, "a zstd-compressed file" },
        { "SQLite format 3", 15, "an SQLite database" },
        { "PPZ1",             4, "a Polypress archive already" },
        { "PPZX",             4, "a Polypress archive already" },
        { "PPZB",             4, "a Polypress archive already" },
        { "FAST",             4, "a Polypress archive already" },
    };
    static const struct { const char *ext; const char *what; } EXT[] = {
        { ".parquet", "Parquet" },
        { ".json",    "JSON" },
        { ".jsonl",   "JSON Lines" },
        { ".ndjson",  "newline-delimited JSON" },
        { ".tsv",     "tab-separated" },
        { ".psv",     "pipe-separated" },
        { ".xlsx",    "Excel" },
        { ".xls",     "Excel" },
        { ".orc",     "ORC" },
        { ".feather", "Feather" },
        { ".arrow",   "Arrow" },
    };
    static char msg[512];

    FILE *f = fopen(path, "rb");
    if (f) {
        char head[16];
        size_t got = fread(head, 1, sizeof(head), f);
        fclose(f);
        for (size_t i = 0; i < sizeof(MAGIC) / sizeof(MAGIC[0]); i++)
            if (got >= MAGIC[i].n && !memcmp(head, MAGIC[i].magic, MAGIC[i].n)) {
                snprintf(msg, sizeof(msg),
                         "%s looks like %s, not comma-separated text.\n"
                         "This binary reads CSV only. Reading it as CSV would "
                         "produce an archive that restores to a corrupt file.",
                         path, MAGIC[i].what);
                return msg;
            }
    }

    size_t n = strlen(path);
    for (size_t i = 0; i < sizeof(EXT) / sizeof(EXT[0]); i++) {
        size_t e = strlen(EXT[i].ext);
        if (n <= e) continue;
        const char *tail = path + n - e;
        int same = 1;
        for (size_t k = 0; k < e; k++) {
            char a = tail[k], b = EXT[i].ext[k];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (a != b) { same = 0; break; }
        }
        if (same) {
            snprintf(msg, sizeof(msg),
                     "%s is %s, which this binary cannot parse -- it reads "
                     "comma-separated text only.\n"
                     "The Python CLI does read it:  polypress compress %s",
                     path, EXT[i].what, path);
            return msg;
        }
    }
    return NULL;
}

static int cmd_compress(int argc, char **argv)
{
    const char *src = NULL, *dst = NULL;
    int verify = 1;
    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "-o") && i + 1 < argc) { dst = argv[++i]; continue; }
        if (!strcmp(argv[i], "--no-verify")) { verify = 0; continue; }
        if (!src) src = argv[i];
    }
    if (!src) { fprintf(stderr, "usage: polypress compress FILE [-o OUT]\n"); return 2; }

    const char *why = input_refusal(src);
    if (why) { fprintf(stderr, "polypress: %s\n", why); return 1; }

    Table t;
    if (table_read_csv(&t, src)) { fprintf(stderr, "cannot read %s\n", src); return 1; }

    Buf blob;
    buf_init(&blob);
    if (ppz_encode(&t, &blob)) {
        fprintf(stderr, "polypress: encode failed\n");
        table_free(&t); buf_free(&blob);
        return 1;
    }

    /* Same contract as tzip.py: decode the blob back and compare every cell
     * before anything is written. A compressor that can silently lose a cell
     * is not one to hand a researcher. */
    if (verify) {
        Table back;
        if (ppz_decode(blob.data, blob.len, &back)) {
            fprintf(stderr, "verification FAILED (undecodable) -- nothing written\n");
            table_free(&t); buf_free(&blob);
            return 1;
        }
        int ok = back.nrows == t.nrows && back.ncols == t.ncols;
        for (size_t j = 0; ok && j < t.ncols; j++)
            if (strcmp(back.names[j], t.names[j])) ok = 0;
        for (size_t i = 0; ok && i < t.nrows; i++)
            for (size_t j = 0; j < t.ncols; j++) {
                Str a = table_at(&t, i, j), b = table_at(&back, i, j);
                if (a.n != b.n || (a.n && memcmp(a.p, b.p, a.n))) { ok = 0; break; }
            }
        table_free(&back);
        if (!ok) {
            fprintf(stderr, "verification FAILED -- nothing written\n");
            table_free(&t); buf_free(&blob);
            return 1;
        }
    }

    char derived[4096];
    if (!dst) {
        snprintf(derived, sizeof(derived), "%s.ppz", src);
        dst = derived;
    }
    FILE *f = fopen(dst, "wb");
    if (!f) { fprintf(stderr, "cannot write %s\n", dst); table_free(&t); buf_free(&blob); return 1; }
    size_t w = fwrite(blob.data, 1, blob.len, f);
    fclose(f);
    if (w != blob.len) { fprintf(stderr, "short write to %s\n", dst); table_free(&t); buf_free(&blob); return 1; }

    printf("%zu rows x %zu cols -> %zu B   %s\n", t.nrows, t.ncols, blob.len, dst);
    table_free(&t);
    buf_free(&blob);
    return 0;
}

static int cmd_info(int argc, char **argv)
{
    if (argc < 1) { fprintf(stderr, "usage: polypress info FILE\n"); return 2; }
    Buf blob;
    if (read_file(argv[0], &blob)) { fprintf(stderr, "cannot read %s\n", argv[0]); return 1; }
    if (blob.len < 4) { buf_free(&blob); return 1; }

    const char *kind = "unknown";
    if (!memcmp(blob.data, PPZ_MAGIC, 4))            kind = "modelled";
    else if (!memcmp(blob.data, PPZ_MAGIC_V0, 4))    kind = "modelled (pre-rename)";
    else if (!memcmp(blob.data, PPZ_MAGIC_RAW_XZ, 4)) kind = "fallback (xz)";
    else if (!memcmp(blob.data, PPZ_MAGIC_RAW_BZ, 4)) kind = "fallback (bzip2)";

    Table t;
    if (ppz_decode(blob.data, blob.len, &t)) {
        fprintf(stderr, "polypress: not a readable Polypress archive\n");
        buf_free(&blob);
        return 1;
    }
    char hb[32];
    human((double)blob.len, hb, sizeof(hb));
    printf("file        %s\n", argv[0]);
    printf("size        %s\n", hb);
    printf("container   %s\n", kind);
    printf("rows        %zu\n", t.nrows);
    printf("columns     %zu\n", t.ncols);
    table_free(&t);
    buf_free(&blob);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr,
                "polypress -- lossless compression for data tables\n\n"
                "  polypress compress FILE [-o OUT]  csv -> .ppz\n"
                "  polypress restore  FILE [-o OUT]  .ppz -> csv\n"
                "  polypress info     FILE           what is inside\n\n"
                "Compression verifies the round trip in memory before writing.\n");
        return 2;
    }
    if (!strcmp(argv[1], "compress")) return cmd_compress(argc - 2, argv + 2);
    if (!strcmp(argv[1], "restore"))  return cmd_restore(argc - 2, argv + 2);
    if (!strcmp(argv[1], "info"))     return cmd_info(argc - 2, argv + 2);
    fprintf(stderr, "polypress: unknown command %s\n", argv[1]);
    return 2;
}
