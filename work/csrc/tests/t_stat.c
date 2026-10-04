/* Stata, SPSS and SAS files (ppz_stat.c, through ReadStat).
 *
 *     t_stat /path/to/polypress [IN/formats]
 *
 * The fixtures are written here by ReadStat's own writers -- one small table
 * in every format it writes -- so nothing binary is committed and every
 * expected cell is known: dates on both sides of the epoch, a datetime, a
 * labelled code, a tagged missing value where the format has them, and an
 * empty string. Then: an archive gives the original back byte for byte; the
 * Stata column layout is undone exactly; damaged and lying archives are
 * refused without a crash; the command line restores, translates and says
 * what a translation drops. A directory of real files, if given and present,
 * is round-tripped too.
 */

#include "harness.h"
#include "../readstat/readstat.h"

#include <dirent.h>

static const char *BIN;

/* -------------------------------------------------------------- fixtures */

static ssize_t to_buf(const void *p, size_t n, void *ctx)
{
    buf_put((Buf *)ctx, p, n);
    return (ssize_t)n;
}

#define NROWS 4
#define NCOLS 5

/* The table every format holds, as the CSV translation must print it. */
static const char *NAMES[NCOLS] = { "num", "code", "name", "day", "stamp" };
static const char *CELLS[NROWS][NCOLS] = {
    { "1.5",         "1", "alpha", "1960-01-01", "1960-01-02 01:01:01" },
    { "-2.25",       "2", "beta",  "1961-01-01", "1959-12-31 23:59:59" },
    { "",            "1", "",      "1959-12-31", "2018-05-06 10:10:10.5" },
    { "10000000000", "2", "gamma", "2018-05-06", "" },
};
/* days and seconds from 1960-01-01 for the cells above */
static const double DAYS[NROWS] = { 0, 366, -1, 21310 };
static const double SECS[NROWS] = { 90061, -1, 1841220610.5, 0 };

static int has_labels(const char *f) { return strcmp(f, "xpt") && strcmp(f, "sas7bdat"); }
static int has_tags(const char *f) { return !strcmp(f, "dta"); }

/* One fixture file in format f. The last row's code is a tagged missing
 * (.a) where the format has them. */
static int make_fixture(const char *f, Buf *out)
{
    buf_init(out);
    int spss = !strcmp(f, "sav") || !strcmp(f, "zsav") || !strcmp(f, "por");
    int dta = !strcmp(f, "dta");
    readstat_writer_t *w = readstat_writer_init();
    readstat_set_data_writer(w, to_buf);
    readstat_writer_set_file_label(w, "fixture");
    readstat_label_set_t *ls = NULL;
    if (has_labels(f)) {
        ls = readstat_add_label_set(w, dta ? READSTAT_TYPE_INT32 : READSTAT_TYPE_DOUBLE, "codes");
        if (dta) { readstat_label_int32_value(ls, 1, "one"); readstat_label_int32_value(ls, 2, "two"); }
        else { readstat_label_double_value(ls, 1, "one"); readstat_label_double_value(ls, 2, "two"); }
    }
    readstat_variable_t *v[NCOLS];
    /* the portable format allows upper-case names only */
    int por = !strcmp(f, "por");
    v[0] = readstat_add_variable(w, por ? "NUM" : "num", READSTAT_TYPE_DOUBLE, 0);
    v[1] = readstat_add_variable(w, por ? "CODE" : "code", dta ? READSTAT_TYPE_INT8 : READSTAT_TYPE_DOUBLE, 0);
    v[2] = readstat_add_variable(w, por ? "NAME" : "name", READSTAT_TYPE_STRING, 8);
    v[3] = readstat_add_variable(w, por ? "DAY" : "day", READSTAT_TYPE_DOUBLE, 0);
    v[4] = readstat_add_variable(w, por ? "STAMP" : "stamp", READSTAT_TYPE_DOUBLE, 0);
    readstat_variable_set_label(v[0], "a number");
    readstat_variable_set_label(v[3], "a day");
    if (ls) readstat_variable_set_label_set(v[1], ls);
    readstat_variable_set_format(v[3], dta ? "%td" : spss ? "DATE11" : "DATE9");
    readstat_variable_set_format(v[4], dta ? "%tc" : "DATETIME22.1");
    if (!strcmp(f, "zsav")) readstat_writer_set_compression(w, READSTAT_COMPRESS_BINARY);
    if (dta) readstat_writer_set_file_format_version(w, 118);
    if (!strcmp(f, "xpt")) readstat_writer_set_file_format_version(w, 5);

    readstat_error_t e;
    if (dta) e = readstat_begin_writing_dta(w, out, NROWS);
    else if (!strcmp(f, "sav") || !strcmp(f, "zsav")) e = readstat_begin_writing_sav(w, out, NROWS);
    else if (!strcmp(f, "por")) e = readstat_begin_writing_por(w, out, NROWS);
    else if (!strcmp(f, "xpt")) e = readstat_begin_writing_xport(w, out, NROWS);
    else e = readstat_begin_writing_sas7bdat(w, out, NROWS);
    static const double NUM[NROWS] = { 1.5, -2.25, 0, 1e10 };
    static const char *NAME[NROWS] = { "alpha", "beta", "", "gamma" };
    for (int i = 0; i < NROWS && e == READSTAT_OK; i++) {
        /* the first row is where some writers set themselves up; going on
         * after a failure there crashes the POR writer */
        if ((e = readstat_begin_row(w)) != READSTAT_OK) {
            printf("  %s writer: %s\n", f, readstat_error_message(e));
            break;
        }
        if (i == 2) readstat_insert_missing_value(w, v[0]);
        else readstat_insert_double_value(w, v[0], NUM[i]);
        if (dta) readstat_insert_int8_value(w, v[1], (int8_t)(i % 2 + 1));
        else readstat_insert_double_value(w, v[1], i % 2 + 1);
        readstat_insert_string_value(w, v[2], NAME[i]);
        double day = DAYS[i], sec = SECS[i];
        if (spss) { day = (DAYS[i] + 137775) * 86400; sec = SECS[i] + 137775.0 * 86400; }
        readstat_insert_double_value(w, v[3], day);
        if (i == 3) readstat_insert_missing_value(w, v[4]);
        else readstat_insert_double_value(w, v[4], dta ? sec * 1000 : sec);
        e = readstat_end_row(w);
    }
    if (e == READSTAT_OK) e = readstat_end_writing(w);
    readstat_writer_free(w);
    return e == READSTAT_OK ? 0 : -1;
}

