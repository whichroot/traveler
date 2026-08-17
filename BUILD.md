# Building Traveler from scratch

This guide takes you from a clean checkout to a working, self-hosted
Traveler compiler that can compile `.tv` programs to native binaries.

Traveler is **fully self-hosted**: the compiler is written in Traveler
(`src/tvc_self.tv`), and the first binary is built from a checked-in
snapshot of *itself*, **no C compiler is in the
trust chain.** The canonical pipeline:

```
src/bootstrap/tvc_self.boot.ll  (Traveler-produced IR, committed)
   --llc-->  .o  --link-->  stage0   (the booted compiler)
stage0  --compiles-->  src/tvc_self.tv  -->  stage1   (canonical compiler)
*.tv --compiles--> raw LLVM IR --optional closed opt profile--> LLVM IR
    --llc -O2--> .o --link--> native binary
```

One command:

```sh
src/bootstrap/build.sh        # boots from the committed IR, asserts the fixed point
# -> src/bootstrap/out/stage1 is the canonical compiler. No C source compiled.
```

See `src/bootstrap/PROVENANCE.md` for why this is honest (the snapshot is a fixed
point; regenerate it with `src/bootstrap/refresh.sh`).

`src-legacy/tvc.c` is the original C bootstrap **seed**, now **optional** — kept only
as an independent provenance/audit path (the bootstrap gate proves the C-free
build is byte-identical to the C-seed build). The legacy seed pipeline below
still works if you prefer it.

## 1. Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| C compiler | clang or gcc | builds the `tvc` seed from `tvc.c` |
| LLVM | **15+** (required) | `llc` (IR → object); `opt` is optional for raw IR |
| make | any | drives `src-legacy/Makefile` |
| bash + coreutils | any | running the test suites |

LLVM 15+ is required to run the build. The `promote` and `o1` CPU middle-end
profiles described below initially require LLVM 21; profile `none` retains the
LLVM 15+ contract.

### macOS (Homebrew)

```sh
brew install llvm@21
# Apple clang (from Xcode Command Line Tools) is fine for compiling tvc.c.
```

LLVM tools land in `/opt/homebrew/opt/llvm@21/bin` (Apple Silicon) or
`/usr/local/opt/llvm@21/bin` (Intel). They are keg-only, so reference them
by full path or add the directory to `PATH`:

```sh
export PATH="/opt/homebrew/opt/llvm@21/bin:$PATH"
```

### Linux (Debian/Ubuntu)

```sh
sudo apt-get install clang-21 llvm-21   # provides llc-21, opt-21
```

Tools are typically under `/usr/lib/llvm-21/bin`.

### Nix / NixOS

```sh
nix develop   # LLVM 21 (llc/opt), cc, make, python3 on PATH
```

The committed `flake.nix` dev shell provides the full toolchain the test
gates consume (`tests/lib/env.sh` discovers everything from `PATH`),
including AMDGCN and NVPTX backends for `tests/gpu/run.sh`.


## 2. Build the compiler (canonical, C-free)

Boot the compiler from the committed Traveler-produced snapshot. **No C source
is compiled** — `cc`/`clang` is invoked only as the system linker.

```sh
export LLC=/opt/homebrew/opt/llvm@21/bin/llc   # your llc-21 path
src/bootstrap/build.sh
# -> src/bootstrap/out/stage1 is the canonical compiler.
```

`build.sh` boots `src/bootstrap/tvc_self.boot.ll`, compiles the current source,
and asserts the self-hosting fixed point (stage1 == stage2). Add `--check` to
also assert the committed snapshot is fresh, or `--target TRIPLE` to
cross-build.

Use the resulting compiler for everything:

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/fb.ll
$LLC -filetype=obj /tmp/fb.ll -o /tmp/fb.o && cc /tmp/fb.o -o /tmp/fb
/tmp/fb        # expected: 49 100 171 2 123 1
```

After an intentional change to `src/tvc_self.tv` that alters emitted IR,
refresh the snapshot (Traveler-produced, still no C) and commit it:

```sh
src/bootstrap/refresh.sh
git add src/bootstrap/tvc_self.boot.ll
```

See `src/bootstrap/PROVENANCE.md` for the full trust model.

## 3. (Optional) The legacy C-seed path

`src-legacy/tvc.c` is the original bootstrap seed — no longer required, kept as an
independent provenance/audit path. It produces a byte-identical compiler to the
C-free path (asserted by `tests/run_bootstrap.sh`).

```sh
cd src-legacy
make tvc                       # build the seed
make test                      # smoke test: 49 100 171 2 123 1
LLC=/opt/homebrew/opt/llvm@21/bin/llc

