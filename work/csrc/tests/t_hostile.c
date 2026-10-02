/* Invariant 3: the decoder treats its input as hostile.
 *
 * It reads files other people made, so a damaged or dishonest archive must be
 * refused -- or decode to some self-consistent table -- and never crash, hang
 * or allocate without bound. Two halves:
 *
 *   damaged bytes        truncations, bit flips, aimed shots at length
 *                        fields. That found decompressors doubling their
 *                        buffer toward a terabyte on input that never decodes
 *                        at any size. Covers both containers: single archives
 *                        and PPZS stream files.
 *   lying headers        headers that are well-formed and LIE. A random
 *                        mutation almost never lands on a coherent header, so
 *                        these are built on purpose: decompress the metadata,
 *                        edit the JSON, recompress. Three unchecked indices and
 *                        a segfault survived every fuzzing pass until this
 *                        existed (found by reading the code on 2026-07-29).
 *
 * Every case runs in a forked child watched by the parent (see harness.h for
 * why the watching is done from outside on macOS). Exit 0 = decoded, 1 =
 * refused: both fine. A signal, a timeout or the memory cap is a failure.
 */

#include "harness.h"

#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

#define TIMEOUT_MS 8000
#define MEM_CAP    ((size_t)768 << 20)

/* ------------------------------------------------------------ the probes */

static const uint8_t *g_blob;
static size_t g_len;
static const char *g_path, *g_out;

/* Decode, then touch every cell: a table whose cells point somewhere wild
 * should fail HERE, in the child, not later in someone's program. */
static int probe_decode(void *arg)
{
    (void)arg;
    Table t;
    if (ppz_decode(g_blob, g_len, &t)) return 1;
    volatile unsigned sum = 0;
    for (size_t j = 0; j < t.ncols; j++) sum += (unsigned)strlen(t.names[j]);
    for (size_t k = 0; k < t.nrows * t.ncols; k++)
        for (size_t i = 0; i < t.cells[k].n; i++) sum += (unsigned char)t.cells[k].p[i];
    table_free(&t);
    return 0;
}

static int probe_stream(void *arg)
{
    (void)arg;
    StreamInfo h;
    char err[512];
    if (ppz_stream_info(g_path, &h, err, sizeof(err))) return 1;
    ppz_stream_info_free(&h);
    StreamStats st;
    return ppz_stream_restore(g_path, g_out, &st, err, sizeof(err)) ? 1 : 0;
}

/* Run one case; returns 1 if it survived. */
static int survive(const char *kind, const char *name, const uint8_t *blob, size_t n)
{
    ChildResult r;
    g_blob = blob;
    g_len = n;
    if (!strcmp(kind, "PPZS")) {
        const char *p = tpath("case.ppz");
        write_bytes(p, blob, n);
        g_path = p;
        g_out = tpath("case-out.csv");
        run_in_child(probe_stream, NULL, TIMEOUT_MS, MEM_CAP, &r);
    } else {
        run_in_child(probe_decode, NULL, TIMEOUT_MS, MEM_CAP, &r);
    }
    char d[128];
    return CHECK(child_survived(&r) && (r.code == 0 || r.code == 1), "%s %s: %s", kind, name,
                 child_describe(&r, d, sizeof(d)));
}

/* A truncated archive is missing its end: raw LZMA2 ends with an end marker,
 * so a cut-short payload is always detectable. It must be refused -- or, if the cut fell somewhere harmless,
 * decode to exactly the original. Decoding to a DIFFERENT table is the worst
 * outcome a decoder has: a file that looks fine and is wrong. Exit 3 means
 * that happened. */
static const Table *g_want;
static int probe_same(void *arg)
{
    (void)arg;
    Table t;
    char why[64];
    if (ppz_decode(g_blob, g_len, &t)) return 1;
    int same = table_same(g_want, &t, why, sizeof(why));
    table_free(&t);
    return same ? 0 : 3;
}

