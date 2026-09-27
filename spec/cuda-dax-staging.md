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

fn cuda_upload_dax_parallel(stream: u64, destination: CudaArgument,
                           source: *DaxView, offset: u64, bytes: u64,
                           staging: *CudaDaxStaging, threads: i32) -> Result<u64, CudaError>;
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

`cuda_upload_dax_parallel` uses the same checks and submission protocol. It creates
one temporary worker pool per nonempty multi-thread call and reuses it for every
chunk. Each chunk activates up to `threads` workers over disjoint byte ranges.
The first remainder workers copy one extra byte. The active count is capped at
the chunk's byte count; a one-thread call uses the serial copy path.
`threads` must be positive, including for empty transfers. This explicit worker
count is independent of `TRAVELER_THREADS`. All active workers finish copying
before the chunk is uploaded.

## Persistent worker pools

```tv
fn cuda_dax_pool_create(threads: i32) -> Result<u64, CudaError>;
fn cuda_dax_pool_close(pool: u64) -> Result<u64, CudaError>;
fn cuda_upload_dax_pooled(stream: u64, destination: CudaArgument,
                         source: *DaxView, offset: u64, bytes: u64,
                         staging: *CudaDaxStaging, pool: u64) -> Result<u64, CudaError>;
```

Create a pool once and pass its checked handle to successive uploads. Its fixed,
positive thread count is independent of `TRAVELER_THREADS`. A one-thread pool
copies on the caller; larger pools start all workers at creation. Idle workers
sleep on condition variables. Dispatch reuses descriptors and creates, joins,
and allocates nothing. Pool close wakes and joins workers.

A pool accepts one upload call at a time. Concurrent submission or close reports
busy. Separate pools operate independently. The pool borrows source and pinned
addresses only during a copy and owns no CUDA device resources. It can be closed
after submission while the final GPU upload is still pending. Stale pool handles
are rejected. Keep staging resources and destinations alive until completion.

Partial startup failure stops and joins workers already started. A cleanup
failure returns the pool handle in `CudaError.resource`; retry pool close to
finish cleanup. A pool with unfinished close cannot accept new submissions.

## Checked copies and completion

Pinned CPU reads and writes validate and increment the buffer's `users` count
under the resource lock, then copy outside that lock. Parallel staging retains
the whole checked chunk while its workers copy their assigned ranges. The hold
is released after copying finishes. During the copy, checked writes, reads,
destruction, async DMA, and graph launches that use the buffer report busy.
Operations on unrelated resources can proceed.

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
Nonpositive counts on the parallel uploader report boundary 15, status `-1`.
Pool errors use boundary 19: `-1` invalid count/handle, `-2` resource or dispatch
generation capacity, `-5` busy/closing pool, or a positive pthread error status.
Pool creation fails before the parallel uploader queues any chunks. Upload errors
release the pool's busy hold; earlier CUDA work retains its completion holds.

On an error after submission begins, earlier chunks may already be queued or
complete; there is no rollback. The caller still owns all resources. Synchronize
the supplied stream to drain outstanding work before reuse or destruction;
an event whose recording failed is not a completion witness. Failed driver
operations retain the existing conservative resource holds and device poisoning.

## Cross-call pipelines with caller-side staging

Import `src/lib/gpu/cuda_dax_pipeline.tv` for an owned ring of staging records:

```tv
fn cuda_dax_pipeline_create(stream: u64, pool: u64, depth: i32,
                            chunk_bytes: u64) -> Result<u64, CudaError>;
fn cuda_dax_pipeline_submit(pipeline: u64, destination: CudaArgument,
                            source: *DaxView, offset: u64, bytes: u64)
                            -> Result<u64, CudaError>;
fn cuda_dax_pipeline_query(pipeline: u64, ticket: u64) -> Result<u64, CudaError>;
fn cuda_dax_pipeline_wait(pipeline: u64, ticket: u64) -> Result<u64, CudaError>;
fn cuda_dax_pipeline_drain(pipeline: u64) -> Result<u64, CudaError>;
fn cuda_dax_pipeline_close(pipeline: u64) -> Result<u64, CudaError>;
```

