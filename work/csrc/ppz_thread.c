/* Threads for the encoder.
 *
 * 72-92% of encode time is lzma, and most of that is TRIAL compressions --
 * candidates that are compressed, measured, and compared. The trials of one
 * decision are independent, so they run at the same time.
 *
 * This changes when the work happens, never what is chosen: every decision
 * still folds its measured sizes in the original order with the original
 * strict `<`, so ties fall where they always did and the archive is byte for
 * byte the one the serial path writes. tests/ checks PPZ_THREADS=1 against the
 * default on every table it builds.
 *
 * The price is memory. An xz -9e compressor touches ~64 MB plus ~8 bytes per
 * input byte. Trials are started from several places at once (the fallback,
 * the strict plan, the text pile), so the heavy compressions also take a slot
 * from one process-wide limit: never more than ppz_nthreads() of them run at
 * the same moment, however many threads are alive. A thread holding a slot
 * never waits for anything else, so the limit cannot deadlock.
 *
 * PPZ_THREADS=1 gives the serial path anywhere. Streaming mode sets it too,
 * because its --budget promises a memory ceiling.
 */

#include "ppz.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

/* Per thread: streaming mode makes each block's worker serial inside while
 * the blocks themselves run side by side. */
static __thread int g_serial = 0;

void ppz_set_serial(int on) { g_serial = on; }
int  ppz_serial(void) { return g_serial; }

/* PPZ_TRACE=1: where the encode time goes, stage by stage, on stderr. */
double ppz_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

void ppz_trace(const char *what, double since)
{
    static int on = -1;
    if (on < 0) { const char *e = getenv("PPZ_TRACE"); on = e && *e && *e != '0'; }
    if (on) fprintf(stderr, "  [trace] %-34s %8.3f s\n", what, ppz_now() - since);
}

int ppz_nthreads(void)
{
    return g_serial ? 1 : ppz_workers();
}

/* How many heavy compressions may run at once, serial or not. Streaming mode
 * is serial INSIDE each block but runs several blocks side by side, and each
 * of those still needs a slot. */
int ppz_workers(void)
{
    const char *e = getenv("PPZ_THREADS");
    if (e && *e) {
        long n = strtol(e, NULL, 10);
        if (n > 0) return n > 64 ? 64 : (int)n;
    }
    /* Four, not every core: each xz -9e compressor zeroes a 64 MB hash table,
     * and this runs on a fanless laptop. Four took the bulk of the gain. */
    long cpu = sysconf(_SC_NPROCESSORS_ONLN);
    if (cpu < 1) cpu = 1;
    return cpu < 4 ? (int)cpu : 4;
}

/* ------------------------------------------------------------ the slots */

static pthread_mutex_t slot_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  slot_cv = PTHREAD_COND_INITIALIZER;
static int slots_used = 0;

void ppz_slot_take(void)
{
    int limit = ppz_workers();
    pthread_mutex_lock(&slot_mu);
    while (slots_used >= limit) pthread_cond_wait(&slot_cv, &slot_mu);
    slots_used++;
    pthread_mutex_unlock(&slot_mu);
}

void ppz_slot_give(void)
{
    pthread_mutex_lock(&slot_mu);
    slots_used--;
    pthread_cond_broadcast(&slot_cv);
    pthread_mutex_unlock(&slot_mu);
}

/* ------------------------------------------------------- parallel for */

typedef struct {
    PpzTask  fn;
    uint8_t *args;
    size_t   size, n;
    size_t   next;           /* claimed with __atomic_fetch_add */
} ParFor;

static void *parfor_worker(void *p)
{
    ParFor *pf = p;
    for (;;) {
        size_t i = __atomic_fetch_add(&pf->next, 1, __ATOMIC_RELAXED);
        if (i >= pf->n) break;
        pf->fn(pf->args + i * pf->size);
    }
    return NULL;
}

void ppz_parallel(PpzTask fn, void *args, size_t argsize, size_t n)
{
    if (!n) return;
    int nt = ppz_nthreads();
    ParFor pf = { fn, args, argsize, n, 0 };
    if (nt <= 1 || n == 1) { parfor_worker(&pf); return; }

    size_t extra = (size_t)nt - 1;
    if (extra > n - 1) extra = n - 1;
    pthread_t *th = malloc(extra * sizeof(pthread_t));
    size_t started = 0;
    if (th)
        for (; started < extra; started++)
            if (pthread_create(&th[started], NULL, parfor_worker, &pf)) break;
    parfor_worker(&pf);                   /* the caller works too */
    for (size_t i = 0; i < started; i++) pthread_join(th[i], NULL);
    free(th);
}

/* ---------------------------------------------------------- background */

struct PpzBg {
    PpzTask   fn;
    void     *arg;
    pthread_t th;
    int       threaded;
};

static void *bg_main(void *p)
{
    PpzBg *b = p;
    b->fn(b->arg);
    return NULL;
}

PpzBg *ppz_bg_start(PpzTask fn, void *arg)
{
    PpzBg *b = calloc(1, sizeof(PpzBg));
    if (!b) { fn(arg); return NULL; }
    b->fn = fn;
    b->arg = arg;
    if (ppz_nthreads() > 1 && !pthread_create(&b->th, NULL, bg_main, b))
        b->threaded = 1;
    return b;
}

void ppz_bg_join(PpzBg *b)
{
    if (!b) return;                       /* ran inline in ppz_bg_start */
    if (b->threaded) pthread_join(b->th, NULL);
    else b->fn(b->arg);                   /* serial: run it now */
    free(b);
}