static void truncations(const char *kind, const Buf *good, const Table *want)
{
    char title[64];
    snprintf(title, sizeof(title), "truncated %s archives: refused, never a different table", kind);
    h_section(title);
    g_want = want;
    int wrong = 0, cases = 0;
    size_t first_wrong = 0;
    for (size_t n = 4; n < good->len; n += (n < 64 ? 1 : 1 + good->len / 97)) {
        ChildResult r;
        g_blob = good->data;
        g_len = n;
        run_in_child(probe_same, NULL, TIMEOUT_MS, MEM_CAP, &r);
        cases++;
        char d[128];
        if (!child_survived(&r) || r.code == 3) {
            if (!wrong) first_wrong = n;
            wrong++;
            if (wrong <= 3)
                CHECK(0, "%s cut to %zu of %zu bytes: %s", kind, n, good->len,
                      r.code == 3 ? "decoded to a DIFFERENT table" : child_describe(&r, d, sizeof(d)));
        }
    }
    CHECK(wrong == 0, "%s: %d of %d truncations decoded to a wrong table or died (first at %zu bytes)",
          kind, wrong, cases, first_wrong);
}

/* ---------------------------------------------------------- the specimens */

/* Wide enough to exercise dict, text, numeric, a 2D group and exceptions --
 * the base table of test_lying_header.py. */
static void base_table(Table *t)
{
    TB b;
    tb_start(&b, 7, (const char *const[]){ "zip", "city", "note", "n", "a", "b", "c" });
    for (int i = 0; i < 120; i++) {
        tb_cellf(&b, "%05d", 90000 + i % 12);
        tb_cellf(&b, "city%d", i % 12);
        tb_cellf(&b, "note %d free text", i);
        if (i == 33) tb_cellz(&b, ""); else tb_cellf(&b, "%d", i * 7);
        tb_cellf(&b, "%.2f", 1.0 + i * 0.01);
        tb_cellf(&b, "%.2f", 1.5 + i * 0.01);
        tb_cellf(&b, "%.2f", 2.0 + i * 0.01);
    }
    tb_finish(&b, t);
}

/* The sample of test_hostile.py: 3000 rows of id, kind, val, note. */
static void sample_table(Table *t)
{
    TB b;
    tb_start(&b, 4, (const char *const[]){ "id", "kind", "val", "note" });
    for (int i = 0; i < 3000; i++) {
        tb_cellf(&b, "%d", i);
        tb_cellz(&b, (const char *[]){ "a", "b", "c" }[i % 3]);
        tb_cellf(&b, "%.2f", i * 1.5);
        tb_cellf(&b, "note %d", i % 9);
    }
    tb_finish(&b, t);
}

/* -------------------------------------------------------- damaged bytes */

static void mutate_all(const char *kind, const Buf *good)
{
    char title[64];
    snprintf(title, sizeof(title), "damaged %s archives", kind);
    h_section(title);
    int bad = 0, cases = 0;
    Buf m;
    buf_init(&m);
    char name[64];

    cases++; bad += !survive(kind, "empty", (const uint8_t *)"", 0);
    cases++; bad += !survive(kind, "magic only", good->data, 4);
    size_t step = good->len / 30 ? good->len / 30 : 1;
    for (size_t n = 0; n < good->len; n += step) {
        snprintf(name, sizeof(name), "truncate@%zu", n);
        cases++; bad += !survive(kind, name, good->data, n);
    }
    Rng r = { 11 };
    for (int i = 0; i < 80; i++) {
        m.len = 0;
        buf_put(&m, good->data, good->len);
        m.data[rng_below(&r, m.len)] = (uint8_t)rng_below(&r, 256);
        snprintf(name, sizeof(name), "byteset%d", i);
        cases++; bad += !survive(kind, name, m.data, m.len);
    }
    for (int i = 0; i < 40; i++) {
        m.len = 0;
        buf_put(&m, good->data, good->len);
        size_t at = (size_t)rng_below(&r, m.len);
        m.data[at] ^= (uint8_t)(1u << rng_below(&r, 8));
        snprintf(name, sizeof(name), "bitflip%d@%zu", i, at);
        cases++; bad += !survive(kind, name, m.data, m.len);
    }
    /* Aimed at the length fields. A fuzzer finds a bad length eventually; a
     * reader of the code finds it immediately. */
    const uint8_t vals[][8] = {
        { 0x80, 0, 0, 0, 0, 0, 0, 0 },          /* 2^63 */
        { 0, 0, 1, 0, 0, 0, 0, 0 },             /* 2^40 */
        { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff },
        { 0, 0, 0, 0, 0, 0, 0, 0 },
    };
    const char *vn[] = { "2^63", "2^40", "max", "0" };
    for (size_t v = 0; v < N_OF(vals); v++) {
        m.len = 0;
        buf_put(&m, good->data, good->len);
        if (!strcmp(kind, "PPZS")) {
            memcpy(m.data + m.len - 8, vals[v], 8);         /* index length, last */
            snprintf(name, sizeof(name), "index len=%s", vn[v]);
            cases++; bad += !survive(kind, name, m.data, m.len);
        } else {
            for (int f = 0; f < 3; f++) {                    /* meta, bins, text */
                m.len = 0;
                buf_put(&m, good->data, good->len);
                memcpy(m.data + 4 + 4 * f, vals[v], 4);
                snprintf(name, sizeof(name), "length field %d=%s", f, vn[v]);
                cases++; bad += !survive(kind, name, m.data, m.len);
            }
        }
    }
    buf_free(&m);
    printf("   %d cases, %d did not survive\n", cases, bad);
}

