/* Real data: the xs_ and s_ tables of the standard suite (IN/suite).
 *
 *     t_suite DIR
 *
 * Optional: the suite is generated data and gitignored, so when DIR is
 * missing or empty this reports a skip and passes. The constructed cases in
 * t_codec cover the shapes someone thought of; real tables are how the
 * front-coding loop bug of 2026-07-30 was found (a Colombian pharmaceutical
 * register whose 26 string groups were all alphabets). Only the two small
 * tiers, to keep this under a minute on a fanless laptop.
 *
 * Per table: it reads, round-trips cell for cell, is never larger than plain
 * xz or bzip2 of its canonical CSV, and (xs_ only, to keep it quick) the
 * serial encoder writes the same bytes as the threaded one.
 */

#include "harness.h"

#include <dirent.h>

static int by_name(const void *a, const void *b)
{
    return strcmp(*(char *const *)a, *(char *const *)b);
}

int main(int argc, char **argv)
{
    h_suite = "suite";
    setvbuf(stdout, NULL, _IOLBF, 0);
    DIR *d = argc > 1 && argv[1][0] ? opendir(argv[1]) : NULL;
    if (!d) {
        printf("-- IN/suite not present: skipped\n");
        return h_done();
    }
    char *names[256];
    size_t n = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL && n < 256) {
        size_t l = strlen(e->d_name);
        if (l > 4 && !strcmp(e->d_name + l - 4, ".csv")
            && (!strncmp(e->d_name, "xs_", 3) || !strncmp(e->d_name, "s_", 2)))
            names[n++] = strdup(e->d_name);
    }
    closedir(d);
    qsort(names, n, sizeof(char *), by_name);
    if (!n) printf("-- no xs_ or s_ tables in %s: skipped\n", argv[1]);

    for (size_t k = 0; k < n; k++) {
        char path[2048], err[1024], why[512];
        snprintf(path, sizeof(path), "%s/%s", argv[1], names[k]);
        double t0 = h_now();
        Table t;
        if (!CHECK(table_read_any(&t, path, NULL, err, sizeof(err)) == 0, "%s: %s", names[k], err)) continue;
        Buf blob, canon, xz, bz;
        buf_init(&blob); buf_init(&canon); buf_init(&xz); buf_init(&bz);
        int rc = ppz_encode(&t, &blob);
        Table back;
        if (CHECK(rc == 0 && ppz_decode(blob.data, blob.len, &back) == 0, "%s: encode/decode", names[k])) {
            CHECK(table_same(&t, &back, why, sizeof(why)), "%s: round trip: %s", names[k], why);
            table_free(&back);
        }
        table_write_canonical(&t, &canon);
        Table ct;
        int survives = table_parse_csv(&ct, canon.data, canon.len) == 0;
        if (survives) { survives = table_same(&t, &ct, why, sizeof(why)); table_free(&ct); }
        if (survives) {
            /* no longer a promise (one pass, 2026-09-29) -- reported, so a
             * table where plain xz wins is seen rather than hidden */
            ppz_lzma_compress(canon.data, canon.len, &xz);
            if (blob.len > 4 + xz.len)
                printf("   note: %s is %zu B, plain xz would be %zu B\n", names[k],
                       blob.len, 4 + xz.len);
        }
        if (!strncmp(names[k], "xs_", 3)) {
            Buf ser;
            buf_init(&ser);
            ppz_set_serial(1);
            rc = ppz_encode(&t, &ser);
            ppz_set_serial(0);
            CHECK(rc == 0 && ser.len == blob.len && !memcmp(ser.data, blob.data, blob.len),
                  "%s: serial and threaded encodes differ", names[k]);
            buf_free(&ser);
        }
        printf("   %-24s %6zu rows x %3zu cols  %8zu B  %.4s  %.1f s\n", names[k], t.nrows, t.ncols,
               blob.len, blob.data, h_now() - t0);
        buf_free(&blob); buf_free(&canon); buf_free(&xz); buf_free(&bz);
        table_free(&t);
        free(names[k]);
    }
    return h_done();
}
