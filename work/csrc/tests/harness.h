/* The shared test harness: counters, a table builder, temp files, and a way
 * to run something in a child process that is killed if it crashes, hangs or
 * eats memory.
 *
 * Header-only on purpose. Every test program is one .c file plus this, linked
 * straight against the codec sources (never ppz_main.c -- the CLI is tested by
 * running the built binary), so a test can reach anything ppz.h declares.
 *
 * Why the child runner exists: invariant 3 says the decoder treats its input
 * as hostile, and the failure being tested for -- a segfault, a loop that never
 * ends, a buffer doubling toward a terabyte -- is precisely the one that takes
 * the test process (or the machine) down with it if it happens in-process.
 *
 * The memory cap is the awkward part on this Mac. RLIMIT_AS and RLIMIT_DATA
 * both exist in the headers, and setrlimit() refuses both with EINVAL on
 * macOS (checked 2026-09-29, Darwin 25), so a child cannot be capped from
 * inside. The parent therefore WATCHES the child instead: it polls the
 * child's physical footprint every millisecond or so and SIGKILLs it past the
 * cap, and a wall-clock timeout covers the hang. On Linux the child also sets
 * RLIMIT_AS, which is enforced there.
 */

#ifndef PPZ_HARNESS_H
#define PPZ_HARNESS_H

#include "../ppz.h"

#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifdef __APPLE__
#include <libproc.h>
#endif

/* every program uses a different subset of what is here */
#pragma GCC diagnostic ignored "-Wunused-function"

extern char **environ;

/* ------------------------------------------------------------ counters */

static int h_pass, h_fail;
static const char *h_suite = "?";

static void h_vfail(const char *file, int line, const char *fmt, va_list ap)
{
    printf("  FAIL %s:%d: ", file, line);
    vprintf(fmt, ap);
    putchar('\n');
    fflush(stdout);
}

static int h_check(int ok, const char *file, int line, const char *fmt, ...)
{
    if (ok) { h_pass++; return 1; }
    h_fail++;
    va_list ap;
    va_start(ap, fmt);
    h_vfail(file, line, fmt, ap);
    va_end(ap);
    return 0;
}

/* CHECK(condition, printf-style message) -- counts one check either way and
 * returns whether it passed, so a caller can skip checks that depend on it. */
#define CHECK(cond, ...) h_check(!!(cond), __FILE__, __LINE__, __VA_ARGS__)

static double h_now(void);

/* A heading, with the time the previous section took. */
static void h_section(const char *name)
{
    static double last = 0;
    double t = h_now();
    if (last > 0) printf("   (%.1f s)\n", t - last);
    last = t;
    printf("-- %s\n", name);
    fflush(stdout);
}

/* The last line of every test program; run.sh adds these up. */
static int h_done(void)
{
    printf("RESULT %s %d %d\n", h_suite, h_pass, h_fail);
    fflush(stdout);
    return h_fail ? 1 : 0;
}

static double h_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

/* ------------------------------------------------------- deterministic rng */

/* splitmix64: seeded, portable, and the same sequence on every machine, so a
 * failing fuzz case is reproducible from its seed and index alone. */
typedef struct { uint64_t s; } Rng;

