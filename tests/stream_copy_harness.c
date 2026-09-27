// SPDX-License-Identifier: Apache-2.0
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

extern void copy_bytes(uint8_t *, const uint8_t *, uint64_t);

int main(void) {
    const size_t sizes[] = {0, 1, 7, 63, 64, 65, 255, 256, 257, 4095, 4096, 4097, 8191, 65537};
    const size_t shifts[] = {0, 1, 7, 31, 63};
    uint8_t *source = malloc(66000), *destination = malloc(66000);
    assert(source && destination);
    for (size_t i = 0; i < 66000; ++i) source[i] = (uint8_t)(i * 13 + 7);
    for (size_t k = 0; k < sizeof sizes / sizeof *sizes; ++k)
        for (size_t a = 0; a < 64; ++a)
            for (size_t b = 0; b < sizeof shifts / sizeof *shifts; ++b) {
                memset(destination, 0xcd, 66000);
                copy_bytes(destination + 64 + a, source + shifts[b], sizes[k]);
                assert(!memcmp(destination + 64 + a, source + shifts[b], sizes[k]));
                for (size_t j = 0; j < 64 + a; ++j) assert(destination[j] == 0xcd);
                for (size_t j = 64 + a + sizes[k]; j < 66000; ++j) assert(destination[j] == 0xcd);
            }
    free(source); free(destination);
    size_t page = (size_t)sysconf(_SC_PAGESIZE), span = 18 * page;
    uint8_t *s = mmap(NULL, span, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    uint8_t *d = mmap(NULL, span, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(s != MAP_FAILED && d != MAP_FAILED);
    assert(!mprotect(s + span - page, page, PROT_NONE));
    assert(!mprotect(d + span - page, page, PROT_NONE));
    for (size_t n = 4095; n < 4160; ++n) {
        uint8_t *src = s + span - page - n, *dst = d + span - page - n;
        memset(src, 0x35, n); memset(dst, 0x77, n);
        copy_bytes(dst, src, n); assert(!memcmp(src, dst, n));
    }
    assert(!munmap(s, span) && !munmap(d, span));
    puts("stream copy PASS: byte oracle, heads/tails, all destination alignments, protected boundaries");
    return 0;
}
