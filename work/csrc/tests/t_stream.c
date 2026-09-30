/* The streaming container (ppz_stream.c): files bigger than memory.
 *
 *   "PPZS" 0 0 0 0 | block | block | ... | header | header length (8, BE)
 *
 * test_stream.py was written because stream.py had no tests at all and
 * shipped three bugs a round-trip suite catches:
 *
 *   - a trailing block was emitted unconditionally, so a row count that
 *     divided evenly by the block size paid for an extra empty block
 *   - restore ignored the output extension and always wrote CSV
 *   - the documented invocation did not run
 *
 * So the checks here are: the block COUNT is exactly what the arithmetic
 * says, every output format reads back as the same table, each block really
 * is a standalone archive, and the table survives wherever the block
 * boundaries fall. Boundaries are the interesting axis -- the reordering is
 * per block -- so every case runs at several block sizes, including 1 and one
 * larger than the table, and 40 and 120, which divide the 120-row cases
 * exactly (the case that produced the spurious empty block).
 */

#include "harness.h"

#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

/* The source files are written here, by the test, with every field quoted --
 * not by the program's own writer, whose round trip is t_io's business. */
static void write_csv(const char *path, const Table *t)
{
    Buf b;
    buf_init(&b);
    if (t->ncols) {
        for (size_t j = 0; j < t->ncols; j++) {
            if (j) buf_putc(&b, ',');
            buf_putc(&b, '"');
            for (const char *s = t->names[j]; *s; s++) {
                if (*s == '"') buf_putc(&b, '"');
                buf_putc(&b, *s);
            }
            buf_putc(&b, '"');
        }
        buf_putc(&b, '\n');
    }
    for (size_t i = 0; i < t->nrows; i++) {
        for (size_t j = 0; j < t->ncols; j++) {
            if (j) buf_putc(&b, ',');
            Str s = table_at(t, i, j);
            buf_putc(&b, '"');
            for (size_t k = 0; k < s.n; k++) {
                if (s.p[k] == '"') buf_putc(&b, '"');
                buf_putc(&b, s.p[k]);
            }
            buf_putc(&b, '"');
        }
        buf_putc(&b, '\n');
    }
    write_bytes(path, b.data, b.len);
    buf_free(&b);
}

#define NR 120

static int make_case(int k, const char **name, Table *t)
{
    TB b;
    switch (k) {
    case 0:     /* the shape the reordering is built for: a child column
                 * determined by a parent, which only collapses if both land
                 * in the same block */
        *name = "parent_child";
        tb_start(&b, 3, (const char *const[]){ "zip", "city", "n" });
        for (int i = 0; i < NR; i++) {
            tb_cellz(&b, (const char *[]){ "98101", "98402", "98501" }[i % 3]);
            tb_cellz(&b, (const char *[]){ "Seattle", "Tacoma", "Olympia" }[i % 3]);
            tb_cellf(&b, "%d", i);
        }
        break;
    case 1:     /* commensurable numeric columns -> 2D grouping in each block */
        *name = "numeric_2d";
        tb_start(&b, 3, (const char *const[]){ "a", "b", "c" });
        for (int i = 0; i < NR; i++)
            for (int j = 0; j < 3; j++) tb_cellf(&b, "%.2f", 1.0 + i * 0.01 + j * 0.05);
        break;
    case 2:     /* embedded delimiters, quotes, newlines, unicode */
        *name = "awkward";
        tb_start(&b, 2, (const char *const[]){ "text", "n" });
        for (int i = 0; i < NR; i++) {
            tb_cellf(&b, "a,b\"c\nd \xc3\xa9\xc3\xbc\xe4\xb8\xad\xe6\x96\x87 %d", i);
            tb_cellf(&b, "%d", i);
        }
        break;
    case 3:     /* empty strings and whitespace, which the numeric parser rejects */
        *name = "empties";
        tb_start(&b, 2, (const char *const[]){ "a", "b" });
        for (int i = 0; i < NR; i++) { tb_cellz(&b, i % 3 == 0 ? "" : "  "); tb_cellf(&b, "%d", i); }
        break;
    case 4:
        *name = "single_row";
        tb_start(&b, 2, (const char *const[]){ "x", "y" });
        tb_row(&b, "1", "2", NULL);
        break;
    case 5:     /* header but no rows: the one case that needs a block anyway */
        *name = "no_rows";
        tb_start(&b, 2, (const char *const[]){ "a", "b" });
        break;
    case 6:     /* numeric with a rare exception, which may land in any block */
        *name = "exceptions";
        tb_start(&b, 2, (const char *const[]){ "v", "k" });
        for (int i = 0; i < NR; i++) {
            if (i == 41 || i == 80) tb_cellz(&b, "");
            else tb_cellf(&b, "%.2f", 3.0 + i * 0.25);
            tb_cellf(&b, "k%d", i % 4);
        }
        break;
    case 7:     /* an empty file: no header, no columns */
        *name = "empty_file";
        tb_start(&b, 0, NULL);
        break;
    default:
        return 0;
    }
    tb_finish(&b, t);
    return 1;
}