/* -------------------------------------------------------- lying headers */

/* An archive from its three parts: the metadata text and the two payloads,
 * raw LZMA2 each, behind the magic and three 4-byte big-endian lengths. */

static void rebuild(const char *meta, size_t mlen, const Buf *bins, const Buf *txt, Buf *out)
{
    Buf mz, bz, tz;
    buf_init(&mz); buf_init(&bz); buf_init(&tz);
    ppz_lzma_compress((const uint8_t *)meta, mlen, &mz);
    ppz_lzma_compress(bins->data, bins->len, &bz);
    ppz_lzma_compress(txt->data, txt->len, &tz);
    buf_init(out);
    buf_put(out, PPZ_MAGIC, 4);
    size_t lens[3] = { mz.len, bz.len, tz.len };
    for (int k = 0; k < 3; k++)
        for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((lens[k] >> s) & 0xFF));
    buf_put(out, mz.data, mz.len);
    buf_put(out, bz.data, bz.len);
    buf_put(out, tz.data, tz.len);
    buf_free(&mz); buf_free(&bz); buf_free(&tz);
}

/* Split an archive into metadata text and the two raw payloads. */
static int split(const Buf *blob, Buf *meta, Buf *bins, Buf *txt)
{
    const uint8_t *d = blob->data;
    size_t ml = ((size_t)d[4] << 24) | ((size_t)d[5] << 16) | ((size_t)d[6] << 8) | d[7];
    size_t bl = ((size_t)d[8] << 24) | ((size_t)d[9] << 16) | ((size_t)d[10] << 8) | d[11];
    buf_init(meta); buf_init(bins); buf_init(txt);
    return ppz_lzma_decompress(d + 16, ml, meta) || ppz_lzma_decompress(d + 16 + ml, bl, bins)
        || ppz_lzma_decompress(d + 16 + ml + bl, blob->len - 16 - ml - bl, txt);
}

/* Text substitution on the metadata: the first `from` becomes `to`. The
 * encoder's metadata is compact JSON in a fixed key order, so a lie can be
 * written as an edit of the text. Returns 0 if `from` was not there. */
static int edit(const Buf *meta, const char *from, const char *to, Buf *out)
{
    buf_init(out);
    const char *m = (const char *)meta->data;
    size_t fl = strlen(from);
    const char *at = NULL;
    for (size_t i = 0; i + fl <= meta->len; i++)
        if (!memcmp(m + i, from, fl)) { at = m + i; break; }
    if (!at) return 0;
    buf_put(out, m, (size_t)(at - m));
    buf_put(out, to, strlen(to));
    buf_put(out, at + fl, meta->len - (size_t)(at - m) - fl);
    return 1;
}

static int lie(const char *name, const Buf *meta, const char *from, const char *to,
               const Buf *bins, const Buf *txt)
{
    Buf m2, arc;
    if (!CHECK(edit(meta, from, to, &m2), "lie %s: the base metadata has no %s", name, from)) return 0;
    rebuild((const char *)m2.data, m2.len, bins, txt, &arc);
    int ok = survive("PPZ2", name, arc.data, arc.len);
    buf_free(&m2); buf_free(&arc);
    return ok;
}

/* A whole metadata document, written by hand, over hand-made payloads. */
static int crafted(const char *name, const char *meta, const void *bins, size_t bl,
                   const void *txt, size_t tl)
{
    Buf b, t, arc;
    buf_init(&b); buf_init(&t);
    buf_put(&b, bins, bl);
    buf_put(&t, txt, tl);
    rebuild(meta, strlen(meta), &b, &t, &arc);
    int ok = survive("PPZ2", name, arc.data, arc.len);
    buf_free(&b); buf_free(&t); buf_free(&arc);
    return ok;
}

