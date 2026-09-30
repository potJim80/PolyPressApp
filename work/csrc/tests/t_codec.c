/* The codec's own contract: ppz_encode / ppz_decode.
 *
 *     t_codec [fuzz-count] [fuzz-seed]
 *
 * Four promises, checked on every table this file builds:
 *
 *   1. Round trip. The decoded table is the table: the same column names, the
 *      same row count, every cell the same bytes. Nothing else the format says
 *      matters if this fails.
 *   2. Old archives open. The encoder is one pass since 2026-09-29 and no
 *      longer writes the plain containers (PPZX = xz, PPZB = bzip2 over the
 *      canonical CSV) or promises to be smaller than them -- Mahdi traded
 *      that guarantee for speed. But archives written before are full of
 *      them, so for every table both are built here and must decode.
 *   3. Threads change nothing. The threaded encoder writes exactly the bytes
 *      the serial one does. That was the promise when the encoder was
 *      threaded on 2026-09-28; this checks it on every hand-built table and a
 *      share of the fuzzed ones.
 *   4. The modelled container round-trips by itself, and ppz_encode writes
 *      it (PPZ1) for every table whose column names are UTF-8.
 *
 * The hand-built tables are the ones from the retired test_dtz.py and
 * test_fast.py (each keeps the reason it was written), plus shapes the port
 * needed: the empty table, NUL and CR in cells, the dictionary-width
 * boundaries at 256 and 65,536 distinct values, and so on. After them comes a
 * seeded random-table fuzzer (test_fuzz.py's generators, and more).
 *
 * The C program is the definition now. Nothing here compares against another
 * implementation -- only against the contract.
 */

#include "harness.h"

#include <bzlib.h>
#include <lzma.h>

static int g_threads_checks = 1;      /* off for the few very large tables */

/* -------------------------------------------------------------- the checks */

static int is_magic(const Buf *b, const char *m)
{
    return b->len >= 4 && !memcmp(b->data, m, 4);
}

/* Everything this file promises, for one table. `quiet_threads` skips the
 * two extra encodes that promise 3 costs. Returns 1 if every check passed. */
static int check_table(const char *name, const Table *t, int check_threads)
{
    int ok = 1;
    char why[512];
    Buf blob;
    buf_init(&blob);

    /* 1. round trip */
    int rc = ppz_encode(t, &blob);
    ok &= CHECK(rc == 0, "%s: ppz_encode failed (rc %d)", name, rc);
    if (rc) { buf_free(&blob); return 0; }
    ok &= CHECK(is_magic(&blob, PPZ_MAGIC) || is_magic(&blob, PPZ_MAGIC_RAW_XZ)
                || is_magic(&blob, PPZ_MAGIC_RAW_BZ),
                "%s: archive starts with an unknown magic", name);
    Table back;
    rc = ppz_decode(blob.data, blob.len, &back);
    ok &= CHECK(rc == 0, "%s: ppz_decode refused its own archive (%.4s)", name, blob.data);
    if (!rc) {
        ok &= CHECK(table_same(t, &back, why, sizeof(why)),
                    "%s: round trip through %.4s differs: %s", name, blob.data, why);
        table_free(&back);
    }

    /* 4. the modelled container on its own */
    Buf mod;
    buf_init(&mod);
    long fired = -1;
    rc = ppz_encode_modelled(t, &mod, &fired);
    /* A column name that is not UTF-8 cannot go in the JSON metadata, so the
     * modelled container must REFUSE it -- never rename the column -- and
     * ppz_encode must then have used a plain container, which stores bytes.
     * (A name with any high byte is only a candidate; the encoder decides.) */
    int high = 0;
    for (size_t j = 0; j < t->ncols; j++)
        for (const unsigned char *p = (const unsigned char *)t->names[j]; *p; p++)
            if (*p >= 0x80) high = 1;
    int refused_ok = rc != 0 && high && !is_magic(&blob, PPZ_MAGIC);
    ok &= CHECK((rc == 0 && is_magic(&mod, PPZ_MAGIC)) || refused_ok,
                "%s: ppz_encode_modelled failed", name);
    if (!rc) {
        rc = ppz_decode(mod.data, mod.len, &back);
        ok &= CHECK(rc == 0, "%s: the modelled container does not decode", name);
        if (!rc) {
            ok &= CHECK(table_same(t, &back, why, sizeof(why)),
                        "%s: the modelled container round trip differs: %s", name, why);
            table_free(&back);
        }
    }

    /* 2. the plain containers older archives use still decode */
    Buf canon, xz, bz;
    buf_init(&canon); buf_init(&xz); buf_init(&bz);
    table_write_canonical(t, &canon);
    Table ct;
    int survives = table_parse_csv(&ct, canon.data, canon.len) == 0;
    if (survives) {
        survives = table_same(t, &ct, why, sizeof(why));
        table_free(&ct);
    }
    if (survives) {
        CHECK(ppz_lzma_compress(canon.data, canon.len, &xz) == 0, "%s: xz failed", name);
        CHECK(test_bz2_compress(canon.data, canon.len, &bz) == 0, "%s: bzip2 failed", name);
        const char *mg[2] = { PPZ_MAGIC_RAW_XZ, PPZ_MAGIC_RAW_BZ };
        Buf *body[2] = { &xz, &bz };
        for (int k = 0; k < 2; k++) {
            Buf fb;
            buf_init(&fb);
            buf_put(&fb, mg[k], 4);
            buf_put(&fb, body[k]->data, body[k]->len);
            rc = ppz_decode(fb.data, fb.len, &back);
            ok &= CHECK(rc == 0, "%s: a %s fallback container does not decode", name, mg[k]);
            if (!rc) {
                ok &= CHECK(table_same(t, &back, why, sizeof(why)),
                            "%s: the %s fallback round trip differs: %s", name, mg[k], why);
                table_free(&back);
            }
            buf_free(&fb);
        }
    } else {
        /* A table the canonical CSV cannot carry must never be shipped in it. */
        ok &= CHECK(is_magic(&blob, PPZ_MAGIC),
                    "%s: canonical CSV does not survive, yet the archive is %.4s",
                    name, blob.data);
    }

    /* 3. threads change nothing */
    if (check_threads && g_threads_checks) {
        Buf ser, two;
        buf_init(&ser); buf_init(&two);
        ppz_set_serial(1);
        rc = ppz_encode(t, &ser);
        ppz_set_serial(0);
        ok &= CHECK(rc == 0 && ser.len == blob.len && !memcmp(ser.data, blob.data, blob.len),
                    "%s: serial encode differs from threaded (%zu B vs %zu B)",
                    name, ser.len, blob.len);
        setenv("PPZ_THREADS", "2", 1);
        rc = ppz_encode(t, &two);
        unsetenv("PPZ_THREADS");
        ok &= CHECK(rc == 0 && two.len == blob.len && !memcmp(two.data, blob.data, blob.len),
                    "%s: PPZ_THREADS=2 encode differs from the default (%zu B vs %zu B)",
                    name, two.len, blob.len);
        buf_free(&ser); buf_free(&two);
    }

    buf_free(&canon); buf_free(&xz); buf_free(&bz);
    buf_free(&mod);
    buf_free(&blob);
    return ok;
}

