/* Stata, SPSS and SAS files, read through ReadStat (vendored in readstat/).
 *
 * These formats are not compressed by the table codec. A .dta or .sav holds
 * more than a table -- variable and value labels, display formats, notes,
 * user-defined missing values -- and no table format can carry all of it
 * back, so an archive of one keeps the ORIGINAL BYTES (xz -9e) plus a JSON
 * schema of everything ReadStat found in them. Restoring to the same format
 * gives the file back byte for byte; restoring or converting to CSV, TSV,
 * JSON or JSON Lines is a translation, and the program says what the
 * translation leaves behind (stat_loss_print). So is converting to another
 * stats format (stat_translate, at the end of this file), which keeps far
 * more and reports the rest the same way.
 *
 * Measured before this was built (2026-09-16 and 2026-10-01): behind a reader
 * the codec beat xz on the original by only 6% on NHANES DEMO_J.xpt -- the
 * binary formats store 8 bytes a value and every compressor scores 12-15x
 * on them -- and rebuilding the original bytes from a decoded table through
 * ReadStat's writers is not exact. Original bytes are exact by construction.
 *
 * Mahdi's rule (2026-09-28) is why this is C: "whatever can be just C, move
 * it to C". ReadStat is C, so it is compiled in rather than reached through
 * Python the way Parquet is.
 */

#include "ppz.h"
#include "readstat/readstat.h"

#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void seterr(char *err, size_t cap, const char *fmt, ...)
{
    if (!err || !cap) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
}

/* ------------------------------------------------------------- formats */

static const struct { const char *ext, *fmt, *name; } STAT_EXT[] = {
    { ".dta",      "dta",      "Stata" },
    { ".sav",      "sav",      "SPSS" },
    { ".zsav",     "zsav",     "SPSS (compressed)" },
    { ".por",      "por",      "SPSS portable" },
    { ".sas7bdat", "sas7bdat", "SAS" },
    { ".xpt",      "xpt",      "SAS transport" },
};
#define NSTAT (sizeof(STAT_EXT) / sizeof(STAT_EXT[0]))

static int ends_ci(const char *s, const char *suf)
{
    size_t n = strlen(s), m = strlen(suf);
    if (n < m) return 0;
    for (size_t i = 0; i < m; i++) {
        char a = s[n - m + i];
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (a != suf[i]) return 0;
    }
    return 1;
}

const char *ppz_stat_format(const char *path)
{
    for (size_t i = 0; i < NSTAT; i++)
        if (ends_ci(path, STAT_EXT[i].ext)) return STAT_EXT[i].fmt;
    return NULL;
}

const char *ppz_stat_name(const char *fmt)
{
    for (size_t i = 0; i < NSTAT; i++)
        if (!strcmp(fmt, STAT_EXT[i].fmt)) return STAT_EXT[i].name;
    return NULL;
}

/* ------------------------------------------------- ReadStat from memory */

/* The parser reads through these instead of a file, so an archive is
 * translated without writing its original anywhere first. */
typedef struct { const uint8_t *p; size_t n, pos; } MemIo;

static int mem_open(const char *path, void *io) { (void)path; ((MemIo *)io)->pos = 0; return 0; }
static int mem_close(void *io) { (void)io; return 0; }

static readstat_off_t mem_seek(readstat_off_t off, readstat_io_flags_t whence, void *io)
{
    MemIo *m = io;
    readstat_off_t base = whence == READSTAT_SEEK_SET ? 0
                        : whence == READSTAT_SEEK_CUR ? (readstat_off_t)m->pos
                        : (readstat_off_t)m->n;
    readstat_off_t to = base + off;
    if (to < 0 || (size_t)to > m->n) return -1;
    m->pos = (size_t)to;
    return to;
}

static ssize_t mem_read(void *buf, size_t nb, void *io)
{
    MemIo *m = io;
    size_t k = m->n - m->pos < nb ? m->n - m->pos : nb;
    memcpy(buf, m->p + m->pos, k);
    m->pos += k;
    return (ssize_t)k;
}

static readstat_error_t mem_update(long size, readstat_progress_handler ph, void *user, void *io)
{
    (void)size; (void)ph; (void)user; (void)io;
    return READSTAT_OK;
}

/* ------------------------------------------------------------- reading */

typedef struct {
    char   *name;            /* the label set's name */
    Buf     pairs;           /* JSON: [value,"label"],[...] */
    size_t  n;
} LabelSet;

typedef enum { DT_NONE, DT_DATE, DT_DATETIME, DT_TIME, DT_OTHER } DateKind;

typedef struct {
    const char *fmt;                 /* "dta", "sav", ... */
    size_t      ncols, nrows, rcap;
    char      **names;
    DateKind   *dk;
    double     *dscale;              /* seconds (or ms) per unit, see date_kind */
    int64_t    *depoch;              /* days from 1970-01-01 to the format's day 0 */
    size_t     *off, *len;           /* per cell, into arena */
    Buf         arena;
    Buf         cols;                /* schema: the "columns" array body */
    LabelSet   *sets;
    size_t      nsets;
    Buf         notes;
    size_t      nnotes;
    StatLoss    loss;
    unsigned char *tagged_col;       /* a column held a tagged missing value */
    int         failed;              /* out of memory, or a bad value */
    char        meta[2048];          /* schema: file-level fields, JSON */
    int         nocells;             /* count and check, keep nothing */
    int         bad_text;            /* nocells: a string was not UTF-8 */
    Writer     *sink;                /* rows go out a block at a time */
    const char *sink_path;
    size_t      base;                /* first row still held (sink) */
    char        werr[512];
} Rd;

/* rows held before they go out: about 4M cells, whatever the width */
#define SINK_CELLS ((size_t)4 << 20)
static size_t sink_rows(const Rd *r) { return r->ncols && SINK_CELLS / r->ncols ? SINK_CELLS / r->ncols : 1; }

static int grow_rows(Rd *r, size_t need)
{
    if (need <= r->rcap) return 0;
    size_t cap = r->rcap ? r->rcap : 1024;
    while (cap < need) cap *= 2;
    if (r->ncols && cap > SIZE_MAX / r->ncols / sizeof(size_t)) return -1;
    size_t *o = realloc(r->off, cap * r->ncols * sizeof(size_t));
    if (!o) return -1;
    r->off = o;
    size_t *l = realloc(r->len, cap * r->ncols * sizeof(size_t));
    if (!l) return -1;
    r->len = l;
    memset(r->len + r->rcap * r->ncols, 0, (cap - r->rcap) * r->ncols * sizeof(size_t));
    memset(r->off + r->rcap * r->ncols, 0, (cap - r->rcap) * r->ncols * sizeof(size_t));
    r->rcap = cap;
    return 0;
}

/* The shortest text that reads back as the same double, without an exponent
 * for whole numbers a spreadsheet would otherwise show as 1e+06. */
static size_t fmt_double(char *s, size_t cap, double v)
{
    if (v == floor(v) && fabs(v) < 1e15) return (size_t)snprintf(s, cap, "%.0f", v);
    for (int p = 15; p <= 17; p++) {
        snprintf(s, cap, "%.*g", p, v);
        if (strtod(s, NULL) == v) break;
    }
    return strlen(s);
}

static size_t fmt_float(char *s, size_t cap, float v)
{
    if (v == floorf(v) && fabsf(v) < 1e7f) return (size_t)snprintf(s, cap, "%.0f", (double)v);
    for (int p = 6; p <= 9; p++) {
        snprintf(s, cap, "%.*g", p, (double)v);
        if ((float)strtod(s, NULL) == v) break;
    }
    return strlen(s);
}

/* Days since 1970-01-01 to a calendar date (H. Hinnant's civil_from_days). */
static void civil(int64_t z, int64_t *y, unsigned *m, unsigned *d)
{
    z += 719468;
    int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned)(z - era * 146097);
    unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    unsigned mp = (5 * doy + 2) / 153;
    *d = doy - (153 * mp + 2) / 5 + 1;
    *m = mp < 10 ? mp + 3 : mp - 9;
    *y = (int64_t)yoe + era * 400 + (*m <= 2);
}

/* Which display formats mean a date, a date-time or a time of day, and in
 * what unit. Stata counts days (%td) or milliseconds (%tc) from 1960, SAS
 * days or seconds from 1960, SPSS seconds from 14 October 1582. Anything
 * else date-like (Stata %tw/%tm/%tq/%th/%ty, SPSS QYR/MOYR/WKYR) is left as
 * its number and counted, so the translation report can name it. */
static DateKind date_kind(const char *fmt, const char *f, double *scale, int64_t *epoch)
{
    *scale = 1; *epoch = -3653;                      /* 1960-01-01 */
    if (!f || !*f) return DT_NONE;
    if (!strcmp(fmt, "dta")) {
        if (*f != '%') return DT_NONE;
        f++;
        if (*f == '-') f++;
        if (f[0] == 'd' || (f[0] == 't' && f[1] == 'd')) return DT_DATE;
        if (f[0] == 't' && (f[1] == 'c' || f[1] == 'C')) {
            /* Stata has no time-of-day type: a time is a %tc shown through a
             * display mask with only clock codes in it, like %tcHH:MM:SS */
            *scale = 1000;
            const char *mask = f + 2;
            int clock = *mask && !strpbrk(mask, "CcYyJjNnDd") ? 1 : 0;
            if (clock) for (const char *q = mask; *q; q++)    /* Mon, mon, Month */
                if ((*q == 'M' || *q == 'm') && q[1] == 'o') clock = 0;
            return clock ? DT_TIME : DT_DATETIME;
        }
        if (f[0] == 't' && f[1] && strchr("wmqhy", f[1])) return DT_OTHER;
        return DT_NONE;
    }
    char u[32];
    size_t k = 0;
    for (; *f && k + 1 < sizeof(u); f++) {
        if ((*f >= '0' && *f <= '9') || *f == '.') break;
        u[k++] = (char)(*f >= 'a' && *f <= 'z' ? *f - 'a' + 'A' : *f);
    }
    u[k] = 0;
    int spss = !strcmp(fmt, "sav") || !strcmp(fmt, "zsav") || !strcmp(fmt, "por");
    if (spss) {
        *epoch = -141428;                            /* 1582-10-14 */
        static const char *D[] = { "DATE", "ADATE", "EDATE", "SDATE", "JDATE", NULL };
        for (int i = 0; D[i]; i++) if (!strcmp(u, D[i])) { *scale = 86400; return DT_DATE; }
        if (!strcmp(u, "DATETIME") || !strcmp(u, "YMDHMS")) return DT_DATETIME;
        if (!strcmp(u, "TIME")) return DT_TIME;
        if (!strcmp(u, "QYR") || !strcmp(u, "MOYR") || !strcmp(u, "WKYR") ||
            !strcmp(u, "DTIME") || !strcmp(u, "MTIME") || !strcmp(u, "WKDAY") ||
            !strcmp(u, "MONTH")) return DT_OTHER;
        return DT_NONE;
    }
    static const char *D[] = { "DATE", "DDMMYY", "DDMMYYB", "DDMMYYC", "DDMMYYD",
        "DDMMYYN", "DDMMYYP", "DDMMYYS", "MMDDYY", "MMDDYYB", "MMDDYYC", "MMDDYYD",
        "MMDDYYN", "MMDDYYP", "MMDDYYS", "YYMMDD", "YYMMDDB", "YYMMDDC", "YYMMDDD",
        "YYMMDDN", "YYMMDDP", "YYMMDDS", "E8601DA", "B8601DA", "WEEKDATE",
        "WORDDATE", "WORDDATX", "DAY", "EURDFDE", NULL };
    for (int i = 0; D[i]; i++) if (!strcmp(u, D[i])) return DT_DATE;
    if (!strcmp(u, "DATETIME") || !strcmp(u, "DATEAMPM") || !strcmp(u, "E8601DT") ||
        !strcmp(u, "B8601DT")) return DT_DATETIME;
    if (!strcmp(u, "TIME") || !strcmp(u, "TOD") || !strcmp(u, "HHMM") ||
        !strcmp(u, "E8601TM") || !strcmp(u, "B8601TM") || !strcmp(u, "TIMEAMPM"))
        return DT_TIME;
    if (!strcmp(u, "MONYY") || !strcmp(u, "YYMON") || !strcmp(u, "YYQ") ||
        !strcmp(u, "YEAR") || !strcmp(u, "MONTH") || !strcmp(u, "QTR") ||
        !strcmp(u, "JULIAN") || !strcmp(u, "DTDATE") || !strcmp(u, "DTMONYY") ||
        !strcmp(u, "DTYEAR")) return DT_OTHER;
    return DT_NONE;
}

/* A date, date-time or time as ISO text. Returns 0 -- and the caller writes
 * the number -- when the value is not one this can print exactly. */
static size_t fmt_date(char *s, size_t cap, double v, DateKind k, double scale, int64_t epoch)
{
    if (!isfinite(v)) return 0;
    if (k == DT_DATE) {
        double days = scale == 1 ? v : v / scale;
        if (days != floor(days) || fabs(days) > 1e8) return 0;
        int64_t y; unsigned m, d;
        civil((int64_t)days + epoch, &y, &m, &d);
        if (y < 0 || y > 9999) return 0;
        return (size_t)snprintf(s, cap, "%04lld-%02u-%02u", (long long)y, m, d);
    }
    /* seconds, as whole microseconds; refuse anything finer */
    double secs = v / (scale == 1000 ? 1000.0 : 1.0);
    if (fabs(secs) > 1e13) return 0;
    double whole = floor(secs);
    double us = (secs - whole) * 1e6;
    double usr = floor(us + 0.5);
    if (fabs(us - usr) > 1e-3) return 0;
    int64_t t = (int64_t)whole, frac = (int64_t)usr;
    if (frac == 1000000) { t++; frac = 0; }
    char tail[16] = "";
    if (frac) {
        snprintf(tail, sizeof(tail), ".%06lld", (long long)frac);
        size_t e = strlen(tail);
        while (tail[e - 1] == '0') tail[--e] = 0;
    }
    if (k == DT_TIME) {
        if (t < 0) return 0;
        return (size_t)snprintf(s, cap, "%02lld:%02lld:%02lld%s", (long long)(t / 3600),
                                (long long)(t / 60 % 60), (long long)(t % 60), tail);
    }
    int64_t day = t >= 0 ? t / 86400 : -((-t + 86399) / 86400);
    int64_t sod = t - day * 86400;
    int64_t y; unsigned m, d;
    civil(day + epoch, &y, &m, &d);
    if (y < 0 || y > 9999) return 0;
    return (size_t)snprintf(s, cap, "%04lld-%02u-%02u %02lld:%02lld:%02lld%s",
                            (long long)y, m, d, (long long)(sod / 3600),
                            (long long)(sod / 60 % 60), (long long)(sod % 60), tail);
}