static void lying_headers(void)
{
    h_section("lying headers: well-formed, and dishonest");
    Table t;
    base_table(&t);
    Buf blob;
    buf_init(&blob);
    ppz_encode(&t, &blob);
    table_free(&t);
    Buf meta, bins, txt;
    if (!CHECK(split(&blob, &meta, &bins, &txt) == 0, "base archive does not split")) return;
    printf("   base metadata: %.*s\n", (int)(meta.len < 600 ? meta.len : 600), meta.data);
    /* the specimens need every column kind the metadata can name */
    CHECK(strstr((char *)meta.data, "\"kind\":\"dict\"") && strstr((char *)meta.data, "\"kind\":\"text\"")
          && strstr((char *)meta.data, "\"kind\":\"grp\"") && strstr((char *)meta.data, "\"nex\""),
          "base table no longer produces dict, text, grp and exceptions");

    /* The Python suite's lies, as text edits of the compact metadata. */
    struct { const char *name, *from, *to; } L[] = {
        { "extra_text_column", "],\"groups\"", ",{\"kind\":\"text\"}],\"groups\"" },
        { "extra_num_column", "],\"groups\"", ",{\"kind\":\"num\",\"dec\":0,\"k\":0,\"warm\":[]}],\"groups\"" },
        { "extra_dict_column", "],\"groups\"", ",{\"kind\":\"dict\",\"n\":3,\"w\":\"<u1\",\"parent\":null}],\"groups\"" },
        { "extra_group", "\"groups\":[", "\"groups\":[[0,1,2]," },
        { "group_names_missing_column", "\"groups\":[", "\"groups\":[[47,48,49]," },
        { "group_of_huge_positions", "\"groups\":[", "\"groups\":[[4611686018427387904,-1,99]," },
        { "order_names_missing_column", "\"order\":[", "\"order\":[106," },
        { "order_repeats", "\"order\":[", "\"order\":[0,0,1,1," },
        { "parent_out_of_range", "\"parent\":", "\"parent\":57,\"x\":" },
        { "parent_negative", "\"parent\":", "\"parent\":-1,\"x\":" },
        { "nrows_huge", "\"nrows\":120", "\"nrows\":1099511627776" },
        { "nrows_negative", "\"nrows\":120", "\"nrows\":-5" },
        { "nrows_zero", "\"nrows\":120", "\"nrows\":0" },
        { "nrows_short", "\"nrows\":120", "\"nrows\":7" },
        { "nex_absurd", "\"nex\":", "\"nex\":1073741824,\"x\":" },
        { "nex_exceeds_rows", "\"nex\":", "\"nex\":130,\"x\":" },
        { "nlenbins_huge", "\"nlenbins\":", "\"nlenbins\":1048576,\"x\":" },
        { "nlenbins_negative", "\"nlenbins\":", "\"nlenbins\":-3,\"x\":" },
        { "bins_list_truncated", "\"bins\":[", "\"bins\":[1],\"was\":[" },
        { "bins_list_extended", "\"bins\":[", "\"bins\":[64,64,64," },
        { "bins_first_inflated", "\"bins\":[", "\"bins\":[100000,\"x\"," },
        { "smeta_truncated", "\"smeta\":[", "\"smeta\":[],\"was\":[" },
        { "smeta_count_inflated", "\"smeta\":[{\"n\":", "\"smeta\":[{\"n\":5000,\"x\":" },
        { "smeta_bytes_inflated", ",\"b\":", ",\"b\":50000,\"x\":" },
        { "k_absurd", "\"k\":", "\"k\":1048576,\"x\":" },
        { "dec_absurd", "\"dec\":", "\"dec\":4000,\"x\":" },
        { "width_lies", "\"<u1\"", "\"<u4\"" },
        { "columns_list_short", "\"columns\":[", "\"columns\":[],\"was\":[" },
        { "unknown_kind", "\"kind\":\"dict\"", "\"kind\":\"wormhole\"" },
        { "cols_not_a_list", "\"cols\":[", "\"cols\":{\"x\":[" },
    };
    int bad = 0;
    for (size_t k = 0; k < N_OF(L); k++) bad += !lie(L[k].name, &meta, L[k].from, L[k].to, &bins, &txt);
    printf("   %zu edited headers, %d did not survive\n", N_OF(L), bad);

    /* Hand-built archives, one lie each, over payloads small enough to reason
     * about. Each targets a length or count the decoder reads from the
     * metadata and then uses. */
    h_section("lying headers: hand-built");
    bad = 0;
    /* A text group stored with explicit lengths (nl:false) whose lengths do
     * not add up to its bytes: 100,000,000 claimed, 4 present. */
    {
        uint8_t bins_lie[] = { 4, 0xFF, 10, 0x00, 0xC2, 0xEB, 0x0B };   /* zigzag 1e8 */
        bad += !crafted("text lengths overrun the text",
                        "{\"columns\":[\"a\"],\"nrows\":2,\"cols\":[{\"kind\":\"text\"}],\"groups\":[],"
                        "\"order\":[],\"bins\":[7],\"smeta\":[{\"n\":2,\"b\":4,\"nl\":false}],\"nlenbins\":1}",
                        bins_lie, sizeof(bins_lie), "abcd", 4);
        uint8_t neg[] = { 4, 1, 1 };                                     /* -1, -1 */
        bad += !crafted("text lengths negative",
                        "{\"columns\":[\"a\"],\"nrows\":2,\"cols\":[{\"kind\":\"text\"}],\"groups\":[],"
                        "\"order\":[],\"bins\":[3],\"smeta\":[{\"n\":2,\"b\":4,\"nl\":false}],\"nlenbins\":1}",
                        neg, sizeof(neg), "abcd", 4);
    }
    /* More length bins claimed than bins exist. */
    bad += !crafted("nlenbins beyond bins",
                    "{\"columns\":[\"a\"],\"nrows\":2,\"cols\":[{\"kind\":\"text\"}],\"groups\":[],"
                    "\"order\":[],\"bins\":[3],\"smeta\":[{\"n\":2,\"b\":4,\"nl\":false}],"
                    "\"nlenbins\":1099511627776}",
                    "\x04\x02\x02", 3, "abcd", 4);
    /* Two nl:false groups and only one length bin. */
    bad += !crafted("more length groups than length bins",
                    "{\"columns\":[\"a\",\"b\"],\"nrows\":1,\"cols\":[{\"kind\":\"text\"},{\"kind\":\"text\"}],"
                    "\"groups\":[],\"order\":[],\"bins\":[2],\"smeta\":[{\"n\":1,\"b\":1,\"nl\":false},"
                    "{\"n\":1,\"b\":1,\"nl\":false}],\"nlenbins\":1}",
                    "\x04\x02", 2, "ab", 2);
    /* Bin sizes whose running total wraps around to fit. */
    bad += !crafted("bin sizes that wrap",
                    "{\"columns\":[\"n\",\"m\"],\"nrows\":3,\"cols\":[{\"kind\":\"num\",\"dec\":0,\"k\":0,\"warm\":[]},"
                    "{\"kind\":\"num\",\"dec\":0,\"k\":0,\"warm\":[]}],"
                    "\"groups\":[],\"order\":[],\"bins\":[4,-4],\"smeta\":[],\"nlenbins\":0}",
                    "\x04\x02\x04\x06", 4, "", 0);
    /* Byte counts of string groups whose running total wraps. */
    bad += !crafted("string group sizes that wrap",
                    "{\"columns\":[\"a\",\"b\"],\"nrows\":3,\"cols\":[{\"kind\":\"text\"},{\"kind\":\"text\"}],"
                    "\"groups\":[],\"order\":[],\"bins\":[],\"smeta\":[{\"n\":3,\"b\":2,\"nl\":true},"
                    "{\"n\":3,\"b\":-2,\"nl\":true}],\"nlenbins\":0}",
                    "", 0, "a\nbc", 4);
    /* Row counts whose products overflow a size_t. */
    bad += !crafted("nrows -1 with a numeric column",
                    "{\"columns\":[\"n\"],\"nrows\":-1,\"cols\":[{\"kind\":\"num\",\"dec\":0,\"k\":0,\"warm\":[]}],"
                    "\"groups\":[],\"order\":[],\"bins\":[4],\"smeta\":[],\"nlenbins\":0}",
                    "\x04\x02\x04\x06", 4, "", 0);
    bad += !crafted("nrows 2^60 with a text column",
                    "{\"columns\":[\"a\"],\"nrows\":1152921504606846976,\"cols\":[{\"kind\":\"text\"}],"
                    "\"groups\":[],\"order\":[],\"bins\":[],\"smeta\":[{\"n\":1,\"b\":1,\"nl\":true}],\"nlenbins\":0}",
                    "", 0, "x", 1);
    bad += !crafted("nrows 2^62 with a 2D group of 4",
                    "{\"columns\":[\"a\",\"b\",\"c\",\"d\"],\"nrows\":4611686018427387904,"
                    "\"cols\":[{\"kind\":\"grp\",\"dec\":0,\"g\":0},{\"kind\":\"grp\",\"dec\":0,\"g\":0},"
                    "{\"kind\":\"grp\",\"dec\":0,\"g\":0},{\"kind\":\"grp\",\"dec\":0,\"g\":0}],"
                    "\"groups\":[[0,1,2,3]],\"order\":[],\"bins\":[1],\"smeta\":[],\"nlenbins\":0}",
                    "\x04", 1, "", 0);
    bad += !crafted("string count 2^60",
                    "{\"columns\":[\"a\"],\"nrows\":1,\"cols\":[{\"kind\":\"text\"}],\"groups\":[],"
                    "\"order\":[],\"bins\":[],\"smeta\":[{\"n\":1152921504606846976,\"b\":3,\"nl\":true}],"
                    "\"nlenbins\":0}",
                    "", 0, "a\nb", 3);
    /* zero columns and a huge row count */
    bad += !crafted("no columns, 2^40 rows",
                    "{\"columns\":[],\"nrows\":1099511627776,\"cols\":[],\"groups\":[],\"order\":[],"
                    "\"bins\":[],\"smeta\":[],\"nlenbins\":0}", "", 0, "", 0);
    /* metadata that is deeply nested JSON: the parser recurses */
    {
        size_t depth = 1000000;
        char *deep = malloc(depth + 1);
        memset(deep, '[', depth);
        deep[depth] = 0;
        bad += !crafted("metadata nested a million deep", deep, "", 0, "", 0);
        free(deep);
    }
    /* metadata that is not an object, or not JSON */
    bad += !crafted("metadata is a list", "[1,2,3]", "", 0, "", 0);
    bad += !crafted("metadata is not JSON", "not json at all", "", 0, "", 0);
    bad += !crafted("metadata is empty", "", "", 0, "", 0);
    printf("   hand-built lies: %d did not survive\n", bad);

    buf_free(&meta); buf_free(&bins); buf_free(&txt);
    buf_free(&blob);
}