/* the same file with the last row's code made a tagged .a, for Stata */
static int make_tagged_dta(Buf *out)
{
    buf_init(out);
    readstat_writer_t *w = readstat_writer_init();
    readstat_set_data_writer(w, to_buf);
    readstat_variable_t *a = readstat_add_variable(w, "x", READSTAT_TYPE_DOUBLE, 0);
    readstat_writer_set_file_format_version(w, 118);
    readstat_error_t e = readstat_begin_writing_dta(w, out, 3);
    readstat_begin_row(w); readstat_insert_double_value(w, a, 7); readstat_end_row(w);
    readstat_begin_row(w); readstat_insert_tagged_missing_value(w, a, 'a'); readstat_end_row(w);
    readstat_begin_row(w); readstat_insert_missing_value(w, a); readstat_end_row(w);
    if (e == READSTAT_OK) e = readstat_end_writing(w);
    readstat_writer_free(w);
    return e == READSTAT_OK ? 0 : -1;
}

static int str_is(Str s, const char *want) { return s.n == strlen(want) && !memcmp(s.p, want, s.n); }

static int tables_same(const Table *a, const Table *b)
{
    if (a->nrows != b->nrows || a->ncols != b->ncols) return 0;
    for (size_t j = 0; j < a->ncols; j++) if (strcmp(a->names[j], b->names[j])) return 0;
    for (size_t k = 0; k < a->nrows * a->ncols; k++)
        if (a->cells[k].n != b->cells[k].n ||
            (a->cells[k].n && memcmp(a->cells[k].p, b->cells[k].p, a->cells[k].n))) return 0;
    return 1;
}

/* An archive with metadata `meta` and `payload` as its binary stream. */
static void forge(const char *meta, const uint8_t *payload, size_t n, Buf *out)
{
    Buf mz, bz, tz;
    buf_init(&mz); buf_init(&bz); buf_init(&tz);
    ppz_lzma_compress((const uint8_t *)meta, strlen(meta), &mz);
    ppz_lzma_compress(payload, n, &bz);
    ppz_lzma_compress((const uint8_t *)"", 0, &tz);
    buf_init(out);
    buf_put(out, PPZ_MAGIC, 4);
    size_t lens[3] = { mz.len, bz.len, tz.len };
    for (int i = 0; i < 3; i++)
        for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((lens[i] >> s) & 0xFF));
    buf_put(out, mz.data, mz.len); buf_put(out, bz.data, bz.len); buf_put(out, tz.data, tz.len);
    buf_free(&mz); buf_free(&bz); buf_free(&tz);
}

