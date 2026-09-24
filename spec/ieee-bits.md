# IEEE bit-pattern numerical boundary

## Profile `ieee-bits-rne-v1`

Traveler has no floating-point types or literals. Ordinary integer and field
operators retain their existing meanings. These explicit compiler operations
interpret unsigned integer bit patterns as IEEE 754 binary32 or binary64 values
at the backend boundary. Arguments and results remain integers in source,
function signatures, memory, and CUDA interfaces.

| Operation | Arguments | Result |
|---|---|---|
| `ieee32_add`, `ieee32_sub`, `ieee32_mul`, `ieee32_div` | two `u32` carriers | `u32` carrier |
| `ieee64_add`, `ieee64_sub`, `ieee64_mul`, `ieee64_div` | two `u64` carriers | `u64` carrier |
| `ieee32_sqrt`, `ieee64_sqrt` | one carrier of the named width | same width |
| `ieee32_fma`, `ieee64_fma` | three carriers of the named width | same width |
| `ieee32_to_ieee64` | one `u32` carrier | `u64` carrier |
| `ieee64_to_ieee32` | one `u64` carrier | `u32` carrier |

Argument counts and unsigned carrier types must match exactly. Integer literals
receive the expected carrier type. A declared function with the same name
shadows a builtin; otherwise these call names have builtin precedence.

```tv
fn main() {
    let one: u32 = 1065353216;       // binary32 1.0: 0x3f800000
    let two: u32 = ieee32_add(one, one);
    print(two);                     // 1073741824: 0x40000000
    let wide: u64 = ieee32_to_ieee64(two);
    print(wide);                    // 4611686018427387904: binary64 2.0
}
```

`1 as u32` is the bit pattern of the smallest positive binary32 subnormal,
not the encoding of 1.0. An integer cast changes the carrier integer; it does
not convert its interpreted IEEE value. The two precision-conversion operations
perform numerical conversion. Merely loading, storing, or passing a carrier
preserves its bits, including NaN payloads.

## Arithmetic contract

- Each operation rounds to nearest, ties to even. Widening is exact for finite
  values. Narrowing rounds once.
- Subnormal operands and results are preserved. Flush-to-zero is disabled.
- Every NaN result, including a converted NaN, becomes the positive quiet NaN
  `0x7fc00000` or `0x7ff8000000000000`. NaN signs and payloads are not propagated
  through numerical operations. Signaling NaNs produce the same canonical NaN.
- Signed zeros and infinities follow IEEE arithmetic. Overflow rounds to signed
  infinity when required. Division by zero produces infinity or NaN as specified
  by IEEE arithmetic; it does not cause an integer division trap.
- `fma(a, b, c)` rounds the exact product-plus-sum once. A separate `mul` followed
  by `add` rounds twice. The compiler cannot contract these separate operations.
- Arithmetic cannot be reassociated. No fast-math, approximate division, or
  approximate square-root policy is selected implicitly.
- No floating-point status flags, traps, or dynamic rounding mode are exposed.
  Operations are pure value computations, subject to the effects of evaluating
  their arguments. The purity and indexed-access analyses inspect those arguments.

Compiled host execution uses LLVM constrained intrinsics with explicit nearest
rounding and ignored exceptions; exact widening uses LLVM `fpext`.
The host execution contract requires the standard IEEE
environment: nearest-even rounding, gradual underflow, and masked exceptions.
Foreign code must restore that environment before returning to Traveler.
Floating-point status flags are outside the observable language state.

## Integer-library numerical utilities

Import `src/lib/float/numeric.tv` for explicit integer conversion and numerical
predicates. These are pure integer library algorithms admitted through ordinary
typed device-function expansion. They do not depend on a floating-point
environment or a new hardware numerical capability.

| Function family | Contract |
|---|---|
| `ieee32_from_i32/u32/i64/u64`, `ieee64_from_i32/u32/i64/u64` | Convert the integer value to an IEEE carrier, nearest-even. |
| `ieee32_to_i32_sat/u32_sat/i64_sat/u64_sat`, corresponding `ieee64_*` | Truncate toward zero and clamp to the integer destination range. NaN maps to zero; infinities map to the corresponding endpoint. |
| `ieee32_eq/lt/le`, `ieee64_eq/lt/le` | Ordered numerical comparison returning `bool`. Any NaN makes the result false. Signed zeros compare equal. |
| `ieee32_classify`, `ieee64_classify` | Return `u32`: zero 0, normal 1, subnormal 2, infinity 3, NaN 4. |
| `ieee32_is_nan/is_finite`, `ieee64_is_nan/is_finite` | Return `bool` classification predicates. |

