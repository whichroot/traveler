# Release the resource lock during CUDA stream synchronization

Status: confirmed implementation behavior; fix pending. Priority: lower, with
ordering implications for future ticket dependencies.

## Current behavior

`cuda_res_async_complete` in `src/lib/gpu/cuda_async.tv` holds the global resource
lock across `cuStreamSynchronize` and `cuEventSynchronize`. Other threads cannot
enter checked CUDA operations while the driver call waits. Stream destruction
in `cuda_resource_close` also synchronizes under that lock.

The asynchronous DAX coordinator's explicit drain already performs its stream
synchronization outside the lock, using its exclusive stream reservation. That
special-case lifetime protection is not sufficient for general shared streams.

## Required contract

Retain the stream/event and owner context before releasing the lock for a
blocking driver wait. Concurrent close, event re-record, and slot reuse must
not invalidate the native handles. Restore the calling thread's prior CUDA
context and preserve existing driver-error poisoning and cleanup behavior.

Completion bookkeeping must release only a prefix known complete. In particular,
an enqueue after the wait began must not lose its buffer/module/event holds when
the waiter reacquires the lock. Define same-stream concurrent enqueue behavior
explicitly; conservative retention is preferable to premature release.

## Delivery and acceptance

1. Specify the retained handle generations, wait prefix, and concurrent enqueue,
   close, and event re-record contracts for stream and event synchronization.
2. Remove blocking waits from the global-lock critical section while preserving
   context restoration, failed-wait retention, and generation-aware completion.
3. Audit stream close and related synchronous paths for the same lock pattern;
   document any remaining lock-held waits instead of claiming a blanket fix.
4. Pause a driver synchronization call deterministically. Prove unrelated checked
   calls on another thread make progress before the wait is released.
5. Queue a later use during the paused wait. Prove it remains retained, alongside
   close/re-record refusal, shared-context aliases, failed waits, restoration
   failures, and successful retry. Run async, graph, pinned, and DAX gates.

No throughput claim follows from the lock change alone. Hardware scheduling
effects require separate before/after measurement.
