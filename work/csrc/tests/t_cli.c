/* The command line, by running a polypress binary.
 *
 *     t_cli /path/to/polypress
 *
 * run.sh builds that binary from the same sources into its own temp
 * directory, so this never depends on (or rewrites) csrc/polypress.
 *
 * Most of this is test_input_guard.py, which pins one bug whose shape
 * recurs in this repo: `compress` handed a .parquet to the CSV reader, which
 * read the binary as text, found 883 "rows", verified that round trip
 * perfectly, wrote the archive, and restored a corrupt file. The check was
 * downstream of the damage and could not see it. So the refusal is at the
 * door -- magic bytes and extensions, deliberately not a content sniff -- and
 * a refusal writes nothing. TSV, PSV, JSON and JSON Lines were on that
 * refused list while the C binary could only read commas; it reads them all
 * now, so they moved to the accepted list.
 */

#include "harness.h"

#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

static const char *BIN;

/* Run polypress with up to 8 arguments; stdout and stderr land in files
 * whose contents are returned in `out` / `err` (caller frees). */
static int cli(Buf *out, Buf *err, const char *stdin_path, int nargs, ...)
{
    char *argv[12];
    argv[0] = (char *)BIN;
    va_list ap;
    va_start(ap, nargs);
    for (int i = 0; i < nargs; i++) argv[1 + i] = va_arg(ap, char *);
    va_end(ap);
    argv[1 + nargs] = NULL;
    char o[1280], e[1280];
    snprintf(o, sizeof(o), "%s", tpath("cli.stdout"));
    snprintf(e, sizeof(e), "%s", tpath("cli.stderr"));
    ChildResult r;
    run_cmd(argv, stdin_path, o, e, 60000, &r);
    Buf tmp;
    if (out) read_bytes(o, out); else { read_bytes(o, &tmp); buf_free(&tmp); }
    if (err) { read_bytes(e, err); buf_putc(err, 0); err->len--; }
    char d[128];
    if (!CHECK(child_survived(&r), "polypress %s %s: %s", nargs > 0 ? argv[1] : "",
               nargs > 1 ? argv[2] : "", child_describe(&r, d, sizeof(d))))
        return -1;
    return r.code;
}

static size_t count_lines(const Buf *b)
{
    size_t n = 0;
    for (size_t i = 0; i < b->len; i++) if (b->data[i] == '\n') n++;
    if (b->len && b->data[b->len - 1] != '\n') n++;
    return n;
}

static int same_file_table(const char *a, const char *b, const char *label)
{
    Table x, y;
    char err[512], why[256];
    if (!CHECK(table_read_any(&x, a, NULL, err, sizeof(err)) == 0, "%s: %s", label, err)) return 0;
    if (!CHECK(table_read_any(&y, b, NULL, err, sizeof(err)) == 0, "%s: %s", label, err)) {
        table_free(&x);
        return 0;
    }
    int ok = CHECK(table_same(&x, &y, why, sizeof(why)), "%s: %s", label, why);
    table_free(&x); table_free(&y);
    return ok;
}

/* ------------------------------------------------------------- happy paths */

static const char *PLAIN = "id,name,value\n1,alpha,10\n2,beta,20\n3,gamma,30\n";

