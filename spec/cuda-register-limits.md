# Per-kernel CUDA register limits

Attach `#[cuda_max_registers(N)]` to a concrete `#[kernel]` function:

```tv
#[kernel]
#[cuda_max_registers(64)]
fn map(i: i32, input: *u32, output: *u32) {
    output[i] = input[i] + 1;
}
```

`N` is an integer literal from 1 through 255. Attribute order is immaterial.
Duplicate attributes, zero, out-of-range values, expressions, and placement on
non-kernel declarations are errors. The existing concrete-kernel restrictions
still apply; this attribute does not introduce generic kernel entries.

The value requests a maximum number of 32-bit registers per thread. The compiler
emits `"nvvm.maxnreg"="N"` on that LLVM kernel entry, which LLVM lowers to PTX
`.maxnreg N`. Other entries in the module are unaffected. Existing launch-bound
attributes remain in place. An absent attribute emits no register-limit request.
Host compilation does not change CPU allocation. Other device targets reject
this CUDA-specific annotation.

This is a backend allocation request, not an occupancy or performance guarantee.
ABI minimums can raise the effective allocation; register pressure can cause
spills. Measure the resulting register use, spills, and numerical output on the
target GPU. A register cap is not a block-size limit.

Kernel descriptors and package manifests carry optional `max_registers` metadata.
The packager verifies that the LLVM attribute and emitted PTX directive agree
with the descriptor. It does not rewrite PTX to insert limits or apply a global
assembler override. Package preparation checks the directive on cache reuse;
source and tool hashes invalidate entries when the attribute or compiler changes.
The checked runtime accepts the optional field. Existing packages without it
remain valid; older readers can reject packages containing the new field.

`tests/gpu/check_cuda_registers.py` checks mixed capped/uncapped entries,
independent and cooperative kernels, optimized LLVM lowering, package/runtime
validation, invalid attributes and metadata, and preparation cache identity.
