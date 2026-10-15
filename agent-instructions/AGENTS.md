# Writing Traveler programs

This is the agent-agnostic guide to using Traveler. For work on this repository,
also read the root [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md).

Use the [language specification](../spec/language-spec.md) for implemented
behavior, [BUILD.md](../BUILD.md) for commands and backend setup, and
[examples](../examples/) for complete programs. Check the actual library
signature before generating a call. Imports are relative to the importing file.

## Build, compile, and inspect

Use LLVM 21 and a system linker driver. The raw IR path supports LLVM 15+;
optimization profiles require LLVM 21. `nix develop` provides the toolchain.
With `llc` and `cc` on `PATH`, run from the repository root:

```sh
src/bootstrap/build.sh
src/bootstrap/out/stage1 examples/field_basics.tv --emit exe -o /tmp/field-basics
/tmp/field-basics
```

Expected output is `49 100 171 2 123 1`, one number per line. The bootstrap
uses the checked-in LLVM snapshot and checks a self-hosting fixed point.
`src-legacy/tvc.c` is a frozen historical seed, not the compiler for new programs.

Set `LLC` for the bootstrap script; use `-llc <path>` and `-cc <path>` for the
compiler driver. Native compilation defaults to the host target. `--emit ir`
(the default), `--emit obj`, and `--emit exe` select output stages. The driver
handles target and Linux link flags. Cross-compilation additionally needs the
matching target toolchain and libraries; see the build guide.

| Mode | Use |
|---|---|
| `--diagnostics` | JSON-Lines diagnostics |
| `--dependencies` | Entry and transitive import paths as JSON |
| `--symbols`, `--references` | Compiler symbol and reference queries |
| `--pfor-report` | Loop admission and emitted dispatch scaffolding |
| `--pfor-alias-report` | Alias guard status and refusal reason |
| `--eval` | Execute the supported interpreter subset |
| `--alloc-debug` | Emit allocation redzones for native debugging |

`--eval` executes the program; it is not a read-only query. It can refuse
features that native compilation supports. Successful diagnostics or device
emission alone do not establish runtime correctness.

## Essential syntax

```traveler
type F = Field<251>;

fn square(x: F) -> F { return x * x; }

fn main() {
    let x: F = 200;
    var total: i32 = 0;
    for i in 0..10 { total += i; }
    print(square(x));
    print(total);
}
```

- End simple statements with `;`. Newlines do not insert semicolons.
- Use decimal or hexadecimal integers without digit separators. At the audited
  revision, strings support basic escapes but not `\xHH` or a `b"..."` prefix.
- `let` creates an immutable binding; `var` permits reassignment. `let mut`
  is rejected. A `let` pointer can still write through its pointee.
- Use explicit `return`. Ordinary function bodies do not have Rust-style
  implicit tail returns.
- `if`/`else` and `match` are statements. `for` uses an exclusive integer
  range; `while`, `break`, and `continue` are supported.
- `match` accepts enum variants, integer cases, and `_`. Exhaustiveness is
  not checked; an unmatched value falls through.
- `&&` and `||` short-circuit. `**` is exponentiation; `^` is bitwise XOR.
- Compound assignment reuses the left expression for its read and write;
  index/object expressions can execute twice. Bind side-effecting indices first.
- `type` aliases `Field`, `BinField`, and `ExtField` constructors only.
  Write primitive and aggregate types directly.

## Numeric contracts

Primitive integers include signed/unsigned widths 8, 16, 32, 64, 128, 256,
and 512. Wide `/` and `%` are refused. Primitive addition, subtraction, and
multiplication wrap at the width; integer division truncates toward zero.
Division by zero and signed minimum divided by `-1` fail explicitly.

`Field<p>` uses a compile-time prime through 64 bits. `BinField<k, poly>`
uses a checked binary reducing polynomial; `ExtField<Field<p>, 2>` is the
quadratic extension. `/` is field division only when an operand is field-typed.
For example, `1 / (100 as F)` in `Field<251>` is `123`; `1 / 100` is `0`.

`let f: Field = field(p);` constructs a runtime prime carrier. A generic
`fn f<F: Field>(...)` can be instantiated with `instantiate f<dyn>;`; the
resulting `f_dyn` takes the carrier as an extra leading argument.

| Family | Meaning | Important rule |
|---|---|---|
| `Int<B>` | `i64` with magnitude bound `B` | Arithmetic propagates bounds |
| `Exp<E>` | Raw `i64` representing `raw / 2^E` | Add/compare require equal scales |
| `Fix<B,E>` | Magnitude-bound raw mantissa with scale `E` | Both contracts propagate |

Family scales are in `0..62`, and bounds must fit a nonnegative `i64`.
Literals in family context are raw integers, not automatically scaled values.
Use `requant(x)` in a typed `Exp` or `Fix` destination to lower the scale;
rounding is nearest with ties toward positive infinity. Explicit `as` casts
cross family boundaries without runtime bound validation or rescaling.
A destination annotation alone cannot turn plain-integer variables into family
operands. See the spec for the operator tables and mutable-alias restrictions.

There are no floating-point source types. For IEEE bit-pattern arithmetic,
read [ieee-bits.md](../spec/ieee-bits.md); for CUDA tensor arithmetic, read
[cuda-native-tensor.md](../spec/cuda-native-tensor.md). These are explicit
numerical boundaries, with different contracts from integer and field operators.

