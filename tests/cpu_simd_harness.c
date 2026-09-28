#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern void dot(void *, void *, void *, void *);
extern void low(void *, void *, void *, void *);
extern void high(void *, void *, void *, void *);
extern void shuffle(void *, void *, void *);
extern void divide(void *, void *, void *);
extern void ratio(void *, void *, void *);
static uint64_t state = 19;
static uint64_t next(void) { state ^= state << 13; state ^= state >> 7; state ^= state << 17; return state; }
int main(int argc, char **argv) {
    (void)argv;
    if (argc > 1 && (!__builtin_cpu_supports("avx512vnni") || !__builtin_cpu_supports("avx512ifma") ||
                    !__builtin_cpu_supports("avx512bw") || !__builtin_cpu_supports("avx512dq") ||
                    !__builtin_cpu_supports("avx512vl") || !__builtin_cpu_supports("avx512vbmi") ||
                    !__builtin_cpu_supports("avx512vbmi2") || !__builtin_cpu_supports("avx512bitalg") ||
                    !__builtin_cpu_supports("avx512vpopcntdq") || !__builtin_cpu_supports("bmi2"))) return 77;
    for (int round = 0; round < 2000; ++round) {
        uint8_t a[64], b[64], c[64], expected[64], storage[80];
        for (int i = 0; i < 64; ++i) { a[i] = next(); b[i] = next(); c[i] = next(); }
        memset(storage, 0xcd, sizeof storage);
        for (int i = 0; i < 16; ++i) {
            uint32_t sum; memcpy(&sum, a + 4*i, 4);
            for (int j = 0; j < 4; ++j) sum += (int32_t)b[4*i+j] * (int8_t)c[4*i+j];
            memcpy(expected + 4*i, &sum, 4);
        }
        uint8_t unaligned_a[67],unaligned_b[67],unaligned_c[67];
        memcpy(unaligned_a+1,a,64); memcpy(unaligned_b+2,b,64); memcpy(unaligned_c+3,c,64);
        dot(storage+3,unaligned_a+1,unaligned_b+2,unaligned_c+3); assert(!memcmp(storage+3,expected,64));
        dot(a,a,b,c); assert(!memcmp(a,expected,64));
        for (int high_half = 0; high_half < 2; ++high_half) {
            for (int i = 0; i < 8; ++i) {
                uint64_t av,bv,cv; memcpy(&av,a+8*i,8); memcpy(&bv,b+8*i,8); memcpy(&cv,c+8*i,8);
                uint64_t mask = (UINT64_C(1)<<52)-1;
                __uint128_t p = (__uint128_t)(bv & mask) * (cv & mask);
                uint64_t r = av + (high_half ? (uint64_t)(p>>52) : ((uint64_t)p & mask));
                memcpy(expected+8*i,&r,8);
            }
            (high_half ? high : low)(storage+3,a,b,c); assert(!memcmp(storage+3,expected,64));
        }
        for (int i = 0; i < 64; ++i) expected[i] = b[i]&128 ? 0 : a[(i&~15)+(b[i]&15)];
        shuffle(storage+3,a,b); assert(!memcmp(storage+3,expected,64));
        for (int rne = 0; rne < 2; ++rne) {
            for (int i = 0; i < 8; ++i) {
                uint64_t x = next() & ((UINT64_C(1)<<53)-1), d = (next() % 255)+1;
                if (round & 1) d = (next() % ((UINT64_C(1)<<53)-1))+1;
                if (round < 8) { x = (UINT64_C(1)<<53)-1-round; d = round+1; }
                __uint128_t n = x;
                if (rne) { x = next() % ((UINT64_C(1)<<32)+1); d = (UINT64_C(1)<<32) + (next() % ((UINT64_C(1)<<32)+1));
                    if (round < 8) { x = (UINT64_C(1)<<32)-round; d = (UINT64_C(1)<<32)+round; }
                    n = (__uint128_t)x << 32; }
                uint64_t q = n/d, rem = n%d;
                if (rne && (rem*2 > d || (rem*2 == d && (q&1)))) ++q;
                memcpy(a+8*i,&x,8); memcpy(b+8*i,&d,8); memcpy(expected+8*i,&q,8);
            }
            (rne ? ratio : divide)(storage+3,a,b); assert(!memcmp(storage+3,expected,64));
        }
        for (int i = 0; i < 3; ++i) assert(storage[i] == 0xcd);
        for (int i = 67; i < 80; ++i) assert(storage[i] == 0xcd);
    }
    puts("CPU SIMD byte oracle PASS"); return 0;
}