For example, `ieee32_from_i32(-3)` produces `0xc0400000`, whereas `-3 as u32`
is an integer cast. `ieee32_to_u32_sat(0xc0400000)` produces zero.
`ieee32_eq(0, 0x80000000)` is true, whereas integer equality compares those
carriers as unequal. `!ieee32_eq(a, b)` includes unordered NaN cases; it is not
the same predicate as ordered greater-than or less-than.

All finite encodings use integer significands and exponents. Rounding tests the
entire discarded suffix, including ties and the 64-bit shift boundary. The
public wrappers fix format parameters; format-parameter helper functions are
implementation details. NaN classification does not modify the input payload.

The host compiler normalizes ABI `bool` values to logical `i1` before `!`, `&&`,
and `||`. RHS evaluation remains short-circuited, including side-effecting calls.

## Binary16, bfloat16, and packed pairs

Import `src/lib/float/low_precision.tv` for these integer-carrier operations:

| Operation | Contract |
|---|---|
| `ieee16_to_ieee32(u16) -> u32` | Exact finite binary16-to-binary32 widening. |
| `ieee16_from_ieee32(u32) -> u16` | Nearest-even narrowing to binary16. |
| `bfloat16_to_ieee32(u16) -> u32` | Exact finite bfloat16-to-binary32 widening. |
| `bfloat16_from_ieee32(u32) -> u16` | Nearest-even narrowing to bfloat16. |
| `ieee_pack_u16x2(low: u16, high: u16) -> u32` | Pack lane zero into bits 0–15 and lane one into bits 16–31. |
| `ieee16x2_fma32(a: u32, b: u32, accumulators: u64) -> u64` | Two binary16 operand pairs widen and independently accumulate with binary32 FMA. |
| `bfloat16x2_fma32(a: u32, b: u32, accumulators: u64) -> u64` | The corresponding bfloat16 operation. |

Narrowing preserves signed zeros and gradual underflow. Overflow rounds to
infinity where required. All NaN conversions produce positive quiet NaN:
`0x7e00` for binary16, `0x7fc0` for bfloat16, and `0x7fc00000` for binary32.

Packed FMA inputs contain two 16-bit lanes; the accumulator and result contain
two 32-bit lanes in a `u64`, low lane first. Each lane performs one binary32 FMA
with the scalar profile's rounding and NaN rules. Narrowing is a separate call.
This operation does not promise single-rounding binary16 FMA or a native packed
half instruction. Conversion and packing use integer algorithms; packed FMA
requires the binary32 arithmetic capability.

The low-precision projection fixture stores four 16-bit values per `u64`, with
eight physical columns per row. Its explicit reduction order uses one binary32
FMA chain for even columns and one for odd columns, followed by one binary32
addition. Bias initializes the even accumulator; the odd accumulator starts at
positive zero. Inactive columns preserve the prior accumulator bits. Output
slots are `u64`, with the binary32 result in the low 32 bits. This is a separate
source-level reduction order from the ascending-column binary64 fixture.

## Composable capability requirements

The compiler derives an optional ordered `requires` array from verified device
IR. Execution profile, numerical policy, and these requirements compose; the
effective target must satisfy all of them.

| Capability, in canonical array order | Minimum SM | Minimum PTX |
|---|---:|---:|
| `ieee32-rne-v1` | 50 | 4.0 |
| `ieee64-rne-v1` | 50 | 4.0 |
| `warp-full32-v1` | 70 | 6.0 |
| `bounded-read-u64-v1` | 70 | 6.0 |
| `mma-f16-m16n8k16-native-v1` | 80 | 7.0 |
| `mma-bf16-m16n8k16-native-v1` | 80 | 7.0 |
| `async-shared-u64-v1` | 80 | 7.0 |

These are profile floors, combined with the existing execution and footprint
requirements. Precision conversion requires both IEEE capabilities. A binary32
projection using bounded reads requires `ieee32-rne-v1` and
`bounded-read-u64-v1`. Integer-only format conversion has no additional numerical
instruction requirement.