## Aggregates and abstraction

Structs and enums can be generic; const generics size fixed arrays.
`Result<T,E>` is an ordinary library enum in `src/lib/core/result.tv`.
`x?` unwraps `Ok` or returns `Err` from a compatible enclosing function.

Traits and operator overloads dispatch statically. Method calls resolve through
the receiver's implementation or a free-function fallback. Closures capture by
value, are stack-only, and can be passed to generic higher-order functions;
they cannot escape through returns or stored aggregate fields. Function pointers
use `fn(T) -> R` and `&function_name` and make indirect calls.

Closure captures are snapshots taken at creation, including whole arrays,
aggregates, and wide integers. Captured pointers still share their pointees.
Nested closures follow lexical shadowing. A closure's explicit result type
uses ordinary return conversions; its loop and defer context is independent of
the enclosing function.

Callback signatures must match in full, including signedness, widths, nested
callback types, and family bounds/scales. Assigning, returning, storing, or
casting a callback does not erase those checks. Use a wrapper function with the
required signature when a conversion is needed. Native callbacks can return
struct/enum values; the interpreter retains its separate indirect-call limits.

`poly(...)` and `register(...)` reserve the full destination degree plus one
coefficients. Omitted coefficients are zero; excess coefficients are rejected.

## Memory and imports

```traveler
fn main() {
    let values: *i32 = alloc(4);
    defer free(values);
    values[0] = 42;
    print(values[0]);
}
```

`alloc(n)` counts destination-typed elements and zero-fills them. It needs a
pointer-typed destination. `realloc(p, n)` preserves existing bytes but does
not zero-fill growth; successful relocation invalidates old derived pointers.
Use indices across growth. `[T; N]` is a fixed array; `var a: [i32; 4] = 0;`
zero-initializes it. Raw pointers and arrays have no automatic bounds or
lifetime checks. There is no slice or borrow-checking type system.

`defer` runs on function exit, in reverse registration order, after computing
the returned value. Declare it on the function's top-level statement spine,
not inside a branch or loop. Deferred expressions use the bindings visible at
registration; inspect the spec before depending on cleanup-time mutations.

`import "../src/lib/collections/vec.tv";` includes source in one shared
namespace. Inclusion is transitive, deduplicated, and cycle-safe. Matching
`extern "C"` declarations coalesce; conflicting declarations and duplicate
native definitions fail. Use `#[export]` for external function linkage.
`pub` does not establish module privacy.

## Parallel and device execution

Use ordinary `for` loops. Admission needs independent iterations and a
supported access/effect pattern. An outer scalar accumulation, an unknown
indirect call, or an unproved write index can keep a loop serial.

Inspect both pfor reports. `dispatched:1` means dispatch scaffolding exists,
not that threads ran. Alias status may be `serial`, `checked`, `disjoint`, or
`not-admitted`; missing read footprints and overlapping or uncertain byte
ranges can force serial execution. Loop-backedge changes matter to that proof.
Trip count and `TRAVELER_THREADS` also affect dispatch. Use `TRAVELER_THREADS=1`
for a serial comparison.

Writes that overlap captured binding storage must use the original serial loop
with live values, rather than a worker's snapshots. Reassigning a callee's pointer
parameter also invalidates its original read-footprint identity and can prevent
parallel admission. Do not infer safety from an initially disjoint pointer alone.

Device support has separate admission rules. Do not infer GPU support from
CPU pfor admission. NVIDIA supports explicit `#[kernel]` entries as well as
admitted workers; cooperative entries use `GpuThread` and checked participation
rules. Start with [device scalar control](../spec/device-scalar-control.md)
and the backend recipes in [BUILD.md](../BUILD.md). Consult the focused specs
for shared memory, warp operations, tensor operations, prepared CUDA packages,
and resource ownership before writing those calls.

## Library starting points

Paths below are relative to `src/lib/`.

| Task | Modules |
|---|---|
| Polynomial operations | `core/poly_core.tv`, `core/poly_core_generic.tv` |
| Compression and streaming | `codec/`, `util/stream_protocol.tv` |
| Containers | `collections/vec.tv`, `collections/string.tv`, `collections/hashmap.tv` |
| Bounded/fixed-point and RNS kernels | `nn/`, `rns/` |
| Number theory and error correction | `nt/`, `ecc/` |
| Files, sockets, clocks | `fs/fs.tv`, `net/tcp.tv`, `net/unix.tv`, `time/time.tv` |
| Memory and DAX | `mem/` |
| Device runtimes | `gpu/` |
| Graphics | `gfx/` |

The cryptographic/ZK proof stack is quarantined and under repair. Its presence
in the tree or successful compilation is not evidence of secure proof behavior.

## Verification workflow

For a Traveler application, compile and run the actual program against expected
results. Use `--eval` as an additional check where supported, not as a substitute
for native execution. For compiler/library edits, use the relevant regression
suite and the contributor gates.

```sh
tests/run_all.sh --list
tests/run_all.sh --suite=diag
tests/run_all.sh
```

The dispatcher adapts to local toolchains; hardware-specific checks can skip.
Record what ran and what skipped. Build/test commands can take several minutes;
use the harness's supported long-running-command mechanism and retain the exit
status. Search named compiler functions and read bounded sections rather than
loading the compiler or bootstrap IR in full.