/* Build, check, free. */
static void run_case(const char *name, TB *b)
{
    Table t;
    tb_finish(b, &t);
    check_table(name, &t, 1);
    table_free(&t);
}

/* A one-column table from an array of C strings. */
static void col1(TB *b, const char *name, const char *const *vals, size_t n)
{
    const char *nm[1] = { name };
    tb_start(b, 1, nm);
    for (size_t i = 0; i < n; i++) tb_cellz(b, vals[i]);
}

#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

/* ------------------------------------------------- cases from test_dtz.py */

/* "The cases that break naive table tools: embedded delimiters and newlines,
 * quotes, unicode, ragged rows, empty values, duplicate headers, degenerate
 * shapes." */
static void cases_dtz(void)
{
    h_section("test_dtz cases");
    TB b;
    {
        const char *nm[] = { "a", "b" };
        tb_start(&b, 2, nm);
        tb_row(&b, "1", "2", NULL); tb_row(&b, "3", "4", NULL);
        run_case("simple", &b);
    }
    {
        const char *nm[] = { "name", "note" };
        tb_start(&b, 2, nm);
        tb_row(&b, "Smith, John", "hello, world", NULL);
        tb_row(&b, "Doe, Jane", "a,b,c", NULL);
        run_case("embedded_comma", &b);
    }
    {
        const char *v[] = { "say \"hi\"", "it's \"fine\"", "\"\"" };
        col1(&b, "q", v, 3);
        run_case("embedded_quote", &b);
    }
    {
        const char *v[] = { "line1\nline2", "a\r\nb", "trailing\n" };
        col1(&b, "text", v, 3);
        run_case("embedded_newline", &b);
    }
    {
        const char *nm[] = { "city", "emoji" };
        tb_start(&b, 2, nm);
        tb_row(&b, "\xc3\x9c" "ber", "\xf0\x9f\x98\x80", NULL);
        tb_row(&b, "\xe5\x8c\x97\xe4\xba\xac", "\xe2\x98\x83", NULL);
        tb_row(&b, "Krak\xc3\xb3w", "\xe2\x9c\x93", NULL);
        run_case("unicode", &b);
    }
    {
        const char *nm[] = { "a", "b", "c" };
        tb_start(&b, 3, nm);
        tb_row(&b, "", "", "", NULL); tb_row(&b, "1", "", "3", NULL);
        tb_row(&b, "", "2", "", NULL);
        run_case("empty_values", &b);
    }
    {
        const char *nm[] = { "x", "x", "y" };
        tb_start(&b, 3, nm);
        tb_row(&b, "1", "2", "3", NULL); tb_row(&b, "4", "5", "6", NULL);
        run_case("duplicate_headers", &b);
    }
    {
        const char *nm[] = { "only" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 50; i++) tb_cellf(&b, "%d", i);
        run_case("single_column", &b);
    }
    {
        const char *nm[] = { "a", "b", "c" };
        tb_start(&b, 3, nm);
        tb_row(&b, "1", "2", "3", NULL);
        run_case("single_row", &b);
    }
    {
        const char *nm[] = { "a", "b" };
        tb_start(&b, 2, nm);
        run_case("no_rows", &b);
    }
    {
        const char *nm[] = { "a" };
        tb_start(&b, 1, nm);
        tb_cellz(&b, "x");
        run_case("one_cell", &b);
    }
    {
        tb_start(&b, 80, NULL);
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 80; j++) tb_cellf(&b, "%d", i * j);
        run_case("wide_shallow", &b);
    }
    {
        const char *v[] = { "-0.50", "0.00", "-12.75", "3.10", "-0.05" };
        col1(&b, "v", v, 5);
        run_case("negatives_decimals", &b);
    }
    {
        const char *v[] = { "01234", "00501", "99950" };
        col1(&b, "zip", v, 3);
        run_case("leading_zeros_zip", &b);
    }
    {
        const char *nm[] = { "n" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 20; i++) tb_cellf(&b, "10000000000000000000000000000%02d", i);
        run_case("big_ints_1e30", &b);
    }
    {
        const char *v[] = { "1", "1.5", "abc", "", "-2", "1e10" };
        col1(&b, "m", v, 6);
        run_case("mixed_types", &b);
    }
    {
        const char *v[] = { "  leading", "trailing  ", "  both  ", "\ttab" };
        col1(&b, "w", v, 4);
        run_case("whitespace", &b);
    }
    {
        const char *nm[] = { "zip", "city", "state" };
        tb_start(&b, 3, nm);
        for (int k = 0; k < 20; k++) {
            tb_row(&b, "10001", "NYC", "NY", NULL);
            tb_row(&b, "10001", "NYC", "NY", NULL);
            tb_row(&b, "90210", "Beverly Hills", "CA", NULL);
            tb_row(&b, "90210", "Beverly Hills", "CA", NULL);
            tb_row(&b, "60601", "Chicago", "IL", NULL);
        }
        run_case("fd_present", &b);
    }
}

/* ------------------------------------------------ cases from test_fast.py */

