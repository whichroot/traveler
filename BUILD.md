# Building Traveler from scratch

This guide takes you from a clean checkout to a working, self-hosted
Traveler compiler that can compile `.tv` programs to native binaries.

Traveler is **fully self-hosted**: the compiler is written in Traveler
(`src/tvc_self.tv`), and the first binary is built from a checked-in
snapshot of *itself*, **no C compiler is in the
trust chain.** The canonical pipeline:

```
src/bootstrap/tvc_self.boot.ll  (Traveler-produced IR, committed)
   --llc-->  .o  --link-->  stage0   (the booted compiler)
stage0  --compiles-->  src/tvc_self.tv  -->  stage1   (canonical compiler)
*.tv --compiles--> raw LLVM IR --optional closed opt profile--> LLVM IR
    --llc -O2--> .o --link--> native binary
```

One command:

```sh
src/bootstrap/build.sh        # boots from the committed IR, asserts the fixed point
# -> src/bootstrap/out/stage1 is the canonical compiler. No C source compiled.
```

See `src/bootstrap/PROVENANCE.md` for why this is honest (the snapshot is a fixed
point; regenerate it with `src/bootstrap/refresh.sh`).

`src-legacy/tvc.c` is the original C bootstrap **seed**, now **optional** — kept only
as an independent provenance/audit path. Frozen-seed compatibility is a separate
effort; the canonical bootstrap gate uses no C source. The legacy seed pipeline
below is retained for that compatibility effort.

## 1. Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| C compiler | clang or gcc | builds the `tvc` seed from `tvc.c` |
| LLVM | **15+** (required) | `llc` (IR → object); `opt` is optional for raw IR |
| make | any | drives `src-legacy/Makefile` |
| bash + coreutils | any | running the test suites |

LLVM 15+ is required to run the build. The `promote`, `o1`, and `o3` CPU middle-end
profiles described below initially require LLVM 21; profile `none` retains the
LLVM 15+ contract.

### macOS (Homebrew)

```sh
brew install llvm@21
# Apple clang (from Xcode Command Line Tools) is fine for compiling tvc.c.
```

LLVM tools land in `/opt/homebrew/opt/llvm@21/bin` (Apple Silicon) or
`/usr/local/opt/llvm@21/bin` (Intel). They are keg-only, so reference them
by full path or add the directory to `PATH`:

```sh
export PATH="/opt/homebrew/opt/llvm@21/bin:$PATH"
```

### Linux (Debian/Ubuntu)

```sh
sudo apt-get install clang-21 llvm-21   # provides llc-21, opt-21
```

Tools are typically under `/usr/lib/llvm-21/bin`.

### Nix / NixOS

```sh
nix develop   # LLVM 21 (llc/opt), cc, make, python3 on PATH
```

The committed `flake.nix` dev shell provides the full toolchain the test
gates consume (`tests/lib/env.sh` discovers everything from `PATH`),
including AMDGCN and NVPTX backends for `tests/gpu/run.sh`.


## 2. Build the compiler (canonical, C-free)

Boot the compiler from the committed Traveler-produced snapshot. **No C source
is compiled** — `cc`/`clang` is invoked only as the system linker.

```sh
export LLC=/opt/homebrew/opt/llvm@21/bin/llc   # your llc-21 path
src/bootstrap/build.sh
# -> src/bootstrap/out/stage1 is the canonical compiler.
```

`build.sh` boots `src/bootstrap/tvc_self.boot.ll`, compiles the current source,
and asserts the self-hosting fixed point (stage1 == stage2). Add `--check` to
also assert the committed snapshot is fresh, or `--target TRIPLE` to
cross-build.

Use the resulting compiler for everything:

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/fb.ll
$LLC -filetype=obj /tmp/fb.ll -o /tmp/fb.o && cc /tmp/fb.o -o /tmp/fb
/tmp/fb        # expected: 49 100 171 2 123 1
```

After an intentional change to `src/tvc_self.tv` that alters emitted IR,
refresh the snapshot (Traveler-produced, still no C) and commit it:

```sh
src/bootstrap/refresh.sh
git add src/bootstrap/tvc_self.boot.ll
```

See `src/bootstrap/PROVENANCE.md` for the full trust model.

## 3. (Optional) The legacy C-seed path

`src-legacy/tvc.c` is the original bootstrap seed — no longer required, kept as an
independent provenance/audit path. It produces a byte-identical compiler to the
C-free path (asserted by `tests/run_bootstrap.sh`).

```sh
cd src-legacy
make tvc                       # build the seed
make test                      # smoke test: 49 100 171 2 123 1
LLC=/opt/homebrew/opt/llvm@21/bin/llc

# Stage 1: seed compiles tvc_self.tv
./tvc ../src/tvc_self.tv -o /tmp/tvc_self.ll
$LLC -filetype=obj /tmp/tvc_self.ll -o /tmp/tvc_self.o
clang /tmp/tvc_self.o -o /tmp/tvc_self
```

`/tmp/tvc_self` is now the compiler to use for all `.tv` programs.

## 4. Compile a program

Use the canonical compiler from §2 (run these from the repo root):

```sh
TVC=src/bootstrap/out/stage1
$TVC examples/field_basics.tv -o /tmp/fb.ll
$LLC -filetype=obj /tmp/fb.ll -o /tmp/fb.o
cc /tmp/fb.o -o /tmp/fb
/tmp/fb        # prints: 49 100 171 2 123 1
```

### One-shot (`--emit exe` / `--emit obj`)

The compiler can drive `llc` (+ `cc`) itself, collapsing the three steps into one:

```sh
TVC=src/bootstrap/out/stage1
$TVC examples/field_basics.tv -o /tmp/fb --emit exe -llc "$LLC"
/tmp/fb        # prints: 49 100 171 2 123 1
```

`--emit obj` stops at the object file; `--emit ir` (the default) writes LLVM IR.
The driver passes `-O2` explicitly to `llc`; it reads the target triple from the
module preamble. Toolchain paths default to `PATH`; pass `-llc <path>` / `-cc
<path>` when tools are not on it (for example, Homebrew LLVM). Tools are launched
with argument vectors, not through a shell, so paths containing spaces are safe.
IR, object, and executable outputs use exclusive sibling stages and are
atomically published only after every requested tool succeeds. Failures preserve
an existing destination and remove intermediates.

### CPU middle-end profiles

Traveler exposes four closed profiles through the same IR/object/executable
flow:

| Profile | LLVM middle-end pipeline | Requires `opt` |
|---|---|---|
| `none` | none; raw compiler IR | no |
| `promote` | `-passes=mem2reg -verify-each` | LLVM 21 |
| `o1` | `-passes=default<O1> -verify-each` | LLVM 21 |
| `o3` | `-passes=default<O3> -verify-each`, explicit CPU | LLVM 21 |

The default is `none`. Omitting `--opt-level` and selecting `none` produce
byte-identical raw IR. `promote`, `o1`, and `o3` are explicit, reproducible LLVM-21
toolchain transformations; their output is verified LLVM but is not a bootstrap
fixed-point artifact. Native host retargeting is applied consistently to both
`opt` and `llc`.

`o3` requires an x86-64 target and `-mcpu x86-64` or
`-mcpu sapphirerapids`. The selected CPU goes to both `opt` and `llc` and is
retained in optimized IR's function attributes. `native` and other CPU names
are refused; `-mcpu` is accepted only with `o3`. This profile enables LLVM's
vectorizing O3 pipeline without fast-math options or changes to Traveler's
integer arithmetic contract. A Sapphire Rapids artifact requires a compatible
execution CPU. The backend remains at its explicit `-O2` setting.

```sh
$TVC program.tv -o program.o --emit obj -target x86_64-linux-gnu \
    --opt-level o3 -mcpu sapphirerapids -opt "$OPT" -llc "$LLC"
```

```sh
OPT=/opt/homebrew/opt/llvm@21/bin/opt

# Observe optimized LLVM.
$TVC examples/field_basics.tv -o /tmp/fb.o1.ll \
    --opt-level o1 -opt "$OPT"

# Or publish a native executable in one call.
$TVC examples/field_basics.tv -o /tmp/fb --emit exe \
    --opt-level o1 -opt "$OPT" -llc "$LLC"
```

`-opt <path>` overrides `PATH`, matching `-llc` and `-cc`. Arbitrary LLVM pass
strings are deliberately not accepted. Standalone AMDGCN, NVPTX, and AGX device
emission and the `-target tpc` typed-pointer compatibility mode accept only
profile `none`; `--agx-dispatch` emits a normal host module and may use a
CPU profile supported by that host target for its fallback path.

### Shared library (`#[export]` functions, callable from Python/ctypes)

```sh
$TVC src/lib/core/poly_core.tv -o /tmp/pc.ll
$LLC -O2 -filetype=obj /tmp/pc.ll -o /tmp/pc.o
cc -shared -O2 -o /tmp/libpoly_core.dylib /tmp/pc.o   # .so on Linux
```

### Multi-file programs

Some demos link against a library kernel (e.g. `examples/poly_core_test.tv` +
`src/lib/core/poly_core.tv`). Compile each `.tv` to a `.o`, then link them
together (using the canonical compiler from §2):

```sh
TVC=src/bootstrap/out/stage1
$TVC src/lib/core/poly_core.tv      -o /tmp/core.ll
$TVC examples/poly_core_test.tv     -o /tmp/test.ll
$LLC -filetype=obj /tmp/core.ll -o /tmp/core.o
$LLC -filetype=obj /tmp/test.ll -o /tmp/test.o
cc /tmp/core.o /tmp/test.o -o /tmp/poly_test
/tmp/poly_test
```

