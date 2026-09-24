# Numerical utility and resident projection validation

Date: 2026-09-23. Base checkpoint: bf5b18d on staging.
Native device: NVIDIA RTX PRO 6000 Blackwell Workstation Edition, SM120,
driver 595.99.02. LLVM 21, native linker driver `cc`, system `libm`.
Native workspace: isolated `/tmp/traveler-k5.pITtS2` on Jane.

## Numerical utilities

`check_ieee_numeric.py` checks 6,280 cases across 28 kernels against independent
integer/rational oracles. Coverage includes signed and unsigned 32/64-bit integer
endpoints, power-of-two neighborhoods, nearest-even encoding, float-to-integer
saturation, NaNs/infinities, signed zeros, subnormals, and ordered comparison.

The library uses integer algorithms throughout. The predicate regression also
checks host ABI bool normalization under `!`, `&&`, and `||`, with side-effect
counts proving both skipped and evaluated RHS cases. Raw and O1 host execution
passed.

```text
IEEE utilities native SM120, PTX SM90: 6280 exact results PASS
IEEE utilities native SM120, PTX SM120: 6280 exact results PASS
IEEE utilities portable: 6280 exact results across 28 kernels PASS
```

## Resident projection

`cuda_numeric_projection.tv` computes binary64
`Y[token,row] = bias + sum_k W[row,k] * X[token,k]`, in ascending column order.
One lane owns each output. It uses the cooperative launch context and checked
bounded reads, with an inner dimension of 1–8 and physical stride 8. The two
explicit policies are FMA accumulation and separate rounded multiply/add.

The gate covers `(rows,tokens,columns)` equal to `(1,1,1)`, `(3,2,3)`, `(17,3,7)`,
and `(33,2,8)`, each with finite and exceptional-value fixtures. Two expert weight
buffers and one input buffer are uploaded once per fixture. Four subsequent
launches alternate expert buffers, change bias and policy, and use block sizes
1, 7, 32, and 64. Output initialization and validation transfers are explicit.

Inactive columns contain NaN/infinity poison values and must not contribute.
Grid-tail lanes write zero; guard words and unused output storage retain their
sentinels. Independent exact-rational arithmetic checks all numerical outputs.
Eleven host admission negatives cover empty/invalid dimensions, mismatched
counts, invalid policy, storage-byte overflow, and logical-grid overflow.

```text
Projection native SM120, PTX SM90: 32 resident launches, 2624 exact output/guard checks PASS
Projection native SM120, PTX SM120: 32 resident launches, 2624 exact output/guard checks PASS
Projection portable: 2624 exact output/guard checks, geometry/tail/policy admission PASS
```

Both targets executed through Traveler's checked resident runtime and CUDA driver
JIT on SM120. SM90 hardware was not tested. The GPU had no reported compute
processes before native acceptance. No GPU settings changed.

## Regression and bootstrap

- Full portable GPU suite, including both new gates: PASS.
- Auto-parallelization soundness: 65 PASS, 0 FAIL.
- Refreshed compiler snapshot: fixed point and freshness PASS, no C source
  compiled in the bootstrap chain.

These are correctness results, not throughput measurements. The codegen-golden
drift investigation remains deferred until after the K arc.

## Source identity

| File | SHA-256 |
|---|---|
| `src/tvc_self.tv` | `f65567709479b931c15f6e79ff545828f48140cb835afe699e9c34cca656996c` |
| `src/bootstrap/tvc_self.boot.ll` | `53bd158f0533f322f15cc0c00573a3e877d34eafbba88ad8323bb89c30c9e7f6` |
| `src/lib/float/numeric.tv` | `9abbee491f369a07bd3b7a3876a28650d3a843fa7351d67679381b3cc173763d` |
| `tests/gpu/check_ieee_numeric.py` | `d9c10ddaa5cc30a3f47bbc2df01e5553a856987fdf622221b40b324f5974e013` |
| `tests/gpu/cuda_numeric_projection.tv` | `a19367083f88bc5938344e60bd5c5f614018aaa4c10c2556374b09aa0acc85ee` |
| `tests/gpu/check_numeric_projection.py` | `476e804717e6882c4a2f22def2801a8dcd6520612baf578439635a6a4fb9c0ff` |
