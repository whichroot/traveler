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

The tree evaluator refuses these numerical calls. AMDGCN device emission refuses
explicit numerical kernels; this profile does not add numerical support to AGX
or Vulkan. Native floating types, integer/IEEE numerical conversion, numerical
comparison/min/max, transcendental functions, alternate rounding, f16/bf16,
packed arithmetic, and tensor operations remain outside this initial profile.

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
