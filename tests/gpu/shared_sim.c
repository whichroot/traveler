// SPDX-License-Identifier: Apache-2.0
// Retargeted block execution uses host threads and a real phase barrier.
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>

static _Thread_local uint32_t tid[3];
static uint32_t bid[3], block[3], grid[3];
static pthread_barrier_t barrier;
static pthread_barrier_t warp_barriers[4];
static uint32_t exchange[128];
static void (*entry)(uint64_t *, uint64_t *, uint64_t, uint64_t, uint64_t);
static void (*dynamic_entry)(uint64_t *, uint64_t *, uint64_t, uint64_t, uint64_t, uint64_t);
static uint64_t slots;
static uint64_t *input, *output, limit, count;
#define REGS(name, values) \
    uint32_t test_##name##_x(void) { return values[0]; } \
    uint32_t test_##name##_y(void) { return values[1]; } \
    uint32_t test_##name##_z(void) { return values[2]; }
REGS(tid, tid)
REGS(ctaid, bid)
REGS(ntid, block)
REGS(nctaid, grid)
void test_barrier(void) { pthread_barrier_wait(&barrier); }
static unsigned local_index(void) { return tid[0] + block[0] * (tid[1] + block[1] * tid[2]); }
void test_warp_sync(uint32_t mask) {
    if (mask != UINT32_MAX) abort();
    pthread_barrier_wait(&warp_barriers[local_index() / 32]);
}
uint32_t test_shuffle(uint32_t mask, uint32_t value, uint32_t source, uint32_t clamp) {
    unsigned local = local_index(), base = local & ~31u;
    if (source > 31 || clamp != 31) abort();
    exchange[local] = value;
    test_warp_sync(mask);
    uint32_t result = exchange[base + source];
    test_warp_sync(mask);
    return result;
}
uint32_t test_shuffle_xor(uint32_t mask, uint32_t value, uint32_t delta, uint32_t clamp) {
    return test_shuffle(mask, value, (local_index() & 31) ^ delta, clamp);
}
uint32_t test_ballot(uint32_t mask, _Bool predicate) {
    unsigned local = local_index(), base = local & ~31u;
    exchange[local] = predicate;
    test_warp_sync(mask);
    uint32_t result = 0;
    for (unsigned i = 0; i < 32; i++) result |= exchange[base + i] << i;
    test_warp_sync(mask);
    return result;
}
_Bool test_any(uint32_t mask, _Bool predicate) { return test_ballot(mask, predicate) != 0; }
_Bool test_all(uint32_t mask, _Bool predicate) { return test_ballot(mask, predicate) == UINT32_MAX; }
static void *lane(void *argument) {
    uintptr_t index = (uintptr_t)argument;
    tid[0] = index % block[0];
    tid[1] = index / block[0] % block[1];
    tid[2] = index / block[0] / block[1];
    if (dynamic_entry) dynamic_entry(input, output, limit, slots, 0, count);
    else entry(input, output, limit, 0, count);
    return NULL;
}
void simulate(void (*kernel)(uint64_t *, uint64_t *, uint64_t, uint64_t, uint64_t),
              uint64_t *in, uint64_t *out, uint64_t active, const uint32_t *shape) {
    entry = kernel; input = in; output = out; limit = active; count = 1;
    for (unsigned i = 0; i < 3; i++) {
        grid[i] = shape[i]; block[i] = shape[i + 3];
        count *= (uint64_t)grid[i] * block[i];
    }
    unsigned threads = block[0] * block[1] * block[2];
    if (!threads || threads > 128) abort();
    pthread_t workers[128];
    for (bid[2] = 0; bid[2] < grid[2]; bid[2]++)
        for (bid[1] = 0; bid[1] < grid[1]; bid[1]++)
            for (bid[0] = 0; bid[0] < grid[0]; bid[0]++) {
                if (pthread_barrier_init(&barrier, NULL, threads)) abort();
                if (threads % 32 == 0)
                    for (unsigned w = 0; w < threads / 32; w++)
                        if (pthread_barrier_init(&warp_barriers[w], NULL, 32)) abort();
                for (uintptr_t i = 0; i < threads; i++)
                    if (pthread_create(&workers[i], NULL, lane, (void *)i)) abort();
                for (unsigned i = 0; i < threads; i++)
                    if (pthread_join(workers[i], NULL)) abort();
                pthread_barrier_destroy(&barrier);
                if (threads % 32 == 0)
                    for (unsigned w = 0; w < threads / 32; w++) pthread_barrier_destroy(&warp_barriers[w]);
            }
}
void simulate_dynamic(void (*kernel)(uint64_t *, uint64_t *, uint64_t, uint64_t, uint64_t, uint64_t),
                      uint64_t *in, uint64_t *out, uint64_t active, uint64_t capacity, const uint32_t *shape) {
    dynamic_entry = kernel; slots = capacity;
    simulate(NULL, in, out, active, shape);
    dynamic_entry = NULL;
}