/* ------------------------------------------------------- every format */

static const char *FORMATS[] = { "dta", "sav", "zsav", "por", "xpt", "sas7bdat" };
#define NFMT (sizeof(FORMATS) / sizeof(FORMATS[0]))

static void formats(void)
{
    h_section("every format ReadStat writes: cells, schema, archive");
    for (size_t q = 0; q < NFMT; q++) {
        const char *f = FORMATS[q];
        Buf file;
        if (!CHECK(make_fixture(f, &file) == 0, "%s: ReadStat could not write the fixture", f)) continue;
        Table t;
        Buf schema;
        StatLoss loss;
        char err[512] = "";
        if (!CHECK(stat_read(file.data, file.len, f, NULL, &t, &schema, &loss, err, sizeof(err)) == 0,
                   "%s: read: %s", f, err)) { buf_free(&file); continue; }
        CHECK(t.nrows == NROWS && t.ncols == NCOLS, "%s: %zu x %zu", f, t.nrows, t.ncols);
        for (size_t j = 0; j < t.ncols && j < NCOLS; j++) {
            /* xpt and por upper-case names */
            CHECK(!strcasecmp(t.names[j], NAMES[j]), "%s: column %zu is %s", f, j, t.names[j]);
            for (size_t i = 0; i < t.nrows && i < NROWS; i++) {
                Str c = table_at(&t, i, j);
                const char *want = CELLS[i][j];
                /* fixed-width strings come back without their padding */
                CHECK(str_is(c, want), "%s: row %zu %s = \"%.*s\", want \"%s\"",
                      f, i, NAMES[j], (int)c.n, c.p, want);
            }
        }
        CHECK(loss.dates == 2, "%s: %zu date columns, want 2", f, loss.dates);
        CHECK(loss.var_labels == 2, "%s: %zu variable labels, want 2", f, loss.var_labels);
        if (has_labels(f)) {
            CHECK(loss.value_labels == 1, "%s: %zu labelled columns", f, loss.value_labels);
            CHECK(memmem(schema.data, schema.len, "\"one\"", 5) != NULL, "%s: label set missing from the schema", f);
        }
        Js *sc = js_parse((const char *)schema.data, schema.len);
        CHECK(sc && js_get(sc, "columns") && js_get(sc, "columns")->count == NCOLS,
              "%s: the schema is not JSON with %d columns", f, NCOLS);
        js_free(sc);

        /* the archive gives the original back, and decodes to the same table */
        Buf arc, back;
        buf_init(&arc); buf_init(&back);
        char f2[16] = "";
        Js *meta = NULL;
        CHECK(ppz_encode_original(file.data, file.len, f, NULL, &schema, &arc) == 0, "%s: encode", f);
        CHECK(ppz_original(arc.data, arc.len, &back, f2, sizeof(f2), &meta) == 1, "%s: not an original archive", f);
        CHECK(back.len == file.len && !memcmp(back.data, file.data, file.len), "%s: original bytes differ", f);
        CHECK(!strcmp(f2, f), "%s: format came back as %s", f, f2);
        const Js *lay = js_get(js_get(meta, "original"), "layout");
        CHECK(!strcmp(f, "dta") ? lay != NULL : lay == NULL, "%s: layout %s", f, lay ? "present" : "absent");
        js_free(meta);
        Table d;
        CHECK(ppz_decode(arc.data, arc.len, &d) == 0 && tables_same(&t, &d), "%s: decoded table differs", f);
        table_free(&d);

        /* an older build's view: no "columns", so it refuses */
        CHECK(ppz_original(arc.data, arc.len, NULL, f2, sizeof(f2), NULL) == 1, "%s: header-only probe", f);

        buf_free(&arc); buf_free(&back); buf_free(&schema); buf_free(&file);
        table_free(&t);
    }

    Buf tg;
    if (CHECK(make_tagged_dta(&tg) == 0, "tagged fixture")) {
        Table t;
        StatLoss l;
        char err[256];
        if (CHECK(stat_read(tg.data, tg.len, "dta", NULL, &t, NULL, &l, err, sizeof(err)) == 0, "tagged: %s", err)) {
            CHECK(str_is(table_at(&t, 0, 0), "7") && str_is(table_at(&t, 1, 0), ".a") &&
                  str_is(table_at(&t, 2, 0), ""), "tagged missing: want 7, .a, empty");
            CHECK(l.tagged == 1 && l.tagged_cols == 1, "tagged count %zu", l.tagged);
            table_free(&t);
        }
        buf_free(&tg);
    }
}