/* Each block is a complete single-shot archive: read the index, slice the
 * file by it, decode every slice, and the rows in order must be the table. */
static void check_blocks_by_hand(const char *label, const char *arc, const Table *t,
                                 size_t rows, int compare_encode)
{
    StreamInfo h;
    char err[512], why[256];
    if (!CHECK(ppz_stream_info(arc, &h, err, sizeof(err)) == 0, "%s: info: %s", label, err)) return;
    Buf f;
    read_bytes(arc, &f);
    CHECK(f.len >= 16 && !memcmp(f.data, "PPZS\0\0\0\0", 8), "%s: file does not open PPZS 0000", label);
    uint64_t hl = 0;
    for (int i = 0; i < 8; i++) hl = (hl << 8) | f.data[f.len - 8 + i];
    uint64_t sum = 0;
    for (size_t i = 0; i < h.nblocks; i++) sum += h.blocks[i];
    CHECK(8 + sum + hl + 8 == f.len, "%s: blocks + index + lengths = %llu, file is %zu", label,
          (unsigned long long)(8 + sum + hl + 8), f.len);
    size_t at = 8, row = 0;
    int ok = 1;
    for (size_t i = 0; i < h.nblocks && ok; i++) {
        Table bt;
        if (!CHECK(ppz_decode(f.data + at, h.blocks[i], &bt) == 0, "%s: block %zu does not decode alone", label, i)) {
            ok = 0;
            break;
        }
        ok &= CHECK(bt.nrows <= (rows ? rows : bt.nrows) && bt.ncols == t->ncols,
                    "%s: block %zu has %zu rows x %zu cols", label, i, bt.nrows, bt.ncols);
        ok &= CHECK(i + 1 == h.nblocks || bt.nrows == rows,
                    "%s: block %zu of %zu is short (%zu rows)", label, i, h.nblocks, bt.nrows);
        for (size_t r = 0; r < bt.nrows && ok; r++, row++)
            for (size_t j = 0; j < bt.ncols; j++) {
                Str x = table_at(&bt, r, j), y = table_at(t, row, j);
                if (x.n != y.n || memcmp(x.p, y.p, x.n)) {
                    ok = CHECK(0, "%s: block %zu row %zu col %zu differs", label, i, r, j);
                    break;
                }
            }
        /* the stream writes each block with the serial encoder; threads
         * change nothing, so ppz_encode must write the very same bytes */
        if (compare_encode && ok) {
            Buf z;
            buf_init(&z);
            ok &= CHECK(ppz_encode(&bt, &z) == 0 && z.len == h.blocks[i]
                        && !memcmp(z.data, f.data + at, z.len),
                        "%s: block %zu is not what ppz_encode writes for its rows", label, i);
            buf_free(&z);
        }
        table_free(&bt);
        at += h.blocks[i];
    }
    CHECK(!ok || row == t->nrows, "%s: blocks hold %zu rows, table has %zu", label, row, t->nrows);
    (void)why;
    buf_free(&f);
    ppz_stream_info_free(&h);
}