### Cross-compilation

The compiler accepts `-target <triple>`. The default is the **detected host
triple** (`uname`), so a plain `stage1 prog.tv -o prog.ll` produces IR the local
`llc` accepts with no `-mtriple`. To cross-compile, pass `-target` to the
compiler **and** the matching `-mtriple` to `llc`:

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/pc.ll -target x86_64-linux-gnu
$LLC -mtriple=x86_64-linux-gnu -filetype=obj /tmp/pc.ll -o /tmp/pc.o
```

The C-free build itself cross-targets too: `src/bootstrap/build.sh --target
x86_64-linux-gnu`.

The compiler's **own** self-compile is pinned to the canonical
`arm64-apple-darwin` triple by `build.sh`/`refresh.sh` (an explicit `-target`),
so the committed snapshot and the fixed-point freshness diff stay
host-independent. On a Linux **host**, `build.sh` and the test gates link with
`-no-pie`; direct `stage1` invocations emit host IR, so add `cc -no-pie` when
linking on Linux.

### GPU device kernels

The compiler can re-emit proven elementwise pfor workers as standalone device
artifacts:

```sh
TVC=src/bootstrap/out/stage1
$TVC --emit-gpu examples/gpu_field_map.tv -o /tmp/map-amd.ll
$TVC --emit-gpu-nvptx examples/gpu_field_map.tv -o /tmp/map-nv.ll
$TVC --emit-gpu-agx examples/gpu_field_map.tv -o /tmp/map-agx.hex
$TVC --emit-gpu-vulkan examples/gpu_field_map.tv -o /tmp/map-vulkan.comp
```

Vulkan Stage 0 reuses the proved worker records but emits canonical GLSL for
exactly one worker per artifact. Its closed profiles are the unary
`Field<2147483647>` map and a signed, overflow-free private-K=8 integer dot.
`glslangValidator -V` is the standard SPIR-V encoder; it does not own source
semantics or admission. `src/lib/gpu/vulkan_runtime.tv` owns instance/device
selection, coherent device-local input buffers, host-cached coherent output
publication, descriptors, pipeline construction, submission, synchronization,
and teardown through the public Vulkan C ABI. `src/lib/gpu/hip_runtime.tv`
owns the corresponding HIP module path. `src/lib/gpu/cuda_runtime.tv` owns
the NVIDIA CUDA driver path: it loads PTX text through `cuModuleLoad`, which
JIT-compiles for the local GPU at module load, so no CUDA toolkit (`ptxas`/
`nvcc`) is in the chain. Their executables link only Traveler
objects plus `libvulkan`/`libamdhip64`/`libcuda`, with no project C, C++, HIP,
CUDA, or shader-runtime shim.

AMDGCN and NVPTX are LLVM device modules; `tests/gpu/run.sh` lowers them with
`llc` to a gfx1100 object and sm_90 PTX. The `udot4` builtin maps to
`v_dot4_u32_u8` on AMDGCN and to `dp4a.u32.u32` through inline asm on NVPTX
(LLVM 21 has no NVPTX dp4a intrinsic); other paths expand it bytewise. With `libcuda` and an NVIDIA device
present, the suite also lowers the NVPTX module for the local architecture
(`sm_120` on Blackwell; PTX JITs forward from `sm_90` otherwise) and executes
the same-source field-map gate and the exact Q8xQ4 projection through the
CUDA runtime, comparing bytes against the CPU pfor oracle. AGX is different: Traveler directly
emits measured G16X instruction bytes in canonical hex, with no Metal compiler
or LLVM device backend. The unary path admits one-input/one-output field maps
over `Field<2147483647>`, odd primes in `2^30 < p < 2^31`, or the canonical
64-bit prime `Field<18446744073709551557>` (`2^64-59`) through two u32 limbs;
other workers emit a skip record. This is not generic 64-bit-prime support. The
profile targets an M4 Pro G16X private interface and is not an Apple-supported
ABI. Narrow workers may also have two read-only inputs plus one output; the
runtime packs both logical inputs into one physical input binding, preserving
the measured two-binding graph. Portable byte goldens run everywhere;
owned-device execution runs only when the external harness is available.

AMDGCN/NVPTX device emission also admits the proof system's closed private K=8
dot shape: one mutable scalar accumulator, one literal `0..8` inner loop, and
one own-cell output. The device lowerer fully unrolls that loop into SSA, so the
module remains alloca-free and registers-only. General private mutables,
dynamic inner loops, and multi-statement reductions remain outside Stage 0.

The elementwise and blocked-dot device classes share one admission contract:

| Rule | Consequence |
| --- | --- |
| Leading bindings must be `var` | an immutable `let` stops the body scan (silent refusal) |
| Index expressions are affine in the raw loop variables | computed index `let`s refuse; `div`/`mod` on the pfor variable are fine inline |
| Dot-class assigns take accumulator arithmetic only | no calls, no loads; elementwise bodies take loads and arithmetic |
| Device calls must have a supported expansion | flat integer elementwise workers admit the scalar-helper profile below; dot classes retain their closed call rules |
| Rolled loop bound is a literal ≤ 128 | nested serial loops refuse; chunk partials and combine on the CPU when the accumulator is order-free |
| i128/i256/i512 element arrays and accumulators work | wide memory operations lower into device limbs; wide scalar captures still refuse (the worker context slot is 8 bytes) |

#### Scalar library calls and 512-bit accumulation

NVPTX and AMDGCN can expand direct, nonrecursive integer helpers in flat
elementwise workers before device emission. Imported helpers and nested calls
work, including unbounded type generics inferred from scalar arguments. Helpers
can bind and shadow scalar locals, assign concrete mutable scalar locals, and end
with a return or a terminal `if`/`else` whose arms each return. Nested terminal
branches are supported. Branch evaluation is lazy; only the selected arm runs.
Bodies can use integer literals, casts, arithmetic, bitwise operations, shifts,
comparisons, and supported nested calls. Arguments are evaluated once, in source
order, including unused arguments. Caller and callee type bindings are separate.

The initial profile covers signed/unsigned 8-, 16-, 32-, 64-, 128-, 256-, and
512-bit integers plus `i1` and `bool`. It excludes const/bounded generics,
nonterminal returns, loops inside helpers, short-circuit operators, arbitrary pointer
parameters, hidden memory reads, external/indirect calls, and field arithmetic.
Helper bodies must pass the typed device verifier. Implicit pfor discovery also
requires the existing CPU loop proof. Division/remainder above
64 bits and literal text exceeding 64 bits also refuse in this profile; wide
values can come from memory, widening, or supported arithmetic. Expansion is
bounded to 16 active calls, 16 type parameters, depth 64, 4096 expression/statement visits, 4096
typed nodes, and 256 live bindings. Unsupported calls retain `uncarried-call`
decisions, with a located `device-call-refused` diagnostic explaining the class
of refusal. Call-free workers keep their existing lowering path.

Explicit kernels can call helpers with local aggregates:

- Fully initialized flat structs with 1–16 scalar fields. Field initializers
  execute once in source order, and mutable fields can be updated.
- Zero-initialized fixed arrays with 1–16 scalar elements, including generic
  element types. Reads and writes require in-bounds integer-literal indexes.
- Trait-qualified associated functions with scalar parameters and returns.

Aggregates are represented by immutable versions of their scalar components.
They require no device allocation, and branch-local changes do not affect the
other branch. A plan holds at most 4096 aggregate component references. Nested
or pointer-bearing aggregates, aggregate copies/returns, array arguments, dynamic
local indexes, and escaping references are outside this profile.
Helpers using local aggregates can require explicit entries when the CPU pfor
proof cannot establish their effects.

Flat local structs can also be passed by value to read-only helpers, including
generic helpers. Read-only receiver methods support both `value.method(args)`
and trait-qualified calls with `&value`. A local struct reference can be forwarded
to another verified read-only helper or supplied to multiple read-only arguments.
The compiler tracks the current scalar version of the struct; it does not create
a device address. Receiver mutation, mutation of by-value struct parameters,
stored reference aliases, address-to-integer casts, and returned references refuse.

Statically resolved `+`, `-`, `*`, and `==` overloads are supported when the left
operand is a local struct identifier and the concrete implementation returns a
supported scalar. Right-hand arguments use the declared parameter type, including
signed/unsigned widening and truncation. The same conversion applies to native
CPU overload calls. `tests/gpu/check_receiver_calls.py` covers these operations,
read-only aliasing, mutation between calls, and transactional refusal cases.

Local nonescaping closures support scalar parameters, returns, and captures up to
64 bits. Parameters must be explicitly typed. Captures snapshot their values at
creation, including values read from mutable locals. Later changes to those locals
do not change the snapshot. Expression bodies and verified block bodies, including
terminal conditional branches, expand through the shared device verifier.

```traveler
fn apply_twice<C>(callback: C, value: i64) -> i64 {
    return callback(value) + callback(value + 1);
}

