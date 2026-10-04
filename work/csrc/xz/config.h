/* config.h for the vendored liblzma subset (see build.sh): raw LZMA2 encode
 * and decode only, no .xz container, no threads, no other filters. Written
 * by hand in place of xz's configure; every HAVE_ below is a feature this
 * subset compiles, every other one is left undefined on purpose. */
#define HAVE_STDBOOL_H 1
#define HAVE__BOOL 1
#define HAVE_STDINT_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_STRING_H 1
#define HAVE_VISIBILITY 1
#define SIZEOF_SIZE_T __SIZEOF_SIZE_T__
#define HAVE_ENCODER_LZMA2 1
#define HAVE_DECODER_LZMA2 1
#define HAVE_MF_HC3 1
#define HAVE_MF_HC4 1
#define HAVE_MF_BT2 1
#define HAVE_MF_BT3 1
#define HAVE_MF_BT4 1
#define HAVE___BUILTIN_BSWAPXX 1
#define HAVE___BUILTIN_ASSUME_ALIGNED 1
#define TUKLIB_SYMBOL_PREFIX lzma_
/* as every release build of xz: its internal assert()s are off. Measured
 * 2026-10-03: left on, they cost 8% more instructions in the encoder. */
#define NDEBUG 1
/* unaligned loads are fast (and legal) on these two only */
#if defined(__x86_64__) || defined(__aarch64__) || defined(__arm64__)
#  define TUKLIB_FAST_UNALIGNED_ACCESS 1
#endif

/* the CRC tables kept here are the little-endian ones */
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
#  error "the vendored xz is set up for little-endian machines only"
#endif
