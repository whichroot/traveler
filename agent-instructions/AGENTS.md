# AGENTS.md — Traveler

Canonical instructions for an AI coding agent working in this repository. 

Everything referenced here lives in the public tree (`src/`, `examples/`,
`spec/`, `tests/`, `BUILD.md`). If a path is not in the checkout, do not rely on
it.

## What Traveler is

- A general-purpose systems language. File extension `.tv`. Compiles to native
  code through emitting LLVM IR.
- **Integer/field arithmetic only: there are no floating-point types.** Exact
  arithmetic is the point. Floats enter only as bit-patterns decoded into exact
  values (`src/lib/float/`).
- The prime field is a first-class type (`Field<p>`), monomorphized to branchless
  modular arithmetic.
- `for` loops auto-parallelize when the compiler can prove the iterations
  independent (default-deny: the fallback is serial, never a race).
- Self-hosting: the compiler `src/tvc_self.tv` is written in Traveler and boots
  from a checked-in snapshot. No C is in the trust chain.

Definitive reference: [`spec/language-spec.md`](../spec/language-spec.md). Build
details: [`BUILD.md`](../BUILD.md). Working code for every construct:
[`examples/`](../examples/).

---

## 1. Build, compile, run

Requires **LLVM 15+** (`llc`) and a C toolchain's `cc` (used only as a linker).
Point `LLC` at your `llc`:

- macOS (Homebrew): `/opt/homebrew/opt/llvm@21/bin/llc`
- Linux (apt): `/usr/lib/llvm-21/bin/llc`

Build the compiler once:

```sh
export LLC=/opt/homebrew/opt/llvm@21/bin/llc      # or your platform's llc path
src/bootstrap/build.sh                             # -> src/bootstrap/out/stage1 (the compiler)
```

Then compile and run a program. **`stage1` defaults to the host triple**
(detected via `uname`), so the emitted IR matches the local `llc` with no
`-mtriple` needed. Cross-compile by passing `-target <triple>` to `stage1` **and**
the matching `-mtriple=<triple>` to `llc`. On Linux, link with `-no-pie`.

Host build (macOS or Linux):

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/p.ll
$LLC -filetype=obj /tmp/p.ll -o /tmp/p.o
cc -no-pie /tmp/p.o -o /tmp/p                      # -no-pie on Linux; plain cc on macOS
/tmp/p                                             # prints: 49 100 171 2 123 1
```

Cross-compile (example: target x86_64-linux from any host):

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/p.ll -target x86_64-linux-gnu
$LLC -mtriple=x86_64-linux-gnu -filetype=obj /tmp/p.ll -o /tmp/p.o
```

`stage1` emits LLVM IR (`-o file.ll`) by default and is the compiler to use for
any program that uses `import` (the legacy `src-legacy/tvc` seed predates
`import`). The one-shot `--emit exe` / `--emit obj` driver host-retargets on its
own — on Linux it auto-adds `-mtriple=<host>` to the internal `llc` and `-no-pie`
to the internal `cc`, so `stage1 prog.tv -o prog --emit exe` links and runs
without the manual triple dance above. Closed LLVM 21 middle-end profiles are
available through `--opt-level promote|o1 -opt <path>`; raw IR remains the
default `none` profile. For profile details, shared-library, and multi-file
recipes, see [`BUILD.md`](../BUILD.md).

Run programs at a fixed worker count (auto-parallelized loops; output is
identical for any valid value):

```sh
TRAVELER_THREADS=8 /tmp/p             # unset -> sysconf(_SC_NPROCESSORS_ONLN); 1 -> serial
```

Run the test suites the local machine supports — the dispatcher probes
llc/link drivers/Wayland/AGX and routes accordingly (missing tools degrade to
SKIP, never a hard failure):

```sh
tests/run_all.sh               # probe + run; --list shows the plan, --suite=NAME runs one
```

Read-only compiler query modes (parse/analyze, no program emitted):

| Flag | Effect |
|---|---|
| `--diagnostics` | Parse + typecheck; JSON-Lines errors (`file:line:col`, message). |
| `--symbols` / `--references` | Enumerate definitions / use-sites. |
| `--pfor-report` | One JSON record per `for` loop: did it parallelize, and if not, the reason. |
| `--eval` | Run in a tree-walking interpreter (no LLVM). |
| `--emit-gpu` | Re-emit elementwise parallel loops as AMD GCN kernels (early). |
| `--emit-gpu-nvptx` | Same, NVIDIA target: `nvptx64-nvidia-cuda` PTX kernels (early). |
| `--emit-gpu-agx` | Direct AGX G16X instruction hex for the proved narrow and canonical `2^64-59` field-map profiles. |
| `-target <triple>` | Cross-compile IR (default: the detected host triple). |

