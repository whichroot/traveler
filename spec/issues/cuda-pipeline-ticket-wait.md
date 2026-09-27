# GPU-side dependencies on DAX pipeline tickets

Status: missing API; contract design pending. Priority: lower.

## Current behavior

An async pipeline ticket can represent a CPU job that has not queued any CUDA
copy. The current API exposes completion query/wait, not a GPU-side ticket wait.
Its staging events are private and reused. `cuda_stream_wait_event` only accepts
an already recorded event; waiting on an unrecorded event does not establish a
dependency on a future recording.

## Required contract

Distinguish **accepted**, **fully enqueued with a recorded completion event**,
and **GPU complete**. A successful compute-stream dependency must order every
upload byte for that ticket before subsequently queued consumer commands.
Retain the correct event recording across record reuse and concurrent consumers.
An ordinary event ID from the staging ring must not become a public ticket.

The first bounded API may return pending/busy until the ticket's final completion
event has been recorded. It can then enqueue a CUDA stream wait without waiting
for DMA completion on the host. Report that readiness explicitly: success must
mean the dependency has actually been queued. Reject invalid/cross-device
tickets, failed or cancelled work, and forbidden stream reservations.

If Jane requires immediate acceptance before CPU staging finishes, a simple
event-wait wrapper is insufficient. That stronger API must defer dependent
consumer submission or establish another supported future-signal mechanism.
Settle this requirement before fixing the public signature.

## Delivery and acceptance

1. Confirm whether an enqueue-ready/pending result meets Jane's scheduling needs.
2. Specify per-ticket recording lifetime, multiple consumers, late waits after
   ticket retirement, close behavior, and bounded resource exhaustion.
3. Define cycle avoidance for upload and compute dependencies and error behavior
   if a producer fails after a dependency request is accepted.
4. Prove with paused CPU staging that a consumer never overtakes its upload.
   Exercise slot reuse, retired tickets, multiple compute streams, partial
   failures, stale handles, and cleanup without host stream synchronization.
5. Run native prefetch/compute overlap and decode parity gates. Keep the existing
   polling-before-consumer contract until the new dependency gate passes.

Coordinate with the [stream synchronization lock fix](cuda-stream-sync-lock.md)
and [completion observation](dax-pipeline-completion-latency.md), but preserve
separate commits and acceptance results for each issue.