The pipeline borrows the stream and pool, and owns `depth` records. Each record
contains two pinned buffers and two events. Depth must be 2 through 255 and fit
the checked resource table. Chunk size must be positive. Creation checks total
size arithmetic and table capacity, then rolls back partial allocation failures.
Pinned capacity is `2 * depth * chunk_bytes`; depth two owns four pinned buffers
and four events. Submission allocates nothing and reuses the persistent workers.

Submit finishes reading the requested DAX bytes before returning, but leaves
final DMA pending. A second call uses a different record so its CPU copies can
overlap the first call's pending transfer. Internal slot reuse within one call
still waits for that slot's event. Hardware completion and these internal waits
can retire earlier transfers; depth is an upper bound on records in flight.

```tv
let pool: u64 = cuda_dax_pool_create(4)?;
let pipeline: u64 = cuda_dax_pipeline_create(stream, pool, 2, chunk_bytes)?;
let a: u64 = cuda_dax_pipeline_submit(pipeline, target_a, source_a, 0, bytes_a)?;
let b: u64 = cuda_dax_pipeline_submit(pipeline, target_b, source_b, 0, bytes_b)?;
if a != 0 { cuda_dax_pipeline_wait(pipeline, a)?; }
// The first record can now stage another expert while the second transfer runs.
if b != 0 { cuda_dax_pipeline_wait(pipeline, b)?; }
cuda_dax_pipeline_close(pipeline)?;
cuda_dax_pool_close(pool)?;
```

For nonempty transfers, submit returns a monotonically increasing ticket local
to that pipeline, not a raw event ID. Query returns zero for pending and one for
complete. Wait returns one on completion. Observing a later ticket completes
earlier tickets on the bound stream. Retired tickets remain complete even after
their internal events are reused. Pair tickets with their originating pipeline;
numeric tickets are not globally unique. Zero and unissued tickets are invalid.
An empty transfer validates but consumes no record or ticket and returns zero,
including when all records are occupied. Skip query/wait for that zero result.

If no record is idle, submit queries the oldest pending ticket once. If it still
cannot reuse a record, it returns busy without copying source bytes or queuing
new work. Wait for the oldest outstanding ticket and retry. This makes full-ring
backpressure explicit; submission itself remains synchronous for CPU copying.

Only one host operation may use a pipeline at a time. Concurrent operations
report busy. Keep the borrowed stream alive, and keep the pool alive for further
submissions. The pool may close while previous transfers remain pending; ticket
observation and pipeline cleanup do not require it. Exclusively control stream
submission during pipeline operations. Keep destinations alive through transfer
completion and any consuming GPU work. Record reuse does not release a device
destination still needed by a consumer.

Same-stream GPU commands follow uploads in order. For a consumer on another
stream, wait for its ticket before scheduling that consumer. Internal reusable
events are private and are not cross-stream dependency handles.

Pipeline errors use boundary 20: `-1` invalid handle, depth, chunk size, or ticket;
`-2` size, resource-table, or ticket capacity; `-4` poisoned device at creation;
and `-5` busy, closing, full, or awaiting drain. Copy, pool, and driver errors keep
their existing boundaries. Validation failures leave the pipeline reusable.
An error after copying/submission may have begun blocks further submissions
until drain, and issues no ticket for the partial transfer.

Drain synchronizes the bound stream, including partial work with no final event.
On success it retires pending records. It does not repair device poisoning.
Close refuses pending work or an unresolved submission error; wait or drain first.
Close frees only owned resources. If cleanup fails, the error's resource is the
pipeline ID: retry close to finish cleanup. Already destroyed resources are not
destroyed twice, including when context restoration failed after destruction.

## Non-blocking submission and prefetch