/* ------------------------------------------------------ derived columns */

/* id, name, lat, lon, location = "POINT (lon lat)": location is derived from
 * lat and lon, and name is text, so a lie can point a reference at it. */
static void derived_table(Table *t)
{
    TB b;
    tb_start(&b, 5, (const char *const[]){ "id", "name", "lat", "lon", "location" });
    for (int i = 0; i < 400; i++) {
        double la = 41.6 + (i * 7919 % 4001) / 10000.0, lo = -87.9 + (i * 104729 % 3001) / 10000.0;
        tb_cellf(&b, "%d", i);
        tb_cellf(&b, "name %d", i % 13);
        tb_cellf(&b, "%.6f", la);
        tb_cellf(&b, "%.6f", lo);
        tb_cellf(&b, "POINT (%.6f %.4f)", lo, la);
    }
    tb_finish(&b, t);
}

/* Must be refused: survive it in a child first, then decode here. */
static int refused(const char *name, const Buf *arc)
{
    if (!survive("PPZ2", name, arc->data, arc->len)) return 0;
    Table t;
    int rc = ppz_decode(arc->data, arc->len, &t);
    if (rc == 0) table_free(&t);
    return CHECK(rc != 0, "PPZ2 %s: decoded instead of being refused", name);
}

static int derive_lie(const char *name, const Buf *meta,
                      const char *from, const char *to, const Buf *bins, const Buf *txt)
{
    Buf m2, arc;
    if (!CHECK(edit(meta, from, to, &m2), "lie %s: the metadata has no %s", name, from)) return 0;
    rebuild((const char *)m2.data, m2.len, bins, txt, &arc);
    int ok = refused(name, &arc);
    buf_free(&m2); buf_free(&arc);
    return ok;
}