---

## 2. Language reference

Enough to generate valid programs. All snippets are the exact syntax used in
`examples/`.

### Program skeleton

```tv
// line comment
type F = Field<251>;               // field type alias (the prime is part of the type)

fn main() {                        // or: fn main() -> i32 { ...; return 0; }
    let a: F = 200;                // let is immutable
    var total: i32 = 0;            // var is mutable (== "let mut")
    total = total + 1;
    print(a + a);                  // print takes one value, writes it as an integer
}
```

### Fields and arithmetic

```tv
type F = Field<251>;
fn main() {
    let a: F = 200; let b: F = 100;
    print(a + b);                  // 49   (300 mod 251)
    print(a * b);                  // 171
    print(a / b);                  // 2    (field division = multiply by inverse)
    print(1 / b);                  // 123  (inverse)
    print(a ** 3);                 // exponentiation
}
```

`/` is field division when an operand has a field type (either side — a typed
binding or an `as F` cast). With **no** field operand — `1 / 100` — it is plain
integer `sdiv` (0 here). To invert an untyped constant, bind or cast one side:
`print(1 / (100 as F))`.

Field carriers: `Field<p>` (any 64-bit prime; `Field<18446744069414584321>` is
Goldilocks), `BinField<K, poly>` (GF(2^K), `+` is XOR), `ExtField<Field<p>, n>`
(degree-`n` extension). A negative literal `-1` in a field means `p-1`. In the
concrete four-operation kernel (§3), `p = 0` selects raw integers (no modulo).

Runtime prime: `let f: Field = field(p);` builds a carrier at runtime. Prefer a
baked `Field<p>` when the prime is fixed (it vectorizes); use `field(p)` when the
prime is a runtime value.

### Control flow, operators, casts

```tv
for i in 0..10 { print(i); }       // range [0, 10)
var j: i32 = 0;
while j < 5 { j += 1; }            // compound assign: += -= *= /= %= &= |= ^= <<= >>=
for k in 0..100 { if k == 50 { break; } if k % 2 == 0 { continue; } }

let n: i64 = (100 as Field<251>) as i64;   // explicit cast; no implicit widening
let w: i128 = 123 as i128;                  // i128/u128/i256/u256 exist (no / or % on them)

let p: *i32 = null;
if p != null && p[0] == 5 { print(1); }     // && / || short-circuit; RHS may not run
```

Operators: arithmetic `+ - * / %`, bitwise `& | ^ << >>`, comparison
`== != < <= > >=`, short-circuit `&& ||`. On a field, `/` is modular inverse.

### Structs, enums, match

```tv
struct Rect { w: i32, h: i32 }

enum Shape { Circle(i32), Rect(i32, i32), Empty }

fn main() {
    let r: Rect = Rect { w: 10, h: 20 };
    print(r.w * r.h);              // 200
    let c: Shape = Shape::Circle(5);
    match c {
        Shape::Circle(radius) => { print(radius); }
        Shape::Rect(w, h)     => { print(w + h); }
        Shape::Empty          => { print(0); }
        _                     => { print(0 - 1); }   // wildcard arm is supported
    }
}
```

Supply every arm (no exhaustiveness inference). `match` also works on integers.

### Generics, traits, closures

```tv
struct Box<T> { val: T }                       // monomorphized per instantiation; no vtables
fn id<T>(x: T) -> T { return x; }
// instantiate id<i32>;                        // force a named instance for separate compilation

trait Shape { fn area(self) -> i32; }
impl Shape for Rect { fn area(self) -> i32 { return self.w * self.h; } }
// call site: Shape__Rect__area(&r)            // static dispatch, mangled Trait__Type__method

fn apply<C>(c: C, v: i32) -> i32 { return c(v); }
fn use_closures() {
    let base: i32 = 100;
    let add_base = |x: i32| x + base;          // captures by value, stack-only
    print(apply(|x: i32| x + 1, 41));          // pass closures DOWN to generic HOFs
}
```

Overloading `+ - * ==` routes through `Add`/`Sub`/`Mul`/`Eq`. Const generics feed
array sizes: `struct Buf<T, const N> { data: [T; N] }`. Function pointers are
`fn(T) -> R`, taken with `&fn_name`.

### Memory, pointers, arrays