Use the opt-in constructor from `src/lib/gpu/cuda_dax_pipeline.tv`:

```tv
fn cuda_dax_pipeline_create_async(stream: u64, pool: u64, depth: i32,
                                  chunk_bytes: u64) -> Result<u64, CudaError>;
```

The same submit/query/wait/drain/close functions operate on this pipeline. The
constructor above selects asynchronous CPU staging; `cuda_dax_pipeline_create`
retains the caller-side staging contract described in the previous section.

Creation reserves a dedicated upload stream and the worker pool until close,
preallocates a bounded job ring and descriptor arrays, and starts one persistent
coordinator. Each async record also owns a separate ticket-completion event,
for five checked resources per record. The pinned capacity remains
`2 * depth * chunk_bytes`. The stream
cannot already belong to another pipeline. Checked external stream submissions,
stream query/sync/close, pool use/close, and competing pipeline construction
refuse while reserved. The coordinator is the authorized stream submitter.

Submit validates bounds, retains the destination allocation, copies the source
view and destination descriptors, publishes a FIFO ticket, and returns. It does
not read source bytes, allocate storage, create or join threads, wait for CPU
workers, or wait for GPU completion. Metadata uses finite scans and short locks;
the API does not promise lock-free or hard real-time execution. A full ring
returns boundary 20/status -5 without consuming a ticket or retaining arguments.
An empty transfer validates and returns zero without occupying a ring slot.

The DaxView descriptor itself can be discarded after submit. **Keep its backing
mapping readable and its source bytes unchanged through successful ticket
completion or successful drain.** Views do not own mappings, so this lifetime is
a caller obligation. Queued destinations are retained before any CUDA command
exists. CPU staging runs on the coordinator/persistent pool even with one worker.
The coordinator queries staging-slot events before reuse and never waits for
slot completion while holding the resource-table lock.

Query returns pending for queued, CPU-active, and GPU-pending work, then complete
after DMA completion. It does not synchronize. Completed FIFO prefixes release
the job holds and free ring slots without requiring caller polling. Retired
successful tickets remain complete after reuse. Wait explicitly waits for the
same result. Concurrent submit/query operations are supported; closing refuses
while another host operation retains the pipeline.

`cuda_dax_pipeline_set_poll_interval(pipeline, microseconds: u32)` selects the
coordinator's pending-GPU and slot-reuse polling backoff for an async pipeline.
Values from 1 through 1,000,000 are accepted; zero, larger values, and caller-side
pipelines refuse with boundary 20/status -1. The default remains 1,000 microseconds.
For decode-sensitive workloads, select a shorter interval and measure CPU use
and completion-observation latency on the target host. The setting affects the
next backoff; it does not interrupt a sleep already in progress. OS scheduling
can make actual sleeps longer, so this is not a completion deadline.

Blocking ticket waits and drain callers use condition notifications when state
changes; they do not add a separate 1 ms polling cycle. Multiple waiters are
notified on completion or failure. Idle coordinators also sleep on a condition
variable, regardless of the selected GPU polling interval.

Use a separate compute stream for consumers. Poll upload completion or install
the ticket dependency described below before launching its consumer. Work on the current expert can run while
the next expert stages and uploads. **Returning from submit does not establish
CUDA enqueue order for a dependent kernel.** An event that the coordinator has
not yet recorded is not a future promise; private slot events are not dependency
handles.

```tv
fn cuda_dax_pipeline_try_wait_stream(pipeline: u64, ticket: u64,
                                     stream: u64) -> Result<u64, CudaError>;
```

For async pipelines, this call returns `Ok(0)` while the ticket is still queued
or staging and its final completion event is not yet recorded. It enqueues
nothing in that case; retry before queuing the consumer. `Ok(1)` means a GPU
stream wait has been enqueued, or that the ticket is already known complete and
needs no wait. The call does not synchronize on the host. Multiple compute
streams can depend on the same ticket. Streams must have the same device owner;
reserved upload streams, invalid/stale tickets, and failed tickets refuse.

