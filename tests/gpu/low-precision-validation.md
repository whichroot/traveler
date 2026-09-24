# Low-precision and capability validation

Date: 2026-09-23. Native target: NVIDIA RTX PRO 6000 Blackwell Workstation
Edition, SM120, driver 595.99.02. LLVM 21 and linker driver `cc`.
Native execution used isolated `/tmp/traveler-k5.pITtS2` on Jane. The GPU had no
reported compute processes before acceptance; GPU settings were not changed.

## Encoding and packed arithmetic

The gate exhausts all 65,536 binary16 and all 65,536 bfloat16 encodings for both
widening and round trip. Additional checks cover binary32 narrowing around exact
midpoints and neighboring bit patterns, exceptional values, and random packed
FMA operands. Oracles use standard binary16 encoding, exact rational midpoint
selection for bfloat16, and exact-rational binary32 FMA. Packed lane order and
both output carriers are checked.

```text
Low precision native SM120, PTX SM90: 268948 exact results PASS
Low precision native SM120, PTX SM120: 268948 exact results PASS
Low precision portable: 268948 exact results, exhaustive encodings, rounding boundaries, packed FMA PASS
```

## Packed resident projection

The projection stores four 16-bit values in each `u64`. It accumulates even and
odd columns in two independent binary32 FMA chains, then adds those accumulators
once. Cases cover both formats, two preloaded experts, changing bias, block sizes
7 and 32, and `(rows,tokens,columns)` of `(1,1,1)`, `(5,3,3)`, and `(17,4,8)`.
Inactive-column poison values are ignored, grid tails become zero, and output
guards remain unchanged. The oracle uses the explicitly selected reduction order.

```text
Low projection native SM120, PTX SM90: 12 resident launches, 664 exact output/guard checks PASS
Low projection native SM120, PTX SM120: 12 resident launches, 664 exact output/guard checks PASS
Low projection portable: 664 exact output/guard checks, packed storage and ordered f32 accumulation PASS
```

The checked runtime admitted the compiler-derived combined binary32/bounded-read
requirements and validated the resident views and launch geometry. Both PTX
targets executed on SM120; this is not an SM90 hardware test. These results make
no throughput or native packed-instruction claim.

## Capability and regression checks

- Closed requirements: precision composition, bounded reads, malformed/unknown/
  duplicate/unordered lists, legacy descriptors, and PTX floor checks PASS.
- Full portable GPU regression suite, including scalar IEEE, numerical utilities,
  both projection gates, and cooperative/async/graph tests: PASS.
- Raw and O1 host execution: PASS.
- Refreshed bootstrap fixed point and freshness: PASS, without C source in the
  bootstrap chain.

## Source identity

| File | SHA-256 |
|---|---|
| `src/tvc_self.tv` | `92a25c0b5a4fb41b4848f6db2fccb41905f627481a7b004bb5726fb20369bf21` |
| `src/bootstrap/tvc_self.boot.ll` | `41e28988a016cc91ed767f90ef098a61e1063b156105a226764119c49b3382b4` |
| `src/lib/float/numeric.tv` | `27c4b7059755a66b8ed6f8a3b2a59e9f3dc29bf6a63a25d40f371897296d53a2` |
| `src/lib/float/low_precision.tv` | `9c094d5a35a737d6c78fd8f5d97fe19065d2130de28bd64c1a85e373fd34d37c` |
| `tools/cuda_package.py` | `cc902253fb98d646afe702da1a64d663fe1233106a920d4c03d266059e1f16c3` |
| `tests/gpu/check_low_precision.py` | `a93cd1d345593989f4c72c3329fc617cfb04204192d71313cb54c76e363e265b` |
| `tests/gpu/check_low_projection.py` | `98494ce1cd1f877d580e842fd049307c9bfd1b036d5e2b7e701a00ae598c7aee` |
| `tests/gpu/cuda_low_projection.tv` | `c837d274dc978cc8837adf376b8fd48ae1339d203c5286a3054c04dae60bf140` |
