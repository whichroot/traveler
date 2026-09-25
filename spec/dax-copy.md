# DAX bulk copies

`src/lib/mem/dax.tv` provides two copy contracts:

| API | Source reads | Intended use |
|---|---|---|
| `dax_copy(view, offset, destination, bytes)` | Relaxed atomic, one byte at a time | Concurrent per-byte observation |
| `dax_copy_bulk(view, offset, destination, bytes)` | Ordinary non-atomic loads | Copying stable bulk data |

Neither operation promises a snapshot of a concurrently changing range.
`dax_copy` retains its per-byte atomic semantics; it is not the throughput path.

## Non-atomic bulk copy

`dax_copy_bulk` checks the source view and byte range, pointer-address overflow,
null pointers for nonempty copies, and source/destination overlap. Invalid
arguments terminate with the existing DAX trap, exit status 96. Overlap is
refused, including an identical source and destination for a nonempty copy.
The caller supplies writable destination storage of at least `bytes` bytes.

Keep the mapping alive and the source stable throughout the call. Exclude
concurrent writes to the source and concurrent accesses to the destination.
No atomic observation or persistence guarantee is provided. Use this operation
for normal cacheable memory, including read-only device-DAX mappings of stable
data, rather than device registers or MMIO.

The implementation uses aligned **eight-byte** `u64` loads and stores when
source and destination have matching alignment modulo eight. A byte prefix
aligns both addresses; a byte suffix handles the remainder. Differently aligned
ranges use an ordinary byte loop. These non-atomic loops permit LLVM
optimization and vectorization; machine vector width is not an API guarantee.
The word loop uses the existing parallel alias checks. No bytes outside the
requested ranges are read or written.

A zero-byte copy validates the view and range but does not dereference either
pointer. Null data pointers and a position at the end of the view are valid for
that case. The view pointer itself must still be valid.

```tv
import "../src/lib/mem/dax.tv";

fn copy_weights(view: *DaxView, output: *u8, count: i64) {
    dax_copy_bulk(view, 0, output, count);
}
```

`cuda_pinned_stage_dax` already uses the non-atomic `cuda_pinned_write`/`memcpy`
path. Its CUDA resource checks and pinned-buffer ownership apply in addition to
the source lifetime and stability requirements above.