/* ------------------------------------------------------------- hostile */

static Buf g_arc;

static int child_decode(void *arg)
{
    (void)arg;
    Table t;
    if (ppz_decode(g_arc.data, g_arc.len, &t) == 0) table_free(&t);
    return 0;
}

static void hostile(void)
{
    h_section("damaged and lying archives of original files");
    Buf file, schema, arc;
    Table t;
    char err[256];
    make_fixture("dta", &file);
    stat_read(file.data, file.len, "dta", NULL, &t, &schema, NULL, err, sizeof(err));
    table_free(&t);
    buf_init(&arc);
    ppz_encode_original(file.data, file.len, "dta", NULL, &schema, &arc);

    /* every cut-off length is refused, never half-restored */
    size_t refused = 0, cuts = 0;
    for (size_t n = 0; n < arc.len; n += (arc.len / 97 ? arc.len / 97 : 1)) {
        Buf b;
        buf_init(&b);
        char f[16];
        cuts++;
        if (ppz_original(arc.data, n, &b, f, sizeof(f), NULL) < 0) refused++;
        buf_free(&b);
    }
    CHECK(refused == cuts, "%zu of %zu truncations accepted", cuts - refused, cuts);

    /* lying layouts: each one would move bytes outside the file */
    const char *LIES[] = {
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":0,\"rows\":99999999,\"widths\":[8]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":999999,\"rows\":1,\"widths\":[1]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":0,\"rows\":1,\"widths\":[0]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":0,\"rows\":1,\"widths\":[]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":-5,\"rows\":1,\"widths\":[1]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":0,\"rows\":4611686018427387904,\"widths\":[4]}",
        "\"layout\":{\"kind\":\"dta-columns\",\"at\":0,\"rows\":1,\"widths\":[\"8\"]}",
        "\"layout\":{\"kind\":\"rows-backwards\",\"at\":0,\"rows\":1,\"widths\":[1]}",
        "\"layout\":7",
    };
    for (size_t k = 0; k < sizeof(LIES) / sizeof(LIES[0]); k++) {
        char meta[512];
        snprintf(meta, sizeof(meta), "{\"original\":{\"format\":\"dta\",\"bytes\":%zu,%s},"
                 "\"schema\":{}}", file.len, LIES[k]);
        Buf fake, b;
        forge(meta, file.data, file.len, &fake);
        buf_init(&b);
        char f[16];
        CHECK(ppz_original(fake.data, fake.len, &b, f, sizeof(f), NULL) == -1, "lie %zu accepted: %s", k, LIES[k]);
        buf_free(&b);
        buf_free(&fake);
    }
    /* wrong byte count, unknown format, format that is not a string */
    const char *BAD[] = {
        "{\"original\":{\"format\":\"dta\",\"bytes\":1},\"schema\":{}}",
        "{\"original\":{\"format\":\"xlsx\",\"bytes\":%zu},\"schema\":{}}",
        "{\"original\":{\"format\":7,\"bytes\":%zu},\"schema\":{}}",
        "{\"original\":{\"bytes\":%zu},\"schema\":{}}",
        "{\"original\":[],\"schema\":{}}",
    };
    for (size_t k = 0; k < sizeof(BAD) / sizeof(BAD[0]); k++) {
        char meta[512];
        snprintf(meta, sizeof(meta), BAD[k], file.len);
        Buf fake, b;
        forge(meta, file.data, file.len, &fake);
        buf_init(&b);
        char f[16];
        Table d;
        CHECK(ppz_original(fake.data, fake.len, &b, f, sizeof(f), NULL) == -1, "bad meta %zu accepted", k);
        CHECK(ppz_decode(fake.data, fake.len, &d) != 0, "bad meta %zu decoded", k);
        buf_free(&b);
        buf_free(&fake);
    }
    /* a valid archive whose original is not a Stata file: refused, no crash */
    {
        char meta[256];
        snprintf(meta, sizeof(meta), "{\"original\":{\"format\":\"dta\",\"bytes\":12},\"schema\":{}}");
        Buf fake;
        forge(meta, (const uint8_t *)"id,name\n1,a\n", 12, &fake);
        Table d;
        CHECK(ppz_decode(fake.data, fake.len, &d) != 0, "a CSV inside a .dta archive decoded");
        buf_free(&fake);
    }

    /* random damage to the original bytes inside a valid archive, in every
     * format: ReadStat parses hostile input here -- in a child, so a crash is
     * a failure, not the end */
    Rng rng = { 2026 };
    for (size_t q = 0; q < NFMT; q++) {
        Buf orig;
        if (make_fixture(FORMATS[q], &orig)) continue;
        size_t ok = 0, trials = 100;
        for (size_t k = 0; k < trials; k++) {
            Buf dam;
            buf_init(&dam);
            buf_put(&dam, orig.data, orig.len);
            for (int f = 0; f < 4; f++) dam.data[rng_next(&rng) % dam.len] ^= (uint8_t)(1 + rng_next(&rng) % 255);
            char meta[128];
            snprintf(meta, sizeof(meta), "{\"original\":{\"format\":\"%s\",\"bytes\":%zu},"
                     "\"schema\":{}}", FORMATS[q], dam.len);
            buf_free(&g_arc);
            forge(meta, dam.data, dam.len, &g_arc);
            ChildResult r;
            run_in_child(child_decode, NULL, 10000, (size_t)512 << 20, &r);
            if (child_survived(&r)) ok++;
            buf_free(&dam);
        }
        CHECK(ok == trials, "%s: %zu of %zu damaged files crashed or hung the decoder",
              FORMATS[q], trials - ok, trials);
        buf_free(&orig);
    }
    buf_free(&g_arc);
    buf_free(&file); buf_free(&schema); buf_free(&arc);
}

