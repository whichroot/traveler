# Arithmetic contract validation

Checkpoint: 2026-09-23, Linux x86-64, LLVM 21.1.8.

| Input | SHA-256 |
|---|---|
| `src/tvc_self.tv` | `a471fd8c6cfc7227ef8bcf80f5f3aec32461de5392bf64242a77076620be1b2f` |
| `src/bootstrap/tvc_self.boot.ll` | `e38353bd8562d12492fd23a3094f34f3bb32d8b23e530e303d29422b04233b0e` |
| `tests/check_arithmetic_contract.py` | `82b9594ef8bf677b6a0c5dd409df0b40d7d2a9451f11a28a15727245c97b8231` |

## Independent arithmetic gate

```sh
python3 tests/check_arithmetic_contract.py src/bootstrap/out/stage1 llc opt cc
```

- 978 exact outputs checked against Python integer oracles across evaluator,
  raw native, and O1 native execution.
- Shift boundaries cover signed and unsigned widths 8 through 256 bits.
- Division/remainder cover widths 8 through 64 bits, including signed minimum
  remainder `-1`, zero divisors, and unrepresentable signed quotients.
- 27 failure fixtures cover discarded results, prime/binary/extension/runtime
  field inversion, and dispatched loops with one and four threads. Extension
  evaluation is explicitly refused; its raw and O1 native failures are checked.
- Invalid field conversions preserve a pre-existing output file.
- NVPTX SM90 and AMDGCN gfx1100 modules verify and lower, including O1 forms.

With `--cuda /run/opengl-driver/lib/libcuda.so`, the same gate passed on Jane's
RTX PRO 6000 Blackwell (SM120, driver 595.99.02). An SM90 PTX package produced
32 exact arithmetic outputs. Separate processes checked that zero division and
signed overflow report positive CUDA driver errors at launch boundary 8.
These are execution checks on SM120, not SM90 hardware validation.

## Bootstrap and regression checks

- The arithmetic lowering change required one additional self-compilation
  generation during snapshot migration. The resulting Traveler-produced snapshot
  passes the normal two-generation fixed-point and `build.sh --check` freshness
  checks without C source in the bootstrap chain.
- Evaluator differential corpus: 104 programs pass, plus the independent gate.
- Parallel soundness: 65 pass.
- Full portable GPU suite: pass; hardware-unavailable legs report skips.
- Emit-driver suite: pass, including none/promote/O1 failure parity.
- Codegen corpus: 265 entries pass with the reviewed arithmetic baseline.
- LLVM-14 typed-pointer execution was unavailable locally.

All host IR hashes change because runtime-field inverse helpers are emitted in
every host module. A historical `field_basics.tv` IR comparison shows only the
new static, dynamic, and wide zero-inverse guards. The device-authority fixture
retains both worker bodies; its changes are the trap declaration and two inverse
guards. Integer division and shift users additionally gain checked helpers and
defined shift lowering. The earlier RNS dispatch drift was separately traced to
bounded alias dispatch; its checksum remains 2912256.

After canonical-harness migration, `tests/run.sh` reports **190 PASS, 0 FAIL,
2 SKIP**. The two named skips are frozen-seed diagnostics for
`instantiate_nongeneric` and `missing_return`. Sub-gates also report unavailable
hardware, LLVM-14, Node.js, and optional audio-corpus legs separately.

The dynamic-field suite passes all available phases, including BN254 arithmetic,
NTT, FRI, and PLONK. The regime-boundary example rejects a zero error before
inversion. Diagnostics pass 69 cases, formatting 417, LSP engine 83, and doc
generation five. The emit fixture uses a source with usable GPU workers.

The canonical dispatcher and tool gates use the C-free bootstrap. Frozen-seed
equivalence remains a separate, explicit `run_bootstrap.sh --legacy-seed` check;
this checkpoint does not claim frozen-seed compatibility.
