// SPDX-License-Identifier: Apache-2.0
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static uintptr_t watched;
static size_t watched_bytes;
static int arrivals, released = 1;
static _Atomic int fail_after = -1, started, joined;

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
    int status = pthread_join(t, ret);
    if (!status) ++joined;
    return status;
}

int dax_threads_drained(void) { return started == joined; }
