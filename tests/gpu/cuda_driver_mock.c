// SPDX-License-Identifier: Apache-2.0
// Test-only CUDA driver double. Values are independent of Traveler codegen.
#include <stdint.h>
#include <stdio.h>
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
    int e = fail(10); if (e) return e;
    switch (attribute) {
    case 75: *v = 12; break;
    case 76: *v = 0; break;
    case 1: case 2: case 3: *v = 1024; break;
    case 4: *v = 64; break;
    case 5: *v = 2147483647; break;
    case 6: case 7: *v = 65535; break;
    case 8: *v = 49152; break;
    case 10: *v = 32; break;
    default: return 1;
    }
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
    *out = malloc(1);
    *(char *)*out = strstr(ptx, "__traveler_shared_dynamic") != NULL;
    if (strstr(ptx, "atom.")) *(char *)*out = 2;
    modules++; return 0;
}
int cuModuleUnload(void *module) { free(module); modules--; return 0; }
int cuModuleGetFunction(void **out, void *module, const char *name) {
    int e = fail(5); if (e) return e;
    if (*(char *)module == 2) {
        unsigned entry;
        if (sscanf(name, "__traveler_kernel_%u", &entry) != 1 || entry >= 24) return 500;
        *out = (void *)(uintptr_t)(20 + entry); return 0;
    }
    if (*(char *)module == 1) {
        if (!strcmp(name, "__traveler_kernel_0")) *out = (void *)10;
        else if (!strcmp(name, "__traveler_kernel_1")) *out = (void *)11;
        else if (!strcmp(name, "__traveler_kernel_2")) *out = (void *)12;
        else return 500;
        return 0;
    }
    if (!strcmp(name, "__pfor_gpu_worker_0")) *out = (void *)1;
    else if (!strcmp(name, "__pfor_gpu_worker_1")) *out = (void *)2;
    else if (!strcmp(name, "__traveler_kernel_0")) *out = (void *)3;
    else if (!strcmp(name, "__traveler_kernel_1")) *out = (void *)4;
    else return 500;
    return 0;
}
int cuFuncGetAttribute(int *out, int attr, void *function) {
    (void)function;
    int e = fail(11); if (e) return e;
    if (attr == 0) *out = 1024;
    else if (attr == 1) *out = 0;
    else if (attr == 8) *out = 49152;
    else return 1;
    return 0;
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
    if ((uintptr_t)function >= 20 && (uintptr_t)function < 44) {
        unsigned entry = (uintptr_t)function - 20;
        unsigned bits = entry % 8 < 4 ? 32 : 64, operation = entry / 8;
        uint64_t count = *(uint64_t *)args[2], modulus = *(uint64_t *)args[3];
        uint64_t lanes = (uint64_t)gx * gy * gz * bx * by * bz;
        uint64_t *output = *(uint64_t **)args[1];
        assert(!shared && !stream && !extra && launches < 2 && modulus);
        assert(*(uint64_t *)args[4] == 0 && *(uint64_t *)args[5] == lanes);
        for (uint64_t i = 0; i < lanes; i++) {
            uint64_t index = i % modulus;
            output[i] = 0;
            if (index < count) {
                uint64_t old = bits == 32 ? (*(uint32_t **)args[0])[index] : (*(uint64_t **)args[0])[index];
                uint64_t value = operation == 0 ? old + 1 : i + 1;
                uint64_t initial = bits == 32 ? UINT32_MAX - 15 : UINT64_MAX - 15;
                output[i] = old;
                if (operation != 2 || old == initial) {
                    if (bits == 32) (*(uint32_t **)args[0])[index] = value;
                    else (*(uint64_t **)args[0])[index] = value;
                }
            }
        }
        launches++; return 0;
    }
    if (function == (void *)10 || function == (void *)11 || function == (void *)12) {
        unsigned threads = bx * by * bz;
        uint64_t active = *(uint64_t *)args[2], slots = *(uint64_t *)args[3];
        assert(current != (void *)0x123 && gx && gy && gz && threads && threads <= 1024);
        assert(!stream && !extra && launches < 2);
        assert(*(uint64_t *)args[4] == 0 && *(uint64_t *)args[5] == (uint64_t)gx * gy * gz * threads);
        if (function != (void *)11) assert(slots && slots <= 1024 && shared == slots * 8);
        else assert(!shared);
        if (function != (void *)10) assert(threads % 32 == 0);
        uint64_t *input = *(uint64_t **)args[0], *output = *(uint64_t **)args[1];
        for (unsigned z = 0; z < gz; z++) for (unsigned y = 0; y < gy; y++) for (unsigned x = 0; x < gx; x++) {
            uint64_t indices[1024], live[1024], peer[1024];
            for (unsigned i = 0; i < threads; i++) {
                uint64_t px = (uint64_t)x * bx + i % bx, py = (uint64_t)y * by + i / bx % by;
                uint64_t pz = (uint64_t)z * bz + i / bx / by;
                indices[i] = px + (uint64_t)gx * bx * (py + (uint64_t)gy * by * pz);
                live[i] = indices[i] < active ? input[indices[i]] : 0;
            }
            for (unsigned i = 0; i < threads; i++) peer[i] = (i ^ 1) < threads && (i ^ 1) < slots ? live[i ^ 1] : 0;
            for (unsigned i = 0; i < threads; i++) if (indices[i] < active) {
                if (function == (void *)10) output[indices[i]] = live[i] + peer[i];
                else {
                    uint64_t *values = function == (void *)11 ? live : peer;
                    unsigned base = i & ~31u;
                    uint32_t votes = 0, sum = 0;
                    for (unsigned lane = 0; lane < 32; lane++) {
                        sum += (uint32_t)values[base + lane];
                        if (indices[base + lane] < active) votes |= UINT32_C(1) << lane;
                    }
                    output[indices[i]] = ((uint64_t)sum | ((uint64_t)votes << 32))
                        ^ ((uint64_t)(uint32_t)values[base] << 1)
                        ^ ((uint64_t)(votes != 0) << 62) ^ ((uint64_t)(votes == UINT32_MAX) << 63);
                }
            }
        }
        launches++;
        return 0;
    }
    if (function == (void *)3 || function == (void *)4) {
        assert(current != (void *)0x123 && gx && gy && gz && bx && by && bz);
        assert(!shared && !stream && !extra);
        assert(gx <= 2147483647 && gy <= 65535 && gz <= 65535);
        assert(bx <= 1024 && by <= 1024 && bz <= 64 && (uint64_t)bx * by * bz <= 1024);
        uint64_t *input = *(uint64_t **)args[0], *output = *(uint64_t **)args[1];
        uint64_t sx = (uint64_t)gx * bx, sy = (uint64_t)gy * by, sz = (uint64_t)gz * bz;
        assert(*(uint64_t *)args[2] == 0 && *(uint64_t *)args[3] == sx * sy * sz);
        assert(launches == 0);
        launches++;
        for (uint64_t z = 0; z < sz; z++) for (uint64_t y = 0; y < sy; y++) for (uint64_t x = 0; x < sx; x++) {
            uint64_t index = x + sx * (y + sy * z);
            uint64_t local = x % bx + bx * (y % by + by * (z % bz));
            uint64_t block = x / bx + gx * (y / by + gy * (z / bz));
            output[index] = function == (void *)3
                ? input[index] + x + y * 17 + z * 257 + local * 65537 + block * 1048576
                : input[index] ^ (sx + (sy << 20) + (sz << 40));
        }
        return 0;
    }
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