static uint64_t rng_next(Rng *r)
{
    uint64_t z = (r->s += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

/* uniform in [0, n) -- n > 0 */
static uint64_t rng_below(Rng *r, uint64_t n) { return rng_next(r) % n; }

/* uniform in [lo, hi] */
static int64_t rng_range(Rng *r, int64_t lo, int64_t hi)
{
    return lo + (int64_t)(rng_next(r) % (uint64_t)(hi - lo + 1));
}

static double rng_unit(Rng *r) { return (double)(rng_next(r) >> 11) / 9007199254740992.0; }

/* ----------------------------------------------------------- table builder */

/* Cells point into one arena, and the arena moves as it grows, so the builder
 * records offsets and turns them into pointers only at tb_finish. */
typedef struct {
    size_t  ncols;
    char  **names;
    Buf     arena;
    size_t *off, *len;
    size_t  n, cap;
} TB;

static void tb_start(TB *b, size_t ncols, const char *const *names)
{
    memset(b, 0, sizeof(*b));
    b->ncols = ncols;
    b->names = calloc(ncols ? ncols : 1, sizeof(char *));
    for (size_t j = 0; j < ncols; j++) {
        if (names) b->names[j] = strdup(names[j]);
        else {
            char nm[32];
            snprintf(nm, sizeof(nm), "c%zu", j);
            b->names[j] = strdup(nm);
        }
    }
    buf_init(&b->arena);
    buf_need(&b->arena, 1);
}

static void tb_cell(TB *b, const void *p, size_t n)
{
    if (b->n == b->cap) {
        b->cap = b->cap ? b->cap * 2 : 64;
        b->off = realloc(b->off, b->cap * sizeof(size_t));
        b->len = realloc(b->len, b->cap * sizeof(size_t));
        if (!b->off || !b->len) { fprintf(stderr, "harness: out of memory\n"); exit(2); }
    }
    b->off[b->n] = b->arena.len;
    b->len[b->n] = n;
    buf_put(&b->arena, p, n);
    b->n++;
}

static void tb_cellz(TB *b, const char *s) { tb_cell(b, s, strlen(s)); }

static void tb_cellf(TB *b, const char *fmt, ...)
{
    char tmp[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    tb_cell(b, tmp, (size_t)n);
}

/* A row of NUL-terminated cells, ended by NULL. */
static void tb_row(TB *b, ...)
{
    va_list ap;
    va_start(ap, b);
    const char *s;
    while ((s = va_arg(ap, const char *)) != NULL) tb_cellz(b, s);
    va_end(ap);
}

static void tb_finish(TB *b, Table *t)
{
    table_init(t);
    t->ncols = b->ncols;
    t->nrows = b->ncols ? b->n / b->ncols : 0;
    t->names = b->names;
    t->arena = b->arena;
    size_t nc = t->nrows * t->ncols;
    t->cells = calloc(nc ? nc : 1, sizeof(Str));
    for (size_t k = 0; k < nc; k++) {
        t->cells[k].p = (const char *)t->arena.data + b->off[k];
        t->cells[k].n = b->len[k];
    }
    free(b->off);
    free(b->len);
    memset(b, 0, sizeof(*b));
}

/* A deep copy, so a test can hand a table to something that frees it. */
static void table_copy(const Table *src, Table *dst)
{
    TB b;
    tb_start(&b, src->ncols, (const char *const *)src->names);
    for (size_t k = 0; k < src->nrows * src->ncols; k++)
        tb_cell(&b, src->cells[k].p, src->cells[k].n);
    tb_finish(&b, dst);
}

/* Printable rendering of some bytes for a failure message. */
static const char *h_show(const char *p, size_t n, char *out, size_t cap)
{
    size_t o = 0;
    if (cap < 8) { if (cap) out[0] = 0; return out; }
    out[o++] = '"';
    for (size_t i = 0; i < n && o + 8 < cap; i++) {
        unsigned char c = (unsigned char)p[i];
        if (c >= 0x20 && c < 0x7f && c != '"' && c != '\\') out[o++] = (char)c;
        else o += (size_t)snprintf(out + o, cap - o, "\\x%02x", c);
    }
    if (o + 5 < cap && n > 0 && o + 8 >= cap) { memcpy(out + o, "...", 3); o += 3; }
    out[o++] = '"';
    out[o] = 0;
    return out;
}

/* Same table, cell for cell? On a difference, `why` says where. */
static int table_same(const Table *a, const Table *b, char *why, size_t cap)
{
    char s1[160], s2[160];
    if (a->ncols != b->ncols) {
        snprintf(why, cap, "%zu columns vs %zu", a->ncols, b->ncols);
        return 0;
    }
    for (size_t j = 0; j < a->ncols; j++)
        if (strcmp(a->names[j], b->names[j])) {
            snprintf(why, cap, "column %zu named %s vs %s", j,
                     h_show(a->names[j], strlen(a->names[j]), s1, sizeof(s1)),
                     h_show(b->names[j], strlen(b->names[j]), s2, sizeof(s2)));
            return 0;
        }
    if (a->nrows != b->nrows) {
        snprintf(why, cap, "%zu rows vs %zu", a->nrows, b->nrows);
        return 0;
    }
    for (size_t i = 0; i < a->nrows; i++)
        for (size_t j = 0; j < a->ncols; j++) {
            Str x = table_at(a, i, j), y = table_at(b, i, j);
            if (x.n != y.n || (x.n && memcmp(x.p, y.p, x.n))) {
                snprintf(why, cap, "row %zu col %zu: %s vs %s", i, j,
                         h_show(x.p, x.n, s1, sizeof(s1)),
                         h_show(y.p, y.n, s2, sizeof(s2)));
                return 0;
            }
        }
    if (cap) why[0] = 0;
    return 1;
}

/* -------------------------------------------------------------- temp files */

static char h_tmpdir[1024];

/* A fresh directory under $TMPDIR; everything a test writes goes in it. */
static const char *tmpdir_make(const char *tag)
{
    const char *base = getenv("TMPDIR");
    if (!base || !*base) base = "/tmp";
    size_t bl = strlen(base);
    snprintf(h_tmpdir, sizeof(h_tmpdir), "%s%sppz-%s-XXXXXX", base,
             bl && base[bl - 1] == '/' ? "" : "/", tag);
    if (!mkdtemp(h_tmpdir)) {
        fprintf(stderr, "harness: mkdtemp %s: %s\n", h_tmpdir, strerror(errno));
        exit(2);
    }
    return h_tmpdir;
}

static int rm_one(const char *p, const struct stat *sb, int flag, struct FTW *ftw)
{
    (void)sb; (void)flag; (void)ftw;
    return remove(p);
}

static void tmpdir_remove(void)
{
    if (!h_tmpdir[0]) return;
    if (getenv("PPZ_KEEP_TMP")) { printf("  (kept %s)\n", h_tmpdir); return; }
    nftw(h_tmpdir, rm_one, 16, FTW_DEPTH | FTW_PHYS);
    h_tmpdir[0] = 0;
}

/* tpath("x.csv") -> "<tmpdir>/x.csv". 64 rotating buffers, so a call can
 * take a few of these as arguments at once -- but a path kept across a loop
 * must be copied. */
static const char *tpath(const char *fmt, ...)
{
    static char bufs[64][1280];
    static int at = 0;
    char name[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(name, sizeof(name), fmt, ap);
    va_end(ap);
    char *b = bufs[at++ & 63];
    snprintf(b, sizeof(bufs[0]), "%s/%s", h_tmpdir, name);
    return b;
}

static int write_bytes(const char *path, const void *p, size_t n)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    size_t w = n ? fwrite(p, 1, n, f) : 0;
    int bad = fclose(f);
    return (w == n && !bad) ? 0 : -1;
}

static int write_str(const char *path, const char *s) { return write_bytes(path, s, strlen(s)); }

static int read_bytes(const char *path, Buf *out)
{
    buf_init(out);
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    uint8_t chunk[65536];
    size_t got;
    while ((got = fread(chunk, 1, sizeof(chunk), f)) > 0) buf_put(out, chunk, got);
    fclose(f);
    return 0;
}

static int file_exists(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0;
}

/* Anything in the temp dir whose name starts with `prefix`? Used to prove a
 * failed write left no temp file behind either (writers write NAME.partXXXXXX
 * and rename). */
#include <dirent.h>
static int dir_has_prefix(const char *prefix)
{
    DIR *d = opendir(h_tmpdir);
    if (!d) return 0;
    struct dirent *e;
    int found = 0;
    size_t pl = strlen(prefix);
    while ((e = readdir(d)) != NULL)
        if (!strncmp(e->d_name, prefix, pl)) { found = 1; break; }
    closedir(d);
    return found;
}

/* ------------------------------------------------------------ child runner */

typedef struct {
    int    exited;       /* ended by exit(), not a signal */
    int    code;         /* its exit status */
    int    sig;          /* the signal that ended it, if any */
    int    timed_out;    /* we killed it: too slow */
    int    mem_killed;   /* we killed it: too much memory */
    size_t peak;         /* the most memory we saw it use, bytes */
    double secs;
} ChildResult;

static size_t child_footprint(pid_t pid)
{
#ifdef __APPLE__
    struct rusage_info_v2 ri;
    if (proc_pid_rusage(pid, RUSAGE_INFO_V2, (rusage_info_t *)&ri) == 0)
        return (size_t)ri.ri_phys_footprint;
    return 0;
#else
    char p[64];
    snprintf(p, sizeof(p), "/proc/%d/statm", (int)pid);
    FILE *f = fopen(p, "r");
    if (!f) return 0;
    unsigned long size = 0, rss = 0;
    if (fscanf(f, "%lu %lu", &size, &rss) != 2) rss = 0;
    fclose(f);
    return (size_t)rss * (size_t)sysconf(_SC_PAGESIZE);
#endif
}

/* Wait for `pid`, killing it past `timeout_ms` or `memcap` bytes. */
static void child_wait(pid_t pid, int timeout_ms, size_t memcap, ChildResult *r)
{
    memset(r, 0, sizeof(*r));
    double t0 = h_now();
    int st = 0;
    useconds_t nap = 200;
    for (;;) {
        pid_t w = waitpid(pid, &st, WNOHANG);
        if (w == pid) break;
        if (w < 0 && errno != EINTR) break;
        size_t fp = child_footprint(pid);
        if (fp > r->peak) r->peak = fp;
        if (memcap && fp > memcap && !r->mem_killed) {
            r->mem_killed = 1;
            kill(pid, SIGKILL);
        }
        if (!r->timed_out && (h_now() - t0) * 1000.0 > timeout_ms) {
            r->timed_out = 1;
            kill(pid, SIGKILL);
        }
        usleep(nap);
        if (nap < 2000) nap += 200;
    }
    r->secs = h_now() - t0;
    if (WIFEXITED(st)) { r->exited = 1; r->code = WEXITSTATUS(st); }
    else if (WIFSIGNALED(st)) r->sig = WTERMSIG(st);
}

static const char *child_describe(const ChildResult *r, char *buf, size_t cap)
{
    if (r->mem_killed)
        snprintf(buf, cap, "killed past the memory cap (%.0f MB seen)", r->peak / 1048576.0);
    else if (r->timed_out) snprintf(buf, cap, "killed after %.1f s: hung", r->secs);
    else if (!r->exited) snprintf(buf, cap, "died of signal %d (%s)", r->sig, strsignal(r->sig));
    else snprintf(buf, cap, "exit %d", r->code);
    return buf;
}

/* Ran to completion by itself -- any exit code, but no signal, no kill. */
static int child_survived(const ChildResult *r)
{
    return r->exited && !r->timed_out && !r->mem_killed;
}

typedef int (*ChildFn)(void *arg);

/* fn(arg) in a forked child; its return value is the exit code. The child's
 * stdout and stderr go to /dev/null unless PPZ_CHILD_STDERR is set (the
 * decoder prints to stderr when it refuses an unknown column kind). The
 * caller must not have encoder threads running -- fork copies only the
 * calling thread. */
static void run_in_child(ChildFn fn, void *arg, int timeout_ms, size_t memcap,
                         ChildResult *r)
{
    fflush(stdout);
    fflush(stderr);
    pid_t pid = fork();
    if (pid == 0) {
        if (!getenv("PPZ_CHILD_STDERR")) {
            int dn = open("/dev/null", O_WRONLY);
            if (dn >= 0) { dup2(dn, 1); dup2(dn, 2); close(dn); }
        }
#ifndef __APPLE__
        if (memcap) {
            struct rlimit rl = { (rlim_t)memcap, (rlim_t)memcap };
            setrlimit(RLIMIT_AS, &rl);
        }
#endif
        _exit(fn(arg) & 0xff);
    }
    if (pid < 0) {
        memset(r, 0, sizeof(*r));
        r->sig = -1;
        return;
    }
    child_wait(pid, timeout_ms, memcap, r);
}

/* Run a program with stdin/stdout/stderr redirected to files (NULL: stdin
 * from /dev/null, output discarded). */
static void run_cmd(char *const argv[], const char *in_path, const char *out_path,
                    const char *err_path, int timeout_ms, ChildResult *r)
{
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 0, in_path ? in_path : "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_addopen(&fa, 1, out_path ? out_path : "/dev/null",
                                     O_WRONLY | O_CREAT | O_TRUNC, 0644);
    posix_spawn_file_actions_addopen(&fa, 2, err_path ? err_path : "/dev/null",
                                     O_WRONLY | O_CREAT | O_TRUNC, 0644);
    pid_t pid;
    fflush(stdout);
    int e = posix_spawn(&pid, argv[0], &fa, NULL, argv, environ);
    posix_spawn_file_actions_destroy(&fa);
    if (e) {
        memset(r, 0, sizeof(*r));
        r->sig = -1;
        fprintf(stderr, "harness: cannot run %s: %s\n", argv[0], strerror(e));
        return;
    }
    child_wait(pid, timeout_ms, (size_t)2 << 30, r);
}

#endif /* PPZ_HARNESS_H */
