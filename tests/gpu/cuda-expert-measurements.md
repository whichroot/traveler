# Expert placement measurements

## Scope and environment

Recorded 2026-09-23 on Jane, RTX PRO 6000 Blackwell Workstation Edition, driver
595.99.02, native SM120, LLVM 21.1.8. The driver reports 128 MiB of L2 cache.
The isolated workspace is `/tmp/traveler-experts.e1ZsPC`, based on checkpoint
`40fa34b`. The bootstrap compiler is unchanged; host code uses the default raw
optimization profile. No GPU compute processes were listed before execution.
Clocks, power limits, CPU affinity, and NUMA placement were not changed.

[Raw JSONL data](cuda-expert-measurements.jsonl) contains all 360 native samples,
source hashes, and each request's host-observed completion time. These are four
sizes × three traces × ten variants × three trials. All results passed full-element
verification. The portable deferred driver gate checks 30 cases, independent
Python cache-model counters, actual upload-byte totals, and leak-free cleanup with
zero context-wide waits. The complete portable GPU suite also passes.

This is a synthetic placement experiment, not a neural-network expert benchmark.
Weights originate in ordinary host memory, not DAX. Each request computes
`(weight[i]*3 + request_id)*5 + 7`, modulo 2^64, over one expert's array. The split
version writes and rereads an intermediate; the fused version produces the same
output in one kernel. Every result element is checked against the equivalent CPU
formula `weight[i]*15 + request_id*5 + 7` after timing.

The expert bank has eight arrays. Per-expert sizes are 64 KiB, 1 MiB, 8 MiB, and
32 MiB. The largest bank is 256 MiB, larger than the reported L2 capacity. This
does not establish hardware cache-hit rates: the measured cache counters describe
the software residency directory, including in-flight loads.

## Controlled variants

There are 32 requests per batch. All request IDs and routes are available before
execution. The traces are:

- **Hot:** six requests out of each eight alternate experts 0/1; the other two
  rotate through experts 2–7.
- **Uniform:** round-robin through all eight experts.
- **Thrash5:** round-robin through five experts, exceeding the four-slot cache.

| Variant | GPU weight slots | Pinned slots / queue depth | Lookahead | Other change |
| --- | ---: | ---: | ---: | --- |
| `stream_serial` | 1 | 1 | 0 | Upload every request; split kernels |
| `stream_n1` | 2 | 2 | 1 | Upload every request; split kernels |
| `cache_demand` | 4 | 2 | 0 | Reuse cached weights |
| `cache_n1` | 4 | 2 | 1 | Reuse cached weights |
| `cache_n3_q4` | 4 | 4 | 3 | Deeper staging/compute queue |
| `cache_n1_q4` | 4 | 4 | 1 | Queue-depth control for n+3 |
| `batch8_n1` | 4 | 2 | 1 | Stable expert grouping within windows of eight |
| `cache_n1_fused` | 4 | 2 | 1 | Fused kernel |
| `resident_split` | 8 | 2 | 0 | Preload all weights before timing |
| `resident_fused` | 8 | 2 | 0 | Preload all weights; fused kernel |

GPU weight capacity and pinned staging capacity are independent. An initial
prototype tied one pinned buffer to each weight slot; its timings are excluded
from this report because it changed both budgets in cache-size comparisons.
Changing queue depth here also changes pinned staging capacity, as shown above.

The cache evicts the least recently prepared unreserved slot. Lookahead reserves
slots until their requests have been enqueued. A copy stream waits on the last
compute-use event before overwriting an evicted slot. A compute stream waits on
the slot's upload event before using it. Pinned slots are reused only after their
last upload completes. Both split kernels execute in order on the compute stream.

Lookahead has perfect routing information. Every prefetched load is consumed;
unused speculative-prefetch bytes are zero by construction. This measures the
scheduling opportunity, not predictor accuracy. Preloading all eight experts for
Thrash5 transfers three unused experts, which is a separate preload cost.

## Timing boundaries

Each size runs in a separate process with a 250 ms GPU warm-up. Variant order
rotates across trials. The source arrays, trace/order construction, allocations,
module loading, and warm-up precede timing. Bounded-cache variants start with an
empty residency directory every sample; hardware caches are not explicitly flushed.

The measured interval includes CPU staging, checked submissions, transfers,
kernels, queue backpressure, and final stream completion. All 32 outputs remain
device-resident. Final downloads and complete CPU validation are outside timing;
results therefore cannot be compared directly to the earlier staging benchmark
that included timed downloads.

Full-residency variants report preload time separately. Their warm batch time is
not a cold-start result; preload plus batch still excludes allocation, module
loading, and source generation. The checked module-load call is recorded in the
metadata, but is not an isolated JIT measurement and may be affected by driver
caching or deferred work.

`staging_ns` is host time inside the checked CPU-to-pinned write calls.
`pin_wait_ns` and `queue_wait_ns` include event-query/synchronization overhead,
not just blocked GPU time. Pending-query counts are recorded separately.
`completed_ns` contains host-observed completion from batch execution start,
indexed by original request ID. Observations can be later than actual GPU
completion. Its p95 is an observed batch-response statistic, not service latency
under a request-arrival process. Group formation latency is not modeled.