/* columns a, b: b is derived from a; texts are the two cells */
static int derive_crafted(const char *name, const char *a, const char *bcell, int want_ok,
                          const char *want_b)
{
    char meta[512];
    snprintf(meta, sizeof(meta),
             "{\"columns\":[\"a\",\"b\"],\"nrows\":1,\"cols\":[{\"kind\":\"text\"},{\"kind\":\"text\"}],"
             "\"groups\":[],\"order\":[],\"bins\":[],\"smeta\":[{\"n\":1,\"b\":%zu,\"nl\":true},"
             "{\"n\":1,\"b\":%zu,\"nl\":true}],\"nlenbins\":0,\"derive\":[[1,[0]]]}",
             strlen(a), strlen(bcell));
    Buf b, t, arc;
    buf_init(&b); buf_init(&t);
    buf_put(&t, a, strlen(a));
    buf_put(&t, bcell, strlen(bcell));
    rebuild(meta, strlen(meta), &b, &t, &arc);
    int ok;
    if (want_ok) {
        Table out;
        ok = CHECK(ppz_decode(arc.data, arc.len, &out) == 0, "PPZ2 %s: refused", name);
        if (ok) {
            Str c = table_at(&out, 0, 1);
            ok = CHECK(c.n == strlen(want_b) && !memcmp(c.p, want_b, c.n),
                       "PPZ2 %s: got %.*s, want %s", name, (int)c.n, c.p, want_b);
            table_free(&out);
        }
    } else {
        ok = refused(name, &arc);
    }
    buf_free(&b); buf_free(&t); buf_free(&arc);
    return ok;
}