```tv
let a: *i32 = alloc(4);            // heap; element type inferred; alloc(n) = n elements
a[0] = 100;
let b: *i32 = realloc(a, 8);
var stack: [i32; 10] = 0;          // fixed stack array, zero-initialized
free(b);
```

`*T` raw pointer, `&x` address-of, `p.field` auto-derefs, `null` literal. No
bounds checking. No slice type — pass a pointer and a length.

### Modules (`import`)

```tv
import "../src/lib/collections/vec.tv";   // path relative to THIS file; spliced at lex time
```

Transitive, diamond-deduped, cycle-safe. `import` requires `stage1` (§1).

### FFI (`extern "C"`)

```tv
extern "C" fn write(fd: i32, buf: *u8, n: usize) -> i64;
fn main() { let s: *u8 = "hi\n"; write(1, s, 3); }   // string literals are *u8, NUL-terminated
```

No struct-layout sugar — build C structs as raw byte buffers.

### Errors: `Result<T, E>` and `?`

```tv
import "../src/lib/fs/fs.tv";
fn run(path: *u8) -> Result<i64, FsError> {
    let w: i64 = fs_write_string(path, /*&Str*/ 0)?;   // ? returns early on Err
    return Result::Ok(w);
}
fn main() -> i32 {
    match run("/tmp/x") {
        Result::Ok(v)  => { print(v); }
        Result::Err(e) => { match e { FsError::OpenFailed => { print(404); } _ => { print(0-1); } } }
    }
    return 0;
}
```

`x?` unwraps `Ok` in place or returns the enclosing function's `Result` on `Err`
(the enclosing function must return a compatible `Result`). This is the error
model at library boundaries; `Result<T,E>` is in `src/lib/core/result.tv`.

---

## 3. Fast, exact computation

### Auto-parallelization

Write a `for` loop the analyzer can prove independent and it dispatches to
threads. No special syntax — the shape decides:

```tv
for i in 0..n { out[i] = a[i] + b[i]; }        // field/primitive elements, own-cell writes -> parallel
```

Admitted: field (`Field<p>`/`field(p)`) or primitive (`i8`..`u64`, `bool`,
`usize`) element arrays; stride-1/affine/injective writes; own-cell or disjoint
reads; pure calls; a `var` declared inside the body.

Refused (loop stays serial — never a race): reassigning an outer `var`
(`assign-carried`); indirect/function-pointer call (`unsupported-stmt`);
non-affine/non-injective index (`nonaffine`/`noninjective`); non-field,
non-primitive capture (`cap-elem`); unproven raw-pointer indexing (`raw`).

Inspect per loop:

```sh
src/bootstrap/out/stage1 prog.tv --pfor-report 2>/dev/null
# {"fn":"main","line":..,"col":..,"var":"i","independent":1,"has_field":1,"ncaps":2,"dispatched":1,"reason":""}
```

`dispatched:1` ran parallel. `dispatched:0` with a named `reason` is a soundness
refusal to design around. `TRAVELER_THREADS` decides worker count at runtime.

### The four operations (`src/lib/core/`)

The kernel most analysis/compression composes from. Two forms ship.

Concrete `i32` (prime is a runtime arg; `p>0` field, `p=0` raw integers), in
`poly_core.tv`:

```tv
fn forward_sum(coeffs: *i32, order: i32, out: *i32, n: i32, p: i32, reg: *i32);
fn forward_diff(data: *i32, n: i32, coeffs: *i32, max_order: i32, p: i32, work: *i32) -> i32;
fn regime_detect(data: *i32, n: i32, order: i32, threshold: i32, p: i32, reg: *i32) -> i32;
fn eval_at(coeffs: *i32, order: i32, x: i32, p: i32) -> i32;
```

Carrier-generic (prime lives in `F`, no `p` arg), in `poly_core_generic.tv`:

```tv
fn forward_sum<F: Field>(coeffs: *F, order: i32, out: *F, n: i32, reg: *F);
fn forward_diff<F: Field>(data: *F, n: i32, coeffs: *F, max_order: i32, work: *F) -> i32;
fn regime_detect<F: Field>(data: *F, n: i32, order: i32, threshold: i32, reg: *F) -> i32;
fn eval_at<F: Field>(coeffs: *F, order: i32, x: F) -> F;
```

- `forward_diff` — recover a polynomial's Newton coefficients from values.
- `forward_sum` — reconstruct values from coefficients (inverse of `forward_diff`).
- `regime_detect` — return the first index where data breaks the order-`k` model.
- `eval_at` — evaluate the polynomial at a point.