fn snapshot_example(value: i64) -> i64 {
    var bias: i64 = value * 3;
    let transform = |x: i64| -> i64 x + bias;
    bias = bias + 99;
    return apply_twice(transform, value);
}
```

Named closures and inline literals can be passed directly to generic callback
parameters. Zero-argument closures use `| |`. Arguments are evaluated once in the
caller environment and converted to the declared parameter types. Closure bodies
use their own capture/parameter bindings. Call resolution follows the CPU compiler:
builtins and declared functions retain precedence over same-named closure locals.

The initial closure profile permits 16 closure values per expanded kernel, with
at most 16 parameters and 16 captures each. It shares the 16-active-call and
depth/work limits with ordinary device helpers. Literals in generic function
bodies, nested closure construction, capture mutation, global/pointer/aggregate/
closure captures, local closure aliases, escaping closures, and function-pointer
coercion are refused. No closure environment, indirect call, or lifted callee is
published in the device artifact. The native closure-call boundary also applies
the declared scalar argument conversions.

`src/lib/core/wide_accum.tv` provides `wide_accumulate_i512` and
`wide_accumulate_u512`. Both widen their 256-bit multiplicands before multiplying
and adding to an explicit 512-bit accumulator. Accumulation wraps at 512 bits;
an exact algorithm must establish that its sum fits the signed/unsigned result.
These helpers expand into the device kernel rather than requiring a device
library symbol. LLVM lowers the wide arithmetic into narrower instructions.

Native compilation supports `i512`/`u512` storage, addition, subtraction,
multiplication, bitwise operations, shifts, comparisons, conversions, and decimal
printing. The evaluator still has 256-bit value boxes and explicitly
refuses 512-bit values with `512-bit-eval`. The device-call gate uses Python
big-integer oracles instead: it compares native CPU results and retargeted device
arithmetic, verifies PTX has no external arithmetic calls, and checks AMD objects
for undefined symbols. Retargeted execution does not replace GPU hardware tests.

#### Explicit kernel entries (provisional K2 syntax)

`#[kernel]` declares an explicit entry on NVPTX or AMDGCN. This syntax is
provisional. The initial profile requires a concrete void function whose first
parameter is an `i32` logical index. Its remaining parameters are typed device
buffers and scalar launch arguments. The body consists of direct-index stores;
their expressions can call the scalar library helpers described above.

```traveler
fn guarded_value<T>(value: T) -> T {
    if value == 0 {
        return 7;
    } else {
        return 30 / value;
    }
}

// Provisional entry syntax; index is supplied by the device launch.
#[kernel]
fn guarded_map(index: i32, input: *i64, output: *i64) {
    output[index] = guarded_value(input[index]);
}
```

The first parameter is replaced by the launch's logical index. It is not a packed
argument. The compiler emits the remaining parameters in declaration order,
followed by synthetic `lo`/`hi` bounds. Launch through the resident API by owner
name (`guarded_map`), with the same typed views, footprint checks, and fixed
`[256,1,1]` block geometry as `direct-index-v1`. Ordinary CPU calls execute the
function for the supplied single index; they do not launch a GPU grid.

NVPTX independent entries always include semantic descriptors. Their `execution`
field is `independent-kernel`, and their symbols start with `__traveler_kernel_`.
Existing pfor entries retain `independent-pfor` and their existing symbols.
Both use the shared typed entry representation and device verifier. An invalid
explicit entry prevents publication of the whole device module, including mixed
modules containing otherwise valid pfor workers. The cooperative context profile
below adds geometry, static shared memory, and block barriers.

`tests/gpu/check_generic_calls.py` checks CPU/Python parity, both LLVM device
backends, lazy branches, and explicit-entry refusals. To include native SM120
execution through the resident API:

```sh
python3 tests/gpu/check_generic_calls.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
python3 tests/gpu/check_receiver_calls.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

The K2 acceptance fixtures cover 17 kernels across these two modules, including
direct/factored generic equivalence, local aggregates, read-only receivers,
operators, closure snapshots, and generic callbacks.

#### Cooperative geometry (provisional K3 profile)

On NVPTX, a leading `GpuThread` parameter selects `cooperative-grid-v1`:

```traveler
import "src/lib/gpu/thread.tv";

#[kernel]
fn copy_grid(thread: GpuThread, input: *u64, output: *u64) {
    output[gpu_global_index(thread)] = input[gpu_global_index(thread)];
}
```

The compiler constructs the twelve context fields from thread/block identity and
block/grid dimension intrinsics. Each value widens to u64 before multiplication.
Context fields and imported helpers use the shared typed device plan; the context
is compiler metadata, with no packed struct or device allocation. Ordinary CPU
calls execute one explicitly supplied context.

Descriptors use `execution: "cooperative-kernel"`, `block: null`, three dimensions,
and 64-bit indexing. Captures retain declaration order, followed by synthetic u64
`lo`/`hi` bounds. `cuda_launch_grid_sync(kernel, geometry, arguments, count)` supplies
`lo=0` and `hi=grid_x*grid_y*grid_z*block_x*block_y*block_z`. Ordinary buffer views must
cover that full physical grid; bounded views use the explicit counts below.
The runtime checks the geometry, byte extents, and
disjointness before submission; footprint checks divide the available bytes by
element size before comparing the lane count.

The profile accepts direct-index stores, scalar locals, bounded flat aggregates,
and the shared-memory operations below. Ordinary global buffer accesses must
lower to the canonical x-fastest global coordinate tree used by
`gpu_global_index(thread)`. Factored calls are allowed; arbitrary offsets and
unproved index reassociations refuse. Index-call arguments contribute memory
effects even when a callee does not use their values. All physical threads execute
the body, with no K2-style per-thread early return. Other device backends
refuse this context profile before publishing output.

`tests/gpu/check_cooperative_geometry.py` checks two kernels across five launch
shapes against CPU, Python, and retargeted-device oracles, then exercises resident
launches and refusals with the driver double. Native acceptance uses:

```sh
python3 tests/gpu/check_cooperative_geometry.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

#### Static shared storage and predicated accesses

`src/lib/gpu/shared.tv` provides the initial u64 block-storage operations for
cooperative NVPTX entries. These names denote device operations in typed lowering.

| Operation | Contract |
| --- | --- |
| `gpu_shared_init_u64(count)` | First kernel statement; one literal-sized arena of 1–1024 slots. Zeroes every slot cooperatively and synchronizes the block. |
| `gpu_shared_load_u64(index)` | Returns the slot value, or zero for an out-of-range unsigned index. |
| `gpu_shared_store_u64(index, value)` | Index must lower to `gpu_local_index(thread)`; an out-of-range lane performs no store. |
| `gpu_block_barrier()` | Full-block shared-memory visibility and participation boundary. |
| `gpu_masked_load_u64(input, index, active)` | Loads only when active; otherwise returns zero without accessing memory. |
| `gpu_masked_store_u64(output, index, value, active)` | Stores only when active; otherwise leaves memory unchanged. |

Shared storage has block lifetime and is initialized on every invocation. It emits
static address-space-3 storage; its bytes count against the driver-reported static
shared-memory limit. Static-only entries require zero dynamic launch bytes. Kernel-local fixed
arrays use the bounded scalarized private-array rules described under K2, with
literal indexes and no shared aliasing.

The initial convergence rule admits shared operations and barriers only in the
straight-line entry body. Pure scalar helpers can still contain lazy branches.
Early returns, conditional barriers, and collectives inside helpers refuse. The
typed verifier requires a barrier between shared writes and reads, and between
reads and subsequent writes. Stores are lane-owned, so arbitrary cross-lane writes
refuse. A read/modify/write phase takes scalar snapshots, synchronizes, writes each
lane's slot, then synchronizes before the next read phase.

Masked global accesses retain the canonical global-index footprint. The active
argument has boolean semantics; arguments evaluate once before the guarded memory
operation. Threads with false masks still participate in block barriers. This
supports partial logical tiles with neutral inputs and preserved inactive outputs.
The current runtime requires buffer capacity for the full physical grid; masks
do not yet permit shorter views. Shared primitives are device-only and cannot be
modeled by sequential ordinary CPU calls.

`tests/gpu/check_shared.py` verifies peer exchange, inclusive block scan, and block
reduction against exact Python oracles. Retargeted kernels run with host threads
and phase barriers, including null global pointers in all-masked cases. Native
SM120 acceptance covers 1D/2D/3D shapes, odd block sizes, 1024-thread blocks,
partial/all-masked tiles, nonzero-offset views, and repeated launches:

```sh
python3 tests/gpu/check_shared.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

#### Dynamic shared storage and warp collectives

`gpu_shared_dynamic_init_u64(count)` selects one runtime-sized u64 arena instead
of a static arena. It must be the first entry statement, and `count` must name a
declared `u64` scalar parameter directly. The checked count is 1–1024 slots.
Initialization, bounds behavior, lane-owned stores, and block phase rules match
static storage. Static and dynamic arenas cannot be combined in one entry.

The descriptor binds `dynamic_shared_bytes` to that parameter with
`{"parameter": capture_slot, "byte_scale": 8, "max_count": 1024}`.
The launch must supply exactly `count * 8` dynamic shared bytes. The runtime checks
the parameter type, device shared-memory limits, and the function's maximum
dynamic shared-memory size before forwarding the byte count to the driver.

`src/lib/gpu/warp.tv` provides the following device-only operations:

| Operation | Result |
| --- | --- |
| `gpu_warp_shuffle_u32(mask, value, source_lane)` | Value from the selected lane in the current physical warp. |
| `gpu_warp_shuffle_xor_u32(mask, value, lane_delta)` | Value from the lane whose index is XORed with the delta. |
| `gpu_warp_ballot(mask, predicate)` | u32 bitset of lanes with a true predicate. |
| `gpu_warp_any(mask, predicate)` | Whether any lane has a true predicate. |
| `gpu_warp_all(mask, predicate)` | Whether every lane has a true predicate. |
| `gpu_warp_sync(mask)` | Warp participation and memory synchronization. |

The initial profile requires the literal full mask `4294967295`, literal shuffle
lane/delta values from 0 through 31, and operations in the straight-line entry
body. Boolean predicates use nonzero truth. Logical inactive lanes still execute
the collectives, using predicated global accesses where appropriate.

Entries using these operations select `cooperative-warp-v1`. Packaging and loading
require SM70 or newer; launches require a device warp size of 32 and a physical
block volume divisible by 32. Warp synchronization does not satisfy the typed
verifier's block-wide shared-memory phase boundary.

`tests/gpu/check_warp_dynamic.py` checks threaded exchange/vote/synchronization
oracles, arena bounds, schema and capability refusals, and driver attribute-query
failures. Native SM120 acceptance covers three kernels, four 1D/2D/3D launch
shapes including 1024-thread blocks, arena sizes 1/19/1024, partial/all-masked
tiles, offset views, and repeated launches:

```sh
python3 tests/gpu/check_warp_dynamic.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

