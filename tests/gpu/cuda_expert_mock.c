// SPDX-License-Identifier: Apache-2.0
#define cuModuleLoadDataEx mock_expert_module_load
#define cuMemcpyHtoDAsync_v2 mock_expert_upload
#include "cuda_driver_mock.c"
#undef cuModuleLoadDataEx
#undef cuMemcpyHtoDAsync_v2

static uint64_t expert_upload_bytes;
int cuMemcpyHtoDAsync_v2(void *dest, const void *src, size_t bytes, AsyncStream *stream) {
    int status = mock_expert_upload(dest, src, bytes, stream);
    if (!status) expert_upload_bytes += bytes;
    return status;
}

__attribute__((destructor)) static void expert_check_bytes(void) {
    assert(expert_upload_bytes == MOCK_EXPERT_UPLOAD_BYTES);
}

int cuModuleLoadDataEx(void **out, void *ptx, unsigned count, int *options, void **values) {
    int status = mock_expert_module_load(out, ptx, count, options, values);
    if (!status) *(char *)*out = 4;
    return status;
}