# Stage 1: seed compiles tvc_self.tv
./tvc ../src/tvc_self.tv -o /tmp/tvc_self.ll
$LLC -filetype=obj /tmp/tvc_self.ll -o /tmp/tvc_self.o
clang /tmp/tvc_self.o -o /tmp/tvc_self
```

`/tmp/tvc_self` is now the compiler to use for all `.tv` programs.

## 4. Compile a program

Use the canonical compiler from §2 (run these from the repo root):

```sh
TVC=src/bootstrap/out/stage1
$TVC examples/field_basics.tv -o /tmp/fb.ll
$LLC -filetype=obj /tmp/fb.ll -o /tmp/fb.o
cc /tmp/fb.o -o /tmp/fb
/tmp/fb        # prints: 49 100 171 2 123 1
```

### One-shot (`--emit exe` / `--emit obj`)

The compiler can drive `llc` (+ `cc`) itself, collapsing the three steps into one:

```sh
TVC=src/bootstrap/out/stage1
$TVC examples/field_basics.tv -o /tmp/fb --emit exe -llc "$LLC"
/tmp/fb        # prints: 49 100 171 2 123 1
```

`--emit obj` stops at the object file; `--emit ir` (the default) writes LLVM IR.
The driver passes `-O2` explicitly to `llc`; it reads the target triple from the
module preamble. Toolchain paths default to `PATH`; pass `-llc <path>` / `-cc
<path>` when tools are not on it (for example, Homebrew LLVM). Tools are launched
with argument vectors, not through a shell, so paths containing spaces are safe.
IR, object, and executable outputs use exclusive sibling stages and are
atomically published only after every requested tool succeeds. Failures preserve
an existing destination and remove intermediates.

### CPU middle-end profiles

Traveler exposes three closed profiles through the same IR/object/executable
flow:

| Profile | LLVM middle-end pipeline | Requires `opt` |
|---|---|---|
| `none` | none; raw compiler IR | no |
| `promote` | `-passes=mem2reg -verify-each` | LLVM 21 |
| `o1` | `-passes=default<O1> -verify-each` | LLVM 21 |

The default is `none`. Omitting `--opt-level` and selecting `none` produce
byte-identical raw IR. `promote` and `o1` are explicit, reproducible LLVM-21
toolchain transformations; their output is verified LLVM but is not a bootstrap
fixed-point artifact. Native host retargeting is applied consistently to both
`opt` and `llc`.

```sh
OPT=/opt/homebrew/opt/llvm@21/bin/opt

# Observe optimized LLVM.
$TVC examples/field_basics.tv -o /tmp/fb.o1.ll \
    --opt-level o1 -opt "$OPT"

# Or publish a native executable in one call.
$TVC examples/field_basics.tv -o /tmp/fb --emit exe \
    --opt-level o1 -opt "$OPT" -llc "$LLC"
```

`-opt <path>` overrides `PATH`, matching `-llc` and `-cc`. Arbitrary LLVM pass
strings are deliberately not accepted. Standalone AMDGCN, NVPTX, and AGX device
emission and the `-target tpc` typed-pointer compatibility mode accept only
profile `none`; `--agx-dispatch` emits a normal host module and may use either
CPU profile for its unchanged fallback path.

### Shared library (`#[export]` functions, callable from Python/ctypes)

```sh
$TVC src/lib/core/poly_core.tv -o /tmp/pc.ll
$LLC -O2 -filetype=obj /tmp/pc.ll -o /tmp/pc.o
cc -shared -O2 -o /tmp/libpoly_core.dylib /tmp/pc.o   # .so on Linux
```

### Multi-file programs

Some demos link against a library kernel (e.g. `examples/poly_core_test.tv` +
`src/lib/core/poly_core.tv`). Compile each `.tv` to a `.o`, then link them
together (using the canonical compiler from §2):