Ticket events are separate from chunk-slot events. A successful stream wait
retains that recording until the consumer stream's checked completion releases
it. Its ring record cannot be reused while retained, even if its upload has
completed. This bounded retention can make submit return busy and close refuse;
query/synchronize the consumer stream to release completed holds. Retired tickets
remain successful after record reuse. Keep destination buffers alive through
their consumer's use as with any async launch.

```tv
let pipeline = cuda_dax_pipeline_create_async(upload_stream, pool, 2, chunk_bytes)?;
let next = cuda_dax_pipeline_submit(pipeline, next_weights, next_source, 0, bytes)?;
// Enqueue independent current-expert work on compute_stream here.
if cuda_dax_pipeline_query(pipeline, next)? == 1 {
    // next_weights is ready for its consumer on compute_stream.
}
```

After an accepted job fails, its ticket and later accepted tickets report the
saved error, while earlier completed tickets retain success. New submissions
refuse. A failed ticket alone does not establish source/DMA quiescence: drain
before releasing its source mapping. Drain gates new submits, finishes healthy
queued work or cancels unstarted work after failure, waits for CPU reads to end,
then synchronizes the stream outside the resource-table lock. A failed drain
keeps holds for retry. Successful drain releases holds but preserves failed
ticket outcomes and device poisoning. Recreate a failed pipeline for further
submissions; drain does not make it reusable.

Close refuses pending or undrained failed work. Once quiescent, it stops and
joins the coordinator, destroys owned resources, and releases stream/pool
reservations. Thread join or resource cleanup failure returns the pipeline ID;
retry close. Partial creation rolls back, or returns a retained pipeline ID if
cleanup itself needs retry.

## Verification

`tests/gpu/check_cuda_dax_staging.py` uses the deferred-copy CUDA mock, without
CUDA hardware or a DAX device. It checks byte-exact destinations, source release
after submission, pinned/destination guards, short and empty transfers, slot
reuse, pending completion on return, validation refusals, and failure retention.
Counters verify at most two outstanding copies and waits only when a slot must
be reused. Raw and available optimized profiles run at one and four CPU threads.

`tests/gpu/check_cuda_dax_parallel.py` repeats those cases with explicit worker
counts and adds uneven slices, more workers than bytes, nonpositive counts, and
partial thread-start failures. Instrumented copies pause all four workers at
once to prove worker concurrency. Paused serial reads/writes and parallel copies
also check lock availability, the CPU hold, DMA refusal, and unrelated progress.

`tests/gpu/check_cuda_dax_pool.py` checks thread counts across repeated uploads,
changing job generations, independent pools, busy and stale handles, closing
before DMA completion, partial synchronization/thread initialization, failed-join
cleanup retry, and reuse after upload failures. Both worker gates run raw and
available optimized profiles at runtime thread settings one and four.

`tests/gpu/check_cuda_dax_pipeline.py` pauses the second call's workers while the
first call's DMA is pending. It checks depths two/three, repeated record reuse,
stable tickets, source release, guards, full-ring refusal without copying,
prefix retirement, worker reuse, validation/capacity errors, partial submission
drain, and allocation/destruction failure cleanup. It runs raw/O1 and supported
O3 profiles at runtime thread settings one and four.

This verifies the overlap mechanism and lifetime contract. Bandwidth and actual
CPU/DMA overlap require a separate hardware measurement.

`tests/gpu/check_cuda_dax_nonblocking.py` uses deterministic CPU-copy latches and
deferred DMA to check that submission returns while workers are paused, full-ring
refusal, independent progress during slot reuse, descriptor snapshots, queued
resource holds, FIFO bytes and guards, stable tickets, drain during CPU copying,
asynchronous failure outcomes, and partial creation/close retry. It covers one
and four pool workers in raw/O1/O3 profiles at runtime thread settings one/four.
