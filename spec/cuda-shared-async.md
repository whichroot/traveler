# CUDA asynchronous shared copies

The `async-shared-u64-v1` capability requires SM80 and PTX 7.0. It composes with
the existing cooperative execution, storage, and numerical profiles. It does
not require full warps or opt in to a different arithmetic policy.

## Interface

`src/lib/gpu/shared.tv` declares:

```tv
extern "C" fn gpu_shared_async_copy_u64(buffer: *u64, count: u64, index: u64);
extern "C" fn gpu_shared_async_commit();
extern "C" fn gpu_shared_async_wait();
```

Initialize the shared arena first with `gpu_shared_init_u64` or
`gpu_shared_dynamic_init_u64`. Each physical thread copies `buffer[index]` into
the slot at its x-fastest block-local index. The source pointer and count must
be kernel captures. Count binds the buffer's checked read-only footprint in
u64 elements. The index may vary by thread.

If the destination slot lies outside the arena, the thread issues no copy.
If the slot exists but `index >= count`, the thread writes zero without reading
the source. Other slots retain their previous values. In-range copies lower to
`cp.async.ca.shared.global` with eight-byte size and naturally aligned addresses.
The arena and global view use the existing eight-byte alignment requirements.

## Completion and visibility

The initial bounded profile permits one issue statement per group and one
outstanding group. Calls must occur directly in the straight-line body of an
explicit cooperative NVPTX kernel.

```tv
gpu_shared_async_copy_u64(input, count, index);
gpu_shared_async_commit();
// Independent register work may run before waiting.
gpu_shared_async_wait();
gpu_block_barrier();
let value: u64 = gpu_shared_load_u64(peer_index);
gpu_block_barrier();
// The next copy may now reuse the arena.
```

Commit lowers to `cp.async.commit_group`. Wait lowers to
`cp.async.wait_group 0` and completes this thread's committed copies. The block
barrier establishes peer visibility. It cannot replace the wait. All lanes,
including those that issue no physical copy, execute commit, wait, and barrier.

The verifier rejects missing or duplicate issue/commit/wait transitions,
outstanding groups at kernel exit, and shared accesses or barriers while a
group is outstanding. Shared loads require a visibility barrier after writes.
Reusing storage after shared reads requires the existing reader-completion
barrier. A terminal wait can complete a copy without a subsequent barrier if
the shared result is never consumed. Ordinary per-lane shared stores after a
wait still follow the existing shared-write rules.

The arena-wide phase rules also prevent reads of a different shared region
while a copy is pending. Multiple outstanding groups and overlapping shared
buffers are outside this profile. Independent register/global work can occur
between issue and wait, subject to normal footprint and alias verification.

These device-local operations are distinct from host stream/event completion.
No physical overlap or performance improvement follows merely from emitting
the asynchronous instruction. The declarations have no host emulation body.

## Validation

`tests/gpu/check_shared_async.py` covers phase refusals, static and dynamic arena
lowering, O1, bounded source footprints, native peer visibility, zero fills,
out-of-arena destinations, and arena reuse. See
[K5-D/K6 validation](../tests/gpu/shared-async-prepare-validation.md).

Instruction semantics are defined by NVIDIA's
[PTX cp.async reference](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#data-movement-and-conversion-instructions-cp-async).
