/* The command line.
 *
 *     stridexz compress  table.csv [out.sxz]
 *     stridexz restore   table.sxz [out.csv]
 *     stridexz info      table.sxz
 *
 * `compress` decodes what it just produced and compares every cell before a
 * file is created. That check is not paranoia about the encoder; it is the
 * only thing standing between a subtle bug and a user who deletes the original
 * on the strength of an exit code of zero.
 */
#include "sxz.h"

#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static int tables_equal(const Table *a, const Table *b)
{
    if (a->ncols != b->ncols || a->nrows != b->nrows)
        return 0;
    for (size_t c = 0; c < a->ncols; c++) {
        if (!str_eq(a->names.v[c], b->names.v[c]))
            return 0;
        for (size_t r = 0; r < a->nrows; r++)
            if (!str_eq(a->cols[c].v[r], b->cols[c].v[r]))
                return 0;
    }
    return 1;
}

/* Refuse at the door. This reader understands comma-separated text and
 * nothing else, and the round-trip check downstream cannot save us: handed a
 * .parquet it would read the binary AS text, encode those "rows", verify them
 * against themselves, and cheerfully write a corrupt archive. The damage
 * happens before either table exists, so the check never sees it. */
static const char *input_refusal(const char *path)
{
    static const struct { const char *ext, *why; } bad[] = {
        { ".parquet", "Parquet" }, { ".pq", "Parquet" },
        { ".orc", "ORC" }, { ".feather", "Feather" }, { ".arrow", "Arrow" },
        { ".xlsx", "Excel" }, { ".xls", "Excel" },
        { ".gz", "gzip" }, { ".xz", "xz" }, { ".bz2", "bzip2" },
        { ".zst", "zstd" }, { ".zip", "zip" }, { ".sqlite", "SQLite" },
        { ".db", "a database" }, { ".duckdb", "DuckDB" },
        { ".tsv", "tab-separated" }, { ".psv", "pipe-separated" },
        { NULL, NULL }
    };
    size_t n = strlen(path);
    for (int i = 0; bad[i].ext; i++) {
        size_t e = strlen(bad[i].ext);
        if (n >= e && !strcasecmp(path + n - e, bad[i].ext))
            return bad[i].why;
    }

    FILE *fh = fopen(path, "rb");
    if (!fh)
        return NULL;
    unsigned char m[8] = { 0 };
    size_t got = fread(m, 1, sizeof m, fh);
    fclose(fh);
    if (got >= 4 && !memcmp(m, "PAR1", 4))       return "Parquet";
    if (got >= 3 && !memcmp(m, "ORC", 3))        return "ORC";
    if (got >= 4 && !memcmp(m, "ARROW", 4))      return "Arrow";
    if (got >= 2 && m[0] == 0x1F && m[1] == 0x8B) return "gzip";
    if (got >= 6 && !memcmp(m, "\xFD" "7zXZ\0", 6)) return "xz";
    if (got >= 3 && !memcmp(m, "BZh", 3))        return "bzip2";
    if (got >= 4 && !memcmp(m, "PK\x03\x04", 4)) return "zip";
    if (got >= 6 && !memcmp(m, "SQLite", 6))     return "SQLite";
    if (got >= 4 && !memcmp(m, SXZ_MAGIC, 4))    return "already a StrideXZ archive";
    return NULL;
}

static int slurp(const char *path, Buf *out)
{
    FILE *fh = fopen(path, "rb");
    if (!fh)
        return -1;
    char chunk[65536];
    size_t got;
    while ((got = fread(chunk, 1, sizeof chunk, fh)) > 0)
        if (buf_put(out, chunk, got) != 0) {
            fclose(fh);
            return -1;
        }
    fclose(fh);
    return 0;
}

