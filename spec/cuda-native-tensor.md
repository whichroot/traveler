# CUDA native tensor operations

## Opt-in numerical policy

Import `src/lib/gpu/tensor.tv` and explicitly call either operation:

```tv
gpu_native_mma_f16_m16n8k16(a: u128, b: u64, c: u128) -> u128
gpu_native_mma_bf16_m16n8k16(a: u128, b: u64, c: u128) -> u128
```

These calls opt in to `cuda-mma-native-v1`. Each warp computes `D = A * B + C`,
with A shaped 16×16, B shaped 16×8, and C/D shaped 16×8. Multiplicands use
binary16 or bfloat16 bit patterns; accumulators and results use binary32 bit
patterns. All source arguments and results remain integer carriers.

The operation follows PTX `mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32`
or its `bf16.bf16` form. PTX specifies at least single-precision accumulation
but leaves accumulation order, rounding, and subnormal handling unspecified.
Results are hardware-defined within those PTX guarantees. There is no promise
of cross-device or cross-driver bit identity, scalar FMA-chain equivalence,
canonical NaNs, or preservation of the scalar profile's gradual underflow.

Calling a native operation is the source-level opt-in; importing the declarations
alone does not opt in. The compiler never replaces scalar IEEE or packed library
operations with tensor instructions. For reproducible operations, use the
`ieee-bits-rne-v1` operations and `src/lib/float/low_precision.tv`.

## Lane fragments

Let `lane` be the x-fastest physical lane index modulo 32, `g = lane / 4`, and
`t = lane % 4`. Pack the elements in increasing index order, least significant
bits first. No pointers or native floating-point source types cross this API.

| Carrier | Element width | Elements | Matrix coordinates for element j |
|---|---:|---:|---|
| A: `u128` | 16 | 8 | row `g + (j % 4 >= 2 ? 8 : 0)`, column `2*t + j%2 + (j >= 4 ? 8 : 0)` |
| B: `u64` | 16 | 4 | row `2*t + j%2 + (j >= 2 ? 8 : 0)`, column `g` |
| C/D: `u128` | 32 | 4 | row `g + (j >= 2 ? 8 : 0)`, column `2*t + j%2` |

The conditional expressions in this table are mathematical notation. The four
32-bit result registers are packed into one `u128`, so extracting multiple
elements from a stored result does not repeat the matrix operation.

## Participation and admission

The initial profile permits calls directly in the straight-line body of an
explicit cooperative `GpuThread` kernel on NVPTX. Calls inside helpers,
conditional regions, and independent kernels are refused. All 32 physical
lanes execute the same operation. Checked launch admission requires complete
physical warps through `cooperative-warp-v1`; logical output tails do not exempt
physical lanes from participation. Each warp acts independently.

The compiler emits `"tensor":"cuda-mma-native-v1"` and the appropriate ordered
capability, `mma-f16-m16n8k16-native-v1` or `mma-bf16-m16n8k16-native-v1`, plus
`warp-full32-v1`. Both tensor forms require SM80 and PTX 7.0 or later.

Both package validators reject unknown policies, policies without tensor
capabilities, tensor capabilities without the policy or full-warp capability,
incompatible execution profiles, and insufficient SM/PTX versions. The checked
runtime also applies its normal device compatibility, bounds, alias, and
resource-lifetime checks. Old descriptor readers reject the additional field.

A mixed kernel can carry both `"numerical":"ieee-bits-rne-v1"` and the tensor
policy: each applies to its own explicit operations. Tensor results subsequently
passed to scalar operations are input bit patterns to those scalar operations.
The scalar contract does not make the preceding tensor arithmetic reproducible.

The native declarations have no CPU emulation implementation. They do not add
native tensor support to AMDGCN, AGX, or the tree evaluator. LLVM emits PTX; the
CUDA driver performs native JIT compilation. No nvcc or C device source is used.

## Evidence and reference

See [native tensor validation](../tests/gpu/native-tensor-validation.md) and
`tests/gpu/check_native_tensor.py`.

The authoritative fragment and arithmetic definitions are NVIDIA PTX ISA
[matrix fragments](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#warp-level-matrix-fragment-mma-16816-float)
and [mma](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#warp-level-matrix-instructions-mma).