Packager and runtime reject unknown, duplicate, unordered, empty, or malformed
capability lists, incompatible numerical/warp profiles, and insufficient SM/PTX
versions. Legacy descriptors without `requires` retain their existing profile
checks. The actual driver must support the emitted PTX version; module JIT
diagnoses incompatibility before launch. No architecture-specific target suffix
is implied by a capability or GPU marketing name.

Native tensor capabilities require an additional explicit policy and full-warp
participation. See [CUDA native tensor operations](cuda-native-tensor.md).
The `ieee-bits-rne-v1` contract continues to govern scalar IEEE operations and
the reproducible packed library operations, including in mixed kernels.
The [asynchronous shared-copy capability](cuda-shared-async.md) adds no arithmetic
policy.

## Lowering, dependencies, and admission

On CUDA, input/output reinterpretation uses LLVM bitcasts. Arithmetic lowers
through explicit PTX `add.rn`, `sub.rn`, `mul.rn`, `div.rn`, `sqrt.rn`, and
`fma.rn` instructions with `.f32` or `.f64` precision. Precision conversion uses
`cvt.f64.f32` and `cvt.rn.f32.f64`. The operations do not use `.ftz` or approximate
instructions. These opaque operations retain rounding boundaries through LLVM
optimization. The CUDA driver JIT performs final machine-code generation.

Numerical kernels add `"numerical":"ieee-bits-rne-v1"` to the existing kernel
descriptor. This is separate from the execution profile. The packager and checked
runtime reject unknown numerical profiles. Integer argument/view type codes,
layouts, footprints, alias rules, and resource lifetimes still apply. The PTX hash
binds the packaged implementation. Older runtime readers reject the added field.

This profile uses instructions within the packager's existing SM50 minimum.
SM90 and SM120 lowering are tested explicitly; no architecture-specific target
variant is required. An SM120-targeted package cannot launch on SM90. Testing
SM90-targeted PTX on SM120 checks forward JIT compatibility, not SM90 hardware.

Host LLVM lowering may use system `libm` for square root or fused multiply-add.
Manual native links must include `-lm`; `--emit exe` adds it when the module uses
these builtins. There is no CUDA device-math library dependency and no new C
source in the compiler bootstrap trust chain. Validation uses LLVM 21.

The tree evaluator refuses the compiler arithmetic intrinsics. AMDGCN device
emission refuses explicit kernels containing those intrinsics; this profile does
not add arithmetic intrinsics to AGX or Vulkan. Native floating types, min/max,
transcendental functions, alternate rounding, native low-precision arithmetic
instructions, and tensor operations remain outside the current profile.

## Verification

`tests/gpu/check_ieee_bits.py` generates compiled host and CUDA kernels from the
same operation cases. Its independent oracle uses exact Python rational
arithmetic and adjacent-encoding rounding, including an exact midpoint test for
square root. It covers exceptional values, subnormals, rounding boundaries,
random bit patterns, precision conversion, FMA versus separate operations, and
ordered sums that distinguish reassociation.

The gate checks unoptimized and optimized host output, the executable driver,
pure-helper parallelization, SM90/SM120 PTX, package-policy rejection, argument
rejection, declaration shadowing, evaluator refusal, and AMDGCN refusal. With a
CUDA driver it executes through Traveler's checked resident runtime and compares
every returned bit pattern. See
[`tests/gpu/ieee-bits-validation.md`](../tests/gpu/ieee-bits-validation.md).

`tests/gpu/check_ieee_numeric.py` checks the integer-library utilities against
independent integer/rational oracles and tests predicate short-circuit effects.
`tests/gpu/check_numeric_projection.py` checks a bounded binary64 resident
projection, including separate/fused policies, ascending-column reduction order,
inactive-column poison values, grid tails, guard storage, and repeated expert
selection. See [K5-B validation](../tests/gpu/numeric-projection-validation.md).

`tests/gpu/check_low_precision.py` exhausts both 16-bit encodings and checks
narrowing boundaries and packed accumulation. `tests/gpu/check_low_projection.py`
validates the packed resident workload. See
[K5-C validation](../tests/gpu/low-precision-validation.md).