static char *swap_ext(const char *path, const char *ext)
{
    size_t n = strlen(path);
    const char *dot = strrchr(path, '.');
    size_t base = dot && dot != path ? (size_t)(dot - path) : n;
    char *out = malloc(base + strlen(ext) + 1);
    if (!out)
        return NULL;
    memcpy(out, path, base);
    strcpy(out + base, ext);
    return out;
}

static int usage(void)
{
    fprintf(stderr,
        "StrideXZ -- a table codec that lays bytes out so xz can copy them cheaply.\n"
        "\n"
        "  stridexz compress <table.csv> [out.sxz]\n"
        "  stridexz restore  <table.sxz> [out.csv]\n"
        "  stridexz info     <table.sxz>\n"
        "\n"
        "Options for compress:\n"
        "  --no-sort     skip the row-reorder candidate (faster, usually bigger)\n"
        "  --no-dict     skip dictionary coding of low-cardinality columns\n"
        "  --no-pool     keep the original column order\n"
        "  --plain       all of the above: the bare column layout\n"
        "  --rows N      compress N rows at a time so peak memory is one block,\n"
        "                not one file. Costs a little compression (~0.3%% at\n"
        "                50000 rows) and turns the sort guard off. Use it when\n"
        "                the table does not fit: expansion is about 8x input.\n");
    return 2;
}

static int cmd_compress(int argc, char **argv)
{
    const char *in = NULL, *outpath = NULL;
    SxzOptions opt = SXZ_DEFAULTS;

    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--no-sort")) { opt.use_sort = 0; opt.guard = 0; }
        else if (!strcmp(argv[i], "--no-dict")) opt.use_dict = 0;
        else if (!strcmp(argv[i], "--no-pool")) opt.use_pool = 0;
        else if (!strcmp(argv[i], "--plain")) {
            opt.use_sort = opt.guard = opt.use_dict = opt.use_pool = 0;
        }
        else if (!strcmp(argv[i], "--rows") && i + 1 < argc) {
            char *endp = NULL;
            long long v = strtoll(argv[++i], &endp, 10);
            if (!endp || *endp || v < 1) {
                fprintf(stderr, "--rows needs a positive row count\n");
                return 2;
            }
            opt.block_rows = (size_t)v;
            opt.guard = 0;       /* choosing per block would mean two passes */
        }
        else if (argv[i][0] == '-') { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
        else if (!in) in = argv[i];
        else if (!outpath) outpath = argv[i];
        else return usage();
    }
    if (!in)
        return usage();

    const char *why = input_refusal(in);
    if (why) {
        fprintf(stderr, "stridexz: %s looks like %s.\n", in, why);
        fprintf(stderr, "  This reader understands comma-separated text only. "
                        "Convert it to CSV first.\n");
        return 1;
    }

    char *err = NULL;
    Buf blob;
    buf_init(&blob);
    size_t nrows = 0, ncols = 0;
    if (sxz_encode_file(in, opt, &blob, &nrows, &ncols, &err) != 0) {
        fprintf(stderr, "stridexz: %s: %s\n", in, err ? err : "encode failed");
        free(err);
        buf_free(&blob);
        return 1;
    }

    /* Verify before writing. Without --rows the whole table fits, so the
     * strongest check is available: decode the finished archive and compare
     * every cell. With --rows it is NOT available -- re-reading the file to
     * compare would undo the very thing --rows exists for -- so each block was
     * instead checked against its source rows at the moment it was built, by
     * the decoder's own unpackers. Every cell is still checked either way. */
    if (opt.block_rows == 0) {
        Table src, back;
        if (table_read_csv(in, &src, &err) != 0) {
            fprintf(stderr, "stridexz: %s: %s\n", in, err ? err : "cannot re-read");
            free(err);
            buf_free(&blob);
            return 1;
        }
        if (sxz_decode(blob.p, blob.len, &back, &err) != 0 ||
            !tables_equal(&src, &back)) {
            fprintf(stderr, "stridexz: round-trip check FAILED -- refusing to write.\n");
            if (err) fprintf(stderr, "  %s\n", err);
            free(err);
            table_free(&back);
            table_free(&src);
            buf_free(&blob);
            return 1;
        }
        table_free(&back);
        table_free(&src);
    }

    char *chosen = outpath ? strdup(outpath) : swap_ext(in, ".sxz");
    FILE *fh = fopen(chosen, "wb");
    if (!fh || fwrite(blob.p, 1, blob.len, fh) != blob.len) {
        fprintf(stderr, "stridexz: cannot write %s\n", chosen);
        if (fh) fclose(fh);
        free(chosen);
        buf_free(&blob);
        return 1;
    }
    fclose(fh);

    /* stat, not slurp. Reading the input back just to print a ratio put the
     * whole file in memory again -- which on a 1.36 GB table was most of the
     * peak, in the one code path whose entire purpose is not doing that. */
    struct stat st;
    size_t insize = (stat(in, &st) == 0 && st.st_size > 0) ? (size_t)st.st_size : 0;
    double ratio = blob.len ? (double)insize / (double)blob.len : 0.0;
    printf("%s -> %s  %zu rows x %zu cols  %zu -> %zu bytes  %.2fx\n",
           in, chosen, nrows, ncols, insize, blob.len, ratio);
    free(chosen);
    buf_free(&blob);
    return 0;
}