Examples: `poly_core_test`, `poly_core_generic_test`, `segment_test`,
`regime_basics`. Callable either by `import "../src/lib/core/poly_core.tv";` or by
declaring the symbol `extern "C"` and linking the compiled object.

### Exact matmul over a prime basis (`src/lib/rns/`)

Bit-exact integer matmul past a 64-bit accumulator; **precision = prime count**
(runtime dial, ~31 bits per prime). Each channel is an independent field array,
so the MAC auto-parallelizes.

```tv
import "../src/lib/rns/rns_dyn.tv";
fn rns_mac_dyn<F: Field>(xr: *F, wr: *F, r: *F, T: i32, K: i32, N: i32);   // + instantiate <dyn>
fn rns_crt_pass(P: *i64, k: i32, rtab: **i64, y: *i64, T: i32, N: i32, shift: i64);
```

`rns_mac_dyn` is declared generic and `instantiate rns_mac_dyn<dyn>;`, so the
callable name is `rns_mac_dyn_dyn(f, xr, wr, r, T, K, N)` with the runtime carrier
`f` prepended. Full pipeline (reduce per prime → MAC → CRT reconstruct):
`examples/rns_dyn_matmul.tv`. Baked fixed-basis variants: `rns3.tv`, `rns4.tv`.
To extend range, add a prime: bump `k`, extend `P`, call the same functions.

### Baked vs `dyn` instantiation

```tv
fn my_poly<F: Field>(x: F) -> F { return x * x + x + 1; }
instantiate my_poly<F251>;         // baked: emits my_poly_F251(x) — vectorizes
instantiate my_poly<dyn>;          // dyn:   emits my_poly_dyn(f, x) — carrier first, prime late-bound
```

No dispatch is erased either way; the carrier is constants, not a vtable.

### ZK proofs (`src/lib/zk/`, `src/lib/crypto/`)

Mark a pure function `#[zk]` and it compiles to native code **and** a PLONK
circuit. Add `export` for a C ABI so a driver can link and call it:

```tv
type Goldilocks = Field<18446744069414584321>;
#[export zk]
fn cubic_check(x: Goldilocks) -> Goldilocks { return x * x * x + x + 5; }
```

The compiler also emits a prover companion `cubic_check_zk_prove(...)`. A driver
declares both `extern "C"` and calls them — full pattern in
`examples/zk_cubic_test.tv`; end-to-end over a socket in `examples/net_zk_serve.tv`.
Use `#[zk]` (no `export`) for a generic circuit over a runtime prime
(`instantiate <dyn>` emits a dyn-PLONK prover). Primitives (`ntt`, `poseidon2`,
`merkle`, `fri`, `plonk`) are generic over the field; reach them by linking the
object and declaring the `extern "C"` symbol.

---

## 4. Standard library — capability index

To do X, import Y. Import paths are written relative to a program in
`examples/`; adjust the prefix for your file's location. **[self]** = uses
`import`, so build with `stage1` (§1).

| To build… | Use | Representative example |
|---|---|---|
| exact/parallel matmul, quantized-model inference | `rns/rns_dyn.tv`, `rns/rns3.tv`, `rns/rns4.tv` [self]; `float/{ieee,embed,quant}.tv`; `nn/{fixed,linear}.tv` [self] | `rns_dyn_matmul`, `wide_acc_matmul`, `nn_kit_gate`, `embed_roundtrip_test` |
| compression / codecs | `codec/piecewise_codec.tv` [self]; the four ops in `core/` | `pc_golden`, `wav_compress`, `cascade_shape` |
| change / regime detection on a stream | `core/poly_core*.tv`; `regime/`; `observe/trace.tv` [self]; `time/time.tv` [self] | `regime_basics`, `time_regime`, `observe_onset` |
| error-correcting codes | `ecc/{rs_core,rs_errdec_core,reed_solomon,rs_errdec}.tv` [self] | `reed_solomon_test`, `rs16_errdec_test` |
| cryptography / zero-knowledge | `crypto/{ntt,poseidon2,merkle,fri,plonk}.tv`; `zk/*.tv` | `plonk_test`, `zk_cubic_test`, `net_zk_serve` |
| number theory (zeta, Möbius, curves) | `nt/{linrec,linalg,polyfield,curve,sqrt,crtsolve}.tv` [self] | `weil_zeta_ec`, `mobius_cutoff`, `perfect_square` |
| curve stepping (rasterization primitives) | `forward_sum` in `core/` (a Bézier is a polynomial) | `glyph_convergence_test`, `convergence_test` |
| files / sockets / clock | `fs/fs.tv`, `net/tcp.tv`, `time/time.tv` [self] | `fs_basics`, `net_loopback`, `time_basics` |
| JSON, string/number formatting | `json/{json,json_parse}.tv`, `fmt/fmt.tv` [self] | `lab_emit`, `lab_request` |
| generic containers | `collections/{vec,string,hashmap}.tv` [self] | `vec_test`, `hashmap_test` |