/* A value as schema JSON: a number, ".a" for a tagged missing, or a string. */
static void json_value(Buf *b, readstat_value_t v)
{
    char s[64];
    if (readstat_value_is_tagged_missing(v)) {
        s[0] = '.'; s[1] = readstat_value_tag(v); s[2] = 0;
        ppz_json_str(b, s, 2);
        return;
    }
    if (readstat_value_is_system_missing(v)) { buf_put(b, "null", 4); return; }
    switch (readstat_value_type(v)) {
    case READSTAT_TYPE_STRING: {
        const char *x = readstat_string_value(v);
        ppz_json_str(b, x ? x : "", x ? strlen(x) : 0);
        return;
    }
    case READSTAT_TYPE_INT8:  buf_put(b, s, (size_t)snprintf(s, sizeof(s), "%d", readstat_int8_value(v))); return;
    case READSTAT_TYPE_INT16: buf_put(b, s, (size_t)snprintf(s, sizeof(s), "%d", readstat_int16_value(v))); return;
    case READSTAT_TYPE_INT32: buf_put(b, s, (size_t)snprintf(s, sizeof(s), "%d", readstat_int32_value(v))); return;
    case READSTAT_TYPE_FLOAT: buf_put(b, s, fmt_float(s, sizeof(s), readstat_float_value(v))); return;
    default: {
        double d = readstat_double_value(v);
        if (!isfinite(d)) { buf_put(b, "null", 4); return; }
        buf_put(b, s, fmt_double(s, sizeof(s), d));
    }
    }
}

/* Stata's own type names. ReadStat reports a Stata string one byte wider
 * than it is stored (room for a terminator) and a strL as a string of width
 * 0, so those two are undone here; the other formats report widths as is. */
static const char *type_name(const char *fmt, readstat_type_t t, size_t width,
                             char *out, size_t cap)
{
    if (!strcmp(fmt, "dta") && t == READSTAT_TYPE_STRING) {
        if (!width) return "strL";
        width--;
    }
    switch (t) {
    case READSTAT_TYPE_INT8:   return "int8";
    case READSTAT_TYPE_INT16:  return "int16";
    case READSTAT_TYPE_INT32:  return "int32";
    case READSTAT_TYPE_FLOAT:  return "float";
    case READSTAT_TYPE_DOUBLE: return "double";
    case READSTAT_TYPE_STRING_REF: return "strL";
    default: snprintf(out, cap, "str%zu", width); return out;
    }
}

static int on_metadata(readstat_metadata_t *m, void *ctx)
{
    Rd *r = ctx;
    int64_t nv = readstat_get_var_count(m);
    if (nv < 0 || nv > 1000000) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
    r->ncols = (size_t)nv;
    r->names = calloc(r->ncols ? r->ncols : 1, sizeof(char *));
    r->dk = calloc(r->ncols ? r->ncols : 1, sizeof(DateKind));
    r->dscale = calloc(r->ncols ? r->ncols : 1, sizeof(double));
    r->depoch = calloc(r->ncols ? r->ncols : 1, sizeof(int64_t));
    r->tagged_col = calloc(r->ncols ? r->ncols : 1, 1);
    if (!r->names || !r->dk || !r->dscale || !r->depoch || !r->tagged_col) {
        r->failed = 1; return READSTAT_HANDLER_ABORT;
    }
    int64_t rows = readstat_get_row_count(m);
    if (r->sink && rows > (int64_t)sink_rows(r)) rows = (int64_t)sink_rows(r);
    if (rows > 0 && !r->nocells && grow_rows(r, (size_t)rows < ((size_t)1 << 24) ? (size_t)rows : ((size_t)1 << 24))) {
        r->failed = 1; return READSTAT_HANDLER_ABORT;
    }

    Buf b;
    buf_init(&b);
    char n[64];
    buf_put(&b, "\"version\":", 10);
    buf_put(&b, n, (size_t)snprintf(n, sizeof(n), "%d", readstat_get_file_format_version(m)));
    const char *enc = readstat_get_file_encoding(m);
    if (enc && *enc) { buf_put(&b, ",\"encoding\":", 12); ppz_json_str(&b, enc, strlen(enc)); }
    const char *lab = readstat_get_file_label(m);
    if (lab && *lab) {
        buf_put(&b, ",\"label\":", 9); ppz_json_str(&b, lab, strlen(lab));
        r->loss.file_label = 1;
    }
    const char *tn = readstat_get_table_name(m);
    if (tn && *tn) { buf_put(&b, ",\"table\":", 9); ppz_json_str(&b, tn, strlen(tn)); }
    time_t ct = readstat_get_creation_time(m), mt = readstat_get_modified_time(m);
    if (ct > 0) { buf_put(&b, ",\"created\":", 11); buf_put(&b, n, (size_t)snprintf(n, sizeof(n), "%lld", (long long)ct)); }
    if (mt > 0) { buf_put(&b, ",\"modified\":", 12); buf_put(&b, n, (size_t)snprintf(n, sizeof(n), "%lld", (long long)mt)); }
    if (b.len < sizeof(r->meta)) { memcpy(r->meta, b.data, b.len); r->meta[b.len] = 0; }
    buf_free(&b);
    return READSTAT_HANDLER_OK;
}

static int on_note(int i, const char *note, void *ctx)
{
    Rd *r = ctx;
    (void)i;
    if (r->nnotes++) buf_putc(&r->notes, ',');
    ppz_json_str(&r->notes, note ? note : "", note ? strlen(note) : 0);
    r->loss.notes++;
    return READSTAT_HANDLER_OK;
}

static int on_variable(int index, readstat_variable_t *v, const char *val_labels, void *ctx)
{
    Rd *r = ctx;
    if (index < 0 || (size_t)index >= r->ncols) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
    const char *name = readstat_variable_get_name(v);
    r->names[index] = strdup(name ? name : "");
    if (!r->names[index]) { r->failed = 1; return READSTAT_HANDLER_ABORT; }

    Buf *b = &r->cols;
    if (index) buf_putc(b, ',');
    char tb[32], n[32];
    buf_put(b, "{\"name\":", 8);
    ppz_json_str(b, r->names[index], strlen(r->names[index]));
    const char *ty = type_name(r->fmt, readstat_variable_get_type(v),
                               readstat_variable_get_storage_width(v), tb, sizeof(tb));
    buf_put(b, ",\"type\":", 8);
    ppz_json_str(b, ty, strlen(ty));
    const char *lab = readstat_variable_get_label(v);
    if (lab && *lab) {
        buf_put(b, ",\"label\":", 9); ppz_json_str(b, lab, strlen(lab));
        r->loss.var_labels++;
    }
    const char *fmt = readstat_variable_get_format(v);
    if (fmt && *fmt) { buf_put(b, ",\"format\":", 10); ppz_json_str(b, fmt, strlen(fmt)); }
    if (val_labels && *val_labels) {
        buf_put(b, ",\"labels\":", 10); ppz_json_str(b, val_labels, strlen(val_labels));
        r->loss.value_labels++;
    }
    int dw = readstat_variable_get_display_width(v);
    if (dw > 0) { buf_put(b, ",\"display_width\":", 17); buf_put(b, n, (size_t)snprintf(n, sizeof(n), "%d", dw)); }
    static const char *MEAS[] = { NULL, "nominal", "ordinal", "scale" };
    readstat_measure_t me = readstat_variable_get_measure(v);
    if (me >= 1 && me <= 3) {
        buf_put(b, ",\"measure\":", 11); ppz_json_str(b, MEAS[me], strlen(MEAS[me]));
        r->loss.measures++;
    }
    static const char *ALIGN[] = { NULL, "left", "center", "right" };
    readstat_alignment_t al = readstat_variable_get_alignment(v);
    if (al >= 1 && al <= 3) { buf_put(b, ",\"align\":", 9); ppz_json_str(b, ALIGN[al], strlen(ALIGN[al])); }
    int nm = readstat_variable_get_missing_ranges_count(v);
    if (nm > 0) {
        buf_put(b, ",\"missing\":[", 12);
        for (int i = 0; i < nm; i++) {
            if (i) buf_putc(b, ',');
            buf_putc(b, '[');
            json_value(b, readstat_variable_get_missing_range_lo(v, i));
            buf_putc(b, ',');
            json_value(b, readstat_variable_get_missing_range_hi(v, i));
            buf_putc(b, ']');
        }
        buf_putc(b, ']');
        r->loss.user_missing++;
    }
    buf_putc(b, '}');

    if (readstat_variable_get_type_class(v) == READSTAT_TYPE_CLASS_NUMERIC) {
        r->dk[index] = date_kind(r->fmt, fmt, &r->dscale[index], &r->depoch[index]);
        if (r->dk[index] == DT_OTHER) r->loss.other_dates++;
        else if (r->dk[index] != DT_NONE) r->loss.dates++;
    }
    return READSTAT_HANDLER_OK;
}

static int valid_utf8(const char *p, size_t n);

/* The rows held so far, out to the sink, and forgotten. Strict UTF-8, as
 * every reader: a block whose text did not convert stops the whole run. */
static int flush_rows(Rd *r)
{
    size_t rows = r->nrows - r->base;
    if (!valid_utf8((const char *)r->arena.data, r->arena.len)) { r->failed = 2; return -1; }
    if (r->sink == (Writer *)1) {
        for (size_t j = 0; j < r->ncols; j++)
            if (!r->names[j] || !valid_utf8(r->names[j], strlen(r->names[j]))) { r->failed = 2; return -1; }
        r->sink = writer_open(r->sink_path, r->names, r->ncols, r->werr, sizeof(r->werr));
        if (!r->sink) { r->failed = 3; return -1; }
    }
    Table t;
    table_init(&t);
    t.ncols = r->ncols;
    t.nrows = rows;
    t.names = r->names;
    t.cells = malloc((rows * r->ncols ? rows * r->ncols : 1) * sizeof(Str));
    if (!t.cells) { r->failed = 3; snprintf(r->werr, sizeof(r->werr), "out of memory"); return -1; }
    for (size_t k = 0; k < rows * r->ncols; k++)
        t.cells[k] = (Str){ r->len[k] ? (const char *)r->arena.data + r->off[k] : "", r->len[k] };
    int bad = writer_rows(r->sink, &t);
    free(t.cells);
    if (bad) { r->failed = 3; snprintf(r->werr, sizeof(r->werr), "cannot write %s", r->sink_path); return -1; }
    memset(r->len, 0, rows * r->ncols * sizeof(size_t));
    r->arena.len = 0;
    r->base = r->nrows;
    return 0;
}

static int on_value(int obs, readstat_variable_t *var, readstat_value_t v, void *ctx)
{
    Rd *r = ctx;
    int j = readstat_variable_get_index(var);
    if (obs < 0 || j < 0 || (size_t)j >= r->ncols || (size_t)obs < r->base) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
    if (r->nocells) {
        if ((size_t)obs >= r->nrows) r->nrows = (size_t)obs + 1;
        if (readstat_value_is_tagged_missing(v)) { r->loss.tagged++; r->tagged_col[j] = 1; }
        else if (readstat_value_type_class(v) == READSTAT_TYPE_CLASS_STRING &&
                 !readstat_value_is_system_missing(v)) {
            const char *p = readstat_string_value(v);
            if (p && !valid_utf8(p, strlen(p))) r->bad_text = 1;
        }
        return READSTAT_HANDLER_OK;
    }
    if (r->sink && (size_t)obs >= r->nrows && r->nrows - r->base >= sink_rows(r) && flush_rows(r))
        return READSTAT_HANDLER_ABORT;
    if ((size_t)obs >= r->nrows) {
        if (grow_rows(r, (size_t)obs + 1 - r->base)) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
        r->nrows = (size_t)obs + 1;
    }
    size_t k = ((size_t)obs - r->base) * r->ncols + (size_t)j;
    char s[96];
    size_t n = 0;
    const char *p = s;
    if (readstat_value_is_tagged_missing(v)) {
        s[0] = '.'; s[1] = readstat_value_tag(v); n = 2;
        r->loss.tagged++;
        r->tagged_col[j] = 1;
    } else if (readstat_value_is_system_missing(v)) {
        n = 0;
    } else if (readstat_value_type(v) == READSTAT_TYPE_STRING ||
               readstat_value_type(v) == READSTAT_TYPE_STRING_REF) {
        p = readstat_string_value(v);
        n = p ? strlen(p) : 0;
    } else {
        double d = readstat_double_value(v);       /* exact for every int type */
        if (r->dk[j] == DT_DATE || r->dk[j] == DT_DATETIME || r->dk[j] == DT_TIME)
            n = fmt_date(s, sizeof(s), d, r->dk[j], r->dscale[j], r->depoch[j]);
        if (!n) {
            if (readstat_value_type(v) == READSTAT_TYPE_FLOAT)
                n = fmt_float(s, sizeof(s), readstat_float_value(v));
            else if (!isfinite(d)) n = 0;
            else n = fmt_double(s, sizeof(s), d);
        }
    }
    r->off[k] = r->arena.len;
    r->len[k] = n;
    if (n) buf_put(&r->arena, p, n);
    return READSTAT_HANDLER_OK;
}

static int on_value_label(const char *set, readstat_value_t v, const char *label, void *ctx)
{
    Rd *r = ctx;
    LabelSet *ls = NULL;
    for (size_t i = r->nsets; i-- > 0; )
        if (!strcmp(r->sets[i].name, set)) { ls = &r->sets[i]; break; }
    if (!ls) {
        LabelSet *ns = realloc(r->sets, (r->nsets + 1) * sizeof(LabelSet));
        if (!ns) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
        r->sets = ns;
        ls = &r->sets[r->nsets++];
        memset(ls, 0, sizeof(*ls));
        buf_init(&ls->pairs);
        ls->name = strdup(set);
        if (!ls->name) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
    }
    if (ls->n++) buf_putc(&ls->pairs, ',');
    buf_putc(&ls->pairs, '[');
    json_value(&ls->pairs, v);
    buf_putc(&ls->pairs, ',');
    ppz_json_str(&ls->pairs, label ? label : "", label ? strlen(label) : 0);
    buf_putc(&ls->pairs, ']');
    return READSTAT_HANDLER_OK;
}

static char g_rs_err[512];
static void on_error(const char *msg, void *ctx)
{
    (void)ctx;
    snprintf(g_rs_err, sizeof(g_rs_err), "%s", msg);
    size_t n = strlen(g_rs_err);
    while (n && (g_rs_err[n - 1] == '\n' || g_rs_err[n - 1] == ' ')) g_rs_err[--n] = 0;
}

static int valid_utf8(const char *p, size_t n)
{
    const unsigned char *s = (const unsigned char *)p;
    for (size_t i = 0; i < n; ) {
        unsigned c = s[i];
        size_t k = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
        if (!k || i + k > n) return 0;
        for (size_t q = 1; q < k; q++) if ((s[i + q] & 0xC0) != 0x80) return 0;
        i += k;
    }
    return 1;
}