static void cases(void)
{
    h_section("stream-compress, info and restore, at every block size");
    const size_t BLOCKS[] = { 1, 7, 40, 120, 10000 };
    const char *fmts[] = { "csv", "tsv", "json", "jsonl", "psv" };
    Table t;
    const char *name;
    for (int k = 0; make_case(k, &name, &t); k++) {
        char src[1280];
        snprintf(src, sizeof(src), "%s", tpath("%s.csv", name));
        write_csv(src, &t);
        for (size_t bi = 0; bi < N_OF(BLOCKS); bi++) {
            size_t rows = BLOCKS[bi];
            char label[96], err[1024] = "", why[512];
            snprintf(label, sizeof(label), "%s @%zu", name, rows);
            const char *arc = tpath("%s-%zu.ppz", name, rows);
            StreamStats st;
            int rc = ppz_stream_compress(src, arc, 1.0, rows, 1, NULL, NULL, &st, err, sizeof(err));
            if (!CHECK(rc == 0, "%s: stream-compress failed: %s", label, err)) continue;

            /* block count is exactly ceil(rows / per block), with a floor of
             * 1 so a header-only table still records its columns */
            size_t want = t.nrows ? (t.nrows + rows - 1) / rows : 1;
            CHECK(st.blocks == want, "%s: %zu blocks, expected %zu", label, st.blocks, want);
            CHECK(st.rows == t.nrows && st.rows_per_block == rows,
                  "%s: stats say %llu rows, %zu per block", label, st.rows, st.rows_per_block);
            struct stat sb;
            stat(arc, &sb);
            CHECK(st.bytes == (uint64_t)sb.st_size, "%s: stats say %llu bytes, file is %lld", label,
                  (unsigned long long)st.bytes, (long long)sb.st_size);

            StreamInfo h;
            if (CHECK(ppz_stream_info(arc, &h, err, sizeof(err)) == 0, "%s: info: %s", label, err)) {
                CHECK(h.nblocks == want && h.nrows == t.nrows && h.ncols == t.ncols
                      && h.rows_per_block == rows && h.size == (uint64_t)sb.st_size,
                      "%s: info says %zu blocks, %llu rows, %zu cols, %zu per block, %llu B",
                      label, h.nblocks, h.nrows, h.ncols, h.rows_per_block,
                      (unsigned long long)h.size);
                for (size_t j = 0; j < h.ncols && j < t.ncols; j++)
                    CHECK(!strcmp(h.columns[j], t.names[j]), "%s: info column %zu is %s", label, j,
                          h.columns[j]);
                ppz_stream_info_free(&h);
            }
            check_blocks_by_hand(label, arc, &t, rows, bi == 1 || bi == 4);

            for (size_t fi = 0; fi < N_OF(fmts); fi++) {
                const char *dst = tpath("%s-%zu-out.%s", name, rows, fmts[fi]);
                StreamStats rs;
                rc = ppz_stream_restore(arc, dst, &rs, err, sizeof(err));
                /* JSON Lines cannot carry the column names of a table with no
                 * rows; the documented limit must leave no file behind */
                if (!strcmp(fmts[fi], "jsonl") && t.nrows == 0 && t.ncols) {
                    CHECK(rc != 0 && !file_exists(dst), "%s .jsonl: zero rows accepted", label);
                    continue;
                }
                if (!CHECK(rc == 0, "%s .%s: restore failed: %s", label, fmts[fi], err)) continue;
                CHECK(rs.rows == t.nrows && rs.blocks == want, "%s .%s: restore stats %llu rows %zu blocks",
                      label, fmts[fi], rs.rows, rs.blocks);
                Table back;
                if (CHECK(table_read_any(&back, dst, NULL, err, sizeof(err)) == 0, "%s .%s: read back: %s",
                          label, fmts[fi], err)) {
                    CHECK(table_same(&t, &back, why, sizeof(why)), "%s .%s: round trip differs: %s",
                          label, fmts[fi], why);
                    table_free(&back);
                }
            }
        }
        table_free(&t);
    }
}

