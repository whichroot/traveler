# Checked registration of DAX upload sources

Status: proposed API; platform admission and implementation pending.
Priority: after W15, independently of the streaming-copy optimization.

## Report and current behavior

Jane reports successful GPU registration of about 61 GiB per process and wants
to register a hot expert window, with existing pinned staging for the rest.
The capacity is an observation for that environment, not a portable CUDA limit.
Exact flags, driver/kernel/GPU versions, mapping type, and failure status still
need to accompany the probe.

`cuda_ffi.tv` declares host register/unregister functions. The checked registry
does not expose a registered DAX resource. `cuda_upload_async` currently accepts
only owned pinned host buffers. The FFI comment that the driver refuses
file-backed mappings is too broad to serve as a platform capability contract;
registration success must be established on the actual DAX mapping and driver.

## Proposed public surface

Names below are proposals, not implemented APIs:

```tv
fn cuda_dax_register(device: u64, source: *DaxView, offset: u64,
                    bytes: u64) -> Result<u64, CudaError>;
fn cuda_dax_registered_view(registration: u64, offset: u64,
                           bytes: u64) -> Result<CudaArgument, CudaError>;
fn cuda_dax_unregister(registration: u64) -> Result<u64, CudaError>;
```

The returned view is an admitted **host upload source** for `cuda_upload_async`.
It does not grant kernels an unchecked device pointer or promise zero-copy
kernel access. Upload queues a host-to-device copy without a CPU staging copy.
Registration itself is an explicit setup operation and may block or fail.

Require bounded, aligned registration, checked arithmetic, device ownership,
generation checks, and a documented policy for overlapping registrations.
Never silently extend a registration beyond the caller's mapped window.
Retain registrations through all outstanding DMA uses. Unregister must refuse
while retained, must not unmap/free caller-owned DAX, and must remain retryable
after a driver failure. Keep the mapping alive until unregister succeeds and
source bytes unchanged while uploads read them. Read-only registration views
must be refused as download destinations or mutable pinned-storage views.

## Delivery and acceptance

1. Reproduce successful registration and the capacity failure on Jane's platform.
   Determine required mapping/registration flags and capability checks rather
   than assuming ordinary DRAM pinning or GPU-mapped-host flags are sufficient.
2. Add owned registration handles and bounded views; retain staging as the
   explicit fallback when a range cannot be registered. Expected admission or
   budget failures must not poison otherwise usable CUDA resources.
3. Verify stale/cross-device handles, bad alignment/ranges, overlapping ranges,
   empty ranges, multiple streams, in-flight unregister refusal, failed enqueue,
   context restoration, and cleanup retry with the driver mock.
4. Verify hot-window uploads and staged cold experts together, including budget
   exhaustion and eviction only after completion. Do not hardcode 61 GiB.
5. Validate byte parity, CPU-copy elimination, and non-waiting upload behavior
   on real DAX/CUDA hardware. Measure registration cost separately from steady
   state and record the actual capacity and statuses observed.
