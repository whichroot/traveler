# K5-D asynchronous shared copies and K6 preparation validation

## Profiles

- [Asynchronous shared copies](../../spec/cuda-shared-async.md): bounded u64
  global-to-shared transfers, one outstanding group, explicit commit/wait,
  block visibility, and arena reuse.
- [Prepared package reuse](../../spec/cuda-prepare.md): compiler-resolved source
  snapshots, source/toolchain/target identity, verified PTX cache records, and
  atomic publication. Persisted native cubins are outside this profile.

## Portable acceptance

`check_shared_async.py` passes with LLVM 21:

- Static-arena lowering for SM80/90/120, dynamic-arena lowering for SM80, and
  O1 retention of copy/commit/wait instructions.
- Compiler-derived `async-shared-u64-v1`, SM80/PTX7.0 floors, and count-parameter
  source footprints.
- Refusal of missing initialization, missing/duplicate commit, missing wait,
  early barriers, early shared reads/stores, duplicate issues, unfinished groups,
  commit without issue, missing visibility barriers, and reuse before readers
  complete.

`check_cuda_prepare.py` passes:

- Cold build followed by a warm hit with identical bytes and request key.
- Relocation under a new source root, including identical cold-build output.
- Transitive dependency and explicit IEEE-policy changes invalidate identity;
  unimported files do not.
- Target, compiler executable, and toolchain-closure changes invalidate identity.
- Corrupt/truncated records, bad digests, wrong request ABI/type, wrong artifact
  identity, and target-incompatible cached packages are rebuilt.
- Concurrent builders publish complete equivalent packages.
- Failed dependency discovery preserves the previous output. Absolute imports
  are refused by the closed snapshot profile.
- Produced packages pass Traveler's package reader.

## Native acceptance on Jane

Device: NVIDIA RTX PRO 6000 Blackwell Workstation Edition, SM120, driver
595.99.02. GPU process queries were empty before execution. Runs used the
isolated `/tmp/traveler-k5.pITtS2` workspace and LLVM 21 environment.

| Input package | Launches | u64 output/guard checks | Result |
|---|---:|---:|---|
| SM80-targeted PTX | 15 | 1,950 | PASS |
| SM90-targeted PTX | 15 | 1,950 | PASS |
| SM120-targeted PTX | 15 | 1,950 | PASS |
| Warm-cache prepared SM90 package | 15 | 1,950 | PASS |

Each launch performs two copy phases into the same 64-slot shared arena, with
peer reads and a reader-completion barrier between phases. Counts 0, 1, 33, 127,
and 129 exercise zero filling and tail boundaries. Block sizes 32, 64, and 128
exercise multi-block execution and destinations outside the arena. The oracle
computes exact peer-exchange and shifted-source sums plus surrounding guards.
Dynamic shared copies have portable lowering coverage here; native runs use
the static arena.

The native preparation check builds cold, reuses the warm record, and executes
the published package through the resident runtime. A second package contains
deliberately invalid PTX with a recomputed PTX hash and otherwise valid metadata.
The driver rejects it with `CudaError.boundary == 4`, a nonzero status, and a
nonempty diagnostic log. The same device context subsequently loads a valid
package and closes successfully.

These are correctness and reuse checks. No GPU-overlap, cache-hit-rate, isolated
kernel-timing, or performance claim is made. SM80/90-targeted PTX execution on
SM120 is forward-JIT compatibility evidence, not SM80/90 hardware validation.

## Gates and source identity

The full portable GPU gate and all 65 pfor cases passed. Bootstrap refresh
reached a fixed point and passed snapshot freshness. The final cache request
type-hardening change was followed by the dedicated preparation gate. The final
unfinished-group refusal fixture was followed by the shared-copy portable gate.

```sh
python3 tests/gpu/check_shared_async.py "$TVC" "$LLC" "$LINK" --opt "$OPT"
python3 tests/gpu/check_cuda_prepare.py "$TVC" "$LLC" "$LINK"
python3 tests/gpu/check_shared_async.py "$TVC" "$LLC" "$LINK" --opt "$OPT" \
  --cuda /run/opengl-driver/lib/libcuda.so
```

Implementation checkpoint SHA-256 identities:

| File | SHA-256 |
|---|---|
| `src/tvc_self.tv` | `cc7c61a4c36fe89b0a25c4c2e2dc0d8625b761eeabbd1f538010d5810f57821f` |
| `src/bootstrap/tvc_self.boot.ll` | `cc0a4aef484b0523534b60aae7436283b68f4020cda4c296fa98e9eeaec60952` |
| `src/lib/gpu/shared.tv` | `ea30db9525cf02e802bc0e3c31ca0bec54b19afd1fba94b787c35c3e7da4a78a` |
| `src/lib/gpu/cuda_package.tv` | `9a413b8c592f5f7e05a51ddd397aac00928051345f0510a1c7a0bc0f5cb9d0b1` |
| `tools/cuda_prepare.py` | `b46444436203b530c32bf8376e75e7ef32dcfb3a8cfc02ad8d4472b595068c73` |
| `tools/cuda_package.py` | `6071378e59b1b035ee30a96bb9694b64b3df5f36b8194f54996b5247352b6587` |
| `tests/gpu/check_shared_async.py` | `76b574acc368134ddb35c75d039a75180c68607cf4b2b724edb9cee024a0cbfb` |
| `tests/gpu/check_cuda_prepare.py` | `e95664b7944f5e213d4f32fa2a80d06f67c6e405bfd3c3f81af476a2f64ee48d` |