static void cases_fast(void)
{
    h_section("test_fast cases: the numeric parser, 2D groups, parents, varints");
    TB b;
    char tmp[128];

    /* numeric edge cases -- these decide whether a column is parsed as a
     * scaled integer or falls back to text */
    { const char *v[] = { "007", "010", "000" }; col1(&b, "z", v, 3); run_case("leading_zeros", &b); }
    { const char *v[] = { "-0.0", "0.0", "-1.5" }; col1(&b, "z", v, 3); run_case("negative_zero", &b); }
    { const char *v[] = { "1.5", "1.50", "1.500" }; col1(&b, "d", v, 3); run_case("ragged_decimals", &b); }
    {
        const char *v[] = { "4611686018427387903", "-4611686018427387903", "0" };
        col1(&b, "n", v, 3);
        run_case("big_ints_2^62", &b);
    }
    {
        const char *v[] = { "9999999999999999999999999999999999999999", "1", "2" };
        col1(&b, "n", v, 3);
        run_case("huge_ints", &b);
    }
    /* The 2^62 acceptance boundary, from both sides and both signs. This is
     * where the C accelerator and the numpy reference used to disagree: the
     * parser checked for overflow *after* multiplying, and signed overflow
     * wraps negative, so INT64_MIN sailed past a limit of 2^62. */
    { const char *v[] = { "-9223372036854775808", "1", "2" }; col1(&b, "n", v, 3); run_case("int64_min", &b); }
    { const char *v[] = { "9223372036854775807", "1", "2" }; col1(&b, "n", v, 3); run_case("int64_max", &b); }
    {
        const char *nm[] = { "lo", "hi" };
        tb_start(&b, 2, nm);
        tb_row(&b, "-4611686018427387903", "4611686018427387903", NULL);
        tb_row(&b, "4611686018427387904", "-4611686018427387904", NULL);
        tb_row(&b, "0", "1", NULL);
        run_case("limit_edge", &b);
    }
    { const char *v[] = { "-0.01", "0.00", "0.01", "-99999.99" }; col1(&b, "v", v, 4); run_case("mixed_sign", &b); }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 300; i++) tb_cellf(&b, "%d", i * 7919);
        run_case("varint_escape", &b);
    }
    /* 2D grouping: commensurable columns should be grouped, and the codec
     * must still round-trip when they are not */
    {
        const char *nm[] = { "a", "b", "c", "d" };
        tb_start(&b, 4, nm);
        for (int i = 0; i < 200; i++)
            for (int j = 0; j < 4; j++) tb_cellf(&b, "%.2f", 1.0 + i * 0.01 + j * 0.05);
        run_case("commensurable_2d", &b);
    }
    {
        const char *nm[] = { "year", "tiny", "huge" };
        tb_start(&b, 3, nm);
        for (int i = 0; i < 120; i++) {
            tb_cellf(&b, "%d", 2000 + i);
            tb_cellf(&b, "%.3f", i / 1000.0);
            tb_cellf(&b, "%lld", (long long)i * 1000000000LL);
        }
        run_case("incommensurable", &b);
    }
    /* parent-sorted reordering: a child column strongly determined by a
     * parent, plus a tie-heavy parent to exercise the stable sort */
    {
        const char *nm[] = { "zip", "city", "noise" };
        tb_start(&b, 3, nm);
        for (int i = 0; i < 500; i++) {
            tb_cellf(&b, "%05d", 90000 + i % 40);
            tb_cellf(&b, "city%d", i % 40);
            tb_cellf(&b, "n%d", i % 7);
        }
        run_case("parent_child", &b);
    }
    {
        const char *nm[] = { "k", "v" };
        tb_start(&b, 2, nm);
        for (int i = 0; i < 200; i++) { tb_cellz(&b, "same"); tb_cellf(&b, "v%d", i % 3); }
        run_case("constant_parent", &b);
    }
    {
        const char *nm[] = { "u" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++) tb_cellf(&b, "value-%d", i);
        run_case("all_unique", &b);
    }
    {
        const char *nm[] = { "s" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 100; i++) tb_cellz(&b, "x");
        run_case("single_distinct", &b);
    }
    /* Numeric-with-exceptions. A column that is numeric apart from a few
     * cells used to be discarded to the dictionary path entirely, and on the
     * Treasury yield curve four blank cells in 72,048 cost 41% of the
     * archive. These force each way a cell can fail while the column stays
     * numeric. */
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i == 137) tb_cellz(&b, ""); else tb_cellf(&b, "%.2f", 1.0 + i * 0.01);
        run_case("one_blank", &b);
    }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i < 3) tb_cellz(&b, ""); else tb_cellf(&b, "%d", i * 3);
        run_case("blank_first", &b);
    }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i >= 397) tb_cellz(&b, ""); else tb_cellf(&b, "%d", i * 3);
        run_case("blank_last", &b);
    }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i == 200) tb_cellz(&b, "-0.0"); else tb_cellf(&b, "%.1f", i * 0.5 - 100);
        run_case("minus_zero_rare", &b);
    }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i == 55) tb_cellf(&b, "%.3f", (double)i); else tb_cellf(&b, "%.2f", (double)i);
        run_case("stray_decimals", &b);
    }
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i == 9) tb_cellz(&b, "007"); else tb_cellf(&b, "%d", i);
        run_case("leading_zero_rare", &b);
    }
    /* Just past the 5% screen, so the lenient path must decline it and the
     * column must stay a dictionary column. */
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i % 10 == 0) tb_cellz(&b, ""); else tb_cellf(&b, "%d", i);
        run_case("too_many_blanks", &b);
    }
    /* Exceptions inside a commensurable group: the group is rebuilt first
     * and the exceptions land afterwards, which is a different code path. */
    {
        const char *nm[] = { "a", "b", "c" };
        tb_start(&b, 3, nm);
        for (int i = 0; i < 200; i++)
            for (int j = 0; j < 3; j++)
                if (i == 100 && j == 1) tb_cellz(&b, "");
                else tb_cellf(&b, "%.2f", 1.0 + i * 0.01 + j * 0.05);
        run_case("grouped_with_blanks", &b);
    }
    /* A value past the 2^62 acceptance limit is an exception, not a refusal
     * of the whole column. */
    {
        const char *nm[] = { "v" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++)
            if (i == 42) tb_cellz(&b, "9223372036854775808"); else tb_cellf(&b, "%d", i);
        run_case("over_limit_rare", &b);
    }
    /* Front-coding, per group. A monotonic text column shares almost every
     * character with its predecessor -- one real timestamp column went 9,438
     * bytes to 434. The reconstruction is a prefix length plus a remainder,
     * so anything that makes the prefix ambiguous shows up here. */
    {
        const char *nm[] = { "t" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 3000; i++)
            tb_cellf(&b, "2015-01-%02dT%02d:%02d:00", 1 + i / 1440, (i / 60) % 24, i % 60);
        run_case("monotonic_text", &b);
    }
    /* Prefix longer than 255 bytes, which is where the one-byte cap bites. */
    {
        const char *nm[] = { "t" };
        tb_start(&b, 1, nm);
        char z[401];
        memset(z, 'Z', 400);
        z[400] = 0;
        for (int i = 0; i < 500; i++) tb_cellf(&b, "%s%04d", z, i);
        run_case("long_shared_prefix", &b);
    }
    /* A non-ASCII shared prefix: the prefix is counted in BYTES. */
    {
        const char *nm[] = { "t" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 600; i++)
            tb_cellf(&b, "\xc3\xa9\xc3\xbc\xe4\xb8\xad\xe6\x96\x87-%05d", i);
        run_case("unicode_prefix", &b);
    }
    /* Front-coding must be refused on a group containing a newline, because
     * the remainders are newline-joined and could not be split back apart. */
    {
        const char *nm[] = { "t" };
        tb_start(&b, 1, nm);
        for (int i = 0; i < 400; i++) tb_cellf(&b, "pre-%04d\nsuffix", i);
        run_case("newline_in_sorted_text", &b);
    }
    {
        const char *v[] = { "", "a", "a", "ab", "", "b" };
        const char *nm[] = { "t" };
        tb_start(&b, 1, nm);
        for (int k = 0; k < 80; k++) for (int i = 0; i < 6; i++) tb_cellz(&b, v[i]);
        run_case("degenerate_text", &b);
    }
    {
        const char *nm[] = { "a", "b" };
        tb_start(&b, 2, nm);
        for (int i = 0; i < 2000; i++) {
            tb_cellf(&b, "2020-01-01T%02d:%02d", i / 60, i % 60);
            tb_cellf(&b, "id-%07d-tail", i * 3);
        }
        run_case("two_monotonic", &b);
    }
    (void)tmp;
}

