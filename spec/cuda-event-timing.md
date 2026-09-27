# CUDA event timing

Import `src/lib/gpu/cuda_async.tv`:

```tv
fn cuda_event_create_timed(device: u64) -> Result<u64, CudaError>;
struct CudaElapsedTime { ready: u64, milliseconds_bits: u32 }
fn cuda_event_elapsed(start: u64, end: u64) -> Result<CudaElapsedTime, CudaError>;
```

Timed events use `cuEventCreate` flags zero. Existing `cuda_event_create` events
remain timing-disabled. Both use the existing record, query, sync, and close APIs.

Record a timed start event, enqueue the kernel, and record a timed end event on
the same stream. `cuda_event_elapsed(start, end)` calls `cuEventElapsedTime`
without event, stream, or context synchronization. Pending driver status returns
`Ok(CudaElapsedTime { ready: 0, milliseconds_bits: 0 })`. Poll later while doing
other work. Success returns ready=1 and the driver's IEEE binary32 millisecond
bits. These bits are not an integer millisecond count; use the IEEE numerical
utilities to interpret them. A ready result can legitimately represent zero time.

Both events must be live, recorded, timing-enabled, owned by the same device, and
recorded on the same stream generation. Start's recording must not follow end's
recording. Passing the same recorded event for both endpoints is permitted.
Cross-stream timing is not admitted by this checked API.

Success releases source-stream resource holds through the end event's prefix;
later uses and other streams' holds remain intact. Re-recording still refuses
while a stream holds an event. Generation checks protect reused resource slots.
The call restores the previous host-thread CUDA context, including pending and
error paths. Invalid pairs use boundary 12/status -1; a poisoned device uses -4.
Driver and context-restoration failures retain their status and existing poisoning
behavior. Pending status 600 is not an error and does not poison the device.

Timing measures the interval between recorded events. Other work and scheduling
can affect it; it is not an isolated kernel execution counter. Completed events
are required to obtain a duration, even though the query itself does not wait.

`tests/gpu/check_cuda_timing.py` checks raw/O1/O3 code against the CUDA driver
mock, including the binary32 pointer ABI, pending and zero-duration results,
prefix retirement, invalid pairs, stale handles, and driver/context failures.
Synchronization counters remain zero during elapsed queries. Hardware timing
accuracy and performance require GPU validation.