static void derived_lies(void)
{
    Table t;
    derived_table(&t);
    Buf good;
    buf_init(&good);
    CHECK(ppz_encode(&t, &good) == 0, "encode the derived sample");
    mutate_all("PPZ2", &good);
    truncations("PPZ2", &good, &t);

    h_section("lying headers: derived columns");
    Buf meta, bins, txt;
    if (!CHECK(split(&good, &meta, &bins, &txt) == 0, "derived archive does not split")) {
        buf_free(&good); table_free(&t); return;
    }
    const char *D = "\"derive\":[[4,[2,3]]]";
    CHECK(strstr((char *)meta.data, D) != NULL, "derived sample metadata has no %s: %.*s",
          D, (int)meta.len, meta.data);
    int bad = 0;
    struct { const char *name, *to; } L[] = {
        { "derived from itself",     "\"derive\":[[4,[4,3]]]" },
        { "source out of range",     "\"derive\":[[4,[2,9]]]" },
        { "source negative",         "\"derive\":[[4,[-1,3]]]" },
        { "column out of range",     "\"derive\":[[40,[2,3]]]" },
        { "column listed twice",     "\"derive\":[[4,[2,3]],[4,[2]]]" },
        { "source is derived",       "\"derive\":[[4,[2,3]],[3,[0]]]" },
        { "five sources",            "\"derive\":[[4,[2,3,0,1,0]]]" },
        { "no sources",              "\"derive\":[[4,[]]]" },
        { "empty list",              "\"derive\":[]" },
        { "not a list",              "\"derive\":{\"4\":[2,3]}" },
        { "reference past sources",  "\"derive\":[[4,[3]]]" },
        { "source is text",          "\"derive\":[[4,[1,3]]]" },
        { "fractional column",       "\"derive\":[[4.5,[2,3]]]" },
    };
    for (size_t k = 0; k < N_OF(L); k++)
        bad += !derive_lie(L[k].name, &meta, D, L[k].to, &bins, &txt);

    /* references written by hand */
    bad += !derive_crafted("a good exact reference", "5", "x\x01" "0:\x02y", 1, "x5y");
    bad += !derive_crafted("a good rounded reference", "2.355", "\x01" "0:2\x02", 1, "2.36");
    bad += !derive_crafted("unterminated reference", "5", "x\x01" "0:", 0, NULL);
    bad += !derive_crafted("reference to candidate 5", "5", "\x01" "5:\x02", 0, NULL);
    bad += !derive_crafted("reference with no colon", "5", "\x01" "0\x02", 0, NULL);
    bad += !derive_crafted("rounding to 999 places", "2.5", "\x01" "0:999\x02", 0, NULL);
    bad += !derive_crafted("rounding a whole number", "5", "\x01" "0:0\x02", 0, NULL);
    bad += !derive_crafted("rounding to as many places", "2.5", "\x01" "0:1\x02", 0, NULL);
    bad += !derive_crafted("stray end marker", "5", "a\x02", 0, NULL);
    bad += !derive_crafted("source is not a number", "five", "\x01" "0:\x02", 0, NULL);
    {
        char big[200];
        memset(big, '7', 100);
        big[100] = 0;
        bad += !derive_crafted("source longer than DRV_MAX_TOK", big, "\x01" "0:\x02", 0, NULL);
    }
    printf("   derived-column lies: %d did not survive\n", bad);

    buf_free(&meta); buf_free(&bins); buf_free(&txt);
    buf_free(&good);
    table_free(&t);
}