/* ------------------------------------------------------------- one pass */

/* ppz_encode is one pass: it writes the modelled container for every table
 * with UTF-8 names -- noise included, where plain xz would be smaller -- and
 * the canonical CSV's frozen quirks still decide what the OLD plain
 * containers could carry. */
static void cases_one_pass(void)
{
    h_section("one pass: always the modelled container");
    Rng r = { 4 };
    const char al[] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    TB b;
    char why[256];

    tb_start(&b, 2, (const char *const[]){ "a", "b" });
    for (int i = 0; i < 3000; i++)
        for (int j = 0; j < 2; j++) {
            char s[25];
            for (int k = 0; k < 24; k++) s[k] = al[rng_below(&r, sizeof(al) - 1)];
            tb_cell(&b, s, 24);
        }
    Table noise;
    tb_finish(&b, &noise);
    Buf got;
    buf_init(&got);
    CHECK(ppz_encode(&noise, &got) == 0 && is_magic(&got, PPZ_MAGIC),
          "noise: one pass must still write PPZ1");
    check_table("noise", &noise, 1);
    table_free(&noise);

    /* a bare '\r' is NOT quoted by the canonical writer (a frozen rule), so
     * the old plain containers could never carry this table */
    tb_start(&b, 2, (const char *const[]){ "t", "u" });
    tb_row(&b, "has,comma", "has\"quote", NULL);
    tb_row(&b, "has\nnewline", "has\ttab", NULL);
    tb_row(&b, "", "   ", NULL);
    tb_row(&b, "\xc3\xa9\xc3\xbc\xe4\xb8\xad\xe6\x96\x87", "trailing ", NULL);
    tb_row(&b, "\r", "a\r\nb", NULL);
    Table nasty;
    tb_finish(&b, &nasty);
    Buf canon;
    buf_init(&canon);
    table_write_canonical(&nasty, &canon);
    Table ct;
    int same = table_parse_csv(&ct, canon.data, canon.len) == 0
               && table_same(&nasty, &ct, why, sizeof(why));
    if (ct.cells) table_free(&ct);
    CHECK(!same, "nasty: the canonical CSV carried a bare \\r -- the frozen rule says it cannot");
    check_table("nasty", &nasty, 1);
    buf_free(&canon);
    table_free(&nasty);

    tb_start(&b, 2, (const char *const[]){ "zip", "city" });
    const char *zips[] = { "98101", "98402", "98501" };
    const char *cities[] = { "Seattle", "Tacoma", "Olympia" };
    for (int i = 0; i < 2000; i++) { tb_cellz(&b, zips[i % 3]); tb_cellz(&b, cities[i % 3]); }
    Table st;
    tb_finish(&b, &st);
    long fired = 0;
    Buf mod;
    buf_init(&mod);
    CHECK(ppz_encode_modelled(&st, &mod, &fired) == 0 && fired > 0,
          "structured table fired no tricks");
    check_table("structured", &st, 1);
    table_free(&st);
    buf_free(&mod); buf_free(&got);
}

/* The canonical CSV is frozen: every PPZX/PPZB archive in existence is built
 * on its exact bytes. Three quirks, pinned here as bytes (they were taken
 * from Python's csv.writer when this was a port, and test_cbin.py pinned them
 * against Python; now the rule itself is pinned). */
