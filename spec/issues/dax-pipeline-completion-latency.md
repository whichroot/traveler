# DAX pipeline completion observation latency

Status: confirmed implementation behavior; tuning pending. Priority: lower.

## Current behavior

`cuda_dax_async_pipeline.tv` uses `usleep(1000)` while waiting for a reusable
staging slot and when polling pending GPU work. Ticket wait and drain callers
also poll state with a 1 ms sleep. Idle coordinators use a condition variable.

The sleeps can delay slot reuse, record retirement, and consumer scheduling
after CUDA completion. One millisecond is the requested sleep interval, not a
measured or guaranteed maximum latency; scheduler delays can add more. The
existing mock correctness gate does not measure hardware notification latency.

## Proposed contract

Offer a bounded low-latency completion policy with an explicit CPU-cost tradeoff.
Preserve non-waiting submit/query and bounded queue capacity. Idle pipelines
must not spin indefinitely. A completion-notification mechanism must not hold
the global resource lock while waiting or call CUDA from a prohibited callback
context. Preserve FIFO ticket outcomes, resource holds, and failed-drain retry.

## Delivery and acceptance

1. Measure CUDA completion-to-ticket-ready and completion-to-slot-reuse latency
   separately from upload duration and host scheduling delay.
2. Compare bounded polling/backoff and notification-based candidates under idle,
   burst, and sustained loads. Include thread CPU time and wakeup counts.
3. Remove unnecessary caller-side sleep delay when completion state is already
   published. Specify defaults and any tuning control from the measured tradeoff.
4. Add deterministic missed-wakeup, shutdown, cancellation, slot-reuse, and
   failure tests; retain paused-worker and full-ring no-wait checks.
5. Publish baseline/changed p50/p95/p99 latency, CPU use, and workload parameters
   on hardware before claiming an improvement.

The [GPU ticket-wait issue](cuda-pipeline-ticket-wait.md) is separate: faster
host observation alone does not create a GPU dependency for an accepted job.