Key signatures (grep the module file for the full set — every module is plain
`.tv`, and nearly all have an example that imports them):

- **collections:** `vec_new<T>` `vec_push` `vec_pop` `vec_get` `vec_set` `vec_len`
  `vec_free`; `str_new` `str_push` `str_len` `str_byte` `str_eq` `str_free`;
  `hashmap_new<K,V>(key_width)` `hashmap_insert` `hashmap_get` `hashmap_contains`
  `hashmap_len` `hashmap_free`.
- **fs** → `Result<_, FsError>`: `fs_write_string` `fs_read_to_string`
  `fs_append_string` `fs_exists` `fs_size` `fs_write_atomic` `fs_mkdir`
  `fs_rename`.
- **net** → `Result<_, NetError>`: `tcp_listen` `tcp_accept` `tcp_connect_local`
  `tcp_read` `tcp_write` `tcp_close`.
- **time** → `Result<i64, TimeError>`: `time_mono_ns` `time_wall_ns`
  `time_sleep_ns` `time_sleep_ms`.
- **ecc:** `rs_encode<F>` `rs_decode<F>` (+ syndrome/Berlekamp-Massey error decode).
- **nt:** `bm<F>` (Berlekamp-Massey); `la_nullspace`/`la_solve`; `sqrt_mod`.

GPU codegen is early-stage. AMD GCN (`--emit-gpu`) and NVIDIA
(`--emit-gpu-nvptx`) emit LLVM device modules gated by `llc`. AGX
(`--emit-gpu-agx`) directly emits measured G16X instructions for unary field
maps over the proved narrow-prime profiles and canonical `2^64-59`, plus narrow
two-input own-cell maps; arbitrary 64-bit primes remain refused. `src/lib/gpu/` contains the
measured-profile Traveler IOKit runtime: it consumes an exact-build `AGXDISP3`
profile image, authors the executed dispatch, and links only IOKit + libSystem.
The hardware gate compiles the same pfor for CPU and AGX and compares raw bytes
and status over 256 reproducible adversarial/random values for each profile.
`--agx-dispatch` is the opt-in host seam: imported runtime + same-source artifact
in, AGX attempted only after worker ID/field/grid and compiler-emitted FNV-1a
code pin match, CPU pfor fallback preserved. The pin catches wrong builds; it is
not artifact authentication. Its RNS consumer forms a three-prime product tensor
on AGX, then reduces and CRTs on CPU. Nested reductions, private mutables,
dynamic carriers, and performance claims remain outside the admitted AGX
boundary; unsupported workers skip.

---

## 5. Refusals and gotchas

These generate compile errors or documented fallbacks, never silent
misbehavior. Do not write code that relies on them.

- **No floating-point types.** This is an exact integer/field language.
- **`let` is immutable.** Use `var` to mutate; reassigning a `let`
  is an error.
- **`type` aliases field types only** (`type F = Field<251>;`). A general type
  alias is refused.
- **Reserved keywords:** `field fn let var mut const type struct enum trait impl
  import instantiate extern match if else for in while return break continue as
  true false null`. (`print` is a builtin, not reserved.)
- **No `dyn Trait`, no vtables.** All trait/generic dispatch is static. Use a
  function pointer or `match` for runtime choice of behavior.
- **Closures are stack-only, by-value, non-escaping.** Returning one, storing one
  in a struct, reassigning a closure identity, or self-recursion are refused.
  Pass closures down (to generic HOFs), not out.
- **A function-pointer / indirect call in a loop blocks parallelization** (the
  callee can't be proven pure); that loop stays serial.
- **`i128/u128/i256/u256` have no `/` or `%`.** `+ - * << >> & | ^ == <`, casts,
  and literals work; division is refused (C-free trust chain).
- **`HashMap` is fixed-capacity and aborts on overflow;** size it up front.
  `Vec` grows. Neither bounds-checks.
- **Arrays are fixed-size `[T; N]`;** no slice type — pass pointer + length.
- **`import` needs `stage1`,** not the legacy C seed.
- **`match` needs every arm** (add `_` for a catch-all).

---