static void cases_canonical(void)
{
    h_section("canonical CSV bytes (frozen format)");
    struct { const char *name; size_t ncols; const char *names[3];
             const char *cells[6]; size_t ncells; size_t lens[6];
             const char *want; size_t wantn; } cs[] = {
        /* an empty field IS quoted when it is the only field in its row */
        { "lone empty field", 1, { "a" }, { "" }, 1, { 0 }, "a\n\"\"\n", 5 },
        /* ...and not when it has company */
        { "empty with company", 2, { "a", "b" }, { "", "" }, 2, { 0, 0 }, "a,b\n,\n", 6 },
        /* a bare '\r' is NOT quoted */
        { "bare CR", 1, { "a" }, { "x\ry" }, 1, { 3 }, "a\nx\ry\n", 6 },
        /* a NUL byte forces quoting */
        { "NUL", 1, { "a" }, { "n\0l" }, 1, { 3 }, "a\n\"n\0l\"\n", 8 },
        /* quotes doubled, commas and newlines quoted */
        { "quote comma newline", 3, { "q", "c", "n" }, { "a\"b", "a,b", "a\nb" }, 3,
          { 3, 3, 3 }, "q,c,n\n\"a\"\"b\",\"a,b\",\"a\nb\"\n", 0 },
        /* a header-only name that is empty, alone -> quoted too */
        { "empty lone name", 1, { "" }, { "x" }, 1, { 1 }, "\"\"\nx\n", 5 },
    };
    for (size_t k = 0; k < N_OF(cs); k++) {
        TB b;
        tb_start(&b, cs[k].ncols, cs[k].names);
        for (size_t i = 0; i < cs[k].ncells; i++) tb_cell(&b, cs[k].cells[i], cs[k].lens[i]);
        Table t;
        tb_finish(&b, &t);
        Buf out;
        buf_init(&out);
        table_write_canonical(&t, &out);
        char s1[128], s2[128];
        if (!cs[k].wantn) cs[k].wantn = strlen(cs[k].want);
        CHECK(out.len == cs[k].wantn && !memcmp(out.data, cs[k].want, out.len),
              "canonical %s: got %s want %s", cs[k].name,
              h_show((const char *)out.data, out.len, s1, sizeof(s1)),
              h_show(cs[k].want, cs[k].wantn, s2, sizeof(s2)));
        buf_free(&out);
        table_free(&t);
    }
}

/* ------------------------------------------------------ shapes the port adds */

