/* Stata, SPSS and SAS files, read through ReadStat (vendored in readstat/).
 *
 * These formats are not compressed by the table codec. A .dta or .sav holds
 * more than a table -- variable and value labels, display formats, notes,
 * user-defined missing values -- and no table format can carry all of it
 * back, so an archive of one keeps the ORIGINAL BYTES (xz -9e) plus a JSON
 * schema of everything ReadStat found in them. Restoring to the same format
 * gives the file back byte for byte; restoring or converting to CSV, TSV,
 * JSON or JSON Lines is a translation, and the program says what the
 * translation leaves behind (stat_loss_print).
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
} Rd;

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
    if (rows > 0 && grow_rows(r, (size_t)rows < ((size_t)1 << 24) ? (size_t)rows : ((size_t)1 << 24))) {
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

static int on_value(int obs, readstat_variable_t *var, readstat_value_t v, void *ctx)
{
    Rd *r = ctx;
    int j = readstat_variable_get_index(var);
    if (obs < 0 || j < 0 || (size_t)j >= r->ncols) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
    if ((size_t)obs >= r->nrows) {
        if (grow_rows(r, (size_t)obs + 1)) { r->failed = 1; return READSTAT_HANDLER_ABORT; }
        r->nrows = (size_t)obs + 1;
    }
    size_t k = (size_t)obs * r->ncols + (size_t)j;
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

int stat_read(const uint8_t *data, size_t n, const char *fmt, const char *encoding,
              Table *t, Buf *schema, StatLoss *loss, char *err, size_t cap)
{
    table_init(t);
    Rd r;
    memset(&r, 0, sizeof(r));
    r.fmt = fmt;
    buf_init(&r.arena); buf_init(&r.cols); buf_init(&r.notes);

    readstat_parser_t *p = readstat_parser_init();
    if (!p) { seterr(err, cap, "out of memory"); return -1; }
    MemIo io = { data, n, 0 };
    readstat_set_open_handler(p, mem_open);
    readstat_set_close_handler(p, mem_close);
    readstat_set_seek_handler(p, mem_seek);
    readstat_set_read_handler(p, mem_read);
    readstat_set_update_handler(p, mem_update);
    readstat_set_io_ctx(p, &io);
    readstat_set_metadata_handler(p, on_metadata);
    readstat_set_note_handler(p, on_note);
    readstat_set_variable_handler(p, on_variable);
    readstat_set_value_handler(p, on_value);
    readstat_set_value_label_handler(p, on_value_label);
    readstat_set_error_handler(p, on_error);
    if (encoding) readstat_set_file_character_encoding(p, encoding);

    g_rs_err[0] = 0;
    readstat_error_t e;
    if (!strcmp(fmt, "dta"))                               e = readstat_parse_dta(p, "", &r);
    else if (!strcmp(fmt, "sav") || !strcmp(fmt, "zsav"))  e = readstat_parse_sav(p, "", &r);
    else if (!strcmp(fmt, "por"))                          e = readstat_parse_por(p, "", &r);
    else if (!strcmp(fmt, "sas7bdat"))                     e = readstat_parse_sas7bdat(p, "", &r);
    else if (!strcmp(fmt, "xpt"))                          e = readstat_parse_xport(p, "", &r);
    else { readstat_parser_free(p); seterr(err, cap, "unknown format %s", fmt); return -1; }
    readstat_parser_free(p);

    const char *what = ppz_stat_name(fmt);
    if (e != READSTAT_OK || r.failed || !r.names) {
        if (r.failed && e == READSTAT_OK) seterr(err, cap, "out of memory reading the %s file", what);
        else seterr(err, cap, "cannot read this %s file: %s%s%s", what,
                    readstat_error_message(e), g_rs_err[0] ? " -- " : "", g_rs_err);
        rd_free(&r);
        return -1;
    }
    for (size_t j = 0; j < r.ncols; j++)
        if (!r.names[j]) {
            seterr(err, cap, "cannot read this %s file: column %zu has no name", what, j + 1);
            rd_free(&r);
            return -1;
        }

    /* strict UTF-8 out, as every other reader: a file whose strings did not
     * convert is refused, not repaired */
    int bad = 0;
    for (size_t j = 0; j < r.ncols && !bad; j++)
        bad = !valid_utf8(r.names[j], strlen(r.names[j]));
    if (!bad) bad = !valid_utf8((const char *)r.arena.data, r.arena.len);
    if (bad) {
        seterr(err, cap, "this %s file's text is not in the encoding it declares.\n"
               "Name the right one, e.g. --encoding windows-1252", what);
        rd_free(&r);
        return -1;
    }

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
    for (size_t j = 0; j < r.ncols; j++) if (r.tagged_col[j]) r.loss.tagged_cols++;

    if (schema) {
        buf_init(schema);
        char nb[32];
        buf_put(schema, "{\"format\":", 10);
        ppz_json_str(schema, fmt, strlen(fmt));
        buf_putc(schema, ',');
        buf_put(schema, r.meta, strlen(r.meta));
        buf_put(schema, ",\"rows\":", 8);
        buf_put(schema, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", r.nrows));
        buf_put(schema, ",\"columns\":[", 12);
        buf_put(schema, r.cols.data, r.cols.len);
        buf_put(schema, "],\"label_sets\":{", 16);
        for (size_t i = 0; i < r.nsets; i++) {
            if (i) buf_putc(schema, ',');
            ppz_json_str(schema, r.sets[i].name, strlen(r.sets[i].name));
            buf_put(schema, ":[", 2);
            buf_put(schema, r.sets[i].pairs.data, r.sets[i].pairs.len);
            buf_putc(schema, ']');
        }
        buf_put(schema, "},\"notes\":[", 11);
        buf_put(schema, r.notes.data, r.notes.len);
        buf_put(schema, "]}", 2);
    }
    if (loss) *loss = r.loss;
    rd_free(&r);
    return 0;
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

/* PPZ2, with metadata {"original":{"format":F,"bytes":N[,"encoding":E]},
 * "schema":{...}}, the original file as the binary stream and an empty text stream. A build
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

/* rows -> columns (dir 1) or back (dir 0), over d[at .. at + rows*sum(w)) */
static void dta_permute(const uint8_t *in, uint8_t *out, size_t at, size_t rows,
                        const size_t *w, size_t nw, int dir)
{
    size_t rw = 0;
    for (size_t j = 0; j < nw; j++) rw += w[j];
    const uint8_t *src = in + at;
    uint8_t *dst = out + at;
    size_t off = 0, col = 0;
    for (size_t j = 0; j < nw; j++) {
        for (size_t i = 0; i < rows; i++) {
            size_t r = i * rw + off, c = col + i * w[j];
            if (dir) memcpy(dst + c, src + r, w[j]);
            else memcpy(dst + r, src + c, w[j]);
        }
        off += w[j];
        col += rows * w[j];
    }
}

int ppz_encode_original(const uint8_t *data, size_t n, const char *fmt,
                        const char *encoding, const Buf *schema, Buf *out)
{
    Buf meta, mz, bz, tz;
    buf_init(&meta); buf_init(&mz); buf_init(&bz); buf_init(&tz);
    char nb[32];
    buf_put(&meta, "{\"original\":{\"format\":", 22);
    ppz_json_str(&meta, fmt, strlen(fmt));
    buf_put(&meta, ",\"bytes\":", 9);
    buf_put(&meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", n));
    if (encoding) {
        buf_put(&meta, ",\"encoding\":", 12);
        ppz_json_str(&meta, encoding, strlen(encoding));
    }
    /* Stata rows stored as columns, when the layout is the expected one */
    uint8_t *perm = NULL;
    if (!strcmp(fmt, "dta")) {
        Js *sc = js_parse((const char *)schema->data, schema->len);
        size_t rows = 0, nw = 0, *w = NULL;
        size_t at = sc ? dta_layout(data, n, sc, &rows, &w, &nw) : 0;
        js_free(sc);
        if (at && (perm = malloc(n))) {
            memcpy(perm, data, n);
            dta_permute(data, perm, at, rows, w, nw, 1);
            put_s(&meta, ",\"layout\":{\"kind\":\"dta-columns\",\"at\":");
            buf_put(&meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", at));
            put_s(&meta, ",\"rows\":");
            buf_put(&meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", rows));
            put_s(&meta, ",\"widths\":[");
            for (size_t j = 0; j < nw; j++) {
                if (j) buf_putc(&meta, ',');
                buf_put(&meta, nb, (size_t)snprintf(nb, sizeof(nb), "%zu", w[j]));
            }
            put_s(&meta, "]}");
        }
        free(w);
    }
    buf_put(&meta, "},\"schema\":", 11);
    buf_put(&meta, schema->data, schema->len);
    buf_putc(&meta, '}');
    int rc = -1;
    if (ppz_lzma_compress(meta.data, meta.len, &mz)) goto done;
    if (ppz_lzma_compress_as(perm ? perm : data, n, &bz, PPZ_XZ_PLAIN)) goto done;
    if (ppz_lzma_compress((const uint8_t *)"", 0, &tz)) goto done;
    if (mz.len > 0xFFFFFFFFu || bz.len > 0xFFFFFFFFu) goto done;
    buf_free(out);
    buf_put(out, PPZ_MAGIC, 4);
    size_t lens[3] = { mz.len, bz.len, tz.len };
    for (int i = 0; i < 3; i++)
        for (int s = 24; s >= 0; s -= 8) buf_putc(out, (char)((lens[i] >> s) & 0xFF));
    buf_put(out, mz.data, mz.len);
    buf_put(out, bz.data, bz.len);
    buf_put(out, tz.data, tz.len);
    rc = 0;
done:
    free(perm);
    buf_free(&meta); buf_free(&mz); buf_free(&bz); buf_free(&tz);
    return rc;
}

/* Put a stored layout back to the original order. The layout comes from
 * the archive, so every number in it is checked against the bytes it is
 * about to move before anything moves. */
static int undo_layout(const Js *lay, Buf *b)
{
    if (!lay) return 0;
    const Js *k = js_get(lay, "kind"), *w = js_get(lay, "widths");
    int64_t at = js_i64(js_get(lay, "at"), -1), rows = js_i64(js_get(lay, "rows"), -1);
    if (lay->kind != JS_OBJ || !k || k->kind != JS_STR || strcmp(k->str, "dta-columns") ||
        !w || w->kind != JS_ARR || !w->count || w->count > DTA_MAX_FIELDS ||
        at < 0 || rows < 0 || (uint64_t)at > b->len)
        return -1;
    size_t *ws = malloc(w->count * sizeof(size_t)), rw = 0;
    if (!ws) return -1;
    for (size_t j = 0; j < w->count; j++) {
        int64_t x = js_i64(&w->items[j], 0);
        if (w->items[j].kind != JS_NUM || x < 1 || x > 2045) { free(ws); return -1; }
        ws[j] = (size_t)x;
        rw += ws[j];
    }
    size_t room = b->len - (size_t)at;
    if ((uint64_t)rows > room / rw) { free(ws); return -1; }
    uint8_t *back = malloc(b->len ? b->len : 1);
    if (!back) { free(ws); return -1; }
    memcpy(back, b->data, b->len);
    dta_permute(b->data, back, (size_t)at, (size_t)rows, ws, w->count, 0);
    memcpy(b->data, back, b->len);
    free(back);
    free(ws);
    return 0;
}

int ppz_original(const uint8_t *blob, size_t n, Buf *orig, char *fmt, size_t fcap, Js **meta_out)
{
    if (meta_out) *meta_out = NULL;
    if (n < 16 || memcmp(blob, PPZ_MAGIC, 4)) return -1;
    size_t ml = ((size_t)blob[4] << 24) | ((size_t)blob[5] << 16) | ((size_t)blob[6] << 8) | blob[7];
    size_t bl = ((size_t)blob[8] << 24) | ((size_t)blob[9] << 16) | ((size_t)blob[10] << 8) | blob[11];
    if (ml > n - 16 || bl > n - 16 - ml) return -1;
    Buf mb;
    buf_init(&mb);
    if (ppz_lzma_decompress(blob + 16, ml, &mb)) return -1;
    Js *meta = js_parse((const char *)mb.data, mb.len);
    buf_free(&mb);
    if (!meta) return -1;
    const Js *o = js_get(meta, "original");
    if (!o) { js_free(meta); return 0; }
    const Js *f = js_get(o, "format");
    const Js *nbytes = js_get(o, "bytes");
    if (o->kind != JS_OBJ || !f || f->kind != JS_STR || !ppz_stat_name(f->str) ||
        !nbytes || nbytes->kind != JS_NUM || !nbytes->is_int || nbytes->inum < 0 ||
        strlen(f->str) >= fcap) {
        js_free(meta);
        return -1;
    }
    if (orig) {
        if (ppz_lzma_decompress(blob + 16 + ml, bl, orig) ||
            (int64_t)orig->len != nbytes->inum ||
            undo_layout(js_get(o, "layout"), orig)) {
            buf_free(orig);
            js_free(meta);
            return -1;
        }
    }
    snprintf(fmt, fcap, "%s", f->str);
    if (meta_out) *meta_out = meta;
    else js_free(meta);
    return 1;
}