/* ------------------------------------------------------- the command line */

static int cli(Buf *out, int nargs, ...)
{
    char *argv[12];
    argv[0] = (char *)BIN;
    va_list ap;
    va_start(ap, nargs);
    for (int i = 0; i < nargs; i++) argv[1 + i] = va_arg(ap, char *);
    va_end(ap);
    argv[1 + nargs] = NULL;
    char o[1280];
    snprintf(o, sizeof(o), "%s", tpath("cli.out"));
    ChildResult r;
    run_cmd(argv, NULL, o, o, 60000, &r);
    if (out) { read_bytes(o, out); buf_putc(out, 0); out->len--; }
    CHECK(child_survived(&r), "polypress %s crashed", argv[1]);
    return r.code;
}

static void command_line(void)
{
    h_section("command line");
    Buf file, out;
    make_fixture("dta", &file);
    char src[1280], arc[1280], back[1280], csv[1280], sav[1280];
    snprintf(src, sizeof(src), "%s", tpath("t.dta"));
    snprintf(arc, sizeof(arc), "%s", tpath("t.dta.ppz"));
    snprintf(back, sizeof(back), "%s", tpath("back.dta"));
    snprintf(csv, sizeof(csv), "%s", tpath("t.csv"));
    snprintf(sav, sizeof(sav), "%s", tpath("t.sav"));
    write_bytes(src, file.data, file.len);

    CHECK(cli(&out, 4, "compress", src, "-o", arc) == 0, "compress: %s", out.data);
    CHECK(strstr((char *)out.data, "byte for byte") != NULL, "compress does not say it kept the file: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 4, "restore", arc, "-o", back) == 0, "restore: %s", out.data);
    buf_free(&out);
    Buf got;
    read_bytes(back, &got);
    CHECK(got.len == file.len && !memcmp(got.data, file.data, file.len), "restored .dta is not the original");
    buf_free(&got);

    CHECK(cli(&out, 4, "restore", arc, "-o", csv) == 0, "restore to csv: %s", out.data);
    CHECK(strstr((char *)out.data, "translation") && strstr((char *)out.data, "value labels"),
          "restore to csv does not name what it dropped: %s", out.data);
    buf_free(&out);
    read_bytes(csv, &got);
    buf_putc(&got, 0);
    CHECK(strstr((char *)got.data, "1960-01-02 01:01:01") != NULL, "the csv has no ISO datetime: %s", got.data);
    buf_free(&got);

    CHECK(cli(&out, 4, "restore", arc, "-o", sav) == 0 && strstr((char *)out.data, "Stata -> SPSS"),
          "a .dta archive restored as .sav: %s", out.data);
    CHECK(file_exists(sav), "the .sav was not written");
    buf_free(&out);
    CHECK(cli(&out, 3, "convert", src, tpath("t2.xpt")) == 0 && strstr((char *)out.data, "SAS transport"),
          "convert .dta -> .xpt: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 3, "convert", csv, tpath("t3.dta")) == 1 && !file_exists(tpath("t3.dta")),
          "convert .csv -> .dta was not refused: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 3, "convert", src, csv) == 0 && strstr((char *)out.data, "translation"),
          "convert .dta -> .csv: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 2, "info", arc) == 0 && strstr((char *)out.data, "Stata"), "info: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 3, "info", arc, "--json") == 0 && strstr((char *)out.data, "\"format\": \"dta\""),
          "info --json: %s", out.data);
    buf_free(&out);
    CHECK(cli(&out, 4, "stream-compress", src, "-o", tpath("s.ppz")) == 1, "stream-compress took a .dta");
    buf_free(&out);
    buf_free(&file);
}