#### Explicit bounded atomic buffers

`src/lib/gpu/atomic.tv` provides unsigned atomic add, exchange, and compare-exchange:

```traveler
import "src/lib/gpu/thread.tv";
import "src/lib/gpu/atomic.tv";

#[kernel]
fn count_threads(thread: GpuThread, counters: *u64, counter_count: u64) {
    gpu_atomic_add_u64(counters, counter_count, 0, 1, 0, 1);
}
```

Both `gpu_atomic_add_u32` and `gpu_atomic_add_u64` take
`(buffer, count, index, value, order, scope)` and return the old value. Addition
wraps at the element width. The unsigned index is guarded by `index < count`;
an out-of-range operation returns zero without accessing memory. Arguments
evaluate once before that guard. Zero counts are supported.

`gpu_atomic_exchange_u32/u64` use the same arguments and replace the value.
`gpu_atomic_compare_exchange_u32/u64` append an `expected` argument after `scope`;
they replace the value only when it equals `expected`, and return the observed
old value. Compare-exchange is strong, without spurious failure. A failed
comparison has acquire ordering for orders 1/3 and relaxed ordering for 0/2;
the PTX operation may provide stronger ordering. The bounds guard still applies.

The count must lower directly to a declared `u64` scalar capture. The compiler
binds that capture to the pointer's footprint as
`{"count_parameter": ordinal, "byte_scale": element_size}`. The runtime checks
the count's type and buffer capacity before launch, using division to avoid
byte-product overflow. A one-element counter needs only a one-element view,
regardless of physical grid volume. Disjointness checks use each buffer's actual
declared extent, permitting adjacent counter and output views in one allocation.

The initial matrix is:

| Element types | Operation | Literal order | Literal scope |
| --- | --- | --- | --- |
| Global `u32`, `u64` | add, exchange, compare-exchange | `0`: relaxed | `1`: device |
| Global `u32`, `u64` | add, exchange, compare-exchange | `1`: acquire | `1`: device |
| Global `u32`, `u64` | add, exchange, compare-exchange | `2`: release | `1`: device |
| Global `u32`, `u64` | add, exchange, compare-exchange | `3`: acquire-release | `1`: device |
| Shared `u64` | add, exchange, compare-exchange | `0`, `1`, `2`, `3` as above | `0`: block |

These operations require cooperative NVPTX entries and SM70 or newer. They emit
explicit PTX order/scope instructions with compiler memory clobbers. Calls must
be in the straight-line entry body; discarding the return value is supported.
Each atomic buffer has one count binding and cannot also have ordinary accesses
in the same entry. Other orders/scopes refuse, including system scope. Additional
widths and global block-scoped operations are not admitted by this profile.

`tests/gpu/check_atomic.py` pins all 24 global instruction combinations at SM70/SM90
and checks contended tickets, exchange chains, and successful/failed comparisons
against integer oracles. Native SM120
acceptance covers four 1D/2D/3D geometries, including two 1024-thread blocks,
counts 0/1/3/19, unsigned wraparound, guarded accesses, adjacent views, repeated
launches, and capacity/type/alias refusals:

```sh
python3 tests/gpu/check_atomic.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

The shared variants `gpu_shared_atomic_add_u64` and
`gpu_shared_atomic_exchange_u64` take `(index, value, order, scope)`.
`gpu_shared_atomic_compare_exchange_u64` appends `expected`. They operate on the
initialized static or dynamic u64 arena, with its existing bounds and lifetime.
Out-of-range operations return zero. Shared atomics may contend across block
threads, and their old values can be used within the atomic phase. A block barrier
is required between atomic and ordinary shared-access phases in either direction;
an atomic's memory order does not replace that participation boundary.
Shared-atomic entries select `cooperative-atomic-v1`, or `cooperative-warp-v1` when
they also use warp collectives. Both require SM70 or newer.

#### Transpose and boundary acceptance

`gpu_bounded_load_u64(buffer, count, index)` in `src/lib/gpu/shared.tv` provides a
guarded read for non-canonical indexes. It binds a direct u64 count capture to a
read-only bounded footprint, using the same capacity checks as atomic buffers.
It returns zero when `index >= count`. The initial bounded-read profile requires
SM70 and straight-line entry calls; its buffer cannot also have ordinary or atomic
accesses in that entry. Output stores retain their canonical full-grid footprint.

`tests/gpu/check_block_atomic_transpose.py` checks 24 shared atomic kernels
(three operations, four orders, static/dynamic arenas) and two parameterized
matrix transpose kernels. The direct and shared-tile variants use an imported
generic bounds helper and agree with an independent matrix oracle. Input views
hold only the logical matrix; output views cover the physical grid, and padding
is preserved. Acceptance covers empty matrices, single rows/columns, non-square
dimensions around 31/33, rectangular and 3D tiles, offset views, output canaries,
and repeated launches. Shared atomic checks include 1024-thread blocks and
explicit phase/scope/capability refusals.

The scan/reduction gate also checks eight native launch shapes, including blocks
of 1/31/32/33/1024 threads, with logical cutoffs immediately around block boundaries.

```sh
python3 tests/gpu/check_block_atomic_transpose.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
python3 tests/gpu/check_shared.py "$TVC" "$LLC" "$OPT" cc /path/to/libcuda.so 120
```

`--pfor-report` reports CPU worker admission: one JSONL record per loop with
`dispatched` and a refusal `reason`. An admitted worker may still execute
serially because of alias guards, loop size, or runtime thread settings.

Use `--pfor-alias-report` to inspect the emitted CPU alias decision:

```sh
$TVC program.tv --pfor-alias-report
```

Each record includes `fn`, `line`, `var`, `admitted`, `alias`, and `reason`.
`alias` is `not-admitted`, `serial` (forced fallback), `checked` (runtime range
checks), or `disjoint` (no overlap check needed). Reasons name untrusted
captures, callee/read/write footprints, or missing footprints. A `checked`
result permits parallel dispatch only when the runtime ranges pass. Pointer
overlap and address/index overflow select serial execution.

A read-only callee can still force serial fallback when its accessed range is
unknown: read-only status alone does not establish non-overlap with outputs.
`tests/pfor/pfor_guard_probe.tv` pins six examples, including affine and
data-dependent pointer callees, an indirect read, and an inline affine control.

The device module's `; skipped __pfor_gpu_worker_N` records report device-side
refusals, including body shape, i64 iterator, dynamic field carrier, and calls
the device module cannot carry. Device admission and CPU alias eligibility
are separate decisions.

Explicit Stage-0 requests (`--emit-gpu` for AMDGCN and `--emit-gpu-nvptx`)
exit **1** when no usable workers are emitted, including sources with no
worker candidates. This refusal publishes no device IR to stdout and creates no
output file. An existing `-o` file keeps its previous contents. Successful
file output is staged and renamed into place. Callers must check the exit
status before using an existing artifact.

Both modes write JSONL decisions to stderr with schema `traveler.device.v1`:

- `kind: "worker"`: `target`, numeric `worker` suffix, `fn`, body `line`,
  `status` (`emitted` or `refused`), and `reason`.
- Refusal reasons: `body-shape`, `i64-iterator`, `dynamic-field-carrier`, or
  `uncarried-call`. Emitted workers have an empty reason.
- `kind: "module"`: `target`, `candidates`, `emitted`, `refused`, and `reason`
  (`no-usable-workers` when empty; otherwise empty).

Worker records cover candidates registered by the device-mode host pass.
They report device code generation, not runtime execution or successful
file publication. Ordinary errors can also appear on stderr. A mixed module
succeeds if it emits at least one kernel; its refused workers remain named
in the decisions and module comments. The CPU admission and alias query
schemas retain their existing contracts.

**Opt-in semantic kernel interfaces**

```sh
$TVC --emit-gpu-nvptx --gpu-interface tests/gpu/gpu_scalar_calls.tv -o kernels.ll
```

Each emitted entry has one `; traveler.kernel.v1 ` comment followed by a JSON
object. The descriptor and LLVM IR share the existing atomic output publication.
Without `--gpu-interface`, device IR is byte-identical to ordinary emission.
The option accepts only standalone NVPTX emission.

Schema `traveler.kernel.v1`, ABI 1, records the actual entry symbol, owner,
generic substitutions, body line/column, target, and parameters in emitted
signature order. Captures precede synthetic signed-i32 `lo` and `hi`. Parameter
records include source and LLVM types, target ABI byte size/alignment, and scalar
signedness or pointer address space, element layout, and read/write role.

The initial `direct-index-v1` profile accepts flat independent workers whose
accesses are `buffer[i]`, with ordinary signed/unsigned 8/16/32/64-bit integer
captures, one-byte `bool` captures, and integer pointer elements through 512 bits.
Booleans use LLVM `i8`, unsigned one-byte storage, and scalar values 0 or 1.
Wide pointer elements use 16-byte alignment; their byte sizes remain 16/32/64.
Supported scalar helper expansion can appear in the values. Offset/scaled indexes,
reductions, wide by-value captures, and other unrepresented footprints fail
descriptor emission. If any emitted
worker fails this profile, the entire request fails and preserves an existing
output file. Ordinary device refusals retain the mixed-module behavior above.

The launch contract requires block `[256,1,1]`, one dimension, one lane per cell,
and zero dynamic shared memory. Bounds must satisfy
`0 <= lo <= hi <= 2147483392`; an empty domain submits no launch. This upper
bound leaves room for padded threads without overflowing the signed-i32 index.
Each accessed pointer requires bytes `[lo * element_size, hi * element_size)`
relative to its supplied view. `footprint.start_parameter` and `end_parameter`
refer to the bound ordinals; `byte_scale` is the element size. Consumers must use
checked arithmetic, validate view extents/alignment, and require non-overlap for
every ordinal pair in `disjoint`. This conservatively includes every pair of
accessed pointers where at least one is written. Read/write pointers are explicit;
argument position does not identify an output.

These are compiler semantic records (`stage: "semantic-ir"`, `artifact: null`).
They are not directly loadable CUDA packages. LLVM tools may discard comments;
`tools/cuda_package.py` retains the records before lowering and binds them to the
final PTX. The portable regression
`tests/gpu/check_kernel_interface.py` checks signature/layout agreement, pointer
effects, bounds metadata, refusal behavior, and atomic publication.

**Resident CUDA packages and launches (K1)**

The resident API is in `src/lib/gpu/cuda_resident.tv`. It targets 64-bit
little-endian Linux and the CUDA driver API. Build the two-kernel example with:

```sh
$TVC --emit-gpu-nvptx --gpu-interface examples/cuda_resident_kernels.tv -o kernels.ll
python3 tools/cuda_package.py kernels.ll --llc "$LLC" --sm 120 -o kernels.tvcp
$TVC examples/cuda_resident_pipeline.tv -o pipeline.ll
$LLC -filetype=obj pipeline.ll -o pipeline.o
cc -no-pie pipeline.o -lcuda -o pipeline
./pipeline kernels.tvcp
# prints 3087 after checking all 1024 results
```

Select the actual target SM explicitly. The builder invokes LLVM, checks emitted
entry signatures and the PTX target/version, and atomically replaces one package
file. No `nvcc` or `ptxas` is required. Format `TVCP0001` consists of eight magic
bytes, a four-byte little-endian JSON length, the JSON manifest, then exact PTX
bytes. Schema `traveler.cuda.package.v1` records ABI 1, `sm`, `ptx_major`,
`ptx_minor`, `ptx_bytes`, `ptx_sha256`, and the semantic `kernels` table.
The digest binds the PTX bytes; it is an integrity check, not a signature or a
compiler attestation.

The Traveler loader validates schema/ABI, duplicate keys and entries, parameter
layouts, effects, disjointness, target/version directives, and SHA-256 before any
CUDA module load. Limits are 256 KiB of ASCII JSON, 16 MiB of PTX, 64 entries,
and 32 captures per entry. Unknown versions, unsupported profiles, malformed
records, digest mismatches, and overflowing integers refuse. The driver supplies
the final PTX/JIT compatibility decision and an owned error log on JIT failure.

| Operation | Result |
| --- | --- |
| `cuda_device_open(ordinal)` | Retained primary-context owner; queries capability and launch limits |
| `cuda_device_limits(device)` | Copy of the queried block/grid axis, thread, shared-memory, and warp limits |
| `cuda_module_load(device, path)` | Validated, resident module |
| `cuda_kernel_get(module, symbol)` | Kernel selected by exact descriptor symbol |
| `cuda_kernel_get_owner(module, owner)` | Kernel selected by an unambiguous logical owner |
| `cuda_buffer_alloc(device, bytes)` | Persistent device allocation |
| `cuda_buffer_view(buffer, offset, bytes, type_code)` | Checked typed view, represented as `CudaArgument` |
| `cuda_argument_index(kernel, name)` | Capture ordinal from the descriptor, excluding synthetic bounds |
| `cuda_upload(view, host, bytes)` / `cuda_download(host, view, bytes)` | Explicit synchronous transfers |
| `cuda_launch_sync(kernel, lo, hi, arguments, count)` | Checked launch and completion, with no implicit copies |
| `cuda_launch_grid_sync(kernel, geometry, arguments, count)` | Checked full-grid cooperative launch with derived u64 bounds |
| `cuda_buffer_close` / `cuda_module_close` / `cuda_device_close` | Checked resource release |

Resource operations return `Result<u64, CudaError>`; successful release/transfer/
launch returns zero. Views return `Result<CudaArgument, CudaError>`; limit queries
return `Result<CudaDeviceLimits, CudaError>`. Resource IDs
are opaque generation-checked values, not pointers. Module close invalidates its
kernels; buffer close invalidates its views. Device close refuses while children
remain. At most 1024 live resource records exist; closed slots can be reused
without making old IDs valid. Distinct device owners cannot exchange buffers,
even if they retain the same physical device's primary context.

`src/lib/gpu/cuda_geometry.tv` provides the K3 geometry-validation foundation.
`CudaGeometry` holds u64 grid/block dimensions and dynamic shared bytes.
`cuda_geometry_status(limits, geometry, function_threads, static_shared)` returns
zero for admissible geometry, `-1` for invalid dimensions/ABI narrowing/index
overflow, or `-3` for exceeded hardware or function limits. It checks every axis,
the block volume, total physical lane count, and static plus dynamic shared bytes
before arithmetic can wrap. Total physical lanes must fit signed 64-bit indexing.
The shared-memory limit is the queried default, without opt-in enlargement.

Zero physical dimensions are invalid; empty logical launches skip submission
separately. Geometry validation is independent of descriptor footprint and
collective-participation validation. The `direct-index-v1` launch API
continues to use its descriptor's `[256,1,1]` block. It checks queried device and
function limits through this shared validator. `cooperative-grid-v1` uses the
caller-supplied geometry with the full-grid footprint contract above.

The provisional cooperative source interface uses a leading `GpuThread` context.
`src/lib/gpu/thread.tv` defines its u64 thread/block coordinates and block/grid
dimensions, plus `gpu_global_x/y/z`, `gpu_global_size_x/y/z`, `gpu_local_index`,
`gpu_block_index`, and `gpu_global_index`. Global linear indexing is x-fastest
over the full global coordinate lattice, rather than block-major lane ordering.
These helpers assume valid coordinates within an admitted geometry. The NVPTX
compiler constructs the context for cooperative entries.

View type codes are positive widths for signed integers, negative widths for
unsigned integers, and `1` for `bool`. For example, `32` is i32, `-64` is u64,
and `512` is i512. Use `cuda_arg_i8/u8/i16/u16/i32/u32/i64/u64` and
`cuda_arg_bool` for scalar arguments. Argument arrays contain captures only;
launch supplies `lo` and `hi`. Packing follows the descriptor, validates exact
types and extents, and rejects overlapping write/read or write/write ranges.
Upload data before a kernel reads it, including read/write buffers. Write-only
arguments do not clear untouched bytes.

Registry and driver operations are serialized by a process-local lock. Each
context-dependent operation establishes the owner's context and restores the
calling thread's previous context. Transfer/launch failures poison all resident
owners sharing that primary context. New submissions then refuse; close first
attempts to drain the context and retains resources if draining/release fails.
A context-restoration failure is reported, poisons the owners, and makes one
best-effort restoration retry. Error recovery must not assume that a persistently
failing driver restored the calling thread's context.

`CudaError` contains `boundary`, driver/validation `status`, `resource`, and an
optional owned JIT `log`; release the log once with `cuda_error_close`.
Boundaries 1–9 identify device-open, capability-query, buffer-allocation,
module-load, kernel/argument-lookup, view, transfer, launch, and close.
Negative statuses are invalid argument/handle (`-1`), registry capacity (`-2`),
unsupported capability (`-3`), poisoned owner (`-4`), and live children (`-5`).
The async extension also uses `-5` for resources retained by pending streams.
Module-load statuses 1/2 can also report package I/O/schema failures.

Portable gates are `check_cuda_package.py` and `check_cuda_resident.py` under
`tests/gpu/`; both run in the Linux GPU suite. The latter links a test-only C
driver double to check cleanup, failure handling, context restoration, and
absence of implicit transfers. Production compiler/runtime code remains Traveler.
Run the hardware lane explicitly with a driver library and the expected native
SM; it refuses to count a different actual capability as acceptance:

```sh
python3 tests/gpu/check_cuda_resident.py "$TVC" "$LLC" cc /path/to/libcuda.so 120
```

This checks resident intermediates, changed scalars, multiple modules/outputs,
partial domains, nonzero views, stale/cross-owner resources, one-byte booleans,
and signed/unsigned 512-bit arithmetic against Python. The read-only DAX staging
gate is `tests/gpu/cuda_resident_dax.tv`; pass a package built from
`tests/gpu/cuda_boolean_kernel.tv` and the DAX device path. It copies a bounded
DAX view into driver-allocated pinned host memory before explicit upload.

**Streams, events, and pinned staging (K4 in progress)**

Import `src/lib/gpu/cuda_async.tv` for explicit nonblocking streams and events
with timing disabled. Handles use the resident registry's generation and device
ownership checks. The initial API is:

| Operation | Contract |
| --- | --- |
| `cuda_stream_create(device)` / `cuda_event_create(device)` | Return owned handles. |
| `cuda_launch_async(kernel, stream, lo, hi, args, count)` | Enqueue an independent kernel with checked arguments. |
| `cuda_launch_grid_async(kernel, stream, geometry, args, count)` | Enqueue a cooperative kernel with checked geometry and arguments. |
| `cuda_event_record(event, stream)` | Record completion of the stream's current prefix. |
| `cuda_stream_wait_event(stream, event)` | Enqueue a dependency on an already recorded event. |
| `cuda_stream_query(stream)` / `cuda_event_query(event)` | Return `Ok(0)` while pending and `Ok(1)` when complete. |
| `cuda_stream_sync(stream)` / `cuda_event_sync(event)` | Wait for completion and return `Ok(1)`. |
| `cuda_stream_close(stream)` | Drain that stream before destroying it. |
| `cuda_event_close(event)` | Destroy an event when no pending stream retains it. |
| `cuda_pinned_alloc(device, bytes)` / `cuda_pinned_close(buffer)` | Allocate nonzero owned pinned storage; close refuses while retained. |
| `cuda_pinned_view(buffer, offset, bytes)` | Return a checked byte view without exposing a raw pinned pointer. |
| `cuda_pinned_write(view, source, bytes)` / `cuda_pinned_read(destination, view, bytes)` | Copy between caller memory and idle pinned storage. |
| `cuda_upload_async(stream, device_view, pinned_view, bytes)` | Enqueue a checked pinned-to-device copy. |
| `cuda_download_async(stream, pinned_view, device_view, bytes)` | Enqueue a checked device-to-pinned copy. |

Enqueue performs no implicit copies and no context-wide synchronization. Argument
arrays can be reused once enqueue returns. Buffer contents must remain valid until
their GPU uses finish. Callers express cross-stream data dependencies with events;
the runtime does not infer dependencies from shared buffer handles.

Each stream retains its referenced buffers, pinned storage, modules, and events.
A successful whole-stream query, synchronization, or close releases its holds.
Successful event completion releases source-stream holds only when their last
use is within the recorded prefix. Later uses of the same resource remain held;
other streams retain independent holds. Source handles include their generation,
so completion of an old event cannot release resources from a reused stream slot.
Closing retained resources, accessing their pinned storage from the CPU, copying
their buffers synchronously, and using their buffers in a synchronous launch refuse.
An event cannot be re-recorded while any stream retains its current recording.
Queries/waits on unrecorded events refuse. All event/stream pairings require the
same resident device owner.

Pinned copies check both view bounds and require the stream, device buffer, and
pinned allocation to share one resident device owner. Zero-byte copies are no-ops
after handle and view validation; zero-byte host access permits a null caller
pointer. A staging slot can be rewritten after its upload event completes while
later kernel/download work remains queued, provided no later use or other stream
still retains that allocation. Retention is allocation-wide, not per byte range.

Synchronous resident transfers drain the copy's default stream rather than the
entire context. The nonblocking streams above are explicitly ordered through
events. A fork/join pipeline can synchronize only the final consumer before
downloading its result; ancestor streams can subsequently be queried or closed
to release their retained resources.

Driver enqueue, event-command, and completion errors poison the owning context's
resident handles. References remain held after an uncertain enqueue or failed
completion, so cleanup must successfully drain the stream before storage is
released. Context-restoration errors retain the existing recovery contract.
New error boundaries are 10 (stream/event creation), 11 (record/wait), 12
(completion), 13 (pinned allocation), 14 (host access), and 15 (async copy).
View validation, kernel enqueue, and close retain boundaries 6, 8, and 9.

`tests/gpu/check_cuda_async.py` verifies eight reusable fork/join rounds, both
kernel profiles, prefix completion, ownership/stale-handle checks, pending-resource
refusals, and driver failures. Its driver double defers work until completion and
asserts zero `cuCtxSynchronize` calls on the normal pipeline path. Native SM120
acceptance verifies the same results and lifetimes:

```sh
python3 tests/gpu/check_cuda_async.py "$TVC" "$LLC" cc /path/to/libcuda.so 120
python3 tests/gpu/check_cuda_staging.py "$TVC" "$LLC" cc /path/to/libcuda.so 120
```

The staging gate verifies 16 rounds across two slots, independent/cooperative
kernels, output canaries, event-controlled host reuse, later-use retention,
multiple-stream holds, and stale source generations. The deferred driver double
checks allocation/free, copy enqueue, completion, cleanup, and context-restoration
failures, plus zero normal-path context-wide waits. Both gates pass on native
SM120. Physical overlap is not measured by these correctness gates.

**Read-only DAX staging and graph replay**

For stable host-side data, `dax_copy_bulk` provides checked non-atomic copies
with an aligned u64 loop and byte heads/tails. `dax_copy` retains per-byte
atomic observations. See [DAX bulk copies](spec/dax-copy.md) for the range,
non-overlap, and concurrency contracts.

`cuda_upload_dax_async` borrows a `CudaDaxStaging` record containing two pinned
buffers, two completion events, and a chunk size. It overlaps CPU staging with
queued uploads, waits only before slot reuse, and returns the final completion
event (zero for an empty transfer). See
[Double-buffered DAX uploads](spec/cuda-dax-staging.md) for setup, resource
lifetimes, preflight checks, and error recovery.

`src/lib/gpu/cuda_dax.tv` provides
`cuda_pinned_stage_dax(destination, source_view, offset, bytes)`. It checks the
source window and copies synchronously into idle owned pinned storage. The caller
keeps the DAX mapping alive during this copy; subsequent GPU work refers only to
the pinned allocation and resident device buffers. The pipeline gate opens and
maps device DAX read-only, reuses two staging slots, and checks downloaded results.
A portable file-backed read-only mapping exercises the same mapping/view path.

`src/lib/gpu/cuda_graph.tv` composes up to 64 ordered copy/kernel commands per
graph, with at most 32 explicit arguments per kernel. Separate graph executions
compose through the existing stream/event API, including fork/join dependencies.

| Operation | Contract |
| --- | --- |
| `cuda_graph_create(device)` | Create a generation-checked, owner-bound graph builder. |
| `cuda_graph_copy(graph, destination, source, bytes, download)` | Append an upload (`download=0`) or download (`download=1`). |
| `cuda_graph_kernel(graph, kernel, lo, hi, geometry, cooperative, args, count)` | Append an independent (`cooperative=0`) or cooperative (`cooperative=1`) launch. |
| `cuda_graph_instantiate(graph)` | Seal the builder, validate commands through private stream capture, and instantiate a CUDA Graph executable. |
| `cuda_graph_launch(graph, stream)` | Enqueue replay and retain the graph and referenced resources through completion. |
| `cuda_graph_close(graph)` | Destroy native graph state and release ownership holds; pending replay refuses close. |

Construction snapshots argument arrays, views, and geometry. It retains referenced
modules, device buffers, and pinned allocations against close. These ownership
holds permit CPU updates to idle pinned allocations between replays; active replay
adds ordinary stream-use holds that block CPU access. Graph instantiation performs
the normal launch/copy shape, bounds, alias, and capability checks without executing
the captured commands. Once instantiation starts, the builder is sealed even if it
fails; close releases partial native state, with retry on cleanup failure.

Each graph is an ordered chain. Branches and joins use events between graph launches
on different streams; internal arbitrary-DAG editing and executable parameter
updates are outside this profile. Completion retires replay resources at whole-graph
granularity. Cross-stream read/write ordering remains the caller's obligation.
Graph boundaries are 16 (construction), 17 (capture/instantiation), and 18 (replay);
close retains boundary 9. Failed replay retains resources until successful drain.

`tests/gpu/check_cuda_pipeline.py` checks command snapshots, mutable pinned contents
between replays, read-only staging, exact results, eight event-linked graph fork/join
rounds, and capture/instantiation/replay/destruction failures. Its normal-path mock
asserts zero context-wide waits and no resource leaks. Native runs additionally
measure serialized submission, two-slot pipelining, and two-slot graph replay:

```sh
python3 tests/gpu/check_cuda_pipeline.py "$TVC" "$LLC" cc /path/to/libcuda.so 120 /dev/dax0.0
```

For a root-only DAX device, append `--sudo-dax` to run only that native read-only
fixture with `sudo -n`. Omit the DAX path to measure ordinary-memory staging.
See [recorded SM120 measurements](tests/gpu/cuda-pipeline-measurements.md) for
workload, timing boundaries, variability, and before/after results.

**Expert placement benchmark**

`tests/gpu/check_cuda_experts.py` compares ten placement/scheduling variants over
deterministic hot-expert, uniform, and cache-thrashing traces. It uses exact u64
arithmetic, a bounded resident cache, separate pinned staging slots, event-ordered
eviction, lookahead, batching, and split/fused kernels. Portable checks validate
every output, compare cache counters against a Python model, and check actual mock
upload volume. The portable gate is included in `tests/gpu/run.sh`.

```sh
python3 tests/gpu/check_cuda_experts.py "$TVC" "$LLC" cc \
  --cuda /path/to/libcuda.so --sm 120 --trials 3 \
  --output /tmp/cuda-experts.jsonl