static int cmd_restore(int argc, char **argv)
{
    if (argc < 1)
        return usage();
    Buf blob;
    buf_init(&blob);
    if (slurp(argv[0], &blob) != 0) {
        fprintf(stderr, "stridexz: cannot read %s\n", argv[0]);
        buf_free(&blob);
        return 1;
    }
    char *err = NULL;
    Table t;
    if (sxz_decode(blob.p, blob.len, &t, &err) != 0) {
        fprintf(stderr, "stridexz: %s: %s\n", argv[0], err ? err : "corrupt archive");
        free(err);
        buf_free(&blob);
        return 1;
    }
    buf_free(&blob);

    char *chosen = argc > 1 ? strdup(argv[1]) : swap_ext(argv[0], ".csv");
    FILE *fh = fopen(chosen, "wb");
    if (!fh || table_write_csv(&t, fh) != 0) {
        fprintf(stderr, "stridexz: cannot write %s\n", chosen);
        if (fh) fclose(fh);
        free(chosen);
        table_free(&t);
        return 1;
    }
    fclose(fh);
    printf("%s -> %s  %zu rows x %zu cols\n", argv[0], chosen, t.nrows, t.ncols);
    free(chosen);
    table_free(&t);
    return 0;
}

static int cmd_info(int argc, char **argv)
{
    if (argc < 1)
        return usage();
    Buf blob;
    buf_init(&blob);
    if (slurp(argv[0], &blob) != 0) {
        fprintf(stderr, "stridexz: cannot read %s\n", argv[0]);
        buf_free(&blob);
        return 1;
    }
    char *err = NULL;
    Table t;
    if (sxz_decode(blob.p, blob.len, &t, &err) != 0) {
        fprintf(stderr, "stridexz: %s: %s\n", argv[0], err ? err : "corrupt archive");
        free(err);
        buf_free(&blob);
        return 1;
    }
    printf("%s\n  %zu rows x %zu columns\n  %zu bytes on disk\n",
           argv[0], t.nrows, t.ncols, blob.len);
    for (size_t c = 0; c < t.ncols && c < 40; c++)
        printf("    %.*s\n", (int)t.names.v[c].n, t.names.v[c].s);
    if (t.ncols > 40)
        printf("    ... and %zu more\n", t.ncols - 40);
    table_free(&t);
    buf_free(&blob);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2)
        return usage();
    if (!strcmp(argv[1], "compress")) return cmd_compress(argc - 2, argv + 2);
    if (!strcmp(argv[1], "restore"))  return cmd_restore(argc - 2, argv + 2);
    if (!strcmp(argv[1], "info"))     return cmd_info(argc - 2, argv + 2);
    return usage();
}