/* ------------------------------------------------- one format to another */

/* x: 1, -9, 95, system missing; -9 and 90-99 are SPSS user-missing, and -9
 * and 1 have value labels */
static int make_user_missing_sav(Buf *out)
{
    buf_init(out);
    readstat_writer_t *w = readstat_writer_init();
    readstat_set_data_writer(w, to_buf);
    readstat_label_set_t *ls = readstat_add_label_set(w, READSTAT_TYPE_DOUBLE, "xl");
    readstat_label_double_value(ls, 1, "one");
    readstat_label_double_value(ls, -9, "refused");
    readstat_variable_t *x = readstat_add_variable(w, "x", READSTAT_TYPE_DOUBLE, 0);
    readstat_variable_set_label_set(x, ls);
    readstat_variable_add_missing_double_range(x, 90, 99);
    readstat_variable_add_missing_double_value(x, -9);
    readstat_variable_t *by = readstat_add_variable(w, "BY_ok", READSTAT_TYPE_DOUBLE, 0);
    readstat_error_t e = readstat_begin_writing_sav(w, out, 4);
    static const double X[4] = { 1, -9, 95, 0 };
    for (int i = 0; i < 4 && e == READSTAT_OK; i++) {
        readstat_begin_row(w);
        if (i == 3) readstat_insert_missing_value(w, x);
        else readstat_insert_double_value(w, x, X[i]);
        readstat_insert_double_value(w, by, i);
        e = readstat_end_row(w);
    }
    if (e == READSTAT_OK) e = readstat_end_writing(w);
    readstat_writer_free(w);
    return e == READSTAT_OK ? 0 : -1;
}

/* a Stata file with awkward names and one long text value */
static int make_awkward_dta(Buf *out, size_t longlen)
{
    buf_init(out);
    readstat_writer_t *w = readstat_writer_init();
    readstat_set_data_writer(w, to_buf);
    readstat_writer_set_file_format_version(w, 118);
    readstat_variable_t *a = readstat_add_variable(w, "with_a_rather_long_name", READSTAT_TYPE_DOUBLE, 0);
    readstat_variable_t *b = readstat_add_variable(w, "txt", READSTAT_TYPE_STRING, longlen);
    readstat_error_t e = readstat_begin_writing_dta(w, out, 2);
    char *big = malloc(longlen + 1);
    memset(big, 'x', longlen); big[longlen] = 0;
    readstat_begin_row(w); readstat_insert_double_value(w, a, 1); readstat_insert_string_value(w, b, big); readstat_end_row(w);
    readstat_begin_row(w); readstat_insert_double_value(w, a, 2); readstat_insert_string_value(w, b, "short"); readstat_end_row(w);
    free(big);
    if (e == READSTAT_OK) e = readstat_end_writing(w);
    readstat_writer_free(w);
    return e == READSTAT_OK ? 0 : -1;
}

static int translate_to(const Buf *in, const char *sf, const char *df, Table *t, Buf *schema, Buf *report)
{
    Buf out;
    char err[1024] = "";
    size_t r, c;
    if (stat_translate(in->data, in->len, sf, NULL, df, &out, report, &r, &c, err, sizeof(err))) {
        printf("  %s -> %s: %s\n", sf, df, err);
        return -1;
    }
    int rc = stat_read(out.data, out.len, df, NULL, t, schema, NULL, err, sizeof(err));
    if (rc) printf("  %s -> %s: reading it back: %s\n", sf, df, err);
    buf_free(&out);
    return rc;
}

static int has(const Buf *b, const char *s) { return b->len && memmem(b->data, b->len, s, strlen(s)) != NULL; }