```

Native runs cover 64 KiB, 1 MiB, 8 MiB, and 32 MiB per expert. `--sizes` selects a
subset. The JSONL output contains source hashes, module-load times, raw timing and
cache counters, and host-observed completion times for all requests. Outputs stay
device-resident during timing; downloading and checking them occurs afterward.
Full-residency preload costs are recorded separately. See the
[placement measurements and controls](tests/gpu/cuda-expert-measurements.md).

**IEEE bit-pattern numerical boundary (K5 foundation)**

Explicit `ieee32_*` and `ieee64_*` operations provide binary32/binary64 arithmetic
over `u32`/`u64` carriers. The `ieee-bits-rne-v1` profile fixes nearest-even
rounding, gradual underflow, canonical NaN results, and explicit FMA. Separate
operations retain their rounding boundaries. Integer/field operators and CUDA
argument layouts keep their existing meanings. See the
[numerical contract](spec/ieee-bits.md) for signatures and supported boundaries.

Host lowering uses constrained LLVM intrinsics and may require system `libm`.
Manual links use `-lm`; `--emit exe` adds it for numerical modules. CUDA lowering
uses explicit PTX instructions and has no device-math library dependency.
Kernel descriptors carry the numerical profile; package readers reject unknown
policies. SM90 and SM120 PTX are covered by the portable gate:

```sh
python3 tests/gpu/check_ieee_bits.py "$TVC" "$LLC" cc --opt "$OPT"
# Native driver/JIT execution, including SM90-targeted PTX on newer hardware:
python3 tests/gpu/check_ieee_bits.py "$TVC" "$LLC" cc --opt "$OPT" \
  --cuda /run/opengl-driver/lib/libcuda.so --sm 120