static void happy(void)
{
    h_section("compress, restore, info, convert, stream-compress");
    char src[1280], arc[1280];
    snprintf(src, sizeof(src), "%s", tpath("data.csv"));
    Buf big;
    buf_init(&big);
    buf_put(&big, "zip,city,n,v\n", 13);
    for (int i = 0; i < 3000; i++) {
        char row[96];
        int n = snprintf(row, sizeof(row), "%05d,city %d,%d,%.2f\n", 90000 + i % 30, i % 30, i, 1.0 + i * 0.01);
        buf_put(&big, row, (size_t)n);
    }
    write_bytes(src, big.data, big.len);
    buf_free(&big);
    Buf out, err;

    /* compress with the default name, restore with the default name */
    snprintf(arc, sizeof(arc), "%s.ppz", src);
    int rc = cli(&out, &err, NULL, 2, "compress", src);
    CHECK(rc == 0 && file_exists(arc), "compress: exit %d: %s", rc, err.data);
    CHECK(strstr((char *)out.data, "3,000 rows x 4 cols") != NULL, "compress summary: %s", out.data);
    buf_free(&out); buf_free(&err);
    char moved[1280];
    snprintf(moved, sizeof(moved), "%s", tpath("copy.csv.ppz"));
    rename(arc, moved);
    rc = cli(&out, &err, NULL, 2, "restore", moved);
    CHECK(rc == 0 && file_exists(tpath("copy.csv")), "restore to the default name: exit %d: %s", rc, err.data);
    same_file_table(src, tpath("copy.csv"), "restore by default name");
    buf_free(&out); buf_free(&err);

    /* restore writes whatever the output extension asks for */
    const char *exts[] = { "csv", "tsv", "psv", "json", "jsonl", "txt" };
    for (size_t k = 0; k < N_OF(exts); k++) {
        char dst[1280];
        snprintf(dst, sizeof(dst), "%s", tpath("r.%s", exts[k]));
        rc = cli(NULL, &err, NULL, 4, "restore", moved, "-o", dst);
        CHECK(rc == 0, "restore -o .%s: exit %d: %s", exts[k], rc, err.data);
        char label[32];
        snprintf(label, sizeof(label), "restore .%s", exts[k]);
        same_file_table(src, dst, label);
        buf_free(&err);
    }

    /* info, text and JSON */
    rc = cli(&out, &err, NULL, 2, "info", moved);
    CHECK(rc == 0 && strstr((char *)out.data, "container   single block")
          && strstr((char *)out.data, "rows        3,000") && strstr((char *)out.data, "columns     4"),
          "info: exit %d: %s%s", rc, out.data, err.data);
    buf_free(&out); buf_free(&err);
    rc = cli(&out, &err, NULL, 3, "info", moved, "--json");
    Js *j = rc == 0 ? js_parse((const char *)out.data, out.len) : NULL;
    const Js *c = js_get(j, "container");
    CHECK(j && c && c->kind == JS_STR && !strcmp(c->str, "modelled")
          && js_int(js_get(j, "rows"), -1) == 3000 && js_int(js_get(j, "columns"), -1) == 4
          && js_get(j, "names") && js_get(j, "names")->count == 4,
          "info --json: %s", out.data);
    js_free(j);
    buf_free(&out); buf_free(&err);

    /* convert between formats, and the table does not change */
    rc = cli(NULL, &err, NULL, 3, "convert", src, tpath("conv.tsv"));
    CHECK(rc == 0, "convert csv->tsv: %s", err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 3, "convert", tpath("conv.tsv"), tpath("conv.jsonl"));
    CHECK(rc == 0, "convert tsv->jsonl: %s", err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 3, "convert", tpath("conv.jsonl"), tpath("conv.json"));
    CHECK(rc == 0, "convert jsonl->json: %s", err.data);
    buf_free(&err);
    same_file_table(src, tpath("conv.json"), "csv -> tsv -> jsonl -> json");

    /* stream-compress; restore and info recognise the result by themselves */
    char sarc[1280];
    snprintf(sarc, sizeof(sarc), "%s", tpath("stream.ppz"));
    rc = cli(&out, &err, NULL, 6, "stream-compress", src, "-o", sarc, "--rows", "700");
    CHECK(rc == 0 && strstr((char *)out.data, "3,000 rows in 5 blocks of 700"),
          "stream-compress: exit %d: %s%s", rc, out.data, err.data);
    buf_free(&out); buf_free(&err);
    rc = cli(&out, &err, NULL, 3, "info", sarc, "--json");
    j = rc == 0 ? js_parse((const char *)out.data, out.len) : NULL;
    c = js_get(j, "container");
    CHECK(j && c && c->kind == JS_STR && !strcmp(c->str, "stream") && js_int(js_get(j, "rows"), -1) == 3000
          && js_int(js_get(j, "blocks"), -1) == 5 && js_int(js_get(j, "rows_per_block"), -1) == 700,
          "info --json on a stream: %s", out.data);
    js_free(j);
    buf_free(&out); buf_free(&err);
    rc = cli(&out, &err, NULL, 2, "info", sarc);
    CHECK(rc == 0 && strstr((char *)out.data, "streamed") && strstr((char *)out.data, "5 of 700"),
          "info on a stream: %s", out.data);
    buf_free(&out); buf_free(&err);
    for (size_t k = 0; k < 5; k++) {
        char dst[1280];
        snprintf(dst, sizeof(dst), "%s", tpath("sr.%s", exts[k]));
        rc = cli(NULL, &err, NULL, 4, "restore", sarc, "-o", dst);
        CHECK(rc == 0, "restore stream -o .%s: %s", exts[k], err.data);
        char label[40];
        snprintf(label, sizeof(label), "stream restore .%s", exts[k]);
        same_file_table(src, dst, label);
        buf_free(&err);
    }
    /* stream-compress reads delimited text only */
    rc = cli(NULL, &err, NULL, 4, "stream-compress", tpath("conv.json"), "-o", tpath("x.ppz"));
    CHECK(rc == 1 && !file_exists(tpath("x.ppz")), "stream-compress of .json: exit %d", rc);
    buf_free(&err);

    /* "-" is standard input and standard output */
    rc = cli(&out, &err, NULL, 2, "compress", "-");
    CHECK(rc == 2, "compress - without -o: exit %d", rc);
    buf_free(&out); buf_free(&err);
    rc = cli(NULL, &err, src, 4, "compress", "-", "-o", tpath("stdin.ppz"));
    CHECK(rc == 0 && file_exists(tpath("stdin.ppz")), "compress - -o: exit %d: %s", rc, err.data);
    buf_free(&err);
    rc = cli(&out, &err, NULL, 4, "restore", tpath("stdin.ppz"), "-o", "-");
    if (CHECK(rc == 0, "restore -o -: exit %d: %s", rc, err.data)) {
        write_bytes(tpath("stdout.csv"), out.data, out.len);
        same_file_table(src, tpath("stdout.csv"), "compress - | restore -o -");
    }
    buf_free(&out); buf_free(&err);
    rc = cli(&out, &err, src, 4, "compress", "-", "-o", "-");
    CHECK(rc == 0 && out.len > 4 && !memcmp(out.data, "PPZ", 3), "compress - -o -: exit %d", rc);
    buf_free(&out); buf_free(&err);
    rc = cli(NULL, &err, src, 3, "convert", "-", tpath("fromstdin.json"));
    CHECK(rc == 0 && same_file_table(src, tpath("fromstdin.json"), "convert -"), "convert - out.json");
    buf_free(&err);
    rc = cli(NULL, &err, src, 4, "stream-compress", "-", "-o", tpath("stdin-stream.ppz"));
    CHECK(rc == 0, "stream-compress -: exit %d: %s", rc, err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 4, "restore", tpath("stdin-stream.ppz"), "-o", tpath("ss.csv"));
    CHECK(rc == 0 && same_file_table(src, tpath("ss.csv"), "stream-compress -"), "restore of a stdin stream");
    buf_free(&err);

    /* --encoding at the command line */
    {
        const char *l1 = "city,note\nZ\xfcrich,caf\xe9\n";
        char p[1280];
        snprintf(p, sizeof(p), "%s", tpath("latin1.csv"));
        write_str(p, l1);
        rc = cli(NULL, &err, NULL, 4, "compress", p, "-o", tpath("l1.ppz"));
        CHECK(rc == 1 && strstr((char *)err.data, "--encoding") && count_lines(&err) <= 3
              && !file_exists(tpath("l1.ppz")), "latin-1 unnamed: exit %d: %s", rc, err.data);
        buf_free(&err);
        rc = cli(NULL, &err, NULL, 6, "compress", p, "--encoding", "latin-1", "-o", tpath("l1.ppz"));
        CHECK(rc == 0, "--encoding latin-1: exit %d: %s", rc, err.data);
        buf_free(&err);
        rc = cli(&out, NULL, NULL, 4, "restore", tpath("l1.ppz"), "-o", "-");
        CHECK(rc == 0 && out.len == 24 && !memcmp(out.data, "city,note\nZ\xc3\xbcrich,caf\xc3\xa9\n", 24),
              "latin-1 restored as UTF-8");
        buf_free(&out);
    }

    /* usage errors exit 2 */
    rc = cli(NULL, NULL, NULL, 0);
    CHECK(rc == 2, "no command: exit %d", rc);
    rc = cli(NULL, NULL, NULL, 1, "frobnicate");
    CHECK(rc == 2, "unknown command: exit %d", rc);
    rc = cli(NULL, NULL, NULL, 3, "compress", src, "--bogus");
    CHECK(rc == 2, "unknown option: exit %d", rc);
    rc = cli(NULL, NULL, NULL, 2, "convert", src);
    CHECK(rc == 2, "convert with one path: exit %d", rc);
    rc = cli(&out, NULL, NULL, 1, "--help");
    CHECK(rc == 0 && strstr((char *)out.data, "polypress compress"), "--help");
    buf_free(&out);
    rc = cli(&out, NULL, NULL, 1, "--version");
    CHECK(rc == 0 && !strncmp((char *)out.data, "polypress ", 10), "--version");
    buf_free(&out);
}

