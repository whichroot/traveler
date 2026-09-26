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

Full-mask warp shuffles, votes, and warp synchronization can appear in branches
and rolled `while` loops when every enclosing condition is proven uniform
within each physical warp. Different warps may take different paths or execute
different numbers of iterations. The proof follows scalar operations, branch
merges, and loop-carried values to a fixed point, including the backedge: a
uniform initial counter alone is insufficient. Memory reads and collective
results are conservatively treated as potentially lane-dependent.

Kernel scalar parameters, constants, block coordinates, and grid/block sizes
are uniform. `gpu_local_index(t) >> 5` is the physical warp index within a block,
including multidimensional blocks. A block-major grid-stride row loop can use:

```tv
let warps = (t.block_dim_x * t.block_dim_y * t.block_dim_z) >> 5;
let stride = t.grid_dim_x * t.grid_dim_y * t.grid_dim_z * warps;
var row = gpu_block_index(t) * warps + (gpu_local_index(t) >> 5);
while row < rows {
    var token: u64 = 0;
    while token < tokens {
        // Full-mask shuffles can reduce lane-local values here.
        token = token + 1;
    }
    row = row + stride;
}
```

The existing cooperative-warp launch contract requires complete 32-lane warps.
Neither `t.thread_x >> 5` nor `gpu_global_index(t) >> 5` establishes warp
uniformity for general multidimensional launch geometry. Lane-dependent scalar
branches can reconverge before a collective; a collective inside such a branch
still refuses with `conditional-effect`.

Shared-memory operations, block barriers, native tensor operations, asynchronous
shared copies, and atomics must remain in the root control-flow region. Warp
uniformity does not establish block-wide participation or shared-memory phase
safety.

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

`check_warp_loops.py` executes nested row/token reductions with real host-thread
warp barriers and independent Python results. It covers zero iterations, unequal
trip counts between warps, reconverged lane-dependent branches, and multidimensional
launches. Negative cases check divergent initial conditions, loop updates, nested
control, memory-derived counters, and block barriers. Optimized device IR is
verified and lowered to SM90 PTX.

The scalar-control modules lower to SM90 and SM120 PTX. The scalar-call gate also
checks AMDGCN lowering and exact helper behavior. These portable checks establish
compiler semantics, not GPU throughput or hardware parity. The full expert
workload still needs the Jane GPU parity gate and its remaining memory/store
admission work.
