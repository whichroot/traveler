// SPDX-License-Identifier: Apache-2.0
// Test-only CUDA driver double. Values are independent of Traveler codegen.
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

static void *current = (void *)0x123;
static int fault, delay, uploads, downloads, launches, allocations, modules, contexts;
void mock_fail(int operation, int after) { fault = operation; delay = after; }
static int fail(int operation) {
    if (operation != fault) return 0;
    if (delay-- > 0) return 0;
    fault = 0;
    return 800 + operation;
}
int mock_clean(void) {
    return !allocations && !modules && !contexts && current == (void *)0x123;
}
int mock_pipeline(void) { return uploads == 3 && downloads == 1 && launches == 4 && mock_clean(); }
int cuInit(unsigned flags) { (void)flags; return 0; }
int cuDeviceGet(int *device, int ordinal) { *device = ordinal; return 0; }
int cuDeviceGetAttribute(int *v, int attribute, int device) {
    (void)device;
    *v = attribute == 75 ? 12 : attribute == 76 ? 0 : attribute == 5 ? 2147483647 : 1024;
    return 0;
}
int cuDevicePrimaryCtxRetain(void **out, int device) {
    *out = (void *)(uintptr_t)(0x10000 + device); contexts++; return 0;
}
int cuDevicePrimaryCtxRelease(int device) { (void)device; contexts--; return 0; }
int cuCtxGetCurrent(void **out) { int e = fail(1); if (!e) *out = current; return e; }
int cuCtxSetCurrent(void *context) { int e = fail(2); if (!e) current = context; return e; }
int cuMemAlloc_v2(void **out, size_t bytes) {
    int e = fail(3); if (e) return e;
    assert(current != (void *)0x123);
    *out = calloc(1, bytes); allocations++; return 0;
}
int cuMemFree_v2(void *pointer) {
    int e = fail(9); if (e) return e;
    assert(current != (void *)0x123);
    free(pointer); allocations--; return 0;
}
int cuModuleLoadDataEx(void **out, void *ptx, unsigned count, int *options, void **values) {
    assert(current != (void *)0x123 && ptx && count == 2 && options[0] == 5 && options[1] == 6);
    int e = fail(4);
    if (e) { strcpy(values[0], "mock JIT failure"); return e; }
    *out = malloc(1); modules++; return 0;
}
int cuModuleUnload(void *module) { free(module); modules--; return 0; }
int cuModuleGetFunction(void **out, void *module, const char *name) {
    (void)module;
    int e = fail(5); if (e) return e;
    if (!strcmp(name, "__pfor_gpu_worker_0")) *out = (void *)1;
    else if (!strcmp(name, "__pfor_gpu_worker_1")) *out = (void *)2;
    else return 500;
    return 0;
}
int cuFuncGetAttribute(int *out, int attr, void *function) {
    (void)attr; (void)function; *out = 1024; return 0;
}
int cuMemcpyHtoD_v2(void *dest, const void *src, size_t bytes) {
    int e = fail(6); if (e) return e;
    assert(current != (void *)0x123); memcpy(dest, src, bytes); uploads++; return 0;
}
int cuMemcpyDtoH_v2(void *dest, const void *src, size_t bytes) {
    assert(current != (void *)0x123); memcpy(dest, src, bytes); downloads++; return 0;
}
int cuCtxSynchronize(void) { return fail(8); }
int cuLaunchKernel(void *function, unsigned gx, unsigned gy, unsigned gz,
                   unsigned bx, unsigned by, unsigned bz, unsigned shared,
                   void *stream, void **args, void **extra) {
    int e = fail(7); if (e) return e;
    assert(current != (void *)0x123 && gy == 1 && gz == 1 && bx == 256 && by == 1 && bz == 1);
    assert(!shared && !stream && !extra && gx > 0);
    launches++;
    if (function == (void *)1) {
        int32_t *input = *(int32_t **)args[0], *middle = *(int32_t **)args[2];
        int32_t scale = *(int32_t *)args[1], lo = *(int32_t *)args[3], hi = *(int32_t *)args[4];
        for (int i = lo; i < hi; i++) middle[i] = input[i] * scale;
    } else {
        int32_t *acc = *(int32_t **)args[0], *middle = *(int32_t **)args[1], *out = *(int32_t **)args[3];
        int32_t bias = *(int32_t *)args[2], lo = *(int32_t *)args[4], hi = *(int32_t *)args[5];
        for (int i = lo; i < hi; i++) { acc[i] += middle[i]; out[i] = acc[i] + bias; }
    }
    return 0;
}