static void translations(void)
{
    h_section("translation between formats");
    size_t pairs = 0;
    for (size_t i = 0; i < sizeof(FORMATS) / sizeof(FORMATS[0]); i++) {
        const char *f = FORMATS[i];
        Buf file;
        if (make_fixture(f, &file)) { buf_free(&file); continue; }
        for (size_t k = 0; k < sizeof(FORMATS) / sizeof(FORMATS[0]); k++) {
            const char *g = FORMATS[k];
            Table t;
            Buf schema, report;
            if (!CHECK(translate_to(&file, f, g, &t, &schema, &report) == 0, "%s -> %s failed", f, g)) continue;
            int ok = t.nrows == NROWS && t.ncols == NCOLS;
            for (size_t r = 0; ok && r < NROWS; r++)
                for (size_t j = 0; ok && j < NCOLS; j++)
                    if (!str_is(table_at(&t, r, j), CELLS[r][j])) {
                        Str c = table_at(&t, r, j);
                        printf("  %s -> %s: row %zu %s = \"%.*s\", want \"%s\"\n", f, g, r, NAMES[j],
                               (int)c.n, c.p, CELLS[r][j]);
                        ok = 0;
                    }
            for (size_t j = 0; ok && j < NCOLS; j++) ok = !strcasecmp(t.names[j], NAMES[j]);
            CHECK(ok, "%s -> %s: cells or names differ", f, g);
            CHECK(has(&schema, "\"a number\""), "%s -> %s: variable label lost", f, g);
            if (has_labels(f) && has_labels(g))
                CHECK(has(&schema, "\"one\""), "%s -> %s: value labels lost", f, g);
            if (has_labels(f) && !has_labels(g))
                CHECK(has(&report, "value labels"), "%s -> %s: dropped labels not reported: %s", f, g,
                      report.len ? (char *)report.data : "");
            table_free(&t);
            buf_free(&schema); buf_free(&report);
            pairs++;
        }
        buf_free(&file);
    }
    printf("  %zu format pairs\n", pairs);

    /* SPSS user-missing codes: letters in Stata and SAS transport, codes in SPSS */
    Buf um;
    if (CHECK(make_user_missing_sav(&um) == 0, "user-missing fixture")) {
        static const struct { const char *to, *cells[4], *say; } UM[] = {
            { "dta",      { "1", ".b", ".a", "" }, "extended missing" },
            { "xpt",      { "1", ".B", ".A", "" }, "extended missing" },
            { "sas7bdat", { "1", "-9", "95", "" }, "as their numbers" },
            { "por",      { "1", "-9", "95", "" }, NULL },
        };
        for (size_t u = 0; u < 4; u++) {
            Table t; Buf schema, report;
            if (!CHECK(translate_to(&um, "sav", UM[u].to, &t, &schema, &report) == 0, "user missing -> %s", UM[u].to)) continue;
            int ok = t.nrows == 4;
            for (size_t r = 0; ok && r < 4; r++) ok = str_is(table_at(&t, r, 0), UM[u].cells[r]);
            if (!ok) for (size_t r = 0; r < t.nrows; r++) { Str c = table_at(&t, r, 0); printf("    [%zu] %.*s\n", r, (int)c.n, c.p); }
            if (!ok) printf("    %.*s\n", (int)schema.len, (char *)schema.data);
            CHECK(ok, "user missing -> %s: cells differ", UM[u].to);
            if (UM[u].say) CHECK(has(&report, UM[u].say), "user missing -> %s: not reported: %s", UM[u].to,
                                 report.len ? (char *)report.data : "");
            if (!strcmp(UM[u].to, "dta")) {
                CHECK(has(&schema, "[\".b\",\"refused\"]"), "the -9 label did not follow it to .b");
                CHECK(has(&report, "x: 90 to 99 -> .a, -9 -> .b"), "the mapping is not reported: %s", (char *)report.data);
                CHECK(has(&schema, "[1,\"one\"]"), "the ordinary label was lost");
            }
            if (!strcmp(UM[u].to, "por")) CHECK(has(&schema, "\"missing\""), "missing codes not kept in .por");
            table_free(&t); buf_free(&schema); buf_free(&report);
        }
        buf_free(&um);
    }

    /* Stata's .a has no SPSS equivalent */
    Buf tg;
    if (CHECK(make_tagged_dta(&tg) == 0, "tagged fixture")) {
        Table t; Buf schema, report;
        if (CHECK(translate_to(&tg, "dta", "sav", &t, &schema, &report) == 0, "tagged -> sav")) {
            CHECK(str_is(table_at(&t, 0, 0), "7") && str_is(table_at(&t, 1, 0), "") &&
                  str_is(table_at(&t, 2, 0), ""), "tagged -> sav cells");
            CHECK(has(&report, "1 extended missing value"), "tagged loss not reported: %s",
                  report.len ? (char *)report.data : "");
            table_free(&t); buf_free(&schema); buf_free(&report);
        }
        buf_free(&tg);
    }

    /* names and long text */
    Buf aw;
    if (CHECK(make_awkward_dta(&aw, 300) == 0, "awkward fixture")) {
        Table t; Buf schema, report;
        if (CHECK(translate_to(&aw, "dta", "por", &t, &schema, &report) == 0, "awkward -> por")) {
            CHECK(!strcmp(t.names[0], "WITH_A_R"), "por name %s", t.names[0]);
            CHECK(table_at(&t, 0, 1).n == 255 && has(&report, "CUT to 255"), "long text -> por: %zu, %s",
                  table_at(&t, 0, 1).n, report.len ? (char *)report.data : "");
            table_free(&t); buf_free(&schema); buf_free(&report);
        }
        if (CHECK(translate_to(&aw, "dta", "xpt", &t, &schema, &report) == 0, "awkward -> xpt")) {
            CHECK(table_at(&t, 0, 1).n == 300 && !strcmp(t.names[0], "with_a_rather_long_name") &&
                  has(&report, "version 8"), "xpt did not move to version 8");
            table_free(&t); buf_free(&schema); buf_free(&report);
        }
        buf_free(&aw);
    }
    Buf lg;
    if (CHECK(make_awkward_dta(&lg, 2000) == 0, "long fixture")) {
        /* sav -> dta with text over 2045 bytes becomes a strL */
        Buf sav, rep2; char err[512]; size_t r, c;
        if (CHECK(stat_translate(lg.data, lg.len, "dta", NULL, "sav", &sav, &rep2, &r, &c, err, sizeof(err)) == 0, "-> sav: %s", err)) {
            buf_free(&rep2);
            Table t; Buf schema, report;
            if (CHECK(translate_to(&sav, "sav", "dta", &t, &schema, &report) == 0, "sav -> dta")) {
                CHECK(table_at(&t, 0, 1).n == 2000, "2000-byte text came back %zu", table_at(&t, 0, 1).n);
                table_free(&t); buf_free(&schema); buf_free(&report);
            }
            buf_free(&sav);
        }
        buf_free(&lg);
    }
}

