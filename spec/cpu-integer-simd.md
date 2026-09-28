# CPU integer SIMD

Native CPU codegen provides fixed-width pointer builtins. These are standalone
statements, not vector-valued C ABI extensions. User function declarations with
the same names take precedence. Evaluation and device/AGX emission refuse them.

## Operations

```tv
cpu_dpbusd_512(dst, accumulator, unsigned_bytes, signed_bytes);
cpu_madd52lo_512(dst, accumulator, a, b);
cpu_madd52hi_512(dst, accumulator, a, b);
cpu_shuffle8_512(dst, table, indices);
cpu_div_u53_512(dst, numerator, denominator);
cpu_ratio_q32_512(dst, numerator, denominator);
```

Every operand points to 64 valid bytes. Destinations must be writable. Alignment
is unrestricted. Each operation reads its input vectors before writing the
destination, so an output may alias an input. Pointer operands are evaluated
once. These are ordinary non-atomic memory operations.

| Builtin | Pointer types, in argument order | Lane semantics |
| --- | --- | --- |
| `cpu_dpbusd_512` | `*i32, *i32, *u8, *i8` | Sixteen i32 lanes. Add four adjacent unsigned-byte × signed-byte products to each accumulator lane, wrapping modulo 2^32. No saturation. |
| `cpu_madd52lo_512` | Four `*u64` | Eight lanes. Mask each multiplicand to 52 bits, multiply exactly, add product bits 0–51 to the accumulator modulo 2^64. |
| `cpu_madd52hi_512` | Four `*u64` | As above, adding product bits 52–103. |
| `cpu_shuffle8_512` | Three `*u8` | Four independent 16-byte blocks. Each index selects within its block using its low four bits; bit 7 produces zero. |
| `cpu_div_u53_512` | Three `*u64` | Eight exact floor quotients. Requires numerator < 2^53 and 0 < denominator < 2^53. |
| `cpu_ratio_q32_512` | Three `*u64` | Eight RNE(numerator × 2^32 / denominator) results. Requires numerator ≤ 2^32 and 2^32 ≤ denominator ≤ 2^33. |

The bounded division operations use floating-point quotient estimates followed
by exact integer remainder corrections. No fast-math flags are used; only the
corrected integer result is observable. Their numeric domains are caller
preconditions, not runtime-checked bounds.

## Target selection

`--opt-level o3 -mcpu sapphirerapids` selects explicit VNNI, IFMA52, and byte-shuffle
intrinsics. Other native CPU profiles use portable LLVM implementations. The
Sapphire Rapids binary requires that target's hardware and OS vector-state
support; this interface does not add runtime dispatch.

**Pass the CPU to Traveler itself.** Compiling generic raw IR and subsequently
passing `-mcpu=sapphirerapids` only to `opt`/`llc` does not select the explicit
intrinsic implementations. Target selection occurs during Traveler codegen.

## Routed expert library

`src/lib/nn/cpu_expert.tv` supplies the complete integer chain:

```tv
cpu_expert_scratch(m, d, f); // Result<CpuExpertScratch, CpuExpertError>
cpu_expert_run(record, input, output, &scratch, beta, linear_beta, clocks);
cpu_expert_scratch_free(&scratch);
```

Shapes require positive token count and positive dimensions divisible by 64.
The constructor rejects shape/size overflow. The caller supplies the complete
record, `m*d` input/output i64 lanes, positive Q32 beta parameters, and exclusive
scratch ownership. Do not use freed scratch or independently free shallow copies.

Record order is W1 packed weights/scales, W3 packed weights/scales, then W2 packed
weights/scales. W1/W3 are `[f,d]`; W2 is `[d,f]`. Each group of 32 has one scale
byte, with scale 255 excluded. Block-18 inputs require absolute value below 2^62.
All buffers must cover their stated shapes; raw pointer lengths are not checked.

The dot implementation prepares unsigned byte planes, uses non-saturating VNNI,
widens before exponent shifts, and retains wrapping i64 reduction. Shift counts
at least 64 produce zero. Q32 output rounding uses the signed i128 reference
operation. Planes are shared between W1 and W3.

SiTU uses IFMA52 limbs for exact products in the documented magnitude domain.
Whole eight-lane chunks use the vector path when both beta parameters are integer
multiples of 2^32 with multipliers 1–255. Other positive parameters, tail elements,
and INT64_MIN lanes use scalar Q32. Rounding is nearest, ties to even.

`clocks` is null or five accumulated nanosecond counters: input block-18/planes,
W1+W3, SiTU, intermediate block-18/planes, W2. These calls add no thread pool;
callers can assign independent experts and scratch instances to workers.

Lower-level helpers are in `cpu_mxfp4.tv`, `cpu_situ.tv`, and `cpu_q32.tv`.
`cpu_mxfp4_planes` requires `m*3*k` output bytes. `cpu_mxfp4_rows` requires
`e[k/32]` and `weights[k]` scratch, `k` divisible by 64, and `0 ≤ r0 ≤ r1 ≤ n`.
Its mantissa contract is `|mantissa| ≤ 2^18`. These helpers assume valid ranges.

## Verification and Jane benchmark

```sh
python3 tests/check_cpu_simd.py "$TVC_SELF" "$LLC" "$OPT" "$LINKER"
python3 tests/check_cpu_expert.py "$TVC_SELF" "$LLC" "$OPT" "$LINKER"
python3 tools/build_jane_cpu_expert.py /storage/jane-dev /tmp/jane-cpu-simd
sudo /tmp/jane-cpu-simd/jane_cpubench_simd_spr 128 32
sudo /tmp/jane-cpu-simd/jane_cpubench_simd_spr 8 32 4
```

The builder uses Jane's existing benchmark/reference chain and substitutes
`examples/jane_cpu_expert.tv` through temporary import copies. It does not edit
Jane's sources. `--no-link` verifies IR, object, and assembly generation without
the CUDA library. `--cuda-lib` selects the driver library directory for linking.

Gates compare primitive and kernel results with scalar C oracles, including full
synthetic routed records at one/four tokens, real dot widths, tails, wrapping,
rounding, aliases, and guards. Native AVX-512 execution is skipped when required
features are absent. Portable parity and instruction selection are not native
hardware acceptance: Jane must report `mismatched_words 0` on real records before
comparing cool-start throughput. No speedup is established by these gates.
