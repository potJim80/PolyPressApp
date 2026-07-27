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
                "  polypress restore FILE [-o OUT]   .ppz -> csv\n"
                "  polypress info    FILE            what is inside\n\n"
                "Writing archives is still the Python CLI: tzip.py compress\n");
        return 2;
    }
    if (!strcmp(argv[1], "restore")) return cmd_restore(argc - 2, argv + 2);
    if (!strcmp(argv[1], "info"))    return cmd_info(argc - 2, argv + 2);
    fprintf(stderr, "polypress: unknown command %s\n", argv[1]);
    return 2;
}
