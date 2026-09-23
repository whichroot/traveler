// SPDX-License-Identifier: Apache-2.0
typedef struct AsyncStream AsyncStream;
typedef struct {
    unsigned operation, until;
    uint64_t lanes;
    uint64_t scalar;
    uint64_t *a, *b, *out;
    AsyncStream *source;
} AsyncCommand;
struct AsyncStream {
    unsigned count, done, refs, capturing, invalidated;
    AsyncCommand commands[4096];
};
typedef struct { AsyncStream *source; unsigned until; } AsyncEvent;
static int async_streams, async_events, async_pinned, async_context_syncs, async_graphs, async_execs;
int mock_async_context_syncs(void) { return async_context_syncs; }
static void async_release(AsyncStream *s) { if (s && !--s->refs) free(s); }
static void async_drain(AsyncStream *s, unsigned until) {
    assert(until <= s->count);
    while (s->done < until) {
        AsyncCommand *c = &s->commands[s->done++];
        if (c->source) { async_drain(c->source, c->until); async_release(c->source); }
        else if (c->operation == 4) { memcpy(c->out, c->a, c->lanes); }
        else for (uint64_t i = 0; i < c->lanes; i++) {
            if (c->operation == 0) c->out[i] = c->a[i] * 2;
            else if (c->operation == 1) c->out[i] = c->a[i] + 3;
            else if (c->operation == 2) c->out[i] = c->a[i] + 5;
            else if (c->operation == 5) c->out[i] = c->a[i] * 3 + c->scalar;
            else if (c->operation == 6) c->out[i] = c->a[i] * 5 + 7;
            else if (c->operation == 7) c->out[i] = (c->a[i] * 3 + c->scalar) * 5 + 7;
            else c->out[i] = c->a[i] + c->b[i];
        }
    }
}
static int mock_async_launch(AsyncStream *s, unsigned operation, uint64_t lanes, void **args) {
    assert(s->count < 4096);
    unsigned n = operation == 3 ? 3 : 2;
    assert(*(uint64_t *)args[n] == 0 && *(uint64_t *)args[n+1] == lanes);
    AsyncCommand *c = &s->commands[s->count++];
    c->operation = operation; c->lanes = lanes;
    c->a = *(uint64_t **)args[0]; c->out = *(uint64_t **)args[n-1];
    if (operation == 3) c->b = *(uint64_t **)args[1];
    launches++; return 0;
}
int cuStreamCreate(void **out, unsigned flags) {
    assert(flags == 1); int e = fail(12); if (e) return e;
    AsyncStream *s = calloc(1, sizeof(*s)); s->refs = 1; *out = s; async_streams++; return 0;
}
int cuStreamSynchronize(AsyncStream *s) {
    int e = fail(16); if (e) return e; if (s) async_drain(s, s->count); return 0;
}
int cuStreamQuery(AsyncStream *s) {
    int e = fail(17); if (e) return e; return s->done == s->count ? 0 : 600;
}
int cuStreamDestroy_v2(AsyncStream *s) {
    int e = fail(20); if (e) return e; assert(s->done == s->count && !s->capturing);
    async_streams--; async_release(s); return 0;
}
int cuEventCreate(void **out, unsigned flags) {
    assert(flags == 2); int e = fail(13); if (e) return e;
    *out = calloc(1, sizeof(AsyncEvent)); async_events++; return 0;
}
int cuEventRecord(AsyncEvent *e, AsyncStream *s) {
    int status = fail(14); if (status) return status;
    async_release(e->source); e->source = s; e->until = s->count; s->refs++; return 0;
}
int cuStreamWaitEvent(AsyncStream *s, AsyncEvent *e, unsigned flags) {
    assert(!flags && e->source && s->count < 4096); int status = fail(15); if (status) return status;
    AsyncCommand *c = &s->commands[s->count++]; c->source = e->source; c->until = e->until;
    c->source->refs++; return 0;
}
int cuEventQuery(AsyncEvent *e) {
    int status = fail(19); if (status) return status;
    return e->source->done >= e->until ? 0 : 600;
}
int cuEventSynchronize(AsyncEvent *e) {
    int status = fail(18); if (status) return status; async_drain(e->source, e->until); return 0;
}
int cuEventDestroy_v2(AsyncEvent *e) {
    int status = fail(21); if (status) return status;
    async_release(e->source); free(e); async_events--; return 0;
}
int cuMemHostAlloc(void **out, size_t bytes, unsigned flags) {
    assert(current != (void *)0x123 && bytes && !flags);
    int status = fail(22); if (status) return status;
    *out = malloc(bytes); async_pinned++; return 0;
}
int cuMemFreeHost(void *pointer) {
    assert(current != (void *)0x123); int status = fail(23); if (status) return status;
    free(pointer); async_pinned--; return 0;
}
static int async_copy(AsyncStream *s, void *dest, const void *src, size_t bytes, int operation) {
    assert(s && s->count < 4096 && bytes && current != (void *)0x123);
    int status = fail(operation); if (status) { if (s->capturing) s->invalidated = 1; return status; }
    AsyncCommand *c = &s->commands[s->count++];
    c->operation = 4; c->a = (uint64_t *)src; c->out = dest; c->lanes = bytes; return 0;
}
int cuMemcpyHtoDAsync_v2(void *dest, const void *src, size_t bytes, AsyncStream *s) { return async_copy(s, dest, src, bytes, 24); }
int cuMemcpyDtoHAsync_v2(void *dest, const void *src, size_t bytes, AsyncStream *s) { return async_copy(s, dest, src, bytes, 25); }
typedef struct { unsigned count; AsyncCommand *commands; } AsyncGraph;
int cuStreamBeginCapture_v2(AsyncStream *s, int mode) {
    int status = fail(26); if (status) return status;
    assert(mode == 2 && !s->count && !s->capturing); s->capturing = 1; return 0;
}
int cuStreamEndCapture(AsyncStream *s, AsyncGraph **out) {
    int status = fail(27); if (status) return status;
    if (s->invalidated) { s->count = 0; s->done = 0; s->capturing = 0; s->invalidated = 0; *out = NULL; return 901; }
    assert(s->capturing); AsyncGraph *g = malloc(sizeof(*g)); g->count = s->count;
    g->commands = malloc(g->count * sizeof(*g->commands)); memcpy(g->commands, s->commands, g->count * sizeof(*g->commands));
    s->count = 0; s->done = 0; s->capturing = 0; *out = g; async_graphs++; return 0;
}
int cuGraphInstantiateWithFlags(AsyncGraph **out, AsyncGraph *g, uint64_t flags) {
    int status = fail(28); if (status) return status; assert(!flags);
    AsyncGraph *e = malloc(sizeof(*e)); e->count = g->count;
    e->commands = malloc(e->count * sizeof(*e->commands)); memcpy(e->commands, g->commands, e->count * sizeof(*e->commands));
    *out = e; async_execs++; return 0;
}
int cuGraphDestroy(AsyncGraph *g) {
    int status = fail(29); if (status) return status; free(g->commands); free(g); async_graphs--; return 0;
}
int cuGraphExecDestroy(AsyncGraph *g) {
    int status = fail(30); if (status) return status; free(g->commands); free(g); async_execs--; return 0;
}
int cuGraphLaunch(AsyncGraph *g, AsyncStream *s) {
    int status = fail(31); if (status) return status; assert(s->count + g->count <= 4096);
    memcpy(s->commands+s->count, g->commands, g->count * sizeof(*g->commands)); s->count += g->count; return 0;
}