/* ------------------------------------------------------------ real files */

static void real_files(const char *dir)
{
    h_section("real files");
    DIR *d = dir ? opendir(dir) : NULL;
    if (!d) { printf("  (no %s -- skipped)\n", dir ? dir : "directory"); return; }
    struct dirent *e;
    size_t n = 0;
    while ((e = readdir(d))) {
        const char *f = ppz_stat_format(e->d_name);
        if (!f) continue;
        char path[2048];
        snprintf(path, sizeof(path), "%s/%s", dir, e->d_name);
        Buf raw, schema, arc, back;
        read_bytes(path, &raw);
        Table t;
        char err[512] = "", f2[16];
        if (!CHECK(stat_read(raw.data, raw.len, f, NULL, &t, &schema, NULL, err, sizeof(err)) == 0,
                   "%s: %s", e->d_name, err)) { buf_free(&raw); continue; }
        buf_init(&arc); buf_init(&back);
        CHECK(ppz_encode_original(raw.data, raw.len, f, NULL, &schema, &arc) == 0 &&
              ppz_original(arc.data, arc.len, &back, f2, sizeof(f2), NULL) == 1 &&
              back.len == raw.len && !memcmp(back.data, raw.data, raw.len),
              "%s: not restored byte for byte", e->d_name);
        n++;
        table_free(&t);
        buf_free(&raw); buf_free(&schema); buf_free(&arc); buf_free(&back);
    }
    closedir(d);
    printf("  %zu files\n", n);
}

int main(int argc, char **argv)
{
    h_suite = "stat";
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc < 2 || access(argv[1], X_OK)) {
        printf("usage: t_stat /path/to/polypress [dir of real files]\n");
        h_fail++;
        return h_done();
    }
    BIN = argv[1];
    tmpdir_make("stat");
    formats();
    hostile();
    command_line();
    translations();
    real_files(argc > 2 && argv[2][0] ? argv[2] : NULL);
    tmpdir_remove();
    return h_done();
}
