# W15: struct bindings must copy values

Status: confirmed defect; fix pending. Priority: correctness first.

## Report and evidence

Jane reports six struct-valued `let`/`var` forms that alias their memory source
at raw/O0 and O1. The defect changed one contract statistic; Jane's decode gate
caught it. The supplied reproduction location is
`audit/runtime/traveler-struct-alias/` in Jane's checkout. It is not present in
this repository checkout, so the original six cases have not been rerun here.

An independent reduced case reproduces both directions of aliasing:

```tv
struct Sample { value: u64 }
fn main() -> i32 {
    let source: *Sample = alloc(1);
    source[0] = Sample { value: 7 };
    let snapshot: Sample = source[0];
    source[0].value = 99;
    print(snapshot.value);
    source[0].value = 7;
    var copy: Sample = source[0];
    copy.value = 42;
    print(source[0].value);
    free(source);
    return 0;
}
```

Expected output is `7` then `7`. The current canonical compiler produced `99`
then `42` in raw IR and after LLVM 21 `default<O1>`, on the repository at
`a0f945a`. This is a frontend value-semantics defect, not an O1-only regression.
`codegen_stmt` in `src/tvc_self.tv` currently binds struct locals directly to
the initializer's storage register.

## Required behavior

[Language spec §12.4](../language-spec.md#124-value-semantics) requires a value
copy. Later writes to source storage must not alter a local snapshot; mutation
of a mutable local must not write back to the source. Pointer fields copy their
pointer values, not the objects they point to. Explicit pointer/reference
bindings must retain aliasing behavior.

## Delivery and acceptance

1. Import Jane's six original forms as regressions, preserving expected output.
2. Correct typed and inferred struct bindings for both `let` and `var`. Include
   indexed/dereferenced memory, local-to-local copies, and aggregate fields.
   Evaluate initializers once and preserve side-effect order.
3. Check whole-value reassignment, by-value arguments/returns, generic structs,
   nested aggregates, and pointer-field behavior. Audit adjacent enum and other
   aggregate binding paths; do not assume they are covered by a struct fix.
4. Verify raw/O1/O3 output and inspect raw IR. Check supported evaluator/device
   paths against the same contract or their documented refusal boundaries.
5. Run compiler, struct/closure, and affected CUDA lifetime gates. Refresh the
   bootstrap snapshot and verify its fixed point. Rerun Jane's decode/statistic
   gate before closing W15.

Do not rely on a struct-valued binding as a snapshot until this gate passes.
