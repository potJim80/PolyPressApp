/* The readers and writers (ppz_io.c): what table a file IS.
 *
 * This is the one place data can be lost before any check can see it. Twice a
 * reader damaged the table first and the round-trip verification, comparing
 * the damaged table with its own decoding, passed:
 *
 *   errors="replace"   every undecodable byte became U+FFFD before the table
 *                      existed; a latin-1 file lost every accent and
 *                      "verified" (test_encoding.py measured six files: three
 *                      destroyed data and said nothing).
 *   row[:width]        rows wider than the header were silently cut; Romeo
 *                      and Juliet read as raw text lost the tail of every line
 *                      with a comma in it.
 *
 * So the rules pinned here are mostly refusals: strict UTF-8 unless a BOM or
 * --encoding says otherwise, and no row trimmed unless what is trimmed is
 * empty. Every expected table below is written out by hand -- never produced
 * by the reader under test.
 */

#include "harness.h"

#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

/* ---------------------------------------------------------------- helpers */

/* A table from C strings: names[ncols], then cells row-major. */
static void mk(Table *t, size_t ncols, const char *const *names,
               const char *const *cells, size_t ncells)
{
    TB b;
    tb_start(&b, ncols, names);
    for (size_t i = 0; i < ncells; i++) tb_cellz(&b, cells[i]);
    tb_finish(&b, t);
}

#define NAMES(...) ((const char *const[]){ __VA_ARGS__ })
#define CELLS(...) ((const char *const[]){ __VA_ARGS__ }), \
                   (sizeof((const char *const[]){ __VA_ARGS__ }) / sizeof(const char *))
#define NOCELLS    ((const char *const[]){ "" }), 0

/* Write `bytes` to the temp dir as `fname`, read it, and compare with `want`.
 * Frees `want`. */
static int expect_read(const char *label, const char *fname, const void *bytes,
                       size_t n, const char *enc, Table *want)
{
    const char *p = tpath("%s", fname);
    write_bytes(p, bytes, n);
    Table got;
    char err[1024] = "", why[512];
    int ok = 0;
    int rc = table_read_any(&got, p, enc, err, sizeof(err));
    if (CHECK(rc == 0, "%s: refused: %s", label, err)) {
        ok = CHECK(table_same(want, &got, why, sizeof(why)), "%s: %s", label, why);
        table_free(&got);
    }
    table_free(want);
    return ok;
}

static int expect_reads(const char *label, const char *fname, const char *text,
                        const char *enc, Table *want)
{
    return expect_read(label, fname, text, strlen(text), enc, want);
}

/* The file must be refused, and the message must contain each of `needles`
 * (NULL-terminated). */
static int expect_refused(const char *label, const char *fname, const void *bytes,
                          size_t n, const char *enc, ...)
{
    const char *p = tpath("%s", fname);
    write_bytes(p, bytes, n);
    Table got;
    char err[1024] = "";
    int rc = table_read_any(&got, p, enc, err, sizeof(err));
    if (!CHECK(rc != 0, "%s: accepted, must be refused", label)) {
        table_free(&got);
        return 0;
    }
    int ok = CHECK(err[0] != 0, "%s: refused with no message", label);
    va_list ap;
    va_start(ap, enc);
    const char *needle;
    while ((needle = va_arg(ap, const char *)) != NULL)
        ok &= CHECK(strstr(err, needle) != NULL, "%s: message lacks \"%s\": %s", label, needle, err);
    va_end(ap);
    return ok;
}

/* ---------------------------------------------------------------- CSV dialect */

static void csv_dialect(void)
{
    h_section("CSV dialect");
    Table w;

    mk(&w, 2, NAMES("a", "b"), CELLS("x,1", "say \"hi\"", "", "\"\""));
    expect_reads("quotes and \"\" escapes", "q.csv",
                 "a,b\n\"x,1\",\"say \"\"hi\"\"\"\n\"\",\"\"\"\"\"\"\n", NULL, &w);

    /* CR, LF and CRLF all end a row outside quotes */
    const char *endings[][2] = { { "LF", "a,b\n1,2\n3,4\n" }, { "CRLF", "a,b\r\n1,2\r\n3,4\r\n" },
                                 { "CR", "a,b\r1,2\r3,4\r" }, { "mixed", "a,b\r\n1,2\r3,4\n" } };
    for (size_t k = 0; k < N_OF(endings); k++) {
        mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "3", "4"));
        char label[64];
        snprintf(label, sizeof(label), "%s line endings", endings[k][0]);
        expect_reads(label, "le.csv", endings[k][1], NULL, &w);
    }

    /* ...and are kept inside them */
    mk(&w, 2, NAMES("t", "n"), CELLS("a\nb", "1", "c\r\nd", "2", "e\rf", "3"));
    expect_reads("newlines inside quotes", "nl.csv",
                 "t,n\n\"a\nb\",1\n\"c\r\nd\",2\n\"e\rf\",3\n", NULL, &w);

    mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "3", "4"));
    expect_reads("last line without a terminator", "nt.csv", "a,b\n1,2\n3,4", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "3", "x\ny"));
    expect_reads("last line ends inside a quoted cell", "ntq.csv", "a,b\n1,2\n3,\"x\ny\"", NULL, &w);

    /* A blank line is a row of empty cells (padded like any short row). */
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "", "", "3", "4"));
    expect_reads("blank line in the middle", "bl.csv", "a,b\n1,2\n\n3,4\n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "", ""));
    expect_reads("blank line at the end", "ble.csv", "a,b\n1,2\n\n", NULL, &w);
    mk(&w, 1, NAMES("a"), CELLS("", "x", ""));
    expect_reads("blank lines in one column", "bl1.csv", "a\n\nx\n\n", NULL, &w);

    /* Short rows are padded: CSV writers drop trailing empties all the time,
     * and nothing is lost. */
    mk(&w, 3, NAMES("a", "b", "c"), CELLS("1", "", "", "1", "2", "", "1", "2", "3"));
    expect_reads("short rows padded", "short.csv", "a,b,c\n1\n1,2\n1,2,3\n", NULL, &w);

    /* A row wider than the header is trimmed only when every extra cell is
     * empty. The Python reader used to cut `row[:width]` silently; that bug is
     * fixed and this pins it. */
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "2", "3", "4"));
    expect_reads("wider row, empty extras trimmed", "wide0.csv", "a,b\n1,2,,\n3,4,\"\"\n", NULL, &w);
    {
        const char *s = "a,b\n1,2\n3,4,5\n";
        expect_refused("wider row with data refused", "wide1.csv", s, strlen(s), NULL,
                       "row 3", "cell 3", "not drop data", NULL);
        const char *r = "speaker,line\nROMEO,But soft, what light\n";
        expect_refused("Romeo and Juliet as raw text", "romeo.csv", r, strlen(r), NULL,
                       "row 2", NULL);
    }

    /* quote rules: a quote opens a field only at its start */
    mk(&w, 2, NAMES("a", "b"), CELLS("x\"y", "z", "abcd", "e", "  \"q\"", "f"));
    expect_reads("quote inside an unquoted field is literal", "qm.csv",
                 "a,b\nx\"y,z\n\"ab\"cd,e\n  \"q\",f\n", NULL, &w);

    /* whitespace is data */
    mk(&w, 2, NAMES(" a ", "b "), CELLS(" 1 ", "\t2\t"));
    expect_reads("whitespace kept", "ws.csv", " a ,b \n 1 ,\t2\t\n", NULL, &w);

    /* duplicate names are kept */
    mk(&w, 3, NAMES("x", "x", "y"), CELLS("1", "2", "3"));
    expect_reads("duplicate header names", "dup.csv", "x,x,y\n1,2,3\n", NULL, &w);

    /* degenerate files */
    mk(&w, 2, NAMES("a", "b"), NOCELLS);
    expect_reads("header only", "ho.csv", "a,b\n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), NOCELLS);
    expect_reads("header only, no newline", "ho2.csv", "a,b", NULL, &w);
    mk(&w, 0, NULL, NOCELLS);
    expect_reads("empty file", "empty.csv", "", NULL, &w);
    mk(&w, 1, NAMES(""), NOCELLS);
    expect_reads("a lone newline: one unnamed column", "nl1.csv", "\n", NULL, &w);

    /* A NUL byte inside a cell is accepted and kept. (Python's csv module
     * refused such files; the C reader never did, and CLAUDE.md records the
     * divergence as a format decision. With Python gone, this is the rule.) */
    {
        const char s[] = "a,b\nn\0l,2\n";
        TB b;
        tb_start(&b, 2, NAMES("a", "b"));
        tb_cell(&b, "n\0l", 3);
        tb_cellz(&b, "2");
        tb_finish(&b, &w);
        expect_read("NUL in a cell", "nul.csv", s, sizeof(s) - 1, NULL, &w);
    }
}