static void rd_free(Rd *r)
{
    if (r->names) for (size_t j = 0; j < r->ncols; j++) free(r->names[j]);
    free(r->names); free(r->dk); free(r->dscale); free(r->depoch); free(r->tagged_col);
    free(r->off); free(r->len);
    buf_free(&r->arena); buf_free(&r->cols); buf_free(&r->notes);
    for (size_t i = 0; i < r->nsets; i++) { free(r->sets[i].name); buf_free(&r->sets[i].pairs); }
    free(r->sets);
}

/* Run ReadStat over data[0..n) with the handlers already set on p (which
 * this frees): reading from memory, in the named encoding, errors into
 * g_rs_err. The one place a format name picks a parser. */
static readstat_error_t rs_run(readstat_parser_t *p, const uint8_t *data, size_t n, const char *fmt,
                               const char *encoding, void *ctx)
{
    MemIo io = { data, n, 0 };
    readstat_set_open_handler(p, mem_open);
    readstat_set_close_handler(p, mem_close);
    readstat_set_seek_handler(p, mem_seek);
    readstat_set_read_handler(p, mem_read);
    readstat_set_update_handler(p, mem_update);
    readstat_set_io_ctx(p, &io);
    readstat_set_error_handler(p, on_error);
    if (encoding) readstat_set_file_character_encoding(p, encoding);
    g_rs_err[0] = 0;
    readstat_error_t e;
    if (!strcmp(fmt, "dta"))                               e = readstat_parse_dta(p, "", ctx);
    else if (!strcmp(fmt, "sav") || !strcmp(fmt, "zsav"))  e = readstat_parse_sav(p, "", ctx);
    else if (!strcmp(fmt, "por"))                          e = readstat_parse_por(p, "", ctx);
    else if (!strcmp(fmt, "sas7bdat"))                     e = readstat_parse_sas7bdat(p, "", ctx);
    else if (!strcmp(fmt, "xpt"))                          e = readstat_parse_xport(p, "", ctx);
    else e = READSTAT_ERROR_UNSUPPORTED_FILE_FORMAT_VERSION;
    readstat_parser_free(p);
    return e;
}

static const char ENC_MSG[] = "this %s file's text is not in the encoding it declares.\n"
                              "Name the right one, e.g. --encoding windows-1252";

/* Parse with the handlers above. mode 0: keep every cell (stat_read);
 * 1: keep nothing, only count and check (stat_scan); 2: send the rows to
 * `out` a block at a time (stat_stream). */
static int stat_parse(Rd *r, const uint8_t *data, size_t n, const char *fmt, const char *encoding,
                      int mode, const char *out, char *err, size_t cap)
{
    memset(r, 0, sizeof(*r));
    r->fmt = fmt;
    r->nocells = mode == 1;
    if (mode == 2) { r->sink = (Writer *)1; r->sink_path = out; }
    buf_init(&r->arena); buf_init(&r->cols); buf_init(&r->notes);
    readstat_parser_t *p = readstat_parser_init();
    if (!p) { seterr(err, cap, "out of memory"); return -1; }
    readstat_set_metadata_handler(p, on_metadata);
    readstat_set_note_handler(p, on_note);
    readstat_set_variable_handler(p, on_variable);
    readstat_set_value_handler(p, on_value);
    readstat_set_value_label_handler(p, on_value_label);
    readstat_error_t e = rs_run(p, data, n, fmt, encoding, r);

    const char *what = ppz_stat_name(fmt);
    if (r->failed == 2) { seterr(err, cap, ENC_MSG, what); goto fail; }
    if (r->failed == 3) { seterr(err, cap, "%s", r->werr); goto fail; }
    if (e != READSTAT_OK || r->failed || !r->names) {
        if (r->failed && e == READSTAT_OK) seterr(err, cap, "out of memory reading the %s file", what);
        else seterr(err, cap, "cannot read this %s file: %s%s%s", what,
                    readstat_error_message(e), g_rs_err[0] ? " -- " : "", g_rs_err);
        goto fail;
    }
    for (size_t j = 0; j < r->ncols; j++)
        if (!r->names[j]) { seterr(err, cap, "cannot read this %s file: column %zu has no name", what, j + 1); goto fail; }

    /* strict UTF-8 out, as every other reader: a file whose strings did not
     * convert is refused, not repaired */
    int bad = 0;
    for (size_t j = 0; j < r->ncols && !bad; j++)
        bad = !valid_utf8(r->names[j], strlen(r->names[j]));
    if (!bad) bad = r->nocells ? r->bad_text : !valid_utf8((const char *)r->arena.data, r->arena.len);
    if (bad) { seterr(err, cap, ENC_MSG, what); goto fail; }

    for (size_t j = 0; j < r->ncols; j++) if (r->tagged_col[j]) r->loss.tagged_cols++;
    return 0;
fail:
    if (r->sink && r->sink != (Writer *)1) writer_close(r->sink, 0, NULL, 0);
    r->sink = NULL;
    rd_free(r);
    return -1;
}