static void options(void)
{
    h_section("budget-planned blocks, --no-verify, encodings, refusals");
    char err[1024], why[256];
    StreamStats st;

    /* rows = 0: planned from the budget and the file's own row width */
    TB b;
    tb_start(&b, 3, (const char *const[]){ "id", "k", "v" });
    for (int i = 0; i < 5000; i++) { tb_cellf(&b, "%d", i); tb_cellf(&b, "k%d", i % 9); tb_cellf(&b, "%.1f", i * 0.5); }
    Table t;
    tb_finish(&b, &t);
    char src[1280];
    snprintf(src, sizeof(src), "%s", tpath("plan.csv"));
    write_csv(src, &t);
    char arc[1280];
    snprintf(arc, sizeof(arc), "%s", tpath("plan.ppz"));
    if (CHECK(ppz_stream_compress(src, arc, 0.25, 0, 1, NULL, NULL, &st, err, sizeof(err)) == 0,
              "planned stream-compress: %s", err)) {
        CHECK(st.rows_per_block >= 1000 && st.rows_per_block <= 2000000,
              "planned rows per block %zu is outside [1000, 2000000]", st.rows_per_block);
        const char *out = tpath("plan-out.csv");
        Table back;
        if (CHECK(ppz_stream_restore(arc, out, &st, err, sizeof(err)) == 0, "restore: %s", err)
            && CHECK(table_read_any(&back, out, NULL, err, sizeof(err)) == 0, "read: %s", err)) {
            CHECK(table_same(&t, &back, why, sizeof(why)), "planned round trip: %s", why);
            table_free(&back);
        }
    }
    /* verify off writes the same bytes: verification only reads */
    {
        const char *a1 = tpath("v1.ppz"), *a0 = tpath("v0.ppz");
        Buf x, y;
        CHECK(ppz_stream_compress(src, a1, 1.0, 700, 1, NULL, NULL, &st, err, sizeof(err)) == 0
              && ppz_stream_compress(src, a0, 1.0, 700, 0, NULL, NULL, &st, err, sizeof(err)) == 0,
              "verify on/off: %s", err);
        read_bytes(a1, &x);
        read_bytes(a0, &y);
        CHECK(x.len == y.len && !memcmp(x.data, y.data, x.len), "verify off changed the archive");
        buf_free(&x); buf_free(&y);
    }
    /* Streaming encodes serially (its budget promises a memory ceiling) but
     * must hand the process back as it found it. */
    {
        int before = ppz_nthreads();
        ppz_stream_compress(src, tpath("s.ppz"), 1.0, 2000, 0, NULL, NULL, &st, err, sizeof(err));
        CHECK(ppz_nthreads() == before, "after stream-compress ppz_nthreads() is %d, was %d",
              ppz_nthreads(), before);
        ppz_set_serial(1);
        ppz_stream_compress(src, tpath("s.ppz"), 1.0, 2000, 0, NULL, NULL, &st, err, sizeof(err));
        int after = ppz_nthreads();
        ppz_set_serial(0);
        CHECK(after == 1, "stream-compress cleared the caller's ppz_set_serial(1) "
              "(ppz_nthreads() is %d afterwards)", after);
    }
    table_free(&t);

    /* latin-1: refused unnamed, block-at-a-time reader included; read when
     * named. The bad byte is in the first block here, and deep in the file
     * in the second case. */
    {
        Buf f;
        buf_init(&f);
        buf_put(&f, "city,note\n", 10);
        const char *rows[3][2] = { { "Z\xfcrich", "caf\xe9" }, { "Malm\xf6", "na\xefve" },
                                   { "M\xfcnchen", "r\xe9sum\xe9" } };
        for (int i = 0; i < 4000; i++) {
            buf_put(&f, rows[i % 3][0], strlen(rows[i % 3][0]));
            buf_putc(&f, ',');
            buf_put(&f, rows[i % 3][1], strlen(rows[i % 3][1]));
            buf_putc(&f, '\n');
        }
        const char *p = tpath("big_latin1.csv");
        write_bytes(p, f.data, f.len);
        buf_free(&f);
        const char *dst = tpath("big_latin1.ppz");
        err[0] = 0;
        int rc = ppz_stream_compress(p, dst, 1.0, 500, 1, NULL, NULL, &st, err, sizeof(err));
        CHECK(rc != 0 && strstr(err, "0xFC") && strstr(err, "--encoding"),
              "latin-1 by the streaming reader: rc %d: %s", rc, err);
        CHECK(!file_exists(dst) && !dir_has_prefix("big_latin1.ppz."), "a refused stream left a file behind");
        rc = ppz_stream_compress(p, dst, 1.0, 500, 1, "latin-1", NULL, &st, err, sizeof(err));
        if (CHECK(rc == 0, "latin-1 named: %s", err)) {
            const char *out = tpath("big_back.csv");
            Table back;
            if (CHECK(ppz_stream_restore(dst, out, &st, err, sizeof(err)) == 0, "restore: %s", err)
                && CHECK(table_read_any(&back, out, NULL, err, sizeof(err)) == 0, "read: %s", err)) {
                Str c = table_at(&back, 0, 0), n = table_at(&back, 2, 1);
                CHECK(back.nrows == 4000 && back.ncols == 2 && c.n == 7 && !memcmp(c.p, "Z\xc3\xbcrich", 7)
                      && n.n == 8 && !memcmp(n.p, "r\xc3\xa9sum\xc3\xa9", 8),
                      "streamed latin-1 did not come back as UTF-8");
                table_free(&back);
            }
        }
        /* a bad byte deep in the file surfaces mid-stream, and the half
         * written archive is removed */
        buf_init(&f);
        buf_put(&f, "a,b\n", 4);
        for (int i = 0; i < 200000; i++) { char s[32]; int n = snprintf(s, sizeof(s), "%d,x%d\n", i, i % 5); buf_put(&f, s, (size_t)n); }
        buf_put(&f, "7,caf\xe9\n", 7);
        p = tpath("late_bad.csv");
        write_bytes(p, f.data, f.len);
        buf_free(&f);
        dst = tpath("late_bad.ppz");
        rc = ppz_stream_compress(p, dst, 1.0, 50000, 1, NULL, NULL, &st, err, sizeof(err));
        CHECK(rc != 0 && strstr(err, "0xE9"), "a late bad byte: rc %d: %s", rc, err);
        CHECK(!file_exists(dst) && !dir_has_prefix("late_bad.ppz."), "a failed stream left a file behind");
    }
    /* a row wider than the header, in the third block */
    {
        Buf f;
        buf_init(&f);
        buf_put(&f, "a,b\n", 4);
        for (int i = 0; i < 2500; i++) { char s[32]; int n = snprintf(s, sizeof(s), "%d,y\n", i); buf_put(&f, s, (size_t)n); }
        buf_put(&f, "1,2,3\n", 6);
        const char *p = tpath("wide.csv"), *dst = tpath("wide.ppz");
        write_bytes(p, f.data, f.len);
        buf_free(&f);
        int rc = ppz_stream_compress(p, dst, 1.0, 1000, 1, NULL, NULL, &st, err, sizeof(err));
        CHECK(rc != 0 && strstr(err, "row 2502"), "a wide row mid-stream: rc %d: %s", rc, err);
        CHECK(!file_exists(dst) && !dir_has_prefix("wide.ppz."), "a failed stream left a file behind");
    }
    /* missing input, unwritable output */
    CHECK(ppz_stream_compress(tpath("nope.csv"), tpath("nope.ppz"), 1.0, 10, 1, NULL, NULL, &st,
                              err, sizeof(err)) != 0 && strstr(err, "no such file"),
          "missing input: %s", err);
    CHECK(ppz_stream_compress(src, tpath("no/dir/x.ppz"), 1.0, 10, 1, NULL, NULL, &st, err,
                              sizeof(err)) != 0, "unwritable output accepted");
    CHECK(ppz_stream_restore(arc, tpath("no/dir/x.csv"), &st, err, sizeof(err)) != 0,
          "restore into a missing directory accepted");
    /* the TSV and PSV readers stream too */
    {
        const char *p = tpath("t.tsv"), *dst = tpath("t.tsv.ppz"), *out = tpath("t-out.csv");
        write_str(p, "a\tb\n1,5\tx\n2\ty\n3\t\n");
        Table w, back;
        TB tb;
        tb_start(&tb, 2, (const char *const[]){ "a", "b" });
        tb_row(&tb, "1,5", "x", "2", "y", "3", "", NULL);
        tb_finish(&tb, &w);
        if (CHECK(ppz_stream_compress(p, dst, 1.0, 2, 1, NULL, NULL, &st, err, sizeof(err)) == 0, "tsv: %s", err)
            && CHECK(ppz_stream_restore(dst, out, &st, err, sizeof(err)) == 0, "tsv restore: %s", err)
            && CHECK(table_read_any(&back, out, NULL, err, sizeof(err)) == 0, "tsv read: %s", err)) {
            CHECK(table_same(&w, &back, why, sizeof(why)), "tsv stream: %s", why);
            table_free(&back);
        }
        table_free(&w);
    }
}

int main(void)
{
    h_suite = "stream";
    setvbuf(stdout, NULL, _IOLBF, 0);
    tmpdir_make("stream");
    cases();
    options();
    tmpdir_remove();
    return h_done();
}
