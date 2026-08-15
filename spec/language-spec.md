# Traveler Language Specification

Working revision, tracking release `v0.1.0`. File extension: `.tv`

This document specifies the syntax, type system, semantics, memory model,
and LLVM code generation strategy for a statically-typed, algebraically-aware
systems language built on polynomial computation over finite fields.

### Specification status and conventions

This specification is partly **normative** (it describes what the `v0.1.0`
compiler, `src/tvc_self.tv`, implements) and partly **aspirational** (it
describes intended surface that is designed but not yet built). To keep the two
honest, features are tagged inline:

- Unmarked text describes behavior **implemented** in the `v0.1.0` compiler.
- A blockquote beginning **NOT YET IMPLEMENTED** marks a designed-but-unbuilt
  feature. `**[planned; not implemented in v0.1.0]**` is the inline form for a
  single item.
- A blockquote beginning **Provided by the standard library** marks behavior
  that is realized in `src/lib/**` (built on the compiler's four primitives —
  `forward_sum`, `forward_diff`, `regime_detect`, `eval_at`) rather than as a
  language builtin.

---

## Table of Contents

1. Design Principles
2. Lexical Structure
3. Type System
4. Declarations
5. Expressions
6. Statements
7. Algebraic Structure System
8. Field Arithmetic Semantics
9. Polynomial Semantics
10. Piecewise Polynomial Semantics
11. Stream Semantics
12. Memory Model
13. Module System
14. Error Model
15. LLVM Code Generation
16. Edge Cases and Corner Behaviors
17. Sequential Polynomial Computation
18. Backends and Targets
19. Graphics (`src/lib/gfx/`)
20. Reserved for Future Extension

---

## 1. Design Principles

1. **Algebraic structure is a compilation directive.** The type of a value determines
   how the compiler generates code for it. `Field<251>` add compiles to a different
   instruction sequence than `Field<65521>` add or `BinField<8>` add.

2. **All stack sizes known at compile time.** A `Poly<F, 3>` is always 4 coefficients.
   A `Field<251>` element is always 1 byte. A `*T` pointer is always one machine word.
   No runtime stack size computation. No GC. Heap-allocated data (`alloc`, `Vec<T>`)
   has runtime-determined size, but the pointer to it has compile-time-known size.

3. **Polynomial representation is the natural representation.** A degree-2 polynomial
   over 250 evaluation points is 3 coefficients, not 250 values. Compression, storage,
   and transmission operate on the coefficient form. Evaluation produces values on demand.

4. **The language is its own math.** The compiler uses Newton forward differences
   (Newton forward differences) to analyze and optimize polynomial expressions.
   Self-hosting means the compiler is written in the language it compiles.

5. **Piecewise polynomial for the real world.** Not all data is exactly polynomial.
   The language supports piecewise polynomial functions with automatic segmentation,
   bridging exact algebra and noisy physical measurements.

---

## 2. Lexical Structure

### 2.1 Character Set

Source files are UTF-8 encoded. Only ASCII characters appear in keywords, operators,
and identifiers. Non-ASCII is permitted in string literals and comments.

### 2.2 Whitespace and Line Structure

Whitespace (space, tab, carriage return, newline) separates tokens but is otherwise
insignificant. There is no significant indentation. Statements are terminated by
semicolons or newlines at the same nesting depth (semicolons are optional when
unambiguous; the parser inserts them at newlines where the preceding token is an
identifier, literal, `)`, `]`, or `}`).

### 2.3 Comments

```
// Line comment (extends to end of line)
/* Block comment (may nest) */
```

Block comments nest: `/* outer /* inner */ still comment */` is a single comment.

### 2.4 Keywords

Reserved words recognized by the lexer:

```
field       binfield    extfield    fn          let
mut         const       struct      enum        trait
impl        import      instantiate extern      pub
if          else        for         in          while
return      break       continue    match       as
true        false       null        unsafe      print
defer
```

Reserved type-name keywords (built-in type constructors):

```
u8   u16  u32  u64   i8    i16   i32   i64
i128 u128 i256 u256  usize bool
Field  BinField  ExtField  Poly  Register
```

`_` is the discard pattern (§2.5), not a keyword.

**Not reserved words in v0.1.0.** Several identifiers used elsewhere in this
document are *not* lexer keywords:

- `eval`, `analyze`, `segment` are **builtin function names** resolved by the
  codegen, not reserved words (§5.4–§5.6, §9).
- `self` is recognized contextually in method receivers (§17.9), not reserved
  globally.
- `type`, `module`, `stream`, `piecewise`, `where`, `mod`, `div`, `export`, and
  a lowercase `poly` type keyword appear in designed-but-unbuilt surface and are
  **[planned; not reserved in v0.1.0]** (`#[export]` is the implemented export
  mechanism — §13.3, §15.4). Some reserved words are gated to features that are
  themselves not yet built: `const` is used for const generics (`<const N>`) but
top-level `const` declarations are not parsed (§4.3); `unsafe` is reserved but
will not become a block construct (decided against — §14.3).

### 2.5 Identifiers

```
identifier = letter (letter | digit | '_')*
letter     = 'a'..'z' | 'A'..'Z' | '_'
digit      = '0'..'9'
```

Identifiers beginning with uppercase are type names by convention.
Identifiers beginning with lowercase are value names by convention.
The identifier `_` is the discard pattern (no binding).

### 2.6 Integer Literals

```
decimal     = digit (digit | '_')*
hexadecimal = '0x' (hex_digit | '_')+
binary      = '0b' ('0' | '1' | '_')+
octal       = '0o' (octal_digit | '_')+

hex_digit   = digit | 'a'..'f' | 'A'..'F'
octal_digit = '0'..'7'
```

Underscores in literals are ignored: `1_000_000` = `1000000`.

Integer literals have no inherent type. Their type is inferred from context.
An unresolved integer literal defaults to the enclosing field type if one exists,
or `i64` otherwise.

### 2.7 Field Element Literals

A field element literal is an integer literal used in a field context:

```
let x: Field<251> = 300    // x = 300 mod 251 = 49
let y: Field<251> = -1     // y = 250 (additive inverse of 1)
```

Negative literals are syntactic sugar for additive inverse. The literal `-k` in
`Field<p>` denotes `p - (k mod p)`.

Literals exceeding the field characteristic are reduced: the value stored is
`literal mod p`. This is NOT an error. The reduction is a fundamental property
of field membership.

### 2.8 String Literals

```
string_literal = '"' string_char* '"'
byte_string    = 'b"' string_char* '"'

string_char    = any_byte_except_backslash_or_quote
               | escape_sequence

escape_sequence = '\n' | '\r' | '\t' | '\\' | '\"' | '\0' | '\x' hex_digit hex_digit
```

A string literal lowers to a raw `*u8` pointing to NUL-terminated bytes in the
binary's read-only data section (the `str = &[u8]` slice type is planned — see
§3.14). The escape/byte content is as follows:

```
let greeting: *u8 = "hello"    // bytes: [104, 101, 108, 108, 111, 0]
let empty: *u8 = ""             // bytes: [0]
let escaped: *u8 = "line\n"    // bytes: [108, 105, 110, 101, 10, 0]
let hex: *u8 = "\x00\xFF"      // bytes: [0, 255, 0]
```

Byte string literals (`b"..."`) are identical to regular string literals in
semantics. The `b` prefix is permitted for readability when the content is
non-textual binary data.

String literals have static lifetime: they are valid for the entire program
execution. They point to immutable memory.

### 2.9 Polynomial Literals

```
poly(c0, c1, c2)           // degree 2: c0 + c1*t + c2*t^2
poly<Field<251>>(10, 3, 1) // explicit field annotation
```

Polynomial literals are syntactic sugar for `Poly<F, d>` construction where
`d = number of arguments - 1`.

### 2.10 Operators

Arithmetic:
```
+    addition (field add in field context)
-    subtraction (field sub in field context) / unary negation
*    multiplication (field mul in field context)
/    division (field multiplicative inverse in field context)
%    modular reduction (integer context only; ill-defined for fields)
**   exponentiation (by squaring in field context)
```

Comparison:
```
==   equality
!=   inequality
<    less than (integer representative ordering for field elements)
>    greater than
<=   less or equal
>=   greater or equal
```

**Edge case: ordering on field elements.** Finite fields have no natural total
order. The operators `<`, `>`, `<=`, `>=` compare the canonical integer
representatives in `{0, 1, ..., p-1}`. This ordering has no algebraic meaning
and exists only for practical branching. The compiler emits a warning if
ordering operators are used on field types, suppressible with `#[allow(field_ord)]`.

Bitwise (integer and `BinField` context):
```
&    bitwise AND (= multiplication in GF(2))
|    bitwise OR
^    bitwise XOR (= addition in GF(2^k))
~    bitwise NOT
<<   left shift
>>   right shift
```

Logical:
```
&&   short-circuit AND
||   short-circuit OR
!    logical NOT
```

Assignment:
```
=    binding / assignment
+=   compound add-assign
-=   compound sub-assign
*=   compound mul-assign
/=   compound div-assign
```

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Only plain `=` assignment is
> implemented. The compound-assign operators `+= -= *= /=` are lexed as `+`,
> `-`, `*`, `/` followed by `=` and are not yet parsed as augmented assignment;
> they are a work-in-progress item in the surface-ergonomics pass. Write
> `x = x + y;` for now. See §6.2.

**Associativity.** All binary operators are **left-associative** except
exponentiation `**`, which is **right-associative** (the mathematical
convention): `a ** b ** c` parses as `a ** (b ** c)`. Thus `2 ** 3 ** 2` is
`2 ** 9 = 512`, not `(2 ** 3) ** 2 = 64`. Left-associativity for the rest means
`a - b - c` is `(a - b) - c` and `a / b / c` is `(a / b) / c`.

**Precedence**, from loosest to tightest binding:
`||` < `&&` < comparison (`== != < > <= >=`) < `|` < `^` < `&` < shift (`<< >>`)
< `+ -` < `* / %` < `**` < unary (`- ! ~ &` address-of, `as` cast) < postfix
(call `()`, index `[]`, member `.`). Parentheses override precedence.

### 2.11 Punctuation

```
(  )    grouping, function call, tuple
[  ]    array indexing, array literal
{  }    block, struct literal
:       type annotation
;       statement terminator (optional)
,       separator
.       member access
..      range (exclusive end)
..=     range (inclusive end)          [planned; not implemented in v0.1.0]
->      function return type
=>      match arm
::      path separator (Type::method, Type::CONST)
?       early-return propagation (Result-shaped operand, §5.10)
```

`..=` is reserved punctuation but inclusive ranges are not yet parsed (§5.8).
`::` resolves associated methods/constants on a type; there is no module-path
`::` (the module system is source inclusion, §13.2).

---

## 3. Type System

### 3.1 Primitive Types

```
bool        1-bit boolean (true / false)
u8          unsigned 8-bit integer
u16         unsigned 16-bit integer
u32         unsigned 32-bit integer
u64         unsigned 64-bit integer
i8          signed 8-bit integer
i16         signed 16-bit integer
i32         signed 32-bit integer
i64         signed 64-bit integer
usize       pointer-sized unsigned integer
i128 u128   signed / unsigned 128-bit integer
i256 u256   signed / unsigned 256-bit integer
```

`bool` is a distinct type, NOT an alias for `Field<2>`. Explicit conversion
is available: `Field<2>::from(b)` and `bool::from(f)`. The compiler MAY
optimize `Field<2>` operations using boolean logic internally.

**Wide integers (`i128`, `u128`, `i256`, `u256`).** These are first-class
surface types lowered to LLVM `i128`/`i256`. They support the full arithmetic,
bitwise, comparison, and cast surface (`+ - * << >> & | ^`, `== != < > <= >=`,
`as`), literals larger than 2^64 (decimal and hex), and decimal printing (via
an internal double-dabble helper). **Division and modulo (`/`, `%`) are
deliberately refused** on wide integers — a `urem`/`udiv` at these widths would
emit a `__udivti3`/`__udivei4`-class libcall, which the C-free trust chain does
not admit. The value carriers used by the wide-field path (§16.16, §17.10) are
built on these types.

### 3.2 Field Types

#### 3.2.1 Prime Fields

```
field F = Field<p>
```

where `p` is a compile-time-known prime literal. The compiler verifies primality
at compile time using a deterministic Miller-Rabin test (sufficient for p < 2^64).

**Representation**: the smallest unsigned integer type that holds `p - 1`:

| Prime range | Element type | Intermediate type | Example |
|---|---|---|---|
| p <= 256 | u8 | u16 | Field<251> |
| 256 < p <= 65536 | u16 | u32 | Field<65521> |
| 65536 < p <= 2^32 | u32 | u64 | Field<4294967291> |
| 2^32 < p <= 2^64 | u64 | u128 | Field<2^61 - 1> |

The intermediate type is used for multiplication: `(a * b) mod p` requires
an intermediate product of width `2 * element_width`.

**Compile-time errors**:
- `Field<0>` — error: 0 is not prime
- `Field<1>` — error: 1 is not prime
- `Field<4>` — error: 4 is not prime (4 = 2^2)
- `Field<p>` where p > 2^64 — error: prime exceeds maximum supported width
  (future extension: multi-limb arithmetic)

**Edge case: Field<2>.**
`Field<2>` is the prime field GF(2) = {0, 1}. Addition is XOR. Multiplication
is AND. This is structurally identical to boolean logic but is a distinct type
from `bool`. The compiler SHOULD optimize `Field<2>` arithmetic to bitwise
operations.

#### 3.2.2 Binary Extension Fields

```
binfield G = BinField<k, poly>
```

where `k` is the extension degree and `poly` is the reducing polynomial
(specified as an integer whose binary representation gives the coefficients).

Example: `BinField<8, 0x11B>` is GF(2^8) with irreducible polynomial
x^8 + x^4 + x^3 + x + 1 (the AES polynomial).

**Representation**: a single machine word (`u8` for k <= 8, `u16` for
k <= 16, `u32` for k <= 32, `u64` for k <= 63). The extension degree `k` is
restricted to the **one-word model**, `2 <= k <= 63`, so that a carry-less
product (up to `2k - 1 < 127` bits before reduction) is handled without
multi-word arithmetic.

**Arithmetic**:
- Addition: XOR (bitwise, no carry).
- Multiplication: two paths. The legacy `GF(2^8, 0x11B)` field (the AES
  polynomial) keeps a precomputed **log/exp table** runtime. Every other
  `BinField<k, poly>` uses a **generic carry-less shift-and-XOR** runtime
  (`@bf<k>_<poly>_mul`): accumulate shifted copies of one operand, then reduce
  modulo the irreducible polynomial. (No PCLMULQDQ intrinsic is emitted; the
  shift-and-XOR form is portable and C-free.)
- Inverse: Fermat's little theorem, `a^(2^k - 2) = a^(-1)`, via the field's
  `pow`.

**Compile-time errors**:
- `BinField<k, poly>` where `k < 2` or `k > 63` — error: degree must lie in the
  one-word range `[2, 63]`.
- `BinField<k, poly>` where `poly` is reducible — error: polynomial must be
  irreducible over GF(2). The compiler verifies irreducibility at compile time
  using Rabin's algorithm (the exact GF(2) mirror of the Miller-Rabin primality
  gate on `Field<p>`).
- `BinField<k, poly>` where `degree(poly) != k` — error: polynomial degree
  must equal the extension degree.

#### 3.2.3 Quadratic Extension Fields

```
extfield E = ExtField<Field<p>, 2>
```

where `Field<p>` is a previously declared prime field. Constructs the quadratic
extension GF(p^2) = {a + b*i : a, b in GF(p), i^2 = nr} where `nr` is the
smallest quadratic non-residue modulo p (computed at compile time).

**Representation**: `{elem, elem}` where `elem` is the base field's element type.
For example, `ExtField<Field<251>, 2>` has IR type `{i8, i8}`;
`ExtField<Field<18446744069414584321>, 2>` has IR type `{i64, i64}`.

**Arithmetic** (all operations decompose into base field operations):
- Addition: `(a+bi) + (c+di) = (a+c, b+d)` — 2 base adds
- Subtraction: `(a+bi) - (c+di) = (a-c, b-d)` — 2 base subs
- Multiplication: `(a+bi)(c+di) = (ac + bd*nr, ad + bc)` — 4 base muls + 2 base adds
- Inverse: conjugate formula — `inv(a+bi) = (a, -b) / (a^2 - b^2*nr)` — 6-7 base ops
- Division: `a / b = a * inv(b)`
- Power: repeated squaring using extension multiplication

**Non-residue selection**: The compiler scans `nr = 2, 3, 5, ...` until
`nr^((p-1)/2) = p-1 mod p` (Legendre symbol equals -1). This is the Euler
criterion for quadratic non-residues. For p=251, nr=2. For the Goldilocks prime
(2^64 - 2^32 + 1), nr=7.

**Constants**:
- `E::ZERO` — additive identity: `(0, 0)`
- `E::ONE` — multiplicative identity: `(1, 0)`
- `E::IMAG` — imaginary unit: `(0, 1)` where `i^2 = nr`
- `E::NR` — the non-residue value (returned as u64)

**Integer literal promotion**: `let x: E = 5;` produces `(5, 0)`. To construct
elements with non-zero imaginary parts, use `E::IMAG`:
```
let i: E = E::IMAG;
let z: E = 3 + 7 * i;  // z = (3, 7) representing 3 + 7i
```

**Generic compatibility**: `ExtField<F, 2>` satisfies the `Field` trait bound.
Generic functions `fn<F: Field>` monomorphize correctly for extension fields,
with the compiler routing arithmetic through extension field functions.

**Purpose**: Extension fields provide the sample space for FRI commitment
challenges. The base field GF(p) has ~2^64 elements, giving collision probability
~2^-64 per challenge. The extension GF(p^2) has ~2^128 elements, achieving
128-bit security (Schwartz-Zippel lemma).

#### 3.2.4 Field Properties (Compile-Time Queryable)

Every field type `F` exposes compile-time constants:

```
F::PRIME         // the characteristic (p for Field<p>, 2 for BinField)
F::ORDER         // number of elements (p for Field<p>, 2^k for BinField<k>)
F::ELEMENT_BITS  // bits per element (ceil(log2(ORDER)))
F::ELEMENT_SIZE  // bytes per element (ceil(ELEMENT_BITS / 8))
F::ZERO          // additive identity
F::ONE           // multiplicative identity
F::NTT_MAX_LOG   // largest k such that 2^k divides ORDER-1 (0 if no NTT support)
F::IS_NTT_FRIENDLY  // bool: NTT_MAX_LOG >= 10 (practical threshold)
```

### 3.3 Polynomial Types

```
Poly<F, d>
```

A polynomial of degree exactly `d` over field `F`. Stored as `d + 1` coefficients
in ascending degree order: `coeffs[i]` is the coefficient of `t^i`.

**Size**: `(d + 1) * F::ELEMENT_SIZE` bytes. Always known at compile time.

**Edge cases**:
- `Poly<F, 0>` is a constant. Equivalent to a single field element. One coefficient.
- Degree must be a compile-time constant for static `Poly`. For runtime-determined
  degree, see `DynPoly` (3.3.2).
- `Poly<F, d>` where `d >= F::ORDER` — warning: polynomial of degree >= field order
  has redundant coefficients (by Fermat's little theorem, x^p = x in GF(p), so
  any polynomial can be reduced to degree < p). The compiler does NOT automatically
  reduce, but emits a warning.

#### 3.3.1 Polynomial Construction

The `poly(...)` builtin constructs a standard-representation polynomial; its
degree and field come from the binding's type annotation (there is no explicit
`poly<F>` turbofish form):

```
let p: Poly<F, 2> = poly(c0, c1, c2)   // 3 coefficients -> Poly<F, 2>
```

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** `Poly<F, d>::from_coeffs(arr)`
> and `Poly<F, d>::zero()` are not implemented. Construct from an explicit
> coefficient list with `poly(...)`, or from data with `analyze(...)` (§5.5,
> §9.3).

#### 3.3.2 Dynamic Polynomials

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** `DynPoly<F>` (a runtime-degree
> polynomial), `analyze()`-returning-`DynPoly`, `dp.degree()`, and
> `into_static()` are not implemented. In `v0.1.0`, `analyze(data, npts)`
> constructs a `Poly<F, d>` with a compile-time degree `d` supplied by the
> binding (§5.5, §9.3); a data-dependent degree is a designed extension. The
> intended surface was:
>
> ```
> DynPoly<F>                        // header (degree: u16) + coefficients
> let dp: DynPoly<F> = analyze(data)
> match dp.degree() { 2 => { let p: Poly<F, 2> = dp.into_static(); ... }, _ => ... }
> ```

### 3.4 Piecewise Polynomial Types

> **NOT YET IMPLEMENTED (v0.1.0 — planned) as a language type.** There is no
> compiler-known `Piecewise<F, d>` or `Segment<F, d>` type. Piecewise
> segmentation is **provided by the standard library** (`src/lib/codec/`, see
> §10), which composes the four compiler primitives over plain arrays. The
> type described below is the intended first-class surface.

```
Piecewise<F, d>
```

A piecewise polynomial function: a sequence of segments, each a polynomial of
degree at most `d` over field `F`, with explicit breakpoints.

**Representation**:
```
struct Piecewise<F, d> {
    segments: [Segment<F, d>],  // dynamically sized
    count:    usize,
}

struct Segment<F, d> {
    start:  usize,              // first index in this segment
    len:    usize,              // number of values in this segment
    coeffs: [F; d + 1],        // polynomial coefficients
}
```

**Invariants** (enforced at construction, checked at runtime in debug mode):
- Segments are non-overlapping and cover the full range contiguously.
- `segments[i].start + segments[i].len == segments[i+1].start` for all i.
- Segment lengths are >= 1.

**Edge case**: A `Piecewise<F, d>` with 1 segment is equivalent to a `Poly<F, d>`.
Implicit conversion is available.

**Lossless constraint**: When constructed with `max_error: 0`, every segment is an
exact polynomial fit — no approximation. The segmenter finds the longest run from
each starting point where Newton forward differences converge to degree <= `d`, then
starts a new segment at the first point of divergence. This is lossless.

**Lossy mode** (future): When `max_error > 0`, the segmenter finds the longest run
where the polynomial approximation error is within the bound. Not specified in this
revision.

### 3.5 Stream Types

> **NOT YET IMPLEMENTED (v0.1.0 — planned) as a language type.** There is no
> compiler-known `Stream<F>` type. The continuation state machine it describes
> is **provided by the standard library** (the codec / `src/lib/util/stream_protocol.tv`,
> see §11), built on `Register` and the four primitives. The type below is the
> intended first-class surface.

```
Stream<F>
```

A lazy, potentially unbounded sequence of field elements, backed by a polynomial
evaluation state.

**Representation**:
```
struct Stream<F> {
    reg:    [F; MAX_DEGREE + 1],  // forward summation registers
    degree: u16,
    active: bool,                 // true if polynomial continuation is possible
}
```

`MAX_DEGREE` is a compile-time configuration constant (default: 250 for
`Field` types with p <= 256, 2048 for larger fields).

**Operations**:
- `Stream::from_poly(p: Poly<F, d>)` — initialize from polynomial
- `stream.next() -> F` — produce next value, advance registers
- `stream.next_n(n) -> [F; n]` — produce n values
- `stream.reset(p: Poly<F, d>)` — reset to new polynomial
- `stream.continues(data: &[F]) -> bool` — check if data matches continuation

### 3.6 Array Types

```
[F; n]           // fixed-size array of n elements of type F  (IMPLEMENTED)
[F]              // slice (borrowed, runtime-known length)     (planned)
```

Fixed-size arrays have compile-time-known length. Stack allocated. The length
`n` may be a const-generic parameter (`[T; N]` with `<const N>`, §3.8).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Slice types (`[F]`, `&[T]`),
> sub-slicing (`s[a..b]`), and the fat-pointer `{ ptr, i64 }` representation are
> not implemented. Pass a raw pointer plus an explicit length (`*T` + `len: i32`)
> across boundaries; this is the idiom the standard library and compiler use
> internally. The slice surface below is the intended design.

Slices are fat pointers: (pointer, length). Borrowed from an owning array
or heap allocation. The slice does not own the data it points to.

**Slice operations** (planned):

| Operation | Syntax | Semantics |
|---|---|---|
| Length | `s.len() -> usize` | Number of elements |
| Index read | `s[i]` | Bounds-checked. Traps if i >= len. |
| Index write | `s[i] = val` | Bounds-checked. Only on `&mut [T]`. |
| Sub-slice | `s[a..b] -> &[T]` | Elements a through b-1. Bounds-checked. |
| Equality | `s1 == s2 -> bool` | Element-wise comparison. Lengths must match. |
| Copy to pointer | `s.copy_to(dst: *T)` | Copies s.len() elements to dst. Unsafe: no bounds check on dst. |
| Borrow from array | `&arr -> &[T; n]` | Fixed-size reference |
| Borrow as slice | `&arr as &[T]` | Coerce fixed-size ref to slice |

**LLVM IR**: `&[T]` compiles to `{ ptr, i64 }` (data pointer + length).

### 3.7 Tuple Types

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Tuple types `(A, B, C)` and the
> unit type `()` are not parsed. Use a `struct` for heterogeneous aggregates,
> and a function with no return type for "unit".

```
(A, B, C)        // heterogeneous fixed-size tuple
()               // unit type (zero-size)
```

### 3.8 Struct Types

```
struct SensorReading<F: Field> {
    timestamp: u64,
    value:     F,
    channel:   u8,
}
```

Structs have a defined memory layout (see section 12). Fields are laid out in
declaration order with alignment padding as specified by the target ABI.

**Generic structs.** A struct may be parameterized over type parameters
(`struct Vec<T> { data: *T, len: i32, cap: i32 }`) and **const generics**
(`struct Buf<const N> { data: [u8; N] }`, where `N` sizes a fixed array). Both
are **monomorphized per instantiation** — each concrete `Vec<i32>`, `Vec<Field<p>>`,
`Buf<64>` produces its own laid-out type and its own copy of any generic
function operating on it. There is no runtime generic dispatch. `instantiate
Ty<Concrete>;` emits a named concrete instance for separate compilation
(§13.2). The standard library's `Vec<T>` (§3.13) is an ordinary generic struct
defined this way, not a compiler builtin.

### 3.9 Enum Types

```
enum Compressed<F: Field> {
    Poly(Poly<F, _>),        // _ means degree is part of the variant data
    Literal([F]),
    Const(F),
    Continue(usize),
}
```

Enums are tagged unions. The tag is the smallest integer type that can hold
the variant count. Layout: tag followed by the largest variant's data, padded
to alignment. (The `Compressed` example above is illustrative: the `Poly<F, _>`
wildcard degree and the `Literal([F])` slice variant use surface that is itself
**[planned; not implemented in v0.1.0]** — §3.3.2, §3.6.)

**Generic enums.** Enums may be generic and are **monomorphized per
instantiation**, exactly like generic structs (§3.8). `Option<T>` and
`Result<T, E>` are ordinary generic enums (not compiler builtins); a user may
declare their own. `match` (§6.7) discriminates the variants and binds their
payloads, and the `?` operator (§5.10) propagates `Err` early. There is no runtime type erasure — `Option<i32>` and `Option<Field<p>>`
are distinct laid-out types.

### 3.10 Function Types

```
fn(A, B) -> C
```

Functions are not closures. They capture no environment. Function pointers are
plain code pointers (same as C function pointers).

#### 3.10.1 Closures

A closure literal `|params| expr` (or `|params| { ... }`) is an anonymous
function paired with an environment of values captured **by value** from the
enclosing scope. Traveler closures are deliberately constrained to the
thesis-preserving subset:

- **Monomorphized.** A closure lowers to a compiler-lifted top-level function
  `@__closure_N(ptr %__env, <params>)` plus a stack-allocated capture struct.
  Its concrete identity is statically known at every call site (the value's
  type is `closure#N`, naming exactly one lifted function) — it is a value like
  a monomorphized generic, **not** a `dyn Fn`. A call through a closure
  variable is a *direct* call to the lifted function.
- **Composition is generic.** Closures have no nameable type, so a
  higher-order function takes them through a generic parameter:
  `fn apply<C>(c: C, v: i32) -> i32 { return c(v); }`, monomorphized per
  closure identity. There is no uniform `closure` type to pass — that would be
  erasure.
- **Stack-only, non-escaping.** Captured values live on the stack; a closure
  may be `let`-bound in its scope, called directly, and passed *down* as a
  generic call argument, but it may **not** escape (be returned, stored in an
  escaping aggregate, reassigned to a different identity, or recurse on
  itself). Escape is a **compile error**, never a silent heap box.
- **Auto-parallelization (prove-through).** A loop calling a closure whose
  identity is statically known and whose body is provably pure parallelizes —
  the compiler can prove what code runs (Section 15.18). A loop calling a
  *function pointer* stays sequential (the target is a runtime value). A
  closure parallelizes precisely when its identity is provable.
- **The boundary IS the thesis.** Every construct that would force type erasure
  — returning a closure, storing one in a struct field, reassigning a closure
  variable to a different identity (or a heterogeneous collection, which is the
  same move), passing a capturing closure where a `fn(...)` pointer is
  expected, recursive self-capture — is **rejected**, not boxed. Despite
  capturing an environment, no call target is ever erased. This is the same
  line `dyn` (Section 17.10) draws: a closure defers a *value* (its
  environment), never the *structure* (its call target), just as `dyn` field
  defers the modulus, never the field axioms. `dyn Fn` / closure trait objects
  remain unspecified and unplanned (§20.1).