```sh
TVC=src/bootstrap/out/stage1
$TVC src/lib/core/poly_core.tv      -o /tmp/core.ll
$TVC examples/poly_core_test.tv     -o /tmp/test.ll
$LLC -filetype=obj /tmp/core.ll -o /tmp/core.o
$LLC -filetype=obj /tmp/test.ll -o /tmp/test.o
cc /tmp/core.o /tmp/test.o -o /tmp/poly_test
/tmp/poly_test
```

### Cross-compilation

The compiler accepts `-target <triple>`. The default is the **detected host
triple** (`uname`), so a plain `stage1 prog.tv -o prog.ll` produces IR the local
`llc` accepts with no `-mtriple`. To cross-compile, pass `-target` to the
compiler **and** the matching `-mtriple` to `llc`:

```sh
src/bootstrap/out/stage1 examples/field_basics.tv -o /tmp/pc.ll -target x86_64-linux-gnu
$LLC -mtriple=x86_64-linux-gnu -filetype=obj /tmp/pc.ll -o /tmp/pc.o
```

The C-free build itself cross-targets too: `src/bootstrap/build.sh --target
x86_64-linux-gnu`.

The compiler's **own** self-compile is pinned to the canonical
`arm64-apple-darwin` triple by `build.sh`/`refresh.sh` (an explicit `-target`),
so the committed snapshot and the fixed-point freshness diff stay
host-independent. On a Linux **host**, `build.sh` and the test gates link with
`-no-pie`; direct `stage1` invocations emit host IR, so add `cc -no-pie` when
linking on Linux.

### GPU device kernels

The compiler can re-emit proven elementwise pfor workers as standalone device
artifacts:

```sh
TVC=src/bootstrap/out/stage1
$TVC --emit-gpu examples/gpu_field_map.tv -o /tmp/map-amd.ll
$TVC --emit-gpu-nvptx examples/gpu_field_map.tv -o /tmp/map-nv.ll
$TVC --emit-gpu-agx examples/gpu_field_map.tv -o /tmp/map-agx.hex
$TVC --emit-gpu-vulkan examples/gpu_field_map.tv -o /tmp/map-vulkan.comp
```

Vulkan Stage 0 reuses the proved worker records but emits canonical GLSL for
exactly one worker per artifact. Its closed profiles are the unary
`Field<2147483647>` map and a signed, overflow-free private-K=8 integer dot.
`glslangValidator -V` is the standard SPIR-V encoder; it does not own source
semantics or admission. `src/lib/gpu/vulkan_runtime.tv` owns instance/device
selection, coherent device-local input buffers, host-cached coherent output
publication, descriptors, pipeline construction, submission, synchronization,
and teardown through the public Vulkan C ABI. `src/lib/gpu/hip_runtime.tv`
owns the corresponding HIP module path. Their executables link only Traveler
objects plus `libvulkan`/`libamdhip64`, with no project C, C++, HIP, or
shader-runtime shim.

AMDGCN and NVPTX are LLVM device modules; `tests/gpu/run.sh` lowers them with
`llc` to a gfx1100 object and sm_90 PTX. AGX is different: Traveler directly
emits measured G16X instruction bytes in canonical hex, with no Metal compiler
or LLVM device backend. The unary path admits one-input/one-output field maps
over `Field<2147483647>`, odd primes in `2^30 < p < 2^31`, or the canonical
64-bit prime `Field<18446744073709551557>` (`2^64-59`) through two u32 limbs;
other workers emit a skip record. This is not generic 64-bit-prime support. The
profile targets an M4 Pro G16X private interface and is not an Apple-supported
ABI. Narrow workers may also have two read-only inputs plus one output; the
runtime packs both logical inputs into one physical input binding, preserving
the measured two-binding graph. Portable byte goldens run everywhere;
owned-device execution runs only when the external harness is available.

AMDGCN/NVPTX device emission also admits the proof system's closed private K=8
dot shape: one mutable scalar accumulator, one literal `0..8` inner loop, and
one own-cell output. The device lowerer fully unrolls that loop into SSA, so the
module remains alloca-free and registers-only. General private mutables,
dynamic inner loops, and multi-statement reductions remain outside Stage 0.

The measured G16X profile also has an in-tree Traveler submission runtime. It
needs a regenerated `AGXDISP3` profile image for the exact OS/GPU build; that
machine-specific image is deliberately not shipped as a portable ABI. On the
matching M4 profile:

```sh
AGX_PROFILE=/path/to/dispatch.img
$TVC tests/gpu/agx_runtime_gate.tv -o /tmp/agx-runtime.ll
$LLC -filetype=obj /tmp/agx-runtime.ll -o /tmp/agx-runtime.o
cc /tmp/agx-runtime.o -framework IOKit -o /tmp/agx-runtime
/tmp/agx-runtime "$AGX_PROFILE" /tmp/map-agx.hex
```

`cc` only links the Traveler-produced object. `otool -L` must show IOKit and
libSystem, with no Metal, Foundation, IOGPU, Objective-C, or project C object.
The runtime refuses any service build, initialization fingerprint, profile call
shape, or GPU-address allocation order outside the measured profile. Set
`AGX_FAULT_RECOVERY=1` on `tests/gpu/run.sh` to exercise destroy/recreate recovery
after the controlled out-of-range kernel. On the measured machine the same gate
also compiles one pfor source for CPU and AGX, runs 256 reproducible
adversarial/random elements over each supported field profile, and requires
equal exit status and byte-exact raw output.

For a source that imports `src/lib/gpu/agx_runtime.tv`, `--agx-dispatch` emits a
normal host program that tries the matching AGX worker by ID and otherwise runs
the unchanged CPU pfor. Runtime selection uses the exact-build profile and the
multi-worker artifact generated from the same source. The host embeds the
FNV-1a digest produced by the shared AGX lowering path; the runtime verifies
worker ID, field, grid, and code digest before submission. This is a deterministic
wrong-build guard, not cryptographic artifact authentication:

```sh
$TVC --emit-gpu-agx tests/gpu/agx_rns_dot_general.tv -o /tmp/rns-agx.hex
$TVC --agx-dispatch tests/gpu/agx_rns_dot_general.tv -o /tmp/rns-host.ll
$LLC -filetype=obj /tmp/rns-host.ll -o /tmp/rns-host.o
cc /tmp/rns-host.o -framework IOKit -o /tmp/rns-host
TRAVELER_AGX_PROFILE="$AGX_PROFILE" \
TRAVELER_AGX_ARTIFACT=/tmp/rns-agx.hex /tmp/rns-host
```

With either variable absent, an unsupported worker, alias uncertainty, an
artifact mismatch, or a launch refusal, execution falls back to the CPU worker.
The compiler and runtime share a 65,535-element maximum AGX grid. Checked alias
intervals reject i32 index wrap and any pointer provenance erased through an
integer, an uncertain control-flow assignment, or an exposed pointer-binding
address.
The gate supplies a different valid kernel with the same worker ID, field, and
grid and requires hash-mismatch fallback. The counted-dot gate derives rows and
columns from canonical source index equations, executes a `1x8 * 8x1024`
reduction for each of three primes on AGX, and uses the shipped Garner CRT on
CPU. `K=8` remains the measured device-loop contract. No performance claim is
made.

### Software graphics (`src/lib/gfx/`)

A CPU framebuffer plus two backends. An application imports one backend.
Both export `gfx_open` / `gfx_frame` / `gfx_present` / `gfx_poll_event` /
`gfx_close`.

```sh
TVC=src/bootstrap/out/stage1
# Headless: draw and write a P6 PPM. No compositor.
$TVC examples/gfx_headless.tv -o /tmp/gh.ll
$LLC -filetype=obj /tmp/gh.ll -o /tmp/gh.o
cc -no-pie /tmp/gh.o -o /tmp/gh && /tmp/gh

# Wayland window (Linux, raw protocol, no libwayland).
$TVC examples/gfx_window.tv -o /tmp/gw.ll
$LLC -filetype=obj /tmp/gw.ll -o /tmp/gw.o
cc -no-pie /tmp/gw.o -o /tmp/gw && /tmp/gw
```

`net/unix.tv` and `mem/shm.tv` are the OS floor. Do not import `net/tcp.tv`
and `net/unix.tv` in the same unit.

## 5. Verify self-hosting (Stage 2 / Stage 3)

The compiler can reproduce itself: the compiler compiling itself (Stage 2) must
produce IR byte-identical to that compiler compiling itself again (Stage 3) —
the fixed point. `src/bootstrap/build.sh` already asserts this on every build.
To check it by hand with the canonical compiler:

```sh
TVC=src/bootstrap/out/stage1

# Stage 2: the compiler compiles itself
$TVC src/tvc_self.tv -o /tmp/s2.ll
$LLC -filetype=obj /tmp/s2.ll -o /tmp/s2.o
cc /tmp/s2.o -o /tmp/tvc_self2

# Stage 3: the Stage 2 binary compiles itself again
/tmp/tvc_self2 src/tvc_self.tv -o /tmp/s3.ll

# Fixed point: the two IRs must be byte-identical
diff /tmp/s2.ll /tmp/s3.ll && echo "FIXED POINT OK"
```

## 6. Run the test suites

From the repo root, the dispatcher probes the environment and runs what it
supports:

```sh
tests/run_all.sh               # probe + run; prints a capability matrix first
tests/run_all.sh --list        # print the probe results and suite plan only
tests/run_all.sh --suite=gpu   # run one suite (see --help for names)
```

The dispatcher's behavior by environment:

- **llc + link driver(s) present:** the full gate on the primary driver
  (`tests/run_dual.sh` when the C seed is available, else `tests/run.sh`),
  then the full regression suite once per *additional* discovered link driver
  (`cc`/`clang`/`gcc`), with tool-neutral sub-gates skipped on repeat passes.
- **No llc or no link driver:** `tests/run.sh` runs degraded — compilation
  and IR validation still happen, link/run stages report `SKIP (no llc)` /
  `SKIP (no linker)` per test — plus the canonical AGX byte goldens via
  `tests/gpu/run.sh --goldens-only`.
- **No toolchain at all:** the coreutils-only `tests/run_sizegate.sh`.

Individual suites remain directly runnable:

```sh
tests/run_dual.sh      # the full gate: regression + pfor + dynfield + bootstrap,
                       # then Stage 1 build + dual-compiler parity (one script)
tests/run.sh           # regression suite alone (incl. lsp/doc/bootstrap gates)
tests/run_pfor.sh      # auto-parallelization soundness suite
tests/dynfield/run.sh  # dynamic-field + traits + closures suite
```

The test scripts auto-detect `llc`/`opt`/link drivers across common locations
(`tests/lib/env.sh` is the shared probe); override with the `LLC`, `OPT`, and
`LINKER` environment variables if detection fails:

```sh
LLC=/usr/lib/llvm-21/bin/llc tests/run.sh
LLC=/usr/lib/llvm-21/bin/llc OPT=/usr/lib/llvm-21/bin/opt tests/run.sh
LINKER=gcc tests/run.sh        # link with a specific driver
```