/* ------------------------------------------------------------- delimiters */

static void delimiters(void)
{
    h_section("delimiter by extension, and sniffed for anything else");
    struct { const char *path, *fmt; } F[] = {
        { "x.csv", "csv" }, { "x.CSV", "csv" }, { "x.tsv", "tsv" }, { "x.tab", "tsv" },
        { "x.psv", "psv" }, { "x.json", "json" }, { "x.jsonl", "jsonl" },
        { "x.ndjson", "jsonl" }, { "x.parquet", "parquet" }, { "x.pq", "parquet" },
        { "x.txt", "text" }, { "x.dat", "text" }, { "noext", "text" }, { "-", "csv" },
        { "dir.v2/x", "text" }, { ".csv", "text" }, { "a.b.TSV", "tsv" },
    };
    for (size_t k = 0; k < N_OF(F); k++)
        CHECK(!strcmp(ppz_format_of(F[k].path), F[k].fmt), "ppz_format_of(%s) = %s, want %s",
              F[k].path, ppz_format_of(F[k].path), F[k].fmt);

    Table w;
    mk(&w, 2, NAMES("a", "b"), CELLS("1,5", "x"));
    expect_reads("TSV by extension", "d.tsv", "a\tb\n1,5\tx\n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), CELLS("1,5", "x"));
    expect_reads(".tab is TSV", "d.tab", "a\tb\n1,5\tx\n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), CELLS("1,5", "x"));
    expect_reads("PSV by extension", "d.psv", "a|b\n1,5|x\n", NULL, &w);
    mk(&w, 1, NAMES("a\tb"), CELLS("1\t2"));
    expect_reads(".csv means comma even when the file has tabs", "t.csv", "a\tb\n1\t2\n", NULL, &w);

    /* sniffed from the header, outside quotes */
    const char *sn[][3] = {
        { "comma", "a,b\n1,2\n", "," }, { "tab", "a\tb\n1\t2\n", "\t" },
        { "semicolon", "a;b\n1;2\n", ";" }, { "pipe", "a|b\n1|2\n", "|" },
    };
    for (size_t k = 0; k < N_OF(sn); k++) {
        mk(&w, 2, NAMES("a", "b"), CELLS("1", "2"));
        char label[64];
        snprintf(label, sizeof(label), ".txt sniffed as %s", sn[k][0]);
        expect_reads(label, "s.txt", sn[k][1], NULL, &w);
        const char *p = tpath("s.txt");
        char err[256];
        CsvIn *c = csv_open(p, NULL, 0, err, sizeof(err));
        if (CHECK(c != NULL, "csv_open %s", label)) {
            CHECK(csv_delim(c) == sn[k][2][0], "%s: csv_delim says 0x%02x", label, csv_delim(c));
            csv_close(c);
        }
    }
    /* the most frequent candidate wins */
    mk(&w, 3, NAMES("a,x", "b", "c"), CELLS("1,2", "3", "4"));
    expect_reads("tab outnumbers comma", "m.txt", "a,x\tb\tc\n1,2\t3\t4\n", NULL, &w);
    /* a delimiter inside quotes in the header does not count */
    mk(&w, 2, NAMES("a;b;c", "d"), CELLS("1", "2"));
    expect_reads("quoted header delimiters ignored", "qh.dat", "\"a;b;c\",d\n1,2\n", NULL, &w);
    /* only the header is read: a tab in a data value cannot win */
    mk(&w, 2, NAMES("a", "b"), CELLS("x\ty\tz", "1"));
    expect_reads("data tabs do not sway the sniff", "hd.txt", "a,b\nx\ty\tz,1\n", NULL, &w);
    /* no candidate at all: one column */
    mk(&w, 1, NAMES("name"), CELLS("alpha", "beta"));
    expect_reads("no delimiter: one column", "one", "name\nalpha\nbeta\n", NULL, &w);
    /* an explicit delimiter overrides everything */
    {
        const char *p = tpath("o.csv");
        write_str(p, "a;b\n1;2\n");
        char err[256];
        CsvIn *c = csv_open(p, NULL, ';', err, sizeof(err));
        Table t;
        if (CHECK(c && csv_next(c, &t, 0) == 1, "csv_open with delim ';'")) {
            CHECK(t.ncols == 2 && t.nrows == 1, "explicit ';' on a .csv: %zu cols", t.ncols);
            table_free(&t);
        }
        csv_close(c);
    }
}

/* ------------------------------------------------ blocks and big records */

/* One file that exercises every boundary the streaming reader has: CRLF
 * split exactly across the 1 MB read chunk, a 3-byte and a 4-byte UTF-8
 * character split across the next two, a record crossing a chunk, and one
 * quoted cell of several MB full of "" and newlines. */
static void build_big(Buf *file, Table *want)
{
    const size_t MB = (size_t)1 << 20;
    TB b;
    tb_start(&b, 3, NAMES("id", "text", "n"));
    buf_init(file);
    buf_put(file, "id,text,n\r\n", 11);
    Rng r = { 5 };
    size_t row = 0;
    int placed_crlf = 0, placed3 = 0, placed4 = 0, placed_big = 0;
    while (file->len < 7 * MB + 1000) {
        char id[32], nn[32];
        int il = snprintf(id, sizeof(id), "%zu", row);
        int nl = snprintf(nn, sizeof(nn), "%llu", (unsigned long long)rng_below(&r, 1000000));
        char text[400];
        size_t tl;
        size_t need;
        /* the row ends "\r\n" with the '\r' as the last byte of chunk 1 */
        if (!placed_crlf && (need = MB + 1 - file->len) < 300 && need > 40) {
            tl = need - (size_t)il - (size_t)nl - 4;
            memset(text, 'c', tl);
            placed_crlf = 1;
        } else if (!placed3 && (need = 2 * MB - file->len) < 300 && need > 40) {
            /* "€" = E2 82 AC with the chunk boundary after its first byte */
            tl = need - (size_t)il - 1;              /* id , text[..E2] */
            memset(text, 'e', tl);
            memcpy(text + tl - 1, "\xe2\x82\xac", 3);
            tl += 2;
            placed3 = 1;
        } else if (!placed4 && (need = 3 * MB - file->len) < 300 && need > 40) {
            /* U+1F389 = F0 9F 8E 89, boundary after two bytes */
            tl = need - (size_t)il - 1;
            memset(text, 'f', tl);
            memcpy(text + tl - 2, "\xf0\x9f\x8e\x89", 4);
            tl += 2;
            placed4 = 1;
        } else if (!placed_big && file->len > 4 * MB) {
            /* a single quoted cell of ~2.5 MB, crossing several chunks */
            Buf cell, q;
            buf_init(&cell); buf_init(&q);
            while (cell.len < (size_t)2500000) {
                const char *piece = (cell.len % 3) ? "lorem \"ipsum\", dolor\r\n" : "sit amet\n";
                buf_put(&cell, piece, strlen(piece));
            }
            buf_putc(&q, '"');
            for (size_t i = 0; i < cell.len; i++) {
                if (cell.data[i] == '"') buf_putc(&q, '"');
                buf_putc(&q, (char)cell.data[i]);
            }
            buf_putc(&q, '"');
            buf_put(file, id, (size_t)il); buf_putc(file, ',');
            buf_put(file, q.data, q.len); buf_putc(file, ',');
            buf_put(file, nn, (size_t)nl); buf_put(file, "\r\n", 2);
            tb_cell(&b, id, (size_t)il); tb_cell(&b, cell.data, cell.len); tb_cell(&b, nn, (size_t)nl);
            buf_free(&cell); buf_free(&q);
            placed_big = 1;
            row++;
            continue;
        } else {
            tl = (size_t)rng_below(&r, 60);
            for (size_t i = 0; i < tl; i++) text[i] = (char)('a' + rng_below(&r, 26));
        }
        buf_put(file, id, (size_t)il); buf_putc(file, ',');
        buf_put(file, text, tl); buf_putc(file, ',');
        buf_put(file, nn, (size_t)nl); buf_put(file, "\r\n", 2);
        tb_cell(&b, id, (size_t)il); tb_cell(&b, text, tl); tb_cell(&b, nn, (size_t)nl);
        row++;
    }
    /* sanity of the construction itself */
    CHECK(placed_crlf && file->data[MB - 1] == '\r' && file->data[MB] == '\n',
          "construction: CRLF not split at the 1 MB chunk");
    CHECK(placed3 && file->data[2 * MB - 1] == 0xE2, "construction: 3-byte char not split");
    CHECK(placed4 && file->data[3 * MB - 2] == 0xF0, "construction: 4-byte char not split");
    CHECK(placed_big, "construction: no big cell");
    tb_finish(&b, want);
}

static void blocks(void)
{
    h_section("blocks: csv_next(max_rows) equals the whole-table read");
    Buf file;
    Table want;
    build_big(&file, &want);
    const char *p = tpath("big.csv");
    write_bytes(p, file.data, file.len);
    char err[1024] = "", why[512];

    Table whole;
    int rc = table_read_any(&whole, p, NULL, err, sizeof(err));
    if (CHECK(rc == 0, "big file refused: %s", err)) {
        CHECK(table_same(&want, &whole, why, sizeof(why)), "big file whole read: %s", why);
        table_free(&whole);
    }

    size_t sizes[] = { 1, 2, 3, 7, 1000, 100000, 0 };
    for (size_t k = 0; k < N_OF(sizes); k++) {
        CsvIn *c = csv_open(p, NULL, 0, err, sizeof(err));
        if (!CHECK(c != NULL, "csv_open: %s", err)) continue;
        size_t row = 0, nblocks = 0;
        int bad = 0;
        Table t;
        int r;
        while ((r = csv_next(c, &t, sizes[k])) == 1) {
            nblocks++;
            if (t.ncols != want.ncols || (sizes[k] && t.nrows > sizes[k])) bad = 1;
            for (size_t j = 0; j < t.ncols && !bad; j++)
                if (strcmp(t.names[j], want.names[j])) bad = 2;
            for (size_t i = 0; i < t.nrows && !bad; i++, row++)
                for (size_t j = 0; j < t.ncols; j++) {
                    Str x = table_at(&t, i, j), y = table_at(&want, row, j);
                    if (x.n != y.n || memcmp(x.p, y.p, x.n)) { bad = 3; break; }
                }
            table_free(&t);
            if (bad) break;
        }
        CHECK(r == 0 && !bad && row == want.nrows,
              "blocks of %zu: r=%d bad=%d, %zu of %zu rows (%s)", sizes[k], r, bad, row,
              want.nrows, csv_error(c));
        size_t expect = sizes[k] ? (want.nrows + sizes[k] - 1) / sizes[k] : 1;
        CHECK(nblocks == expect, "blocks of %zu: %zu blocks, want %zu (no trailing empty block)",
              sizes[k], nblocks, expect);
        CHECK(csv_width(c) == 3, "csv_width = %zu", csv_width(c));
        csv_close(c);
    }
    buf_free(&file);
    table_free(&want);

    /* the block count is exact when the rows divide evenly */
    {
        Buf f;
        buf_init(&f);
        buf_put(&f, "a\n", 2);
        for (int i = 0; i < 12; i++) { char s[8]; int n = snprintf(s, sizeof(s), "%d\n", i); buf_put(&f, s, (size_t)n); }
        const char *q = tpath("even.csv");
        write_bytes(q, f.data, f.len);
        buf_free(&f);
        size_t per[] = { 1, 3, 4, 6, 12, 13 };
        for (size_t k = 0; k < N_OF(per); k++) {
            CsvIn *c = csv_open(q, NULL, 0, err, sizeof(err));
            Table t;
            size_t n = 0, rows = 0;
            while (csv_next(c, &t, per[k]) == 1) { n++; rows += t.nrows; table_free(&t); }
            CHECK(n == (12 + per[k] - 1) / per[k] && rows == 12,
                  "12 rows in blocks of %zu: %zu blocks", per[k], n);
            csv_close(c);
        }
    }
    /* A header-only file yields one empty block, so it still has columns; an
     * empty file yields one block with nothing. Then 0. */
    {
        const char *q = tpath("hdr.csv");
        write_str(q, "a,b\n");
        CsvIn *c = csv_open(q, NULL, 0, err, sizeof(err));
        Table t;
        int r1 = csv_next(c, &t, 5);
        CHECK(r1 == 1 && t.ncols == 2 && t.nrows == 0, "header-only: first block r=%d", r1);
        if (r1 == 1) table_free(&t);
        CHECK(csv_next(c, &t, 5) == 0, "header-only: a second block");
        csv_close(c);
        q = tpath("zero.csv");
        write_str(q, "");
        c = csv_open(q, NULL, 0, err, sizeof(err));
        r1 = csv_next(c, &t, 5);
        CHECK(r1 == 1 && t.ncols == 0 && t.nrows == 0, "empty file: first block r=%d", r1);
        if (r1 == 1) table_free(&t);
        CHECK(csv_next(c, &t, 5) == 0, "empty file: a second block");
        csv_close(c);
    }
    /* the refusal of a wide row arrives in whichever block holds it */
    {
        Buf f;
        buf_init(&f);
        buf_put(&f, "a,b\n", 4);
        for (int i = 0; i < 5000; i++) { char s[16]; int n = snprintf(s, sizeof(s), "%d,x\n", i); buf_put(&f, s, (size_t)n); }
        buf_put(&f, "9,y,z\n", 6);
        const char *q = tpath("late.csv");
        write_bytes(q, f.data, f.len);
        buf_free(&f);
        CsvIn *c = csv_open(q, NULL, 0, err, sizeof(err));
        Table t;
        int r, n = 0;
        while ((r = csv_next(c, &t, 1000)) == 1) { n++; table_free(&t); }
        CHECK(r == -1 && n == 5 && strstr(csv_error(c), "row 5002"),
              "late wide row: r=%d after %d blocks: %s", r, n, csv_error(c));
        csv_close(c);
    }
}

/* ---------------------------------------------------------------- encodings */

/* Accented text is the whole point: exactly what a non-UTF-8 encoding gets
 * wrong, and exactly what errors="replace" used to throw away. */
static const char *ENC_TEXT =
    "city,note\nZ\xc3\xbcrich,caf\xc3\xa9\nMalm\xc3\xb6,na\xc3\xafve\nM\xc3\xbcnchen,r\xc3\xa9sum\xc3\xa9\n";

static void enc_want(Table *w)
{
    mk(w, 2, NAMES("city", "note"),
       CELLS("Z\xc3\xbcrich", "caf\xc3\xa9", "Malm\xc3\xb6", "na\xc3\xafve",
             "M\xc3\xbcnchen", "r\xc3\xa9sum\xc3\xa9"));
}

/* UTF-8 to code points, and code points out in the encodings under test --
 * written here so the expected bytes do not come from iconv, which the reader
 * itself uses. */
static size_t utf8_decode(const uint8_t *s, size_t n, uint32_t *out)
{
    size_t k = 0;
    for (size_t i = 0; i < n; ) {
        uint8_t c = s[i];
        uint32_t cp;
        int len;
        if (c < 0x80) { cp = c; len = 1; }
        else if (c < 0xE0) { cp = c & 0x1F; len = 2; }
        else if (c < 0xF0) { cp = c & 0x0F; len = 3; }
        else { cp = c & 0x07; len = 4; }
        for (int j = 1; j < len; j++) cp = (cp << 6) | (s[i + j] & 0x3F);
        out[k++] = cp;
        i += (size_t)len;
    }
    return k;
}

static void put16(Buf *b, uint16_t u, int be)
{
    uint8_t x[2] = { (uint8_t)(be ? u >> 8 : u), (uint8_t)(be ? u : u >> 8) };
    buf_put(b, x, 2);
}

static void to_utf16(const char *s, size_t n, int be, int bom, Buf *out)
{
    uint32_t *cp = malloc((n + 1) * sizeof(uint32_t));
    size_t k = utf8_decode((const uint8_t *)s, n, cp);
    buf_init(out);
    if (bom) put16(out, 0xFEFF, be);
    for (size_t i = 0; i < k; i++) {
        if (cp[i] >= 0x10000) {
            uint32_t v = cp[i] - 0x10000;
            put16(out, (uint16_t)(0xD800 + (v >> 10)), be);
            put16(out, (uint16_t)(0xDC00 + (v & 0x3FF)), be);
        } else put16(out, (uint16_t)cp[i], be);
    }
    free(cp);
}

static void to_utf32(const char *s, size_t n, int be, Buf *out)
{
    uint32_t *cp = malloc((n + 2) * sizeof(uint32_t));
    size_t k = utf8_decode((const uint8_t *)s, n, cp + 1);
    cp[0] = 0xFEFF;
    buf_init(out);
    for (size_t i = 0; i <= k; i++) {
        uint32_t v = cp[i];
        uint8_t x[4];
        for (int j = 0; j < 4; j++) x[j] = (uint8_t)(v >> (be ? 24 - 8 * j : 8 * j));
        buf_put(out, x, 4);
    }
    free(cp);
}

/* One byte per code point below 0x100; cp1252 maps a few above it. */
static void to_8bit(const char *s, int cp1252, Buf *out)
{
    size_t n = strlen(s);
    uint32_t *cp = malloc((n + 1) * sizeof(uint32_t));
    size_t k = utf8_decode((const uint8_t *)s, n, cp);
    buf_init(out);
    for (size_t i = 0; i < k; i++) {
        uint32_t v = cp[i];
        if (cp1252 && v == 0x20AC) v = 0x80;
        else if (cp1252 && v == 0x201C) v = 0x93;
        else if (cp1252 && v == 0x201D) v = 0x94;
        buf_putc(out, (char)(uint8_t)v);
    }
    free(cp);
}

static void encodings(void)
{
    h_section("encodings: read it right, or refuse -- never guess");
    Table w;
    size_t tn = strlen(ENC_TEXT);
    Buf b;

    enc_want(&w);
    expect_reads("plain UTF-8", "u8.csv", ENC_TEXT, NULL, &w);

    /* Excel's "CSV UTF-8" writes a BOM, which used to end up INSIDE the first
     * column's name: 'city' became '\ufeffcity'. */
    buf_init(&b);
    buf_put(&b, "\xef\xbb\xbf", 3);
    buf_put(&b, ENC_TEXT, tn);
    enc_want(&w);
    expect_read("UTF-8 with BOM", "u8bom.csv", b.data, b.len, NULL, &w);
    enc_want(&w);
    expect_read("UTF-8 with BOM, --encoding utf-8", "u8bom2.csv", b.data, b.len, "utf-8", &w);
    buf_free(&b);

    /* Excel's "Unicode text" export, and the rest of the BOM family */
    for (int be = 0; be <= 1; be++) {
        to_utf16(ENC_TEXT, tn, be, 1, &b);
        enc_want(&w);
        expect_read(be ? "UTF-16BE with BOM" : "UTF-16LE with BOM", "u16.csv", b.data, b.len, NULL, &w);
        buf_free(&b);
        to_utf32(ENC_TEXT, tn, be, &b);
        enc_want(&w);
        expect_read(be ? "UTF-32BE with BOM" : "UTF-32LE with BOM", "u32.csv", b.data, b.len, NULL, &w);
        buf_free(&b);
    }
    /* --encoding utf-16 with no mark: little-endian */
    to_utf16(ENC_TEXT, tn, 0, 0, &b);
    enc_want(&w);
    expect_read("UTF-16 without a BOM, named", "u16n.csv", b.data, b.len, "utf-16", &w);
    buf_free(&b);

    /* latin-1 and cp1252 are REFUSED unnamed, with the byte and its offset:
     * "city,note\nZ" is 11 bytes, so the first bad byte is the u-umlaut 0xFC
     * at offset 11. */
    to_8bit(ENC_TEXT, 0, &b);
    expect_refused("latin-1 unnamed", "l1.csv", b.data, b.len, NULL,
                   "0xFC", "offset 11", "--encoding", NULL);
    enc_want(&w);
    expect_read("latin-1 named", "l1.csv", b.data, b.len, "latin-1", &w);
    const char *aliases[] = { "latin1", "LATIN_1", "iso-8859-1", "ISO8859-1", "l1" };
    for (size_t k = 0; k < N_OF(aliases); k++) {
        enc_want(&w);
        char label[64];
        snprintf(label, sizeof(label), "latin-1 named as %s", aliases[k]);
        expect_read(label, "l1.csv", b.data, b.len, aliases[k], &w);
    }
    /* named wrongly: strict UTF-8 still refuses */
    expect_refused("latin-1 named utf-8", "l1.csv", b.data, b.len, "utf-8", "0xFC", NULL);
    buf_free(&b);

    const char *cp_text = "q,price\n\xe2\x80\x9cquoted\xe2\x80\x9d,\xe2\x82\xac" "5\n";
    to_8bit(cp_text, 1, &b);
    expect_refused("cp1252 unnamed", "cp.csv", b.data, b.len, NULL, "0x93", "--encoding", NULL);
    mk(&w, 2, NAMES("q", "price"), CELLS("\xe2\x80\x9cquoted\xe2\x80\x9d", "\xe2\x82\xac" "5"));
    expect_read("cp1252 named", "cp.csv", b.data, b.len, "cp1252", &w);
    mk(&w, 2, NAMES("q", "price"), CELLS("\xe2\x80\x9cquoted\xe2\x80\x9d", "\xe2\x82\xac" "5"));
    expect_read("cp1252 named windows-1252", "cp.csv", b.data, b.len, "windows-1252", &w);
    buf_free(&b);

    expect_refused("unknown encoding", "u8.csv", ENC_TEXT, tn, "klingon", "unknown encoding", NULL);

    /* one illegal byte in otherwise valid UTF-8 */
    {
        buf_init(&b);
        size_t at = tn / 2;
        buf_put(&b, ENC_TEXT, at);
        buf_putc(&b, (char)0xFF);
        buf_put(&b, ENC_TEXT + at, tn - at);
        char off[32];
        snprintf(off, sizeof(off), "offset %zu", at);
        expect_refused("UTF-8 with one illegal byte", "stray.csv", b.data, b.len, NULL,
                       "0xFF", off, "--encoding", NULL);
        buf_free(&b);
    }
    /* strict the way Python's decoder is: no overlongs, no surrogates,
     * nothing past U+10FFFF, no stray continuation, no truncated tail */
    struct { const char *label; const char *bytes; size_t n; } bad[] = {
        { "overlong 2-byte", "a\nx\xc0\xafy\n", 6 },
        { "overlong 3-byte", "a\nx\xe0\x80\xafy\n", 7 },
        { "overlong 4-byte", "a\nx\xf0\x80\x80\xafy\n", 8 },
        { "UTF-16 surrogate", "a\nx\xed\xa0\x80y\n", 7 },
        { "past U+10FFFF", "a\nx\xf4\x90\x80\x80y\n", 8 },
        { "F5 lead byte", "a\nx\xf5\x80\x80\x80y\n", 8 },
        { "lone continuation", "a\nx\x80y\n", 5 },
        { "truncated at the end", "a\nx\xe2\x82", 5 },
        { "truncated mid-file", "a\nx\xe2\x82y\n", 6 },
    };
    for (size_t k = 0; k < N_OF(bad); k++)
        expect_refused(bad[k].label, "bad.csv", bad[k].bytes, bad[k].n, NULL, "offset 3", NULL);
    /* and the valid neighbours of those are accepted */
    mk(&w, 1, NAMES("a"), CELLS("\xf4\x8f\xbf\xbf", "\xed\x9f\xbf", "\xe0\xa0\x80", "\xc2\x80"));
    expect_reads("edges of valid UTF-8", "edge.csv",
                 "a\n\xf4\x8f\xbf\xbf\n\xed\x9f\xbf\n\xe0\xa0\x80\n\xc2\x80\n", NULL, &w);

    /* UTF-16 that is not: an unpaired surrogate, and an odd byte at the end */
    {
        Buf u;
        to_utf16("a\nxy\n", 5, 0, 1, &u);
        Buf lone;
        buf_init(&lone);
        buf_put(&lone, u.data, 6);                    /* BOM a \n */
        put16(&lone, 0xD800, 0);                      /* high surrogate, no low */
        buf_put(&lone, u.data + 6, u.len - 6);
        expect_refused("UTF-16 unpaired surrogate", "lone16.csv", lone.data, lone.len, NULL,
                       "UTF-16", NULL);
        buf_free(&lone);
        buf_putc(&u, 'x');
        expect_refused("UTF-16 odd length", "odd16.csv", u.data, u.len, NULL, "UTF-16", NULL);
        buf_free(&u);
    }

    /* A big UTF-16 file with non-BMP characters, one surrogate pair split
     * across the 1 MB read chunk -- iconv sees half a character there. */
    {
        Buf txt;
        buf_init(&txt);
        TB tb;
        tb_start(&tb, 2, NAMES("k", "v"));
        buf_put(&txt, "k,v\n", 4);
        size_t row = 0;
        /* after a 2-byte BOM, UTF-16 byte = 2 + 2 * (UTF-16 units so far) */
        size_t units = 4;
        int split = 0;
        while (units * 2 + 2 < (size_t)3 << 20) {
            char k[32];
            int kl = snprintf(k, sizeof(k), "%zu", row++);
            char v[64];
            size_t vl;
            size_t boundary_unit = ((size_t)1 << 20) / 2 - 2;   /* bytes 1 MB-2 .. 1 MB+1 */
            size_t need = boundary_unit - units;
            if (!split && need < 60 && need > (size_t)kl + 2) {
                /* k , filler... then the emoji starting at boundary_unit */
                size_t fill = need - (size_t)kl - 1;
                memset(v, 'u', fill);
                memcpy(v + fill, "\xf0\x9f\x98\x80", 4);
                vl = fill + 4;
                split = 1;
            } else {
                vl = 3 + row % 20;
                for (size_t i = 0; i < vl; i++) v[i] = (char)('a' + (row + i) % 26);
                if (row % 5 == 0) { memcpy(v, "\xf0\x9f\x8e\x89", 4); if (vl < 4) vl = 4; }
            }
            buf_put(&txt, k, (size_t)kl); buf_putc(&txt, ',');
            buf_put(&txt, v, vl); buf_putc(&txt, '\n');
            tb_cell(&tb, k, (size_t)kl); tb_cell(&tb, v, vl);
            uint32_t tmp[128];
            units += (size_t)kl + 2;
            size_t nc = utf8_decode((const uint8_t *)v, vl, tmp);
            for (size_t i = 0; i < nc; i++) units += tmp[i] >= 0x10000 ? 2 : 1;
        }
        Buf u;
        to_utf16((const char *)txt.data, txt.len, 0, 1, &u);
        CHECK(split && u.data[((size_t)1 << 20) - 2] == 0x3D && u.data[((size_t)1 << 20) - 1] == 0xD8,
              "construction: the surrogate pair is not split at 1 MB");
        Table big;
        tb_finish(&tb, &big);
        expect_read("big UTF-16 with a pair split across the read chunk", "big16.csv",
                    u.data, u.len, NULL, &big);
        buf_free(&u);
        buf_free(&txt);
    }

    /* JSON and JSON Lines get the same treatment */
    {
        const char *recs = "[{\"city\": \"Z\xc3\xbcrich\", \"note\": \"caf\xc3\xa9\"}, "
                           "{\"city\": \"Malm\xc3\xb6\", \"note\": \"na\xc3\xafve\"}, "
                           "{\"city\": \"M\xc3\xbcnchen\", \"note\": \"r\xc3\xa9sum\xc3\xa9\"}]";
        buf_init(&b);
        buf_put(&b, "\xef\xbb\xbf", 3);
        buf_put(&b, recs, strlen(recs));
        enc_want(&w);
        expect_read("JSON with a BOM", "bom.json", b.data, b.len, NULL, &w);
        buf_free(&b);
        const char *lines = "{\"city\": \"Z\xc3\xbcrich\", \"note\": \"caf\xc3\xa9\"}\n"
                            "{\"city\": \"Malm\xc3\xb6\", \"note\": \"na\xc3\xafve\"}\n"
                            "{\"city\": \"M\xc3\xbcnchen\", \"note\": \"r\xc3\xa9sum\xc3\xa9\"}";
        buf_init(&b);
        buf_put(&b, "\xef\xbb\xbf", 3);
        buf_put(&b, lines, strlen(lines));
        enc_want(&w);
        expect_read("JSON Lines with a BOM", "bom.jsonl", b.data, b.len, NULL, &w);
        buf_free(&b);
        to_8bit(recs, 0, &b);
        expect_refused("JSON in latin-1", "l1.json", b.data, b.len, NULL, "0xFC", "--encoding", NULL);
        enc_want(&w);
        expect_read("JSON in latin-1, named", "l1.json", b.data, b.len, "latin-1", &w);
        buf_free(&b);
    }

    /* and what is read must survive the codec, not just the reader */
    {
        to_8bit(ENC_TEXT, 0, &b);
        const char *p = tpath("l1c.csv");
        write_bytes(p, b.data, b.len);
        buf_free(&b);
        Table t, back;
        char err[512], why[256];
        if (CHECK(table_read_any(&t, p, "latin-1", err, sizeof(err)) == 0, "latin-1: %s", err)) {
            Buf z;
            buf_init(&z);
            CHECK(ppz_encode(&t, &z) == 0 && ppz_decode(z.data, z.len, &back) == 0,
                  "latin-1 table through the codec");
            enc_want(&w);
            CHECK(table_same(&w, &back, why, sizeof(why)), "latin-1 through the codec: %s", why);
            table_free(&w); table_free(&back); table_free(&t); buf_free(&z);
        }
    }
}

/* -------------------------------------------------------------------- JSON */

/* JSON parsing in a child: a hostile file must not take the reader down. */
static const char *g_child_path;
static int child_read(void *arg)
{
    (void)arg;
    Table t;
    char err[256];
    return table_read_any(&t, g_child_path, NULL, err, sizeof(err)) == 0 ? 0 : 1;
}

static void json(void)
{
    h_section("JSON and JSON Lines");
    Table w;

    mk(&w, 2, NAMES("a", "b"), CELLS("1", "x", "2", "y"));
    expect_reads("records", "r.json", "[{\"a\": 1, \"b\": \"x\"}, {\"a\": 2, \"b\": \"y\"}]\n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "x", "2", "y"));
    expect_reads("column form", "c.json", "{\"a\": [1, 2], \"b\": [\"x\", \"y\"]}", NULL, &w);

    /* columns are the union of keys in first-seen order; a missing key is an
     * empty cell */
    mk(&w, 3, NAMES("a", "b", "c"), CELLS("1", "", "", "", "2", "3", "4", "5", ""));
    expect_reads("missing keys", "m.json",
                 "[{\"a\": 1}, {\"b\": 2, \"c\": 3}, {\"b\": 5, \"a\": 4}]", NULL, &w);

    /* values: numbers as written, null empty, booleans, nesting compact */
    mk(&w, 1, NAMES("v"), CELLS("1.50", "-0", "1e5", "1E+05", "-2.5e-3",
                                "123456789012345678901234567890", "74884171959489212",
                                "", "true", "false", "{\"x\":[1,2.0,\"\xc3\xa9\"],\"y\":null}",
                                "[]", "{}", "[true,false,null]"));
    expect_reads("values", "v.json",
                 "[{\"v\": 1.50}, {\"v\": -0}, {\"v\": 1e5}, {\"v\": 1E+05}, {\"v\": -2.5e-3},"
                 " {\"v\": 123456789012345678901234567890}, {\"v\": 74884171959489212},"
                 " {\"v\": null}, {\"v\": true}, {\"v\": false},"
                 " {\"v\": {\"x\": [1, 2.0, \"\\u00e9\"], \"y\": null}},"
                 " {\"v\": []}, {\"v\": {}}, {\"v\": [true, false, null]}]", NULL, &w);

    /* string escapes, \u0000 included: a NUL is a real byte of the cell */
    {
        TB b;
        tb_start(&b, 1, NAMES("s"));
        tb_cell(&b, "a\0b", 3);
        tb_cellz(&b, "q\"b\\s/n\nt\tr\rbf\b\f");
        tb_cellz(&b, "\xc3\xa9\xf0\x9f\x8e\x89");
        tb_cellz(&b, "\xc3\xa9\xf0\x9f\x8e\x89");
        tb_finish(&b, &w);
        expect_reads("string escapes", "e.json",
                     "[{\"s\": \"a\\u0000b\"}, {\"s\": \"q\\\"b\\\\s\\/n\\nt\\tr\\rbf\\b\\f\"},"
                     " {\"s\": \"\\u00e9\\ud83c\\udf89\"}, {\"s\": \"\xc3\xa9\xf0\x9f\x8e\x89\"}]",
                     NULL, &w);
    }
    /* keys are escaped strings too */
    mk(&w, 2, NAMES("a b", "\xc3\xa9"), CELLS("1", "2"));
    expect_reads("escaped keys", "k.json", "[{\"a b\": 1, \"\\u00e9\": 2}]", NULL, &w);

    /* a key repeated in one record keeps its last value */
    mk(&w, 1, NAMES("a"), CELLS("2"));
    expect_reads("repeated key", "rk.json", "[{\"a\": 1, \"a\": 2}]", NULL, &w);

    /* empty */
    mk(&w, 0, NULL, NOCELLS);
    expect_reads("empty list", "el.json", "[]", NULL, &w);
    mk(&w, 0, NULL, NOCELLS);
    expect_reads("empty object", "eo.json", " {} \n", NULL, &w);
    mk(&w, 2, NAMES("a", "b"), NOCELLS);
    expect_reads("column form with no rows keeps its columns", "c0.json", "{\"a\": [], \"b\": []}", NULL, &w);

    /* refusals */
    struct { const char *label, *text, *needle; } bad[] = {
        { "trailing comma", "[{\"a\": 1},]", "not valid JSON" },
        { "unclosed", "[{\"a\": 1}", "not valid JSON" },
        { "trailing garbage", "[{\"a\": 1}] x", "not valid JSON" },
        { "empty file", "", "not valid JSON" },
        { "records that are not objects", "[1, 2]", "not an object" },
        { "a single value", "\"hello\"", "single value" },
        { "column form with a non-list", "{\"a\": [1], \"b\": 2}", "not a list" },
        { "bad escape", "[{\"a\": \"\\q\"}]", "not valid JSON" },
        { "bad \\u", "[{\"a\": \"\\u12G4\"}]", "not valid JSON" },
        { "unquoted key", "[{a: 1}]", "not valid JSON" },
        { "single quotes", "[{'a': 1}]", "not valid JSON" },
        { "hex number", "[{\"a\": 0x10}]", "not valid JSON" },
        { "bare word", "[{\"a\": yes}]", "not valid JSON" },
    };
    for (size_t k = 0; k < N_OF(bad); k++)
        expect_refused(bad[k].label, "bad.json", bad[k].text, strlen(bad[k].text), NULL,
                       bad[k].needle, NULL);

    /* NaN and Infinity are not JSON. Refusing them is right; so would be
     * keeping the token as written. Turning them into an EMPTY cell, or into
     * "-", is silent data loss. */
    const char *nonjson[] = { "NaN", "Infinity", "-Infinity", "nan", "inf" };
    for (size_t k = 0; k < N_OF(nonjson); k++) {
        char text[64];
        snprintf(text, sizeof(text), "[{\"a\": %s}]", nonjson[k]);
        const char *p = tpath("nan.json");
        write_str(p, text);
        Table t;
        char err[256];
        int rc = table_read_any(&t, p, NULL, err, sizeof(err));
        int ok = rc != 0;
        char shown[64] = "";
        if (rc == 0) {
            Str s = t.nrows && t.ncols ? table_at(&t, 0, 0) : (Str){ "", 0 };
            ok = s.n == strlen(nonjson[k]) && !memcmp(s.p, nonjson[k], s.n);
            h_show(s.p, s.n, shown, sizeof(shown));
            table_free(&t);
        }
        CHECK(ok, "JSON value %s was accepted and became the cell %s", nonjson[k], shown);
    }

    /* JSON Lines: blank and whitespace-only lines skipped, CRLF fine */
    mk(&w, 2, NAMES("a", "b"), CELLS("1", "", "2", "x"));
    expect_reads("JSON Lines", "l.jsonl", "{\"a\": 1}\r\n\r\n   \n{\"a\": 2, \"b\": \"x\"}\n\n", NULL, &w);
    mk(&w, 1, NAMES("a"), CELLS("1"));
    expect_reads(".ndjson is JSON Lines", "l.ndjson", "{\"a\": 1}", NULL, &w);
    expect_refused("JSON Lines with a bad line", "bad.jsonl", "{\"a\": 1}\n{\"a\": \n{\"a\": 3}\n",
                   25, NULL, "line 2", NULL);
    expect_refused("JSON Lines with two values on a line", "two.jsonl", "{\"a\": 1} {\"a\": 2}\n",
                   18, NULL, "line 1", NULL);
    expect_refused("JSON Lines with a non-object", "arr.jsonl", "[1]\n", 4, NULL, "not an object", NULL);

    /* Nesting depth: the parser recurses, so a file of nothing but '[' is a
     * stack-depth test. It must be refused (or read) -- never crash. Run in
     * a child, since a crash is exactly what it is looking for. */
    size_t depths[] = { 1000, 100000, 3000000 };
    for (size_t k = 0; k < N_OF(depths); k++) {
        Buf d;
        buf_init(&d);
        buf_need(&d, depths[k] + 1);
        memset(d.data, '[', depths[k]);
        d.len = depths[k];
        const char *p = tpath("deep%zu.json", k);
        write_bytes(p, d.data, d.len);
        buf_free(&d);
        g_child_path = p;
        ChildResult r;
        run_in_child(child_read, NULL, 20000, (size_t)1 << 30, &r);
        char desc[128];
        CHECK(child_survived(&r) && r.code == 1, "a .json of %zu '[' : %s", depths[k],
              child_describe(&r, desc, sizeof(desc)));
    }
    /* ...and the same depth, well-formed, as the value of a cell */
    {
        size_t n = 200000;
        Buf d;
        buf_init(&d);
        buf_put(&d, "[{\"a\": ", 7);
        buf_need(&d, 2 * n + 16);
        memset(d.data + d.len, '[', n); d.len += n;
        memset(d.data + d.len, ']', n); d.len += n;
        buf_put(&d, "}]", 2);
        const char *p = tpath("deepcell.json");
        write_bytes(p, d.data, d.len);
        buf_free(&d);
        g_child_path = p;
        ChildResult r;
        run_in_child(child_read, NULL, 20000, (size_t)1 << 30, &r);
        char desc[128];
        CHECK(child_survived(&r), "a cell nested %zu deep: %s", n, child_describe(&r, desc, sizeof(desc)));
    }
}

/* ------------------------------------------------------------------ writers */

/* Tables that are awkward in every format, written by table_write_any and
 * read back by table_read_any. */
static int tricky(int k, Table *t)
{
    TB b;
    switch (k) {
    case 0:
        tb_start(&b, 3, NAMES("name", "note", "n"));
        tb_row(&b, "Smith, John", "say \"hi\"", "1", NULL);
        tb_row(&b, "a\nb", "c\r\nd", "2", NULL);
        tb_row(&b, "\r", "tab\there", "3", NULL);
        tb_row(&b, "", "", "", NULL);
        tb_row(&b, "  lead", "trail  ", "-0.0", NULL);
        tb_row(&b, "|pipe|", "\"", "\"\"", NULL);
        break;
    case 1:
        tb_start(&b, 2, NAMES("\xc3\x9c" "ber", "\xf0\x9f\x98\x80"));
        tb_row(&b, "\xe5\x8c\x97\xe4\xba\xac", "\xe2\x98\x83", NULL);
        tb_row(&b, "Krak\xc3\xb3w", "\xe2\x9c\x93", NULL);
        break;
    case 2:                     /* one column whose cells are empty: blank lines */
        tb_start(&b, 1, NAMES("only"));
        tb_row(&b, "", NULL); tb_row(&b, "x", NULL); tb_row(&b, "", NULL);
        break;
    case 3:                     /* header only */
        tb_start(&b, 3, NAMES("a", "b", "c"));
        break;
    case 4:                     /* control bytes, NUL included */
        tb_start(&b, 2, NAMES("c", "d"));
        tb_cell(&b, "n\0l", 3); tb_cellz(&b, "\x01\x1f\x7f");
        tb_cellz(&b, "back\\slash"); tb_cellz(&b, "\b\f");
        break;
    case 5:                     /* names that need quoting */
        tb_start(&b, 3, NAMES("a,b", "c\"d", "e\nf"));
        tb_row(&b, "1", "2", "3", NULL);
        break;
    case 6:                     /* an unnamed single column, one empty cell */
        tb_start(&b, 1, NAMES(""));
        tb_row(&b, "", NULL);
        break;
    case 7: {                   /* bigger than the writer's 1 MB flush */
        tb_start(&b, 2, NAMES("i", "t"));
        for (int i = 0; i < 40000; i++) { tb_cellf(&b, "%d", i); tb_cellf(&b, "row %d, \"q\"\n%d", i, i * 7); }
        break;
    }
    case 8:                     /* no columns at all */
        tb_start(&b, 0, NULL);
        break;
    default:
        return 0;
    }
    tb_finish(&b, t);
    return 1;
}

static void writers(void)
{
    h_section("writers round-trip through the readers");
    const char *ext[] = { "csv", "tsv", "psv", "json", "jsonl", "txt" };
    Table t;
    for (int k = 0; tricky(k, &t); k++) {
        for (size_t e = 0; e < N_OF(ext); e++) {
            /* a .txt is sniffed on read, so only a comma table is sure */
            if (!strcmp(ext[e], "txt") && k != 0 && k != 7) continue;
            /* JSON Lines cannot carry the column names of a table with no
             * rows (pinned below); every other format can */
            int limit = !strcmp(ext[e], "jsonl") && t.nrows == 0 && t.ncols;
            const char *p = tpath("w%d.%s", k, ext[e]);
            char err[512] = "", why[256];
            int rc = table_write_any(&t, p, err, sizeof(err));
            if (limit) {
                CHECK(rc != 0 && !file_exists(p), "tricky %d .%s: zero rows should be refused", k, ext[e]);
                continue;
            }
            if (!CHECK(rc == 0, "tricky %d .%s: write failed: %s", k, ext[e], err)) continue;
            Table back;
            rc = table_read_any(&back, p, NULL, err, sizeof(err));
            if (!CHECK(rc == 0, "tricky %d .%s: read back refused: %s", k, ext[e], err)) continue;
            CHECK(table_same(&t, &back, why, sizeof(why)), "tricky %d .%s: %s", k, ext[e], why);
            table_free(&back);
        }
        table_free(&t);
    }

    h_section("writer refusals leave nothing behind");
    char err[512];
    Table d;
    mk(&d, 3, NAMES("x", "x", "y"), CELLS("1", "2", "3"));
    const char *jx[] = { "dup.json", "dup.jsonl" };
    for (int k = 0; k < 2; k++) {
        const char *p = tpath("%s", jx[k]);
        err[0] = 0;
        int rc = table_write_any(&d, p, err, sizeof(err));
        CHECK(rc != 0 && strstr(err, "duplicate"), "%s: duplicate names accepted (%s)", jx[k], err);
        CHECK(!file_exists(p), "%s: refused but a file exists", jx[k]);
    }
    {   /* CSV and TSV hold duplicates fine */
        const char *p = tpath("dup.tsv");
        Table back;
        char why[256];
        if (CHECK(table_write_any(&d, p, err, sizeof(err)) == 0, "dup.tsv: %s", err)
            && CHECK(table_read_any(&back, p, NULL, err, sizeof(err)) == 0, "dup.tsv read")) {
            CHECK(table_same(&d, &back, why, sizeof(why)), "dup.tsv: %s", why);
            table_free(&back);
        }
    }
    table_free(&d);

    /* zero rows: jsonl refuses, json writes the column form */
    mk(&d, 2, NAMES("a", "b"), NOCELLS);
    {
        const char *p = tpath("zero.jsonl");
        err[0] = 0;
        CHECK(table_write_any(&d, p, err, sizeof(err)) != 0 && strstr(err, "no rows"),
              "zero-row .jsonl accepted (%s)", err);
        CHECK(!file_exists(p), "zero-row .jsonl: refused but a file exists");
        p = tpath("zero.json");
        Buf got;
        if (CHECK(table_write_any(&d, p, err, sizeof(err)) == 0, "zero-row .json: %s", err)
            && CHECK(read_bytes(p, &got) == 0, "read zero.json")) {
            const char *want = "{\"a\": [], \"b\": []}\n";
            CHECK(got.len == strlen(want) && !memcmp(got.data, want, got.len),
                  "zero-row .json is not the column form: %.*s", (int)got.len, got.data);
            buf_free(&got);
        }
    }
    table_free(&d);

    /* Parquet is someone else's job */
    mk(&d, 1, NAMES("a"), CELLS("1"));
    {
        const char *p = tpath("t.parquet");
        CHECK(table_write_any(&d, p, err, sizeof(err)) != 0 && !file_exists(p), "writing .parquet");
        write_str(p, "PAR1....PAR1");
        Table x;
        CHECK(table_read_any(&x, p, NULL, err, sizeof(err)) != 0 && strstr(err, "Parquet"),
              "reading .parquet: %s", err);
    }

    /* A failed write leaves no file behind: the writer writes NAME.partXXXXXX
     * and renames at the end, so neither the name nor a part file remains. */
    {
        const char *p = tpath("abandon.csv");
        Writer *wr = writer_open(p, d.names, d.ncols, err, sizeof(err));
        if (CHECK(wr != NULL, "writer_open: %s", err)) {
            CHECK(writer_rows(wr, &d) == 0, "writer_rows");
            writer_close(wr, 0, NULL, 0);
        }
        CHECK(!file_exists(p) && !dir_has_prefix("abandon.csv"),
              "an abandoned write left %s or a part file behind", p);
        const char *q = tpath("no/such/dir/x.csv");
        CHECK(table_write_any(&d, q, err, sizeof(err)) != 0 && err[0],
              "writing into a missing directory succeeded");
        /* an existing file is replaced only by a complete one */
        const char *keep = tpath("keep.csv");
        write_str(keep, "old,contents\n");
        wr = writer_open(keep, d.names, d.ncols, err, sizeof(err));
        writer_rows(wr, &d);
        writer_close(wr, 0, NULL, 0);
        Buf got;
        read_bytes(keep, &got);
        CHECK(got.len == 13 && !memcmp(got.data, "old,contents\n", 13),
              "an abandoned write damaged the existing file");
        buf_free(&got);
        CHECK(!dir_has_prefix("keep.csv.part"), "an abandoned write left a part file");
        CHECK(table_write_any(&d, keep, err, sizeof(err)) == 0 && read_bytes(keep, &got) == 0
              && got.len == 4 && !memcmp(got.data, "a\n1\n", 4), "a completed write replaced it");
        buf_free(&got);
    }
    table_free(&d);

    /* the exact bytes of the delimited writer: quote only when needed */
    {
        Table x;
        mk(&x, 3, NAMES("a", "b c", "d"), CELLS("x,y", "p\"q", "plain", "", "\r", "t\tu"));
        const char *p = tpath("exact.csv");
        Buf got;
        const char *want = "a,b c,d\n\"x,y\",\"p\"\"q\",plain\n,\"\r\",t\tu\n";
        if (CHECK(table_write_any(&x, p, err, sizeof(err)) == 0 && read_bytes(p, &got) == 0, "exact.csv")) {
            char s1[160], s2[160];
            CHECK(got.len == strlen(want) && !memcmp(got.data, want, got.len), "csv bytes %s want %s",
                  h_show((const char *)got.data, got.len, s1, sizeof(s1)), h_show(want, strlen(want), s2, sizeof(s2)));
            buf_free(&got);
        }
        p = tpath("exact.tsv");
        want = "a\tb c\td\nx,y\t\"p\"\"q\"\tplain\n\t\"\r\"\t\"t\tu\"\n";
        if (CHECK(table_write_any(&x, p, err, sizeof(err)) == 0 && read_bytes(p, &got) == 0, "exact.tsv")) {
            char s1[160], s2[160];
            CHECK(got.len == strlen(want) && !memcmp(got.data, want, got.len), "tsv bytes %s want %s",
                  h_show((const char *)got.data, got.len, s1, sizeof(s1)), h_show(want, strlen(want), s2, sizeof(s2)));
            buf_free(&got);
        }
        table_free(&x);
    }

    /* ppz_json_str: quote, backslash and control characters escaped, UTF-8 as is */
    {
        Buf j;
        buf_init(&j);
        const char in[] = "a\"b\\c\n\t\r\b\f\x01\x1f\0\xc3\xa9/";
        ppz_json_str(&j, in, sizeof(in) - 1);
        const char *want = "\"a\\\"b\\\\c\\n\\t\\r\\b\\f\\u0001\\u001f\\u0000\xc3\xa9/\"";
        char s1[160], s2[160];
        CHECK(j.len == strlen(want) && !memcmp(j.data, want, j.len), "ppz_json_str %s want %s",
              h_show((const char *)j.data, j.len, s1, sizeof(s1)), h_show(want, strlen(want), s2, sizeof(s2)));
        buf_free(&j);
    }
}

/* ------------------------------------------------------------- odds and ends */

static void misc(void)
{
    h_section("paths the reader refuses");
    char err[512];
    Table t;
    CHECK(table_read_any(&t, tpath("does-not-exist.csv"), NULL, err, sizeof(err)) != 0
          && strstr(err, "no such file"), "missing file: %s", err);
    CHECK(table_read_any(&t, h_tmpdir, NULL, err, sizeof(err)) != 0 && strstr(err, "directory"),
          "a directory: %s", err);
    CHECK(csv_open(tpath("nope.csv"), NULL, 0, err, sizeof(err)) == NULL, "csv_open on a missing file");
}

int main(void)
{
    h_suite = "io";
    setvbuf(stdout, NULL, _IOLBF, 0);
    tmpdir_make("io");
    csv_dialect();
    delimiters();
    blocks();
    encodings();
    json();
    writers();
    misc();
    tmpdir_remove();
    return h_done();
}