The set of rejected constructs (the negative catalogue) is the precise
definition of the line; see `@internal-note: plan-b2-closures-rubicon` and
`@internal-note: memo-2026-06-19-b2-closures`.

### 3.11 Type Aliases

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** The `type Name = ...` declaration
> is not parsed (`type` is not a reserved word — §2.4). Field-alias unification
> (`type N = <T>;`) is a scoped item in the surface-ergonomics pass; general
> aliasing of structs/primitives is a separate follow-on. Write the underlying
> type directly for now. The intended surface was:

```
type Byte = Field<251>
type Sample = Field<65521>
type Timestamp = Field<4294967291>

type SensorPoly = Poly<Sample, 2>
```

### 3.12 Pointer Types

```
*T               // raw pointer to a value of type T   (also **T, ***T, ...)
```

> **Implemented as specified below.** `*T` (and nested `**T`) are implemented
> and mutable-by-default. The `*mut T` / `*const T` distinction is
> **[planned; not implemented in v0.1.0]** — there is one raw-pointer form.
> There is **no `unsafe` construct** in Traveler and none is planned: the
> unsafe surface is exactly two named doors (the integer-to-pointer cast and
> `extern "C"`), documented in the operations table. Element access uses
> indexing — `p[i]` and `p[i] = v`; there is no `offset` method and no
> pointer arithmetic (see the operations table).

A raw pointer is an address-width integer that points to a value in memory.
All pointer types have compile-time-known size: one machine word (`usize`).
This preserves the stack-size-known-at-compile-time principle while enabling
heap-allocated and recursive data structures.

**Size**: `sizeof(*T) == sizeof(usize)` on all platforms.

**Null**: The literal `null` has type `*T` for any T. Dereferencing null is
undefined behavior.

```
let p: *ASTNode = null
if p != null {
    let node: ASTNode = p[0]
}
```

**Operations.** A pointer can be FORMED in exactly three ways — `alloc`
(§12.3), address-of (`&x`, `&a[i]`, `&p[i]`), and the integer-to-pointer
cast — and CONSUMED only by indexing. There is **no pointer arithmetic**
(`ptr + int` is refused; advance a position by indexing `p[i]`, or by
re-taking an element address `&p[i]`):

| Operation | Syntax | Class | Semantics |
|---|---|---|---|
| Element read | `p[i]` | checked-size | Read element i (i32 index model, §3.13) |
| Element write | `p[i] = v` | checked-size | Write element i |
| Element address | `&p[i]`, `&a[i]` | safe formation | Address of element i |
| Address-of | `&x` | safe formation | Address of a mutable binding |
| Null check | `p == null`, `p != null` | safe | Compare to null |
| Pointer equality | `p1 == p2` | safe | Compare addresses |
| Pointer to integer | `p as i64` / `(&x) as i64` | safe observation | The address as an integer (`ptrtoint`) |
| Integer to pointer | `n as *T` | **the unsafe door** | Forge a pointer (`inttoptr`). The FFI/membrane floor: mmap'd regions, device registers. No provenance; all safety obligations are the programmer's. |
| Reinterpret | `p as *U` | **the unsafe door** | Retype a pointer; element size follows `*U` |

"Checked-size" means the ELEMENT SIZE is compiler-derived (from the pointee
type) and can never silently disagree with the allocation's sizing (§12.3);
bounds and lifetime are NOT checked (see Ownership discipline).

**Recursive types** are now possible:

```
enum Expr {
    Lit(i64),
    Add(*Expr, *Expr),
    Mul(*Expr, *Expr),
    Ident(str),
}
```

The size of `Expr` is known at compile time: tag (1 byte) + max variant
payload (`*Expr` + `*Expr` = 2 words). The pointed-to `Expr` values live
on the heap, allocated via `alloc`.

**Ownership discipline**: Raw pointers have no automatic lifetime management
— no destructors, no GC, no borrow checker, by design (memory management is
never hidden in control flow). The programmer is responsible for `free`-ing
heap-allocated pointees; the compiler does not track pointer aliasing,
lifetimes, or use-after-free. What the compiler DOES enforce is the spatial
side: allocation sizing is destination-typed and refusal-backed (§12.3),
pointer arithmetic does not exist, and `alloc` zero-fills. Temporal safety
is structural: stack discipline, wholesale-freed arenas, and index handles
over held pointers for anything that grows (a `realloc` invalidates every
derived pointer; handles survive it). Structured lifetime surface: a `defer`
statement (§6.8) runs scope-exit cleanup on every exit path, including `?`
early returns, and the library ships arena/pool types (§12.3, §13.4).

**LLVM IR**: `*T` maps to `ptr` (LLVM's opaque pointer type).

### 3.13 Vec Type

> **Provided by the standard library** (`src/lib/collections/vec.tv`), not a
> language builtin. `Vec<T>` is an ordinary generic struct (§3.8) built on
> `*T` + `alloc`/`realloc`/`free`; a program `import`s it. It is `tvc_self`-only
> (the frozen C seed has no generic-struct support). The description below
> matches the shipped module.

```
Vec<T>           // growable heap-allocated array
```

`Vec<T>` is a concrete struct. Its layout is:

```
struct Vec<T> {
    data: *T,        // pointer to heap-allocated element array
    len:  i32,       // number of live elements
    cap:  i32,       // allocated capacity (in elements)
}
```

**Size**: `sizeof(Vec<T>) == sizeof(*T) + 8` (pointer + two `i32`).
Compile-time known. Stack-allocated. The elements are heap-allocated.

**API.** The canonical, globally-unique surface is a set of free functions;
thin forwarding methods give `v.push(x)` ergonomics (`v.push(x)` desugars to
`push(&v, x)`):

| Operation | Free function | Method | Semantics |
|---|---|---|---|
| Create | `vec_new<T>() -> Vec<T>` | — | len=0, cap=4, data=alloc(4) |
| Push | `vec_push(&v, val)` | `v.push(val)` | Append. If len==cap, cap*=2 then realloc. |
| Pop | `vec_pop(&v) -> T` | — | Remove and return last (no emptiness check). |
| Length | `vec_len(&v) -> i32` | `v.len()` | Number of live elements |
| Get | `vec_get(&v, i) -> T` | `v.get(i)` | Element `i` (no bounds check). |
| Set | `vec_set(&v, i, val)` | — | Write element `i` (no bounds check). |
| Free | `vec_free(&v)` | — | Deallocate backing memory. Vec invalid after. |

Access is **not** bounds-checked — `vec_get`/`vec_set` index `data` directly.
`instantiate vec_new<i32>;` (and friends) emit concrete mangled instances for
cross-file linking (§13.2).

> **NOT YET IMPLEMENTED (planned).** `with_cap`, `as_slice`, `as_ptr`, and
> `clear` are not provided (the last two await the slice type, §3.6).

**Growth strategy**: `vec_new` allocates 4 elements; `push` doubles `cap` and
`realloc`s when `len == cap`. Amortized O(1) push.

**LLVM IR**: `Vec<T>` compiles to `{ ptr, i32, i32 }`.

### 3.14 String Types

> **Partly implemented.** A **string literal** is a real, implemented value —
> but it lowers to a **raw `ptr`** into static, NUL-terminated read-only data,
> **not** a `{ ptr, i64 }` fat pointer, and it carries no length with it. The
> `str = &[u8]` borrowed-slice type below is **[planned; not implemented in
> v0.1.0]** (it awaits the slice type, §3.6). For owned, length-carrying,
> mutable strings the standard library provides `Str`
> (`src/lib/collections/string.tv`).

Traveler has no character type. Strings are byte sequences. The compiler
operates on ASCII; non-ASCII bytes are preserved but not interpreted.

**String literals** lower to a pointer to static data:

```
let name: *u8 = "hello"       // ptr to static bytes "hello\0"
let empty: *u8 = ""            // ptr to "\0"
```

String literals are stored in the binary's read-only data section and live for
the entire program. The intended (planned) borrowed-slice surface was:

```
type str = &[u8]              // planned — see §3.6
let name: str = "hello"       // planned — length-carrying literal
```

**Byte string literals** produce a `*u8` to explicit byte values:

```
let raw: *u8 = b"\x00\xFF\n"  // ptr to 3 bytes: 0x00, 0xFF, 0x0A
```

**Escape sequences** in string literals:

| Escape | Byte value |
|---|---|
| `\n` | 0x0A (newline) |
| `\r` | 0x0D (carriage return) |
| `\t` | 0x09 (tab) |
| `\\` | 0x5C (backslash) |
| `\"` | 0x22 (double quote) |
| `\0` | 0x00 (null) |
| `\xHH` | Arbitrary byte value (hex) |

**Operations.** On a raw `*u8` literal, byte access is direct indexing (`s[i]`,
no bounds check) and there is no carried length. The slice-based `s.len()` /
`s[a..b]` / `s1 == s2` surface is **[planned; not implemented]** (§3.6). For
length-aware operations use the standard library `Str`:

| Operation | `Str` API (`src/lib/collections/string.tv`) |
|---|---|
| Create | `str_new() -> Str`  (`{data:*u8, len:i32, cap:i32}`, cap 8) |
| Push byte | `str_push(&s, c)`  (realloc-doubling) |
| Concatenate | `str_concat(&a, &b)`  (appends `b.len` bytes, NUL-safe) |
| Length | `str_len(&s) -> i32` |
| Equality | `str_eq(&a, &b) -> i32`  (length-first, then bytes) |

**Building strings at runtime**: accumulate into a `Str`:

```
let mut buf: Str = str_new();
str_push(&buf, 72);   // 'H'
str_push(&buf, 105);  // 'i'
// buf.data / buf.len now hold "Hi"
```

**LLVM IR**: a string literal compiles to a global constant byte array
(NUL-terminated) referenced by a raw `ptr` (`getelementptr`). There is no
fat-pointer string type in `v0.1.0`.

---

## 4. Declarations

### 4.1 Field Declarations

```
field F = Field<251>
field G = BinField<8, 0x11B>
```

A field declaration introduces a named field type into scope. Field declarations
are only permitted at module scope.

### 4.2 Function Declarations

```
fn name<T: Trait>(param: Type, ...) -> ReturnType {
    body
}
```

Functions may be generic over types constrained by algebraic traits.
All type parameters must be fully resolved at compile time (monomorphization).

```
fn add_fields<F: Field>(a: F, b: F) -> F {
    a + b
}
```

Compiles to a separate native function for each instantiation:
`add_fields::<Field<251>>`, `add_fields::<Field<65521>>`, etc.

#### 4.2.1 Explicit Instantiation

Generic functions may be explicitly instantiated with the `instantiate` directive
to produce externally-linkable monomorphized functions:

```
instantiate ntt_forward<Field<18446744069414584321>>;
```

This emits a concrete function `ntt_forward_Field18446744069414584321` with
`dso_local` linkage, callable from other compilation units via extern
declarations using the mangled name. The `>>` token at the end of nested
generics (e.g., `Field<P>>`) is automatically split by the parser.

Without explicit instantiation, monomorphized functions receive `internal`
linkage and are only visible within the compilation unit. Explicit
instantiation is the mechanism for multi-file generic module architecture.

### 4.3 Constant Declarations

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** `const` is a reserved word (used
> for const generics, `<const N>`, §3.8), but **top-level `const` declarations
> are not parsed**, and there is no compile-time constant-evaluation engine
> (`355 / 113` field division at compile time is not available). Use a `let`
> binding for now. The intended surface was:

```
const MAX_DEGREE: usize = 250
const PI_APPROX: Field<65521> = 355 / 113  // field division at compile time
```

Constants are evaluated at compile time. All operands must be compile-time-known.
Field arithmetic in constant expressions follows the same modular rules as runtime.

### 4.4 Let Bindings

```
let x: F = 42              // immutable binding with type annotation
let x = 42                 // type inferred from context
let mut y: F = 0           // mutable binding
```

`let` bindings are immutable by default. `mut` permits reassignment.

### 4.5 Stream Declarations

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** `stream` is not a reserved word
> or a construct (§2.4, §3.5). Streaming continuation is a standard-library
> concern (§11). The intended sugar was:

```
stream sensor: Stream<Field<65521>> = poly(1000, 5, 1)
```

Syntactic sugar for creating a stream backed by a polynomial.

---

## 5. Expressions

### 5.1 Arithmetic Expressions

All arithmetic operators dispatch based on the type of the operands:

| Operator | Integer context | Field context | BinField context |
|---|---|---|---|
| `a + b` | Integer addition | `(a + b) mod p` | `a XOR b` |
| `a - b` | Integer subtraction | `(a + p - b) mod p` | `a XOR b` (same as add) |
| `a * b` | Integer multiplication | `(a * b) mod p` | carry-less multiply mod poly |
| `a / b` | Integer division (truncating) | `a * b^(p-2) mod p` | field inverse then multiply |
| `a ** n` | Integer power | modular exponentiation by squaring | field exponentiation |
| `a % b` | Integer modulo | **compile error** (ill-defined for fields) | **compile error** |

**Edge case: `%` on field types.** The modulo operator is not defined on field
elements because field elements are already reduced. Using `%` on a field type
is a compile-time error with the message:
```
error: modulo operator `%` is not defined for field types.
       Field elements are already reduced modulo the characteristic.
       If you need integer modulo, cast to an integer type first: `x as u32 % n`
```

### 5.2 Division by Zero

In a field, every nonzero element has a multiplicative inverse. Division by zero
(`a / 0`) is undefined.

**Behavior**:
- If the compiler can prove the divisor is zero at compile time: **compile error**.
- At runtime: **trap** (program aborts with a diagnostic message and stack trace).

**Rationale**: Silently returning 0 (as some implementations do) masks bugs.
Trapping is the safe default. (An earlier revision offered `unsafe { a / b }`
to elide the check; there is no `unsafe` construct — §14.3 — so if the trap
lands, elision will be a compiler flag or a proven-nonzero path, not a
block form.)

> **NOT YET IMPLEMENTED (v0.1.0).** The runtime zero-divisor trap is not
> implemented. Division lowers to `a * inv(b)`
> with `inv(x) = pow(x, p-2)`, so `a / 0` currently computes `a * pow(0, p-2)`
> = `0` **silently**, with no trap (§16.11). Compile-time-provable-zero
> divisors are likewise not yet diagnosed.

### 5.3 Polynomial Expressions

```
let p: Poly<F, 2> = poly(1, 2, 3)   // 1 + 2t + 3t^2
let q: Poly<F, 1> = poly(4, 5)      // 4 + 5t

let sum = p + q               // Poly<F, 2>: (5, 7, 3)   -- IMPLEMENTED
let eval_at_5 = eval(p, 5)   // F: evaluate p(5)         -- IMPLEMENTED (scalar)
```

> **Partly implemented.** Of the polynomial *operators*, only repr-aware
> addition (`p + q`) and scalar evaluation (`eval(p, x)`, §5.4) are compiler
> builtins. Polynomial multiplication (`p * q`), composition (`p.compose(q)`),
> and differentiation (`p.derivative()`) are **not** language builtins — they
> are **provided by the standard library** (`src/lib/nt/polyfield.tv`:
> `pf_mul`/`pf_deriv`/…, §9.4). The degree-propagation and multiplication-strategy
> tables below describe the intended language-level operator surface.

**Degree propagation rules** (compile-time):

| Expression | Result degree |
|---|---|
| `Poly<F,a> + Poly<F,b>` | `max(a, b)` |
| `Poly<F,a> - Poly<F,b>` | `max(a, b)` |
| `Poly<F,a> * Poly<F,b>` | `a + b` |
| `Poly<F,a> * F` | `a` (scalar multiply) |
| `Poly<F,a>.compose(Poly<F,b>)` | `a * b` |
| `Poly<F,a>.derivative()` | `a - 1` (0 if a == 0) |
| `Poly<F,a>.eval(F)` | `F` (scalar, not a polynomial) |

**Multiplication strategy selection** (compile-time):
- If `a + b <= 32`: schoolbook O(n^2)
- If `32 < a + b <= 64` or `F::IS_NTT_FRIENDLY == false`: Karatsuba O(n^1.585)
- If `a + b > 64` and `F::IS_NTT_FRIENDLY`: NTT-based O(n log n)
  The compiler selects the NTT root of unity from `F`'s multiplicative group.

### 5.4 Evaluation Expression

```
let v: F = eval(polynomial, x)    // evaluate the polynomial at a single point x
```

`eval(poly, point)` evaluates a polynomial at one point and returns a scalar
field element. It is repr-aware: on a NEWTON-form `Poly` it walks the forward
registers directly; on a STANDARD-form `Poly` the compiler auto-inserts a
standard-to-Newton conversion first (§9.1, §9.2).

**Complexity**: O(d) per evaluation, d = polynomial degree.

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** The range/vector form
> `eval(poly, 0..n) -> [F; n]` (decode a run of consecutive points via forward
> summation) is not a builtin. Loop over `eval(poly, i)`, or use the
> forward-summation register directly (`Register`, §17.2), which *is* the
> decode primitive:
>
> ```
> // planned vector form:
> let values: [F; 256] = eval(polynomial, 0..256)
> ```

### 5.5 Analysis Expression

```
let result: Poly<F, d> = analyze(data, npts)
```

`analyze(data, npts)` computes Newton forward differences over the first `npts`
input values and constructs a **NEWTON-representation `Poly<F, d>`** whose degree
`d` is taken from the binding's type annotation (the compiler emits `d+1` levels
of differencing). The repr tag is NEWTON, so a subsequent `eval` needs no
conversion (§9.1, §9.3).

**Complexity**: O(npts * d).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Data-dependent *degree detection*
> — returning `Option<DynPoly<F>>` / `None` when no polynomial structure is
> found within `max_degree` levels, with the early-termination heuristics
> (all-zero level, constant-difference level, nonzero-count thresholds) — is not
> implemented. In `v0.1.0` the caller fixes `d` via the annotation and `analyze`
> extracts exactly that degree. The intended detecting form was:
>
> ```
> let result: Option<DynPoly<F>> = analyze(data, max_degree)  // planned
> ```

### 5.6 Segmentation Expression

> **NOT YET IMPLEMENTED (v0.1.0 — planned) as an expression.** There is no
> `segment()` builtin and no `Piecewise` type (§3.4). Segmentation is
> **provided by the standard library** (`src/lib/codec/`, §10), which uses
> cost-optimized strategies (DP / greedy / particle scan) over plain arrays —
> not the "analyze-then-binary-search-longest-prefix" sketch below. The
> expression form described here is aspirational.

```
let pw: Piecewise<F, 2> = segment(data, max_degree: 2, max_error: 0)
```

`segment()` partitions an input array into contiguous segments, each fit by a
polynomial of degree at most `max_degree`.

**Algorithm**:
1. Starting at position 0, run `analyze()` on the remaining data.
2. If polynomial detected: extend the segment as far as the polynomial holds.
   Binary search for the longest prefix where the polynomial exactly reconstructs
   the data (for `max_error: 0`).
3. Record the segment (start, length, coefficients).
4. Advance to the end of the segment, repeat from step 1.
5. If no polynomial detected: emit a literal segment of length 1 and advance.

**Edge case**: If the entire input is a single polynomial, the result is a
`Piecewise` with 1 segment. Implicit conversion to `Poly` is available.

### 5.7 Control Flow Expressions

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** `if`/`else` and `match` are
> **statements**, not value-producing expressions — `parse_expr` does not admit
> them, so `let x = if c { a } else { b };` does not parse. Assign inside each
> branch instead (`let mut x; if c { x = a; } else { x = b; }`). The
> expression forms below are the intended surface.

```
if condition { expr } else { expr }    // expression, not statement
match value {
    pattern => expr,
    pattern => expr,
    _ => expr,                          // exhaustive match required
}
```

`if/else` and `match` are expressions that return values. Both branches of
`if/else` must have the same type. `match` must be exhaustive.

### 5.8 Range Expressions

```
0..n       // exclusive: 0, 1, ..., n-1                    -- IMPLEMENTED
0..=n      // inclusive: 0, 1, ..., n    [planned; not implemented in v0.1.0]
```

The exclusive range `a..b` is implemented and used in `for` loops (§6.3).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** The inclusive range `a..=b` is not
> parsed (§2.11). Write `a..(b + 1)`.

### 5.9 Cast Expressions

```
let x: Field<251> = 200
let n: u8 = x as u8           // extract integer representative
let y: Field<251> = n as F    // reduce into field (mod p)
```

The `as` operator is the implemented cast mechanism. It covers:
- **Field ↔ integer**: `field_elem as uN` extracts the integer representative
  (always in `0..p-1`); `integer as Field<p>` reduces mod p. Signed integers
  reduce to their additive inverse — `(-3) as Field<251>` = 248.
- **Integer width changes**, including the wide integers `i128`/`u128`/`i256`/`u256`
  (zero/sign-extend or truncate, §3.1).
- **Pointer ↔ integer** and **pointer ↔ pointer** reinterpretation (`p as *U`,
  `p as usize`).
- **Scalar → `ExtField<F, 2>`**: `a as E` builds `(a, 0)`.
- Under a `dyn` field carrier, a reducing cast routes through the runtime
  reduction (§17.10).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** The `lift()` / `project()`
> *methods* (embed `Field<p>` into a larger `Field<q>` preserving the integer
> representative / reduce back) are not implemented. Use an explicit `as` cast
> through an integer (`(a as u64) as Field<q>`). Cross-characteristic value
> preservation is the programmer's responsibility; no semantic-change warning is
> emitted.

### 5.10 The `?` Operator (Early-Return Propagation)

```
expr?
```

Postfix early-return propagation for fallible calls. The operand must be
**Result-shaped**: an enum with exactly two single-payload variants `Ok(T)`
and `Err(E)`. `Result<T, E>` from `src/lib/core/result.tv` is the canonical
one (§3.9), but the check is structural — any user enum of that shape
qualifies.

- **`Ok(t)`** yields the payload `t` in place; evaluation continues. An
  aggregate `T` binds by pointer (the enum/struct convention) and reads like
  any struct value.
- **`Err(e)`** early-returns from the enclosing function: the payload is
  rebuilt as the function's own `Err` variant and returned inline. This is an
  exit path — the `defer` chain runs before propagation (§6.8), so resources
  live at the `?` are cleaned up.

Constraints, all compile-time errors with source positions:

- The operand must be Result-shaped (exactly `Ok(T)` / `Err(E)`).
- The enclosing function must itself return a Result-shaped type.
- The two `E` payload types must match **exactly** (after generic
  substitution) — there is no `From`/`Into`-style conversion; declare the
  same `E` on both Results.

`?` works in statement position (`let x = f()?;`) and chained inside a larger
expression (`f()? + g()?`):

```
fn total(a: i32, b: i32) -> Result<i64, FsError> {
    let x: i64 = get(a)?;       // unwrap, or early-return Err
    let y: i64 = get(b)?;
    return Result::Ok(x + y);
}
```

See `examples/qmark_basics.tv`.

---

## 6. Statements

### 6.1 Let Statements

```
let x = expr
let x: Type = expr
let mut x = expr
let (a, b) = tuple_expr       // destructuring  [planned; not implemented]
```

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Let-**destructuring**
> (`let (a, b) = ...`) is not parsed — only `let [mut] ident [: Type] = expr;`
> is accepted (tuples themselves are planned, §3.7). Bind the aggregate and read
> its fields.

### 6.2 Assignment

```
x = expr                      // only if x is `mut`
x += expr                     // compound assignment  [planned]
arr[i] = expr                 // array element assignment
s.field = expr                // struct field assignment
p[i] = expr                   // pointer element assignment
```

The implemented assignment forms are plain `=` to a `mut` binding, an array or
pointer element (`a[i] = e`), and a struct field (`s.field = e`).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Compound assignment
> (`+= -= *= /=`) is not parsed (§2.10). Write `x = x + e;`.

### 6.3 For Loops — Algebraic Bounds

```
for i in 0..n {
    body
}
```

A `for` loop iterates over an exclusive integer range `a..b`. The bounds may be
runtime values — `for i in 0..n {}` with a runtime `n` compiles fine.

**Bound width adoption.** The iterator adopts the width of the bounds: `i32` by
default, promoted to `i64` when either bound is 64-bit-typed (`i64`/`u64`/
`usize`), is a `u32` (whose values can exceed the signed-i32 space), or is an
integer literal `>= 2^31`. Narrower bounds (`i8`/`i16`/`u8`/`u16`) widen by
signedness. The iteration space is *signed* `i64`; comparisons are signed `<`.
Two hard edges: a bound literal `>= 2^63` is a compile-time error, and a
runtime `u64`/`usize` bound value `>= 2^63` aborts at loop entry (a one-icmp
guard — the value cannot be represented in the signed space, and Traveler
traps rather than silently misreads). Non-integer bounds (field elements,
pointers, booleans, aggregates, `i128`+) are compile-time errors: iteration
order lives outside the field. Auto-parallelized loops (§ auto-parallelization)
dispatch `i64`-wide iterators through a dedicated `i64` runtime path; bound
width never affects the dispatch decision — the analyzer's independence axioms
alone decide. Inside `#[zk]` functions, loop bounds must additionally fit
`i32` (the unroll count is the circuit size).

> **Descriptive, not enforced.** Earlier revisions specified that the range
> bound *must* be a compile-time constant or an algebra-derivable value, "to
> guarantee termination without runtime fuel." The `v0.1.0` compiler does **not**
> enforce this: it accepts arbitrary runtime bounds and performs no termination
> proof. The material below is the *idiom* Traveler code follows (every core
> loop is bounded by data length, degree, or field order), not a checked
> constraint. It is retained because it explains why `for` + `break` suffices
> and `while` (§6.5) is rarely needed.

In finite field computation, every core algorithm has a bound derivable from
three compile-time-known quantities:

| Bound source | What it limits | Origin |
|---|---|---|
| `n` (data length) | Outer iteration count | Block size, array length, or dataset size |
| `d` (polynomial degree) | Inner register updates | `Poly<F, d>` type parameter |
| `p` (field order) | Maximum state space | `Field<p>` type parameter |

These bounds cover all core operations:

```
// Polynomial evaluation: O(n * d)
for i in 0..n {                     // bound: data length
    for k in 0..d {                 // bound: polynomial degree
        reg[k] = reg[k] + reg[k + 1]
    }
}

// Newton forward differences: O(n * d)
for level in 0..max_degree + 1 {    // bound: degree from type
    for i in 0..remaining {         // bound: data length (strictly decreasing)
        diff[i] = diff[i + 1] - diff[i]
    }
}

// ML training: all bounds are compile-time constants
for epoch in 0..EPOCHS {
    for batch in 0..N_BATCHES {
        for i in 0..784 {
            for j in 0..HIDDEN {
                // field matmul
            }
        }
    }
}
```

**Early exit**: `break` terminates the innermost enclosing loop. This replaces
the need for `while` in cases like early termination of polynomial analysis:

```
for level in 0..max_degree + 1 {
    let nz = count_nonzero(diff[0..remaining])
    if nz == 0 { break }            // convergence: exact polynomial found
    if level >= 4 && nz >= prev_nz { break }  // stall: not polynomial
    // ...
}
```

**Continue**: `continue` skips to the next iteration.

**Rationale**: In finite field computation, every loop is bounded by
combinatorial properties of the algebra. The field order, polynomial degree,
and data length together determine the maximum number of operations. The
compiler can verify bounds, unroll small loops (degree <= 3), and vectorize
without speculation. No runtime fuel checks are needed.

For degree <= 3, the compiler SHOULD unroll the inner register update loop
entirely, eliminating the loop overhead.

### 6.4 Field Enumeration

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Iterating a field type directly
> (`for x in F`) is not implemented; `for` iterates integer ranges only (§6.3).
> Enumerate with an explicit range and cast: `for i in 0..p { let x: F = i as F; ... }`.
> The direct form below is aspirational.

```
for x in F {
    body
}
```

Iterates over all elements of a field type in order of their integer
representatives: `0, 1, 2, ..., p-1`. The bound is the field order `p`,
which is a compile-time constant from the `Field<p>` type parameter.

This is equivalent to:

```
for i in 0..F::ORDER {
    let x: F = F::from(i)
    body
}
```

Field enumeration is useful for building lookup tables (e.g., inverse tables
for small primes, log/exp tables for binary fields) and for exhaustive
verification of field identities.

### 6.5 While Loops

```
while condition {
    body
}
```

`while` is a **general native construct**: it lowers to a standard
condition/body/latch control-flow diamond, supports `break` and `continue`, and
is used throughout the standard library (the codec, wide-integer, and number-theory
modules all use `while`). It is NOT restricted to the ZK backend.

Prefer `for i in a..b { ... }` for counted iteration (it is the vectorizable,
auto-parallelizable shape, §15.18); reach for `while` when the trip count is
data-dependent.

> **Corrected from an earlier revision.** A prior draft declared `while` "ZK
> backend only" and a **compile error** for native compilation, with a
> `while cond fuel(n)` form. That is stale: native `while` is fully supported,
> and there is no `fuel(n)` annotation. The ZK circuit backend (§15, `#[zk]`)
> obtains its trip counts by **unrolling `for` loops**, not from `fuel`.

### 6.6 Return

```
return expr
```

Returns from the enclosing function. The last expression in a function body is
an implicit return (like Rust).

### 6.7 Match Statements

```
match expr {
    Pattern::Variant(x) => { ... },
    Pattern::Other => { ... },
    _ => { ... },                    // wildcard / default
}
```

`match` discriminates **enum variants** (binding payloads) and **integer
values**. It is a statement (§5.7).