```

For an SM90 device, select `--sm 90`. The native gate uses checked resident
allocations, typed views, launches, downloads, and cleanup. The
[validation record](tests/gpu/ieee-bits-validation.md) records the tested hardware
and exact-oracle results. Further numerical widths and architecture facilities
remain subsequent K5 work.

The integer-library utilities in `src/lib/float/numeric.tv` add numerical
integer conversion, saturating conversion back, classification, and ordered
comparison. They preserve the integer-carrier boundary. The resident binary64
projection fixture uses inner dimensions 1–8 with stride 8, ascending-column
accumulation, and an explicit fused/separate policy:

```sh
python3 tests/gpu/check_ieee_numeric.py "$TVC" "$LLC" cc --opt "$OPT"
python3 tests/gpu/check_numeric_projection.py "$TVC" "$LLC" cc --opt "$OPT"
```

Both gates accept `--cuda /path/to/libcuda.so` for checked native SM90/SM120 PTX
execution on an SM120 device. See the
[utility and projection validation record](tests/gpu/numeric-projection-validation.md).

`src/lib/float/low_precision.tv` adds binary16/bfloat16 conversion and packed
pairs with explicit binary32 FMA accumulation. The compiler derives composable
precision/warp/bounded-read requirements; package readers check their SM/PTX
floors and reject unknown capability names. Run the exhaustive encoding and
packed-projection gates with:

```sh
python3 tests/gpu/check_low_precision.py "$TVC" "$LLC" cc --opt "$OPT"
python3 tests/gpu/check_low_projection.py "$TVC" "$LLC" cc --opt "$OPT"
```

Both accept `--cuda /path/to/libcuda.so` for native SM120 acceptance using both
PTX targets. See [low-precision validation](tests/gpu/low-precision-validation.md)
and the [packing and accumulation contract](spec/ieee-bits.md).

Explicit [native tensor operations](spec/cuda-native-tensor.md) opt in to the
hardware numerical policy. [Asynchronous shared copies](spec/cuda-shared-async.md)
have separate device-local issue/commit/wait and visibility rules. Run their
portable gates and the prepared-package cache gate with:

```sh
python3 tests/gpu/check_native_tensor.py "$TVC" "$LLC" cc --opt "$OPT"
python3 tests/gpu/check_shared_async.py "$TVC" "$LLC" cc --opt "$OPT"
python3 tests/gpu/check_cuda_prepare.py "$TVC" "$LLC" cc
```

The tensor and shared-copy runners accept `--cuda /path/to/libcuda.so` for native
execution. The shared-copy runner also checks warm prepared-package execution
and native JIT diagnostics.

To prepare a source snapshot and reuse its verified PTX package, set
`TOOLCHAIN_ID` to the immutable identity of the complete toolchain environment:

```sh
python3 tools/cuda_prepare.py tests/gpu/cuda_shared_async.tv \
  --root "$PWD" --compiler "$TVC" --llc "$LLC" --sm 120 \
  --toolchain-id "$TOOLCHAIN_ID" --cache /tmp/traveler-cuda-cache \
  -o /tmp/shared-async.tvcp
```

The JSON report identifies cache status, source request, artifact, and preparation
intervals. See the [identity and reuse contract](spec/cuda-prepare.md) and
[validation record](tests/gpu/shared-async-prepare-validation.md).

Per-kernel opt-ins travel as owner-fn attributes. `#[wave_pipe]` pipelines the
wave loads through loop-carried phis; `#[prefetch]` instead warms L2 for the
next block's addresses (`prefetch.global.L2`, NVPTX only) with no carried
registers; `#[readonly]` marks device loads `!invariant.load`
(`ld.global.nc` on NVPTX) on the source's read-only promise; `#[fits_i32]`
narrows signed 64-bit mul operands; `#[unroll2]` doubles the rolled body;
`#[wave2]` pairs two lanes per thread. On the launch side,
`cuda_runtime_launch_caps` assumes one thread per cell; wave-mapped kernels
take `cuda_runtime_launch_caps_wave` with 32 lanes per cell (16 under
`#[wave2]`).

The measured G16X profile also has an in-tree Traveler submission runtime. It
needs a regenerated `AGXDISP3` profile image for the exact OS/GPU build; that
machine-specific image is deliberately not shipped as a portable ABI. On the
matching M4 profile:

```sh
AGX_PROFILE=/path/to/dispatch.img
$TVC tests/gpu/agx_runtime_gate.tv -o /tmp/agx-runtime.ll
$LLC -filetype=obj /tmp/agx-runtime.ll -o /tmp/agx-runtime.o
cc /tmp/agx-runtime.o -framework IOKit -o /tmp/agx-runtime
/tmp/agx-runtime "$AGX_PROFILE" /tmp/map-agx.hex
```

`cc` only links the Traveler-produced object. `otool -L` must show IOKit and
libSystem, with no Metal, Foundation, IOGPU, Objective-C, or project C object.
The runtime refuses any service build, initialization fingerprint, profile call
shape, or GPU-address allocation order outside the measured profile. Set
`AGX_FAULT_RECOVERY=1` on `tests/gpu/run.sh` to exercise destroy/recreate recovery
after the controlled out-of-range kernel. On the measured machine the same gate
also compiles one pfor source for CPU and AGX, runs 256 reproducible
adversarial/random elements over each supported field profile, and requires
equal exit status and byte-exact raw output.

For a source that imports `src/lib/gpu/agx_runtime.tv`, `--agx-dispatch` emits a
normal host program that tries the matching AGX worker by ID and otherwise runs
the unchanged CPU pfor. Runtime selection uses the exact-build profile and the
multi-worker artifact generated from the same source. The host embeds the
FNV-1a digest produced by the shared AGX lowering path; the runtime verifies
worker ID, field, grid, and code digest before submission. This is a deterministic
wrong-build guard, not cryptographic artifact authentication:

```sh
$TVC --emit-gpu-agx tests/gpu/agx_rns_dot_general.tv -o /tmp/rns-agx.hex
$TVC --agx-dispatch tests/gpu/agx_rns_dot_general.tv -o /tmp/rns-host.ll
$LLC -filetype=obj /tmp/rns-host.ll -o /tmp/rns-host.o
cc /tmp/rns-host.o -framework IOKit -o /tmp/rns-host
TRAVELER_AGX_PROFILE="$AGX_PROFILE" \
TRAVELER_AGX_ARTIFACT=/tmp/rns-agx.hex /tmp/rns-host
```

With either variable absent, an unsupported worker, alias uncertainty, an
artifact mismatch, or a launch refusal, execution falls back to the CPU worker.
The compiler and runtime share a 65,535-element maximum AGX grid. Checked alias
intervals reject i32 index wrap and any pointer provenance erased through an
integer, an uncertain control-flow assignment, or an exposed pointer-binding
address.
The gate supplies a different valid kernel with the same worker ID, field, and
grid and requires hash-mismatch fallback. The counted-dot gate derives rows and
columns from canonical source index equations, executes a `1x8 * 8x1024`
reduction for each of three primes on AGX, and uses the shipped Garner CRT on
CPU. `K=8` remains the measured device-loop contract. No performance claim is
made.

### Software graphics (`src/lib/gfx/`)

A CPU framebuffer plus two backends. An application imports one backend.
Both export `gfx_open` / `gfx_frame` / `gfx_present` / `gfx_poll_event` /
`gfx_close`.