/* ------------------------------------------------------------ input guard */

static void guard(void)
{
    h_section("the input guard: refuse at the door, write nothing");
    struct { const char *name; const char *body; size_t n; const char *why; } REFUSE[] = {
        { "data.parquet", "PAR1\x15\x04\x15\x00\x15\x02", 10,
          "the file that started this: binary read as text, verified, corrupted" },
        { "data.csv", "PAR1\x15\x04\x15\x00\x15\x02", 10, "Parquet magic under a .csv name" },
        { "data.orc", "ORC\x00\x01\x02\x03", 7, "another columnar container" },
        { "data.feather", "ARROW1\x00\x00", 8, "Arrow IPC" },
        { "book.xlsx", "PK\x03\x04\x14\x00", 6, "xlsx is a zip, and a zip read as CSV is nonsense" },
        { "book.xlsx.csv", "PK\x03\x04\x14\x00", 6, "a zip whatever it is called" },
        { "old.xls", "\xd0\xcf\x11\xe0\xa1\xb1", 6, "an old Office file" },
        { "t.csv.gz", "\x1f\x8b\x08\x00", 4, "already compressed" },
        { "t.csv", "\x1f\x8b\x08\x00", 4, "gzip under a .csv name" },
        { "t.csv.bz2", "BZh91AY", 7, "already compressed" },
        { "t.csv.xz", "\xfd" "7zXZ\x00", 6, "already compressed" },
        { "t.csv.zst", "\x28\xb5\x2f\xfd", 4, "already compressed" },
        { "db.sqlite", "SQLite format 3\x00", 16, "a database, not a table file" },
        { "again.ppz", "PPZ2\0\0\0\0", 8, "compressing an archive again is always a mistake" },
        { "again.csv", "PPZ2\0\0\0\0", 8, "an archive named .csv" },
        { "agains.csv", "PPZS\0\0\0\0", 8, "a stream archive named .csv" },
        { "empty.xlsx", "id,name\n1,a\n", 12, "an Excel extension, whatever is inside" },
        { "stata.dta", "id,name\n1,a\n", 12, "Stata" },
        { "spss.sav", "id,name\n1,a\n", 12, "SPSS" },
    };
    const char *cmds[] = { "compress", "convert", "stream-compress" };
    for (size_t k = 0; k < N_OF(REFUSE); k++) {
        char src[1280], out[1280];
        snprintf(src, sizeof(src), "%s", tpath("%s", REFUSE[k].name));
        write_bytes(src, REFUSE[k].body, REFUSE[k].n);
        for (size_t c = 0; c < N_OF(cmds); c++) {
            snprintf(out, sizeof(out), "%s", tpath("%s.out.%s", REFUSE[k].name, c == 1 ? "json" : "ppz"));
            Buf err;
            int rc = !strcmp(cmds[c], "convert")
                     ? cli(NULL, &err, NULL, 3, cmds[c], src, out)
                     : cli(NULL, &err, NULL, 4, cmds[c], src, "-o", out);
            CHECK(rc == 1, "%s %s: exit %d, must be refused (%s)", cmds[c], REFUSE[k].name, rc, REFUSE[k].why);
            CHECK(!file_exists(out), "%s %s: refused but wrote output", cmds[c], REFUSE[k].name);
            CHECK(err.len > 0 && count_lines(&err) <= 3, "%s %s: the refusal is %zu lines: %s", cmds[c],
                  REFUSE[k].name, count_lines(&err), err.data);
            buf_free(&err);
        }
    }

    /* Ordinary delimited text and JSON must still be accepted -- a careless
     * guard is exactly as bad as a missing one. */
    struct { const char *name; const char *body; } ACCEPT[] = {
        { "plain.csv", "id,name,value\n1,alpha,10\n2,beta,20\n3,gamma,30\n" },
        { "quoted.csv", "id,note\n1,\"has, comma\"\n2,\"has \"\"quotes\"\"\"\n" },
        { "newline.csv", "id,note\n1,\"line one\nline two\"\n2,plain\n" },
        { "utf8.csv", "id,name\n1,caf\xc3\xa9\n2,\xe5\x8c\x97\xe4\xba\xac\n" },
        { "noext", "id,name,value\n1,alpha,10\n" },
        { "upper.CSV", "id,name,value\n1,alpha,10\n" },
        { "data.tsv", "id\tname\n1\talpha\n2\tbeta\n" },
        { "data.psv", "id|name\n1|alpha\n2|beta\n" },
        { "data.json", "[{\"id\": 1, \"name\": \"alpha\"}, {\"id\": 2, \"name\": \"beta\"}]\n" },
        { "data.jsonl", "{\"id\": 1}\n{\"id\": 2}\n" },
        { "data.ndjson", "{\"id\": 1}\n{\"id\": 2}\n" },
        { "nul.csv", "id,name\n1,a\n" },            /* a NUL is legal in a cell */
        { "PK.csv", "PK,name\n1,a\n" },             /* "PK" is not "PK\3\4" */
    };
    for (size_t k = 0; k < N_OF(ACCEPT); k++) {
        char src[1280], arc[1280], back[1280];
        snprintf(src, sizeof(src), "%s", tpath("%s", ACCEPT[k].name));
        if (!strcmp(ACCEPT[k].name, "nul.csv")) write_bytes(src, "id,name\n1,a\0b\n", 14);
        else write_str(src, ACCEPT[k].body);
        snprintf(arc, sizeof(arc), "%s", tpath("%s.ppz", ACCEPT[k].name));
        Buf err;
        int rc = cli(NULL, &err, NULL, 4, "compress", src, "-o", arc);
        CHECK(rc == 0 && file_exists(arc), "%s: refused, must be accepted: %s", ACCEPT[k].name, err.data);
        buf_free(&err);
        /* restored into the same format, it is the same table */
        const char *dot = strrchr(ACCEPT[k].name, '.');
        snprintf(back, sizeof(back), "%s", tpath("back-%s", dot && strcmp(dot, ".CSV") ? ACCEPT[k].name : "x.csv"));
        if (!dot) snprintf(back, sizeof(back), "%s", tpath("back-noext.csv"));
        rc = cli(NULL, &err, NULL, 4, "restore", arc, "-o", back);
        if (CHECK(rc == 0, "%s: restore: %s", ACCEPT[k].name, err.data)) same_file_table(src, back, ACCEPT[k].name);
        buf_free(&err);
    }
}

