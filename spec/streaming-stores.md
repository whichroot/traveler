# Host streaming stores

Native x86-64 CPU codegen provides two builtins:

```tv
stream_store_u64(dst, index, value);
stream_store_fence();
```

Both builtins require standalone statements. The store destination is a `*u64`,
its index is an 8/16/32/64-bit integer or `usize`, and its value is `u64`.
An integer literal receives the `u64` value context. Address calculation and
operand evaluation follow `dst[index] = value`. The destination must be valid
for an eight-byte write and eight-byte aligned. Bounds and alignment are the
caller's responsibility, as with raw pointer indexing.

The store emits LLVM `!nontemporal` metadata with eight-byte alignment. It is
intended for large, contiguous output buffers that will not be reused soon by
the writing CPU. It is neither atomic nor a synchronization operation. The
backend controls instruction selection; the initial x86-64 lowering uses
scalar `movnti`, not an explicit AVX-512 vector store.

## Ordering and parallel loops

Call `stream_store_fence()` after a batch of stores, before reading the result,
publishing it to another thread, submitting a DMA upload, or freeing/reusing the
storage. The builtin emits `mfence` with a compiler memory clobber. This full
fence also orders subsequent CPU reads. It drains only the issuing thread;
cross-thread publication still requires the usual synchronization.

The compiler exposes streaming stores as indexed writes to the existing
independence and alias analysis. They do not bypass its admission checks.
Every generated parallel worker that issues streaming stores fences before
returning to the runtime, before completion is published. Keep the explicit
fence after the loop: it also covers serial execution and runtime alias fallback.
An explicit fence inside a loop prevents parallel admission.

```tv
fn stage_copy(dst: *u64, src: *u64, words: i64) {
    for i in 0..words {
        stream_store_u64(dst, i, src[i]);
    }
    stream_store_fence();
}
```

Function declarations with the builtin names retain ordinary function-call
semantics. Builtin streaming operations are refused by `--eval`, device
emission, AGX dispatch, and non-x86-64 native targets.

## DAX staging and persistence

The DAX staging workload reads a read-only persistent-memory mapping into
pinned **DRAM**, then uploads the DRAM buffer to VRAM. Use the stores on the
pinned destination and complete the copy, including every worker fence, before
`cuMemcpyHtoDAsync_v2`. CUDA event completion still controls when a pinned slot
can be overwritten for its next upload.

These builtins do not establish a platform-independent persistence guarantee.
Writing a persistent destination requires a separate contract for its mapping,
cacheability, persistence domain, and any cached stores mixed into the update.
The DAX-to-DRAM staging use does not write persistent media.

## Verification

On an x86-64 Linux host with LLVM tools:

```sh
python3 tests/check_stream_stores.py "$TVC_SELF" "$LLC" "$OPT" "$LINKER"
```

The gate checks instruction selection, per-worker fences, raw and O1 output
at one and four threads, non-cache-line-sized ranges, overlapping alias
fallback, guards, and unsupported uses. Bandwidth must be measured separately
on the staging workload; instruction selection alone is not a speedup result.
