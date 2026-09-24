# Native tensor validation

## Accepted profile

`cuda-mma-native-v1` exposes explicit FP16/BF16 `m16n8k16` operations with FP32
accumulators through integer carriers. The public contract, fragment mapping,
and opt-in rules are in [the tensor specification](../../spec/cuda-native-tensor.md).

Portable validation with LLVM 21 passed:

- Both native instruction forms lower for SM80, SM90, and SM120.
- O1 retains the two matrix instructions. Calls carry `convergent noduplicate`
  and inline-assembly side effects.
- Compiler descriptors identify the native policy, precise format capability,
  and full-warp execution requirement. Mixed scalar/tensor kernels retain both
  numerical policies.
- Python and Traveler validators reject missing/unknown tensor policies,
  missing warp or tensor capabilities, duplicates, and incompatible profiles.
- SM75 packaging is refused. Runtime admission rejects downgraded SM/PTX
  manifests even with matching modified PTX and recomputed hashes.
- Independent kernels, helper calls, wrong carrier types, and conditional use
  are refused. AMDGCN does not emit native tensor instructions.

## Native execution

Native runs used Jane's NVIDIA RTX PRO 6000 Blackwell Workstation Edition,
SM120, driver 595.99.02. The GPU process query was empty before execution.
Runs used the isolated `/tmp/traveler-k5.pITtS2` workspace.

| PTX target | Physical device | Resident launches | Packed output/guard checks | Result |
|---|---|---:|---:|---|
| SM80 | SM120 | 4 | 1,040 | PASS |
| SM90 | SM120 | 4 | 1,040 | PASS |
| SM120 | SM120 | 4 | 1,040 | PASS |

Each format uses four distinct warp tiles, with permutation/basis and seeded
random matrix inputs, nonzero accumulators, and block sizes 32 and 64. A separate
integer matrix multiplication computes expected results before packing according
to the documented fragment map. Each launch checks both packed result halves
and surrounding guard words. A 31-thread block is refused by checked admission.

Multiplicands are integers from -4 through 4 and accumulators from 1 through 16.
Every product and possible partial sum is exactly representable in FP32. Exact
comparison is therefore appropriate for this subset without imposing a scalar
rounding or accumulation order on native MMA. These tests do not establish a
bit-exact contract for arbitrary floating inputs, exceptional values, or
subnormals. They are correctness checks, not performance measurements. Execution
of SM80/90-targeted PTX on SM120 does not validate SM80/90 hardware.

## Commands and regressions

```sh
python3 tests/gpu/check_native_tensor.py "$TVC" "$LLC" "$LINK" --opt "$OPT"
python3 tests/gpu/check_native_tensor.py "$TVC" "$LLC" "$LINK" --opt "$OPT" \
  --cuda /run/opengl-driver/lib/libcuda.so
```

The portable runner is included in `tests/gpu/run.sh`. The full portable GPU
gate passed, including scalar IEEE and exhaustive low-precision regressions.
The pfor suite passed all 65 cases. Bootstrap refresh reached a fixed point and
passed snapshot freshness; the final tensor-only convergence-attribute change
was followed by a fresh bootstrap, full GPU gate, and native tensor run.

## Source identity

SHA-256 identities for this implementation checkpoint:

| File | SHA-256 |
|---|---|
| `src/tvc_self.tv` | `340a858674d2b40a300424feba0f586930f0af8af68802fa820a08c9533d9a3f` |
| `src/bootstrap/tvc_self.boot.ll` | `407200f85e4b506ca18c5ec325f63990c4fc464e4e269bbc355362191c74b7e0` |
| `src/lib/gpu/tensor.tv` | `de73b38dab3a42484232575733ce57737ac6a0239f182d300aba45828e4bfec3` |
| `src/lib/gpu/cuda_package.tv` | `7131295d79dfb5ad0ecd113146cd5c7b9dbba4fefc08bd49305117e55aafc6c1` |
| `tools/cuda_package.py` | `f322ec8b6a32012301653507748505f46ca39e256734d311b8089c70d3f90dc8` |
| `tests/gpu/cuda_native_tensor.tv` | `d7be18f1ff189b9ad447aaa37bd5d703199a344ca4cc85ba3caf9d9d809b2def` |
| `tests/gpu/check_native_tensor.py` | `a04777370ae6bb8f1efcfbc9e4ac31822b16d3c728d9433fc9e17c00e9b697e2` |
