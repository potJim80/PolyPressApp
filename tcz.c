/* Hot loops for the table codec, in C.
 *
 * Only four things live here, and each earned its place by showing up in a
 * profile:
 *
 *   scan_decimals / parse_fixed / fmt_fixed
 *       Converting between decimal text and scaled integers was 70% of decode
 *       and a large share of encode. Both directions are here so the caller
 *       can verify exactness by parsing, re-formatting, and comparing bytes.
 *
 *   pack_ints / unpack_ints
 *       One-byte varints with an escape.
 *
 * Deliberately plain C with no Python headers: it builds with a bare `cc`
 * and is loaded through ctypes, so there is no extension-module toolchain to
 * keep working. Python falls back to the numpy path if the library is
 * missing, so this is an accelerator, never a requirement.
 */

#include <stdint.h>
#include <string.h>

#define ESCAPE 255

/* Largest magnitude we accept, matching INT_LIMIT on the Python side. */
static const int64_t LIMIT = ((int64_t)1) << 62;

/* ------------------------------------------------------------------ scan */

/* Scan a newline-separated buffer of numbers.
 * Returns the maximum number of decimal places, or -1 if any field is not a
 * plain decimal number (which sends the column down the text path).
 * `count` receives the number of fields. */
int32_t scan_decimals(const char *buf, int64_t len, int64_t *count)
{
    int32_t max_dec = 0;
    int64_t fields = 0;
    int64_t i = 0;

    while (i <= len) {
        int64_t start = i;
        while (i < len && buf[i] != '\n') i++;
        int64_t end = i;
        int64_t p = start;
        int digits = 0, dec = 0;

        if (p < end && buf[p] == '-') p++;
        while (p < end && buf[p] >= '0' && buf[p] <= '9') { p++; digits++; }
        if (p < end && buf[p] == '.') {
            p++;
            while (p < end && buf[p] >= '0' && buf[p] <= '9') { p++; dec++; }
            if (dec == 0) return -1;            /* "12." is not accepted */
        }
        if (digits == 0 || p != end) return -1; /* empty or trailing junk */
        if (dec > max_dec) max_dec = dec;
        fields++;

        if (i >= len) break;
        i++;                                     /* step over the newline */
    }
    if (count) *count = fields;
    return max_dec > 18 ? -1 : max_dec;
}

/* ----------------------------------------------------------------- parse */

/* Parse `n` newline-separated decimals into integers scaled by 10^dec.
 * Returns 0 on success, -1 on malformed input or overflow. */
int32_t parse_fixed(const char *buf, int64_t len, int32_t dec,
                    int64_t *out, int64_t n)
{
    static const int64_t POW10[19] = {
        1LL, 10LL, 100LL, 1000LL, 10000LL, 100000LL, 1000000LL,
        10000000LL, 100000000LL, 1000000000LL, 10000000000LL,
        100000000000LL, 1000000000000LL, 10000000000000LL,
        100000000000000LL, 1000000000000000LL, 10000000000000000LL,
        100000000000000000LL, 1000000000000000000LL};

    int64_t i = 0, k = 0;
    while (k < n) {
        int64_t start = i;
        while (i < len && buf[i] != '\n') i++;
        int64_t end = i;
        int64_t p = start;
        int neg = 0;
        int64_t v = 0;
        int seen = 0;

        if (p < end && buf[p] == '-') { neg = 1; p++; }
        while (p < end && buf[p] >= '0' && buf[p] <= '9') {
            v = v * 10 + (buf[p] - '0');
            if (v >= LIMIT) return -1;
            p++; seen = 1;
        }
        if (!seen) return -1;

        int used = 0;
        if (p < end && buf[p] == '.') {
            p++;
            while (p < end && buf[p] >= '0' && buf[p] <= '9') {
                if (used < dec) {
                    v = v * 10 + (buf[p] - '0');
                    if (v >= LIMIT) return -1;
                    used++;
                }
                p++;
            }
        }
        if (p != end) return -1;
        while (used < dec) {                     /* pad to the column scale */
            v *= 10;
            if (v >= LIMIT) return -1;
            used++;
        }
        out[k++] = neg ? -v : v;

        if (i >= len) break;
        i++;
    }
    return k == n ? 0 : -1;
}

/* ---------------------------------------------------------------- format */

/* Write `n` scaled integers back as newline-separated decimal text.
 * Returns bytes written, or -1 if `cap` was too small. */
int64_t fmt_fixed(const int64_t *in, int64_t n, int32_t dec,
                  char *out, int64_t cap)
{
    char digits[24];
    int64_t at = 0;

    for (int64_t k = 0; k < n; k++) {
        int64_t v = in[k];
        /* worst case: sign + 19 integer digits + point + 18 decimals + \n */
        if (at + 40 > cap) return -1;

        if (v < 0) { out[at++] = '-'; v = -v; }

        int64_t scale = 1;
        for (int32_t d = 0; d < dec; d++) scale *= 10;
        int64_t q = dec ? v / scale : v;
        int64_t r = dec ? v % scale : 0;

        int nd = 0;
        if (q == 0) digits[nd++] = '0';
        while (q > 0) { digits[nd++] = (char)('0' + (q % 10)); q /= 10; }
        while (nd > 0) out[at++] = digits[--nd];

        if (dec) {
            out[at++] = '.';
            for (int32_t d = dec - 1; d >= 0; d--) {
                int64_t p = 1;
                for (int32_t e = 0; e < d; e++) p *= 10;
                out[at++] = (char)('0' + ((r / p) % 10));
            }
        }
        out[at++] = '\n';
    }
    return at ? at - 1 : 0;                      /* drop the final newline */
}

/* ------------------------------------------------------------- varints */

/* Zigzag each value; small ones take a byte, the rest an escape plus a
 * 32-bit tail entry. Returns how many went to the tail. */
int64_t pack_ints(const int64_t *in, int64_t n,
                  uint8_t *head, uint32_t *tail)
{
    int64_t nbig = 0;
    for (int64_t i = 0; i < n; i++) {
        int64_t v = in[i];
        uint64_t u = v < 0 ? (uint64_t)(-v) * 2 - 1 : (uint64_t)v * 2;
        if (u < ESCAPE) {
            head[i] = (uint8_t)u;
        } else {
            head[i] = ESCAPE;
            tail[nbig++] = (uint32_t)u;
        }
    }
    return nbig;
}

void unpack_ints(const uint8_t *head, int64_t n,
                 const uint32_t *tail, int64_t *out)
{
    int64_t nbig = 0;
    for (int64_t i = 0; i < n; i++) {
        uint64_t u = head[i];
        if (u == ESCAPE) u = tail[nbig++];
        out[i] = (u & 1) ? -(int64_t)((u + 1) / 2) : (int64_t)(u / 2);
    }
}