> **Corrected from an earlier revision.** Exhaustiveness is **not** checked. A
> prior draft claimed "match must be exhaustive; the compiler verifies
> exhaustiveness at compile time" — the `v0.1.0` compiler performs no such check.
> A value matching no arm simply falls through (no arm executes); supply a `_`
> wildcard arm to handle the default case yourself.

### 6.8 Defer

```
defer stmt;
defer { stmts }
```

`defer` registers an exit-cleanup action on the enclosing **function** frame.
The body runs on **every exit path** — a `return expr`, a bare `return`,
falling off the end of the body, and each `?` early-return propagation —
**after the return value is computed**, so a deferred `free` never invalidates
the value being returned. Multiple defers in one function run
**last-in, first-out** (LIFO). The evaluator runs the defer chain at frame
teardown, so the `?` unwind behaves identically in evaluated and compiled
code.

Two placement constraints, both enforced at compile time:

- **Function spine only.** A `defer` must be a top-level statement of its
  function; a `defer` inside `if`/`for`/`while`/`match` is a compile-time
  error. (A conditional or looping defer would need runtime "did it execute"
  tracking — hidden state the memory model refuses, §12.3.)
- **No control flow out.** A defer body cannot `return`; it is cleanup, not
  control flow.

The motivating case is the `?` error path: an allocation live at a `?`
propagation would otherwise leak, because the operator rebuilds-and-returns
inline, bypassing hand-written cleanup. `defer` keeps cleanup adjacent to
acquisition without hidden destructors (§12.3, §14.3).

---

## 7. Algebraic Structure System

### 7.1 Built-in Traits

The language defines a hierarchy of algebraic traits. This **built-in hierarchy**
is compiler-known and automatically implemented for the appropriate types; it is
not extended or overridden by users (you cannot re-`impl Field` for your own
type). This is distinct from **user-defined traits**, which *are* supported and
are specified in §7.4 — the two systems coexist without interacting.

```
trait Add {
    fn add(self, other: Self) -> Self
}

trait Mul {
    fn mul(self, other: Self) -> Self
}

trait Neg {
    fn neg(self) -> Self
}

trait Inv {
    fn inv(self) -> Self    // multiplicative inverse; traps on zero
}

trait Zero {
    const ZERO: Self
}

trait One {
    const ONE: Self
}

// Composed structures:

trait Semigroup: Add {}                        // associative addition
trait Monoid: Semigroup + Zero {}              // + identity
trait Group: Monoid + Neg {}                   // + inverses
trait AbelianGroup: Group {}                   // + commutativity

trait Semiring: Monoid + Mul + One {}           // add is a commutative monoid, mul is a monoid
trait Ring: AbelianGroup + Mul + One {}        // add is an abelian group, mul distributes over add
trait CommutativeRing: Ring {}                 // mul is commutative
trait IntegralDomain: CommutativeRing {}       // no zero divisors
trait Field: IntegralDomain + Inv {}           // every nonzero has mul inverse
```

### 7.2 Automatic Implementation

| Type | Implements |
|---|---|
| `Field<p>` for any prime p | `Field` (and all supertraits) |
| `BinField<k, poly>` | `Field` (characteristic 2) |
| `Poly<F, d>` where F: Field | `Ring` (polynomial ring), NOT `Field` |
| `[F; n]` where F: Field | `Module<F>` (vector space) |
| `u8, u16, u32, u64` | `Ring` (integer ring, not a field) |
| `bool` | `Zero`, `One` only (logical type, not algebraic — use `Field<2>` for GF(2) arithmetic) |

### 7.3 Structure-Directed Compilation

The compiler uses the algebraic structure of a type to select code generation
strategies:

| Structure property | Compilation effect |
|---|---|
| `F: Field` with small p (p < 256) | Use u8 arithmetic + conditional subtract |
| `F: Field` with `F::IS_NTT_FRIENDLY` | Use NTT for poly multiplication |
| `F: Field` with characteristic 2 | Use XOR for addition, CLMUL for multiplication |
| `M: Module<F>` | Vectorize element-wise operations |
| `Poly<F, d>` with d <= 3 | Unroll forward summation loop |
| `Poly<F, d>` with d > 64 | Use heap allocation for coefficient arrays |

This is the core differentiator: **the algebraic structure is not just checked,
it drives code generation.** A generic function `fn f<F: Field>(x: F) -> F`
compiles to different instruction sequences for `Field<251>`, `Field<65521>`,
and `BinField<8, 0x11B>`.

### 7.4 User-Defined Traits and Operator Overloading

Separately from the built-in algebraic hierarchy (§7.1), users may declare their
own `trait`s and `impl` them for their own types:

```
trait Shape {
    fn area(self) -> i64;
}

impl Shape for Rect {
    fn area(self) -> i64 { return self.w * self.h; }
}
```

Semantics:

- **Static dispatch only.** Trait methods are **monomorphized**, never boxed.
  A method is mangled per implementing type (`Trait__Type__method`) and resolved
  at   compile time. There is **no `dyn Trait`, no vtable** — the design line
  Traveler holds (§20.1). Runtime code-selection is expressed with function
  pointers (§3.10), not trait objects.
- **Receiver.** A method's `self` parameter is passed as `&Type` (a pointer to
  the receiver); `x.method()` resolves type-directed through the receiver
  (§17.9).
- **Bounds.** A generic function may require a trait: `fn f<T: Shape>(x: T)`.
  The bound is enforced at instantiation.
- **Operator overloading.** Implementing the operator traits `Add`, `Sub`,
  `Mul`, `Eq` for a user struct makes `+`, `-`, `*`, `==` desugar to the
  corresponding method for that type. Scalar and field arithmetic are untouched
  by this (they are compiler-known, §8); overloading applies only to user types
  that carry the relevant `impl`.

---

## 8. Field Arithmetic Semantics

### 8.1 Prime Field Arithmetic: Z/pZ

All values are in the canonical range `{0, 1, ..., p-1}`.

**Addition**: `(a + b) mod p`
```
add(a, b):
    let s = a + b               // in the wider intermediate type
    if s >= p: s -= p
    return s
```
Branchless: `s - (p & -(s >= p))` where `-true = all-ones mask`.

**Subtraction**: `(a + p - b) mod p`
```
sub(a, b):
    let d = a + p - b           // in the wider intermediate type
    if d >= p: d -= p
    return d
```

**Multiplication**: `(a * b) mod p`
```
mul(a, b):
    let prod = a * b            // in the intermediate type (double width)
    return prod % p             // modular reduction
```

**Reduction, by width (as implemented).** For primes up to 2^32 the reduction
is a single hardware `urem` in the intermediate type. For 64-bit primes the
compiler emits **Barrett reduction** (multiply-high with a compile-time constant
`m = floor(2^128 / p)` via an `i256` intermediate), *not* `urem i128` — a 128-bit
`urem` lowers to a `__udivti3`/`__udivei4` software-division libcall, which the
C-free trust chain does not admit. Wider (>2^64) primes use the runtime
wide-field Barrett path (§16.16, §17.10). Montgomery form is permitted but not
currently emitted.

**Width classes**:

| Field | Element type | Intermediate type | Max product | Fits in |
|---|---|---|---|---|
| `Field<p>`, p <= 255 | `u8` | `u16` | 250 * 250 = 62,500 | u16 (65,535) |
| `Field<p>`, p <= 65,535 | `u16` | `u32` | 65,520^2 = 4,292,870,400 | u32 (4,294,967,295) |
| `Field<p>`, p <= 2^32 - 1 | `u32` | `u64` | (2^32-6)^2 ≈ 2^64 - 12*2^32 | u64 (2^64 - 1) |
| `Field<p>`, p <= 2^64 - 1 | `u64` | `u128` | (2^64-60)^2 ≈ 2^128 - 120*2^64 | u128 (2^128 - 1) |

The largest primes fitting each width:
- 8-bit: **251** (2^8 - 5)
- 16-bit: **65,521** (2^16 - 15)
- 32-bit: **4,294,967,291** (2^32 - 5)
- 64-bit: **18,446,744,073,709,551,557** (2^64 - 59)

For 64-bit fields, both addition and multiplication require `u128` intermediate
arithmetic. Addition: `max(a + b) = 2*(p-1) ≈ 2^65`, which overflows `u64`.
Multiplication: `max(a * b) = (p-1)^2 ≈ 2^128`, which fits in `u128`.