static void cases_more(void)
{
    h_section("more shapes: degenerate, bytes, widths, parents, big values");
    TB b;

    /* no columns at all */
    tb_start(&b, 0, NULL);
    run_case("empty_table", &b);

    /* header only, one column, and one whose name is empty */
    tb_start(&b, 1, (const char *const[]){ "a" });
    run_case("header_only_one_col", &b);
    tb_start(&b, 1, (const char *const[]){ "" });
    run_case("header_only_empty_name", &b);
    tb_start(&b, 1, (const char *const[]){ "" });
    tb_cellz(&b, ""); tb_cellz(&b, "");
    run_case("one_empty_column_named_empty", &b);

    /* a column that is nothing but empty cells, next to one that is not */
    tb_start(&b, 2, (const char *const[]){ "blank", "n" });
    for (int i = 0; i < 300; i++) { tb_cellz(&b, ""); tb_cellf(&b, "%d", i); }
    run_case("only_empty_column", &b);

    /* NUL, CR, CRLF, lone quotes, tabs: every byte a CSV writer worries about,
     * in a table big enough that the modelled container wins */
    tb_start(&b, 3, (const char *const[]){ "bytes", "k", "n" });
    const char *odd[] = { "\r", "\r\n", "\n", "\"", ",", "\t", "a\"b,c\nd", "", " " };
    for (int i = 0; i < 400; i++) {
        if (i % 7 == 3) tb_cell(&b, "nu\0l", 4);
        else tb_cellz(&b, odd[i % N_OF(odd)]);
        tb_cellf(&b, "k%d", i % 5);
        tb_cellf(&b, "%d", i);
    }
    run_case("control_bytes", &b);

    /* Cells are bytes, not text: invalid UTF-8 in a cell is still data. The
     * readers refuse such a FILE, but a Table can hold anything and the codec
     * must not care. */
    tb_start(&b, 2, (const char *const[]){ "raw", "k" });
    const char *raw[] = { "\xff", "\xc0\x80", "\xed\xa0\x80", "\xf4\x90\x80\x80", "\x80", "ok" };
    for (int i = 0; i < 300; i++) { tb_cellz(&b, raw[i % 6]); tb_cellf(&b, "%d", i % 3); }
    run_case("invalid_utf8_cells", &b);

    /* Column names go through the JSON metadata, escaped: non-BMP characters
     * become surrogate pairs, control characters \u escapes. */
    tb_start(&b, 7, (const char *const[]){ "\xc3\xa9", "\xf0\x9f\x8e\x89", "a\tb",
                                           "q\"uote", "back\\slash", "ctl\x01", "new\nline" });
    for (int i = 0; i < 200; i++)
        for (int j = 0; j < 7; j++) tb_cellf(&b, "%d", (i * (j + 1)) % 17);
    run_case("names_escaped", &b);

    /* dictionary width boundaries: 256 ids fit one byte, 257 need two */
    for (int u = 256; u <= 257; u++) {
        tb_start(&b, 2, (const char *const[]){ "k", "v" });
        for (int i = 0; i < 4 * u; i++) { tb_cellf(&b, "key-%d", (i * 7) % u); tb_cellf(&b, "%d", i % 3); }
        char nm[32];
        snprintf(nm, sizeof(nm), "dict_%d_distinct", u);
        run_case(nm, &b);
    }

    /* A dictionary alphabet with a newline in it: the group is stored with
     * explicit lengths instead of newline-joined. */
    tb_start(&b, 2, (const char *const[]){ "k", "n" });
    for (int i = 0; i < 300; i++) { tb_cellf(&b, "line %d\nnext", i % 6); tb_cellf(&b, "%d", i); }
    run_case("dict_value_with_newline", &b);

    /* Front-coded dictionary alphabets: sorted, long shared prefixes */
    tb_start(&b, 2, (const char *const[]){ "cat", "n" });
    for (int i = 0; i < 2000; i++) {
        tb_cellf(&b, "category/subcategory/department-%03d", (i * 37) % 300);
        tb_cellf(&b, "%d", i % 11);
    }
    run_case("front_coded_alphabet", &b);

    /* A text column with a reorder parent: too many distinct values for a
     * dictionary, but determined in large part by a dictionary column. */
    tb_start(&b, 3, (const char *const[]){ "zip", "addr", "id" });
    for (int i = 0; i < 1200; i++) {
        int z = (i * 13) % 25;
        tb_cellf(&b, "%05d", 60600 + z);
        tb_cellf(&b, "%d N STREET-%02d AVENUE, CITY-%02d", i, z, z);
        tb_cellf(&b, "%d", i);
    }
    run_case("text_with_parent", &b);

    /* ...and the same with a newline in the text, so the reordered group is
     * stored with lengths */
    tb_start(&b, 2, (const char *const[]){ "zip", "addr" });
    for (int i = 0; i < 800; i++) {
        int z = (i * 13) % 20;
        tb_cellf(&b, "%05d", 60600 + z);
        tb_cellf(&b, "%d N STREET-%02d\nCITY-%02d", i, z, z);
    }
    run_case("text_parent_with_newline", &b);

    /* Exceptions whose text has a newline: the exception strings are a group
     * of their own and take the explicit-lengths layout. Once in an ungrouped
     * column and once inside a 2D group. */
    tb_start(&b, 1, (const char *const[]){ "v" });
    for (int i = 0; i < 400; i++)
        if (i == 17) tb_cellz(&b, "n/a\nsee note"); else tb_cellf(&b, "%.2f", 10 + i * 0.25);
    run_case("exception_with_newline", &b);
    tb_start(&b, 3, (const char *const[]){ "a", "b", "c" });
    for (int i = 0; i < 300; i++)
        for (int j = 0; j < 3; j++)
            if (i == 150 && j == 2) tb_cellz(&b, "x\ny");
            else if (i == 20 && j == 0) tb_cellz(&b, "-0.00");
            else tb_cellf(&b, "%.2f", 5.0 + i * 0.02 + j * 0.1);
    run_case("grouped_exception_with_newline", &b);

    /* Long text: one 200 KB cell, and a column of 5 KB cells */
    {
        tb_start(&b, 2, (const char *const[]){ "big", "n" });
        size_t n = 200000;
        char *s = malloc(n);
        for (size_t i = 0; i < n; i++) s[i] = (char)('a' + (i * 7 + i / 13) % 26);
        s[1000] = '\n'; s[5000] = '"'; s[9000] = ',';
        tb_cell(&b, s, n); tb_cellz(&b, "1");
        tb_cellz(&b, "short"); tb_cellz(&b, "2");
        run_case("one_huge_cell", &b);
        tb_start(&b, 1, (const char *const[]){ "t" });
        for (int i = 0; i < 60; i++) tb_cell(&b, s + i * 97, 5000);
        run_case("long_text_column", &b);
        free(s);
    }

    /* Big magnitudes in a commensurable group: the double differences of
     * values near +-2^62 do not fit in int64. The format relies on two's
     * complement wrap-around to invert them exactly. */
    tb_start(&b, 3, (const char *const[]){ "a", "b", "c" });
    for (int i = 0; i < 60; i++)
        for (int j = 0; j < 3; j++)
            tb_cellz(&b, ((i + j) % 2) ? "4611686018427387903" : "-4611686018427387903");
    run_case("big_2d_group", &b);

    /* ...and in a finite-difference column (k-th differences overflow too) */
    tb_start(&b, 1, (const char *const[]){ "v" });
    for (int i = 0; i < 200; i++)
        tb_cellf(&b, "%lld", (long long)(4611686018427387903LL - (long long)i * i * 1000003LL));
    run_case("big_diff_column", &b);
    tb_start(&b, 1, (const char *const[]){ "v" });
    for (int i = 0; i < 200; i++)
        tb_cellz(&b, (i % 3 == 0) ? "4611686018427387903" : (i % 3 == 1) ? "-4611686018427387903" : "0");
    run_case("big_alternating_column", &b);

    /* numeric column of exactly representable decimals at 18 places */
    tb_start(&b, 1, (const char *const[]){ "v" });
    for (int i = 0; i < 100; i++) tb_cellf(&b, "0.%018d", i * 12345);
    run_case("eighteen_decimals", &b);
    tb_start(&b, 1, (const char *const[]){ "v" });
    for (int i = 0; i < 100; i++) tb_cellf(&b, "0.%019d", i * 12345);
    run_case("nineteen_decimals", &b);

    /* rows = 1, 2, 3, 8: the thresholds for 2D groups (3) and parents (8) */
    int small[] = { 1, 2, 3, 7, 8, 9 };
    for (size_t k = 0; k < N_OF(small); k++) {
        tb_start(&b, 4, (const char *const[]){ "a", "b", "c", "k" });
        for (int i = 0; i < small[k]; i++) {
            for (int j = 0; j < 3; j++) tb_cellf(&b, "%.1f", 1.0 + i + j * 0.5);
            tb_cellf(&b, "k%d", i % 2);
        }
        char nm[32];
        snprintf(nm, sizeof(nm), "threshold_rows_%d", small[k]);
        run_case(nm, &b);
    }
}

/* Column names are bytes too, in principle: a Table built in memory can hold
 * a name that is not UTF-8. Every file reader validates UTF-8 first, so the
 * command line never produces one -- this is the API's contract only. */
static void case_invalid_utf8_names(void)
{
    h_section("column names that are not UTF-8 (API only)");
    TB b;
    tb_start(&b, 2, (const char *const[]){ "bad\xff", "k" });
    for (int i = 0; i < 400; i++) { tb_cellf(&b, "%d", i * 3); tb_cellf(&b, "k%d", i % 4); }
    run_case("invalid_utf8_name", &b);
}

/* 65,536 distinct values is the largest dictionary; one more is text. Both
 * tables are ~130k rows, so the thread check is skipped to keep the run short
 * (the smaller tables cover it). */
static void cases_dict_limit(void)
{
    h_section("the 65,536-value dictionary limit");
    g_threads_checks = 0;
    for (int u = 65536; u <= 65537; u++) {
        TB b;
        tb_start(&b, 1, (const char *const[]){ "v" });
        for (int i = 0; i < 2 * u; i++) tb_cellf(&b, "v%x", (unsigned)((i * 40503u) % (unsigned)u));
        char nm[48];
        snprintf(nm, sizeof(nm), "distinct_%d", u);
        run_case(nm, &b);
    }
    g_threads_checks = 1;
}

/* ----------------------------------------------------------------- V0 magic */