`tests/run.sh` never fails hard on a missing tool: an absent `llc`, link
driver, C seed, or python3 turns the affected tests into named SKIPs (the C
seed's output tests fall back to stage1; its diagnostics negative tests skip).
The Wayland window test (`gfx_window`) runs only when a live compositor
socket exists, and pops a real window for ~5 seconds when it does.

### What each suite needs

Not every suite needs the full toolchain. Pick by environment:

- **stage1** — the self-hosted compiler at `src/bootstrap/out/stage1`
  (build it once with `src/bootstrap/build.sh`, which itself needs `llc`
  and a link driver).
- **llc** — LLVM 21 `llc` for object lowering.
- **link driver** — a `cc`/`clang` used only to link objects.
- **C compiler** — builds the frozen seed `src-legacy/tvc` (unneeded where a
  prebuilt `tvc` is already checked out/built).

| Suite | stage1 | llc | Link driver | C compiler | Extra |
|---|---|---|---|---|---|
| `tests/run.sh` (regression) | ✓ | ✓ | ✓ | ✓ | `opt` recommended (IR verify) |
| `tests/run_dual.sh` (full gate) | ✓ | ✓ | ✓ | ✓ | regression + pfor + dynfield + bootstrap + parity |
| `tests/run_pfor.sh` | ✓ | ✓ | ✓ | — | |
| `tests/dynfield/run.sh` | ✓ | ✓ | ✓ | ✓ | |
| `tests/emit/run.sh` (`--emit` driver) | ✓ | ✓ | ✓ | — | |
| `tests/eval_diff/run.sh` (evaluator oracle) | ✓ | ✓ | ✓ | — | |
| `tests/alloc_debug/run.sh` | ✓ | ✓ | ✓ | — | |
| `tests/foldbug/run.sh` | ✓ | ✓ | ✓ | — | |
| `tests/run_diag.sh` / `run_fmt.sh` / `run_lsp.sh` / `run_doc.sh` | ✓ | ✓ | ✓ | ✓ | also run as `run.sh` sub-gates |
| `tests/run_bootstrap.sh` (fixed point) | ✓ | ✓ | ✓ | — | rebuilds stage1/stage2 |
| `tests/typedptr/run.sh` (`-target tpc`) | ✓ | ✓ | ✓ | — | also needs an LLVM-14-era `llvm-as` + `llc` pair |
| `tests/gpu/run.sh` | ✓ | per leg | AGX leg | — | AMDGCN/NVPTX legs skip if `llc` lacks the target; AGX hardware legs need the measured M4 profile (macOS) |
| `tests/fuzz_diff.py` | ✓ | — | — | ✓ | dual-compiler IR fuzzing |
| `tests/codegen_diff/run.sh` | ✓ | — | — | — | IR-hash manifest |
| `tests/repl/run.sh` | ✓ | — | — | — | evaluator only |
| `tests/pow_assoc/run.sh` / `tests/pfor_report/run.sh` | ✓ | — | — | — | compiler-query gates |
| `tests/run_sizegate.sh` | — | — | — | — | coreutils only |

### Per-environment guide

- **macOS (M4, measured AGX profile):** everything runs, including the
  owned-device AGX legs of `tests/gpu/run.sh`.
- **macOS (other) / Linux with LLVM 21 + cc:** everything except the
  owned-device AGX legs (the canonical byte goldens still run). Add the
  LLVM-14 pair for `tests/typedptr/run.sh`.
- **Linux + Wayland:** as above. The `gfx_headless` / `gfx_pixel_test` /
  `gfx_wire_test` regression entries need no compositor. The window demo
  `examples/gfx_window.tv` is run manually against a live compositor (it
  prints `404` and exits if `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY` is absent;
  with a compositor it prints `1` on open and runs until the window is
  closed):

  ```sh
  src/bootstrap/out/stage1 examples/gfx_window.tv -o /tmp/gw.ll -target x86_64-linux-gnu
  $LLC -mtriple=x86_64-linux-gnu -filetype=obj /tmp/gw.ll -o /tmp/gw.o
  cc -no-pie /tmp/gw.o -o /tmp/gw && /tmp/gw
  ```

- **No LLVM toolchain (llc/opt absent):** the stage1-only gates still run —
  `tests/codegen_diff/run.sh`, `tests/repl/run.sh`, `tests/pow_assoc/run.sh`,
  `tests/pfor_report/run.sh` — plus `tests/run_sizegate.sh` (coreutils only)
  and the canonical AGX byte goldens (emit with `--emit-gpu-agx` and diff
  against `tests/gpu/golden/`).
- **No C compiler:** the seed-dependent gates cannot build `src-legacy/tvc`
  (`run_dual.sh` parity, `tests/fuzz_diff.py`); everything stage1-driven is
  unaffected.

## Troubleshooting

- **`llc not found`** — `tests/run.sh` no longer fails hard; it degrades to
  IR-only checks and SKIPs the link/run stages. `tests/gpu/run.sh` still
  requires `llc` for its device legs — use `tests/gpu/run.sh --goldens-only`
  for the llc-free AGX byte goldens, or set `LLC` to the full path of your
  `llc` binary.
- **`make test` fails on the `llc` step** — the hardcoded path in
  `src-legacy/Makefile` doesn't match your install. Pass `LLC=<path>` to `make`.
- **Linker errors about undefined field/helper symbols** — you're building a
  multi-file program; compile and link every required `.tv` (see §4).
- **Invalid IR / verifier errors after `opt`** — confirm `opt`, `llc`, and
  the IR all come from the same LLVM 21 toolchain (don't mix versions).
- **`error: opt failed`** — a requested `promote`/`o1` profile could not run
  LLVM 21 `opt`, or verification failed. Set `-opt <path>` to the matching tool.
  `opt` remains unnecessary when the profile is `none`.