/* The schema JSON for a parsed file. */
static void schema_put(const Rd *r, const char *fmt, Buf *schema)
{
    buf_init(schema);
    char nb[32];
    buf_put(schema, "{\"format\":", 10);
    ppz_json_str(schema, fmt, strlen(fmt));
    buf_putc(schema, ',');
    buf_put(schema, r->meta, strlen(r->meta));
    buf_put(schema, ",\"rows\":", 8);
    buf_put(schema, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", r->nrows));
    buf_put(schema, ",\"columns\":[", 12);
    buf_put(schema, r->cols.data, r->cols.len);
    buf_put(schema, "],\"label_sets\":{", 16);
    for (size_t i = 0; i < r->nsets; i++) {
        if (i) buf_putc(schema, ',');
        ppz_json_str(schema, r->sets[i].name, strlen(r->sets[i].name));
        buf_put(schema, ":[", 2);
        buf_put(schema, r->sets[i].pairs.data, r->sets[i].pairs.len);
        buf_putc(schema, ']');
    }
    buf_put(schema, "},\"notes\":[", 11);
    buf_put(schema, r->notes.data, r->notes.len);
    buf_put(schema, "]}", 2);
}

int stat_read(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
              Table *t, Buf *schema, StatLoss *loss, char *err, size_t cap)
{
    table_init(t);
    Rd r;
    if (stat_parse(&r, data, n, fmt, encoding, 0, NULL, err, cap)) return -1;
    t->ncols = r.ncols;
    t->nrows = r.nrows;
    t->names = r.names;
    r.names = NULL;
    t->arena = r.arena;
    buf_init(&r.arena);
    t->cells = malloc((r.nrows * r.ncols ? r.nrows * r.ncols : 1) * sizeof(Str));
    if (!t->cells) { table_free(t); rd_free(&r); seterr(err, cap, "out of memory"); return -1; }
    for (size_t k = 0; k < r.nrows * r.ncols; k++)
        t->cells[k] = (Str){ r.len[k] ? (const char *)t->arena.data + r.off[k] : "", r.len[k] };
    if (schema) schema_put(&r, fmt, schema);
    if (loss) *loss = r.loss;
    rd_free(&r);
    return 0;
}

int stat_scan(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
              Buf *schema, StatLoss *loss, size_t *rows, size_t *cols, char *err, size_t cap)
{
    Rd r;
    if (stat_parse(&r, data, n, fmt, encoding, 1, NULL, err, cap)) return -1;
    if (schema) schema_put(&r, fmt, schema);
    if (loss) *loss = r.loss;
    if (rows) *rows = r.nrows;
    if (cols) *cols = r.ncols;
    rd_free(&r);
    return 0;
}

int stat_stream(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
                const char *out, StatLoss *loss, size_t *rows, size_t *cols, char *err, size_t cap)
{
    Rd r;
    if (stat_parse(&r, data, n, fmt, encoding, 2, out, err, cap)) return -1;
    int rc = flush_rows(&r);                  /* the last rows; opens the writer if none did */
    if (rc && r.failed == 2) seterr(err, cap, ENC_MSG, ppz_stat_name(fmt));
    else if (rc) seterr(err, cap, "%s", r.werr);
    if (r.sink && r.sink != (Writer *)1 && writer_close(r.sink, rc == 0, err, cap)) rc = -1;
    if (!rc) {
        if (loss) *loss = r.loss;
        if (rows) *rows = r.nrows;
        if (cols) *cols = r.ncols;
    }
    rd_free(&r);
    return rc;
}

/* What a CSV/TSV/JSON copy of this file cannot hold. Kept in the archive
 * either way: the original bytes are there, and so is the schema. */
void stat_loss_print(FILE *f, const StatLoss *l, const char *src_fmt, const char *dst)
{
    const char *what = ppz_stat_name(src_fmt);
    int any = l->var_labels || l->value_labels || l->notes || l->file_label ||
              l->user_missing || l->tagged || l->other_dates || l->measures;
    fprintf(f, "%s is a translation of the %s file:\n", dst, what);
    fprintf(f, "  column types and widths are not kept (text has none)\n");
    if (l->dates) fprintf(f, "  %zu date/time column%s written as dates (YYYY-MM-DD hh:mm:ss)\n",
                          l->dates, l->dates == 1 ? "" : "s");
    if (l->other_dates) fprintf(f, "  %zu column%s with a week/month/quarter/year format "
                                "written as plain numbers\n", l->other_dates, l->other_dates == 1 ? "" : "s");
    if (l->value_labels) fprintf(f, "  value labels on %zu column%s: the codes are written, "
                                 "not the labels\n", l->value_labels, l->value_labels == 1 ? "" : "s");
    if (l->var_labels) fprintf(f, "  variable labels on %zu column%s dropped\n",
                               l->var_labels, l->var_labels == 1 ? "" : "s");
    if (l->user_missing) fprintf(f, "  user-defined missing values on %zu column%s: written as "
                                 "their numbers\n", l->user_missing, l->user_missing == 1 ? "" : "s");
    if (l->tagged) fprintf(f, "  %zu tagged missing value%s (.a to .z) written as text, in %zu "
                           "column%s\n", l->tagged, l->tagged == 1 ? "" : "s",
                           l->tagged_cols, l->tagged_cols == 1 ? "" : "s");
    if (l->measures) fprintf(f, "  measure levels (nominal/ordinal/scale) dropped\n");
    if (l->notes) fprintf(f, "  %zu note%s dropped\n", l->notes, l->notes == 1 ? "" : "s");
    if (l->file_label) fprintf(f, "  the file label dropped\n");
    if (any) fprintf(f, "  (all of it stays in the archive: restore to .%s for the original)\n", src_fmt);
}

/* ------------------------------------------------------------- archives */

/* PPZ2, with metadata {"original":{"format":F,"bytes":N[,"encoding":E]
 * [,"layout":{...}]},"schema":{...}}, the original file (or a permutation
 * of it, the layout) as the binary stream and an empty text stream. A build
 * from before this refuses such an archive: it has no "columns". */

/* ------------------------------------------- Stata data, column by column */

/* A Stata 117-119 file stores its observations as fixed-width rows between
 * <data> and </data>. Rewritten column by column -- every row's first
 * field, then every row's second -- the same bytes compress far better,
 * because a column's values sit next to values like themselves. Measured
 * 2026-10-02 on 12 public .dta files (Stata Press, POE, NLSW): -28.0% in
 * total against xz -9e on the original, -29.8% nhanes2, -29.3% nlswork;
 * 11 of 12 smaller, citytemp +3.0%. Splitting numbers further into byte
 * planes was -13% (worse), and xz's lc/lp/pb moved nothing. SAS transport
 * files were measured the same way and gain only -6.6%, one of seven
 * getting larger, so they are stored as they are. It is a permutation of
 * bytes, so the original comes back exactly; compress checks that it does. */

static void put_s(Buf *b, const char *s) { buf_put(b, s, strlen(s)); }

static size_t dta_field_width(const char *type)
{
    if (!strcmp(type, "int8")) return 1;
    if (!strcmp(type, "int16")) return 2;
    if (!strcmp(type, "int32") || !strcmp(type, "float")) return 4;
    if (!strcmp(type, "double") || !strcmp(type, "strL")) return 8;
    if (!strncmp(type, "str", 3)) {
        long w = strtol(type + 3, NULL, 10);
        return w > 0 && w <= 2045 ? (size_t)w : 0;
    }
    return 0;
}

#define DTA_MAX_FIELDS 32767

/* Where the rows start and how wide each field is, or 0 if this file's
 * layout is not the one expected -- it is then stored as it is. */
static size_t dta_layout(const uint8_t *d, size_t n, const Js *sc, size_t *rows,
                         size_t **widths, size_t *nw)
{
    *widths = NULL;
    long v = js_int(js_get(sc, "version"), 0);
    const Js *cols = js_get(sc, "columns");
    int64_t nr = js_i64(js_get(sc, "rows"), -1);
    if (v < 117 || v > 119 || !cols || cols->kind != JS_ARR || !cols->count ||
        cols->count > DTA_MAX_FIELDS || nr <= 0)
        return 0;
    static const char tag[] = "</characteristics><data>";
    size_t tl = sizeof(tag) - 1, at = 0;
    for (size_t i = 0; i + tl <= n; i++)
        if (d[i] == '<' && !memcmp(d + i, tag, tl)) { at = i + tl; break; }
    if (!at) return 0;
    size_t *w = malloc(cols->count * sizeof(size_t)), rw = 0;
    if (!w) return 0;
    for (size_t j = 0; j < cols->count; j++) {
        const Js *ty = js_get(&cols->items[j], "type");
        w[j] = ty && ty->kind == JS_STR ? dta_field_width(ty->str) : 0;
        if (!w[j]) { free(w); return 0; }
        rw += w[j];
    }
    if ((size_t)nr > (n - at) / rw || n - at - (size_t)nr * rw < 7 ||
        memcmp(d + at + (size_t)nr * rw, "</data>", 7)) {
        free(w);
        return 0;
    }
    *rows = (size_t)nr;
    *widths = w;
    *nw = cols->count;
    return at;
}

/* Where each byte of the stored stream lives in the original file. The
 * stream is the file with its rows region rewritten column by column: a
 * stored position inside that region belongs to column j, row i, byte b of
 * the field, which sits at at + i*rw + off[j] + b in the original. Every
 * reader and writer of these archives goes through lay_map, a piece at a
 * time, so a file bigger than memory never has to be held whole.
 *
 * Column by column over the WHOLE region reads it once per column -- fine
 * from memory, one disk pass per column for a file bigger than it. So a
 * region over 1 GB is rewritten in blocks of rows (about 64 MB each), every
 * block column by column on its own; a block fits in memory and is read
 * once. That is a different layout kind ("dta-column-blocks"), which builds
 * from before it refuse rather than misread. The limits are constants, not
 * the machine's memory, so the same file always gives the same archive. */
uint64_t ppz_lay_whole_max = (uint64_t)1 << 30;     /* variables only so the tests */
uint64_t ppz_lay_block_bytes = (uint64_t)64 << 20;  /* can reach blocks on small files */

typedef struct {
    uint64_t  at, end, rows, rw;
    uint64_t  block;                 /* rows per block; == rows when whole */
    size_t    nw;
    size_t   *w, *off;
} Lay;

static void lay_free(Lay *L) { if (L) { free(L->w); free(L->off); memset(L, 0, sizeof(*L)); } }

static int lay_make(Lay *L, uint64_t at, uint64_t rows, uint64_t block, const size_t *w, size_t nw,
                    uint64_t total)
{
    memset(L, 0, sizeof(*L));
    if (!nw || nw > DTA_MAX_FIELDS) return -1;
    L->w = malloc(nw * sizeof(size_t));
    L->off = malloc(nw * sizeof(size_t));
    if (!L->w || !L->off) { lay_free(L); return -1; }
    uint64_t rw = 0;
    for (size_t j = 0; j < nw; j++) {
        if (w[j] < 1 || w[j] > 2045) { lay_free(L); return -1; }
        L->w[j] = w[j]; L->off[j] = (size_t)rw; rw += w[j];
    }
    if (at > total || rows > (total - at) / rw || !block || block > rows + !rows) { lay_free(L); return -1; }
    L->at = at; L->rows = rows; L->rw = rw; L->nw = nw;
    L->block = block;
    L->end = at + rows * rw;
    return 0;
}

/* mode 0: buf = stored bytes [p, p+len), gathered from the original `src`;
 * 1: scatter buf into the original at `dst`; 2: compare buf with `src`,
 * -1 on the first difference. L NULL: the stream is the file as it is. */
static int lay_map(const Lay *L, uint64_t p, uint8_t *buf, size_t len, const uint8_t *src, uint8_t *dst, int mode)
{
    size_t k = 0;
    while (k < len) {
        uint64_t q = p + k;
        if (!L || q < L->at || q >= L->end) {
            size_t run = len - k;
            if (L && q < L->at && L->at - q < run) run = (size_t)(L->at - q);
            if (mode == 0) memcpy(buf + k, src + q, run);
            else if (mode == 1) memcpy(dst + q, buf + k, run);
            else if (memcmp(buf + k, src + q, run)) return -1;
            k += run;
            continue;
        }
        /* which block, which column of it, which row and byte */
        uint64_t d = q - L->at, bspan = L->block * L->rw;
        uint64_t b = d / bspan, in = d - b * bspan;
        uint64_t nb = L->rows - b * L->block < L->block ? L->rows - b * L->block : L->block;
        size_t lo = 0, hi = L->nw;              /* last j with nb*off[j] <= in */
        while (hi - lo > 1) { size_t m = (lo + hi) / 2; if (nb * L->off[m] <= in) lo = m; else hi = m; }
        size_t j = lo, w = L->w[j];
        uint64_t idx = in - nb * L->off[j], i = idx / w;
        size_t bb = (size_t)(idx % w);
        uint64_t base = L->at + b * bspan + L->off[j];
        /* then field by field down the column, stepping a row at a time */
        while (k < len && i < nb) {
            size_t run = w - bb < len - k ? w - bb : len - k;
            uint64_t o = base + i * L->rw + bb;
            if (mode == 0) memcpy(buf + k, src + o, run);
            else if (mode == 1) memcpy(dst + o, buf + k, run);
            else if (memcmp(buf + k, src + o, run)) return -1;
            k += run;
            bb = 0;
            i++;
        }
    }
    return 0;
}

/* The metadata for an archive of data[0..n): {"original":{...},"schema":...},
 * and the Stata column layout when the file has the expected one. */
static void original_meta(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
                          const Buf *schema, Buf *meta, Lay *L, int *has_lay)
{
    char nb[32];
    *has_lay = 0;
    buf_init(meta);
    put_s(meta, "{" PPZ_V_FIELD "\"original\":{\"format\":");
    ppz_json_str(meta, fmt, strlen(fmt));
    put_s(meta, ",\"bytes\":");
    buf_put(meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", n));
    if (encoding) {
        put_s(meta, ",\"encoding\":");
        ppz_json_str(meta, encoding, strlen(encoding));
    }
    if (!strcmp(fmt, "dta")) {
        Js *sc = js_parse((const char *)schema->data, schema->len);
        size_t rows = 0, nw = 0, *w = NULL;
        size_t at = sc ? dta_layout(data, n, sc, &rows, &w, &nw) : 0;
        js_free(sc);
        uint64_t rw = 0;
        for (size_t j = 0; j < nw; j++) rw += w[j];
        uint64_t block = rows;
        if (rw && (uint64_t)rows * rw > ppz_lay_whole_max)
            block = ppz_lay_block_bytes / rw ? ppz_lay_block_bytes / rw : 1;
        if (at && !lay_make(L, at, rows, block, w, nw, n)) {
            *has_lay = 1;
            put_s(meta, block == rows ? ",\"layout\":{\"kind\":\"dta-columns\",\"at\":"
                                      : ",\"layout\":{\"kind\":\"dta-column-blocks\",\"at\":");
            buf_put(meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", at));
            put_s(meta, ",\"rows\":");
            buf_put(meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", rows));
            if (block != rows) {
                put_s(meta, ",\"block\":");
                buf_put(meta, nb, (size_t)snprintf(nb, sizeof(nb), "%llu", (unsigned long long)block));
            }
            put_s(meta, ",\"widths\":[");
            for (size_t j = 0; j < nw; j++) {
                if (j) buf_putc(meta, ',');
                buf_put(meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", w[j]));
            }
            put_s(meta, "]}");
        }
        free(w);
    }
    put_s(meta, "},\"schema\":");
    buf_put(meta, schema->data, schema->len);
    buf_putc(meta, '}');
}

typedef struct { const uint8_t *d; uint64_t n, pos; const Lay *L; } Gather;

static size_t gather_src(uint8_t *buf, size_t cap, void *ctx)
{
    Gather *g = ctx;
    size_t k = g->n - g->pos < cap ? (size_t)(g->n - g->pos) : cap;
    lay_map(g->L, g->pos, buf, k, g->d, NULL, 0);
    g->pos += k;
    return k;
}

typedef struct { int fd; uint64_t at; } FileSink;

static int file_sink(const uint8_t *p, size_t n, void *ctx)
{
    FileSink *f = ctx;
    while (n) {
        ssize_t w = pwrite(f->fd, p, n, (off_t)f->at);
        if (w <= 0) return -1;
        p += w; n -= (size_t)w; f->at += (uint64_t)w;
    }
    return 0;
}

static void put_header(uint8_t h[16], const uint64_t lens[3])
{
    memcpy(h, PPZ_MAGIC, 4);
    for (int i = 0; i < 3; i++)
        for (int s = 24, k = 0; s >= 0; s -= 8, k++) h[4 + i * 4 + k] = (uint8_t)((lens[i] >> s) & 0xFF);
}

/* Write an archive of data[0..n) through `sink`: 16 header bytes (zeros,
 * for the caller to fill from lens), then the three streams. -2 when a
 * stream is past the 32-bit length the header holds. */
static int write_original(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
                          const Buf *schema, int (*sink)(const uint8_t *, size_t, void *), void *ctx,
                          uint64_t lens[3])
{
    Buf meta, mz, tz;
    Lay L;
    int has_lay = 0, rc = -1;
    memset(&L, 0, sizeof(L));
    buf_init(&mz); buf_init(&tz);
    original_meta(data, n, fmt, encoding, schema, &meta, &L, &has_lay);
    uint8_t zero[16] = { 0 };
    if (ppz_lzma_compress(meta.data, meta.len, &mz) || ppz_lzma_compress((const uint8_t *)"", 0, &tz))
        goto done;
    if (sink(zero, 16, ctx) || sink(mz.data, mz.len, ctx)) goto done;
    Gather g = { data, n, 0, has_lay ? &L : NULL };
    uint64_t bl = 0;
    if (ppz_xz_stream(PPZ_XZ_PLAIN, gather_src, &g, sink, ctx, &bl)) goto done;
    if (sink(tz.data, tz.len, ctx)) goto done;
    lens[0] = mz.len; lens[1] = bl; lens[2] = tz.len;
    rc = mz.len > 0xFFFFFFFFu || bl > 0xFFFFFFFFu ? -2 : 0;
done:
    lay_free(&L);
    buf_free(&meta); buf_free(&mz); buf_free(&tz);
    return rc;
}

static int buf_sink(const uint8_t *p, size_t n, void *ctx) { buf_put((Buf *)ctx, p, n); return 0; }

/* The archive in memory -- for the tests; the program streams to disk. */
int ppz_encode_original(const uint8_t *data, size_t n, const char *fmt,
                        const char *encoding, const Buf *schema, Buf *out)
{
    buf_free(out);
    uint64_t lens[3];
    if (write_original(data, n, fmt, encoding, schema, buf_sink, out, lens)) { buf_free(out); return -1; }
    put_header(out->data, lens);
    return 0;
}

/* An archive of an original file, opened: its format, size, layout and
 * where the stored stream is. Every number comes from the archive, so all
 * of it is checked against the bytes before anything is moved. */
typedef struct {
    char      fmt[16];
    uint64_t  bytes;
    const uint8_t *bin;
    size_t    binlen;
    Lay       L;
    int       has_lay;
    int       is_original;     /* says it is one, whether or not it checks out */
    Js       *meta;
} Orig;

/* 1 an original archive, 0 a table archive, -1 not an archive or damaged */
static int orig_open(const uint8_t *blob, size_t n, Orig *o)
{
    memset(o, 0, sizeof(*o));
    Js *meta = ppz_meta(blob, n);
    if (!meta) return -1;
    const Js *og = js_get(meta, "original");
    if (!og) { js_free(meta); return 0; }
    o->is_original = 1;
    size_t ml = ((size_t)blob[4] << 24) | ((size_t)blob[5] << 16) | ((size_t)blob[6] << 8) | blob[7];
    size_t bl = ((size_t)blob[8] << 24) | ((size_t)blob[9] << 16) | ((size_t)blob[10] << 8) | blob[11];
    const Js *f = js_get(og, "format"), *nb = js_get(og, "bytes");
    if (bl > n - 16 - ml || og->kind != JS_OBJ || !f || f->kind != JS_STR || !ppz_stat_name(f->str) ||
        !nb || nb->kind != JS_NUM || !nb->is_int || nb->inum < 0 || strlen(f->str) >= sizeof(o->fmt))
        goto bad;
    snprintf(o->fmt, sizeof(o->fmt), "%s", f->str);
    o->bytes = (uint64_t)nb->inum;
    o->bin = blob + 16 + ml;
    o->binlen = bl;
    const Js *lay = js_get(og, "layout");
    if (lay) {
        const Js *k = js_get(lay, "kind"), *w = js_get(lay, "widths");
        int64_t at = js_i64(js_get(lay, "at"), -1), rows = js_i64(js_get(lay, "rows"), -1);
        int blocks = k && k->kind == JS_STR && !strcmp(k->str, "dta-column-blocks");
        int64_t block = blocks ? js_i64(js_get(lay, "block"), -1) : rows;
        if (lay->kind != JS_OBJ || !k || k->kind != JS_STR ||
            (strcmp(k->str, "dta-columns") && !blocks) ||
            !w || w->kind != JS_ARR || !w->count || w->count > DTA_MAX_FIELDS || at < 0 || rows < 0 ||
            block < (rows ? 1 : 0))
            goto bad;
        size_t *ws = malloc(w->count * sizeof(size_t));
        if (!ws) goto bad;
        for (size_t j = 0; j < w->count; j++) {
            int64_t x = js_i64(&w->items[j], 0);
            ws[j] = w->items[j].kind == JS_NUM && x >= 1 && x <= 2045 ? (size_t)x : 0;
        }
        int no = lay_make(&o->L, (uint64_t)at, (uint64_t)rows, rows ? (uint64_t)block : 1, ws, w->count, o->bytes);
        free(ws);
        if (no) goto bad;
        o->has_lay = 1;
    }
    o->meta = meta;
    return 1;
bad:
    js_free(meta);
    return -1;
}

static void orig_close(Orig *o) { lay_free(&o->L); js_free(o->meta); o->meta = NULL; }

typedef struct { const Orig *o; uint64_t pos; uint8_t *dst; const uint8_t *cmp; } Emit;

static int emit_sink(const uint8_t *p, size_t n, void *ctx)
{
    Emit *e = ctx;
    if (lay_map(e->o->has_lay ? &e->o->L : NULL, e->pos, (uint8_t *)p, n, e->cmp, e->dst, e->dst ? 1 : 2))
        return -1;
    e->pos += n;
    return 0;
}

/* The stored stream decoded into dst (the original, o->bytes long), or
 * compared with cmp. 0 only when every byte arrived and matched; the
 * decoder's limit keeps every write inside the o->bytes the caller sized. */
static int orig_emit(const Orig *o, uint8_t *dst, const uint8_t *cmp)
{
    Emit e = { o, 0, dst, cmp };
    uint64_t got = 0;
    if (ppz_xz_unstream(o->bin, o->binlen, o->bytes, emit_sink, &e, &got)) return -1;
    return got == o->bytes ? 0 : -1;
}

int ppz_original(const uint8_t *blob, size_t n, Buf *orig, char *fmt, size_t fcap, Js **meta_out)
{
    if (meta_out) *meta_out = NULL;
    Orig o;
    int k = orig_open(blob, n, &o);
    if (k != 1) return k;
    if (strlen(o.fmt) >= fcap) { orig_close(&o); return -1; }
    if (orig) {
        /* in memory: the same bound as every other decode */
        buf_free(orig);
        if (o.bytes > ((uint64_t)4 << 30)) { orig_close(&o); return -1; }
        buf_need(orig, (size_t)o.bytes + 1);
        if (orig_emit(&o, orig->data, NULL)) { buf_free(orig); orig_close(&o); return -1; }
        orig->len = (size_t)o.bytes;
    }
    snprintf(fmt, fcap, "%s", o.fmt);
    if (meta_out) { *meta_out = o.meta; o.meta = NULL; }
    orig_close(&o);
    return 1;
}

/* ----------------------------------------------- files bigger than memory */

/* Files are mapped, not read: the system pages them in and out as needed,
 * so a 30 GB .dta on a machine with 8 GB works the same as a small one. */

int ppz_map(const char *path, Map *m, char *err, size_t cap)
{
    memset(m, 0, sizeof(*m));
    m->fd = open(path, O_RDONLY);
    struct stat st;
    if (m->fd < 0 || fstat(m->fd, &st)) {
        seterr(err, cap, "cannot read %s: %s", path, strerror(errno));
        if (m->fd >= 0) close(m->fd);
        m->fd = -1;
        return -1;
    }
    if (S_ISDIR(st.st_mode)) { seterr(err, cap, "%s is a directory, not a file", path); close(m->fd); m->fd = -1; return -1; }
    m->n = (size_t)st.st_size;
    if (m->n) {
        void *p = mmap(NULL, m->n, PROT_READ, MAP_PRIVATE, m->fd, 0);
        if (p == MAP_FAILED) { seterr(err, cap, "cannot map %s: %s", path, strerror(errno)); close(m->fd); m->fd = -1; return -1; }
        m->p = p;
    } else m->p = (const uint8_t *)"";
    return 0;
}

void ppz_unmap(Map *m) { if (m->n) munmap((void *)m->p, m->n); if (m->fd >= 0) close(m->fd); m->fd = -1; m->n = 0; }

/* A temporary file named after dst (dst.partXXXXXX, so it never clobbers
 * anything), registered so an interrupt removes it. ppz_tmp_done renames it
 * to dst when ok, else removes it. */
int ppz_tmp_open(const char *dst, char *tmp, size_t cap)
{
    snprintf(tmp, cap, "%s.partXXXXXX", dst);
    int fd = mkstemp(tmp);
    if (fd < 0) return -1;
    ppz_tmp_register(tmp);
    mode_t um = umask(0);
    umask(um);
    fchmod(fd, 0666 & ~um);
    return fd;
}

int ppz_tmp_done(int fd, const char *tmp, const char *dst, int ok)
{
    if (close(fd)) ok = 0;
    if (ok && dst && rename(tmp, dst)) ok = 0;
    if (!ok || !dst) unlink(tmp);
    ppz_tmp_forget(tmp);
    return ok ? 0 : -1;
}

int ppz_stat_archive(const char *src, const char *fmt, const char *encoding, const char *dst,
                     int verify, StatArchived *info, char *err, size_t cap)
{
    memset(info, 0, sizeof(*info));
    Map m;
    if (ppz_map(src, &m, err, cap)) return -1;
    Buf schema;
    buf_init(&schema);
    int rc = -1, fd;
    char tmp[4200];
    if (stat_scan(m.p, m.n, fmt, encoding, &schema, &info->loss, &info->rows, &info->cols, err, cap)) goto out;
    if ((fd = ppz_tmp_open(dst, tmp, sizeof(tmp))) < 0) {
        seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
        goto out;
    }
    FileSink fs = { fd, 0 };
    uint64_t lens[3];
    int w = write_original(m.p, m.n, fmt, encoding, &schema, file_sink, &fs, lens);
    if (w == -2) seterr(err, cap, "it compresses to more than 4 GB, past what one archive holds");
    else if (w) seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
    uint8_t h[16];
    FileSink hs = { fd, 0 };
    int ok = !w;
    if (ok) {
        put_header(h, lens);
        if (file_sink(h, 16, &hs)) { seterr(err, cap, "cannot write %s: %s", dst, strerror(errno)); ok = 0; }
    }
    info->in_bytes = m.n;
    info->out_bytes = fs.at;
    if (ok && verify) {
        /* the archive as written, decoded, against the source */
        Map a;
        Orig o;
        ok = !ppz_map(tmp, &a, err, cap);
        if (ok) {
            ok = orig_open(a.p, a.n, &o) == 1 && !strcmp(o.fmt, fmt) && o.bytes == m.n &&
                 !orig_emit(&o, NULL, m.p);
            orig_close(&o);
            ppz_unmap(&a);
        }
        if (!ok) seterr(err, cap, "verification FAILED -- nothing written");
    }
    if (ppz_tmp_done(fd, tmp, dst, ok)) {
        if (ok) seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
        goto out;
    }
    rc = 0;
out:
    buf_free(&schema);
    ppz_unmap(&m);
    return rc;
}

/* Restore an original archive to `dst`: the original bytes when dst has the
 * archive's own format; otherwise the original is decoded into a temporary
 * file, translated from its mapping, and removed. A translation to another
 * stats format comes back in info->out for the caller to write; one to a
 * table format is written to dst here, a block of rows at a time. Returns
 * 1 when the file is not an archive of an original at all (the caller
 * decodes it as a table), -1 with err set when it is one and fails. */
int ppz_stat_restore(const char *arc, const char *dst, StatRestored *info, char *err, size_t cap)
{
    memset(info, 0, sizeof(*info));
    buf_init(&info->out);
    buf_init(&info->report);
    Map a;
    if (ppz_map(arc, &a, err, cap)) return 1;
    Orig o;
    memset(&o, 0, sizeof(o));
    int k = a.n >= 16 ? orig_open(a.p, a.n, &o) : -1;
    if (k != 1) {
        int was = k < 0 && o.is_original;
        ppz_unmap(&a);
        if (!was) return 1;
        seterr(err, cap, "cannot read %s -- it is damaged", arc);
        return -1;
    }
    int rc = -1, fd;
    char tmp[4200];
    const char *want = ppz_format_of(dst);
    snprintf(info->fmt, sizeof(info->fmt), "%s", o.fmt);
    info->packed = a.n;
    info->bytes = o.bytes;
    int same = !strcmp(want, o.fmt);
    char base[4096];
    const char *td = getenv("TMPDIR");
    snprintf(base, sizeof(base), "%s/polypress-restore", td && *td ? td : "/tmp");
    if ((fd = ppz_tmp_open(strcmp(dst, "-") ? dst : base, tmp, sizeof(tmp))) < 0) {
        seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
        goto out;
    }
    uint8_t *out = NULL;
    int ok = 1;
    if (o.bytes) {
        if (ftruncate(fd, (off_t)o.bytes)) {
            seterr(err, cap, "no room for %s (%llu bytes)", dst, (unsigned long long)o.bytes);
            ok = 0;
        } else {
            void *p = mmap(NULL, (size_t)o.bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
            if (p == MAP_FAILED) { seterr(err, cap, "cannot map %s: %s", tmp, strerror(errno)); ok = 0; }
            else out = p;
        }
    }
    if (ok && orig_emit(&o, out ? out : (uint8_t *)"", NULL)) {
        seterr(err, cap, "cannot read %s -- it is damaged", arc);
        ok = 0;
    }
    if (ok && !same) {
        /* translate straight from the decoded original, then drop it */
        const Js *enc = js_get(js_get(o.meta, "original"), "encoding");
        const char *e = enc && enc->kind == JS_STR ? enc->str : NULL;
        const uint8_t *src = out ? out : (const uint8_t *)"";
        info->translated = 1;
        if (ppz_stat_name(want)) {
            info->stat_dst = 1;
            ok = !stat_translate(src, (size_t)o.bytes, o.fmt, e, want, &info->out, &info->report,
                                 &info->rows, &info->cols, err, cap);
        } else
            ok = !stat_stream(src, (size_t)o.bytes, o.fmt, e, dst, &info->loss, &info->rows, &info->cols, err, cap);
    }
    if (out) {
        if (same && ok && msync(out, (size_t)o.bytes, MS_SYNC)) {
            seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
            ok = 0;
        }
        munmap(out, (size_t)o.bytes);
    }
    int bad = ppz_tmp_done(fd, tmp, same ? dst : NULL, ok);
    if (bad && ok) seterr(err, cap, "cannot write %s: %s", dst, strerror(errno));
    rc = ok && !bad ? 0 : -1;
out:
    if (rc) { buf_free(&info->out); buf_free(&info->report); }
    orig_close(&o);
    ppz_unmap(&a);
    return rc;
}

/* ------------------------------------- translation between stats formats */

/* A Stata, SPSS or SAS file rewritten as another of them -- .dta to .sav and
 * back, either to .xpt or .sas7bdat -- through ReadStat's writers. Unlike a
 * CSV copy this keeps what the formats share: column types, variable and
 * value labels, dates (re-counted from the new format's day zero, in its
 * unit), user-defined and extended missing values, notes, the file label.
 * What the destination cannot hold is changed or dropped and SAID: every
 * rename, truncation and dropped label goes into the report (rep), one line
 * each. The new file is read back and compared cell by cell with what was
 * meant to be written before it is handed over.
 *
 * Missing values follow what Stata's own `import spss` does: SPSS
 * user-defined missing codes become extended missing values (.a, .b, ...),
 * one letter per code or range, and their value labels move with them.
 * Going the other way SPSS has no extended missing values, so they become
 * system missing and the report counts them. */

typedef enum { SV_SYS, SV_NUM, SV_STR, SV_TAG } SvKind;
typedef struct { SvKind k; double d; char *s; char tag; } Sv;

typedef struct { Sv v; char *label; } SvPair;
typedef struct { char *name; SvPair *p; size_t n; int string; } SvSet;

typedef struct {
    char           *name, *label, *format, *lset;
    readstat_type_t type;
    size_t          width;
    int             measure, align, dispw;
    Sv             *mlo, *mhi;
    int             nmiss;
    DateKind        dk;
    double          dscale;
    int64_t         depoch;
    double         *num;          /* numeric cells */
    char           *tag;          /* 0 a value, 1 system missing, else the tag */
    size_t         *off, *len;    /* string cells, into the arena */
} SCol;

typedef struct {
    const char *fmt;
    size_t      ncols, nrows, rcap;
    SCol       *c;
    SvSet      *sets;
    size_t      nsets;
    char      **notes;
    size_t      nnotes;
    char       *file_label, *table;
    time_t      stamp;
    Buf         arena;
    int         failed;
} SFile;

static char *sdup(const char *s) { return strdup(s ? s : ""); }

static void sv_free(Sv *v) { if (v && v->k == SV_STR) free(v->s); }

static Sv sv_of(readstat_value_t v)
{
    Sv r = { SV_SYS, 0, NULL, 0 };
    if (readstat_value_is_tagged_missing(v)) { r.k = SV_TAG; r.tag = readstat_value_tag(v); }
    else if (readstat_value_is_system_missing(v)) r.k = SV_SYS;
    else if (readstat_value_type_class(v) == READSTAT_TYPE_CLASS_STRING) {
        r.k = SV_STR; r.s = sdup(readstat_string_value(v));
    } else { r.k = SV_NUM; r.d = readstat_double_value(v); }
    return r;
}

static void sfile_free(SFile *f)
{
    for (size_t j = 0; j < f->ncols && f->c; j++) {
        SCol *c = &f->c[j];
        free(c->name); free(c->label); free(c->format); free(c->lset);
        for (int i = 0; i < c->nmiss; i++) { sv_free(&c->mlo[i]); sv_free(&c->mhi[i]); }
        free(c->mlo); free(c->mhi);
        free(c->num); free(c->tag); free(c->off); free(c->len);
    }
    free(f->c);
    for (size_t i = 0; i < f->nsets; i++) {
        for (size_t k = 0; k < f->sets[i].n; k++) { sv_free(&f->sets[i].p[k].v); free(f->sets[i].p[k].label); }
        free(f->sets[i].p); free(f->sets[i].name);
    }
    free(f->sets);
    for (size_t i = 0; i < f->nnotes; i++) free(f->notes[i]);
    free(f->notes);
    free(f->file_label); free(f->table);
    buf_free(&f->arena);
    memset(f, 0, sizeof(*f));
}

static int sgrow(SFile *f, size_t need)
{
    if (need <= f->rcap) return 0;
    size_t cap = f->rcap ? f->rcap : 1024;
    while (cap < need) cap *= 2;
    for (size_t j = 0; j < f->ncols; j++) {
        SCol *c = &f->c[j];
        if (readstat_type_class(c->type) == READSTAT_TYPE_CLASS_STRING) {
            size_t *o = realloc(c->off, cap * sizeof(size_t)); if (!o) return -1; c->off = o;
            size_t *l = realloc(c->len, cap * sizeof(size_t)); if (!l) return -1; c->len = l;
            memset(c->len + f->rcap, 0, (cap - f->rcap) * sizeof(size_t));
            memset(c->off + f->rcap, 0, (cap - f->rcap) * sizeof(size_t));
        } else {
            double *d = realloc(c->num, cap * sizeof(double)); if (!d) return -1; c->num = d;
            char *t = realloc(c->tag, cap); if (!t) return -1; c->tag = t;
            memset(c->tag + f->rcap, 1, cap - f->rcap);
            memset(c->num + f->rcap, 0, (cap - f->rcap) * sizeof(double));
        }
    }
    f->rcap = cap;
    return 0;
}

#define SABORT do { f->failed = 1; return READSTAT_HANDLER_ABORT; } while (0)

static int s_meta(readstat_metadata_t *m, void *ctx)
{
    SFile *f = ctx;
    int64_t nv = readstat_get_var_count(m);
    if (nv < 0 || nv > 1000000) SABORT;
    f->ncols = (size_t)nv;
    f->c = calloc(f->ncols ? f->ncols : 1, sizeof(SCol));
    if (!f->c) SABORT;
    const char *lab = readstat_get_file_label(m);
    if (lab && *lab) f->file_label = sdup(lab);
    const char *tn = readstat_get_table_name(m);
    if (tn && *tn) f->table = sdup(tn);
    time_t mt = readstat_get_modified_time(m), ct = readstat_get_creation_time(m);
    f->stamp = mt > 0 ? mt : ct > 0 ? ct : 0;
    return READSTAT_HANDLER_OK;
}

static int s_note(int i, const char *note, void *ctx)
{
    SFile *f = ctx;
    (void)i;
    char **n = realloc(f->notes, (f->nnotes + 1) * sizeof(char *));
    if (!n) SABORT;
    f->notes = n;
    if (!(f->notes[f->nnotes++] = sdup(note))) SABORT;
    return READSTAT_HANDLER_OK;
}

static int s_var(int index, readstat_variable_t *v, const char *val_labels, void *ctx)
{
    SFile *f = ctx;
    if (index < 0 || (size_t)index >= f->ncols || f->rcap) SABORT;
    SCol *c = &f->c[index];
    c->name = sdup(readstat_variable_get_name(v));
    const char *lab = readstat_variable_get_label(v);
    if (lab && *lab) c->label = sdup(lab);
    const char *fm = readstat_variable_get_format(v);
    if (fm && *fm) c->format = sdup(fm);
    if (val_labels && *val_labels) c->lset = sdup(val_labels);
    c->type = readstat_variable_get_type(v);
    if (c->type == READSTAT_TYPE_STRING_REF) c->type = READSTAT_TYPE_STRING;
    c->width = readstat_variable_get_storage_width(v);
    c->measure = readstat_variable_get_measure(v);
    c->align = readstat_variable_get_alignment(v);
    c->dispw = readstat_variable_get_display_width(v);
    int nm = readstat_variable_get_missing_ranges_count(v);
    if (nm > 0) {
        c->mlo = calloc((size_t)nm, sizeof(Sv));
        c->mhi = calloc((size_t)nm, sizeof(Sv));
        if (!c->mlo || !c->mhi) SABORT;
        for (int i = 0; i < nm; i++) {
            c->mlo[i] = sv_of(readstat_variable_get_missing_range_lo(v, i));
            c->mhi[i] = sv_of(readstat_variable_get_missing_range_hi(v, i));
        }
        c->nmiss = nm;
    }
    if (readstat_type_class(c->type) == READSTAT_TYPE_CLASS_NUMERIC)
        c->dk = date_kind(f->fmt, c->format, &c->dscale, &c->depoch);
    if (!c->name) SABORT;
    return READSTAT_HANDLER_OK;
}

static int s_value(int obs, readstat_variable_t *var, readstat_value_t v, void *ctx)
{
    SFile *f = ctx;
    int j = readstat_variable_get_index(var);
    if (obs < 0 || j < 0 || (size_t)j >= f->ncols) SABORT;
    if ((size_t)obs >= f->nrows) {
        if (sgrow(f, (size_t)obs + 1)) SABORT;
        f->nrows = (size_t)obs + 1;
    }
    SCol *c = &f->c[j];
    if (readstat_type_class(c->type) == READSTAT_TYPE_CLASS_STRING) {
        const char *s = readstat_value_is_system_missing(v) ? NULL : readstat_string_value(v);
        size_t n = s ? strlen(s) : 0;
        c->off[obs] = f->arena.len;
        c->len[obs] = n;
        if (n) buf_put(&f->arena, s, n);
        buf_putc(&f->arena, 0);           /* so a cell can go to ReadStat as it is */
    } else if (readstat_value_is_tagged_missing(v)) {
        c->tag[obs] = readstat_value_tag(v);
        if (c->tag[obs] < 2) c->tag[obs] = 1;
    } else if (readstat_value_is_system_missing(v)) {
        c->tag[obs] = 1;
    } else {
        double d = readstat_double_value(v);
        if (isnan(d)) c->tag[obs] = 1;
        else { c->num[obs] = d; c->tag[obs] = 0; }
    }
    return READSTAT_HANDLER_OK;
}

static int s_label(const char *set, readstat_value_t v, const char *label, void *ctx)
{
    SFile *f = ctx;
    SvSet *ls = NULL;
    for (size_t i = f->nsets; i-- > 0; )
        if (!strcmp(f->sets[i].name, set)) { ls = &f->sets[i]; break; }
    if (!ls) {
        SvSet *ns = realloc(f->sets, (f->nsets + 1) * sizeof(SvSet));
        if (!ns) SABORT;
        f->sets = ns;
        ls = &f->sets[f->nsets++];
        memset(ls, 0, sizeof(*ls));
        if (!(ls->name = sdup(set))) SABORT;
    }
    SvPair *p = realloc(ls->p, (ls->n + 1) * sizeof(SvPair));
    if (!p) SABORT;
    ls->p = p;
    ls->p[ls->n].v = sv_of(v);
    if (ls->p[ls->n].v.k == SV_STR) ls->string = 1;
    ls->p[ls->n].label = sdup(label);
    ls->n++;
    return READSTAT_HANDLER_OK;
}

static int sfile_read(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
                      SFile *f, char *err, size_t cap)
{
    memset(f, 0, sizeof(*f));
    f->fmt = fmt;
    buf_init(&f->arena);
    readstat_parser_t *p = readstat_parser_init();
    if (!p) { seterr(err, cap, "out of memory"); return -1; }
    readstat_set_metadata_handler(p, s_meta);
    readstat_set_note_handler(p, s_note);
    readstat_set_variable_handler(p, s_var);
    readstat_set_value_handler(p, s_value);
    readstat_set_value_label_handler(p, s_label);
    readstat_error_t e = rs_run(p, data, n, fmt, encoding, f);
    if (e == READSTAT_OK && !f->failed && f->c && sgrow(f, 1)) f->failed = 1;
    if (e != READSTAT_OK || f->failed || !f->c) {
        if (f->failed && e == READSTAT_OK) seterr(err, cap, "out of memory reading the %s file", ppz_stat_name(fmt));
        else seterr(err, cap, "cannot read this %s file: %s%s%s", ppz_stat_name(fmt),
                    readstat_error_message(e), g_rs_err[0] ? " -- " : "", g_rs_err);
        sfile_free(f);
        return -1;
    }
    /* strict UTF-8, as stat_read: every piece of text that will be written */
    int ok = valid_utf8((const char *)f->arena.data, f->arena.len) &&
             (!f->file_label || valid_utf8(f->file_label, strlen(f->file_label)));
    for (size_t j = 0; ok && j < f->ncols; j++) {
        const SCol *c = &f->c[j];
        ok = valid_utf8(c->name, strlen(c->name)) && (!c->label || valid_utf8(c->label, strlen(c->label)));
        for (int i = 0; ok && i < c->nmiss; i++)
            ok = (c->mlo[i].k != SV_STR || valid_utf8(c->mlo[i].s, strlen(c->mlo[i].s))) &&
                 (c->mhi[i].k != SV_STR || valid_utf8(c->mhi[i].s, strlen(c->mhi[i].s)));
    }
    for (size_t i = 0; ok && i < f->nsets; i++)
        for (size_t k = 0; ok && k < f->sets[i].n; k++)
            ok = valid_utf8(f->sets[i].p[k].label, strlen(f->sets[i].p[k].label)) &&
                 (f->sets[i].p[k].v.k != SV_STR || valid_utf8(f->sets[i].p[k].v.s, strlen(f->sets[i].p[k].v.s)));
    for (size_t i = 0; ok && i < f->nnotes; i++) ok = valid_utf8(f->notes[i], strlen(f->notes[i]));
    if (!ok) { seterr(err, cap, ENC_MSG, ppz_stat_name(fmt)); sfile_free(f); return -1; }
    return 0;
}

/* What each destination can hold. Limits are ReadStat's checks, which are
 * the formats' own. */
typedef struct {
    const char *fmt;
    int    family;            /* 0 Stata, 1 SPSS, 2 SAS: how dates are counted */
    size_t name_max;          /* bytes */
    int    upper;             /* names upper case only (portable) */
    size_t label_max;         /* variable label, characters */
    size_t file_label_max;
    size_t str_max;           /* longest string; Stata goes on to strL */
    int    labels;            /* value labels at all */
    int    tags;              /* extended missing: 0, 'a' (Stata), 'A' (SAS) */
    int    user_missing;      /* SPSS-style missing codes */
    int    notes;             /* 80: SPSS document lines of 80 bytes; 1: any */
    int    measure;
} Dest;

static int dest_of(const char *fmt, Dest *d)
{
    static const Dest D[] = {
        { "dta",      0, 32, 0, 80,  80,  2045,  1, 'a', 0, 1,  0 },
        { "sav",      1, 64, 0, 255, 64,  32767, 1, 0,   1, 80, 1 },
        { "zsav",     1, 64, 0, 255, 64,  32767, 1, 0,   1, 80, 1 },
        { "por",      1, 8,  1, 255, 60,  255,   1, 0,   1, 80, 0 },
        { "xpt",      2, 8,  0, 40,  40,  200,   0, 'A', 0, 0,  0 },
        { "sas7bdat", 2, 32, 0, 256, 256, 32767, 0, 0,   0, 0,  0 },
    };
    for (size_t i = 0; i < sizeof(D) / sizeof(D[0]); i++)
        if (!strcmp(D[i].fmt, fmt)) { *d = D[i]; return 0; }
    return -1;
}

static int family_of(const char *fmt)
{
    if (!strcmp(fmt, "dta")) return 0;
    if (!strcmp(fmt, "sav") || !strcmp(fmt, "zsav") || !strcmp(fmt, "por")) return 1;
    return 2;
}

/* UTF-8 characters, and the byte length of the first `chars` of them */
static size_t u8_chars(const char *s) { size_t n = 0; for (; *s; s++) n += ((unsigned char)*s & 0xC0) != 0x80; return n; }
static size_t u8_cut(const char *s, size_t chars, size_t bytes)
{
    size_t i = 0, n = 0;
    while (s[i] && n < chars) {
        size_t k = 1;
        while (((unsigned char)s[i + k] & 0xC0) == 0x80) k++;
        if (i + k > bytes) break;
        i += k; n++;
    }
    return i;
}

static int reserved(const Dest *d, const char *s)
{
    static const char *STATA[] = { "_all", "_b", "byte", "_coef", "_cons", "double", "float", "if",
        "in", "int", "long", "_n", "_N", "_pi", "_pred", "_rc", "_skip", "strL", "using", "with", NULL };
    static const char *SPSS[] = { "ALL", "AND", "BY", "EQ", "GE", "GT", "LE", "LT", "NE", "NOT",
        "OR", "TO", "WITH", NULL };
    static const char *SAS[] = { "_N_", "_ERROR_", "_NUMERIC_", "_CHARACTER_", "_ALL_", NULL };
    const char **R = d->family == 0 ? STATA : d->family == 1 ? SPSS : SAS;
    for (int i = 0; R[i]; i++) if (!strcmp(R[i], s)) return 1;
    if (d->family == 0) {
        int len = 0, used = 0;
        if (sscanf(s, "str%d%n", &len, &used) == 1 && !s[used] && len >= 1 && len <= 2045) return 1;
    }
    return 0;
}

/* A legal, unique name for the destination: illegal characters become _,
 * a bad first character gets a prefix, then cut to length and numbered
 * apart from the names already taken (compared ignoring case, as SPSS and
 * SAS do). */
static char *legal_name(const Dest *d, const char *src, char **taken, size_t ntaken)
{
    char b[80];
    size_t n = 0;
    int stata = d->family == 0, spss = d->family == 1;
    for (size_t i = 0; src[i] && n < sizeof(b) - 8; i++) {
        unsigned char ch = (unsigned char)src[i];
        int ok = ch == '_' || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
                 (spss && (ch == '@' || ch == '.' || ch == '$' || ch == '#')) ||
                 (ch >= 0x80 && (stata || (spss && !d->upper)));
        b[n++] = ok ? (char)ch : '_';
    }
    b[n] = 0;
    if (d->upper) for (size_t i = 0; i < n; i++) if (b[i] >= 'a' && b[i] <= 'z') b[i] = (char)(b[i] - 32);
    unsigned char f0 = (unsigned char)b[0];
    int first_ok = (f0 >= 'a' && f0 <= 'z') || (f0 >= 'A' && f0 <= 'Z') ||
                   (f0 == '_' && !spss) || (f0 == '@' && spss) || (f0 >= 0x80 && (stata || spss));
    if (!n || !first_ok) {
        memmove(b + 1, b, n + 1);
        b[0] = d->upper ? 'V' : 'v';
        n++;
    }
    size_t max = d->name_max;
    if (stata) n = u8_cut(b, 32, 128);
    else if (n > max) n = u8_cut(b, max, max);
    b[n] = 0;
    if (reserved(d, b)) { if (n + 1 > max) n = max - 1; b[n++] = '_'; b[n] = 0; }
    char t[96];
    snprintf(t, sizeof(t), "%s", b);
    for (unsigned k = 2; ; k++) {
        int clash = 0;
        for (size_t i = 0; i < ntaken && !clash; i++) clash = !strcasecmp(taken[i], t);
        if (!clash) break;
        char suf[16];
        int sl = snprintf(suf, sizeof(suf), "_%u", k);
        size_t keep = n + (size_t)sl > max ? max - (size_t)sl : n;
        keep = u8_cut(b, keep, keep);
        snprintf(t, sizeof(t), "%.*s%s", (int)keep, b, suf);
    }
    return strdup(t);
}

/* A display format as kind + width + decimals, so it can be written in
 * another family's spelling. Only fixed and comma formats carry over; a
 * general one (%9.0g, BEST12.) is every format's default and is left to it. */
typedef enum { NF_NONE, NF_FIXED, NF_COMMA, NF_GENERAL, NF_OTHER } NumFmt;

static NumFmt num_fmt(int family, const char *f, int *w, int *dd)
{
    *w = 0; *dd = 0;
    if (!f || !*f) return NF_NONE;
    if (family == 0) {
        int a = 0, b = 0, used = 0;
        const char *p = f;
        if (*p++ != '%') return NF_OTHER;
        if (*p == '-' || *p == '~') p++;
        if (sscanf(p, "%d.%d%n", &a, &b, &used) == 2) {
            *w = a; *dd = b;
            if (!strcmp(p + used, "f")) return NF_FIXED;
            if (!strcmp(p + used, "fc")) return NF_COMMA;
            if (!strcmp(p + used, "g") || !strcmp(p + used, "gc")) return NF_GENERAL;
        }
        if (sscanf(p, "%ds%n", &a, &used) == 1 && !p[used]) return NF_NONE;
        return NF_OTHER;
    }
    char u[16];
    size_t k = 0;
    while (f[k] && !(f[k] >= '0' && f[k] <= '9') && f[k] != '.' && k + 1 < sizeof(u)) {
        u[k] = (char)(f[k] >= 'a' && f[k] <= 'z' ? f[k] - 32 : f[k]); k++;
    }
    u[k] = 0;
    const char *p = f + k;
    sscanf(p, "%d", w);
    const char *dot = strchr(p, '.');
    if (dot) *dd = atoi(dot + 1);
    if (!strcmp(u, "A") || !strcmp(u, "$") || !strcmp(u, "$CHAR") || !strcmp(u, "AHEX")) return NF_NONE;
    if ((family == 1 && !strcmp(u, "F")) || (family == 2 && (!*u || !strcmp(u, "F")))) return *w ? NF_FIXED : NF_GENERAL;
    if (!strcmp(u, "COMMA")) return NF_COMMA;
    if (!strcmp(u, "BEST")) return NF_GENERAL;
    return NF_OTHER;
}

static void num_fmt_put(int family, NumFmt k, int w, int dd, char *out, size_t cap)
{
    out[0] = 0;
    if (w <= 0 || w > 40) w = 9;
    if (dd < 0 || dd >= w) dd = 0;
    if (family == 0) {
        if (k == NF_FIXED) snprintf(out, cap, "%%%d.%df", w, dd);
        else if (k == NF_COMMA) snprintf(out, cap, "%%%d.%dfc", w, dd);
    } else if (family == 1) {
        if (k == NF_FIXED) snprintf(out, cap, "F%d.%d", w, dd);
        else if (k == NF_COMMA) snprintf(out, cap, "COMMA%d.%d", w, dd);
    } else {
        if (k == NF_FIXED) snprintf(out, cap, "%d.%d", w, dd);
        else if (k == NF_COMMA) snprintf(out, cap, "COMMA%d.%d", w, dd);
    }
}

/* A cell of the file about to be written: what is checked against the
 * file once it has been read back. */
typedef struct {
    char   *name, *label, *format;
    int     string, strl;
    readstat_type_t type;
    size_t  width;
    int     lset;                 /* index into the planned label sets, or -1 */
    double *num;                  /* numeric: values */
    char   *tag;                  /* 0, 1 system missing, or the tag */
    const char **s;               /* string: pointer + length */
    size_t *len;
    Sv     *mlo, *mhi;            /* missing codes kept as codes (SPSS) */
    int     nmiss;
    int     measure, align, dispw;
} PCol;

typedef struct { char *name; int string; SvPair *p; size_t n; } PSet;

static void rep(Buf *r, const char *fmt, ...)
{
    char s[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s, sizeof(s), fmt, ap);
    va_end(ap);
    buf_put(r, "  ", 2);
    buf_put(r, s, strlen(s));
    buf_putc(r, '\n');
}

static const char *plural(size_t n) { return n == 1 ? "" : "s"; }

/* A date, date-time or time value counted the destination's way. */
static double date_to(double v, const SCol *c, int family, int *ok)
{
    *ok = 1;
    if (c->dk == DT_DATE) {
        double days = (c->dscale == 1 ? v : v / c->dscale) + (double)c->depoch;   /* from 1970 */
        if (family == 0 || family == 2) return days + 3653;
        return (days + 141428) * 86400;
    }
    double secs = v / (c->dscale == 1000 ? 1000.0 : 1.0);
    if (c->dk == DT_DATETIME) secs += (double)c->depoch * 86400;          /* from 1970 */
    else if (c->dk != DT_TIME) { *ok = 0; return v; }
    if (family == 0) {
        double ms = (c->dk == DT_TIME ? secs : secs + 3653.0 * 86400) * 1000;
        double r = floor(ms + 0.5);
        if (fabs(ms - r) > 1e-3) *ok = 0;       /* finer than a millisecond */
        return r;
    }
    if (c->dk == DT_TIME) return secs;
    return family == 1 ? secs + 141428.0 * 86400 : secs + 3653.0 * 86400;
}

static const char *date_fmt(int family, DateKind k, int frac)
{
    if (family == 0) return k == DT_DATE ? "%td" : k == DT_TIME ? "%tcHH:MM:SS" : "%tc";
    if (k == DT_DATE) return family == 1 ? "DATE11" : "DATE9";
    if (k == DT_TIME) return frac ? "TIME12.3" : "TIME8";
    return frac ? "DATETIME24.3" : "DATETIME20";
}

static ssize_t to_mem(const void *p, size_t n, void *ctx)
{
    buf_put((Buf *)ctx, p, n);
    return (ssize_t)n;
}

static int pad_family(const char *fmt) { return strcmp(fmt, "dta") != 0; }

static size_t trim_len(const char *s, size_t n) { while (n && s[n - 1] == ' ') n--; return n; }

/* Write the plan with ReadStat. */
static readstat_error_t write_plan(const char *dfmt, PCol *pc, size_t ncols, size_t nrows,
                                   PSet *ps, size_t nps, char **notes, size_t nnotes,
                                   const char *file_label, const char *table, time_t stamp,
                                   int xpt_version, Buf *out)
{
    readstat_writer_t *w = readstat_writer_init();
    if (!w) return READSTAT_ERROR_MALLOC;
    readstat_set_data_writer(w, to_mem);
    readstat_writer_set_error_handler(w, on_error);
    readstat_error_t e = READSTAT_OK;
    readstat_label_set_t **ls = calloc(nps ? nps : 1, sizeof(*ls));
    readstat_variable_t **vv = calloc(ncols ? ncols : 1, sizeof(*vv));
    readstat_string_ref_t ***refs = calloc(ncols ? ncols : 1, sizeof(*refs));
    if (!ls || !vv || !refs) { e = READSTAT_ERROR_MALLOC; goto done; }
    int dta = !strcmp(dfmt, "dta");
    if (file_label && *file_label) readstat_writer_set_file_label(w, file_label);
    if (stamp > 0) readstat_writer_set_file_timestamp(w, stamp);
    if (dta) readstat_writer_set_file_format_version(w, ncols > 32767 ? 119 : 118);
    if (!strcmp(dfmt, "xpt")) {
        readstat_writer_set_file_format_version(w, (uint8_t)xpt_version);
        readstat_writer_set_table_name(w, table && *table ? table : "DATASET");
    }
    if (!strcmp(dfmt, "zsav")) readstat_writer_set_compression(w, READSTAT_COMPRESS_BINARY);
    else if (!strcmp(dfmt, "sav")) readstat_writer_set_compression(w, READSTAT_COMPRESS_ROWS);
    for (size_t i = 0; i < nps; i++) {
        readstat_type_t t = ps[i].string ? READSTAT_TYPE_STRING : dta ? READSTAT_TYPE_INT32 : READSTAT_TYPE_DOUBLE;
        if (!(ls[i] = readstat_add_label_set(w, t, ps[i].name))) { e = READSTAT_ERROR_MALLOC; goto done; }
        for (size_t k = 0; k < ps[i].n; k++) {
            const Sv *v = &ps[i].p[k].v;
            const char *lab = ps[i].p[k].label;
            if (v->k == SV_TAG) readstat_label_tagged_value(ls[i], v->tag, lab);
            else if (v->k == SV_STR) readstat_label_string_value(ls[i], v->s, lab);
            else if (dta) readstat_label_int32_value(ls[i], (int32_t)v->d, lab);
            else readstat_label_double_value(ls[i], v->d, lab);
        }
    }
    for (size_t j = 0; j < ncols; j++) {
        PCol *c = &pc[j];
        readstat_type_t t = c->strl ? READSTAT_TYPE_STRING_REF : c->type;
        vv[j] = readstat_add_variable(w, c->name, t, c->string ? c->width : 0);
        if (!vv[j]) { e = READSTAT_ERROR_MALLOC; goto done; }
        if (c->label) readstat_variable_set_label(vv[j], c->label);
        if (c->format) readstat_variable_set_format(vv[j], c->format);
        if (c->lset >= 0) readstat_variable_set_label_set(vv[j], ls[c->lset]);
        if (c->measure) readstat_variable_set_measure(vv[j], (readstat_measure_t)c->measure);
        if (c->align) readstat_variable_set_alignment(vv[j], (readstat_alignment_t)c->align);
        if (c->dispw > 0) readstat_variable_set_display_width(vv[j], c->dispw);
        for (int i = 0; i < c->nmiss; i++) {
            if (c->mlo[i].k == SV_NUM && c->mhi[i].k == SV_NUM) {
                if (c->mlo[i].d == c->mhi[i].d) readstat_variable_add_missing_double_value(vv[j], c->mlo[i].d);
                else readstat_variable_add_missing_double_range(vv[j], c->mlo[i].d, c->mhi[i].d);
            } else if (c->mlo[i].k == SV_STR && c->mhi[i].k == SV_STR) {
                if (!strcmp(c->mlo[i].s, c->mhi[i].s)) readstat_variable_add_missing_string_value(vv[j], c->mlo[i].s);
                else readstat_variable_add_missing_string_range(vv[j], c->mlo[i].s, c->mhi[i].s);
            }
        }
        if (c->strl) {
            /* a strL column: one reference per non-empty cell, all made
             * before the first row, as Stata's layout needs */
            if (!(refs[j] = calloc(nrows ? nrows : 1, sizeof(**refs)))) { e = READSTAT_ERROR_MALLOC; goto done; }
            for (size_t r = 0; r < nrows; r++) {
                if (!c->len[r]) continue;
                refs[j][r] = readstat_add_string_ref(w, c->s[r]);     /* NUL-ended in the arena */
                if (!refs[j][r]) { e = READSTAT_ERROR_MALLOC; goto done; }
            }
        }
    }
    for (size_t i = 0; i < nnotes; i++) readstat_add_note(w, notes[i]);

    if (dta) e = readstat_begin_writing_dta(w, out, (long)nrows);
    else if (!strcmp(dfmt, "sav") || !strcmp(dfmt, "zsav")) e = readstat_begin_writing_sav(w, out, (long)nrows);
    else if (!strcmp(dfmt, "por")) e = readstat_begin_writing_por(w, out, (long)nrows);
    else if (!strcmp(dfmt, "xpt")) e = readstat_begin_writing_xport(w, out, (long)nrows);
    else e = readstat_begin_writing_sas7bdat(w, out, (long)nrows);
    if (e != READSTAT_OK) goto done;
    if ((e = readstat_validate_metadata(w)) != READSTAT_OK) goto done;
    for (size_t j = 0; j < ncols; j++)
        if ((e = readstat_validate_variable(w, vv[j])) != READSTAT_OK) goto done;

    Buf tmp;
    buf_init(&tmp);
    for (size_t r = 0; r < nrows && e == READSTAT_OK; r++) {
        if ((e = readstat_begin_row(w)) != READSTAT_OK) break;
        for (size_t j = 0; j < ncols && e == READSTAT_OK; j++) {
            PCol *c = &pc[j];
            if (c->string) {
                if (c->strl) {
                    e = c->len[r] ? readstat_insert_string_ref(w, vv[j], refs[j][r])
                                  : readstat_insert_missing_value(w, vv[j]);
                    continue;
                }
                const char *v = c->s[r];
                if (v[c->len[r]]) {               /* cut short: needs its own end */
                    tmp.len = 0;
                    buf_put(&tmp, v, c->len[r]);
                    buf_putc(&tmp, 0);
                    v = (const char *)tmp.data;
                }
                e = readstat_insert_string_value(w, vv[j], v);
            } else if (c->tag[r] == 1) e = readstat_insert_missing_value(w, vv[j]);
            else if (c->tag[r]) e = readstat_insert_tagged_missing_value(w, vv[j], c->tag[r]);
            else switch (c->type) {
                case READSTAT_TYPE_INT8:  e = readstat_insert_int8_value(w, vv[j], (int8_t)c->num[r]); break;
                case READSTAT_TYPE_INT16: e = readstat_insert_int16_value(w, vv[j], (int16_t)c->num[r]); break;
                case READSTAT_TYPE_INT32: e = readstat_insert_int32_value(w, vv[j], (int32_t)c->num[r]); break;
                case READSTAT_TYPE_FLOAT: e = readstat_insert_float_value(w, vv[j], (float)c->num[r]); break;
                default:                  e = readstat_insert_double_value(w, vv[j], c->num[r]);
            }
        }
        if (e == READSTAT_OK) e = readstat_end_row(w);
    }
    buf_free(&tmp);
    if (e == READSTAT_OK) e = readstat_end_writing(w);
done:
    if (refs) for (size_t j = 0; j < ncols; j++) free(refs[j]);
    free(refs); free(vv); free(ls);
    readstat_writer_free(w);
    return e;
}

/* The written file, read back, against the plan. */
static int check_plan(const Buf *out, const char *dfmt, const PCol *pc, size_t ncols, size_t nrows,
                      char *err, size_t cap)
{
    SFile g;
    if (sfile_read(out->data, out->len, dfmt, NULL, &g, err, cap)) return -1;
    int bad = 0;
    if (g.ncols != ncols || (g.nrows != nrows && !(nrows == 0 && g.nrows <= 1))) {
        seterr(err, cap, "the written file has %zu x %zu cells, meant %zu x %zu", g.nrows, g.ncols, nrows, ncols);
        bad = 1;
    }
    int pad = pad_family(dfmt);
    for (size_t j = 0; j < ncols && !bad; j++) {
        const PCol *c = &pc[j];
        const SCol *h = &g.c[j];
        if (strcmp(h->name, c->name)) { seterr(err, cap, "column %zu came back named %s", j + 1, h->name); bad = 1; break; }
        for (size_t r = 0; r < nrows && !bad; r++) {
            if (c->string) {
                size_t a = c->len[r], b = h->len[r];
                const char *hs = (const char *)g.arena.data + h->off[r];
                if (pad) { a = trim_len(c->s[r], a); b = trim_len(hs, b); }
                if (a != b || memcmp(c->s[r], hs, a)) bad = 1;
            } else {
                char t1 = c->tag[r], t2 = h->tag[r];
                if (t1 > 1 && t2 > 1) bad = (t1 | 0x20) != (t2 | 0x20);
                else if (t1 || t2) bad = t1 != t2;
                else {
                    double want = c->type == READSTAT_TYPE_FLOAT ? (double)(float)c->num[r] : c->num[r];
                    bad = h->num[r] != want;
                }
            }
            if (bad) seterr(err, cap, "column %s row %zu did not read back as written", c->name, r + 1);
        }
    }
    sfile_free(&g);
    return bad ? -1 : 0;
}

int stat_translate(const uint8_t *data, size_t n, const char *sfmt, const char *encoding,
                   const char *dfmt, Buf *out, Buf *report, size_t *rows, size_t *cols,
                   char *err, size_t cap)
{
    buf_init(report);
    buf_init(out);
    Dest d;
    if (dest_of(dfmt, &d)) { seterr(err, cap, "cannot write %s files", dfmt); return -1; }
    SFile f;
    if (sfile_read(data, n, sfmt, encoding, &f, err, cap)) return -1;
    int sfam = family_of(sfmt), same = sfam == d.family;
    size_t nr = f.nrows, nc = f.ncols;
    PCol *pc = calloc(nc ? nc : 1, sizeof(PCol));
    PSet *ps = NULL;
    size_t nps = 0;
    char **notes = NULL, **taken = calloc(nc ? nc : 1, sizeof(char *));
    size_t nnotes = 0;
    char *flabel = NULL;
    int rc = -1;
    if (!pc || !taken) { seterr(err, cap, "out of memory"); goto done; }

    /* counts for the report */
    size_t renamed = 0, lab_cut = 0, str_cut_cells = 0, str_cut_cols = 0, strl = 0;
    size_t tag_lost = 0, tag_lost_cols = 0, um_tagged = 0, um_lost = 0, um_kept = 0;
    size_t vl_dropped_cols = 0, vl_pairs_dropped = 0, fmt_dropped = 0, dates = 0, other_dates = 0;
    size_t sub_ms = 0, measures = 0, pad_lost = 0, upcased = 0, widened = 0;

    /* SAS transport: version 5 when everything fits it, else version 8 */
    int xpt_version = 5;
    if (!strcmp(dfmt, "xpt")) {
        for (size_t j = 0; j < nc && xpt_version == 5; j++) {
            SCol *c = &f.c[j];
            char *nm = legal_name(&d, c->name, NULL, 0);
            if (!nm || strcmp(nm, c->name) || (c->label && u8_chars(c->label) > 40)) xpt_version = 8;
            free(nm);
            if (readstat_type_class(c->type) == READSTAT_TYPE_CLASS_STRING)
                for (size_t r = 0; r < nr; r++) if (c->len[r] > 200) { xpt_version = 8; break; }
        }
        if (xpt_version == 8) { d.name_max = 32; d.label_max = 256; d.str_max = 32767; }
    }

    for (size_t j = 0; j < nc; j++) {
        SCol *c = &f.c[j];
        PCol *p = &pc[j];
        p->lset = -1;
        p->name = legal_name(&d, c->name, taken, j);
        if (!p->name) { seterr(err, cap, "out of memory"); goto done; }
        taken[j] = p->name;
        if (strcmp(p->name, c->name) && !strcasecmp(p->name, c->name)) upcased++;
        else if (strcmp(p->name, c->name)) {
            if (renamed < 8) rep(report, "column %s renamed %s (%s naming rules)", c->name, p->name, ppz_stat_name(dfmt));
            renamed++;
        }
        if (c->label) {
            size_t k = u8_cut(c->label, d.label_max, d.family == 0 ? 320 : d.label_max);
            if (k < strlen(c->label)) lab_cut++;
            p->label = strndup(c->label, k);
        }
        p->measure = d.measure ? c->measure : 0;
        if (c->measure && !d.measure) measures++;
        if (d.measure) { p->align = c->align; p->dispw = c->dispw; }

        if (readstat_type_class(c->type) == READSTAT_TYPE_CLASS_STRING) {
            p->string = 1;
            p->type = READSTAT_TYPE_STRING;
            p->s = malloc((nr ? nr : 1) * sizeof(char *));
            p->len = malloc((nr ? nr : 1) * sizeof(size_t));
            if (!p->s || !p->len) { seterr(err, cap, "out of memory"); goto done; }
            size_t longest = 0, cut = 0;
            for (size_t r = 0; r < nr; r++) {
                p->s[r] = (const char *)f.arena.data + c->off[r];
                p->len[r] = c->len[r];
                if (p->len[r] > longest) longest = p->len[r];
            }
            if (longest > d.str_max && !strcmp(dfmt, "dta")) { p->strl = 1; strl++; }
            else if (longest > d.str_max) {
                for (size_t r = 0; r < nr; r++)
                    if (p->len[r] > d.str_max) {
                        /* cut on a character boundary */
                        size_t k = d.str_max;
                        while (k && ((unsigned char)p->s[r][k] & 0xC0) == 0x80) k--;
                        p->len[r] = k;
                        cut++;
                    }
                str_cut_cells += cut;
                str_cut_cols++;
                longest = d.str_max;
            }
            if (pad_family(dfmt))
                for (size_t r = 0; r < nr; r++)
                    if (p->len[r] && p->s[r][p->len[r] - 1] == ' ') pad_lost++;
            size_t sw = c->width;
            if (!strcmp(sfmt, "dta") && sw) sw--;          /* see type_name */
            p->width = longest > sw ? longest : sw;
            if (p->width > d.str_max && !p->strl) p->width = d.str_max;
            if (!p->width) p->width = 1;
        } else {
            p->string = 0;
            /* Stata keeps its own integer types; everything else is a double */
            p->type = (!strcmp(dfmt, "dta") && !strcmp(sfmt, "dta")) ? c->type : READSTAT_TYPE_DOUBLE;
            p->num = c->num; c->num = NULL;        /* taken over, not copied */
            p->tag = c->tag; c->tag = NULL;
            if (c->type == READSTAT_TYPE_FLOAT && p->type == READSTAT_TYPE_DOUBLE) widened++;

            /* dates, re-counted */
            int isdate = c->dk == DT_DATE || c->dk == DT_DATETIME || c->dk == DT_TIME;
            if (isdate && !same) {
                int frac = 0;
                for (size_t r = 0; r < nr; r++) {
                    if (p->tag[r]) continue;
                    int ok;
                    double v = date_to(p->num[r], c, d.family, &ok);
                    if (!ok) sub_ms++;
                    p->num[r] = v;
                    if (d.family != 0 && c->dk != DT_DATE && v != floor(v)) frac = 1;
                }
                p->format = sdup(date_fmt(d.family, c->dk, frac));
                dates++;
            } else if (same) {
                if (c->format) p->format = sdup(c->format);
            } else if (c->dk == DT_OTHER) {
                other_dates++;
            } else {
                int w, dd;
                NumFmt k = num_fmt(sfam, c->format, &w, &dd);
                char fb[32];
                if (k == NF_FIXED || k == NF_COMMA) {
                    num_fmt_put(d.family, k, w, dd, fb, sizeof(fb));
                    if (*fb) p->format = sdup(fb);
                } else if (k == NF_OTHER) fmt_dropped++;
            }

            /* missing values */
            size_t lost_here = 0;
            for (size_t r = 0; r < nr; r++) {
                char t = p->tag[r];
                if (t <= 1) continue;
                if (d.tags == 'a' && t >= 'A' && t <= 'Z') p->tag[r] = (char)(t + 32);
                else if (d.tags == 'A' && t >= 'a' && t <= 'z') p->tag[r] = (char)(t - 32);
                else if (d.tags == 'A' && t == '_') ;
                else if (!d.tags || t == '_') { p->tag[r] = 1; lost_here++; }
            }
            tag_lost += lost_here;
            if (lost_here) tag_lost_cols++;
        }

        /* user-defined missing codes: kept as codes (SPSS), turned into
         * extended missing values (Stata, SAS transport), or left as plain
         * values with a note (SAS) */
        if (c->nmiss) {
            if (d.user_missing) {
                p->mlo = c->mlo; p->mhi = c->mhi; p->nmiss = c->nmiss;
                um_kept++;
            } else if (d.tags && !p->string) {
                char base = (char)d.tags;
                for (size_t r = 0; r < nr; r++) {
                    if (p->tag[r]) continue;
                    for (int i = 0; i < c->nmiss && i < 26; i++)
                        if (c->mlo[i].k == SV_NUM && c->mhi[i].k == SV_NUM &&
                            p->num[r] >= c->mlo[i].d && p->num[r] <= c->mhi[i].d) {
                            p->tag[r] = (char)(base + i);
                            break;
                        }
                }
                if (um_tagged < 3) {
                    char line[400];
                    size_t at = (size_t)snprintf(line, sizeof(line), "%s:", p->name);
                    for (int i = 0; i < c->nmiss && i < 26 && at < sizeof(line) - 60; i++) {
                        if (c->mlo[i].k != SV_NUM || c->mhi[i].k != SV_NUM) continue;
                        char lo[32], hi[32];
                        fmt_double(lo, sizeof(lo), c->mlo[i].d);
                        fmt_double(hi, sizeof(hi), c->mhi[i].d);
                        if (c->mlo[i].d == c->mhi[i].d)
                            at += (size_t)snprintf(line + at, sizeof(line) - at, "%s %s -> .%c", i ? "," : "", lo, base + i);
                        else
                            at += (size_t)snprintf(line + at, sizeof(line) - at, "%s %s to %s -> .%c", i ? "," : "", lo, hi, base + i);
                    }
                    rep(report, "%s", line);
                }
                um_tagged++;
            } else um_lost++;
        }
    }

    /* value labels */
    for (size_t j = 0; j < nc; j++) {
        SCol *c = &f.c[j];
        PCol *p = &pc[j];
        if (!c->lset) continue;
        SvSet *s = NULL;
        for (size_t i = 0; i < f.nsets; i++) if (!strcmp(f.sets[i].name, c->lset)) { s = &f.sets[i]; break; }
        if (!s) continue;
        if (!d.labels || (p->string && !strcmp(dfmt, "dta"))) { vl_dropped_cols++; continue; }
        /* a column whose missing codes became letters gets a set of its own,
         * so the codes' labels can follow them */
        int own = c->nmiss && !d.user_missing && d.tags && !p->string;
        int at = -1;
        if (!own) for (size_t i = 0; i < nps; i++) if (!strcmp(ps[i].name, s->name)) { at = (int)i; break; }
        if (at < 0) {
            PSet *nps2 = realloc(ps, (nps + 1) * sizeof(PSet));
            if (!nps2) { seterr(err, cap, "out of memory"); goto done; }
            ps = nps2;
            PSet *q = &ps[nps];
            memset(q, 0, sizeof(*q));
            char want[80];
            snprintf(want, sizeof(want), own ? "%s_%s" : "%s", s->name, p->name);
            char **tk = calloc(nps + 1, sizeof(char *));
            if (!tk) { seterr(err, cap, "out of memory"); goto done; }
            for (size_t i = 0; i < nps; i++) tk[i] = ps[i].name;
            q->name = legal_name(&d, want, tk, nps);
            free(tk);
            q->string = p->string;
            q->p = calloc(s->n ? s->n : 1, sizeof(SvPair));
            if (!q->name || !q->p) { seterr(err, cap, "out of memory"); goto done; }
            for (size_t k = 0; k < s->n; k++) {
                Sv v = s->p[k].v;
                int keep = 1;
                if (v.k == SV_TAG) {
                    char t = v.tag;
                    if (d.tags == 'a' && t >= 'A' && t <= 'Z') t = (char)(t + 32);
                    else if (d.tags == 'A' && t >= 'a' && t <= 'z') t = (char)(t - 32);
                    else if (!d.tags || t == '_') keep = 0;
                    v.tag = t;
                } else if (v.k == SV_NUM && own) {
                    for (int i = 0; i < c->nmiss && i < 26; i++)
                        if (c->mlo[i].k == SV_NUM && v.d >= c->mlo[i].d && v.d <= c->mhi[i].d) {
                            v.k = SV_TAG; v.tag = (char)(d.tags + i); break;
                        }
                }
                if (keep && v.k == SV_NUM && !strcmp(dfmt, "dta"))
                    keep = v.d == floor(v.d) && v.d >= -2147483647.0 && v.d <= 2147483620.0;
                if (keep && (v.k == SV_STR) != (q->string != 0)) keep = 0;
                if (keep && v.k == SV_SYS) keep = 0;
                if (!keep) { vl_pairs_dropped++; continue; }
                /* duplicate keys (two codes that became one letter) keep the first */
                int dup = 0;
                for (size_t m = 0; m < q->n && !dup; m++) {
                    const Sv *u = &q->p[m].v;
                    dup = u->k == v.k && (v.k == SV_TAG ? u->tag == v.tag : v.k == SV_STR ? !strcmp(u->s, v.s) : u->d == v.d);
                }
                if (dup) { vl_pairs_dropped++; continue; }
                q->p[q->n].v = v;
                q->p[q->n].label = s->p[k].label;
                q->n++;
            }
            at = (int)nps++;
        }
        p->lset = at;
    }

    /* notes */
    if (f.nnotes) {
        if (!d.notes) rep(report, "%zu note%s dropped (%s files have none)", f.nnotes, plural(f.nnotes), ppz_stat_name(dfmt));
        else for (size_t i = 0; i < f.nnotes; i++) {
            const char *s = f.notes[i];
            size_t len = strlen(s);
            do {
                size_t k = d.notes == 80 ? u8_cut(s, 80, 80) : len;
                if (!k) k = len;              /* valid UTF-8 never gets here */
                char **nn = realloc(notes, (nnotes + 1) * sizeof(char *));
                if (!nn) { seterr(err, cap, "out of memory"); goto done; }
                notes = nn;
                notes[nnotes++] = strndup(s, k);
                s += k; len -= k;
            } while (len && d.notes == 80);
        }
    }
    if (f.file_label) {
        size_t k = u8_cut(f.file_label, d.file_label_max, d.family == 0 ? 320 : d.file_label_max);
        flabel = strndup(f.file_label, k);
        if (k < strlen(f.file_label)) rep(report, "the file label cut to %zu characters", d.file_label_max);
    }

    if (renamed > 8) rep(report, "... %zu columns renamed in all", renamed);
    if (upcased) rep(report, "%zu column name%s written in capitals (%s names have no lower case)",
                     upcased, plural(upcased), ppz_stat_name(dfmt));
    if (widened) rep(report, "%zu float column%s stored as doubles: the same numbers, but they print "
                     "with more digits (10.333334 shows as 10.333333969116211)", widened, plural(widened));
    if (dates) rep(report, "%zu date/time column%s re-counted for %s (values change, dates do not)",
                   dates, plural(dates), ppz_stat_name(dfmt));
    if (sub_ms) rep(report, "%zu time%s finer than a millisecond rounded to one (Stata counts milliseconds)",
                    sub_ms, plural(sub_ms));
    if (other_dates) rep(report, "%zu week/month/quarter/year column%s written as plain numbers",
                         other_dates, plural(other_dates));
    if (um_tagged) rep(report, "user-defined missing values on %zu column%s became extended missing "
                       "values (%s, ...)", um_tagged, plural(um_tagged), d.tags == 'a' ? ".a, .b" : ".A, .B");
    if (um_kept && sfam != 1) rep(report, "missing codes on %zu column%s kept", um_kept, plural(um_kept));
    if (um_lost) rep(report, "user-defined missing values on %zu column%s written as plain values",
                     um_lost, plural(um_lost));
    if (tag_lost) rep(report, "%zu extended missing value%s (.a to .z) written as system missing, in %zu column%s",
                      tag_lost, plural(tag_lost), tag_lost_cols, plural(tag_lost_cols));
    if (vl_dropped_cols) rep(report, "value labels on %zu column%s dropped (%s)", vl_dropped_cols,
                             plural(vl_dropped_cols), d.labels ? "Stata labels numbers only"
                             : "SAS keeps them in a separate catalog file");
    if (vl_pairs_dropped) rep(report, "%zu value label%s dropped (keys %s cannot hold)", vl_pairs_dropped,
                              plural(vl_pairs_dropped), ppz_stat_name(dfmt));
    if (lab_cut) rep(report, "%zu variable label%s cut to %zu characters", lab_cut, plural(lab_cut), d.label_max);
    if (strl) rep(report, "%zu text column%s longer than 2045 bytes stored as strL", strl, plural(strl));
    if (str_cut_cells) rep(report, "%zu text value%s in %zu column%s CUT to %zu bytes -- %s holds no longer text",
                           str_cut_cells, plural(str_cut_cells), str_cut_cols, plural(str_cut_cols),
                           d.str_max, ppz_stat_name(dfmt));
    if (pad_lost) rep(report, "%zu text value%s lose trailing spaces (%s pads text with spaces)",
                      pad_lost, plural(pad_lost), ppz_stat_name(dfmt));
    if (fmt_dropped) rep(report, "%zu display format%s with no %s equivalent left to the default",
                         fmt_dropped, plural(fmt_dropped), ppz_stat_name(dfmt));
    if (measures) rep(report, "measure levels (nominal/ordinal/scale) dropped");
    if (!strcmp(dfmt, "xpt") && xpt_version == 8)
        rep(report, "written as SAS transport version 8: names, labels or text too long for version 5");

    g_rs_err[0] = 0;
    readstat_error_t e = write_plan(dfmt, pc, nc, nr, ps, nps, notes, nnotes, flabel,
                                    f.table, f.stamp, xpt_version, out);
    if (e != READSTAT_OK) {
        seterr(err, cap, "cannot write this as a %s file: %s%s%s", ppz_stat_name(dfmt),
               readstat_error_message(e), g_rs_err[0] ? " -- " : "", g_rs_err);
        goto done;
    }
    if (check_plan(out, dfmt, pc, nc, nr, err, cap)) {
        char e2[600];
        snprintf(e2, sizeof(e2), "the %s file did not read back as written (%s) -- nothing written",
                 ppz_stat_name(dfmt), err);
        seterr(err, cap, "%s", e2);
        goto done;
    }
    if (rows) *rows = nr;
    if (cols) *cols = nc;
    rc = 0;
done:
    if (rc) { buf_free(out); buf_free(report); }
    if (pc) for (size_t j = 0; j < nc; j++) {
        free(pc[j].name); free(pc[j].label); free(pc[j].format);
        free(pc[j].num); free(pc[j].tag); free((void *)pc[j].s); free(pc[j].len);
    }
    free(pc);
    for (size_t i = 0; i < nps; i++) { free(ps[i].name); free(ps[i].p); }
    free(ps);
    for (size_t i = 0; i < nnotes; i++) free(notes[i]);
    free(notes);
    free(taken);
    free(flabel);
    sfile_free(&f);
    return rc;
}