Nsight Systems and Nsight Compute were unavailable in the environment. There are
no device timelines, isolated transfer/kernel timings, or hardware cache counters
in this result set. All three samples, including first-trial outliers, are retained.
The small sample count supports exploratory comparisons rather than tight claims
about percent-level differences.

## Main results

Median milliseconds per 32 requests, at **1 MiB per expert**:

| Variant | Hot | Uniform | Thrash5 |
| --- | ---: | ---: | ---: |
| Stream, serial | 3.413 | 3.411 | 3.437 |
| Stream, n+1 | 2.610 | 2.605 | 2.604 |
| Cache, demand | 1.105 | 2.615 | 2.611 |
| Cache, n+1 | 1.072 | 2.610 | 2.604 |
| Cache, n+3, depth 4 | 1.012 | 2.586 | 2.588 |
| Cache, n+1, depth 4 | 1.028 | 2.602 | 2.610 |
| Batch eight, n+1 | 1.087 | 2.609 | 1.715 |
| Cache, n+1, fused | 1.011 | 2.539 | 2.548 |
| Resident, split | 0.302 | 0.317 | 0.304 |
| Resident, fused | 0.207 | 0.199 | 0.204 |

Resident preload medians at this size are approximately 0.67–0.68 ms for 8 MiB.
For context, hot n+1 samples range from 1.070 to 2.257 ms, and hot streamed n+1
from 2.604 to 5.851 ms. The raw data preserves these outliers.

### Residency and batching change transfer volume

| Policy | Hot uploads / hits | Uniform uploads / hits | Thrash5 uploads / hits |
| --- | ---: | ---: | ---: |
| Always upload | 32 / 0 | 32 / 0 | 32 / 0 |
| Four-slot cache, original order | 10 / 22 | 32 / 0 | 32 / 0 |
| Four-slot cache, batch eight | 10 / 22 | 32 / 0 | 20 / 12 |
| Preloaded eight-slot resident | 0 / 32 | 0 / 32 | 0 / 32 |

The resident row additionally uploads eight experts before timing. Counts are
identical across sizes and trials. The hot trace benefits from reuse; the uniform
trace cannot reuse weights under this four-slot LRU policy. Batching improves the
Thrash5 reuse distance enough to remove 12 uploads without changing arithmetic.

At 32 MiB per expert, Thrash5 n+1 transfers 1,024 MiB and takes **90.025 ms**;
batching transfers 640 MiB and takes **57.268 ms**. Their sample ranges are
89.704–90.511 ms and 57.241–57.327 ms, respectively.

### The host staging path dominates large misses

At 32 MiB per expert on the uniform trace:

| Variant | Batch median | CPU staging median | Queue-wait-call median |
| --- | ---: | ---: | ---: |
| Stream, serial | 110.814 ms | 90.592 ms | 19.608 ms |
| Cache, n+1 | 93.755 ms | 92.429 ms | 0.710 ms |
| Cache, n+3, depth 4 | 93.789 ms | 92.502 ms | 0.681 ms |
| Cache, n+1, fused | 93.676 ms | 92.480 ms | 0.704 ms |

CPU staging occupies nearly the entire elapsed interval on the pipelined miss
path. Additional lookahead or fusion has little effect on that path. The host
measurements identify a staging bottleneck; they do not isolate CPU cache, NUMA,
or memory-bandwidth causes within it.

### Deeper buffering and fusion are conditional

On the hot 8 MiB trace, cache n+1 takes **7.643 ms** with two staging slots versus
**8.142 ms** with four. CPU staging medians increase from 6.496 to 7.084 ms even
though upload counts are unchanged. More buffering is not a universal win.

On the resident hot trace, split → fused medians are:

| Expert size | Split | Fused |
| --- | ---: | ---: |
| 64 KiB | 0.301 ms | 0.196 ms |
| 1 MiB | 0.302 ms | 0.207 ms |
| 8 MiB | 0.328 ms | 0.239 ms |
| 32 MiB | 1.365 ms | 1.388 ms |

Fusion removes 32 launches and the nominal intermediate write/read traffic of
`2 × 32 × expert_bytes`. That is logical traffic, not measured DRAM traffic.
It helps the smaller resident workloads but does not improve the largest case.
Hardware cache effects and kernel execution details require device-side profiling
before assigning a cause to the latter result.

The resident hot 32 MiB case also needs **25.646 ms** of preload before its
1.365 ms split-kernel batch. Keeping that cost visible is essential when deciding
whether residency will be reused across enough future requests.

## Reproduce and inspect

Use the command in `BUILD.md`. `--sizes 1048576` selects just the 1 MiB experiment.
The JSONL first line is metadata; remaining lines are raw samples. The Python
runner's `summarize(records)` function derives medians, ranges, upload/preload
volumes, unused preload bytes, host wait times, and observed completion p95.
