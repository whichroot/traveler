// SPDX-License-Identifier: Apache-2.0
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static uintptr_t watched;
static size_t watched_bytes;
static int arrivals, released = 1;
static _Atomic int fail_after = -1, started, joined;
static _Atomic uint64_t copied_bytes;
static _Atomic int short_polls;
int dax_test_usleep(unsigned interval) {
    if (interval == 13) ++short_polls;
    return usleep(interval);
}
int dax_short_polls(void) { return short_polls; }
static int join_fail, init_fail = -1, sync_live;

void dax_copy_pause(void *pointer, uint64_t bytes) {
    pthread_mutex_lock(&lock);
    watched = (uintptr_t)pointer;
    watched_bytes = (size_t)bytes;
    arrivals = 0;
    released = 0;
    pthread_mutex_unlock(&lock);
}

void dax_copy_wait(int count) {
    pthread_mutex_lock(&lock);
    while (arrivals < count) pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
}

void dax_copy_release(void) {
    pthread_mutex_lock(&lock);
    released = 1;
    watched_bytes = 0;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
}

void *dax_test_memcpy(void *dst, const void *src, size_t bytes) {
    pthread_mutex_lock(&lock);
    uintptr_t d = (uintptr_t)dst, s = (uintptr_t)src;
    if (!released && ((d >= watched && d - watched < watched_bytes) ||
                      (s >= watched && s - watched < watched_bytes))) {
        if (bytes == 0) abort();
        ++arrivals;
        pthread_cond_broadcast(&changed);
        while (!released) pthread_cond_wait(&changed, &lock);
    }
    pthread_mutex_unlock(&lock);
    copied_bytes += bytes;
    return memcpy(dst, src, bytes);
}

void dax_thread_fail(int after) {
    fail_after = after;
    started = joined = 0;
}

int dax_test_pthread_create(pthread_t *t, const pthread_attr_t *attr,
                            void *(*entry)(void *), void *arg) {
    if (fail_after == 0) { fail_after = -1; return 11; }
    if (fail_after > 0) --fail_after;
    int status = pthread_create(t, attr, entry, arg);
    if (!status) ++started;
    return status;
}

int dax_test_pthread_join(pthread_t t, void **ret) {
    if (join_fail) { join_fail = 0; return 11; }
    int status = pthread_join(t, ret);
    if (!status) ++joined;
    return status;
}

int dax_threads_drained(void) { return started == joined; }
uint64_t dax_copied_bytes(void) { return copied_bytes; }
int dax_threads_started(void) { return started; }
int dax_threads_joined(void) { return joined; }
void dax_join_fail(void) { join_fail = 1; }
void dax_init_fail(int after) { init_fail = after; }
int dax_sync_live(void) { return sync_live; }

static int fail_init(void) {
    if (init_fail == 0) { init_fail = -1; return 1; }
    if (init_fail > 0) --init_fail;
    return 0;
}

int dax_test_pthread_mutex_init(pthread_mutex_t *mu, const pthread_mutexattr_t *attr) {
    if (fail_init()) return 11;
    int status = pthread_mutex_init(mu, attr);
    if (!status) ++sync_live;
    return status;
}

int dax_test_pthread_cond_init(pthread_cond_t *cv, const pthread_condattr_t *attr) {
    if (fail_init()) return 11;
    int status = pthread_cond_init(cv, attr);
    if (!status) ++sync_live;
    return status;
}

int dax_test_pthread_mutex_destroy(pthread_mutex_t *mu) {
    int status = pthread_mutex_destroy(mu);
    if (!status) --sync_live;
    return status;
}

int dax_test_pthread_cond_destroy(pthread_cond_t *cv) {
    int status = pthread_cond_destroy(cv);
    if (!status) --sync_live;
    return status;
}
