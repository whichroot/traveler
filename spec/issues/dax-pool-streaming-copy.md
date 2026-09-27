# DAX pool copies with vector streaming stores

Status: proposed optimization; implementation and local measurement pending.
Priority: after W15 correctness.

## Report and current behavior

Jane reports that an AVX-512 non-temporal-store probe was 1.5× faster for
decode-shaped bursts. Raw before/after timings and probe configuration are not
available here. This is a reported result, not a repository performance claim.

`cuda_dax_pool_worker` and the single-worker branch of `cuda_dax_pool_copy` in
`src/lib/gpu/cuda_dax_pool.tv` currently call `memcpy`. Existing
[streaming-store builtins](../streaming-stores.md) promise scalar `u64` stores;
their current lowering is `movnti`, not explicit AVX-512 vector stores.

## Proposed contract

Provide a bounded DAX-to-pinned-DRAM copy policy with a portable fallback.
The optimized path must check CPU and OS vector-state support before using
AVX-512. Short or unsuitable ranges retain a measured fallback. Do not enable
AVX-512 for the whole binary or treat non-temporal metadata as proof of vector
instruction selection.

Every byte must match the ordinary copy, including unaligned heads, tails, and
uneven worker partitions. Do not read or write beyond either checked range.
Each worker must fence its own streaming stores before publishing completion;
the coordinator's fence cannot replace worker fences. GPU DMA may start only
after all writers have completed that publication. CUDA event completion still
controls staging-slot reuse. This path writes pinned DRAM, not persistent DAX.

## Delivery and acceptance

1. Obtain the probe, CPU/NUMA topology, chunk and burst distributions, thread
   counts, compiler flags, affinity, and raw before/after results.
2. Add explicit baseline/optimized selection for comparison, then choose any
   automatic threshold from measurements. Cover one and multiple workers.
3. Verify dispatch, emitted vector instructions, alignment, per-worker fences,
   guards, zero/short transfers, odd partitions, and unsupported-CPU fallback.
4. Run pooled and async-pipeline gates in raw/O1/O3 with paused workers and
   deferred DMA; verify no early publication or changed lifetime contract.
5. Measure both CPU copy time and end-to-end decode-shaped bursts, including
   cold/warm data, pinned memory, NUMA placement, and concurrent GPU uploads.
   Record baseline and changed absolute times, dispersion, and throughput.

Acceptance requires byte parity and the ordering proof before performance
evaluation. Any performance commit must contain before/after measurements.