**Multiplicative inverse**: `a^(p-2) mod p` (by Fermat's little theorem)
```
inv(a):
    return pow(a, p - 2)        // modular exponentiation by squaring
```

> **NOT YET IMPLEMENTED (v0.1.0).** `inv` emits no zero check: `inv(0)` computes
> `pow(0, p-2) = 0` silently rather than trapping (§5.2, §16.11). A guarding
> trap is planned.

For small primes (p < 256), the compiler MAY precompute an inverse table at
compile time and use a lookup instead of exponentiation. (Not currently emitted;
the `pow`-based inverse is used at all widths.)

**Exponentiation**: `a^n mod p` by repeated squaring.
```
pow(a, n):
    let result = 1
    let base = a
    while n > 0:
        if n & 1: result = (result * base) % p
        base = (base * base) % p
        n >>= 1
    return result
```

### 8.2 Binary Extension Field Arithmetic: GF(2^k)

Elements are polynomials over GF(2) of degree < k, represented as k-bit integers.

**Addition**: XOR
```
add(a, b): return a ^ b
```

**Subtraction**: XOR (same as addition in characteristic 2)
```
sub(a, b): return a ^ b
```

**Multiplication**: carry-less polynomial multiplication modulo the reducing
polynomial. Two runtime paths (§3.2.2), and **no PCLMULQDQ intrinsic is
emitted** (the generic form is portable and C-free):

- **Generic shift-and-XOR** (`@bf<k>_<poly>_mul`, the default for every
  `BinField<k, poly>` except the legacy AES field): accumulate shifted copies of
  one operand while reducing modulo `poly` bit by bit.
  ```
  mul(a, b):
      let r = 0
      for i in 0..k:
          if (b >> i) & 1: r ^= a << i     // partial products (carry-less)
      return reduce(r, poly)                // fold high bits mod the polynomial
  ```
- **Log/exp table** (the legacy `GF(2^8, 0x11B)` field only):
  ```
  mul(a, b):
      if a == 0 || b == 0: return 0
      return exp_table[(log_table[a] + log_table[b]) % (2^k - 1)]
  ```

**Inverse**: `a^(2^k - 2)` by Fermat's little theorem in GF(2^k), via `pow`.

### 8.3 Characteristic-Dependent Behavior

| Property | Prime field Z/pZ | Binary field GF(2^k) |
|---|---|---|
| `a + a` | `2a mod p` (nonzero if p > 2) | `0` (always, characteristic 2) |
| `a - b` | `a + p - b mod p` | `a XOR b` (same as addition) |
| `-a` | `p - a` | `a` (every element is its own additive inverse) |
| `2` exists and is nonzero? | Yes (if p > 2) | No (2 = 0 in char 2) |
| Division by 2 | `a * inv(2)` | **compile error**: 2 = 0 in characteristic 2 |

**Edge case: division by 2 in characteristic 2.**
```
field F = BinField<8, 0x11B>
let x: F = 42 / 2    // compile error: division by zero (2 = 0 in GF(2^k))
```

The compiler evaluates the literal `2` in the field context and determines
it equals zero. This is caught at compile time.

### 8.4 Platform Determinism Guarantee

All field arithmetic is deterministic across platforms. The result of `Field<p>`
operations is identical on x86, ARM64, RISC-V, WASM, or any other target that
correctly implements unsigned integer arithmetic:

- **Addition**: `(a + b) mod p` — unsigned add + conditional subtract. No rounding.
- **Subtraction**: `(a + p - b) mod p` — unsigned add + conditional subtract. No rounding.
- **Multiplication**: `(a * b) mod p` — unsigned widening multiply + modular reduction. No rounding.
- **Inverse**: `a^(p-2) mod p` — repeated squaring of the above. No rounding.

There are no floating-point operations, no FMA (fused multiply-add) variance,
no rounding modes, no NaN, no denormals. The bit-exact output of every field
operation is determined solely by the input values and the prime `p`.

**Consequence for compression**: when data is represented as field elements from
the point of creation, the compressed output is identical on every architecture.
This is not true for data that passes through a float-to-integer quantization
step, where IEEE 754 nondeterminism at rounding boundaries (particularly FMA
availability: ARM64 default vs x86 `-mfma`) can produce different integer
sequences for the same logical signal. The staircase segment boundaries become
platform-dependent, and while polycompress handles both correctly (lossless),
the compression ratio on quantized data is nondeterministic across platforms.

The language eliminates this class of nondeterminism entirely by making
`Field<p>` the native numeric type, ensuring the polynomial never leaves the
algebraic domain.

**Binary extension fields** (`BinField<k>`) inherit the same guarantee: XOR and
carry-less multiplication are bitwise-deterministic operations with no
platform-dependent behavior.

---

## 9. Polynomial Semantics

> **Compiler primitives vs. standard library.** The compiler provides a small
> set of polynomial *primitives* directly: the `Poly<F, d>` type with
> representation tracking (§9.1), scalar `eval` (§9.2), `analyze` (§9.3), and
> repr-aware polynomial addition. Everything richer — polynomial multiplication,
> composition, differentiation, GCD (§9.4), and the NTT (§9.5) — is **provided by
> the standard library** (`src/lib/nt/polyfield.tv`, `src/lib/crypto/ntt.tv`),
> built on those primitives and on plain field arithmetic over arrays. The
> subsections below mark, per topic, which side of that line each item is on.

### 9.1 Newton Forward Difference Representation

Internally, polynomials may be stored in either **standard coefficient form**
(`c_0 + c_1*t + c_2*t^2 + ...`) or **Newton forward difference form**
(`N_0 + N_1*binom(t,1) + N_2*binom(t,2) + ...`).

The Newton form is the natural output of `analyze()` and the natural input of
`eval()`. The standard form is the natural input for algebraic operations
(addition, multiplication, composition).

The compiler tracks which form a polynomial is in and inserts conversions as
needed. This is an implementation detail not exposed to the programmer.

#### 9.1.1 Newton Form → Standard Form

Given Newton coefficients `N[0..d]`, compute standard coefficients `S[0..d]`
such that `N_0 + N_1*C(t,1) + N_2*C(t,2) + ... = S_0 + S_1*t + S_2*t^2 + ...`

**Precondition**: `d < F::PRIME`. The conversion divides by integers `1, 2, ..., d`.
When `d >= p`, one of these divisors is `p = 0` in the field, causing division
by zero. See Section 16.10 for the full analysis of this barrier.

The conversion uses the fact that the falling factorial basis `C(t,k)` expands
as a sum of powers of `t` with Stirling-number coefficients. However, the
direct O(d^2) algorithm avoids computing Stirling numbers explicitly by
accumulating the contribution of each Newton coefficient one at a time:

```
fn newton_to_standard(N: &[F; d + 1]) -> [F; d + 1] {
    // Start with the zero polynomial in standard form
    let mut S: [F; d + 1] = [F::ZERO; d + 1]

    // Process Newton coefficients from highest degree down.
    // Maintain a running polynomial in standard form that represents
    // the accumulated result so far.
    //
    // Key identity: C(t, k) = C(t, k-1) * (t - k + 1) / k
    // We build up the polynomial by repeatedly multiplying by (t - j)/j
    // and adding the next Newton coefficient.
    //
    // Equivalently: we accumulate using the recurrence
    //   P_d(t) = N[d]
    //   P_k(t) = P_{k+1}(t) * (t - k) / (k + 1) + N[k]   (Horner on falling factorial basis)
    //
    // But division by (k+1) is a field operation: multiply by inv(k+1).

    // Working array: standard-form coefficients of the current accumulated polynomial
    S[0] = N[d]

    for k_rev in 0..d {
        let k = d - 1 - k_rev        // k goes d-1, d-2, ..., 0

        // Multiply current polynomial by (t - (k+1)):
        //   If P(t) = s_0 + s_1*t + ... + s_m*t^m
        //   then P(t) * (t - c) = -c*s_0 + (s_0 - c*s_1)*t + (s_1 - c*s_2)*t^2 + ... + s_m*t^{m+1}
        let c: F = F::from(k + 1)
        let m = d - k - 1             // current degree of accumulated polynomial

        // Shift up: process from high to low to avoid overwriting
        for j_rev in 0..m + 1 {
            let j = m + 1 - j_rev     // j goes m+1, m, ..., 1
            if j > m {
                S[j] = S[j - 1]
            } else {
                S[j] = S[j - 1] - c * S[j]
            }
        }
        S[0] = F::ZERO - c * S[0]

        // Divide by (k_rev + 1) = (d - k):
        //   This accounts for the factorial denominator in C(t, k+1)
        let divisor = F::from(k_rev + 1)
        let inv_d = F::ONE / divisor
        for j in 0..m + 2 {
            S[j] = S[j] * inv_d
        }

        // Add N[k]
        S[0] = S[0] + N[k]
    }

    return S
}
```

**Complexity**: O(d^2) field multiplications and additions.

**Correctness**: This is Horner's method applied to the Newton basis. At each
step, the accumulated polynomial represents `sum_{i=k}^{d} N[i] * C(t, i)`,
converted to standard form. The final result represents
`sum_{i=0}^{d} N[i] * C(t, i)` in standard form.

#### 9.1.2 Standard Form → Newton Form

Given standard coefficients `S[0..d]`, compute Newton coefficients `N[0..d]`.

**Precondition**: `d < F::PRIME`. The algorithm evaluates the polynomial at
`t = 0, 1, ..., d`. When `d >= p`, these evaluation points wrap around the
field (since `p = 0` in Z/pZ), and the resulting Newton coefficients are
incorrect — they represent a different polynomial of degree < p. See Section
16.10 for the full analysis.

This is exactly what `analyze()` computes when applied to the values
`S(0), S(1), ..., S(d)` — the iterated forward differences of the polynomial
evaluated at consecutive integers starting from 0.

```
fn standard_to_newton(S: &[F; d + 1]) -> [F; d + 1] {
    // Evaluate the standard-form polynomial at t = 0, 1, ..., d
    let mut vals: [F; d + 1]
    for t in 0..d + 1 {
        let mut v: F = F::ZERO
        let mut t_pow: F = F::ONE
        let t_field: F = F::from(t)
        for k in 0..d + 1 {
            v = v + S[k] * t_pow
            t_pow = t_pow * t_field
        }
        vals[t] = v
    }

    // Compute iterated forward differences: vals[0] is N[0],
    // first differences give N[1], second differences give N[2], etc.
    let mut N: [F; d + 1]
    for level in 0..d + 1 {
        N[level] = vals[0]
        for i in 0..d - level {
            vals[i] = vals[i + 1] - vals[i]
        }
    }

    return N
}
```

**Complexity**: O(d^2) for evaluation, O(d^2) for differencing. Total O(d^2).

**Correctness**: The Newton forward difference coefficients of a polynomial
`p(t)` are by definition the iterated forward differences of `p(0), p(1), ...`,
i.e., `N[k] = Delta^k p(0)`. This algorithm computes those values directly.

**Alternative**: The conversion can also be expressed as multiplication by the
lower-triangular matrix of binomial coefficients (Newton → standard) or its
inverse (standard → Newton). The matrix entries are `C(i, j)` for the forward
direction and `(-1)^{i-j} * C(i, j)` for the inverse. The above algorithms
are equivalent to these matrix multiplications but avoid explicitly constructing
the matrix.

#### 9.1.3 When the Compiler Inserts Conversions

The compiler tracks a `repr` tag on each `Poly<F, d>` value:

| Tag | Meaning | Produced by | Consumed by |
|---|---|---|---|
| `newton` | Newton forward difference form | `analyze()` | `eval()`, streaming |
| `standard` | Standard coefficient form | literal `poly(...)`, arithmetic | `+`, `*`, `compose`, Karatsuba, NTT |

Automatic conversion (implemented) is inserted at the following points:
- Passing a `standard`-tagged polynomial to `eval()` → insert `standard_to_newton`
- Passing a `newton`-tagged polynomial to `+` → insert `newton_to_standard`

The generated converters use fixed `[32 x elem]` scratch/output buffers, so
conversion is supported up to degree 31 (not the unbounded-`d` algorithm the
prose above suggests). The conversion cost is O(d^2).

> **NOT YET IMPLEMENTED (v0.1.0 — planned).** Explicit `poly.to_newton()` /
> `poly.to_standard()` methods are not available; only the *implicit* insertions
> above exist. There is also no hot-loop conversion warning. (`*` and `compose`
> are library operations, §9.4, so no implicit conversion is inserted for them.)

### 9.2 Evaluation

The implemented `eval(poly, point)` returns a single scalar (§5.4). The
forward-summation loop below is the *decode* algorithm — the mechanism the
scalar `eval` and the (planned) vector form share, and exactly what the
`Register` primitive (§17.2) exposes for streaming a run of points:

```
// decode a range via forward summation (the planned vector form of eval):
fn decode(poly: Poly<F, d>, n: usize) -> [F; n] {
    let mut reg: [F; d + 1] = poly.newton_coefficients()
    let mut out: [F; n]
    out[0] = reg[0]
    for i in 1..n {
        for k in 0..d {
            reg[k] = reg[k] + reg[k + 1]
        }
        out[i] = reg[0]
    }
    return out
}
```

### 9.3 Analysis

`analyze(data)` computes iterated forward differences:

```
fn analyze(data: &[F], max_deg: usize) -> Option<DynPoly<F>> {
    let diff = data.copy()
    let mut remaining = data.len()
    let mut prev_nz = remaining
    let mut newton: [F; max_deg + 1]

    for level in 0..=max_deg {
        newton[level] = diff[0]

        if level > 0 {
            let nz = count_nonzero(diff[0..remaining])
            if nz == 0: return Some(DynPoly { degree: level - 1, coeffs: newton })
            if all_same(diff[0..remaining]): return Some(DynPoly { degree: level, coeffs: newton })

            // Early termination heuristics
            if level >= 4 && nz >= prev_nz: return None
            if level >= 4 && nz > remaining * 17 / 20: return None
            if level >= max_deg / 2 && nz > remaining / 3: return None

            prev_nz = nz
        }

        // Compute next level of differences
        for i in 0..remaining-1 {
            diff[i] = diff[i+1] - diff[i]
        }
        remaining -= 1
    }
    return None
}
```

> **Partly implemented.** The **iterated forward-difference core** is what the
> `v0.1.0` `analyze(data, npts)` computes — but it constructs a NEWTON-form
> `Poly<F, d>` at the degree `d` fixed by the binding, not a runtime-degree
> `DynPoly`, and it does **not** run the `Option`/`None` degree-detection or the
> early-termination heuristics shown here (§5.5). Degree detection is planned.

### 9.4 Polynomial Arithmetic

> **Addition is a builtin; the rest is standard library.** Repr-aware
> polynomial `+` is emitted by the compiler. Scalar multiply, polynomial
> multiplication (schoolbook / Karatsuba / NTT-based), composition,
> differentiation, division, and GCD are **provided by the standard library** —
> `src/lib/nt/polyfield.tv` gives carrier-generic `pf_mul`, `pf_divmod`,
> `pf_gcd`, `pf_deriv`, `pf_eval`, etc. over any `Field<p>`/`BinField`. The
> strategy-selection thresholds and algorithms in the rest of this subsection
> describe how that library computes and the intended operator surface, not a
> set of compiler builtin operators (§5.3).

**Addition**: `Poly<F,a> + Poly<F,b> -> Poly<F, max(a,b)>`
Pad the shorter polynomial with zeros, add coefficients element-wise.

**Scalar multiplication**: `Poly<F,d> * F -> Poly<F,d>`
Multiply each coefficient by the scalar.

**Polynomial multiplication**: `Poly<F,a> * Poly<F,b> -> Poly<F, a+b>`

For small degrees (a + b <= 64): schoolbook O(a*b):
```
for i in 0..=a:
    for j in 0..=b:
        result[i+j] += self[i] * other[j]
```

For large degrees with NTT-friendly field: NTT-based O(n log n):
```
let n = next_power_of_2(a + b + 1)
let omega = F::primitive_root_of_unity(n)
let fa = ntt(self.pad_to(n), omega)
let fb = ntt(other.pad_to(n), omega)
let fc = pointwise_multiply(fa, fb)
let result = inverse_ntt(fc, omega)
```

For large degrees with non-NTT-friendly field (or NTT-friendly fields with
degree 33–64 where NTT overhead is not justified): Karatsuba O(n^1.585).

#### 9.4.1 Karatsuba Multiplication

Karatsuba multiplication reduces polynomial multiplication from O(n^2) to
O(n^log2(3)) ≈ O(n^1.585) by trading one multiplication for three at each
recursive level.

Given `A(t) = A_lo + A_hi * t^m` and `B(t) = B_lo + B_hi * t^m` where
`m = ceil(n/2)`:

```
A * B = A_lo * B_lo
      + (A_lo * B_hi + A_hi * B_lo) * t^m
      + A_hi * B_hi * t^{2m}
```

The naive approach requires 4 sub-multiplications. Karatsuba's insight: compute
3 products and derive the middle term by subtraction:

```
z0 = A_lo * B_lo
z2 = A_hi * B_hi
z1 = (A_lo + A_hi) * (B_lo + B_hi) - z0 - z2
```

Then `A * B = z0 + z1 * t^m + z2 * t^{2m}`.

**Full algorithm** (recursive, both inputs in standard form):

```
fn karatsuba(A: &[F], B: &[F]) -> Vec<F> {
    let na = A.len()
    let nb = B.len()
    let n = max(na, nb)

    // Base case: fall back to schoolbook for small polynomials
    if n <= 32 {
        let mut result = Vec::with_cap(na + nb - 1)
        for i in 0..na + nb - 1 {
            result.push(F::ZERO)
        }
        for i in 0..na {
            for j in 0..nb {
                result[i + j] = result[i + j] + A[i] * B[j]
            }
        }
        return result
    }

    let m = (n + 1) / 2  // split point: ceil(n / 2)

    // Split A = A_lo + A_hi * t^m
    // A_lo = A[0..m], A_hi = A[m..na]  (may be shorter than m if na < 2m)
    let a_lo_len = min(m, na)
    let a_hi_len = if na > m { na - m } else { 0 }
    let b_lo_len = min(m, nb)
    let b_hi_len = if nb > m { nb - m } else { 0 }

    // Compute the three sub-products:
    // z0 = A_lo * B_lo
    let z0 = karatsuba(A[0..a_lo_len], B[0..b_lo_len])

    // z2 = A_hi * B_hi (may be trivially zero if either is empty)
    let z2 = if a_hi_len > 0 && b_hi_len > 0 {
        karatsuba(A[m..na], B[m..nb])
    } else {
        vec![F::ZERO]
    }

    // sum_a = A_lo + A_hi (element-wise, zero-pad shorter to length max(a_lo_len, a_hi_len))
    let sum_a_len = max(a_lo_len, a_hi_len)
    let mut sum_a = Vec::with_cap(sum_a_len)
    for i in 0..sum_a_len {
        let a_lo_i = if i < a_lo_len { A[i] } else { F::ZERO }
        let a_hi_i = if i < a_hi_len { A[m + i] } else { F::ZERO }
        sum_a.push(a_lo_i + a_hi_i)
    }

    // sum_b = B_lo + B_hi
    let sum_b_len = max(b_lo_len, b_hi_len)
    let mut sum_b = Vec::with_cap(sum_b_len)
    for i in 0..sum_b_len {
        let b_lo_i = if i < b_lo_len { B[i] } else { F::ZERO }
        let b_hi_i = if i < b_hi_len { B[m + i] } else { F::ZERO }
        sum_b.push(b_lo_i + b_hi_i)
    }

    // z1_full = (A_lo + A_hi) * (B_lo + B_hi)
    let z1_full = karatsuba(sum_a.as_slice(), sum_b.as_slice())

    // z1 = z1_full - z0 - z2
    let z1_len = z1_full.len()
    let mut z1 = Vec::with_cap(z1_len)
    for i in 0..z1_len {
        let z0_i = if i < z0.len() { z0[i] } else { F::ZERO }
        let z2_i = if i < z2.len() { z2[i] } else { F::ZERO }
        z1.push(z1_full[i] - z0_i - z2_i)
    }

    // Combine: result = z0 + z1 * t^m + z2 * t^{2m}
    let result_len = na + nb - 1
    let mut result = Vec::with_cap(result_len)
    for i in 0..result_len {
        result.push(F::ZERO)
    }

    for i in 0..z0.len() {
        result[i] = result[i] + z0[i]
    }
    for i in 0..z1.len() {
        if i + m < result_len {
            result[i + m] = result[i + m] + z1[i]
        }
    }
    for i in 0..z2.len() {
        if i + 2 * m < result_len {
            result[i + 2 * m] = result[i + 2 * m] + z2[i]
        }
    }

    return result
}
```

**Complexity**: T(n) = 3 * T(n/2) + O(n) → O(n^log2(3)) ≈ O(n^1.585).

**Base case threshold**: The algorithm falls back to schoolbook when
`max(deg_a, deg_b) + 1 <= 32`. This threshold is chosen because schoolbook
has lower constant factors for small n (no recursion overhead, no temporary
allocations).

**Memory**: Each recursive level allocates O(n) temporary storage for `sum_a`,
`sum_b`, `z0`, `z1`, `z2`, and `result`. Total allocation across all levels
is O(n * log n). The compiler MAY reuse buffers across recursive calls as an
optimization.

**Correctness**: The identity `z1 = (A_lo + A_hi)(B_lo + B_hi) - z0 - z2 =
A_lo*B_hi + A_hi*B_lo` holds over any ring, including Z/pZ. No division is
used, so the algorithm works for all fields including `Field<2>`.

**Compiler dispatch**: The compiler selects the multiplication strategy based
on degree and field properties:

| Condition | Strategy |
|---|---|
| `a + b <= 64` | Schoolbook (Section 9.4, inline) |
| `a + b > 64` and field is NTT-friendly | NTT (Section 9.5) |
| `a + b > 64` and field is NOT NTT-friendly | Karatsuba (Section 9.4.1) |
| `a + b > 32` and `a + b <= 64` and NOT NTT-friendly | Karatsuba |

**Composition**: `p.compose(q)` = `p(q(t))`
Computed via Horner's method on polynomial arguments:
```
result = p[d]
for i in (0..d).rev() {
    result = result * q + p[i]
}
```
Result degree: `d_p * d_q`.

**Derivative**: `p.derivative() -> Poly<F, d-1>`
```
result[i] = (i + 1) * p[i + 1]   for i in 0..d
```

**Edge case**: Derivative of degree-0 polynomial returns `Poly<F, 0>` with
coefficient 0. This is the zero polynomial.

**Edge case in characteristic p**: In `Field<p>`, the derivative of `t^p` is
`p * t^(p-1) = 0` (since p = 0 in the field). This means polynomials of degree
>= p may have zero derivatives even though they are not constant. This is
mathematically correct and not an error.

### 9.5 NTT (Number Theoretic Transform)

> **Provided by the standard library** (`src/lib/crypto/ntt.tv`), not a language
> builtin. The compiler supplies only the field-level ingredients the transform
> needs (roots of unity, inverse roots, `n^-1`); the Cooley-Tukey butterfly
> network, bit-reversal, and inverse transform are ordinary Traveler code over
> field arrays, `import`ed and often `instantiate`d per prime. The description
> below documents that library and the shared algorithm.

Available when `F::IS_NTT_FRIENDLY == true`.

```
fn ntt(coeffs: &[F; n], omega: F) -> [F; n]
```

where `n` is a power of 2 and `omega` is a primitive n-th root of unity in F.

**Implementation**: Cooley-Tukey butterfly network, in-place, bit-reversal
permutation for input ordering.

```
fn ntt_inplace(a: &mut [F; n], omega: F) {
    // Bit-reversal permutation
    bit_reverse(a)

    // Butterfly layers: log2(n) stages, bounded by 64 (max NTT size = 2^64)
    let mut len = 2
    for _stage in 0..64 {
        if len > n { break }
        let w = omega.pow((n / len) as u64)
        for start in (0..n).step_by(len) {
            let mut wk = F::ONE
            for k in 0..len/2 {
                let u = a[start + k]
                let v = a[start + k + len/2] * wk
                a[start + k]         = u + v
                a[start + k + len/2] = u - v
                wk = wk * w
            }
        }
        len *= 2
    }
}
```

**NTT-friendly prime detection** (compile-time):
```
IS_NTT_FRIENDLY = (exists k >= 10 such that 2^k divides p - 1)
NTT_MAX_LOG = largest k such that 2^k divides p - 1
```

Known NTT-friendly primes the compiler recognizes:
- 998244353 (NTT_MAX_LOG = 23)
- 2^31 - 2^27 + 1 = 2013265921 (BabyBear, NTT_MAX_LOG = 27)
- 2^64 - 2^32 + 1 (Goldilocks, NTT_MAX_LOG = 32)
- 251 (NTT_MAX_LOG = 1, NOT NTT-friendly)
- 65521 (NTT_MAX_LOG = 4, NOT NTT-friendly)
- 18446744073709551557 (2^64 - 59, NTT_MAX_LOG = 2, NOT NTT-friendly)
- 2^64 - 2^32 + 1 = 18446744069414584321 (Goldilocks, NTT_MAX_LOG = 32, NTT-friendly)

The Goldilocks prime is the preferred 64-bit NTT-friendly prime. It is used
extensively in ZK-STARK systems (Plonky2, Plonky3). When NTT-based polynomial
multiplication is needed at 64-bit width, the compiler selects the Goldilocks
prime. The general-purpose 64-bit prime (2^64 - 59) is preferred for
compression and general arithmetic where NTT is not required.

#### 9.5.1 Bit-Reversal Permutation

Reorders array elements so that index `i` maps to `bit_reverse(i, log2(n))`.
Required before the in-place Cooley-Tukey butterfly.

```
fn bit_reverse(a: &mut [F], n: usize) {
    let log_n = log2(n)     // n is a power of 2; log2 is exact
    for i in 0..n {
        let j = reverse_bits(i, log_n)
        if i < j {
            let tmp = a[i]
            a[i] = a[j]
            a[j] = tmp
        }
    }
}

fn reverse_bits(x: usize, bits: usize) -> usize {
    let mut r: usize = 0
    let mut v = x
    for _b in 0..bits {
        r = (r << 1) | (v & 1)
        v = v >> 1
    }
    return r
}

fn log2(n: usize) -> usize {
    // Precondition: n is a power of 2
    let mut k: usize = 0
    let mut v = n
    for _i in 0..64 {
        if v <= 1 { break }
        v = v >> 1
        k = k + 1
    }
    return k
}
```

**Complexity**: O(n * log n) for the bit-reversal loop, O(n) swaps.

#### 9.5.2 Primitive Root of Unity

To perform an NTT of size `n` over `Field<p>`, we need `omega` such that
`omega^n = 1` and `omega^k != 1` for `0 < k < n`. This requires `n | (p - 1)`.

**Algorithm**: Find a generator `g` of the multiplicative group Z/pZ*, then
compute `omega = g^((p-1)/n)`.

Finding a generator uses trial search with order verification:

```
fn find_generator(p: u64) -> F {
    // A generator g of Z/pZ* has order p-1.
    // Equivalently: for every prime factor q of p-1, g^((p-1)/q) != 1.
    let pm1 = p - 1
    let factors = prime_factors(pm1)

    // Trial search starting from 2
    for g_val in 2..p {
        let g: F = F::from(g_val)
        let mut is_gen = true
        for _idx in 0..factors.len() {
            let q = factors[_idx]
            let exp = pm1 / q
            if g ** (exp as u32) == F::ONE {
                is_gen = false
                break
            }
        }
        if is_gen {
            return g
        }
    }
    // Unreachable for any prime p >= 2: generators always exist.
}

fn primitive_root_of_unity(n: usize, p: u64) -> F {
    // Precondition: n divides p - 1 (guaranteed by NTT-friendly check)
    let g = find_generator(p)
    let exp = (p - 1) / (n as u64)
    return g ** (exp as u32)
}
```

`prime_factors(m)` returns the distinct prime factors of `m`. For compile-time
use, a simple trial division suffices:

```
fn prime_factors(m: u64) -> Vec<u64> {
    let mut factors = Vec::new()
    let mut n = m
    let mut d: u64 = 2
    for _trial in 0..64 {
        if d * d > n { break }
        if n % d == 0 {
            factors.push(d)
            for _div in 0..64 {
                if n % d != 0 { break }
                n = n / d
            }
        }
        d = d + 1
    }
    if n > 1 {
        factors.push(n)
    }
    return factors
}
```

**When this runs**: `find_generator` and `primitive_root_of_unity` are
compile-time computations. The compiler evaluates them during type checking
when it encounters NTT-based polynomial multiplication on a specific
`Field<p>`. The results are embedded as constants in the generated code.

**Correctness**: For any prime `p`, the multiplicative group Z/pZ* is cyclic
of order `p - 1`. A generator exists and can be found by exhaustive trial.
For known NTT-friendly primes (e.g., 998244353), the generator is typically
small (g = 3). The trial search terminates quickly in practice.

#### 9.5.3 Inverse NTT

The inverse NTT recovers polynomial coefficients from point evaluations.
It is the forward NTT with `omega` replaced by `omega^(-1)`, followed by
scaling all elements by `n^(-1) mod p`.

```
fn inverse_ntt(a: &mut [F; n], omega: F) {
    let omega_inv = F::ONE / omega       // field inverse of omega
    ntt_inplace(a, omega_inv)

    // Scale by n^(-1) mod p
    let n_inv = F::ONE / F::from(n)
    for i in 0..n {
        a[i] = a[i] * n_inv
    }
}
```

**Complexity**: Same as forward NTT: O(n log n) field operations, plus O(n)
for the final scaling pass.

**Correctness**: The NTT matrix is `M[i][j] = omega^(i*j)`. Its inverse is
`(1/n) * M'[i][j]` where `M'[i][j] = omega^(-i*j)`. Applying the forward
NTT with `omega^(-1)` computes `M' * a`, and dividing by `n` completes the
inversion. Since `n | (p-1)` and `p` is prime, `n` has a multiplicative
inverse in Z/pZ.

#### 9.5.4 Padding

`pad_to(poly, n)` extends a polynomial's coefficient array to length `n` by
appending zeros. The polynomial's mathematical value is unchanged; only the
storage representation grows.

```
fn pad_to(coeffs: &[F; d + 1], n: usize) -> [F; n] {
    // Precondition: n >= d + 1
    let mut padded: [F; n] = [F::ZERO; n]
    for i in 0..d + 1 {
        padded[i] = coeffs[i]
    }
    return padded
}
```

For NTT multiplication, the target `n` is `next_power_of_2(deg_a + deg_b + 1)`:

```
fn next_power_of_2(x: usize) -> usize {
    let mut n: usize = 1
    for _i in 0..64 {
        if n >= x { break }
        n = n * 2
    }
    return n
}
```

### 9.6 Coefficient Scaling Convention

When a continuous-domain polynomial must be represented as a field polynomial,
the coefficients are scaled to integers before entering the field. This avoids
the quantization boundary problem where `floor()`/`round()` destroys polynomial
structure.

**Standard procedure**:

Given `f(t) = a_0 + a_1*t + a_2*t^2 + ... + a_d*t^d` with real coefficients:

1. Choose a scale factor `S` such that `S * a_i` is an integer for all `i`.
   In practice, `S` is the LCM of the denominators when the `a_i` are expressed
   as exact rationals, or a power of 10 when working from decimal notation.

2. Define the scaled polynomial: `g(t) = S*a_0 + S*a_1*t + ... + S*a_d*t^d`.

3. Reduce each coefficient modulo `p`: `h(t) = (S*a_0 mod p) + (S*a_1 mod p)*t + ...`.
   For negative scaled coefficients, use the additive inverse: if `S*a_i < 0`,
   the field coefficient is `p - (|S*a_i| mod p)`.

4. The field polynomial `h` is the representation. The receiver divides by `S`
   after decompression to recover the original scale.

**Language syntax**:

```
// f(t) = 120.0 + 0.05t - 0.0001t^2
// Scale by S = 10000:
// g(t) = 1200000 + 500t - t^2
// In Field<4294967291>: -1 mod p = p - 1
let reading = poly<Field<4294967291>>(1200000, 500, 4294967290)
```

**Standard library function**:

```
fn poly_from_scaled<F: Field, const D: usize>(
    coeffs: [i64; D + 1]
) -> Poly<F, D> {
    let mut fc: [F; D + 1]
    for i in 0..=D {
        if coeffs[i] >= 0 {
            fc[i] = F::from(coeffs[i] as u64)
        } else {
            fc[i] = F::ZERO - F::from((-coeffs[i]) as u64)
        }
    }
    return Poly::from_standard(fc)
}
```

**Invariant**: if the original `a_i` are exact rationals and `S` is chosen
correctly, then `h(t)` evaluates to `S * f(t) mod p` for all `t`. No rounding
occurs at any step. The polynomial structure is preserved exactly.

**When scaling is not possible**: if the coefficients are irrational or known
only to finite precision (e.g., from a curve fit), the programmer must choose
`S` large enough that the rounding error in `round(S * a_i)` is acceptable.
The spec does not define a tolerance — this is an application-level decision.
For lossless compression, the coefficients must be exact integers in the field.

---

## 10. Piecewise Polynomial Semantics

> **Provided by the standard library**, not the language. There is no
> `Piecewise`/`Segment` type (§3.4) and no `segment()` builtin (§5.6). The
> piecewise codec lives in `src/lib/codec/` (`piecewise_core.tv`,
> `piecewise_segment_{a,b,c}.tv`, `piecewise_wire.tv`) and operates on plain
> arrays. Its real segmenter is **cost-optimized** — dynamic-programming /
> greedy / particle-scan strategies that minimize an encoded-size objective —
> not the "analyze then binary-search the longest prefix" sketch shown in this
> section, which is kept as a conceptual illustration. The block/wire details
> in §10.5 are corrected to the shipped format.

### 10.1 Construction

```
let pw = segment(data, max_degree: 2, max_error: 0)
```

The segmentation algorithm:
1. Position cursor at index 0.
2. Run `analyze(data[cursor..], max_degree)`.
3. If polynomial detected with degree d:
   a. Binary search for the longest prefix where the polynomial exactly
      reconstructs the data (for max_error = 0).
   b. Record segment: (start=cursor, len=prefix_len, coeffs).
   c. Advance cursor by prefix_len.
4. If no polynomial detected:
   a. Record a literal segment: (start=cursor, len=1, data[cursor]).
   b. Advance cursor by 1.
5. Repeat until cursor reaches end of data.

### 10.2 Evaluation

```
fn eval(pw: &Piecewise<F, d>, index: usize) -> F {
    // Binary search for the segment containing index
    let seg = find_segment(pw, index)
    let local_index = index - seg.start
    return eval_poly(seg.coeffs, local_index)
}
```

### 10.3 Compression Ratio

For a piecewise polynomial with `k` segments, each of degree at most `d`:
- Compressed size: `k * (d + 1 + overhead)` coefficients + segment boundaries
- Raw size: `n` field elements
- Ratio: `k * (d + 3) / n` approximately (overhead = start + len per segment)

For data that is piecewise polynomial with long segments, this approaches the
per-segment polynomial ratio. For data that is entirely non-polynomial, every
element is its own segment and the ratio approaches 1.0 + overhead.

### 10.4 Edge Cases

- Input length 0: returns empty Piecewise (0 segments).
- Input length 1: returns Piecewise with 1 constant segment.
- All data identical: returns Piecewise with 1 constant segment (degree 0).
- Perfectly polynomial data: returns Piecewise with 1 segment.
- Completely random data: returns Piecewise with n literal segments.

### 10.5 Piecewise Constant Detection Fast Path

> **Does not match the shipped codec.** The actual `src/lib/codec/` wire format
> has block types `LITERAL`, `POLY_RESIDUAL`, `CONSTANT`, `CONT` (continuation),
> and `PREDICTIVE` — there is **no** `PIECEWISE_CONST` run-length-staircase block
> and no `constant_runs` pass. A constant segment is just a degree-0 `CONSTANT`
> block chosen by the cost-optimizing segmenter (§10, intro). The `constant_runs`
> algorithm and `[PIECEWISE_CONST]` layout below are an illustrative fast path,
> retained for intuition, not the implemented format.

After polynomial analysis fails to find structure, the segmenter SHOULD scan
for runs of identical values. This catches the quantization staircase pattern
— where a continuous polynomial has been truncated to integers — with O(n)
work and no field arithmetic.

**Algorithm**:

```
fn constant_runs(data: &[F]) -> Vec<Segment> {
    let mut segs = []
    let mut i = 0
    for _step in 0..data.len() {
        if i >= data.len() { break }
        let val = data[i]
        let start = i
        for _scan in 0..data.len() {
            if i >= data.len() { break }
            if data[i] != val { break }
            i += 1
        }
        segs.push(Segment { start, len: i - start, degree: 0, coeffs: [val] })
    }
    return segs
}
```

**When to use the fast path**:

The fast path is invoked when `analyze()` returns `None` (no polynomial
detected) and the data contains at least 2 distinct constant runs each of
length >= 3. If the piecewise constant representation is smaller than the
literal representation, it is used.

**Wire format for piecewise constant blocks**:

Each block is encoded as a sequence of (value, run_length) pairs.
Per-width layouts:

```
8-bit:   [PIECEWISE_CONST:1] [n_total:1] [n_segments:1] {[value:1][run_length:1]}*
16-bit:  [PIECEWISE_CONST:1] [n_total:2] [n_segments:2] {[value:2][run_length:2]}*
32-bit:  [PIECEWISE_CONST:1] [n_total:2] [n_segments:2] {[value:4][run_length:2]}*
```

The block type is smaller than literal when:
`overhead + n_segments * (value_width + length_width) < n_total * value_width`

**Staircase example**:

```
Input:  120 120 120 120 120 120 120 120 120 120 120 121 121 ...
        |<-------- run 1: val=120, len=11 -------->|<- run 2 ->|

Segments: [(120, 11), (121, 1), (120, 21), (121, 1), ...]
~7 segments instead of 200 literal bytes
```

This is the intermediate compression strategy for legacy sensor pipelines that
produce quantized integers. It does not achieve the 40:1 ratios of field-native
polynomials, but it significantly outperforms literal fallback on staircase data.

---

## 11. Stream Semantics

> **Provided by the standard library**, not the language. There is no
> compiler-known `Stream<F>` type (§3.5). The continuation state machine
> described here is realized in the codec and `src/lib/util/stream_protocol.tv`
> (`cont_active`, `check_continuation`, `advance_register`, `build_register_state`)
> on top of the compiler's `Register<F, d>` primitive (§17.2) and plain arrays.
> This section specifies that protocol's semantics.

### 11.1 Stream State Machine

A stream has three states:
1. **Uninitialized**: no polynomial loaded. `active = false`.
2. **Active**: polynomial loaded, registers hold continuation state. `active = true`.
3. **Stale**: previous polynomial exhausted or non-polynomial data encountered.
   `active = false`.

Transitions:
```
Uninitialized --[encode polynomial block]--> Active
Active --[next block matches continuation]--> Active (emit CONTINUE)
Active --[next block doesn't match]-------> Active (emit new polynomial, reset regs)
Active --[non-polynomial block]-----------> Stale
Stale --[polynomial block]----------------> Active
```

### 11.2 Continuation Check

When in Active state, the encoder checks if the next block matches the predicted
continuation:

```
fn check_continuation(state: &Stream<F>, data: &[F]) -> bool {
    if !state.active: return false
    let mut reg = state.reg.clone()
    for i in 0..data.len() {
        for k in 0..state.degree {
            reg[k] = reg[k] + reg[k + 1]
        }
        if reg[0] != data[i]: return false
    }
    return true
}
```

This is an **exact match** — every value must match. No tolerance. Lossless.

The check is O(n * d) where n = block length, d = polynomial degree. For typical
degrees (2-3) and block sizes (128-256), this is a few hundred field operations.

### 11.3 Continuation Encoding

If continuation matches:
- Emit a CONTINUE block: 2 bytes (8-bit) or 3 bytes (16/32-bit)
- Update register state to reflect the additional n steps

If continuation doesn't match:
- Compress the block normally (analyze, encode as polynomial/literal/constant)
- Update register state from the NEW polynomial (if polynomial block)
- If literal/constant: set `active = false`

### 11.4 Register State Tracking

The register state is tracked in the **original data space** (not offset-shifted).
This ensures continuation predictions compare correctly against original values.

For a polynomial block compressed with offset mode: the encoder re-analyzes the
original (non-offset) data to derive the register state. This costs one extra
`analyze()` call but ensures correctness.

---

## 12. Memory Model

### 12.1 No Garbage Collection

All data types have stack sizes known at compile time. Heap-allocated data
(`Vec<T>`, `alloc`-ed buffers) has runtime-determined size, but the handles
that point to it (pointers, Vec structs, slices) have fixed compile-time size.

| Type | Size (bytes) |
|---|---|
| `Field<p>` where p <= 256 | 1 |
| `Field<p>` where p <= 65536 | 2 |
| `Field<p>` where p <= 2^32 | 4 |
| `Field<p>` where p <= 2^64 | 8 |
| `BinField<k, _>` | ceil(k / 8) |
| `Poly<F, d>` | (d + 1) * sizeof(F) |
| `Register<F, d>` | (d + 1) * sizeof(F) |
| `[F; n]` | n * sizeof(F) |
| `*T` (pointer) | sizeof(usize) |
| `Vec<T>` (stdlib) | sizeof(*T) + 8 (pointer + two i32) |
| `Str` (stdlib) | sizeof(*u8) + 8 (pointer + two i32) |
| string literal | sizeof(usize) (raw `ptr`, no carried length — §3.14) |
| `i128`/`u128` | 16 |
| `i256`/`u256` | 32 |

Planned (not implemented in v0.1.0): `&[T]` slice and `str` as a `2 * sizeof(usize)`
fat pointer (§3.6, §3.14); `Stream<F>` (§3.5).

### 12.2 Stack Allocation

All local variables, arrays, and polynomials are stack-allocated by default.
The compiler calculates the total stack frame size for each function at compile
time. If the frame exceeds a configurable threshold (default: 1 MB), the
compiler emits a warning.

### 12.3 Heap Allocation

Heap allocation provides runtime-sized memory for data structures whose size
is not known at compile time: AST nodes, token lists, output buffers,
piecewise segment arrays, and any growable collection.

**Allocation primitives** are compiler **builtins** (`alloc`, `realloc`, `free`
— not a `std::mem` module; §13.6). The element size is derived from the
assignment-target pointer type, so the count is in *elements*, not bytes.

```
fn alloc(count) -> *T
```

Allocates `count * sizeof(T)` bytes (via `malloc`) and **zero-fills them**
(`llvm.memset`). Returns a raw pointer to the first element.

**Sizing is destination-typed and refusal-backed.** `T` is taken from the
pointer-typed destination the allocation flows into — a binding annotation
(`let p: *T = alloc(n)`), an assignment target, a parameter/field/return
type, or a pointer-target cast (`alloc(n) as *T`; both spellings size
identically). An allocation with **no pointer-typed destination is a
compile-time error** — the count would be uninterpretable. Raw-byte
allocations are spelled with a `*u8` destination.

> **NOT YET IMPLEMENTED (v0.1.0).** There is **no** out-of-memory check: on
> allocation failure `malloc` returns null and that null is returned unchecked
> (no trap). Also note the returned memory is **zero-filled**, not
> uninitialized — an earlier revision said "uninitialized," which was wrong.

```
fn realloc(ptr, new_count) -> *T
```

Resizes a previously `alloc`-ed buffer to `new_count` elements (via libc
`realloc`). Existing contents are preserved; **new bytes beyond the old size are
uninitialized** (unlike `alloc`, `realloc` does not zero-fill). Returns a new
pointer (may differ from `ptr`); the old pointer is invalid after `realloc`
returns. As with `alloc`, there is **no OOM check**.

**Implemented form** (bootstrap + `tvc_self`): like `alloc`, the builtin takes
the element count and derives `sizeof(T)` from the pointer context type, so the
call site is `p = realloc(p, new_count)` (the `old_count` is not needed — the
underlying allocator tracks the prior size). New bytes beyond the old size are
uninitialized. The compiler's own growable arenas (AST nodes, args/string
pools, IR buffers) are built on this primitive: they double capacity via
`realloc` on demand rather than capping program size.

```
fn free<T>(ptr: *T)
```

