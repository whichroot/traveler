# Traveler Language Specification

This reference describes the implemented language in `src/tvc_self.tv`.
Source files use the `.tv` extension. Traveler is an early-alpha systems
language; backend admission and library contracts are part of its usable surface.

Audit baseline: commit `b1bfccb`. The examples and rules below describe that
revision, not uncommitted work or a promise about a future release. Historical
unimplemented proposals are indexed separately in
[language-proposals.md](language-proposals.md).

The native compiler, interpreter, and device backends have different supported
subsets. A feature described as native does not imply interpreter or device
support. Library functions are identified as such; importing a library does not
add language syntax. Focused contracts under `spec/` supply the details for
numerical and device boundaries.

## Contents

1. [Compilation model](#1-compilation-model)
2. [Lexical structure](#2-lexical-structure)
3. [Type system](#3-type-system)
4. [Declarations](#4-declarations)
5. [Expressions](#5-expressions)
6. [Statements](#6-statements)
7. [Generics, traits, and closures](#7-generics-traits-and-closures)
8. [Field arithmetic](#8-field-arithmetic-semantics)
9. [Polynomials](#9-polynomial-semantics)
10. [Piecewise codec](#10-piecewise-polynomial-semantics)
11. [Streaming library](#11-stream-semantics)
12. [Memory](#12-memory-model)
13. [Imports and FFI](#13-module-system)
14. [Errors](#14-error-model)
15. [Code generation and parallel loops](#15-llvm-code-generation)
16. [Boundary cases](#16-edge-cases-and-corner-behaviors)
17. [Registers and dynamic fields](#17-sequential-polynomial-computation)
18. [Backends](#18-backends-and-targets)
19. [Library and verification index](#19-library-and-verification-index)

## 1. Compilation Model

Traveler compiles a source entry and its transitive imports into one LLVM
module. The canonical compiler is written in Traveler. The bootstrap pipeline
is:

```text
src/bootstrap/tvc_self.boot.ll -> llc + linker -> stage0
stage0 compiles src/tvc_self.tv -> stage1.ll -> llc + linker -> stage1
stage1 compiles src/tvc_self.tv -> stage2.ll
compare stage1.ll and stage2.ll
```

`src/bootstrap/build.sh` performs this fixed-point check. `--check` additionally
checks snapshot freshness. The historical C seed under `src-legacy/` is not
required for this pipeline and does not implement all current language features.

User compilation defaults to the detected host triple. `-target <triple>`
selects another target; a runnable cross-built program also requires compatible
backend tools, a linker, and target libraries. Bootstrap IR uses a canonical
triple for reproducible comparison; this is separate from the user default.

`--emit ir` is the default. `--emit obj` drives `llc`, and `--emit exe` also
drives the system linker. See [BUILD.md](../BUILD.md) for toolchain setup,
separate compilation, shared libraries, and cross-compilation.

## 2. Lexical Structure

### 2.1 Source, identifiers, and comments

Identifiers use ASCII letters, digits, and underscore, with no leading digit.
Keywords are case-sensitive. String literals and comments can contain non-ASCII
bytes; the lexer does not interpret Unicode identifiers.

Spaces, tabs, carriage returns, and newlines separate tokens. **Newlines do not
insert semicolons.** Bindings, assignments, expression statements, `return`,
`break`, and `continue` require `;`. Blocks use braces.

`//` comments extend to the next newline. `/* ... */` comments nest.

### 2.2 Keywords

The lexer recognizes these non-type keywords:

```text
field binfield extfield fn let var mut const type pub struct enum
trait impl import instantiate extern if else for in while return
break continue match as true false null unsafe defer
```

Reserved words do not all introduce usable constructs: `mut` remains reserved
but `let mut` is rejected; `unsafe` has no block construct. `const` marks
generic parameters, not top-level constant declarations. `pub` is accepted
without module-privacy semantics.

Type-name tokens are:

```text
u8 u16 u32 u64 i8 i16 i32 i64 i128 u128 i256 u256 i512 u512
usize bool Field BinField ExtField Poly Register Int Exp Fix
```

Names such as `print`, `alloc`, `eval`, `register`, and `requant` are builtin
call names, not reserved words. Guarded builtins can be shadowed by a user
function. `self` is contextual in method declarations. `dyn` is reserved as a
type identity for runtime-field specialization rather than a general trait-object
keyword.

### 2.3 Integer and character literals

The lexer accepts decimal and hexadecimal (`0x` or `0X`) integer literals.
Binary/octal prefixes and underscore digit separators are not supported at this
revision. Malformed literal spellings are not reliably diagnosed; successful
compilation alone does not establish that such a spelling was read as intended.
A negative value is unary minus applied to a literal.

Literals use the expected type where available. An otherwise untyped integer
literal defaults to `i32`. Wide literals need a wide context; the compiler uses
their source digits rather than the lexer's 64-bit accumulator when emitting
wide values. Field and family contexts have the rules in sections 3 and 8.

Character literals such as `'A'` and `'\n'` produce integer values; there is
no separate character type.

### 2.4 String literals

A string literal is a `*u8` pointing to static, read-only, NUL-terminated
storage. It carries no length. Embedded NUL bytes are retained, including bytes
after the NUL and the additional final terminator. Decoded literal storage uses
a growable byte arena rather than a 255-byte token-text limit.

```traveler
fn main() {
    let text: *u8 = "a\0b";
    print(text[0] as i32);
    print(text[1] as i32);
    print(text[2] as i32);
    print(text[3] as i32);
}
```

This prints `97`, `0`, `98`, `0`. A C-style string operation stops at the first
NUL; explicit-length byte operations can access the whole literal.

At the audit baseline, the lexer decodes `\n`, `\r`, `\t`, and `\0`.
For other escaped bytes it drops the backslash and retains the following byte;
this includes `\\` and `\"`. **`\xHH` is not a hexadecimal escape at this
revision**, and `b"..."` is not a byte-string literal prefix. For arbitrary
binary data, initialize a `*u8` buffer or fixed byte array explicitly.

### 2.5 Operators and precedence

From tightest to loosest:

| Group | Operators/forms |
|---|---|
| Postfix | Calls, indexing, member access, casts with `as`, propagation `?` |
| Unary | `-`, `!`, `~`, address-of `&` |
| Power | `**` (right-associative) |
| Product | `*`, `/`, `%` |
| Sum | `+`, `-` |
| Shift | `<<`, `>>` |
| Bitwise | `&`, then `^`, then `|` |
| Comparison | `==`, `!=`, `<`, `>`, `<=`, `>=` |
| Logical | `&&`, then `||` |

Parentheses override precedence. In particular, write `(&a[i]) as *T` when
casting an element address: the postfix cast binds more tightly than address-of.
`^` is XOR, not power. Assignment is a statement, not an expression.

## 3. Type System

### 3.1 Primitive types

Signed and unsigned integers have widths 8, 16, 32, 64, 128, 256, and 512.
The compiler uses `usize` as a 64-bit unsigned integer on its current host
profiles. `bool` lowers to LLVM `i1`; in-memory boolean slots occupy a byte.
Comparisons produce boolean values.

Primitive `+`, `-`, and `*` wrap modulo the result width; generated operations
do not assert LLVM `nsw`/`nuw`. Signedness controls comparison, extension,
division, and right shift. Casts can truncate or sign-/zero-extend, so use
explicit types when width matters. The compiler also performs integer width
conversions at assignment, argument, and return boundaries; it is not correct
to describe all implicit integer conversion as forbidden.

`/` and `%` are supported through 64 bits. Signed division truncates toward
zero; the signed remainder follows the dividend. Division/remainder by zero
fail explicitly. Signed minimum divided by `-1` fails; its remainder is zero.
Wide `/` and `%` are rejected.

For a width `W`, shift counts are interpreted as unsigned values at the operand
width. Counts at least `W` produce zero for left shift and unsigned right shift;
signed right shift produces the sign fill (`0` or `-1`). Smaller signed right
shifts are arithmetic. Negative signed counts therefore follow the oversized
count rule, not LLVM poison behavior.

### 3.1.1 Magnitude-bounded integers: `Int<B>`

`Int<B>` stores an `i64` with the type-level contract `|x| <= B`, where
`0 <= B <= 9223372036854775807`. Bounds are erased in native representation.

| Operation | Result |
|---|---|
| `Int<a> + Int<b>` | `Int<a+b>` |
| `Int<a> - Int<b>` | `Int<a+b>` |
| `Int<a> * Int<b>` | `Int<a*b>` |
| Unary minus | Same bound |
| Division or arithmetic right shift | Left operand's bound |
| Comparison | Boolean |

Bound sums/products must fit the bound range. Left shift, remainder, bitwise
operations, and power are refused. Division/right shift require family-typed
operands (a contextually typed literal is permitted); a plain integer variable
requires an explicit cast. Division retains the integer failure rules.

A literal has its own tight magnitude bound. For example, `x + 100` for
`x: Int<5>` has bound 105; the literal need not fit the other operand's bound.
A literal stored directly in `Int<B>` must fit `B`.

### 3.1.2 Scale-typed integers: `Exp<E>`

`Exp<E>` stores an `i64` raw mantissa representing `raw / 2^E`, with
`0 <= E <= 62`. It tracks scale, not magnitude or runtime overflow.

| Operation | Result/requirement |
|---|---|
| `Exp<a> * Exp<b>` | `Exp<a+b>`, scale at most 62 |
| Multiply by a dimensionless plain integer | Same scale |
| Addition, subtraction, comparison | Equal operand scales |
| `x << k` | Scale `E+k`, at most 62 |
| `x >> k` | Scale `E-k`, with `k <= E` |
| Unary minus | Same scale |

Shift amounts must be literal integers in `0..62`. Division, remainder,
bitwise operations, and power are refused. Plain integer variables cannot
participate in addition/subtraction/comparison without an explicit cast.
Literals in scale context denote raw mantissas: `65536` at `Exp<16>` is 1.0.
Multiplication literals are dimensionless, so `x * 2` preserves `x`'s scale.

### 3.1.3 Bound-and-scale integers: `Fix<B,E>`

`Fix<B,E>` combines the magnitude and scale contracts on a raw `i64`.
The bound and scale ranges are those above.

| Operation | Result/requirement |
|---|---|
| `Fix<a,e> + Fix<b,e>` | `Fix<a+b,e>` |
| `Fix<a,e> - Fix<b,e>` | `Fix<a+b,e>` |
| `Fix<a,e> * Fix<b,f>` | `Fix<a*b,e+f>` |
| Multiply by a plain integer literal `k` | `Fix<a*abs(k),e>` |
| `x << k` | `Fix<B*2^k,E+k>` |
| `x >> k` | `Fix<ceil(B/2^k),E-k>` |
| Unary minus | Same bound and scale |
| Comparison | Equal scales, boolean result |

Computed bounds/scales must remain in range. Shifts require literal amounts in
`0..62`; right shifts cannot exceed the scale. The right-shift bound rounds
up because arithmetic shift rounds negative mantissas down. Division, remainder,
bitwise operations, and power are refused. A plain integer multiplier must be
a literal with a known magnitude, not an unbounded variable.

### 3.1.4 Family conversions and requantization

Within `Int`, a value may widen its bound implicitly. Within `Fix`, it may
widen its bound only at the same scale. `Exp` implicit conversion requires
the same scale. These checks apply to bindings, assignments, arguments, returns,
and aggregate stores. Pointer, array, and generic-wrapper conversions enforce
family compatibility in both directions to avoid widening through mutable aliases.

Plain-integer variables and values of another family require explicit `as`
casts to enter a family. A family destination annotation does not make arithmetic
on two plain-integer variables family arithmetic. Explicit casts change the
type contract; they do not insert runtime bound checks or rescale the mantissa.
The caller must establish the promised bound when using that escape hatch.

`requant(x)` uses a typed destination in the same scale-bearing family. For
source scale `E` and destination scale `D <= E`, put `s = E-D`:

- `s = 0`: preserve the mantissa.
- `s > 0`: compute mathematically `floor((raw + 2^(s-1)) / 2^s)` without
  overflowing the rounding addition. Ties round toward positive infinity.
- For `Fix`, the result bound is `floor((B + 2^(s-1)) / 2^s)`; the destination
  must admit that bound. The compiler rejects the rounding path if
  `B + 2^(s-1)` exceeds the bound range.

The current same-scale `Fix` no-op returns the destination type directly;
it does not recheck a narrower destination bound. Do not use that path to
establish a smaller magnitude contract.

```traveler
fn main() {
    let a: Int<5> = 5;
    let b: Int<105> = a + 100;
    let x: Exp<16> = 98304;
    let y: Exp<8> = requant(x);
    let q: Fix<10,8> = -9;
    let r: Fix<5,7> = q >> 1;
    print(b);
    print(y);
    print(r);
}
```

Native output: `105`, `384`, `-5`. The evaluator currently refuses `requant`
with `eval-refused: uncovered-call`; this example needs native compilation.

Type constants accept literals, const-generic parameters, parentheses, and
`+ - * << >>`. Closed expressions are checked for overflow. Parenthesize
shifts in type arguments, for example `Int<(N << 2)>`, to distinguish them
from closing angle brackets.

### 3.2 Field types

`Field<p>` requires a compile-time prime through 64 bits. The compiler checks
primality. Its carrier is the smallest supported unsigned width that holds
`p-1`: 8, 16, 32, or 64 bits.

`BinField<k, polynomial>` represents GF(2^k). The supported degree range is
`2..63` inclusive. The reducing polynomial must have degree `k` and pass the
irreducibility check. Elements use 8-, 16-, 32-, or 64-bit carriers, not
arbitrary byte lengths. `BinField<8, 0x11B>` is an example.

`ExtField<Field<p>, 2>` is a quadratic extension, stored as a pair of base-field
elements. Arithmetic uses a compiler-selected quadratic non-residue.
`ZERO`, `ONE`, `IMAG`, and `NR` are extension constants. Higher extension
degrees are not part of this implementation.

Bare `Field` is a runtime carrier, not a scalar residue. See section 17 for
`field(p)`, `dyn`, and `field_wide`.

### 3.3 Aggregates, pointers, and functions

| Type | Surface |
|---|---|
| Fixed array | `[T; N]`, including const-generic lengths |
| Struct | Named fields in declaration order |
| Enum | Named variants with positional payloads |
| Raw pointer | `*T`, including nested pointers |
| Function pointer | `fn(T, U) -> R` |
| Polynomial | `Poly<F, d>` |
| Summation register | `Register<F, d>` |

Structs and enums may have type and const parameters. They are monomorphized
per concrete instantiation. Recursive by-value layouts are unsupported; use a
pointer to express recursion. `Vec<T>`, `Str`, `HashMap<K,V>`, and `Result<T,E>`
are library-defined aggregates.

Pointers are raw addresses with no carried length or ownership. `&x` and
`&a[i]` form addresses; indexing reads/writes pointees. Member access through a
typed struct pointer automatically dereferences it. Pointer addition is refused;
use indexing or take an element address. Integer-to-pointer and pointer
reinterpretation casts are explicit unchecked operations.

There are no slice, tuple, unit-value, borrowed-reference, or floating-point
source types. A function without `-> Type` has no returned value.

## 4. Declarations

Field aliases may use `type` or the corresponding field declaration form:

```text
type F = Field<251>;
field F = Field<251>;
type B = BinField<8, 0x11B>;
type E = ExtField<Field<251>, 2>;
```

`type` accepts field constructors only; it is not a general alias for primitive
or aggregate types. Declarations belong at top level.

Functions declare typed parameters and an optional result type:

```traveler
fn add(x: i32, y: i32) -> i32 {
    return x + y;
}

fn main() {
    print(add(20, 22));
}
```

Use explicit `return`; ordinary function bodies do not return their last
expression implicitly. Calls check argument count. Function-pointer calls also
carry a declared parameter/result signature.

`let name [: Type] = expression;` creates an immutable binding;
`var name [: Type] = expression;` creates a mutable binding. Binding immutability
does not make a pointed-to allocation immutable. Top-level `let`/`var`
declarations use the compiler's global-initializer subset; they are not arbitrary
runtime initialization or a general compile-time evaluation engine.

`instantiate name<Concrete>;` requests a named concrete generic instance for
separate compilation. `#[export]` gives a function external linkage.
`extern "C" fn ...;` declares a foreign symbol. Backend annotations such as
`#[kernel]` have the contracts in section 18.

## 5. Expressions

Arithmetic dispatch follows the actual operand types and their field/family
contracts. A destination type alone does not redefine an operation on already
typed primitive operands. Numeric literals can acquire context from an operand
or destination.

`&&` and `||` evaluate the right operand only when needed. Ordinary call
arguments evaluate once in source order. `if` and `match` are statements,
not value-producing expressions.

`as` explicitly converts integer widths/signedness, field representatives,
family contracts, and pointer representations as supported by the source and
destination types. Distinct field domains cannot be converted directly: convert
to a plain integer and then into the destination field. Values wider than 64 bits
cannot be cast directly to a narrow static or dynamic field. An integer cannot
be cast to the bare `Field` carrier.

Enum values use `Name::Variant(arguments)`. Struct values use
`Name { field: expression, ... }`. Fixed-array zero initialization uses
`var a: [T; N] = 0;`; it initializes the array storage rather than assigning
a scalar pointer value.

The propagation operator `value?` applies to a compatible `Result<T,E>`.
It yields the `Ok` payload or constructs and returns the enclosing function's
`Err` result. Deferred cleanup runs on this early return. `Result` is defined
by the library; native aggregate payloads and their storage are preserved.

## 6. Statements

### 6.1 Assignment and blocks

Assignment supports mutable bindings, array/pointer elements, and struct
members. Compound operators are `+= -= *= /= %= &= |= ^= <<= >>=`.
They desugar using the left expression as both a read and a destination;
side-effecting object/index expressions can run twice. To guarantee a single
index evaluation, bind it before the assignment.

Blocks introduce lexical scopes. Shadowing does not change which earlier
declaration a deferred action references.

### 6.2 Conditionals and match

`if condition { ... } else if condition { ... } else { ... }` supports
ordinary native conditional control flow.

`match` selects enum variants or integer cases. Arms contain blocks; `_`
provides a default. Exhaustiveness is not checked. If no arm matches, execution
continues after the match. Enum payload bindings have independent value storage;
they are not views that can be changed by overwriting the source enum.

```traveler
enum Value { Number(i64), Empty }

fn main() {
    let v: Value = Value::Number(42);
    match v {
        Value::Number(n) => { print(n); }
        Value::Empty => { print(0); }
    }
}
```

### 6.3 Loops

`for i in lo..hi { ... }` iterates an exclusive integer range. Runtime
bounds are supported; the compiler does not prove termination. An empty or
reversed signed range runs zero iterations. Direct field enumeration and
inclusive `..=` ranges are not supported.

The iterator defaults to `i32` and widens to `i64` when a bound is 64-bit,
`u32`, or a literal at least `2^31`. Narrower bounds extend according to
signedness. The iteration space is signed; literal bounds at least `2^63`
are rejected, and runtime `u64`/`usize` bounds outside signed `i64` fail at
loop entry. Field, pointer, boolean, aggregate, and wide-integer bounds are
not supported.

`while condition { ... }` is a general native loop. `break;` exits the
innermost loop; `continue;` advances to its next iteration. Device loops
have a separate, narrower contract.

### 6.4 Return and defer

`return expression;` returns a value converted to the declared result type.
`return;` exits a function with no returned value.

`defer statement;` or `defer { statements }` registers cleanup on the enclosing
function frame. Cleanup runs in reverse registration order on explicit return,
`?` propagation, and fallthrough. Return values are computed before cleanup.

Deferred names bind to declarations visible where the `defer` occurs, but
mutable bindings are read when cleanup executes. Later shadowing does not
retarget a defer. Deferred local declarations stay within their cleanup scope.

A defer must appear on the function's top-level statement spine, not inside
`if`, `match`, `for`, or `while`. Its body cannot return from the function.

```traveler
fn main() {
    var n: i32 = 1;
    defer print(n);
    n = 2;
    print(3);
}
```

Output: `3`, then `2`.

## 7. Generics, Traits, and Closures

Generic functions, structs, and enums specialize at compile time. Type
parameters and `const` parameters can appear in field types, numeric-family
types, and fixed array sizes. Bare const parameters can be inferred from
concrete argument types; compound type expressions require their parameters
to be resolved.

`F: Field` selects compiler-known field arithmetic. User traits declare
methods, and `impl Trait for Type` supplies static implementations. Trait
bounds are checked when instantiating functions. `Add`, `Sub`, `Mul`, and `Eq`
implementations provide the corresponding operators on user types.

Methods resolve against the receiver's implementation, then through the
free-function fallback `method(&receiver, arguments)`. Trait methods receive
a pointer to the receiver. There is no dynamic trait/vtable dispatch.

A closure `|x: T| expression` or `|x: T| { ... }` captures its environment
by value. It has a compiler-known identity and lowers to a lifted function
plus stack capture storage. Generic higher-order functions specialize for that
identity. Closures can be called locally and passed down to generic functions;
escaping through a return or aggregate field, changing closure identity, and
recursive self-capture are refused.

Function pointers are distinct from closures. `&function_name` produces a
pointer for an explicit indirect call. An unknown indirect call blocks loop
parallelization because the analyzer cannot establish its effects.

```traveler
fn apply<C>(f: C, x: i32) -> i32 { return f(x); }

fn main() {
    let base: i32 = 10;
    let add = |x: i32| x + base;
    print(apply(add, 32));
}
```

## 8. Field Arithmetic Semantics

For a prime field, valid representatives are `0..p-1`:

| Operation | Meaning |
|---|---|
| `a + b` | `(a+b) mod p` |
| `a - b` | `(a-b) mod p` |
| `a * b` | `(a*b) mod p` |
| `a / b` | `a * inverse(b) mod p` |
| `-a` | Additive inverse; zero remains zero |
| `a ** n` | Repeated-squaring exponentiation |

Field literals and integer-to-field conversions reduce to canonical
representatives. `%` is not a field operation. A field operand is required
to select field division: with `type F = Field<251>;`, `1 / (100 as F)`
is 123, whereas primitive integer `1 / 100` is zero.

Inversion/division by zero fail, including in `Field<2>`; the exponent-zero
case does not define an inverse of zero. Native arithmetic failure calls
`abort`; the evaluator reports failure with status 134; emitted AMD/NVIDIA
device arithmetic uses a device trap.

For narrow primes multiplication widens before reduction. For 64-bit primes
the compiler emits Barrett reduction through wide intermediates rather than
requiring a software integer-division libcall. These are implementation choices;
the modular result is the contract. No blanket constant-time claim is implied.

Binary-field elements encode polynomial coefficients as bits. Addition and
subtraction are XOR; multiplication is carry-less multiplication followed by
reduction. An integer bit pattern `2` is the polynomial `x`, **not zero**:
characteristic two means `one + one == zero`, not that every even bit-pattern
representative is zero. The AES field has a table-based multiplication path;
other supported binary fields use shift/XOR reduction.

Quadratic extension arithmetic operates on base-field pairs. Integer literals
promote to a real component with zero imaginary component; `E::IMAG` supplies
the imaginary basis element.

Explicit IEEE operations on integer bit carriers have the independent
[IEEE numerical contract](ieee-bits.md). CUDA native tensor arithmetic has the
[native tensor contract](cuda-native-tensor.md). Neither changes ordinary
integer or field operators.

## 9. Polynomial Semantics

`Poly<F,d>` stores `d+1` field coefficients, with a compile-time degree.
The compiler tracks standard power-basis and Newton forward-difference
representations:

- `poly(c0, ..., cd)` constructs standard coefficients in a typed destination.
- `analyze(data, npts)` computes forward differences into the degree fixed by
  the destination `Poly<F,d>`; it does not discover a runtime degree or return
  an `Option`.
- `eval(p, point)` returns one scalar evaluation.
- Polynomial addition is representation-aware.

The constructor reserves all `d+1` coefficients. Omitted trailing coefficients
are zero; `poly()` constructs the zero polynomial at the declared degree.
More than `d+1` coefficients is an error. The same rules apply to `register(...)`;
an explicit dynamic-field carrier is not counted as a coefficient.

The emitted representation converters use 32-element scratch buffers, limiting
that conversion path to degree 31. Newton/power conversion also needs invertible
factorial divisors (`d < p` for the prime-field construction). These are caller
constraints, not a general degree-barrier diagnostic or an unlimited-degree
guarantee.

```traveler
type F = Field<251>;

fn main() {
    let p: Poly<F, 2> = poly(1, 2, 3);
    print(eval(p, 2));
}
```

Output: `17`.

Polynomial multiplication, division, GCD, and differentiation are library
operations over coefficient arrays, for example in `src/lib/nt/polyfield.tv`.
The compiler does not select a schoolbook/Karatsuba/NTT strategy for a general
`Poly * Poly` operator. The NTT is library code in `src/lib/crypto/ntt.tv`;
its field/order requirements belong to that API.

## 10. Piecewise Polynomial Semantics

Piecewise segmentation and compression are library operations in
`src/lib/codec/`, not `Piecewise`/`Segment` language types or a `segment()`
builtin. The library operates on buffers with explicit lengths and workspace.

The shipped segmenter uses encoded-size costs and dynamic-programming, greedy,
or particle-scan strategies. Its block kinds include `LITERAL`,
`POLY_RESIDUAL`, `CONSTANT`, `CONT`, and `PREDICTIVE`. A constant block is
not the hypothetical `PIECEWISE_CONST` staircase format from older drafts.

Use `src/lib/codec/piecewise_codec.tv` and the codec examples for signatures
and wire behavior. `examples/pc_golden.tv` exercises encoded bytes; language
syntax alone does not specify a compression ratio or segmentation optimum.

## 11. Stream Semantics

`src/lib/util/stream_protocol.tv` implements `StreamState`, a layered stream
adapter. It buffers signed integer samples, splits them into byte layers,
and maintains per-layer continuation state. It is not a compiler-known
`Stream<F>` type.

An active layer has a degree in `1..6` and seven register slots. Inactive
state has degree zero and zeroed slots. A polynomial block replaces state,
a continuation block advances it, and a literal block clears it. Continuation
is exact: all predicted values must match. A PCPW `CONT` block contains one
type byte and a two-byte length.

The standalone layered codec uses PCLY v1 with a dynamically selected layer
prime and no initial continuation state. `StreamState` emits PCLY v2 with
prime 257 in each layer. After the common 12-byte header, each v2 layer stores:

```text
layer_prime: i32 little-endian (257)
pcpw_size: i32 little-endian
active: u8
degree: u8
reserved: u16 (zero)
register[7]: i32 little-endian
pcpw_payload: pcpw_size bytes (PCPW version 1)
```

The stored registers describe the state immediately before the chunk, making
a chunk independently decodable even if its first block is `CONT`. The decoder
rejects noncanonical state, a nonzero reserved field, another layer prime, or
a register value outside `0..256`.

Encoding uses distinct committed/candidate continuation buffers. Candidate
state is committed only after encoding, lossless decode verification, and the
caller capacity check succeed. Failure preserves the previous state for retry.
The latest committed chunk is the late-join keyframe. An interior particle
boundary or full buffer emits a chunk; a capacity split preserves continuation.

## 12. Memory Model

### 12.1 Storage and layout

Traveler has no garbage collector, automatic destructors, or borrow checker.
Local arrays and aggregates have compile-time storage sizes; heap buffers can
have runtime sizes. Raw pointers have no automatic bounds or lifetime checks.

Struct fields retain declaration order with target-layout padding. Arrays use
the native element stride, including the tail padding of aggregate elements.
Do not calculate a struct-array stride by summing field sizes. The evaluator
uses host layout, including pointer width and aggregate alignment.

The current native enum layout has a tag byte at offset zero and payload
storage beginning at byte eight. Payload fields use their actual storage sizes
with alignment capped at eight; the maximum variant payload plus the prefix
is rounded to eight bytes. LLVM represents the enum as a tag plus byte array,
not as the platform's C enum or an automatically sized tag type. Aggregate
payload reservations include nested arrays, structs, enums, polynomials,
registers, and wide values. This is a compiler layout, not a portable wire format.

Struct/enum copies copy their value storage; copying pointer fields still
shares the pointed-to objects. A container copy is not a deep clone of its
allocation. Taking an address does not introduce a checked lifetime.

### 12.2 Allocation

`alloc(count)` derives its element type from its pointer destination and
allocates `count * sizeof(T)` bytes, then zero-fills them. Pointer context can
come from an annotation, assignment, argument, return, field, or pointer cast.
An allocation with no pointer-typed destination is rejected. Use `*u8` for
byte counts.

`realloc(pointer, count)` resizes to an element count. Preserved bytes retain
their values; newly grown bytes are uninitialized. On successful relocation,
old pointers and derived element pointers are invalid. `free(pointer)` releases
the allocation; `free(null)` is a no-op.

Default allocation has no reliable language-level out-of-memory recovery
contract. Invalid access, out-of-bounds indexing, double free, and use after
free are undefined in native code. Destination-derived sizing is not bounds
checking. `defer` can arrange cleanup; arenas and handle-based pools are
provided in `src/lib/mem/`.

`--alloc-debug` adds a 16-byte header and 16-byte trailer canary and checks
them on `free`/`realloc`. This can detect corruption at deallocation; it is
not an access-by-access memory checker. The evaluator separately tracks live
heap buffers and refuses double free and reallocating freed buffers.

### 12.3 Atomics

Host atomics accept naturally aligned pointers to 8-, 16-, 32-, or 64-bit
integers with statically visible element types:

| Builtin | Result and orders |
|---|---|
| `atomic_load(p, order)` | Loaded value; `relaxed`, `acquire`, `seq_cst` |
| `atomic_store(p, value, order)` | Store; `relaxed`, `release`, `seq_cst` |
| `atomic_add(p, value, order)` | Old value, wrapping addition |
| `atomic_cas(p, expected, new, order)` | Old value; writes if equal |
| `fence(order)` | `acquire`, `release`, `acq_rel`, `seq_cst` |

Read-modify-write operations admit `relaxed`, `acquire`, `release`, `acq_rel`,
and `seq_cst`. Orders are bare identifiers with LLVM memory-order meaning.
Compare-and-swap failure ordering drops the release component. Unaligned atomic
access is undefined. Ordinary loads/stores remain non-atomic.

These host builtins are impure, fence CPU loop parallelization, and are refused
by device emission. Device-specific atomic APIs have their own admission rules.

### 12.4 Streaming stores and DAX

[Host streaming stores](streaming-stores.md) specifies non-temporal store
operations, fences, and worker-completion publication.
[DAX bulk copies](dax-copy.md) distinguishes non-atomic copies from atomic
accesses. [CUDA DAX staging](cuda-dax-staging.md) specifies pinned buffers,
asynchronous uploads, event completion, and worker-pool ownership. These are
explicit APIs, not a change to ordinary pointer load/store semantics.

## 13. Module System

### 13.1 Imports

`import "path/to/file.tv";` includes source at lex time. Paths resolve relative
to the importing file. Inclusion is transitive, diamond-deduplicated, and
cycle-safe. Definitions share one translation-unit namespace; there are no
`module::item` imports or enforced module-private definitions.

Duplicate native definitions fail. Matching extern declarations coalesce.
`pub` does not control linkage; `#[export]` and explicit instantiation provide
external symbols. Separate compilation uses those symbols plus extern declarations.

### 13.2 Foreign function interface

`extern "C"` declares the symbol and signature to be resolved by the linker.
Calls pass the declared ABI arguments directly; no automatic marshalling or
C struct layout annotation is supplied. Libraries commonly pack OS structs
into byte buffers matching the target ABI.

Repeated extern declarations must match in return type, ordered parameter
types, ABI, and declaration attributes. Parameter names may differ. Equal LLVM
representations do not by themselves make distinct source signatures compatible.
A matching native definition suppresses the redundant extern declaration,
regardless of declaration order.

### 13.3 I/O and library imports

`print(value)` writes a decimal integer representation and a newline. Field
values print their representatives; boolean output is numeric. `read_bytes`
provides a POSIX read boundary. Formatting, files, sockets, clocks, and JSON
are library APIs under `src/lib/`; there is no `std::` namespace.

For example, a program in `examples/` can import
`"../src/lib/collections/vec.tv"`. Inspect module signatures for ownership,
capacity, and failure behavior; container operations do not gain automatic
bounds checks merely by being library calls.

## 14. Error Model

Compilation errors report source locations and exit nonzero. `--diagnostics`
provides JSON-Lines diagnostics. Parser recovery can report multiple errors in
one run. Examples of enforced errors include invalid field parameters,
incompatible family contracts, wrong call arity, conflicting declarations,
unsupported operators, and invalid defer placement.

| Runtime condition | Native behavior |
|---|---|
| Integer or field division by zero | Explicit arithmetic failure |
| Signed minimum divided by `-1` | Explicit arithmetic failure |
| Unsigned loop bound outside signed `i64` | Failure at loop entry |
| Unmatched non-exhaustive match | Fall through |
| Out-of-bounds or null pointer access | Undefined behavior |
| Use after free or double free | Undefined behavior |
| Allocation failure | No checked recovery guarantee |

The evaluator and `--alloc-debug` add the checks described in section 12.
Library `Result` values represent recoverable library failures. They do not
turn native arithmetic failures or arbitrary invalid memory accesses into `Err`.

`unsafe` is reserved but there is no `unsafe { ... }` construct. Pointer
forging/reinterpretation, FFI, and unchecked accesses remain explicit programmer
responsibilities throughout native code.

## 15. LLVM Code Generation

### 15.1 Native lowering

Primitives lower to LLVM integers; pointer types lower to opaque `ptr` while
the compiler retains source pointee types for indexing and sizing. Field
operations use generated arithmetic helpers. Families lower to raw `i64` with
compile-time bound/scale checks. Aggregate layout follows section 12.

Generic functions and lifted closures are emitted as specialized functions.
The compiler also emits runtime helpers for supported field, allocation,
printing, and parallel-dispatch operations. A selected LLVM middle-end profile
is separate from Traveler's type checking and loop admission.

### 15.18 Auto-Parallelization

Ordinary `for` loops can receive CPU dispatch scaffolding when the analyzer
proves supported iteration independence. The proof tracks declarations,
affine/injective writes, reads, captures, and effects through statically known
calls. Outer carried state, unknown indirect calls, incomplete effects, or
unproved access geometry can refuse admission.

Field and supported primitive element loops are eligible; the feature is not
limited to field arithmetic. A pure call alone is insufficient: its memory
footprint and write geometry must also be accounted for. Device admission is
a separate proof with additional limits.

Admission and runtime alias safety are distinct. The runtime guard uses byte
ranges, native element strides, and checked endpoint arithmetic, rather than
only comparing base pointers. Overlap, unknown read footprints, unsupported
provenance, and possible index wrap can force serial execution. Pointer facts
must remain valid across enclosing loop backedges; an initially fresh or
disjoint pointer is not enough if a later iteration changes it.

`--pfor-report` reports source loops and admission. `dispatched:1` means
dispatch scaffolding was emitted; it does not establish that threads ran.
`--pfor-alias-report` distinguishes:

| Status | Meaning |
|---|---|
| `not-admitted` | No admitted worker dispatch |
| `serial` | Alias guard forces serial execution |
| `checked` | Runtime guard decides |
| `disjoint` | Ranges proved disjoint |

Even with disjoint ranges, trip-count thresholds, grain size, and
`TRAVELER_THREADS` affect execution. `TRAVELER_THREADS=1` selects serial
execution; the default uses online CPU information subject to runtime limits.
The emitted runtime caps workers at 32 and uses a 512-iteration threshold.
If thread creation fails, the caller executes the affected disjoint chunk;
failure to join aborts instead of publishing an incomplete result.

Wrapping integer semantics remain defined in workers. Arithmetic failures
retain failure behavior across serial and parallel execution; this does not
promise identical timing of partially executed work before failure.

## 16. Edge Cases and Corner Behaviors

- `Field<2>` is distinct from `bool`. Its zero still has no inverse.
- A negative prime-field literal denotes its modular additive inverse, with
  zero remaining canonical zero.
- Different field aliases for the same domain can be compatible; different
  moduli require an explicit integer conversion boundary.
- A zero-length fixed array has no accessible elements. Zero initialization
  does not make an out-of-range access valid.
- `Poly<F,0>` stores a constant coefficient. Scalar `eval` remains scalar;
  there is no vector/range evaluation overload.
- Neither the polynomial type nor the codec guarantees a useful compression
  ratio for arbitrary data.
- `Fix` right shift and `requant` differ: arithmetic right shift rounds down;
  requantization rounds nearest with ties toward positive infinity.
- A cast into `Int` or `Fix` can assert an invalid bound. Type propagation
  assumes that explicit unchecked entry contract was satisfied.
- `let` pointer immutability prevents pointer reassignment, not writes through
  the pointer or aliasing through another pointer.
- A successful `--eval` run covers the interpreter subset on the host. It is
  not evidence that foreign calls or GPU execution occurred.

## 17. Sequential Polynomial Computation

### 17.2 Registers

`Register<F,d>` is compiler-known forward-summation state holding `d+1` field
values. Initialize it with `register(c0, ..., cd)` in a typed destination.
`advance(reg)` **returns the current `state[0]` first**, then updates
`state[k] += state[k+1]` in ascending order for `k < d`. The highest difference
is unchanged. `reg.advance()` is the method spelling.

```traveler
type F = Field<251>;

fn main() {
    var reg: Register<F, 2> = register(10, 4, 2);
    print(advance(reg));
    print(advance(reg));
    print(advance(reg));
}
```

Output: `10`, `14`, `20`. This order differs from returning the value after
the update.

### 17.3 Library polynomial operations

`src/lib/core/poly_core.tv` supplies concrete `i32` buffer operations:

```text
forward_sum(coeffs, order, out, n, p, reg)
forward_diff(data, n, coeffs, max_order, p, work) -> i32
regime_detect(data, n, order, threshold, p, reg) -> i32
eval_at(coeffs, order, x, p) -> i32
```

Here `p > 0` selects modular arithmetic and `p = 0` raw integer arithmetic.
`poly_core_generic.tv` provides carrier-generic versions with the field in
`F: Field`. These are imported functions, not four additional lexer keywords
or first-class regime/stream types. Buffer extents and workspace requirements
come from the library API.

### 17.10 Dynamic fields

`let f: Field = field(p);` constructs a runtime prime carrier and validates
the modulus. Its fields include `p`, `half_p`, `elem_bytes`, and `data_bytes`.
A second argument to `field` supplies a maximum data value from which packing
width is derived, not a literal number of bytes.

```traveler
fn square<F: Field>(x: F) -> F { return x * x; }
instantiate square<dyn>;

fn main() {
    let f: Field = field(251);
    print(square_dyn(f, 20 as i64));
}
```

Output: `149`. A `dyn` specialization gets an extra leading carrier argument.
Dynamic scalar elements use `i64` backing; the carrier supplies the modulus
and reduction constants. Calls remain statically selected: the carrier contains
constants, not a vtable. Dynamic-field workers can be admitted under the CPU
parallel proof.

`field_wide` constructs the separate four-limb wide-field carrier from a hex
string or four little-endian limbs. Its arithmetic uses wide intermediates
and the `field_wide_*` runtime helpers. This is not a static arbitrary-width
`Field<p>` type. Consult `tests/dynfield/` and wide-field examples for the
supported calling/storage protocol.

## 18. Backends and Targets

### 18.1 CPU middle-end profiles

| `--opt-level` | LLVM pipeline | Tool requirement |
|---|---|---|
| `none` | Raw Traveler IR (default) | No `opt` |
| `promote` | `mem2reg`, verification | LLVM 21 |
| `o1` | `default<O1>`, verification | LLVM 21 |
| `o3` | `default<O3>`, verification, explicit CPU | LLVM 21 |

These are closed profiles, not arbitrary pass strings. `o3` requires x86-64
and `-mcpu x86-64` or `-mcpu sapphirerapids`; `native` is refused. Backend
object lowering uses `llc -O2`. Explicit CPU SIMD operations have additional
target-selection requirements in [cpu-integer-simd.md](cpu-integer-simd.md).

Tools run with argument vectors. Outputs use exclusive sibling stages and are
published only after the requested compilation/tool steps succeed. Failed
compilation preserves an existing destination. Optimized IR is not the raw
bootstrap fixed-point artifact.

### 18.2 Typed-pointer compatibility

`-target tpc` selects a compatibility conversion from opaque-pointer LLVM IR
to typed-pointer IR. It is not a hardware architecture. It accepts profile
`none`; unsupported combinations refuse. The typed-pointer test suite checks
the admitted output against older LLVM tools.

### 18.3 NVIDIA and AMD

`--emit-gpu-nvptx` and `--emit-gpu` emit LLVM device modules for NVPTX and
AMDGCN respectively. `llc` lowers these to PTX or AMD objects. Emission is
not a device launch. Traveler runtimes under `src/lib/gpu/` use the CUDA driver
or HIP to load and submit supported artifacts.

The NVIDIA surface includes admitted parallel workers and explicit `#[kernel]`
entries. Cooperative entries use a leading `GpuThread` parameter, multidimensional
geometry, and explicit memory/participation APIs. Scalar device helpers support
mutable locals, branches, early returns, and rolled `while` loops within the
checked subset. Support is substantially broader than a fixed private-K=8 dot,
but does not imply that arbitrary native code can run on a device.

| Contract | Details |
|---|---|
| [Scalar control](device-scalar-control.md) | Helper expansion, loops, branch merges, warp-uniform participation, limits |
| [IEEE bit operations](ieee-bits.md) | Rounding, NaNs, carriers, backend admission |
| [Native tensor operations](cuda-native-tensor.md) | Opt-in MMA policy and lane fragments |
| [Asynchronous shared copies](cuda-shared-async.md) | Copy groups, completion, visibility |
| [Register limits](cuda-register-limits.md) | `#[cuda_max_registers(N)]` |
| [Prepared packages](cuda-prepare.md) | Source/toolchain identity and cache publication |
| [Event timing](cuda-event-timing.md) | Timed events and elapsed-time results |
| [DAX staging](cuda-dax-staging.md) | Pinned staging and asynchronous ownership |

These capabilities are backend-specific. In particular, warp uniformity does
not establish block-wide barrier participation or shared-memory phase safety.
Unsupported explicit entries fail emission rather than publishing a partial
module. See [BUILD.md](../BUILD.md) for runtime interfaces, bounded atomic
buffers, geometry, shared storage, and hardware checks.

### 18.4 AGX and Vulkan

`--emit-gpu-agx` emits measured Apple G16X instruction bytes directly. The
runtime requires an exact OS/GPU profile (`AGXDISP3`) and links through IOKit;
this is not an Apple-supported portable ABI. Admission covers bounded scalar
control/aggregates/calls and specific field/reduction profiles, with a
65,535-element grid limit. It is not generic support for arbitrary 64-bit primes.

`--agx-dispatch` emits a host program that can attempt the matching AGX worker
and fall back to CPU. Worker identity, field, grid, alias checks, and a
compiler-emitted machine-code digest constrain admission. The digest detects
wrong artifacts; it is not cryptographic authentication.

`--emit-gpu-vulkan` emits a closed GLSL profile, assembled to SPIR-V by a
standard tool. Its admitted map/private-dot shapes and Traveler-owned Vulkan
runtime are described in the build guide. GPU backends have separate proof
and runtime boundaries; acceptance by one does not establish acceptance by another.

### 18.5 Interpreter and research proof backend

`--eval` executes the supported tree-walking/evaluator path without LLVM
execution. It can refuse native features such as extension-field operations
or unsupported foreign/indirect calls. It is useful for differential checks,
not a substitute for native or device tests.

`#[zk]` and its PLONK/circuit companion machinery remain in the compiler and
libraries. The cryptographic/ZK proof stack is quarantined and under repair.
Emitting or accepting a proof with this research stack is not a security
guarantee. This reference does not retain the older claims of certified
soundness or production proof security.

## 19. Library and Verification Index

The standard library is source under `src/lib/`, included with `import`.

| Subsystem | Location |
|---|---|
| Polynomial buffers, `Result` | `core/` |
| Vectors, owned strings, maps | `collections/` |
| Piecewise and layered codecs | `codec/` |
| Regime and degree analysis | `regime/`, `genus/`, `observe/` |
| Error correction | `ecc/` |
| Number theory and polynomial arrays | `nt/` |
| Exact/fixed-point and RNS kernels | `nn/`, `rns/`, `float/` |
| Files, sockets, clocks, formatting, JSON | `fs/`, `net/`, `time/`, `fmt/`, `json/` |
| Arenas, pools, shared memory, DAX | `mem/` |
| Device runtimes | `gpu/` |
| Graphics | `gfx/` |

Graphics supplies a CPU framebuffer and headless/Wayland backends selected
by import. The Wayland implementation speaks the wire protocol over the OS
socket/shared-memory APIs. Both backend definitions cannot be imported together
when they define the same native symbols. Matching extern declarations, however,
can coexist under the rules in section 13.

The following checks provide concrete evidence for frequently changed contracts:

| Contract | Existing checks |
|---|---|
| Integer domains, field conversions, defined shifts | `tests/check_arithmetic_contract.py` |
| Literal storage | `tests/check_long_literals.py`, `tests/eval_diff/` |
| Family operands, stores, aliases, literals | `tests/check_family_*.py` |
| Scale/rounding and type-constant limits | `tests/check_exp_limits.py`, `tests/check_fix_shift_bound.py`, `tests/check_type_const_overflow.py` |
| Aggregate layout and value copies | `tests/check_enum_aggregate_layout.py`, `tests/check_struct_copies.py`, `tests/check_enum_snapshots.py` |
| Evaluator/native layout | `tests/check_eval_native_layout.py`, `tests/check_eval_alloc_layout.py` |
| Deferred cleanup | `tests/check_defer_*.py`, `tests/check_eval_defer_unwind.py` |
| Extern compatibility | `tests/check_extern_declarations.py` |
| Alias ranges and loop backedges | `tests/check_alias_layout.py`, `tests/check_alias_backedge.py` |
| Worker publication and dispatch reports | `tests/check_pfor_publication.py`, `tests/pfor_report/` |
| Device capabilities | `tests/gpu/` and checks named by each focused spec |

`tests/run_all.sh` selects suites for the local environment. Toolchain and
hardware skips are not passing runtime evidence. Test commands and prerequisites
are in [BUILD.md](../BUILD.md); contributor requirements are in
[CONTRIBUTING.md](../CONTRIBUTING.md).