/* Archives written before the rename to PPZ1 carry "FAST" and still open. */
static void case_v0_magic(void)
{
    h_section("FAST (pre-rename) archives still open");
    TB b;
    tb_start(&b, 2, (const char *const[]){ "zip", "city" });
    for (int i = 0; i < 300; i++) { tb_cellf(&b, "%05d", 90000 + i % 9); tb_cellf(&b, "c%d", i % 9); }
    Table t;
    tb_finish(&b, &t);
    Buf blob;
    buf_init(&blob);
    long fired;
    CHECK(ppz_encode_modelled(&t, &blob, &fired) == 0, "encode");
    memcpy(blob.data, PPZ_MAGIC_V0, 4);
    Table back;
    char why[256];
    int rc = ppz_decode(blob.data, blob.len, &back);
    if (CHECK(rc == 0, "a FAST archive is refused")) {
        CHECK(table_same(&t, &back, why, sizeof(why)), "FAST archive differs: %s", why);
        table_free(&back);
    }
    buf_free(&blob);
    table_free(&t);
}

/* ------------------------------------------------------ capped compressors */

/* The capped forms are streamed so another thread can stop them early.
 * Stopping may change only the time taken, never the answer: when a capped
 * run does NOT stop, its bytes must be exactly the one-shot library call's.
 * The reference here is liblzma's and libbzip2's own buffer-to-buffer API,
 * not the program's wrappers. Inputs cross the 1 MB chunk the capped forms
 * feed at, because a chunk boundary is where a streamed encoder can differ. */
static void oneshot_xz(const uint8_t *in, size_t n, Buf *out)
{
    lzma_options_lzma opt;
    lzma_lzma_preset(&opt, 9 | LZMA_PRESET_EXTREME);
    lzma_filter f[2] = { { LZMA_FILTER_LZMA2, &opt }, { LZMA_VLI_UNKNOWN, NULL } };
    buf_free(out);
    size_t cap = n + n / 2 + 65536;
    buf_need(out, cap);
    size_t pos = 0;
    if (lzma_raw_buffer_encode(f, NULL, in, n, out->data, &pos, cap) != LZMA_OK) pos = 0;
    out->len = pos;
}

static void oneshot_bz(const uint8_t *in, size_t n, Buf *out)
{
    buf_free(out);
    unsigned int cap = (unsigned int)(n + n / 100 + 1024);
    buf_need(out, cap);
    if (BZ2_bzBuffToBuffCompress((char *)out->data, &cap, (char *)(uintptr_t)in,
                                 (unsigned int)n, 9, 0, 0) != BZ_OK) cap = 0;
    out->len = cap;
}

static void make_input(Rng *r, size_t n, Buf *out)
{
    /* table-ish text: compressible, but not trivially */
    buf_init(out);
    buf_need(out, n + 128);         /* never NULL, even for n = 0 */
    while (out->len < n) {
        char line[96];
        int k = snprintf(line, sizeof(line), "%llu,%s,%.3f,row %llu\n",
                         (unsigned long long)rng_below(r, 100000),
                         (const char *[]){ "alpha", "beta", "gamma", "delta" }[rng_below(r, 4)],
                         rng_unit(r) * 1000, (unsigned long long)(out->len / 7));
        buf_put(out, line, (size_t)k);
    }
    out->len = n;
}

static void cases_capped(void)
{
    h_section("the compressor equals the one-shot one; bzip2 still decodes");
    Rng r = { 77 };
    size_t sizes[] = { 0, 1, 1000, ((size_t)1 << 20) - 1, (size_t)1 << 20,
                       ((size_t)1 << 20) + 1, (size_t)5 * 1000 * 1000 + 17 };
    for (size_t k = 0; k < N_OF(sizes); k++) {
        size_t n = sizes[k];
        Buf in, ref, got, back;
        make_input(&r, n, &in);
        buf_init(&ref); buf_init(&got); buf_init(&back);
        /* streamed a megabyte at a time, it must still be the one-shot bytes */
        oneshot_xz(in.data, n, &ref);
        int rc = ppz_lzma_compress(in.data, n, &got);
        CHECK(rc == 0 && got.len == ref.len && !memcmp(got.data, ref.data, ref.len),
              "ppz_lzma_compress on %zu B differs from one-shot", n);
        rc = ppz_lzma_decompress(got.data, got.len, &back);
        CHECK(rc == 0 && back.len == n && (!n || !memcmp(back.data, in.data, n)),
              "xz round trip on %zu B", n);
        oneshot_bz(in.data, n, &ref);
        rc = ppz_bz2_decompress(ref.data, ref.len, &back);
        CHECK(rc == 0 && back.len == n && (!n || !memcmp(back.data, in.data, n)),
              "bz2 decode on %zu B", n);
        buf_free(&in); buf_free(&ref); buf_free(&got); buf_free(&back);
    }
}

/* ------------------------------------------------------------------- fuzzer */

/* test_fuzz.py's generators, and a few that aim at machinery the Python ones
 * reached only by luck: commensurable runs (2D groups), derived columns
 * (parents), monotonic text (front-coding), rare exceptions (the lenient
 * numeric path), and raw bytes. Its record: in one run it found a JSON parser
 * storing int64 warm-start values as doubles (74884171959489212 came back as
 * ...216), an `a <= 8*b` that overflowed and refused every group of large
 * columns, and an overflow check placed after the multiply. */
enum { M_INT, M_DEC, M_SMALLINT, M_CAT, M_TEXT, M_NASTY, M_BLANK, M_BIG,
       M_GRP, M_DERIVED, M_MONO, M_LAX, M_BYTES, M_NMODES };

