// SPDX-License-Identifier: Apache-2.0
#define _POSIX_C_SOURCE 200809L
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

extern _Atomic uint64_t __pfor_gen[32];
extern _Atomic int __pfor_ack[64];
extern void __parallel_for(void (*)(void *, int, int), void *, int, int);
extern void __parallel_for_i64(void (*)(void *, int64_t, int64_t), void *, int64_t, int64_t);

static int mode, context_a, context_b;
static const int64_t base = INT64_C(8589934603);
static atomic_int release_entry, snapshot_started, release_snapshot, publishing;
static atomic_int odd_skipped, retried, torn_seen, parked, released, returned, nested;
static atomic_int calls[9];
static _Thread_local int held_snapshot;

static void check(int ok, const char *message) {
    if (!ok) { fprintf(stderr, "%s\n", message); _Exit(1); }
}

static void await(atomic_int *flag) {
    while (!atomic_load(flag)) sched_yield();
}

void publication_entry(int index) {
    if (index == 0) await(&released);
    if (index == 8 && mode == 0) await(&release_entry);
}

void publication_after_fn(int index) {
    if (index == 8 && (mode == 1 || mode == 2) && !held_snapshot) {
        held_snapshot = 1;
        atomic_store(&snapshot_started, 1);
        await(&release_snapshot);
    }
}

void publication_spin_more(int index, uint64_t generation) {
    if (index == 8 && (generation & 1)) atomic_store(&odd_skipped, 1);
}

void publication_park(int index) {
    if (index == 8) atomic_store(&parked, 1);
}

void publication_retry(int index) {
    if (index == 8 && atomic_load(&publishing)) atomic_store(&retried, 1);
}

void publication_steady(int index) {
    if (index == 8)
        check(!atomic_load(&publishing), "snapshot accepted during publication");
}

static void job_a(void *context, int lo, int hi);

void publication_snapshot(int index, void *fn, void *ctx, int64_t lo, int64_t hi, int nthr, int wide) {
    if (index != 8 || !atomic_load(&publishing) || mode == 0 || mode == 3) return;
    check(fn == (void *)job_a && ctx == &context_b, "torn function/context pair not exercised");
    if (mode == 1)
        check(lo == 0 && hi == 513 && nthr == 8 && wide == 0, "unexpected early torn record");
    else
        check(lo == base && hi == base + 576 && nthr == 9 && wide == 0, "unexpected late torn record");
    atomic_store(&torn_seen, 1);
}

void publication_writer(int threads, int phase) {
    if (threads != 9 || phase != mode) return;
    atomic_store(&publishing, 1);
    if (mode == 0) {
        atomic_store(&release_entry, 1);
        await(&odd_skipped);
        check(atomic_load(&__pfor_ack[0]) == 0, "early acknowledgement");
    } else if (mode == 1 || mode == 2) {
        atomic_store(&release_snapshot, 1);
        await(&retried);
        check(atomic_load(&torn_seen), "torn snapshot was not taken");
        check(atomic_load(&__pfor_ack[0]) == 7, "torn snapshot changed old acknowledgements");
    } else {
        check(atomic_load(&parked), "parked worker was not exercised");
    }
    check(atomic_load(&__pfor_gen[0]) == 3, "writer did not mark generation odd");
    atomic_store(&publishing, 0);
}

static void job_a(void *context, int lo, int hi) {
    check(context == &context_a && lo >= 0 && hi <= 513, "mixed old job executed");
    if (lo == 0 && (mode == 1 || mode == 2)) await(&snapshot_started);
    if (lo == 0 && mode == 3) await(&parked);
}

static void serial_nested(void *context, int lo, int hi) {
    check(context == &context_b && lo == 0 && hi == 576, "nested dispatch did not use serial fallback");
    atomic_fetch_add(&nested, 1);
}

static void job_b(void *context, int64_t lo, int64_t hi) {
    check(context == &context_b && lo >= base && hi <= base + 576 && hi - lo == 64,
          "mixed bounds, ABI, or context executed");
    int index = (int)((lo - base) / 64);
    check(index >= 0 && index < 9 && lo == base + index * 64, "invalid slice");
    check(atomic_load(&__pfor_gen[0]) == 4, "new job executed before publication");
    check(atomic_fetch_add(&calls[index], 1) == 0, "duplicate slice execution");
    if (index == 0) __parallel_for(serial_nested, &context_b, 0, 576);
    if (index == 1) await(&released);
}

static void *complete_job(void *unused) {
    (void)unused;
    for (int i = 0; i < 9; ++i) await(&calls[i]);
    while (atomic_load(&__pfor_ack[0]) < 7) sched_yield();
    check(atomic_load(&__pfor_ack[0]) == 7 && !atomic_load(&returned), "premature dispatch completion");
    atomic_store(&released, 1);
    return NULL;
}

int main(int argc, char **argv) {
    alarm(15);
    check(argc == 2, "missing schedule");
    mode = atoi(argv[1]);
    check(mode >= 0 && mode <= 3, "invalid schedule");
    check(setenv("TRAVELER_THREADS", "9", 1) == 0, "setenv failed");
    __parallel_for(job_a, &context_a, 0, 513);
    check(atomic_load(&__pfor_gen[0]) == 2, "first generation is not committed even");
    pthread_t observer;
    check(pthread_create(&observer, NULL, complete_job, NULL) == 0, "observer creation failed");
    __parallel_for_i64(job_b, &context_b, base, base + 576);
    atomic_store(&returned, 1);
    check(pthread_join(observer, NULL) == 0, "observer join failed");
    check(atomic_load(&released) && atomic_load(&__pfor_ack[0]) == 8, "incomplete dispatch");
    for (int i = 0; i < 9; ++i) check(atomic_load(&calls[i]) == 1, "missing or duplicate slice");
    check(atomic_load(&nested) == 1, "nested fallback was not exercised");
    printf("publication PASS: schedule=%d, even generation=4, nine exact slices, eight acknowledgements\n", mode);
    return 0;
}