Deallocates a previously `alloc`-ed or `realloc`-ed buffer. `free(null)` is a
no-op (libc's guarantee; Traveler adds no check). Double-free is undefined
behavior. Accessing memory through `ptr` after `free` is undefined behavior.

**Ownership discipline**: The language provides no automatic lifetime
management for heap memory, and none is planned — destructors, GC, and
borrow checking are refused (hidden scope-exit calls are invisible control
flow). The programmer matches every `alloc` with exactly one `free` (or
`realloc`, which invalidates the old pointer AND every pointer derived from
it — code that holds element pointers across growth is wrong; hold indices
instead). Structured patterns over the primitives: `Vec<T>`/`Str` (owned,
freed by their free functions), library arena (wholesale free) and
handle-based pool types (`src/lib/mem/arena.tv`, `src/lib/mem/pool.tv` —
§13.4), plus a `defer` statement for exit-path cleanup (§6.8).

**Opt-in checked allocations (`--alloc-debug`).** Off by default; default
output is unchanged. With the flag, every `alloc`/`realloc` result carries a
16-byte header (magic + payload size) and a 16-byte trailer canary, and
`free`/`realloc` verify both before touching the allocator: a smeared canary
aborts with a named message (`alloc-debug: trailer canary smeared`), and
freeing a pointer that carries no redzone header aborts likewise
(`alloc-debug: free of non-redzone pointer`). This catches a heap overflow on
the first run, at the cost of per-allocation overhead — a debugging net, not
a model change (§14.2 keeps the C discipline).

**Evaluator checks.** The evaluator keeps a registry of live heap buffers: a
double free and a `realloc` of an already-freed buffer are refused loudly
(compile-time-style diagnostic, not UB), and an optional report counts
buffers still live at exit.

**LLVM codegen**: `alloc<T>(n)` compiles to a call to a runtime allocator
function (e.g., `malloc(n * sizeof(T))` or a custom arena). `free` compiles
to `free()`. `realloc` compiles to `realloc()`. The specific allocator is
a linker-time decision, not a language-level concern.

### 12.4 Value Semantics

All types have value semantics by default. Assignment copies the value:
```
let a: Poly<F, 2> = poly(1, 2, 3)
let b = a              // b is a copy of a; modifying b doesn't affect a
```

For large types (arrays, piecewise), pass by reference using `&`:
```
fn process(data: &[F; 1024]) -> Poly<F, 2> { ... }
```

References are borrowed pointers with lifetime constraints (like Rust borrows
but simpler — no lifetime annotations in the initial version, just lexical
scoping: a reference cannot outlive the block in which the referent is declared).

### 12.5 Alignment

Field elements are naturally aligned:
- 1-byte elements: 1-byte aligned
- 2-byte elements: 2-byte aligned
- 4-byte elements: 4-byte aligned
- 8-byte elements: 8-byte aligned

Arrays of field elements are contiguous with no padding between elements.
This enables SIMD loads/stores for vectorized operations.

---

## 13. Module System

### 13.1 Module Declaration

> **Corrected from an earlier revision.** There is no `module` declaration
> keyword and no filename-derived module name (`module` is not reserved, §2.4).
> Traveler's module system is **source inclusion**, not namespacing — see §13.2.

### 13.2 Imports (Model A — source inclusion)

```
import "path/to/file.tv";
```

`import` splices another source file into the current compilation **at lex
time**: the parser sees one merged translation unit, so imported definitions
share the global namespace (there is no `module::item` path qualification, and
no `::` module paths — `::` is only `Type::method`/`Type::CONST`, §2.11).

Semantics:
- **Transitive.** An imported file's own `import`s are pulled in recursively.
- **Diamond-dedup.** A file imported along two paths is included once.
- **Cycle-safe.** Import cycles terminate (each file is included at most once).
- A **duplicate top-level definition** across the merged unit is a diagnostic.

`import` is a `tvc_self` feature; the frozen C seed predates it. The explicit
separate-compilation path is `instantiate` + `extern` (§4.2.1, §13.3) — emit a
mangled concrete instance in one unit and declare it `extern` in another.

> **NOT YET IMPLEMENTED (planned).** Namespaced modules with `module::item`
> paths, item/glob imports (`import math::*`), and per-module privacy were the
> earlier design and are not built. The intended surface was:
>
> ```
> import math::polynomial          // planned
> import math::polynomial::Poly    // planned
> ```

### 13.3 Visibility and Export

Because a program is one merged unit (§13.2), ordinary definitions are visible
across all imported files; there is no module-private scoping. Symbol *linkage*
is controlled by export, not by `pub`:

- `#[export]` on a function gives it external (`dso_local`) linkage so another
  compilation unit can link against it; without it, monomorphized/instantiated
  symbols are `internal`.
- `instantiate name<Concrete>;` emits a concrete, externally-linkable mangled
  instance of a generic (§4.2.1).

> **`pub` is a vestige.** The `pub` keyword is accepted by the parser but is
> effectively a **no-op** — the export gate checks `#[export]`, not `pub`.
> Per-module privacy is not enforced.

### 13.4 Standard Library Modules

The standard library is a **tree of `.tv` files under `src/lib/`**, consumed
with `import "path"` (§13.2) — there is no `std::` namespace. By subsystem:

```
src/lib/core/         poly_core.tv, poly_core_generic.tv, result.tv  (the four-op kernel)
src/lib/collections/  vec.tv, string.tv, hashmap.tv         (Vec<T>, Str, HashMap)
src/lib/crypto/       ntt.tv, poseidon2.tv, poseidon2_wide.tv, merkle.tv, fri.tv,
                      plonk.tv, plonk_dyn.tv, mds_check.tv, grain_lfsr.tv
src/lib/zk/           #[zk] circuit libraries
src/lib/codec/        piecewise_*.tv                        (the piecewise codec)
src/lib/float/        ieee.tv, embed.tv, quant.tv           (float -> field)
src/lib/rns/          rns3.tv, rns4.tv, rns_dyn.tv          (RNS matmul)
src/lib/nt/           curve.tv, linrec.tv, polyfield.tv, linalg.tv, crtsolve.tv,
                      sqrt.tv                               (number theory)
src/lib/util/         wideint.tv, lnbounds.tv, stream_protocol.tv, ...
src/lib/regime/       regime_fri.tv, regime_zk.tv, runtime_circuit.tv
src/lib/genus/        genus_probe.tv, genus_classifier.tv, genus_profile.tv, ...
                      (degree-persistence / genus signature, §17.11)
src/lib/observe/      trace.tv, learn.tv, wire.tv           (signal observation/codec)
src/lib/ecc/          reed_solomon.tv, rs_core.tv, rs_errdec.tv, rs_errdec_core.tv
src/lib/dsp/          optical_compressor.tv
src/lib/features/     turning.tv, relational.tv
src/lib/fmt/          fmt.tv                                (integer/string formatting)
src/lib/fs/           fs.tv                                 (filesystem via extern "C")
src/lib/json/         json.tv, json_parse.tv
src/lib/nn/           fixed.tv, linear.tv                   (fixed-point NN kernels)
src/lib/time/         time.tv                               (clocks and deltas)
src/lib/net/          tcp.tv, unix.tv        (sockets via extern "C"; do not co-import
                                               both in one unit — duplicate definitions)
src/lib/mem/          arena.tv, pool.tv, shm.tv             (§12.3 patterns + shm)
src/lib/gpu/          agx_data.tv, agx_ffi.tv, agx_runtime.tv (AGX dispatch runtime, §18.4)
src/lib/gfx/          framebuffer.tv, pixel.tv, event.tv, backend_headless.tv,
                      wayland/  (client.tv, wl_wire.tv, backend_wayland.tv)   (§19)
```

Most library modules are `tvc_self`-only (they use `import`
and/or generics the frozen C seed lacks).

> **NOT YET IMPLEMENTED (planned).** The `std::field` / `std::poly` / `std::io` /
> `std::mem` / `std::vec` namespaced module surface below was the earlier design;
> it is superseded by the file-based tree above.

### 13.5 std::io Specification

> **The `std::io` handle API below is [planned; not implemented in v0.1.0].**
> The **implemented** I/O surface is two builtins plus raw FFI:
> - `print(expr)` — decimal print of an integer/field/wide-int/bool to stdout
>   with a trailing newline (§15.11).
> - `read_bytes(fd, buf, n) -> i64` — reads up to `n` bytes into `buf` from file
>   descriptor `fd`, lowering directly to POSIX `read`.
> - Everything else — `open`/`write`/`close`, sockets, `sockaddr_in`, etc. — is
>   reached through `extern "C"` (§13.7), binding libc/POSIX symbols directly.
>   There is no `IoHandle` type and no `io::*` module.
>
> The specification below documents the intended future `std::io`.

`std::io` provides file and stream I/O sufficient for a self-hosting compiler
to read source files, write output files, and emit diagnostics.

**Types**:

```
struct IoHandle {
    fd: i32,             // platform file descriptor
}
```

`IoHandle` wraps an OS file descriptor. It is not copyable. The programmer
is responsible for closing handles to avoid descriptor leaks.

**Functions**:

```
// Open a file. mode: IO_READ (0) or IO_WRITE (1) or IO_READWRITE (2).
// Returns null on failure (file not found, permission denied).
fn io::open(path: str, mode: u8) -> *IoHandle

// Read up to n bytes into buf. Returns number of bytes actually read.
// Returns 0 at end of file.
fn io::read(h: *IoHandle, buf: *u8, n: usize) -> usize

// Write n bytes from buf. Returns number of bytes actually written.
fn io::write(h: *IoHandle, buf: *u8, n: usize) -> usize

// Close a handle. The handle is invalid after this call.
fn io::close(h: *IoHandle)

// Standard streams (always open, never close these):
fn io::stdout() -> *IoHandle
fn io::stderr() -> *IoHandle
fn io::stdin() -> *IoHandle

// Read entire file into a new Vec<u8>. Convenience function.
// Caller must free the returned Vec.
fn io::read_file(path: str) -> Vec<u8>

// Write entire slice to a file. Creates or truncates the file.
fn io::write_file(path: str, data: &[u8]) -> bool
```

**Constants**:

```
const IO_READ: u8 = 0
const IO_WRITE: u8 = 1
const IO_READWRITE: u8 = 2
```

**Print**:

`print(expr)` is syntactic sugar for converting the expression to its decimal
string representation and writing it to stdout followed by a newline. It is
defined for integer types, field types (prints the integer representative),
and `bool` (prints `true` or `false`).

For formatted output beyond `print`, use `io::write` with a manually
constructed `Vec<u8>` buffer.

**LLVM codegen**: `io::open` maps to `open()`, `io::read` maps to `read()`,
`io::write` maps to `write()`, `io::close` maps to `close()`. On platforms
without POSIX (future), the implementation may differ. The function signatures
are the language-level contract; the mapping to syscalls is a codegen decision.

### 13.6 std::mem Specification

> **The `mem::*` namespace is [planned; not implemented in v0.1.0].** The
> implemented heap primitives are the bare builtins `alloc`, `realloc`, `free`
> (§12.3) — there is no `mem::` module. `mem::copy`, `mem::zero`, `mem::size_of`,
> and `mem::align_of` are **not** implemented as builtins (copy/zero via an
> explicit loop or an `extern "C"` `memcpy`/`memset`). The intended surface
> follows.

`std::mem` provides the heap allocation primitives described in Section 12.3:

```
fn mem::alloc<T>(count: usize) -> *T
fn mem::realloc<T>(ptr: *T, old_count: usize, new_count: usize) -> *T
fn mem::free<T>(ptr: *T)

fn mem::copy<T>(src: *T, dst: *T, count: usize)    // memcpy
fn mem::zero<T>(dst: *T, count: usize)              // memset to 0
fn mem::size_of<T>() -> usize                       // compile-time sizeof
fn mem::align_of<T>() -> usize                      // compile-time alignof
```

`mem::copy` copies `count` elements from `src` to `dst`. Source and
destination must not overlap (use `mem::move` for overlapping ranges,
reserved for future specification). Both pointers must be valid for
`count * sizeof(T)` bytes.

`mem::zero` sets `count * sizeof(T)` bytes at `dst` to zero.

`mem::size_of<T>()` and `mem::align_of<T>()` are compile-time intrinsics.
They produce `usize` constants known at compile time.

### 13.7 Foreign Function Interface (`extern "C"`)

`extern "C"` binds an arbitrary C/POSIX symbol by declaring its signature; the
symbol is resolved by the linker.

```
extern "C" fn malloc(n: i64) -> *u8;
extern "C" fn write(fd: i32, buf: *u8, n: i64) -> i64;
```

This is the systems-programming escape hatch — it is how the standard library
and downstream programs reach libc, POSIX I/O, and sockets. There is **no**
struct-layout sugar for C structs: a C `struct` is passed as a raw byte buffer
(`*u8`) hand-built to the target ABI (e.g. `sockaddr_in` for sockets). There is
no `net`/socket standard library; sockets, IP packet formation, and file I/O
beyond `print`/`read_bytes` are reachable today only through `extern "C"`.

`extern` composes with `instantiate` (§4.2.1) for cross-compilation-unit
linking of monomorphized generics.

---

## 14. Error Model

### 14.1 Compile-Time Errors

Every diagnostic carries `file:line:col`, an echoed source line, and a caret;
the parser recovers to sync points (many per run) and exits non-zero. Implemented
compile-time errors include:

| Condition | Error message |
|---|---|
| Non-prime field parameter | `Field<n>: n is not prime` |
| Reducible BinField polynomial | `BinField<k, poly>: polynomial is reducible over GF(2)` |
| BinField degree out of range | `BinField degree must be in [2, 63]` (§3.2.2) |
| Type mismatch across fields | `cannot add Field<251> and Field<65521>` |
| `%` on field type | `modulo operator not defined for field types` |
| `/` or `%` on a wide integer | `division is not supported on i128/i256` (§3.1) |
| Degree mismatch in static poly | `expected Poly<F,2>, found Poly<F,3>` |
| Call-argument arity mismatch | under- or over-arity at every call site |
| Duplicate top-level definition | across the merged import unit (§13.2) |
| Returning the address of a local | dangling by construction; refused with source position (a pointer into caller-owned or heap pointees stays legal) |
| `free` of a non-pointer | refused with source position |
| `defer` off the function spine | `defer` inside `if`/`for`/`while`/`match` is refused (§6.8) |

> **Corrected — these are NOT diagnosed in v0.1.0** (an earlier revision listed
> them): a native `while` loop (it is supported, §6.5); a non-exhaustive `match`
> (no exhaustiveness check, §6.7); division by a compile-time-zero divisor (§5.2);
> and pointer access / casts "outside `unsafe`" (there is no `unsafe`
> construct, §14.3). A `Poly<F, d>` with `d >= F::ORDER` is likewise not
> currently warned.

### 14.2 Runtime Errors

| Condition | Behavior (v0.1.0) |
|---|---|
| Division by zero (field) | **No trap** — `a * inv(0)` = 0, silent (§5.2, §16.11) |
| Array / pointer index out of bounds | **No check** — out-of-bounds access is UB |
| `vec_pop` on empty | **No check** — underflows `len`, UB |
| Heap allocation failure | **No check** — null returned unchecked (§12.3) |
| Stack overflow | Trap (OS-level) |
| Null pointer dereference | Undefined behavior (no runtime check) |
| Use after free | Undefined behavior |
| Double free | Undefined behavior |

> **Corrected from an earlier revision.** The field division-by-zero trap,
> array/`Vec` bounds-check traps, empty-pop trap, OOM trap, `DynPoly::into_static`
> trap (no `DynPoly`, §3.3.2), and "fuel exhaustion" (no fuel construct, §6.5)
> are **not** implemented. Traveler currently follows the C discipline: these are
> the programmer's responsibility. Guarding traps are a planned hardening item.

Two **opt-in** nets ship today and leave the default table above unchanged:
`--alloc-debug` redzones in compiled output, and the evaluator's heap-buffer
registry (double free / realloc-of-freed refused loudly) — both in §12.3.

### 14.3 The Unsafe Surface (no `unsafe` construct)

> **DECIDED (v0.1.x): there is no `unsafe { ... }` construct and none is
> planned.** `unsafe` remains a reserved word (§2.4) but will not become a
> block form. Earlier revisions sketched one as "the intended future
> surface"; that sketch is withdrawn. Rationale: an `unsafe` block earns its
> keep by locally ELIDING checks that exist everywhere else. Traveler's
> memory model (§3.12, §12.3) puts its checks at different points — sizing
> and formation at compile time, structure (arenas/handles/`defer`) for
> lifetimes, and opt-in dynamic nets — so a block-scoped elision marker
> would gate nothing and communicate a safety boundary the language does
> not draw there.

The actual unsafe surface is small and NAMED, not block-scoped:

- **`n as *T`** — the pointer forge (`inttoptr`): the FFI/membrane floor
  (mmap'd regions, device registers). All obligations are the
  programmer's: validity, alignment, lifetime, aliasing (§3.12).
- **`p as *U`** — pointer reinterpretation; element sizing follows `*U`.
- **`extern "C"`** — foreign code with foreign invariants (§13.5).
- **Unchecked operations** — indexing bounds, use-after-free, double-free,
  null dereference: undefined behavior per §14.2, everywhere, uniformly.

The programmer's obligations at these doors:
- Not dereferencing null or dangling pointers
- Not double-freeing heap memory
- Not reading uninitialized memory (`realloc` growth bytes, §12.3)
- Not holding derived pointers across `realloc` (hold indices instead)
- Keeping forged pointers within genuinely owned, live regions

---

## 15. LLVM Code Generation

### 15.0 Compilation Model

> **Scope of this section.** The IR patterns below are illustrative and, for
> several subsections, describe algorithms that are **provided by the standard
> library** or are **planned**, not compiler builtins — specifically §15.12
> (`std::io`, planned — §13.5), §15.13 (`segment`, library — §5.6, §10), §15.14
> (`Stream`, library — §11), §15.15 (polynomial multiplication, library — §9.4),
> and §15.16 (NTT, library — §9.5). Those library routines still *compile*
> through the field-arithmetic and control-flow codegen shown here; the
> per-feature status markers in §5, §9–§11, and §13 govern what is builtin. The
> major implemented codegen features not covered by their own subsection below —
> monomorphization, closures, dynamic/wide fields, the `#[zk]`/PLONK backend,
> and `extern "C"` — are summarized in §15.19. Middle-end optimization
> profiles, target-selection modes (including `-target tpc`), and GPU/AGX
> device emission are specified in §18; the graphics library surface in §19.

Traveler is **self-hosting**. The canonical compiler is `tvc_self.tv` —
the Traveler compiler written in Traveler. It compiles `.tv` source to
LLVM IR, and it compiles itself: Stage 1 (a seed compiler builds
`tvc_self`), Stage 2 (`tvc_self` compiles `tvc_self`), Stage 3
(`tvc_self2` compiles `tvc_self`). Stage 2 and Stage 3 emit
byte-identical IR — the self-hosting fixed point.

The bootstrap C compiler `tvc.c` is a **historical seed**, frozen as of
Phase 6. Its sole remaining purpose is to build Stage 1 of `tvc_self`
from a C toolchain. The two compilers are at exact core-semantic parity:
they emit equivalent IR for the full language, including the cryptographic
stack (NTT, Poseidon2, Merkle, FRI, PLONK) and the `#[zk]` backend, as
verified by the dual-parity suite (`tests/run_dual.sh`).

**New language features land in `tvc_self.tv` only.** `tvc.c` is not
extended further. This is a one-way decision: parity has been
established, the fixed point holds, and the seed has served its purpose.
Where this specification describes code generation, the canonical
behavior is whatever `tvc_self.tv` emits; `tvc.c` is retained for
reproducibility of the bootstrap chain, not as a second source of truth.

### 15.1 Field Arithmetic → LLVM IR

#### Field<251> (u8 element, u16 intermediate)

**Addition**:
```llvm
define i8 @field251_add(i8 %a, i8 %b) {
    %a16 = zext i8 %a to i16
    %b16 = zext i8 %b to i16
    %sum = add i16 %a16, %b16
    %ge  = icmp uge i16 %sum, 251
    %sub = sub i16 %sum, 251
    %res = select i1 %ge, i16 %sub, i16 %sum
    %out = trunc i16 %res to i8
    ret i8 %out
}
```

**Subtraction**:
```llvm
define i8 @field251_sub(i8 %a, i8 %b) {
    %a16 = zext i8 %a to i16
    %b16 = zext i8 %b to i16
    %d   = add i16 %a16, 251
    %d2  = sub i16 %d, %b16
    %ge  = icmp uge i16 %d2, 251
    %sub = sub i16 %d2, 251
    %res = select i1 %ge, i16 %sub, i16 %d2
    %out = trunc i16 %res to i8
    ret i8 %out
}
```

**Multiplication**:
```llvm
define i8 @field251_mul(i8 %a, i8 %b) {
    %a16  = zext i8 %a to i16
    %b16  = zext i8 %b to i16
    %prod = mul i16 %a16, %b16
    %rem  = urem i16 %prod, 251
    %out  = trunc i16 %rem to i8
    ret i8 %out
}
```

#### Field<65521> (u16 element, u32 intermediate)

Same pattern, `i16` → `i32`, prime = 65521.

#### Field<4294967291> (u32 element, u64 intermediate)

Same pattern, `i32` → `i64`, prime = 4294967291.

#### Field<18446744073709551557> (u64 element, u128 intermediate)

**Addition** (note: both add and sub require `i128` because
`max(a + b) = 2*(p-1) > 2^64`):

```llvm
define i64 @field64_add(i64 %a, i64 %b) {
    %a128 = zext i64 %a to i128
    %b128 = zext i64 %b to i128
    %sum  = add i128 %a128, %b128
    %ge   = icmp uge i128 %sum, 18446744073709551557
    %sub  = sub i128 %sum, 18446744073709551557
    %res  = select i1 %ge, i128 %sub, i128 %sum
    %out  = trunc i128 %res to i64
    ret i64 %out
}
```

**Multiplication** (Barrett reduction — the *actual* emitted form; **not**
`urem i128`):

```llvm
; (a * b) mod p via Barrett with a compile-time m = floor(2^128 / p),
; computed through an i256 intermediate so no i128 division is emitted:
define i64 @field64_mul(i64 %a, i64 %b) {
    ; prod = a*b (i128); q = (prod_wide * m) >> 128 (i256 multiply-high);
    ; r = prod - q*p; one conditional subtract; trunc to i64
    ...
    ret i64 %out
}
```

**Why Barrett, not `urem`.** LLVM supports `i128` natively, and on x86-64
`mul i128` lowers to `MUL r/m64` (128-bit result in `rdx:rax`). But `urem i128`
has no single-instruction counterpart — it lowers to a `__udivti3`/`__udivei4`
**software-division libcall**, which the C-free trust chain cannot link (macOS
provides `__udivti3`; Linux/LLVM≤15 needs `__udivei4`, which no shipped libgcc
supplies — see known-issues #15). So for 64-bit primes the compiler **always
emits Barrett** reduction: precompute `m = floor(2^128 / p)` at compile time,
`q = (prod * m) >> 128` (via an `i256` intermediate), `r = prod - q*p`, one
conditional subtract. On AArch64 `umulh` makes the multiply-high especially
cheap. Primes up to 2^32 still use a single hardware `urem` in the intermediate
type; wider (>2^64) primes use the runtime wide-field Barrett path (§16.16).

#### BinField<8, 0x11B> (u8 element)

**Addition**:
```llvm
define i8 @gf256_add(i8 %a, i8 %b) {
    %out = xor i8 %a, %b
    ret i8 %out
}
```

**Multiplication** (log/exp table — the legacy `GF(2^8, 0x11B)` path). This
specific field keeps a precomputed table; **every other** `BinField<k, poly>`
uses a generic carry-less shift-and-XOR routine `@bf<k>_<poly>_mul` instead
(§3.2.2, §8.2). No `PCLMULQDQ` is emitted on any path.
```llvm
define i8 @gf256_mul(i8 %a, i8 %b) {
    ; log/exp table lookup for GF(2^8, 0x11B)
    %a_idx = zext i8 %a to i64
    %b_idx = zext i8 %b to i64
    %log_a = load i8, i8* getelementptr([256 x i8], [256 x i8]* @log_table, i64 0, i64 %a_idx)
    %log_b = load i8, i8* getelementptr([256 x i8], [256 x i8]* @log_table, i64 0, i64 %b_idx)
    %log_sum = add i8 %log_a, %log_b
    ; Modular reduction of log sum (mod 255)
    %ge255  = icmp uge i8 %log_sum, 255
    %reduced = sub i8 %log_sum, 255
    %log_res = select i1 %ge255, i8 %reduced, i8 %log_sum
    %exp_idx = zext i8 %log_res to i64
    %result  = load i8, i8* getelementptr([256 x i8], [256 x i8]* @exp_table, i64 0, i64 %exp_idx)
    ; Handle zero inputs: if a == 0 || b == 0, return 0
    %a_zero = icmp eq i8 %a, 0
    %b_zero = icmp eq i8 %b, 0
    %either_zero = or i1 %a_zero, %b_zero
    %out = select i1 %either_zero, i8 0, i8 %result
    ret i8 %out
}
```

### 15.2 Forward Summation Loop → LLVM IR

For `eval(poly, 0..n)` with `Poly<Field<251>, 2>`:

```llvm
; poly = [c0, c1, c2], degree = 2, n values
define void @eval_poly251_deg2(i8* %out, i8 %c0, i8 %c1, i8 %c2, i32 %n) {
entry:
    store i8 %c0, i8* %out
    br label %loop

loop:
    %i   = phi i32 [1, %entry], [%i_next, %loop]
    %r0  = phi i8 [%c0, %entry], [%r0_new, %loop]
    %r1  = phi i8 [%c1, %entry], [%r1_new, %loop]
    ; r0 += r1
    %r0_new = call i8 @field251_add(i8 %r0, i8 %r1)
    ; r1 += c2 (r2 is constant for degree 2)
    %r1_new = call i8 @field251_add(i8 %r1, i8 %c2)
    ; store output
    %ptr = getelementptr i8, i8* %out, i32 %i
    store i8 %r0_new, i8* %ptr
    ; loop control
    %i_next = add i32 %i, 1
    %done   = icmp eq i32 %i_next, %n
    br i1 %done, label %exit, label %loop

exit:
    ret void
}
```

**Optimization note**: For degree <= 3, the compiler unrolls the inner
register update loop (eliminating the inner `for k` loop). For degree > 3,
the inner loop is emitted as a counted loop.

**SIMD opportunity**: The forward summation inner loop (`reg[k] += reg[k+1]`)
is a shift-and-add pattern that can be vectorized for wide registers when
processing multiple independent streams simultaneously.

### 15.3 NTT → LLVM IR

NTT is emitted as a series of butterfly operations. For a length-n NTT:
- log2(n) stages
- n/2 butterflies per stage
- Each butterfly: 1 field mul + 1 field add + 1 field sub

The compiler emits the bit-reversal permutation as a compile-time-computed
index array, and the twiddle factors as compile-time-computed constants
(since the NTT size and root of unity are known at compile time).

### 15.4 Module Structure

Every `.ll` file emitted by the compiler begins with:

```llvm
; Traveler compiler output
target triple = "TRIPLE"
target datalayout = "DATALAYOUT"

; String constant for print
@.fmt_u = private constant [4 x i8] c"%u\0A\00"

; External declarations
declare i32 @printf(ptr, ...)
declare ptr @malloc(i64)
declare ptr @realloc(ptr, i64)
declare void @free(ptr)
declare i64 @read(i32, ptr, i64)
declare i64 @write(i32, ptr, i64)
declare i32 @open(ptr, i32, ...)
declare i32 @close(i32)
```

`TRIPLE` and `DATALAYOUT` are platform-specific. Examples:
- arm64 macOS: `target triple = "arm64-apple-darwin"`
- x86_64 Linux: `target triple = "x86_64-unknown-linux-gnu"`

The compiler determines these from the host platform or a `--target` flag.

### 15.5 Function Calls and Compound Types

**Scalar types** (field elements, integers, bool) pass in registers:

```llvm
define i8 @cube_activate(i8 %x) {
    %t0 = call i8 @field251_mul(i8 %x, i8 %x)
    %t1 = call i8 @field251_mul(i8 %t0, i8 %x)
    ret i8 %t1
}
```

**Compound types** (Poly, structs, Vec) pass by pointer. The caller allocates
stack space and passes a pointer. The callee reads/writes through the pointer.

```
// Traveler:
fn dot(a: &[F; 4], b: &[F; 4]) -> F { ... }
```

```llvm
; a and b are pointers to [4 x i8] on the caller's stack
define i8 @dot(ptr %a, ptr %b) {
entry:
    %sum = alloca i8
    store i8 0, ptr %sum
    br label %loop

loop:
    %i = phi i64 [0, %entry], [%i.next, %loop.body]
    %done = icmp eq i64 %i, 4
    br i1 %done, label %exit, label %loop.body

loop.body:
    %a.ptr = getelementptr i8, ptr %a, i64 %i
    %b.ptr = getelementptr i8, ptr %b, i64 %i
    %a.val = load i8, ptr %a.ptr
    %b.val = load i8, ptr %b.ptr
    %prod = call i8 @field251_mul(i8 %a.val, i8 %b.val)
    %cur = load i8, ptr %sum
    %new = call i8 @field251_add(i8 %cur, i8 %prod)
    store i8 %new, ptr %sum
    %i.next = add i64 %i, 1
    br label %loop

exit:
    %result = load i8, ptr %sum
    ret i8 %result
}
```

**Vec<T>** is passed as a pointer to `{ ptr, i64, i64 }`:

```llvm
; Vec<u8> layout: { data: ptr, len: i64, cap: i64 }
%Vec = type { ptr, i64, i64 }
```

### 15.6 Heap Allocation

`alloc<T>(n)` compiles to a `@malloc` call with size `n * sizeof(T)`:

```llvm
; let buf: *u8 = alloc<u8>(1024)
%size = mul i64 1024, 1            ; 1024 * sizeof(u8) = 1024
%buf = call ptr @malloc(i64 %size)
; trap if null:
%is_null = icmp eq ptr %buf, null
br i1 %is_null, label %oom, label %ok

oom:
    call void @abort()
    unreachable

ok:
    ; use %buf
```

`free(ptr)` compiles to `@free`:

```llvm
call void @free(ptr %buf)
```

`realloc(ptr, old, new)` compiles to `@realloc` with new byte size:

```llvm
%new_size = mul i64 %new_count, 1   ; new_count * sizeof(T)
%new_ptr = call ptr @realloc(ptr %old_ptr, i64 %new_size)
%is_null = icmp eq ptr %new_ptr, null
br i1 %is_null, label %oom, label %ok
```

### 15.7 Vec Operations

**Vec::push** inlines as: check capacity, realloc if needed, store element,
increment length.

```llvm
; v.push(val) where v: Vec<u8>, val: i8
; %v is a pointer to %Vec = { ptr, i64, i64 }

; Load len and cap
%len.ptr = getelementptr %Vec, ptr %v, i32 0, i32 1
%cap.ptr = getelementptr %Vec, ptr %v, i32 0, i32 2
%data.ptr = getelementptr %Vec, ptr %v, i32 0, i32 0
%len = load i64, ptr %len.ptr
%cap = load i64, ptr %cap.ptr

; Check if full
%full = icmp eq i64 %len, %cap
br i1 %full, label %grow, label %store

grow:
    ; New capacity: max(cap * 2, 8)
    %cap2 = mul i64 %cap, 2
    %use8 = icmp ult i64 %cap2, 8
    %new_cap = select i1 %use8, i64 8, i64 %cap2
    %old_data = load ptr, ptr %data.ptr
    %new_data = call ptr @realloc(ptr %old_data, i64 %new_cap)
    ; (null check omitted for brevity; production code traps)
    store ptr %new_data, ptr %data.ptr
    store i64 %new_cap, ptr %cap.ptr
    br label %store

store:
    %data = load ptr, ptr %data.ptr
    %elem.ptr = getelementptr i8, ptr %data, i64 %len
    store i8 %val, ptr %elem.ptr
    %new_len = add i64 %len, 1
    store i64 %new_len, ptr %len.ptr
```

**Vec indexing** (`v[i]`): bounds check + `getelementptr` + `load`.

```llvm
; v[i] where v: Vec<u8>, i: i64
%len = load i64, ptr %len.ptr
%oob = icmp uge i64 %i, %len
br i1 %oob, label %trap, label %ok

trap:
    call void @abort()
    unreachable

ok:
    %data = load ptr, ptr %data.ptr
    %elem.ptr = getelementptr i8, ptr %data, i64 %i
    %val = load i8, ptr %elem.ptr
```

### 15.8 String Literals and Slices

A string literal compiles to a global constant byte array referenced by a raw
`ptr` (NUL-terminated). It is **not** a `{ ptr, i64 }` fat pointer — no length
is carried (§3.14):

```llvm
; let msg: *u8 = "hello"
@.str.0 = private constant [6 x i8] c"hello\00"
%msg.ptr = getelementptr [6 x i8], ptr @.str.0, i64 0, i64 0
; %msg.ptr is a raw i8* to the bytes
```

> **NOT YET IMPLEMENTED (planned).** The fat-pointer `str`/`&[T]` slice model and
> its codegen — sub-slicing (`s[a..b]`) and slice equality below — are not
> emitted in v0.1.0 (§3.6, §3.14). They document the intended slice lowering.

**Slice sub-slicing** (`s[a..b]`):

```llvm
; result = s[a..b] where s: { ptr, i64 }
; Bounds check: a <= b <= s.len
%s.len = extractvalue {ptr, i64} %s, 1
%a.ok = icmp ule i64 %a, %b
%b.ok = icmp ule i64 %b, %s.len
%both.ok = and i1 %a.ok, %b.ok
br i1 %both.ok, label %slice.ok, label %trap

slice.ok:
    %s.ptr = extractvalue {ptr, i64} %s, 0
    %new.ptr = getelementptr i8, ptr %s.ptr, i64 %a
    %new.len = sub i64 %b, %a
    ; result is { %new.ptr, %new.len }
```

**Slice equality** (`s1 == s2`):

```llvm
; First check lengths, then memcmp
%len1 = extractvalue {ptr, i64} %s1, 1
%len2 = extractvalue {ptr, i64} %s2, 1
%len.eq = icmp eq i64 %len1, %len2
br i1 %len.eq, label %cmp.bytes, label %not.equal

cmp.bytes:
    %ptr1 = extractvalue {ptr, i64} %s1, 0
    %ptr2 = extractvalue {ptr, i64} %s2, 0
    %cmp = call i32 @memcmp(ptr %ptr1, ptr %ptr2, i64 %len1)
    %eq = icmp eq i32 %cmp, 0
    br label %done

not.equal:
    br label %done

done:
    %result = phi i1 [%eq, %cmp.bytes], [false, %not.equal]
```

(Requires `declare i32 @memcmp(ptr, ptr, i64)` in the module header.)

### 15.9 Pointer Operations

**Address-of** (`&x as *T`):

```llvm
; x is in an alloca:
%x = alloca i8
store i8 42, ptr %x
; &x as *u8 is simply %x — alloca returns a pointer
```

**Dereference read** (`*ptr`):

```llvm
; unsafe { let val = *ptr }
%val = load i8, ptr %ptr
```

**Dereference write** (`*ptr = val`):

```llvm
; unsafe { *ptr = val }
store i8 %val, ptr %ptr
```

**Null check** (`ptr != null`):

```llvm
%is_null = icmp eq ptr %ptr, null
```

**Element address** (`&p[i]` — there is no `offset` method; §3.12):

```llvm
; &p[i] where T = u8
%new_ptr = getelementptr i8, ptr %ptr, i64 %n
```

For types wider than 1 byte, `getelementptr` scales by element size
automatically:

```llvm
; &p[i] where T = i32 (4 bytes)
%new_ptr = getelementptr i32, ptr %ptr, i64 %n
; Advances by i * 4 bytes
```

### 15.10 Control Flow

**For loop with break/continue**:

```llvm
; for i in 0..n { if cond { break } body; }
entry:
    %i.addr = alloca i64
    store i64 0, ptr %i.addr
    br label %loop.cond

loop.cond:
    %i = load i64, ptr %i.addr
    %done = icmp slt i64 %i, %n
    br i1 %done, label %loop.body, label %loop.end

loop.body:
    ; ... evaluate cond ...
    br i1 %cond, label %loop.end, label %loop.continue
    ; break = br to %loop.end
    ; continue = br to %loop.incr

loop.continue:
    ; ... body ...
    br label %loop.incr

loop.incr:
    %i.cur = load i64, ptr %i.addr
    %i.next = add i64 %i.cur, 1
    store i64 %i.next, ptr %i.addr
    br label %loop.cond

loop.end:
    ; after loop
```

**Match on enum tag**:

```llvm
; match node.kind { Lit => ..., Add => ..., Mul => ... }
; Enum tag is the first byte of the struct.
%tag.ptr = getelementptr i8, ptr %node, i64 0
%tag = load i8, ptr %tag.ptr
switch i8 %tag, label %match.default [
    i8 0, label %match.lit
    i8 1, label %match.add
    i8 2, label %match.mul
]

match.lit:
    ; Extract Lit payload (starts after tag + padding)
    br label %match.end

match.add:
    ; Extract Add payload: two *Expr pointers
    %lhs.ptr = getelementptr i8, ptr %node, i64 8   ; after tag + 7 padding (align 8)
    %lhs = load ptr, ptr %lhs.ptr
    %rhs.ptr = getelementptr i8, ptr %node, i64 16
    %rhs = load ptr, ptr %rhs.ptr
    br label %match.end

match.default:
    call void @abort()
    unreachable

match.end:
    ; phi nodes for result if match is an expression
```

**If/else as expression** (with phi):

```llvm
; let x = if cond { a } else { b }
    br i1 %cond, label %if.then, label %if.else

if.then:
    ; compute %a
    br label %if.end

if.else:
    ; compute %b
    br label %if.end

if.end:
    %x = phi i8 [%a, %if.then], [%b, %if.else]
```

### 15.11 Print

`print(expr)` for field and integer types:

```llvm
; print(x) where x: Field<251> (i8)
%x.ext = zext i8 %x to i32
%ignore = call i32 (ptr, ...) @printf(ptr @.fmt_u, i32 %x.ext)
```

For `bool`:

```llvm
; print(b) where b: bool
@.str.true = private constant [5 x i8] c"true\00"
@.str.false = private constant [6 x i8] c"false\00"

%sel = select i1 %b, ptr @.str.true, ptr @.str.false
%ignore = call i32 (ptr, ...) @printf(ptr %sel)
; followed by newline
```

### 15.12 std::io Codegen

`io::open` maps to POSIX `open()`:

```llvm
; let h = io::open("input.tv", IO_READ)
; IO_READ = 0 maps to O_RDONLY = 0
; IO_WRITE = 1 maps to O_WRONLY | O_CREAT | O_TRUNC = 0x601
%fd = call i32 (ptr, i32, ...) @open(ptr %path.ptr, i32 %flags)
%failed = icmp slt i32 %fd, 0
; if failed, return null IoHandle
```

`io::read` maps to POSIX `read()`:

```llvm
%n_read = call i64 @read(i32 %fd, ptr %buf, i64 %count)
```

`io::write` maps to POSIX `write()`:

```llvm
%n_written = call i64 @write(i32 %fd, ptr %buf, i64 %count)
```

`io::close` maps to POSIX `close()`:

```llvm
call i32 @close(i32 %fd)
```

`io::stdout()` returns fd 1. `io::stderr()` returns fd 2. `io::stdin()`
returns fd 0. These are compile-time constants.

### 15.13 Analyze and Segment Codegen

`analyze()` compiles to a loop nest matching the pseudocode in Section 9.3.
The key patterns:

**Iterated forward difference** (inner loop):

```llvm
; diff[i] = diff[i+1] - diff[i]  for i in 0..remaining-1
%i = phi i64 [0, %diff.entry], [%i.next, %diff.body]
%done = icmp eq i64 %i, %rem.minus1
br i1 %done, label %diff.end, label %diff.body

diff.body:
    %cur.ptr = getelementptr i8, ptr %diff, i64 %i
    %next.idx = add i64 %i, 1
    %next.ptr = getelementptr i8, ptr %diff, i64 %next.idx
    %cur = load i8, ptr %cur.ptr
    %next = load i8, ptr %next.ptr
    %sub = call i8 @field251_sub(i8 %next, i8 %cur)
    store i8 %sub, ptr %cur.ptr
    %i.next = add i64 %i, 1
    br label %diff.loop
```

**Early termination** (nonzero count check):

```llvm
; if nz == 0 { break }  — all differences are zero, polynomial found
%all_zero = icmp eq i64 %nz, 0
br i1 %all_zero, label %found, label %check.stall

; if level >= 4 && nz >= prev_nz { break }  — stall
check.stall:
    %past4 = icmp uge i64 %level, 4
    %stalled = icmp uge i64 %nz, %prev_nz
    %bail.stall = and i1 %past4, %stalled
    br i1 %bail.stall, label %not.poly, label %check.85pct

; if level >= 4 && nz > remaining * 17 / 20 { break }  — >85%
check.85pct:
    %threshold = mul i64 %remaining, 17
    %threshold.div = udiv i64 %threshold, 20
    %too.many = icmp ugt i64 %nz, %threshold.div
    %bail.85 = and i1 %past4, %too.many
    br i1 %bail.85, label %not.poly, label %continue.level
```

`segment()` dispatches to `analyze()` per segment and assembles the result
into a `Vec<Segment>`. The binary search for evaluation within a `Piecewise`
is a standard sorted-array search on segment start offsets.

### 15.14 Stream State Machine Codegen

The `Stream<F>` state machine (Section 11) compiles to a struct containing
the register array, degree, and active flag:

```llvm
; Stream<Field<251>>:
; { reg: [256 x i8], degree: i32, active: i1 }
%Stream251 = type { [256 x i8], i32, i1 }
```

**Continuation check**: predict the next block by advancing the registers,
compare against actual data. Early-exit on first mismatch.

```llvm
; for i in 0..block_len:
;     for k in 0..degree: treg[k] += treg[k+1]
;     if treg[0] != data[i]: match = false; break

check.element:
    ; Inner register update (same as forward summation)
    ; ... degree iterations of reg[k] += reg[k+1] ...

    ; Compare predicted vs actual
    %predicted = load i8, ptr %treg.0
    %actual.ptr = getelementptr i8, ptr %data, i64 %i
    %actual = load i8, ptr %actual.ptr
    %eq = icmp eq i8 %predicted, %actual
    br i1 %eq, label %check.next, label %no.match
```

**State transition**: if continuation matches, emit a 2-byte CONT block and
update the register state. If not, compress the block independently and
reset the stream state from the new polynomial.

### 15.15 Karatsuba and Schoolbook Codegen

**Schoolbook** (degree <= 64): double nested loop, all inline.

```llvm
; result[i+j] += A[i] * B[j]
%a.val = load i8, ptr %a.i.ptr
%b.val = load i8, ptr %b.j.ptr
%prod = call i8 @field251_mul(i8 %a.val, i8 %b.val)
%ij = add i64 %i, %j
%r.ptr = getelementptr i8, ptr %result, i64 %ij
%r.cur = load i8, ptr %r.ptr
%r.new = call i8 @field251_add(i8 %r.cur, i8 %prod)
store i8 %r.new, ptr %r.ptr
```

**Karatsuba**: the compiler emits a recursive function `@karatsuba_F251`
that calls itself for the three sub-products. The base case (n <= 32) inlines
the schoolbook loop. Temporary `Vec<F>` allocations use `@malloc`/`@free`.

For compile-time-known degrees, the compiler MAY specialize and unroll the
recursion entirely, producing a fixed sequence of multiplications and
additions with no runtime dispatch.

### 15.16 NTT Codegen

**Forward NTT**: the butterfly loop from Section 9.5, with twiddle factors
precomputed at compile time and stored as global constant arrays:

```llvm
; Twiddle factors for NTT size 1024 on Field<998244353>
@ntt_twiddle_1024 = private constant [512 x i32] [
    i32 1, i32 <omega^1>, i32 <omega^2>, ...
]
```

Each butterfly:

```llvm
; u = a[start + k], v = a[start + k + half] * twiddle[k]
%u = load i32, ptr %u.ptr
%v.raw = load i32, ptr %v.ptr
%tw = load i32, ptr %tw.ptr
%v = call i32 @field998244353_mul(i32 %v.raw, i32 %tw)
%sum = call i32 @field998244353_add(i32 %u, i32 %v)
%dif = call i32 @field998244353_sub(i32 %u, i32 %v)
store i32 %sum, ptr %u.ptr
store i32 %dif, ptr %v.ptr
```

**Inverse NTT**: same structure, but with `omega^(-1)` twiddle factors and a
final scaling pass multiplying every element by `n^(-1) mod p`.

### 15.17 Optimization Hints

> Whole-module middle-end pipelines are a separate, closed surface: the
> `--opt-level` profiles in §18.1. The hints below are what the compiler emits
> itself, before any profile runs.

These are not required for correctness but the compiler SHOULD implement them
when targeting release builds:

**Barrett reduction** for specific primes: replace `urem` with a
multiply-high + shift sequence. LLVM already does this for constant divisors,
but for primes near power-of-2 boundaries, a hand-written Barrett sequence
may be faster than what LLVM's generic constant-divisor optimization produces.

**Inverse lookup table** for small primes (p < 256): precompute all 255
inverses at compile time, emit as a global constant `[255 x i8]`, and replace
`fieldP_inv(a)` with a table load:

```llvm
@field251_inv_table = private constant [251 x i8] [
    i8 0,    ; inv(0) = trap (never reached)
    i8 1,    ; inv(1) = 1
    i8 126,  ; inv(2) = 126
    ...
]

; fieldP_div inlined as: table lookup + mul
%inv = load i8, getelementptr [251 x i8], ptr @field251_inv_table, i64 0, i64 %b
%result = call i8 @field251_mul(i8 %a, i8 %inv)
```

**Degree <= 3 unrolling**: for forward summation with degree known at compile
time, the inner `for k in 0..d` loop is fully unrolled:

```llvm
; Degree 2: inner loop unrolled to 2 adds
%r0.new = call i8 @field251_add(i8 %r0, i8 %r1)
%r1.new = call i8 @field251_add(i8 %r1, i8 %r2)
; r2 unchanged (constant for degree 2)
```

**Field<2> bitwise emission**: when the field type is `Field<2>`, the compiler
emits `xor` for addition and `and` for multiplication, bypassing the general
conditional-subtract/widening-multiply patterns entirely.

### 15.18 Auto-Parallelization

The compiler automatically parallelizes `for` loops when it can prove iteration
independence. This requires no syntax annotations, no pragmas, and no
user-visible API. The design principle is **default-deny**: the only failure
mode is *lost parallelism* (a loop runs sequentially), never a race. "Traveler
proves loop parallelism, period" — the proof is not limited to field loops.

**The two-part proof.** A `for i in a..b` loop is dispatched to threads only if
the analyzer proves BOTH:

1. **Element independence** — no loop-carried dependency: no value written in one
   iteration is read in another. Reads are own-cell (`a[i]`) or loop-invariant;
   there is no cross-iteration scalar accumulation or recurrence.
2. **Index-map soundness** — every array/pointer *write* index is a stride-1
   affine, injective function of `i` (`a[i]`, `a[i + c]` for loop-invariant `c`),
   so distinct iterations write distinct cells; multiple writes must be provably
   disjoint. Non-affine or non-injective write indices refuse.

**Admissible element/capture types (the "U1 lift").** Earlier the element type
had to be a **field** (field axioms guarantee no inter-element carry). As of the
U1 lift this is broadened: the loop parallelizes when its captured
arrays/elements are field or `dyn`-field carriers **or** a primitive element
from the closed set

```
i8 i16 i32 i64  u8 u16 u32 u64  bool  usize
```

(pointers to those, or scalars of those). The dispatch predicate is
`has_field || pfor_caps_elem_ok(...)`. Captures outside this set — struct
elements, multi-level `**T`, wide integers, closures — refuse with the
diagnostic tag `cap-elem`. (The old field-only gate `PFR_NOT_FIELD` is retired.)

**Integer semantics inside a parallel loop.** Per-element integer wrap is defined
(no `nsw`/`nuw` is assumed), so a wrapping computation is deterministic
regardless of thread count. Integer division is admitted under
**outcome-parity trap semantics**: a division that would trap traps identically
whether the loop ran serially or in parallel (abort-is-abort; results never
diverge).

**Runtime aliasing guard.** When the same buffer could be passed as two
arguments (`f(buf, buf)`), the compiler emits a base-pointer-equality check at
the dispatch site that falls back to **serial** execution if the pointers alias
— soundness is preserved even when the compiler cannot statically rule out
aliasing.

**Fences (kept sequential, never unsound).** A loop body containing an
**indirect call through a function pointer** (§3.10) stays sequential — the
callee is a runtime value, so purity is unprovable. A **closure** call
parallelizes only when the closure's identity is statically known and its body
is provably pure (§3.10.1). An impure call, an I/O or heap-mutating call, or an
indexed store through an unprovable index all refuse.

**What parallelizes:**

```traveler
for i in 0..n {
    out[i] = a[i] + b[i];       // element-wise (field OR primitive-int)
}

for i in 0..n {
    a[i] = field_mul(a[i], b[i]);  // pure function call on own-cell elements
}
```

**What does NOT parallelize (correctly rejected):**

```traveler
for i in 0..n {
    sum = sum + a[i];    // loop-carried dependency (accumulation)
}

for i in 0..n {
    w = w * omega;       // sequential recurrence (iteration depends on previous)
    data[i] = data[i] * w;
}

for i in 0..n {
    out[idx[i]] = a[i];  // scatter: write index not proven injective (refuse)
}
```

**Inspecting decisions (`--pfor-report`).** The read-only `--pfor-report` query
mode emits one JSON-Lines verdict record per `for` loop (parallelized or not,
with the refusal reason — e.g. `cap-elem`, loop-carried, non-injective index).
It is a pure tap: it does not change codegen.

**Function purity.** Loop bodies containing function calls are only
parallelized when the called function is proven pure. A function is pure if:

- It contains no I/O operations (print, write, read)
- It performs no heap mutation (alloc, free)
- It assigns no global variables
- All functions it calls are also pure (transitive closure)

Compiler-emitted field arithmetic functions (field_add, field_mul, etc.) are
trivially pure. Purity is computed via two-pass fixed-point iteration over
the function registry at compile time.

**Recursive proofs (authoritative for CPU dispatch).** Admission is decided by
a recursive analysis over the loop body, not a flat scan:

- **Effect summaries through calls.** Function and closure effects are
  summarized through recursive calls before admission — hidden reads, member
  calls, aggregate alias writes, and unknown effects are tracked, and an
  incomplete summary fails closed (the loop stays sequential).
- **Static call targets are carried through.** Statically known closure,
  direct, generic, trait, operator, and builtin call targets are followed into
  the callee; callee reads are mapped back onto caller captures. Unknown or
  erased targets (function pointers, §3.10) stay sequential, per the fence
  rule above.
- **Declaration-aware affine analysis.** Affine names resolve through lexical
  declarations, so shadowed variables are kept distinct; geometry checks use
  exact `i32` literals and modular arithmetic, and duplicate parameters are
  rejected on every declaration surface.
- **Fresh allocations do not overlap.** Storage allocated inside an iteration
  is proven fresh per iteration, so per-iteration temporaries cannot alias
  across workers.
- **Worker records.** The dispatch record preserves ordered captures, write
  bases, iterator width, generic substitutions, lexical owners, and the
  dynamic field context — what the proof saw is what the worker runs.

Device admission (AMDGCN/NVPTX/AGX) uses the separate, stricter device proof
described in §18.3–§18.4.

**Runtime dispatch.** When the compiler proves a loop is parallelizable, it
emits a call to `__parallel_for(worker_fn, ctx, lo, hi)` instead of a
sequential loop. The runtime:

1. Checks `TRAVELER_THREADS` environment variable (manual override)
2. Queries `sysconf(_SC_NPROCESSORS_ONLN)` (platform-detected core count)
3. Clamps the thread count by the core count and a per-thread grain size
4. Below a trip-count threshold (`PFOR_THRESHOLD`, **512** iterations —
   lowered from 1024 so large-reduction/low-output-count matmuls cross it),
   runs sequentially

This threshold is a **performance** heuristic only — soundness was decided at
compile time, so changing it can never introduce a race, only trade dispatch
overhead for parallelism. Compile time proves WHAT is safe; runtime decides HOW
MANY threads.

**Lambda lifting.** The loop body is extracted into a standalone worker
function. Free variables from the enclosing scope are collected into a
context struct, filled at the dispatch site, and unpacked in the worker.
The distinction between alloca-stored (`let mut`) and register-stored
(`let`) bindings must be preserved: alloca captures need a load before
store to context; register captures are stored directly.

**Worker emission.** Workers are emitted as a deferred pass after all user
functions and ZK companion functions. This avoids forward reference issues
and follows the same pattern as monomorphization.

**Target triple.** The `-target <triple>` flag configures the LLVM IR target
triple and derives the platform-specific sysconf constant:

```sh
./tvc input.tv -o out.ll -target x86_64-linux-gnu
```

- `darwin` → sysconf constant 58
- `linux` → sysconf constant 84
- unknown → skip sysconf, use env var + fallback only
- Default: `arm64-apple-darwin`

**SIMD interaction.** All field arithmetic functions emit `alwaysinline`.
At `-O2`, LLVM auto-vectorizes array loops using SIMD instructions:
`<16 x i8>` for Field<251>, `<4 x i32>` for Field<65521>, `<4 x i64>`
for Goldilocks. SIMD operates within each core; threading operates across
cores. Both are derived from the same algebraic independence property.

### 15.19 Additional Implemented Codegen

The following major features are implemented in `tvc_self.tv` but lack a
dedicated IR walkthrough above; they are summarized here for completeness.

- **Monomorphization.** Generic functions, structs, and enums (§3.8, §3.9,
  §4.2) are specialized per concrete instantiation, emitted as a deferred pass
  after user functions. Each instance gets a mangled name; `instantiate`
  promotes an instance to external (`dso_local`) linkage for cross-unit linking.
- **Closures.** A closure literal lowers to a lifted top-level function
  `@__closure_N(ptr %__env, <params>)` plus a stack-allocated capture struct
  filled at the definition site; calls are *direct* calls to the lifted
  function. Stack-only, non-escaping; the negative catalogue is enforced at
  compile time (§3.10.1).
- **Dynamic and wide fields.** `instantiate fn<dyn>` emits a runtime-field
  instance carrying an implicit `ptr %__field`; arithmetic routes through
  `@field_dyn_{add,sub,mul,pow,inv,div}` with a narrow fast path (`p <= 2^32`,
  single `urem`) and a wide Barrett path (`i256`). Primes > 2^64 use the
  `field_wide(...)` carrier and `@field_wide_*` (4-limb `i256`/`i512` Barrett).
  See §16.16, §17.10.
- **`#[zk]` / PLONK backend.** A `#[zk]` function compiles to BOTH native code
  and a PLONK circuit (two-pass constraint builder + witness emitter +
  union-find copy constraints + companion generator, with for-loop unrolling
  for trip counts). A `dyn` `#[zk]` generic additionally emits a runtime-prime
  PLONK prover.
- **`extern "C"`.** An `extern "C"` declaration (§13.7) emits an LLVM `declare`
  for the named symbol with the declared ABI signature; call sites lower to a
  direct call. No wrapper or marshalling is generated — arguments pass by the
  raw ABI.

---

## 16. Edge Cases and Corner Behaviors

### 16.1 Field<2> and Boolean Logic

`Field<2>` is the prime field with 2 elements: {0, 1}.

| Expression in Field<2> | Value | Boolean equivalent |
|---|---|---|
| `1 + 1` | `0` | `true XOR true = false` |
| `1 * 1` | `1` | `true AND true = true` |
| `1 + 0` | `1` | `true XOR false = true` |
| `0 * 1` | `0` | `false AND true = false` |
| `1 / 1` | `1` | `true` |
| `0 / 1` | `0` | `false` |
| `1 / 0` | trap | division by zero |

`Field<2>` is NOT the same type as `bool`. They are structurally isomorphic
but nominally distinct. Explicit conversion: `Field<2>::from(b: bool)` and
`bool::from(f: Field<2>)`.

**Compiler optimization**: When targeting `Field<2>`, the compiler SHOULD
recognize that addition is XOR and multiplication is AND, and emit bitwise
instructions instead of the general conditional-subtract pattern.

### 16.2 Degree-0 Polynomials

`Poly<F, 0>` contains a single coefficient. It represents a constant function.

- `eval(Poly<F, 0>, 0..n)` returns an array of n copies of the coefficient.
- `Poly<F, 0> + Poly<F, 0>` returns `Poly<F, 0>` with summed coefficient.
- `Poly<F, 0> * Poly<F, d>` returns `Poly<F, d>` (scalar multiplication).
- `analyze(data)` returns `Poly<F, 0>` if all values in data are equal.

### 16.3 Empty Arrays and Zero-Length Ranges

- `[F; 0]` is a valid type with size 0. It contains no elements.
- `eval(poly, 0..0)` returns `[F; 0]`.
- `analyze(&[])` returns `None` (no polynomial detected).
- `segment(&[], ...)` returns an empty `Piecewise` with 0 segments.

### 16.4 Single-Element Data

- `analyze(&[x])` returns `Some(Poly<F, 0>)` with coefficient `x`. Every
  single value is trivially a degree-0 polynomial.
- `segment(&[x], ...)` returns `Piecewise` with 1 constant segment.

### 16.5 Large Degree Polynomials

When `analyze()` detects a polynomial of high degree:
- The compressed size is `(d + 1) * sizeof(F) + header`, which may exceed the
  literal size `n * sizeof(F)` for large degrees relative to n.
- The `analyze()` caller (the block compression function) checks
  `compressed_size < literal_size` before emitting a polynomial block. If
  compression doesn't save space, the data is stored as literal. The
  `max_deg` parameter passed to `analyze()` limits the search depth to
  avoid exploring degrees that cannot compress.

### 16.6 NTT on Non-NTT-Friendly Fields

When polynomial multiplication is requested on a field where
`IS_NTT_FRIENDLY == false`:
- The compiler falls back to schoolbook or Karatsuba multiplication.
- No error or warning is emitted (this is a performance characteristic, not
  a correctness issue).
- The threshold for selecting Karatsuba over schoolbook is degree > 32.

### 16.7 Overflow in Intermediate Arithmetic

**Impossible by construction.** The intermediate type is always wide enough
to hold the maximum product and the maximum sum:

| Field width | Max product | Intermediate | Headroom |
|---|---|---|---|
| 8-bit (p=251) | 250^2 = 62,500 | u16 (65,535) | 3,035 |
| 16-bit (p=65521) | 65,520^2 = 4,292,870,400 | u32 (4,294,967,295) | 2,096,895 |
| 32-bit (p=2^32-5) | (2^32-6)^2 = 2^64 - 12*2^32 + 36 | u64 (2^64 - 1) | ~5.15 * 10^10 |
| 64-bit (p=2^64-59) | (2^64-60)^2 = 2^128 - 120*2^64 + 3,600 | u128 (2^128 - 1) | ~2.21 * 10^21 |

For addition, the intermediate type must also hold `max(a + b) = 2*(p-1)`:

| Field width | Max sum | Intermediate | Fits? |
|---|---|---|---|
| 8-bit | 2*250 = 500 | u16 (65,535) | yes |
| 16-bit | 2*65,520 = 131,040 | u32 | yes |
| 32-bit | 2*(2^32-6) ≈ 2^33 | u64 | yes |
| 64-bit | 2*(2^64-60) ≈ 2^65 | **u128 required** | yes (u64 overflows) |

The 64-bit case is the first width where addition also requires the double-width
intermediate. For 8/16/32-bit fields, `a + b` fits in the element's own type
plus one bit, so the next-size integer suffices. For 64-bit fields, `a + b`
can reach `2^65 - 120`, which overflows `u64`. The compiler must use `i128`
for all 64-bit field arithmetic, not just multiplication.

### 16.8 Streaming with Non-Polynomial Blocks

When a stream encounters a non-polynomial block (literal or constant with
values outside the field, or random data):
- The stream state transitions to Stale (`active = false`).
- The next block is compressed independently (no continuation check).
- If the next block IS polynomial, the stream re-enters Active state.

This means a stream of `poly, poly, random, poly, poly` will have continuation
blocks for positions 2, 5 (after the first polynomial of each run), but not
for position 4 (after the random block).

### 16.9 Piecewise with All-Literal Segments

If no polynomial structure is detected anywhere in the data:
- Every element becomes its own literal segment.
- The `Piecewise` has `n` segments, each of length 1.
- The compressed size is `n * (sizeof(F) + segment_overhead)`, which is LARGER
  than the literal size `n * sizeof(F)`.

The `segment()` function SHOULD detect this case and return a single literal
segment spanning the entire range, rather than n individual segments.

### 16.10 Polynomial Degree vs Field Order (The Characteristic Barrier)

This is the most important edge case in the language. It affects Newton
conversion, polynomial arithmetic, derivative, and evaluation.

**The mathematical fact**: In `Field<p>`, Fermat's little theorem gives
`t^p = t` for all `t`. This means every polynomial function Z/pZ → Z/pZ
can be represented with degree < p. The vector space of polynomial functions
over Z/pZ has dimension exactly p.

**Consequences for each operation:**

**Newton ↔ Standard conversion (Section 9.1)**:

The Newton-to-standard conversion divides by `1, 2, 3, ..., d` during the
Horner accumulation. When `d >= p`, one of these divisors is `p`, which is
zero in the field.

This reflects a mathematical impossibility. The binomial coefficient `C(t, k)`
involves `k!` in its denominator. When `k >= p`, we have `p | k!`, so `k!` has
no inverse in Z/pZ. The Newton basis is undefined for degree >= p over Z/pZ.

> **NOT ENFORCED (v0.1.0).** `newton_to_standard` / `standard_to_newton`
> *require* `d < p`, but the compiler does **not** check it — there is no
> degree-vs-prime guard. The converters divide by `k mod p` and call `inv`; at
> `k = p` this computes `inv(0) = 0` (§16.11) and silently produces a **wrong
> result** rather than the error shown below. (In practice the conversion
> buffers cap `d` at 31, §9.1.3, far below any real prime, so the barrier is not
> approached.) The intended diagnostic was:
>
> ```
> error: Newton form conversion requires degree < field order.
>        Poly<Field<251>, 300> cannot be converted (degree 300 >= prime 251).
> ```

This is a compile-time error when the degree and prime are both compile-time
constants. When the degree is runtime (DynPoly), it is a runtime trap.

**Polynomial multiplication**:

`Poly<F, a> * Poly<F, b>` produces `Poly<F, a+b>`. If `a + b >= p`, the
result polynomial has degree >= field order. This is valid as a formal
polynomial in the polynomial ring Z/pZ[t], but:
- It cannot be converted to Newton form (see above)
- Its evaluation as a function is equivalent to a lower-degree polynomial
- The derivative may be unexpectedly zero (Section 9.4, derivative edge case)

**Resolution**: The compiler emits a warning (not an error) for `d >= p`:

```
warning: polynomial degree 300 >= field order 251; polynomial functions
         of degree >= p are equivalent to polynomials of degree < p.
         This polynomial cannot be used with eval() or analyze().
```

Multiplication itself works correctly on formal coefficients — schoolbook,
Karatsuba, and NTT all operate on coefficients without hitting the
characteristic barrier. The issue only arises when converting to Newton form
or evaluating as a function.

**Forward summation / eval()**:

`eval()` uses Newton form coefficients. If the polynomial is in standard form
with degree >= p, the required conversion to Newton form will trap. The
compiler inserts this conversion automatically (Section 9.1.3), which means:

```
let high_deg: Poly<Field<251>, 300> = ...
let values = eval(high_deg, 0..100)   // ERROR: conversion to Newton form traps
```

**Resolution**: `eval()` requires its input to have degree < p. This is
enforced at the conversion insertion point. The programmer can explicitly
reduce the polynomial modulo `t^p - t` before evaluation if needed (future:
a `reduce_mod_fermat()` function).

**analyze()**:

`analyze()` produces Newton coefficients and never encounters the barrier,
because it produces at most `d` coefficients where `d <= max_deg`, and the
caller controls `max_deg`. For polycompress, `max_deg` is the block size
(typically 250 for Field<251>), which is always < p.

**Summary**:

| Operation | d >= p behavior | Safety |
|---|---|---|
| Schoolbook / Karatsuba / NTT multiplication | Correct (formal polynomial) | Warning |
| `newton_to_standard()` | Division by zero | Compile error or runtime trap |
| `standard_to_newton()` | Produces wrong result (evaluation wraps) | Compile error or runtime trap |
| `eval()` on standard-form poly | Requires Newton conversion, which traps | Error at conversion point |
| `eval()` on Newton-form poly | Correct (no conversion needed) | OK if coefficients are valid |
| `analyze()` | Always produces d < n <= block_size | OK for typical use |
| Derivative of `t^p` | Returns zero polynomial | Correct, not an error |
| Addition / subtraction | Correct (coefficient-wise) | OK |
| Composition `p(q(t))` | May produce high degree | Warning if result >= p |

### 16.11 Power and Inverse Edge Cases

**x ** 0 = F::ONE for all x including x = 0.**

`0^0 = 1` is the standard algebraic convention. This holds in every field:
the empty product is the multiplicative identity. The compiler's exponentiation
by squaring naturally produces this result (the accumulator starts at 1 and
the loop body executes zero times).

**x ** 1 = x for all x.**

Trivial but the compiler SHOULD optimize this away (no mul emitted).

**F::ZERO ** n = F::ZERO for n > 0.**

The compiler MAY special-case this to avoid the squaring loop. Not required.

**inv(0)**: mathematically undefined (0 has no multiplicative inverse).

> **NOT YET IMPLEMENTED (v0.1.0).** No zero check is emitted. `inv(x)` is
> `pow(x, p-2)`, so `inv(0) = pow(0, p-2) = 0`, and division `a / 0 = a * inv(0)`
> returns `0` **silently** — no trap, no diagnostic (§5.2, §14.2). A guarding
> trap (with `unsafe`/provably-nonzero elision) is the intended behavior.

### 16.12 Field<2> and Polynomial Operations

`Field<2>` has special behavior throughout the polynomial subsystem:

**Newton conversion**: The divisors in `newton_to_standard` are `1, 2, 3, ...`.
In `Field<2>`, the divisor 2 = 0. This means Newton conversion is only valid
for degree 0 and degree 1 (d < 2). Note this precondition is **not enforced**
by the compiler (§16.10); a degree ≥ 2 conversion over `Field<2>` silently
misbehaves rather than erroring.

**Polynomial multiplication**: Schoolbook and Karatsuba work correctly (they
use only addition and multiplication, both well-defined in Field<2>). NTT is
not applicable (Field<2> is not NTT-friendly: `p - 1 = 1` has no factor of
`2^k` for `k >= 10`).

**eval()**: Only works for degree 0 (constant) and degree 1 (linear).
Higher-degree polynomials over Field<2> must be evaluated by direct
substitution, not forward summation with Newton coefficients.

**analyze()**: Returns degree 0 or degree 1 only. The forward difference of
degree-2+ data over Field<2> cannot be represented in Newton form.

**Practical impact**: Minimal. Field<2> is primarily used for XOR-based
operations (checksums, CRC), not polynomial compression. Binary extension
fields (`BinField<8, 0x11B>`) are the practical path for GF(2^k) polynomial
work.

### 16.13 Karatsuba Edge Cases

**Zero-length input**: `karatsuba(&[], &[])` — both inputs empty. The result
polynomial has degree -1 (the zero polynomial). Return `vec![F::ZERO]` (a
single zero coefficient, representing the zero polynomial as `Poly<F, 0>`).

**One input empty**: `karatsuba(&[a], &[])` — multiplication by zero polynomial.
Return `vec![F::ZERO]`.

**Both inputs length 1**: `karatsuba(&[a], &[b])` — multiplication of constants.
Result is `vec![a * b]`. Falls to schoolbook base case.

**Asymmetric inputs**: `karatsuba(deg-100, deg-3)` — the split point `m = 51`
puts all of B in `B_lo` and makes `B_hi` empty. `z2 = A_hi * B_hi` is
trivially zero. The algorithm handles this via the `a_hi_len == 0` check.

### 16.14 NTT Edge Cases

**n = 1**: NTT of a single element is the identity. `bit_reverse` is a no-op.
The butterfly loop doesn't execute (len = 2 > n = 1). Correct.

**n = 2**: Smallest nontrivial NTT. One butterfly: `a[0], a[1] = a[0]+a[1], a[0]-a[1]`.
`omega = g^((p-1)/2)`. For any odd prime, `omega^2 = 1` and `omega = p - 1`
(the unique element of order 2). Correct.

**Requested NTT size exceeds 2^NTT_MAX_LOG**: This means `n` does not divide
`p - 1`, violating the precondition. The compiler detects this at compile time
(when degree bounds are static) and emits:

```
error: NTT size 2^24 exceeds maximum for Field<998244353> (NTT_MAX_LOG = 23).
       Maximum polynomial degree for NTT multiplication: 2^23 - 1 = 8388607.
```

For larger polynomials on this field, fall back to Karatsuba.

**n = 0**: `next_power_of_2(0)` returns 1. `pad_to` with length 1 produces
a single-coefficient polynomial. NTT of size 1 is the identity. Correct.

### 16.15 alloc/Vec Edge Cases

**`alloc<T>(0)`**: Returns a non-null pointer that must still be freed, but
must not be dereferenced. This matches C's `malloc(0)` behavior. The returned
pointer is valid for `realloc` and `free` but not for load/store.

**`vec_new()` (empty Vec, §3.13)**: `data = alloc(4)`, `len = 0`, `cap = 4` —
the backing buffer is allocated up front, not lazily. Access (`vec_get`/`vec_set`)
is **unchecked**; `vec_pop` on an empty Vec underflows `len` (no trap).
`vec_free` frees the buffer.

**`vec_push` when `len == cap`**: doubles `cap` and `realloc`s, then stores.
Amortized O(1).

**`realloc(ptr, 0)`**: libc-defined (typically frees and returns a minimal or
null pointer).

**Integer overflow in Vec capacity doubling**: `cap` is `i32`; doubling past
`2^31` overflows. There is no guard — practical Vec sizes never approach this.

### 16.16 Maximum Prime Size

The largest prime a **static** `Field<p>` type can name is 2^64 - 59 — the
`Field<p>` type parameter is a `u64`. Wider primes are reached through a
**runtime carrier** (`field_wide`, below), not a static type parameter.

**p = 18,446,744,073,709,551,557 = 2^64 - 59** — the largest static prime.

Note: 2^64 - 1 is NOT prime (2^64 - 1 = 3 * 5 * 17 * 257 * 641 * 65537 *
6700417). The integers 2^64 - 2 through 2^64 - 58 are all composite. The
first prime below 2^64 is 2^64 - 59.

The 64-bit field width enables Traveler to operate on machine-word-sized data
natively: timestamps (nanosecond epoch), memory addresses, file sizes, process
IDs, and any 64-bit integer produced by the operating system or hardware.
Field arithmetic at this width uses `u128` intermediate with LLVM's `i128`
type (see Section 15.1 for the codegen patterns).

**Practical block size**: The theoretical block size for `Field<2^64-59>` is
`p - 1 ≈ 1.8 * 10^19` elements, which is impractical (~147 exabytes at 8
bytes per element). The practical block size is a configurable constant,
typically 2048 elements, matching the 16-bit and 32-bit conventions. The
characteristic barrier (Section 16.10) is never approached with practical
block sizes: max degree ≈ 2047 << 2^64 - 59.

**Primes larger than 2^64 (ZK-SNARK fields like BN254 Fr, ~254 bits) ARE
supported** — as a runtime carrier, not a static `Field<p>` type. This
supersedes the earlier "not supported in this revision":

- `field_wide("0x30644e72...")` (prime as a hex string) or
  `field_wide(l0, l1, l2, l3)` (prime as four little-endian `u64` limbs)
  constructs a wide field carrier at runtime, the >2^64 sibling of the dynamic
  `field(p)` carrier (§17.10).
- Elements are 4-limb (`i256`) values; arithmetic routes through
  `@field_wide_{add,sub,mul,pow,inv,div}`, with multiplication by `i512`
  Barrett reduction (Barrett factor `m = floor(2^512 / p)` as 8 limbs). The
  construction path is division-free (no `__udivti3`/`__udivei4` libcall — see
  known-issues #15). Verified against a BN254-Fr oracle.
- Hardcoded at 4 limbs (~254-bit cap). There is no `BigField<p>` static type;
  the carrier is the surface. See §17.10 and @internal-design: rns.

---

## 17. Sequential Polynomial Computation

### 17.1 Overview

Traveler programs operate on sequential data over finite fields. The
fundamental unit of computation is the forward summation register: a
state machine of `d+1` field elements that advances by `d` additions per
step, evaluating a degree-`d` polynomial at consecutive integer points.

The register is domain-agnostic. The field parameter determines the
arithmetic. The data determines the field. Audio samples, price ticks,
sensor readings, pixel intensities, and polynomial coefficients are all
sequences of field elements. The type system makes no distinction between
domains. The algebra is the same.

Four operations define the complete algebra of sequential polynomial
computation:

| Operation | Signature | Semantics |
|---|---|---|
| `forward_sum` | `Register<F,d> -> F` | Advance register one step, emit value |
| `forward_diff` | `[F; n] -> Register<F,d>` | Extract polynomial structure from data |
| `regime_detect` | `Register<F,d> × [F] -> [Boundary]` | Find where polynomial structure changes |
| `eval_at` | `Register<F,d> × F -> F` | Query polynomial at arbitrary field point |

Three of the four are morphisms in the algebraic sense: `forward_diff`
and `forward_sum` are mutually inverse linear isomorphisms (the binomial
transform), and `eval_at` is the evaluation ring homomorphism at a fixed
point. `regime_detect` is a threshold detector and preserves no algebraic
structure — it is not a morphism, which is why the four are called
operations rather than morphisms. All sequential polynomial computation —
encoding, decoding, analysis, synthesis, compression, prediction — is
composition of these operations.

### 17.2 Register\<F, d\>

The register is the atomic type.

```traveler
struct Register<F: Field, const d: usize> {
    state: [F; d + 1],
}
```

A `Register<F, d>` holds `d+1` field elements representing the current
state of a degree-`d` forward summation. At each step, the register
advances by `d` additions:

```
state[0] += state[1]
state[1] += state[2]
...
state[d-1] += state[d]
// state[d] unchanged (highest-order difference is constant)
```

After advancing, `state[0]` holds the polynomial value at the next
consecutive integer point.

**Cost**: `d` field additions per step. No multiplication. No division.
O(1) per sample regardless of data length.

**Initialization**: `forward_diff` on the first `d+1` samples of a
sequence produces the Newton forward difference coefficients, which
initialize the register. Equivalently, the user may set coefficients
directly:

```traveler
let reg = Register<Field<251>, 2> { state: [10, 3, 1] };
// Represents the polynomial p(x) = 10 + 3*x + 1*x*(x-1)/2
// (Newton basis)
```

**Domain independence**: `Register<Field<251>, 3>` is a byte-range
cubic register. `Register<Field<65521>, 1>` is a 16-bit linear register.
`Register<Field<65537>, 3>` is the LA-2A optical envelope. The domain is
in the prime. The operation is always `d` additions.

### 17.3 Regime\<F\>

> **NOT YET IMPLEMENTED (v0.1.0 — planned) as first-class types.** Of the
> sequential-computation hierarchy, only **`Register<F, d>`** (§17.2) is a
> compiler-known type. `Regime<F>` (§17.3), `Segment<F, d>` (§17.4), and
> `Stream<F>` (§17.5), and the `regime_detect` operation, have **no** dedicated
> type or builtin — they are realized in the standard library (the codec, the
> `genus/` and `regime/` modules, §10, §11) as ordinary structs and functions
> over `Register` and plain arrays. The type declarations in §17.3–§17.5 are the
> intended first-class surface.

A regime is the span of sequential data during which a single polynomial
structure holds. Regimes are delimited by boundaries — points where the
register's prediction diverges from the actual data beyond a threshold.

```traveler
struct Regime<F: Field> {
    start: usize,
    end: usize,
    data: &[F],
}
```

A regime is not detected by a separate analysis pass applied after the
fact. It emerges from the register's own operation. The register runs
forward, predicting each next value. When the prediction residual
exceeds the field's tolerance, the regime has ended and a new one begins.

**Detection**: `regime_detect` runs a register forward over a data
slice, monitoring the absolute centered residual
`|predicted - actual|` (balanced representation in `[-p/2, p/2]`).
When the residual exceeds a threshold, a boundary is emitted.
Binary search refines the boundary to sample precision.

**Regime as event**: in real-time contexts (DSP, streaming), regime
boundaries are events. In the LA-2A, a regime boundary is an
attack-to-release transition. In a streaming codec, a regime boundary
triggers chunk emission. In financial data, a regime boundary is a
market structure change. The same algebraic event, different domain
semantics.

**Block-rate detection**: for real-time processing, `regime_detect`
operates within the host's block budget. At 44.1kHz with 2048-sample
blocks (46ms), detection is O(n) — one register advance and one
subtraction per sample, plus O(n) binary search when a boundary fires.

#### 17.3.1 Boundary Invariance Theorem

Regime boundary positions are properties of the signal's polynomial
structure, not of the field it is embedded in. This section proves
this formally and characterizes the exact failure mode.

**Definitions.** Let `data[0..n-1]` be integer-valued input. Fix
polynomial order `d` and threshold `t >= 0`. Define:

- **Integer-mode boundary** `B_Z`: the index returned by
  `regime_detect(data, n, d, t, 0, reg)` — raw integer arithmetic,
  no modular reduction.
- **Field-mode boundary** `B_p`: the index returned by
  `regime_detect(data, n, d, t, p, reg)` — all arithmetic mod p,
  residuals interpreted via balanced representation `signed_val`.
- **Integer residual** `r_i`: `actual[i] - predicted[i]` in Z, where
  the register is advanced using integer arithmetic.
- **Field residual** `r_i^(p)`: `signed_val(mod_sub(actual, predicted, p), p)`.

**Lemma 1 (Register homomorphism).** The field-mode register state
at every step is the mod-p reduction of the integer-mode register
state. That is, `reg_p[j] = reg_Z[j] mod p` for all j at every
step of initialization, advance, and correction.

*Proof.* By induction over the three register operations:

1. **Initialization** (forward differencing, poly_core.tv:253-268):
   `reg[j] = mod_sub(reg[j], reg[j-1], p)`. Since `(a - b) mod p =
   ((a mod p) - (b mod p)) mod p`, the field-mode differences are
   the mod-p reductions of the integer differences. Base case:
   `reg[k] = data[k]`, assumed reduced (input values in `[0, p)`).
2. **Advance** (poly_core.tv:276-280 and 289-291):
   `reg[j] = mod_add(reg[j], reg[j+1], p)`. Same homomorphism:
   `(a + b) mod p = ((a mod p) + (b mod p)) mod p`.
3. **Correction** (poly_core.tv:302): `reg[0] = actual`, directly
   from input (assumed reduced). The integer and field registers
   agree at reg[0]; by (2), subsequent advances propagate the
   agreement. QED.

**Lemma 2 (Balanced exactness).** For integer `x` with `|x| <= (p-1)/2`:

```
signed_val(((x mod p) + p) mod p, p) = x
```

*Proof.* If `x >= 0`: `x mod p = x` (since `x <= (p-1)/2 < p`),
and `signed_val(x, p) = x` (since `x <= (p-1)/2`). If `x < 0`:
`x mod p = x + p` (since `-p/2 < x < 0`), and `signed_val(x + p, p) =
(x + p) - p = x` (since `x + p > (p-1)/2`). QED.

**Theorem (Boundary Invariance).** Let `B_Z` be the integer-mode
boundary index. If all integer residuals through the boundary satisfy
`|r_i| <= (p-1)/2` for `i` in `[d+1, B_Z]` (the boundary index `B_Z`
included — the proof uses the bound at `B_Z` itself), then `B_p = B_Z`.

*Proof.* By Lemma 1, the predicted value at each step in field mode
is the mod-p reduction of the integer prediction. The residual
`r_i^(p) = signed_val(mod_sub(actual, predicted, p), p)`. The argument
to `signed_val` is `(actual - predicted) mod p = r_i mod p`. By
Lemma 2, `r_i^(p) = r_i` whenever `|r_i| <= (p-1)/2`. Therefore
`|r_i^(p)| = |r_i|` at every step before the boundary, and the
threshold comparison yields the same result. At the boundary itself,
`|r_{B_Z}| > t` in integer mode; since `|r_{B_Z}| <= (p-1)/2` by
hypothesis, `|r_{B_Z}^(p)| = |r_{B_Z}| > t` and field mode reports the
same index. QED.

**Corollary 1 (Soundness / one-sided error).** For threshold
`t < (p-1)/2`, every boundary reported by field mode is a true
integer boundary. The error is one-sided: field mode may *miss*
a boundary (when `p | r_i`, aliasing the residual to zero), but
can never report a *false* boundary.

*Proof.* If field mode reports a boundary at index i, then
`|r_i^(p)| > t`. Since `|r_i^(p)| = |signed_val(r_i mod p, p)|
<= (p-1)/2`, and `t < (p-1)/2`, the balanced representation is
exact by Lemma 2 for any value in `[-(p-1)/2, (p-1)/2]`. Thus
`|r_i| = |r_i^(p)| > t`, confirming the integer-mode boundary. QED.

**Corollary 2 (Divisibility condition / CRT certification).** At
threshold `t = 0`, prime p misses boundary i if and only if `p | r_i`.
Primes p and q both miss boundary i if and only if `pq | r_i`.

*Consequence:* two-prime agreement with `pq > 2 * max|r_i|`
**certifies** that all reported boundaries are integer-correct and
no boundaries were missed. The verifier is a composition of two
`regime_detect` calls — not a fifth operation but a certificate
built from the existing four.

**Hypotheses.** Input values must be reduced to `[0, p)` (the
`mod_sub` implementation uses i64 intermediate arithmetic that
assumes non-negative inputs). Prime fields only — `signed_val`
requires odd characteristic; GF(2^k) regime invariance is out of
scope. The theorem applies to the reference implementation in
`examples/poly_core.tv` (lines 241-307).

**Empirical verification.** `regime_topology_test.tv` tests 5 signal
classes x 3 primes {251, 65521, 65537} with identical boundary
positions (15/15). `regime_invariance_test.tv` tests the failure
mode: engineered residuals at p and pq demonstrate aliased misses
and CRT detection.

### 17.4 Segment\<F, d\>

A segment is a regime that has been analyzed: the polynomial structure
has been extracted.

```traveler
struct Segment<F: Field, const d: usize> {
    regime: Regime<F>,
    poly: Register<F, d>,     // Newton coefficients
    residuals: &[F],          // prediction errors (may be empty)
}
```

The transition from regime to segment is `forward_diff`: given the
regime's data, extract the Newton forward difference coefficients that
best describe the polynomial structure. The polynomial order `d` is
selected by the data — try orders 1 through `MAX_ORDER`, choose the
one with minimum residual cost. Greedy selection is near-optimal because
forward differences are a linear operator and the field is finite.

**Lossless invariant**: if the data is exactly polynomial of degree `d`,
the residuals are all zero. If not, the residuals are the difference
between the polynomial prediction and the actual data. The segment
encodes both. `Register<F,d>` + residuals = lossless reconstruction.

**Encoding**: segment data is written to the wire format as Newton
coefficients (polynomial header) followed by residuals (entropy-coded).
For segments where the polynomial prediction is exact, only the header
is needed.

### 17.5 Stream\<F\>

A stream is an ordered sequence of segments with a continuation relation.

```traveler
struct Stream<F: Field> {
    segments: Vec<Segment<F, d>>,  // d varies per segment
}
```

The continuation relation: when consecutive segments follow the same
polynomial, the second segment can be encoded as a CONT block (3 bytes)
instead of a full polynomial header + residuals. The check is exact
field equality — does the register from the previous segment, advanced
through the boundary, correctly predict the first `d+1` values of the
next segment?

**Streaming**: `Stream<F>` supports push semantics. Samples accumulate
in a regime buffer. When `regime_detect` finds an interior boundary,
everything before the boundary becomes a segment, encoded and emitted.
The remainder carries forward. Late-joining clients wait for the next
non-CONT segment, which contains full Newton coefficients — a keyframe
by construction.

**Progressive delivery**: N-layer encoding decomposes each sample into
byte layers via divmod chain. Each layer is an independent
`Stream<Field<p_k>>` where `p_k` is the smallest prime containing the
layer's value range. Layers share boundaries (detected once on
the full-resolution signal). Send layer 0 first for immediate low-
resolution playback. Send subsequent layers to refine. The skeleton
ratio converges on `1/n_layers` — bandwidth scaling is linear.

### 17.6 Composition and Multi-Domain Application

Arrays of registers over the same field compose naturally.

```traveler
let channels: [Register<F, d>; 2] = ...;  // stereo
```

Regime boundaries are field-level events: if the polynomial structure
changes in the shared field, all registers observe it. Multi-channel
processing uses shared boundaries from a single `regime_detect` pass
on any representative channel (or a merged signal). This is not a
coupling type — it is a consequence of the field axioms. Elements of
the same field obey the same arithmetic. Structure changes in the field
affect all computations over that field.

The NTT butterfly — combining two register states via field
multiplication and addition — is field arithmetic on register contents.
No special type. The PLONK gate equation — combining three wire values
via `q_L*a + q_R*b + q_M*a*b + q_C - q_O*c = 0` — is a polynomial
identity over field elements. The ZK circuit is a `Register<F, d>`
where the "data" is the constraint satisfaction witness.

The algebra is the type system. Composition falls out of field
axioms. Domain-specific behavior falls out of field selection.
The register advances. The regime detects. The segment encodes.
The stream delivers. The field determines everything else.

### 17.7 Relationship to Existing Constructs

| Current construct | Section 17 type | Transition |
|---|---|---|
| `Poly<F, d>` (Section 7) | Static coefficients | `Register<F, d>` adds the advance operation |
| `forward_sum` (poly_core.tv) | Free function | Method on `Register<F, d>` |
| `forward_diff` (poly_core.tv) | Free function | `Regime<F> -> Segment<F, d>` morphism |
| `regime_detect` (poly_core.tv) | Free function | `Register<F, d> -> [Boundary]` detector (not a morphism) |
| `eval_at` (poly_core.tv) | Free function | `Segment<F, d> -> F` query |
| `FieldCtx` (piecewise_codec.tv) | Struct with prime | Subsumed by `F: Field` type parameter |
| `particle_scan` (piecewise_codec.tv) | Offline regime detection | Composition: `regime_detect` over full signal |
| `StreamState` (stream_protocol.tv) | Flat buffer struct | `Stream<F>` with typed push semantics |
| `la2a_core.tv` state[64] | Flat i32 array | `Register<Field<65537>, 3>` + parameter struct |

### 17.8 Implementation Path

Section 17 types do not require new runtime machinery. A
`Register<F, d>` is `[F; d+1]` — the same flat array the codec and
DSP already use. The type system provides compile-time verification
that the register's field and degree are consistent across operations.
Codegen emits the same field additions, the same LLVM IR, the same
`alwaysinline` arithmetic functions. The abstraction is zero-cost:
the type erases to the same representation the current code uses
manually.

### 17.9 Method Syntax

Method call syntax is supported as syntactic sugar:

```traveler
let mut reg: Register<F, 2> = register(10, 4, 2);
let v: F = reg.advance();   // desugars to advance(&reg)
```

When the parser sees `expr.ident(args)` (a dot-access followed by a
parenthesized argument list), the receiver is passed by pointer and
`ident` is resolved against the receiver's type. Resolution now proceeds
in two steps:

1. **Trait/impl method** — if the receiver's type has an `impl` providing
   `ident` (§7.4), that method is selected (mangled `Trait__Type__ident`),
   with `self` bound to `&expr`.
2. **UFCS fallback** — otherwise the call rewrites to `ident(&expr, args)`:
   any free function whose first parameter matches (or is a pointer to) the
   receiver's type is callable as a method.

Both paths are **static** — no trait method tables, no virtual dispatch. This
is a change from an earlier revision, which described method syntax as *only*
the parser-level UFCS rewrite (step 2); trait/impl method resolution (step 1)
is now implemented (§7.4).

This still enables algebraic method resolution: a `Register<F, d>` has
`advance` because the free function `advance` accepts `*Register<F, d>` as its
first parameter — no `impl` block needed. The `Section 17` types are concrete,
not trait-based; user traits (§7.4) coexist with this UFCS resolution. The
built-in algebraic hierarchy (`Field > IntegralDomain > CommutativeRing > Ring
> Semiring > Monoid`) provides the foundation §17 builds on.

### 17.10 Dynamic Fields (`dyn`)

The modulus lives in the **type** when known at compile time, and in
the **value** when known only at runtime. The same generic source
compiles both ways:

```traveler
fn forward_sum<F: Field>(coeffs: *F, order: i32, out: *F, n: i32, reg: *F) {
    // ... a + b over F ...
}

instantiate forward_sum<Field<251>>;   // baked: constants inlined, vectorizes
instantiate forward_sum<dyn>;          // runtime: modulus late-bound
```

**The `Field` value type.** `field(p)` constructs a runtime Field
carrier — a pointer to a compiler-emitted `%__Field` struct holding
`{p, half_p, elem_bytes, data_bytes, barrett_lo, barrett_hi}`.
Construction validates primality (deterministic Miller-Rabin, abort
on composite — the runtime analogue of the compile-time primality
check) and computes the Barrett factor `floor(2^128 / p)` once. The
bare type name `Field` (no `<p>`) denotes this carrier:

```traveler
let f: Field = field(65521);     // primality checked, Barrett computed
```

**Carrier construction with explicit data width.** A second argument
sets `data_bytes` — the byte width of raw data values for wire-format
literal blocks — independently of `elem_bytes` (the field-element width
derived from the prime). Omitting it derives `data_bytes` from `p - 1`:

```traveler
let f: Field = field(65537);        // elem_bytes=4, data_bytes=3 (from p-1)
let g: Field = field(65537, 65535); // elem_bytes=4, data_bytes=2 (16-bit data)
```

This decouples the field's arithmetic width from the data's packing
width — e.g. a 16-bit signal projected into a Fermat prime field.

**Carrier member access.** The carrier's fields are readable on a
`Field` value: `f.p` and `f.half_p` (i64), `f.elem_bytes` and
`f.data_bytes` (i32). This lets code read the prime and its derived
constants without threading them as separate parameters — the carrier
replaces ad-hoc context structs:

```traveler
let half: i64 = f.p / 2;            // balanced-representation threshold
let width: i32 = f.data_bytes;      // wire-format literal byte width
```

**Dyn instantiation.** `instantiate fn<dyn>;` emits one concrete
function with the `_dyn` mangle suffix and an **implicit leading
parameter** `ptr %__field` carrying the runtime Field. Callers pass
the carrier explicitly as the first argument:

```traveler
forward_sum_dyn(f, coeffs, 2, out, 10, reg);
```

Inside the body, every field operation routes through the
once-per-module runtime functions `@field_dyn_{add,sub,mul,pow,inv,div}`
(`i64, i64, ptr` — add/sub branchless via i128). Multiplication takes
one of two paths, chosen at runtime from the carrier: a **narrow fast
path** for `p <= 2^32` (a single `urem` in the intermediate type) and a
**wide Barrett path** for larger primes (Goldilocks-class, multiply-high
via `i256`, no `__udivti3` libcall). Element backing is uniformly `i64`:
`*F` in a dyn context is `*i64`, `signed()` returns `i64`. Integer-to-dyn
conversions (`let x: F = 42`, `expr as F`) reduce by runtime `urem`
against `p` loaded from the carrier.

**Not Rust's `dyn`.** Despite the keyword, no dispatch is erased and
no vtable exists. There is exactly one function (the operations are
statically known — they are the field axioms); only the *modulus* is
late-bound. The Field carrier is a table of constants (the prime and
its precomputed reduction factors), not a table of function pointers.
Rust's `dyn Trait` defers *which code runs*; Traveler's `dyn` defers
*which field the one algorithm runs over*. The dispatch-relevant
structure — that this is prime-field arithmetic — is still fixed at
compile time, which is why dyn loops remain eligible for
auto-parallelization: element independence follows from the field
axioms regardless of when the modulus binds.

**Parallelization.** A `for` loop over dyn elements parallelizes under
the same index-map soundness rules as baked fields (Section 15.18).
The implicit carrier is captured into the worker context alongside the
array pointers; it is read-only after construction, so it introduces
no aliasing hazard.

**Cost model.** Dyn arithmetic is scalar (the modulus is not an
immediate, so LLVM cannot fold or vectorize the reduction). Measured
on the codec workload (forward summation, add-dominated): ~2x baked.
Multiplication-heavy paths (eval_at) pay more. The baked path remains
the choice for compile-time-known primes (crypto: NTT, FRI, PLONK);
dyn serves the paths where the data selects the field at runtime
(codec: `scan_max → next_prime → field(p)`).

`dyn` is a reserved type name: it cannot be declared as a field alias,
struct, or enum name.

#### 17.10.1 Wide fields (`field_wide`) — primes > 2^64

The `dyn` carrier above holds the prime in a single `i64`, so it covers primes
up to ~2^63. For ZK-SNARK fields larger than 2^64 (e.g. BN254 Fr, ~254 bits)
there is a **wide** carrier, the >2^64 sibling of `field(p)`:

```traveler
let f: Field = field_wide("0x30644e72...");   // prime as a hex string, OR
let g: Field = field_wide(l0, l1, l2, l3);      // as 4 little-endian u64 limbs
```

`field_wide(...)` emits `@__field_wide_init`, which builds a `%__FieldWide`
carrier holding the prime as `i256` limbs and the Barrett factor
`m = floor(2^512 / p)` as `i512` limbs. Elements are 4-limb (`i256`) buffers
(`*i64` of length 4); arithmetic routes through
`@field_wide_{add,sub,mul,pow,inv,div}`, with multiplication reduced by `i512`
Barrett. The carrier construction is **division-free** (bit-serial long
division — no `__udivti3`/`__udivei4` libcall on any target; known-issues #15),
which is what makes the whole wide surface link under the C-free chain.

Hardcoded at 4 limbs (~254-bit cap). This is the substrate under the crypto
tower (BN254) and is the same exact-finite-algebraic path the induced-wide
RNS+CRT NN kernels rely on (§16.16, @internal-design: rns).

### 17.11 The Degree-Persistence Profile and the Genus Signature

A sequence sampled at consecutive integer points carries a
deformation-stable complexity invariant: the degree at which exact
polynomial structure first appears, and the way that structure resolves
into pieces as the model degree increases. This subsection defines the
invariant, proves the two theorems that bound it (one arithmetic, one
approximation-theoretic), records its measured invariances, and
describes the two ZK certificates that attest it. All artifacts are
pure Traveler library code over the `dyn` field (Section 17.10); the
compiler is untouched.

**Naming status.** The terms below are chosen for what is *measured*,
and the choice is recorded honestly because the program located its
invariant by finding where its enabling assumptions fail.

- **Onset degree** `d*` — the minimal polynomial degree `d` such that
  the `(d+1)`-th forward difference of the sequence vanishes
  identically. Operationally, the smallest `d` at which a canonical
  segment beats the trivial interpolation window (`maxseg > d+1`): a
  degree-`d` polynomial interpolates *any* `d+1` points, so only a
  segment longer than `d+1` witnesses real structure. Sentinel `d*  =
  d_max + 1` records "no onset within the probed range" — the honest
  bounded result (real recorded audio lives here).
- **Degree-persistence profile** `nseg(d)` — the canonical regime count
  as a function of model degree, `d = 0..d_max`. This is the order-free
  invariant. The single point `(d*, nseg@d*)` is a greedy artifact and
  is order-dependent; the full profile is symmetric
  (`line+parab == parab+line`). Below `d*` the profile tracks the
  trivial maximum `n/(d+1)`; at and above `d*` it plateaus at the true
  piece count. For piecewise data the invariant is therefore a *curve*
  over the degree axis, not a number.
- **Genus signature** — the `(d*, nseg@d*)` pair (with the full profile
  as its order-free refinement). "Genus" is used at the
  intuition/banner level: a deformation-stable complexity invariant.
  The literal topological sense (enclosed area / Betti numbers, the
  even-odd intersection work) is reserved to avoid self-collision.

*Footnote on the formal cousin.* No standard mathematical term names
`d*` for a one-dimensional integer sequence; the finite-difference
literature describes it only procedurally ("difference until constants
appear"). The closest formal relative is **Castelnuovo–Mumford / Hilbert
regularity**, but that names *eventual* polynomiality — the asymptotic
onset of agreement between a Hilbert function and its Hilbert polynomial
— whereas `d*` is *exact* reproduction from sample 0. A commutative
algebraist should read "onset degree" as the exact-from-zero analogue,
not as regularity proper. "Degree-persistence profile" borrows the
filtration/persistence framing of topological data analysis, which is
the technically honest description of an invariant that resolves as a
parameter (here, degree) increases.

#### 17.11.1 The Onset-Degree Aliasing Theorem (arithmetic boundary)

The onset degree measured over a finite field can only *under*-report
the integer truth, never over-report. This is the Boundary Invariance
Theorem (Section 17.3.1) lifted from *boundary present* to *degree
present* — the same one-sided structure, the same arithmetic mechanism.

**Definitions.** Let `x` be an integer sequence. Let `d*_Z` be its
integer onset degree and `d*_p` the onset degree measured over `Z/pZ`,
on the data **reduced mod p**.

**Theorem (one-sided).** `d*_p <= d*_Z` always, with equality iff `p`
divides none of the nonzero integer differences that witness the true
degree.

*Proof.* The forward difference `Delta` is `Z`-linear and commutes with
reduction mod `p`: `(Delta^k x) mod p = Delta^k (x mod p)`. Hence
`Delta^d x = 0` over `Z` implies `Delta^d x = 0` mod `p` — true
structure is never lost. The field can only *gain* the kernel
`p | Delta^d x`, making a nonzero integer difference vanish mod `p` and
lowering the measured degree. QED.

**Corollary (safe-prime condition).** Since `|Delta^k x| <= 2^k max|x|`,
any prime `p > 2 * max_k |Delta^k x|` (the max over the probed degrees)
cannot divide a witnessing difference, so `d*_p = d*_Z`. The `2^k` worst
case is loose; the data-driven `2 * max|Delta^k x| + 1` is a tight,
computable safe threshold. (Goldilocks `~2^64` is safe for any realistic
data, which is why every measurement over a large prime holds.)

**Subtlety.** "What `Field<p>` sees" requires measuring the data
*reduced mod p*. Raw integers may exceed `p`, and aliasing is precisely
a witnessing difference vanishing mod `p`; the certifiers reduce a
working copy first (`genus_onset_modp`, `genus_alias.tv:50`).

**Certification (both halves — soundness and completeness).**

- `genus_certify_crt2` (soundness / detect): measure `d*` over two
  primes; agreement certifies, disagreement *detects* the alias
  (sentinel `-1`). Never false-certifies.
- `genus_certify_crt3` (completeness / resolve): the max of `d*` over
  three primes. Valid *because* aliasing is one-sided — the max climbs
  back to the integer truth as long as at least one measuring prime is
  safe. The one-sided theorem is exactly what licenses max-vote.

**Empirical verification.** 27,000 random `(polynomial, prime)` pairs:
zero violations of the one-sided bound; aliasing (`d*_p < d*_Z`) occurs
~17% of the time. Artifact: `genus_alias.tv` /
`genus_alias_test.tv` (aliased parabola, 2nd difference `= 251`:
`Field<251>` sees `d* = 1`, `Field<65537>` sees `d* = 2`; crt2 detects,
crt3 resolves to 2; safe bound 5031). Gate `dynfield-genus-alias`.

#### 17.11.2 The Degree Filtration and the Exact–Soft Boundary (approximation boundary)

The second boundary is approximation-theoretic, and measurement
*corrected* the original hypothesis. The plan assumed exact fit is
infix-closed (greedy = global) while threshold fit is not, and that the
divergence was the soft-regime signal. That guess was wrong.

**Measured fact.** Difference-based fit is infix-closed for *both* the
exact predicate (`Delta^{d+1} = 0`) and the bounded-threshold predicate
(`|Delta^{d+1}| <= eps`): a sub-interval's `(d+1)`-th differences are a
literal subset of the parent's, so if the parent fits, every
sub-interval fits. Infix-closure yields an exchange argument, so local
greedy left-extension *equals* the global DP-optimal minimal partition.

- Exact: 60,000 piecewise-polynomial cases, zero greedy ≠ global.
- Bounded-threshold: 200,000 random sequences, zero greedy ≠ global.
- Infix-closure of bounded-threshold: 100,000 trials, zero violations.

So the Newton-difference filtration is canonically greedy-computable;
there is **no** exact–soft gap *within* the difference family.

**Where the boundary actually lives.** Under a best-fit-residual
criterion (degree-`d` least squares within tolerance, or minimum
description length), a longer window has *more* fitting freedom, so the
criterion is not infix-closed and greedy over-counts versus global
(verified: `data = [19,21,13,15,8,16,18]`, `d=1`, `eps=3` gives
greedy 3, global 2).

**Conclusion (sharper than the plan).** The exact–soft boundary is a
property of the **fit criterion**, not of the data. The difference
operator family (exact + bounded) is on the crisp side — order-free,
greedy-canonical. A best-fit-residual / MDL objective crosses to the
soft side where local and global diverge, and that divergence is the
soft-regime signal. Artifact: `genus_profile.tv`
(`genus_filtration`, `genus_greedy_is_global`) / `genus_soft_test.tv`
(order-free filtration `[12,4,2,2,2]`, greedy == global). Gate
`dynfield-genus-soft`.

#### 17.11.3 Invariances

The degree-persistence profile is invariant under the maps that should
not change a shape's structure (measured, Phase A.3b + D):

- **Field** — Goldilocks / BabyBear / 65537, modulo the aliasing
  theorem: when the prime is safe (`> 2 max|Delta^k|`) the measurement
  is field-invariant.
- **Representation** — standard vs. centered/balanced residues.
- **Affine** — `+C`, `*k`, `*k + C`.
- **Decimation** — every `k`-th sample (a degree-`d` polynomial
  subsampled stays degree-`d`).

This deformation stability is what makes "genus" the right banner: the
invariant is preserved under the structure-preserving maps, even though
the literal object is a degree filtration rather than topological genus.
Artifacts: `genus_invariance_test.tv` (8/8 field + affine checks),
`genus_probe_test.tv` (saw `(1,4)`, triangle `(1,8)`, square `(0,8)`,
parabola `(2,4)`, cubic `(3,1)`). Gates `dynfield-genus-invariance`,
`dynfield-genus-probe`.

#### 17.11.4 ZK Certificates

The genus signature is ZK-certifiable from both boundaries of
Section 17.11.2 — the adaptive (soft) side and the fixed-degree (crisp)
side.

**Adaptive (soft) certificate** — "what degree is this shape?" for
mixed-degree data: the MDL-optimal per-segment degree vector.
`genus_mdl_segment` runs a DP over per-span minimal degree, minimizing
total description length; it recovers true structure where greedy fails
(`const+lin+quad -> [0,1,2]` vs. greedy `[4,4,1]`). Infeasible spans
return `-1` and are skipped. The certificate (`genus_adaptive_certify`)
combines a safe prime (17.11.1), one PLONK proof over the whole
mixed-degree descriptor (degree-0 pieces emit no gates — their constancy
rests on the Merkle data-binding), and local stability (every interior
boundary merge-forced). Local stability is *necessary* for
MDL-optimality (measured at every optimum over 50,000 shapes) but **not
sufficient** (3,752 locally-stable non-optima), and is described exactly
as "locally MDL-stable," never "globally optimal." Gate
`dynfield-genus-adaptive`.

**Fixed-degree (crisp) certificate** — for single-degree shapes: the
degree-persistence profile `nseg(d)` plus its meaningful plateau.
`genus_resolve_plateau` returns `(d*, d_end, P)` where `nseg(d) = P` and
`maxseg(d) > d+1` (real structure, not the trivial-interpolation
artifact). The certificate (`genus_profile_certify`) combines profile,
plateau, the greedy == global identity (infix-closure, 17.11.2), one
PLONK proof of the circuit at `d*`, and three tamper rejections
(`line [8,1,1,1,1]` `d*=1`; quadratic `[8,4,1,1,1]` `d*=2`). Gate
`dynfield-genus-profile-cert`.

The two certificates *are* the crisp–soft boundary of 17.11.2, now both
ZK-attested.

**Global optimality (closed).** A global MDL-*optimality* proof is now a
DP-witness verified in ZK (`genus_global_certify`). By Bellman optimality
the DP `cost[n]` is the global minimum description length iff, for every
endpoint `e`, *dominance* (`cost[e] <= cost[s] + c(s,e)` for every
feasible `s`) and *achieving* (equality at the chosen predecessor) hold;
`c(s,e) = mindeg(s,e)+1` is bound to the data by the FIT machinery
(`seg_fits`). The dominance inequalities rule out every cheaper partition
— including all 3,752 locally-stable non-optima that defeated local
stability alone. Each inequality is proven by a ZK range proof (`leq`,
the first ordering primitive in the stack — §17.11.5 / `zk_range`), one
small `padded_n<=32` proof per pair, composed rather than monolithic so
every sub-proof stays within the measured FRI buffers. The step the
field's equality/nonzero algebra could not make — comparison — is
supplied by bit-decomposition. Gate `dynfield-genus-global`
(`const+lin -> [0,1]` cost 3; `lin+quad -> [1,2]` cost 5;
`const+lin+quad -> [0,1,2]` cost 6), cross-checked against a brute-force
optimum over all partitions (`genus_global_oracle.py`).

#### 17.11.5 ZK Infrastructure: Ordering and Circuit Description

Two general dyn-ZK capabilities underpin the global-optimality certificate
above; both are independent of the genus program and reusable by any
`dyn` circuit.

**The ordering primitive (range / comparison).** Every prior relation in
the ZK stack is equality- or nonzero-based — a boundary is forced iff
`err·err⁻¹ = 1` (17.3.1), an MDS diagonal iff a characteristic polynomial
is irreducible, a degree fit iff the `(d+1)`-th difference vanishes. A
finite field has no order, so none of these can express `a <= b`. The
range gadget (`zk_range.tv`) supplies the missing primitive by
bit-decomposition: it hand-builds a PLONK circuit (selectors, witness,
union-find permutation written directly into caller arrays, then handed to
`plonk_prove_dyn` / `plonk_verify_dyn` — the same construction style as the
runtime circuit builder, §17.10, with **no compiler change**) proving
`0 <= x < 2^nbits` via

- a boolean-constraint gate per bit (`b_j·(b_j − 1) = 0`),
- a reconstruction chain (`acc_j = acc_{j−1} + 2^j·b_j`) bound to `x`,

with the permutation closing every bit and accumulator cell — the classic
range-proof holes (an unconstrained bit, a reconstruction not bound to the
value) are exactly the cells the permutation binds. Comparison follows:
`leq(a, b)` range-proves `(b − a)` in `[0, 2^nbits)`, which holds iff
`a <= b` with no field wraparound. Gate `dynfield-zk-range`, cross-checked
by `zk_range_oracle.py`.

**Circuit description (external re-derivation).** The PLONK circuit a
`#[zk]` function compiles to — gate selectors and the copy-constraint
permutation — previously lived only inside the prover companion: an
external party received a proof but could not re-derive the circuit to
check it. The compiler now emits a third companion alongside the native
function and the prover,

```
<fn>_zk_describe_dyn(f, q_L, q_R, q_O, q_M, q_C,
                     sigma_a, sigma_b, sigma_c, n_out)
```

which writes the compile-time-known selectors and the omega-encoded sigma
(root of unity from the carrier chain, coset factors `k1=13`, `k2=17`)
into caller buffers and reports `[padded_n, log_n]`. It rebuilds the
topology identically to the prover, so the description provably matches the
circuit the proof used; an external verifier then calls `plonk_verify_dyn`
itself. Gate `dynfield-zk-describe` runs the full external loop on the
`#[zk]` cubic and rejects a tampered selector.

#### 17.11.6 Artifact and Gate Index

| Property | Artifact | Gate |
|---|---|---|
| Onset-degree aliasing + safe prime + crt2/crt3 | `genus_alias.tv` / `_test` | `dynfield-genus-alias` |
| Degree filtration order-free + greedy == global | `genus_profile.tv` / `genus_soft_test` | `dynfield-genus-soft` |
| Genus signature + invariance + certificate + edge | `genus_probe` / `_invariance` / `_certify` / `_edge` / `_wav` | `dynfield-genus-*` |
| Adaptive (MDL) certificate: FIT + STABILITY | `genus_adaptive.tv` / `genus_adaptive_certify.tv` | `dynfield-genus-adaptive` |
| Fixed-degree (crisp) certificate: filtration + plateau | `genus_profile.tv` / `genus_profile_certify.tv` | `dynfield-genus-profile-cert` |
| Global MDL-optimality: DP-witness (dominance + achieving) | `genus_global_certify.tv` | `dynfield-genus-global` |
| Ordering primitive: range proof + `leq` (bit-decomposition) | `zk_range.tv` / `zk_range_test.tv` | `dynfield-zk-range` |
| Circuit description: external re-derivation of `#[zk]` circuits | `tvc_self.tv` (`codegen_zk_fn_describe_dyn`) / `zk_describe_test.tv` | `dynfield-zk-describe` |

The onset degree is the same arithmetic invariant the Boundary
Invariance Theorem (17.3.1) certifies for boundaries; the degree of a
segment is selected exactly as in Section 17.4; and every certificate
runs over the `dyn` field of Section 17.10.

---

## 18. Backends and Targets

Beyond the default host flow (§15.0), the compiler exposes a small set of
**closed** backend surfaces. "Closed" means the accepted configurations are an
enumerated set: arbitrary LLVM pass strings, arbitrary device shapes, and
arbitrary GPU source patterns are deliberately not accepted — an unsupported
shape is a refusal or a CPU fallback, never a silent best effort. Operational
detail (tool paths, environment variables, the machine-specific AGX profile
image) lives in `BUILD.md`; this section specifies the language-visible
contracts.

### 18.1 CPU Middle-End Profiles (`--opt-level`)

Three closed profiles run through the same IR/object/executable flow:

| Profile | LLVM middle-end pipeline | Requires `opt` |
|---|---|---|
| `none` | none; raw compiler IR | no |
| `promote` | `-passes=mem2reg -verify-each` | LLVM 21 |
| `o1` | `-passes=default<O1> -verify-each` | LLVM 21 |

- `none` is the default; omitting `--opt-level` and selecting `none` produce
  byte-identical raw IR.
- `promote`/`o1` are explicit, reproducible LLVM-21 toolchain transformations.
  Their output is verified LLVM but is **not** a bootstrap fixed-point
  artifact (§15.0). Arbitrary pass strings are refused — the set above is the
  whole surface. `-opt <path>` overrides `PATH`, matching `-llc` and `-cc`.
- Standalone AMDGCN/NVPTX/AGX device emission (§18.3, §18.4) and the `-target
  tpc` compatibility mode (§18.2) accept only profile `none`.
  `--agx-dispatch` (§18.4) emits a normal host module and may use either CPU
  profile for its unchanged fallback path.
- External tools run with argument vectors, not through a shell. IR, object,
  and executable outputs use exclusive sibling stages and are atomically
  published only after every requested tool succeeds; a failure preserves any
  existing destination and removes intermediates.

### 18.2 The TPC Target (`-target tpc`)

`-target tpc` selects a **typed-pointer compatibility mode**: a post-pass
converts the finished opaque-pointer IR to LLVM-12-style typed-pointer IR.
Default output is unchanged, and mode combinations that cannot be converted
faithfully (e.g. a non-`none` middle-end profile) are refused. Typed-pointer
output is covered by parser and runtime parity gates, so `tpc` IR behaves
identically to the default output on the programs it accepts.

### 18.3 GPU Device Emission (AMDGCN / NVPTX)

`--emit-gpu` and `--emit-gpu-nvptx` emit **LLVM device modules** (triples
`amdgcn-amd-amdhsa` and `nvptx64-nvidia-cuda`) for loops the parallel proof
(§15.18) has already admitted; an external `llc` lowers them (e.g. a gfx1100
object, sm_90 PTX). This is **Stage 0** device support — a closed source
shape, not general GPU compilation:

- **Own-cell elementwise field maps** — the proven parallel-loop shape with
  field or `dyn`-field carriers.
- **The private K=8 dot shape** — one mutable scalar accumulator, one literal
  `0..8` inner loop, one own-cell output. The device lowerer fully unrolls the
  inner loop into SSA, so the module stays alloca-free and registers-only.

General private mutables, dynamic inner loops, and multi-statement reductions
remain outside Stage 0: unsupported workers are not emitted.

### 18.4 The AGX (G16X) Backend

`--emit-gpu-agx` is different in kind: Traveler **directly emits measured
G16X instruction bytes** in canonical hex — no Metal compiler, no LLVM device
backend. The target is an M4 Pro G16X private interface and is **not** an
Apple-supported ABI; no performance claim is made.

**Source admission.** A loop must pass the recursive parallel proof (§15.18)
*and* the device proof, and the recursive and legacy classifications must
agree on captures, writes, geometry, worker identity, and machine bytes
before any device emission. Within that gate, the backend admits a closed
scalar kernel language:

- **Structured scalar kernels** — scalar locals, comparisons, short-circuit
  control, bounded private loops, and local exits.
- **Aggregates** — flat structs, scalar enums, `match`, and constant-index
  fixed arrays, scalarized into registers.
- **Static and bounded calls** — expression-bodied direct, generic, trait,
  operator, and local-closure calls inlined when identity and effects are
  statically complete; scalar callees expanded with explicit depth and
  capacity limits (no machine call ABI). Bounded dynamic reads/writes on
  local fixed arrays lower to ordered scalar-slot selections.
- **Counted reductions** — fixed-shape field dot products for the three RNS
  primes emit counted G16X loops (not unrolled code) with exact K=8 row and
  column geometry and explicit input extents; CRT reconstruction is validated
  against independent CPU results.

Unsupported conversions, storage shapes, control shapes, register pressure,
recursion, and erased targets stay on the CPU.

**Field profiles.** The unary map path admits one-input/one-output field maps
over `Field<2147483647>`, odd primes in `2^30 < p < 2^31`, or the canonical
64-bit prime `Field<18446744073709551557>` (`2^64 - 59`) carried as two u32
limbs (narrow Montgomery, Mersenne, and two-limb profiles), plus binary field
maps. Other workers emit a skip record — this is **not** generic 64-bit-prime
support.

**Limits and refusals.** Compiler and runtime share a **65,535-element
maximum grid**. Alias checking uses overflow-checked byte intervals (not
base-pointer-only), and pointer provenance is preserved only through trusted
typed paths: an i32 index wrap, provenance erased through an integer, an
uncertain control-flow assignment, or an exposed pointer-binding address all
refuse.

**Runtime dispatch (`--agx-dispatch`).** For a source that imports
`src/lib/gpu/agx_runtime.tv`, this mode emits a normal **host** program that
tries the matching AGX worker by ID and otherwise runs the unchanged CPU
pfor. The host embeds the FNV-1a digest produced by the shared AGX lowering
path; the runtime verifies worker ID, field, grid, and code digest before
submission. This is a deterministic wrong-build guard, not cryptographic
artifact authentication. Absence, an unsupported worker, alias uncertainty,
an artifact mismatch, or a launch refusal all fall back to the CPU worker.

**Submission runtime.** The in-tree Traveler runtime links only IOKit and
libSystem — no Metal, Foundation, IOGPU, Objective-C, or project C object —
and requires a regenerated machine-specific `AGXDISP3` profile image for the
exact OS/GPU build (deliberately not shipped as a portable ABI). It refuses
any service build, initialization fingerprint, profile call shape, or
GPU-address allocation order outside the measured profile. Build/run
instructions and the hardware parity gates are in `BUILD.md`.

---

## 19. Graphics (`src/lib/gfx/`)

> **Provided by the standard library** (`src/lib/gfx/`), not a language
> builtin — the same seam as `fs/`, `net/`, and `time/`: `extern "C"`
> against the OS, `Result` at the membrane, C structs hand-packed to the
> target ABI, no new dependencies.

A CPU framebuffer plus two backends:

- **`framebuffer.tv` / `pixel.tv` / `event.tv`** — a pure integer pixel
  buffer (`u8` channels packed into `u32`; buffers are `*T` plus a length),
  drawing primitives, and the event type.
- **`backend_headless.tv`** — draws into the framebuffer and writes a P6 PPM
  (`gfx_write_ppm`); no compositor.
- **`wayland/backend_wayland.tv`** (with `wayland/wl_wire.tv`,
  `wayland/client.tv`) — a real window on Linux over the **raw Wayland
  protocol**, no libwayland. The OS floor is `net/unix.tv` + `mem/shm.tv`.

**The backend API** is five functions over the library `Window` /
`Framebuffer` / `Event` types, exported identically by each backend:

```
fn gfx_open(w: i32, h: i32, title: *u8) -> Result<Window, GfxError>
fn gfx_frame(win: *Window) -> *Framebuffer
fn gfx_present(win: *Window) -> Result<i64, GfxError>
fn gfx_poll_event(win: *Window) -> Event
fn gfx_close(win: *Window)
```

**Backend selection is by import, not by value.** There is no `dyn Trait`
(§20.1), so there is no runtime vtable of backends: an application picks its
backend by which file it `import`s. Importing both backends in one unit is a
duplicate-definition error (§13.2) — that is the intended failure mode.

Two coexistence rules fall out of the hand-packed-POSIX floor: do not import
`net/tcp.tv` and `net/unix.tv` in the same unit (overlapping `extern "C"`
sets), and note `mem/shm.tv` deliberately declares no `close` so it can
coexist with `net/unix.tv`.

Examples: `examples/gfx_headless.tv`, `examples/gfx_window.tv`,
`examples/gfx_pixel_test.tv`, `examples/gfx_wire_test.tv`. Build commands are
in `BUILD.md`.

---

## 20. Reserved for Future Extension

### 20.1 Deliberately not planned

These are design lines Traveler holds, not backlog:

1. **`dyn Fn` / closure trait objects** (`Fn`/`FnMut`/`FnOnce` as boxed,
   type-erased values). Monomorphized stack-only closures ARE implemented
   (§3.10.1); only the erased form — a closure stored heterogeneously / escaping
   its frame, which would require a vtable — is refused (the Rubicon, §3.10.1).
2. **Dynamic trait dispatch (`dyn Trait` / vtables)**. Traveler dispatch is over
   *data* (the modulus), never *code* — user traits always monomorphize (§7.4).
   Runtime code-selection is expressed via function pointers (§3.10), not vtables.

### 20.2 Designed but not yet built (backlog)

Larger items with no in-body section of their own:

3. **Generics with lifetime annotations**: for complex borrowing patterns.
4. **Async streams**: for network I/O integration.
5. **FPGA/RTL backend**: emit Verilog from field operations.
6. **Lossy piecewise approximation** (`max_error > 0` in the piecewise type).
   (A lossy *audio* codec — `observe` wire v3, reduced-depth quantization — does
   ship as a library; this item is the language-level piecewise type, §3.4.)
7. **Constant evaluation engine**: full compile-time evaluation of field
   arithmetic (needed for top-level `const`, §4.3).
8. **Package manager**: dependency resolution and versioning.
9. **RAII / automatic destructors**: scoped deallocation for heap-owning types.
10. **`mem::move`**: overlapping memory copy (memmove equivalent).
11. **Multi-limb static primes** (`> 2^63` as a `Field<p>` *type*). Wide primes
    are already supported as a runtime carrier (`field_wide`, §16.16, §17.10.1);
    only a static wide `Field<p>` type parameter remains future.

### 20.3 Smaller planned surface (marked in the body)

Per the mark-in-place convention (see the front-matter legend), the following
designed-but-unbuilt features are flagged `NOT YET IMPLEMENTED` at their point
of definition. This is the index:

- **Types**: `DynPoly<F>` (§3.3.2); `Piecewise`/`Segment`/`Stream`/`Regime`
  types (§3.4, §3.5, §17.3–17.5); slices `[T]`/`&[T]` (§3.6); tuples/unit (§3.7);
  `type` aliases (§3.11); `*mut T` sugar (§3.12); `str = &[u8]` (§3.14).
- **Declarations/expressions/statements**: top-level `const` (§4.3); `stream`
  decls (§4.5); `segment()` (§5.6); `if`/`match` as expressions (§5.7);
  inclusive range `..=` (§5.8); `lift()`/`project()` (§5.9); let-destructuring
  (§6.1); compound assignment `+= -= *= /=` (§6.2, §2.10); field enumeration
  `for x in F` (§6.4).
- **Semantics/safety**: `unsafe` blocks (§14.3); the field division-by-zero /
  bounds / OOM traps and match-exhaustiveness / degree-barrier diagnostics
  (§14.1, §14.2, §16.10, §16.11); explicit `poly.to_newton()`/`to_standard()`
  (§9.1.3); namespaced `module::item` imports and `std::*` / `mem::*` / `io::*`
  modules (§13.2, §13.4–13.6).

### 20.4 Implemented since the earliest revision

Previously listed here as deferred, now implemented and specified in the body:

- **User traits + operator overloading** (§7.4) — static monomorphized `trait`/
  `impl`; `+ - * ==` desugar to `Add`/`Sub`/`Mul`/`Eq`.
- **Function pointers** (§3.10) and **monomorphized stack closures** (§3.10.1).
- **Generic structs / enums / const generics** (§3.8, §3.9).
- **`HashMap`/`Vec`/`Str`** stdlib collections (§3.13, §3.14, §13.4).
- **`extern "C"` FFI** — fully specified in §13.7 (this supersedes the earlier
  "extern C not yet specified").
- **I/O** — the `print` and `read_bytes` builtins plus `extern "C"` (§13.5);
  the namespaced `std::io` module remains planned.
- **Memory allocation** — `alloc`/`realloc`/`free` builtins (§12.3).
- **`#[zk]`/PLONK backend** (§15.19), **implicit SIMD** + **auto-parallelization**
  (§15.18), **dynamic and wide fields** (§17.10, §17.10.1).
- **Sequential computation** — `Register<F, d>` (§17.2) and the four operations;
  the `Regime`/`Segment`/`Stream` *types* remain library-level (§17.3–17.5).
- **Degree-persistence / genus signature** (§17.11).
- **`defer`** (§6.8) — function-spine exit cleanup on every return path,
  LIFO, including the `?` early-return unwind.
- **The `?` operator** (§5.10) — structural Result-shaped early-return
  propagation with exact `E` matching.
- **Library arena/pool** (§12.3, §13.4) — wholesale-free arenas and
  handle-based pools (`src/lib/mem/`).
- **Memory hardening** — compile-time refusal of returning a local address and
  of freeing a non-pointer (§14.1); opt-in `--alloc-debug` redzones and the
  evaluator heap-buffer registry (§12.3, §14.2).
- **Recursive parallel proofs** (§15.18) — recursive effect summaries,
  static-call targets, declaration-aware affine analysis, and
  fresh-allocation non-overlap; authoritative for CPU dispatch.
- **CPU middle-end profiles** (`--opt-level none|promote|o1`, §18.1) and the
  **TPC typed-pointer target** (`-target tpc`, §18.2).
- **GPU device emission** — AMDGCN/NVPTX Stage 0 (§18.3) and the direct
  AGX/G16X backend with content-checked dispatch and CPU fallback (§18.4).
- **Software graphics** — the CPU framebuffer plus headless and raw-Wayland
  backends (§19).