static void gen_cell(Rng *r, int mode, size_t row, size_t col, TB *b, Str prev)
{
    char s[128];
    switch (mode) {
    case M_INT: {
        int64_t lim = 1;
        int e = (int)rng_range(r, 1, 18);
        for (int i = 0; i < e; i++) lim *= 10;
        int64_t lo = 1;
        e = (int)rng_range(r, 1, 18);
        for (int i = 0; i < e; i++) lo *= 10;
        tb_cellf(b, "%lld", (long long)rng_range(r, -lo, lim));
        return;
    }
    case M_DEC:
        tb_cellf(b, "%.*f", (int)rng_range(r, 0, 9), (rng_unit(r) * 2 - 1) * 1e6);
        return;
    case M_SMALLINT: tb_cellf(b, "%d", (int)rng_below(r, 6)); return;
    case M_CAT: {
        const char *c[] = { "a", "bb", "ccc", "", " ", "ZZ" };
        tb_cellz(b, c[rng_below(r, 6)]);
        return;
    }
    case M_TEXT: {
        /* Python's string.printable: digits, letters, punctuation, whitespace */
        static const char pr[] = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                 "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ \t\n\r\x0b\x0c";
        size_t n = (size_t)rng_below(r, 21);
        for (size_t i = 0; i < n; i++) s[i] = pr[rng_below(r, sizeof(pr) - 1)];
        tb_cell(b, s, n);
        return;
    }
    case M_NASTY: {
        const char *c[] = { "", " ", "\n", "\r\n", "\"", ",", "\t", "a\"b,c\nd",
                            "\xc3\xa9", "\xe4\xb8\xad", "\xf0\x9f\x8e\x89", "x", "007", "-0.0",
                            "1.", ".5", "+3", "NaN", "inf", "9223372036854775808",
                            "-9223372036854775808", "000000000000000000000000000000", "\r" };
        size_t k = (size_t)rng_below(r, N_OF(c) + 1);
        if (k == N_OF(c)) tb_cell(b, "\0", 1);
        else tb_cellz(b, c[k]);
        return;
    }
    case M_BLANK: tb_cellz(b, ""); return;
    case M_BIG: tb_cellf(b, "%lld", (long long)(rng_next(r) >> 2)); return;
    case M_GRP:
        /* smooth in both directions, same decimals: a planar-predictor table */
        tb_cellf(b, "%.2f", 100.0 + row * 0.37 + col * 1.5 + (rng_below(r, 3) - 1) * 0.01);
        return;
    case M_DERIVED: {
        /* determined by the previous column: a parent candidate */
        uint64_t h = 1469598103934665603ULL;
        for (size_t i = 0; i < prev.n; i++) { h ^= (uint8_t)prev.p[i]; h *= 1099511628211ULL; }
        tb_cellf(b, "d%llu", (unsigned long long)(h % 7));
        return;
    }
    case M_MONO:
        tb_cellf(b, "2024-%02zu-%02zuT%02zu:%02zu", 1 + row / 2000 % 12, 1 + row / 60 % 28,
                 row / 7 % 24, row % 60);
        return;
    case M_LAX:
        if (rng_below(r, 40) == 0) {
            const char *c[] = { "", "-0.0", "n/a", "1.2345", "x\ny" };
            tb_cellz(b, c[rng_below(r, 5)]);
        } else tb_cellf(b, "%.1f", row * 0.5 + (double)rng_below(r, 3));
        return;
    case M_BYTES: {
        size_t n = (size_t)rng_below(r, 9);
        for (size_t i = 0; i < n; i++) s[i] = (char)rng_below(r, 256);
        tb_cell(b, s, n);
        return;
    }
    }
    tb_cellz(b, "x");
}

static void gen_table(Rng *r, Table *t)
{
    size_t nc = (size_t)rng_range(r, 1, 8);
    const size_t rows[] = { 0, 1, 2, 3, 5, 8, 9, 40, 200, 200, 1000 };
    size_t nr = rows[rng_below(r, N_OF(rows))];
    int modes[8];
    for (size_t j = 0; j < nc; j++) modes[j] = (int)rng_below(r, M_NMODES);
    /* commensurable runs only matter as runs: extend one sometimes */
    if (nc >= 3 && rng_below(r, 3) == 0) {
        size_t at = (size_t)rng_below(r, nc - 2);
        for (size_t j = at; j < at + 3; j++) modes[j] = M_GRP;
    }
    if (modes[0] == M_DERIVED) modes[0] = M_CAT;

    const char *odd[] = { "\xc3\xa9", "a b", "a,b", "a\"b", "", "x\ty", "\xf0\x9f\x8e\x89", "dup" };
    char *names[8];
    for (size_t j = 0; j < nc; j++) {
        char nm[32];
        if (rng_unit(r) < 0.15) snprintf(nm, sizeof(nm), "%s", odd[rng_below(r, N_OF(odd))]);
        else snprintf(nm, sizeof(nm), "c%zu", j);
        names[j] = strdup(nm);
    }
    TB b;
    tb_start(&b, nc, (const char *const *)names);
    for (size_t j = 0; j < nc; j++) free(names[j]);
    for (size_t i = 0; i < nr; i++)
        for (size_t j = 0; j < nc; j++) {
            Str prev = { "", 0 };
            if (j) {
                size_t k = b.n - 1;
                prev.p = (const char *)b.arena.data + b.off[k];
                prev.n = b.len[k];
            }
            gen_cell(r, modes[j], i, j, &b, prev);
        }
    tb_finish(&b, t);
}

static void fuzz(int count, uint64_t seed)
{
    char title[96];
    snprintf(title, sizeof(title), "fuzz: %d random tables, seed %llu", count,
             (unsigned long long)seed);
    h_section(title);
    Rng r = { seed };
    int bad = 0;
    double t0 = h_now();
    for (int i = 0; i < count; i++) {
        Table t;
        gen_table(&r, &t);
        char nm[64];
        snprintf(nm, sizeof(nm), "fuzz#%d (%zux%zu, seed %llu)", i, t.nrows, t.ncols,
                 (unsigned long long)seed);
        if (!check_table(nm, &t, i % 4 == 0)) bad++;
        table_free(&t);
    }
    printf("   %d tables in %.1f s, %d with a failure\n", count, h_now() - t0, bad);
}

int main(int argc, char **argv)
{
    h_suite = "codec";
    int count = argc > 1 ? atoi(argv[1]) : 1000;
    uint64_t seed = argc > 2 ? strtoull(argv[2], NULL, 10) : 1;
    setvbuf(stdout, NULL, _IOLBF, 0);

    cases_canonical();
    cases_dtz();
    cases_fast();
    cases_one_pass();
    cases_more();
    case_invalid_utf8_names();
    case_v0_magic();
    cases_dict_limit();
    cases_capped();
    fuzz(count, seed);
    return h_done();
}
