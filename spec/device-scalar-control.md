# Scalar device control flow

Typed integer helpers can use mutable scalar locals, `if`/`else` assignments,
early returns, lazy `&&`/`||`, and rolled `while` loops. This applies to helpers
expanded into NVPTX and AMDGCN workers and to the bodies of NVPTX cooperative
`#[kernel]` entries with a leading `GpuThread` parameter.

```tv
fn bit_length(value: u64) -> i64 {
    var v: u64 = value;
    var bits: i64 = 0;
    while v > 0 {
        bits = bits + 1;
        v = v >> 1;
    }
    return bits;
}

fn guarded_ratio(value: i64, divisor: i64) -> i64 {
    if divisor == 0 { return 0; }
    return value / divisor;
}
```

## Evaluation and representation

Branch assignments merge the current values of scalar locals. Loop-carried
locals use SSA header and backedge values; the compiler does not expand one
copy of the body per iteration or allocate addressable local storage. Nested
loops, multiple accumulators, zero iterations, and data-dependent conditions
retain their source behavior. There is no hidden iteration limit or implicit
parallel reduction. Programs remain responsible for loop termination.

Early-return helpers evaluate only the continuation of a falling-through path.
Logical operators evaluate their right operand only when required. Arguments
to ordinary calls still evaluate once in source order, including unused
arguments. Arithmetic failures on executed paths remain failures; an inactive
division does not execute. Existing defined integer shifts and wrapping integer
operations retain their contracts.

The supported scalar types are the existing integer device types, through
512 bits, plus `bool`/`i1`. Field arithmetic, recursive calls, external/indirect
calls, and division or remainder wider than 64 bits retain their restrictions.

## Memory and participation

Cooperative entry branches and loops can access their own canonical global
index. `gpu_bounded_load_u64` can also appear in these regions, with the same
direct buffer/count capture binding and runtime footprint checks as a
straight-line bounded read. Pure scalar helpers still cannot hide pointer reads.

Shared-memory operations, barriers, warp collectives, native tensor operations,
asynchronous shared copies, and atomics must remain in the root control-flow
region. A scalar branch or loop does not establish collective participation or
shared-memory phase safety. A uniform-looking condition is not yet an admitted
uniformity proof. Collectives after a scalar loop retain the normal root-region
rules; collectives inside it refuse.

This milestone does not admit `for`, `break`, `continue`, returns from inside
loops, or early returns from cooperative entries. Only scalar values can merge
across a statement branch or loop boundary; mutation of an enclosing aggregate
across that boundary refuses. Existing terminal helper branches may still use
local aggregates to compute a returned scalar.

Non-canonical stores still require a separate uniqueness proof. Range checks
alone do not make conflicting non-atomic writes valid.

## Diagnostics and limits

Refusals identify the offending source location. Device decision records use
`for-loop`, `loop-or-return-control`, `aggregate-merge`, `var-assignment`,
`non-canonical-store`, or `conditional-effect` for the corresponding boundaries.
The supplementary `device-call-refused` line uses the same detail. Existing
callee/type/expression refusals retain their established categories.

The existing 16-active-call, depth-64, 4096-node/work, and 256-binding limits
remain enforced. Early-return continuation expansion counts toward those
limits. Unsupported explicit entries prevent publication of a partial module.

## Verification

`tests/gpu/device_q32.tv` preserves the scalar helper definitions from the Jane
Q32 port report: round-to-nearest-even, multiplication, restoring division,
exponential approximation, sigmoid, and tanh. `check_scalar_control.py` compares
native host compilation and retargeted device IR against independent exact
Python oracles in raw and O1 profiles, including the SiTU composition. It also
checks nested loops, zero iterations, shadowing, guarded memory, short-circuit
failures, and rejection of unsupported control and synchronization effects.

The emitted modules lower to SM90 and SM120 PTX. The scalar-call gate also checks
AMDGCN lowering and exact helper behavior. These portable checks establish
compiler semantics, not GPU throughput or hardware parity. The full expert
workload still needs the Jane GPU parity gate and a scheduling plan for its
collectives inside token/row loops.