```sh
TVC=src/bootstrap/out/stage1
# Headless: draw and write a P6 PPM. No compositor.
$TVC examples/gfx_headless.tv -o /tmp/gh.ll
$LLC -filetype=obj /tmp/gh.ll -o /tmp/gh.o
cc -no-pie /tmp/gh.o -o /tmp/gh && /tmp/gh

# Wayland window (Linux, raw protocol, no libwayland).
$TVC examples/gfx_window.tv -o /tmp/gw.ll
$LLC -filetype=obj /tmp/gw.ll -o /tmp/gw.o
cc -no-pie /tmp/gw.o -o /tmp/gw && /tmp/gw
```

`net/unix.tv` and `mem/shm.tv` are the OS floor. Do not import `net/tcp.tv`
and `net/unix.tv` in the same unit.

## 5. Verify self-hosting (Stage 2 / Stage 3)

The compiler can reproduce itself: the compiler compiling itself (Stage 2) must
produce IR byte-identical to that compiler compiling itself again (Stage 3) —
the fixed point. `src/bootstrap/build.sh` already asserts this on every build.
To check it by hand with the canonical compiler:

```sh
TVC=src/bootstrap/out/stage1

# Stage 2: the compiler compiles itself
$TVC src/tvc_self.tv -o /tmp/s2.ll
$LLC -filetype=obj /tmp/s2.ll -o /tmp/s2.o
cc /tmp/s2.o -o /tmp/tvc_self2

# Stage 3: the Stage 2 binary compiles itself again
/tmp/tvc_self2 src/tvc_self.tv -o /tmp/s3.ll

# Fixed point: the two IRs must be byte-identical
diff /tmp/s2.ll /tmp/s3.ll && echo "FIXED POINT OK"
```

## 6. Run the test suites

From the repo root, the dispatcher probes the environment and runs what it
supports:

```sh
tests/run_all.sh               # probe + run; prints a capability matrix first
tests/run_all.sh --list        # print the probe results and suite plan only
tests/run_all.sh --suite=gpu   # run one suite (see --help for names)
```

The dispatcher's behavior by environment:

- **llc + link driver(s) present:** the full gate on the primary driver
  (`tests/run.sh` through the canonical bootstrap),
  then the full regression suite once per *additional* discovered link driver
  (`cc`/`clang`/`gcc`), with tool-neutral sub-gates skipped on repeat passes.
- **No llc or no link driver:** `tests/run.sh` runs degraded — compilation
  and IR validation still happen, link/run stages report `SKIP (no llc)` /
  `SKIP (no linker)` per test — plus the canonical AGX byte goldens via
  `tests/gpu/run.sh --goldens-only`.
- **No toolchain at all:** the coreutils-only `tests/run_sizegate.sh`.

Individual suites remain directly runnable:

```sh
tests/run.sh           # canonical full gate, including lsp/doc/bootstrap
tests/run_dual.sh      # separate legacy-seed compatibility effort
tests/run_bootstrap.sh --legacy-seed  # explicitly request C-seed equivalence
tests/run_pfor.sh      # auto-parallelization soundness suite
tests/dynfield/run.sh  # dynamic-field + traits + closures suite
```

The test scripts auto-detect `llc`/`opt`/link drivers across common locations
(`tests/lib/env.sh` is the shared probe); override with the `LLC`, `OPT`, and
`LINKER` environment variables if detection fails:

```sh
LLC=/usr/lib/llvm-21/bin/llc tests/run.sh
LLC=/usr/lib/llvm-21/bin/llc OPT=/usr/lib/llvm-21/bin/opt tests/run.sh
LINKER=gcc tests/run.sh        # link with a specific driver
```

`tests/run.sh` never fails hard on a missing tool: an absent `llc`, link
driver, or python3 turns the affected tests into named SKIPs once stage1 is
available. Frozen-seed-only diagnostics (`instantiate_nongeneric` and
`missing_return`) report legacy-only skips. The canonical gate does not build
or invoke the frozen seed. `TVC_SELF` can select an existing canonical compiler.
The Wayland window test (`gfx_window`) runs only when a live compositor
socket exists, and pops a real window for ~5 seconds when it does.

### What each suite needs

Not every suite needs the full toolchain. Pick by environment:

- **stage1** — the self-hosted compiler at `src/bootstrap/out/stage1`
  (build it once with `src/bootstrap/build.sh`, which itself needs `llc`
  and a link driver).
- **llc** — LLVM 21 `llc` for object lowering.
- **link driver** — a `cc`/`clang` used only to link objects.
- **C compiler** — builds the frozen seed `src-legacy/tvc` (unneeded where a
  prebuilt `tvc` is already checked out/built).

| Suite | stage1 | llc | Link driver | C compiler | Extra |
|---|---|---|---|---|---|
| `tests/run.sh` (full gate) | ✓ | ✓ | ✓ | — | `opt` recommended (IR verify) |
| `tests/run_dual.sh` (legacy compatibility) | ✓ | ✓ | ✓ | ✓ | separate frozen-seed effort |
| `tests/run_pfor.sh` | ✓ | ✓ | ✓ | — | |
| `tests/dynfield/run.sh` | ✓ | ✓ | ✓ | — | |
| `tests/emit/run.sh` (`--emit` driver) | ✓ | ✓ | ✓ | — | |
| `tests/eval_diff/run.sh` (evaluator oracle) | ✓ | ✓ | ✓ | — | |
| `tests/alloc_debug/run.sh` | ✓ | ✓ | ✓ | — | |
| `tests/foldbug/run.sh` | ✓ | ✓ | ✓ | — | |
| `tests/run_diag.sh` / `run_fmt.sh` / `run_lsp.sh` / `run_doc.sh` | ✓ | ✓ | ✓ | — | also run as `run.sh` sub-gates |
| `tests/run_bootstrap.sh` (fixed point) | ✓ | ✓ | ✓ | — | rebuilds stage1/stage2 |
| `tests/typedptr/run.sh` (`-target tpc`) | ✓ | ✓ | ✓ | — | also needs an LLVM-14-era `llvm-as` + `llc` pair |
| `tests/gpu/run.sh` | ✓ | per leg | AGX/CUDA legs | — | AMDGCN/NVPTX legs skip if `llc` lacks the target; CUDA execution needs `libcuda` + device node; AGX hardware legs need the measured M4 profile (macOS) |
| `tests/fuzz_diff.py` | ✓ | — | — | ✓ | dual-compiler IR fuzzing |
| `tests/codegen_diff/run.sh` | ✓ | — | — | — | IR-hash manifest |
| `tests/repl/run.sh` | ✓ | — | — | — | evaluator only |
| `tests/pow_assoc/run.sh` / `tests/pfor_report/run.sh` | ✓ | — | — | — | compiler-query gates |
| `tests/run_sizegate.sh` | — | — | — | — | coreutils only |

### Per-environment guide

- **macOS (M4, measured AGX profile):** everything runs, including the
  owned-device AGX legs of `tests/gpu/run.sh`.
- **macOS (other) / Linux with LLVM 21 + cc:** everything except the
  owned-device AGX legs (the canonical byte goldens still run). Add the
  LLVM-14 pair for `tests/typedptr/run.sh`.
- **Linux + Wayland:** as above. The `gfx_headless` / `gfx_pixel_test` /
  `gfx_wire_test` regression entries need no compositor. The window demo
  `examples/gfx_window.tv` is run manually against a live compositor (it
  prints `404` and exits if `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY` is absent;
  with a compositor it prints `1` on open and runs until the window is
  closed):

  ```sh
  src/bootstrap/out/stage1 examples/gfx_window.tv -o /tmp/gw.ll -target x86_64-linux-gnu
  $LLC -mtriple=x86_64-linux-gnu -filetype=obj /tmp/gw.ll -o /tmp/gw.o
  cc -no-pie /tmp/gw.o -o /tmp/gw && /tmp/gw
  ```

- **No LLVM toolchain (llc/opt absent):** the stage1-only gates still run —
  `tests/codegen_diff/run.sh`, `tests/repl/run.sh`, `tests/pow_assoc/run.sh`,
  `tests/pfor_report/run.sh` — plus `tests/run_sizegate.sh` (coreutils only)
  and the canonical AGX byte goldens (emit with `--emit-gpu-agx` and diff
  against `tests/gpu/golden/`).
- **No C compiler:** the seed-dependent gates cannot build `src-legacy/tvc`
  (`run_dual.sh` parity, `tests/fuzz_diff.py`); everything stage1-driven is
  unaffected.

## Troubleshooting

- **`llc not found`** — `tests/run.sh` no longer fails hard; it degrades to
  IR-only checks and SKIPs the link/run stages. `tests/gpu/run.sh` still
  requires `llc` for its device legs — use `tests/gpu/run.sh --goldens-only`
  for the llc-free AGX byte goldens, or set `LLC` to the full path of your
  `llc` binary.
- **`make test` fails on the `llc` step** — the hardcoded path in
  `src-legacy/Makefile` doesn't match your install. Pass `LLC=<path>` to `make`.
- **Linker errors about undefined field/helper symbols** — you're building a
  multi-file program; compile and link every required `.tv` (see §4).
- **Invalid IR / verifier errors after `opt`** — confirm `opt`, `llc`, and
  the IR all come from the same LLVM 21 toolchain (don't mix versions).
- **`error: opt failed`** — a requested `promote`/`o1`/`o3` profile could not run
  LLVM 21 `opt`, or verification failed. Set `-opt <path>` to the matching tool.
  `opt` remains unnecessary when the profile is `none`.