/* ------------------------------------------------- damaged and wrong input */

static void damaged(void)
{
    h_section("restore and info on things that are not archives");
    char src[1280], arc[1280];
    snprintf(src, sizeof(src), "%s", tpath("d.csv"));
    write_str(src, PLAIN);
    snprintf(arc, sizeof(arc), "%s", tpath("d.ppz"));
    cli(NULL, NULL, NULL, 4, "compress", src, "-o", arc);
    Buf good;
    read_bytes(arc, &good);

    struct { const char *label; Buf body; } C[6];
    for (int k = 0; k < 6; k++) buf_init(&C[k].body);
    C[0].label = "a CSV";            buf_put(&C[0].body, PLAIN, strlen(PLAIN));
    C[1].label = "an empty file";
    C[2].label = "truncated";        buf_put(&C[2].body, good.data, good.len / 2);
    C[3].label = "bit-flipped";      buf_put(&C[3].body, good.data, good.len);
    C[3].body.data[good.len - 3] ^= 0x40;
    C[4].label = "magic only";       buf_put(&C[4].body, good.data, 4);
    C[5].label = "random bytes";
    Rng r = { 3 };
    for (int i = 0; i < 5000; i++) buf_putc(&C[5].body, (char)rng_below(&r, 256));

    for (int k = 0; k < 6; k++) {
        char p[1280], out[1280];
        snprintf(p, sizeof(p), "%s", tpath("bad%d.ppz", k));
        write_bytes(p, C[k].body.data, C[k].body.len);
        snprintf(out, sizeof(out), "%s", tpath("bad%d-out.csv", k));
        Buf err;
        int rc = cli(NULL, &err, NULL, 4, "restore", p, "-o", out);
        /* a bit flip in the last bytes may still decode; everything else
         * must be refused -- and never by a crash */
        if (k != 3) {
            CHECK(rc == 1, "restore of %s: exit %d", C[k].label, rc);
            CHECK(!file_exists(out), "restore of %s: refused but wrote %s", C[k].label, out);
            CHECK(count_lines(&err) == 1, "restore of %s: the error is %zu lines: %s", C[k].label,
                  count_lines(&err), err.data);
        } else CHECK(rc == 0 || rc == 1, "restore of %s: exit %d", C[k].label, rc);
        buf_free(&err);
        rc = cli(NULL, &err, NULL, 2, "info", p);
        if (k != 3) CHECK(rc == 1 && count_lines(&err) == 1, "info on %s: exit %d: %s", C[k].label, rc, err.data);
        buf_free(&err);
        buf_free(&C[k].body);
    }
    /* missing files and directories */
    Buf err;
    int rc = cli(NULL, &err, NULL, 2, "restore", tpath("missing.ppz"));
    CHECK(rc == 1 && count_lines(&err) == 1, "restore of a missing file: exit %d: %s", rc, err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 2, "compress", tpath("missing.csv"));
    CHECK(rc == 1 && strstr((char *)err.data, "no such file"), "compress of a missing file: %s", err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 2, "compress", h_tmpdir);
    CHECK(rc == 1 && strstr((char *)err.data, "directory"), "compress of a directory: %s", err.data);
    buf_free(&err);
    rc = cli(NULL, &err, NULL, 4, "compress", src, "-o", tpath("no/such/dir.ppz"));
    CHECK(rc == 1, "compress into a missing directory: exit %d", rc);
    buf_free(&err);
    /* a wide row is refused at the command line too, nothing written */
    write_str(tpath("wide.csv"), "a,b\n1,2,3\n");
    rc = cli(NULL, &err, NULL, 4, "compress", tpath("wide.csv"), "-o", tpath("wide.ppz"));
    CHECK(rc == 1 && !file_exists(tpath("wide.ppz")) && strstr((char *)err.data, "not drop data"),
          "a wide row: exit %d: %s", rc, err.data);
    buf_free(&err);
    buf_free(&good);
}

int main(int argc, char **argv)
{
    h_suite = "cli";
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc < 2 || access(argv[1], X_OK)) {
        printf("usage: t_cli /path/to/polypress\n");
        h_fail++;
        return h_done();
    }
    BIN = argv[1];
    tmpdir_make("cli");
    happy();
    guard();
    damaged();
    tmpdir_remove();
    return h_done();
}
