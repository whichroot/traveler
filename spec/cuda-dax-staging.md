# Double-buffered DAX uploads

Import `src/lib/gpu/cuda_dax.tv` and use:

```tv
struct CudaDaxStaging {
    slot0: u64,
    slot1: u64,
    event0: u64,
    event1: u64,
    chunk_bytes: u64,
}

fn cuda_upload_dax_async(stream: u64, destination: CudaArgument,
                        source: *DaxView, offset: u64, bytes: u64,
                        staging: *CudaDaxStaging) -> Result<u64, CudaError>;
```

The staging record **borrows** two distinct pinned-buffer IDs from
`cuda_pinned_alloc` and two distinct event IDs from `cuda_event_create`.
It allocates and owns no resources. Each pinned buffer must hold at least
`chunk_bytes` bytes, which must be nonzero. All resources and the destination
buffer must belong to the supplied stream's device handle.

## Submission and completion

The helper copies each source chunk into alternating pinned buffers with
`cuda_pinned_stage_dax`, queues `cuda_upload_async`, and records that slot's
event. Before overwriting a previously submitted slot, it waits for that event.
Thus CPU staging of the next chunk can overlap the preceding GPU upload.

CPU staging remains synchronous: the call has read all requested source bytes
when it returns successfully. It does not wait for the final uploads or issue a
stream/context-wide synchronization. Its success value is the final recorded
event ID; synchronizing that event completes the transfer prefix on the stream.
For an empty transfer, it returns zero and records no event.

```tv
let completion: u64 = cuda_upload_dax_async(stream, device_view, source,
    offset, count, &staging)?;
if completion != 0 {
    cuda_event_sync(completion)?;
}
```

Alternatively, subsequent GPU work on the same stream follows the uploads in
order. Another stream can wait on the returned event. The destination's unused
suffix is unchanged; the final chunk uses only its requested byte count.

## Lifetime and access rules

- Keep the DAX mapping alive and its requested bytes stable until the call
  returns. CPU copies are **non-atomic** and do not provide a concurrent snapshot.
  No CUDA registration or writable mapping of the DAX source is required.
- Exclusively control the staging record, its buffers/events, and submission on
  the supplied stream during the call. The helper is a multi-operation protocol,
  not a transaction against concurrent callers.
- The slots and events must have no outstanding tracked users on entry. Keep
  them alive until transfer completion. Checked APIs refuse premature pinned
  writes, buffer destruction, and event re-recording while uses remain pending.
- Keep the destination alive through completion. Event-prefix retirement does
  not release resources retained by later commands or other streams.
- Wait for completion before reusing the staging record for another call.
  CUDA may finish early, but the checked runtime must observe completion first.

Validation checks source/destination ranges, source-address overflow, distinct
slots/events, ownership, slot capacity, busy resources, command-sequence space,
and overlap between the requested source range and either pinned staging range.
It finishes before the first copy or upload. Empty transfers still validate
resources and ranges, but may use a null source data pointer.

Validation failures use copy boundary 15: `-1` invalid arguments, `-2` sequence
capacity, `-4` poisoned device, or `-5` busy staging resources. Copy/event/driver
failures propagate their existing boundary and status.

On an error after submission begins, earlier chunks may already be queued or
complete; there is no rollback. The caller still owns all resources. Synchronize
the supplied stream to drain outstanding work before reuse or destruction;
an event whose recording failed is not a completion witness. Failed driver
operations retain the existing conservative resource holds and device poisoning.

## Verification

`tests/gpu/check_cuda_dax_staging.py` uses the deferred-copy CUDA mock, without
CUDA hardware or a DAX device. It checks byte-exact destinations, source release
after submission, pinned/destination guards, short and empty transfers, slot
reuse, pending completion on return, validation refusals, and failure retention.
Counters verify at most two outstanding copies and waits only when a slot must
be reused. Raw and available optimized profiles run at one and four CPU threads.

This verifies the overlap mechanism and lifetime contract. Bandwidth and actual
CPU/DMA overlap require a separate hardware measurement.