/* A stream index that lies: sizes past the file, a deep header, and so on. */
static void stream_lies(const Buf *good)
{
    h_section("lying stream indexes");
    /* the blocks of the good file, reused behind a new index */
    uint64_t hl = 0;
    for (int i = 0; i < 8; i++) hl = (hl << 8) | good->data[good->len - 8 + i];
    size_t body = good->len - 8 - (size_t)hl;
    const char *idx[] = {
        "{\"columns\":[\"id\",\"kind\",\"val\",\"note\"],\"nrows\":3000,\"rows_per_block\":500,\"blocks\":[99999999]}",
        "{\"columns\":[\"id\",\"kind\",\"val\",\"note\"],\"nrows\":3000,\"rows_per_block\":500,\"blocks\":[-1]}",
        "{\"columns\":[\"id\",\"kind\",\"val\",\"note\"],\"nrows\":-3,\"rows_per_block\":500,\"blocks\":[]}",
        "{\"columns\":[\"x\"],\"nrows\":3000,\"rows_per_block\":500,\"blocks\":[10,10,10]}",
        "{\"columns\":[1,2],\"nrows\":1,\"rows_per_block\":1,\"blocks\":[]}",
        "{\"columns\":[],\"nrows\":18446744073709551615,\"rows_per_block\":1,\"blocks\":[]}",
        "{\"nrows\":1}",
        "[]",
    };
    for (size_t k = 0; k < N_OF(idx) + 1; k++) {
        Buf h, hz, f;
        buf_init(&h); buf_init(&hz); buf_init(&f);
        if (k < N_OF(idx)) buf_put(&h, idx[k], strlen(idx[k]));
        else { buf_need(&h, 1000000); memset(h.data, '[', 1000000); h.len = 1000000; }
        ppz_lzma_compress(h.data, h.len, &hz);
        buf_put(&f, good->data, body);
        buf_put(&f, hz.data, hz.len);
        uint8_t lb[8];
        for (int i = 0; i < 8; i++) lb[i] = (uint8_t)((uint64_t)hz.len >> (56 - 8 * i));
        buf_put(&f, lb, 8);
        char name[48];
        snprintf(name, sizeof(name), k < N_OF(idx) ? "index %zu" : "index nested a million deep", k);
        survive("PPZS", name, f.data, f.len);
        buf_free(&h); buf_free(&hz); buf_free(&f);
    }
}

int main(void)
{
    h_suite = "hostile";
    setvbuf(stdout, NULL, _IOLBF, 0);
    tmpdir_make("hostile");

    Table t;
    sample_table(&t);

    Buf one, good;
    buf_init(&one);
    CHECK(ppz_encode(&t, &one) == 0, "encode the sample");
    mutate_all("PPZ2", &one);
    truncations("PPZ2", &one, &t);

    /* PPZS: stream-compressed in blocks of 500 */
    {
        const char *src = tpath("sample.csv");
        char err[512];
        table_write_any(&t, src, err, sizeof(err));
        const char *arc = tpath("sample.ppzs");
        StreamStats st;
        if (CHECK(ppz_stream_compress(src, arc, 1.0, 500, 1, NULL, NULL, &st, err, sizeof(err)) == 0,
                  "stream the sample: %s", err)) {
            read_bytes(arc, &good);
            mutate_all("PPZS", &good);
            stream_lies(&good);
            buf_free(&good);
        }
    }
    lying_headers();
    derived_lies();

    buf_free(&one);
    table_free(&t);
    tmpdir_remove();
    return h_done();
}
