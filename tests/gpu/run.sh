#!/bin/bash
# GPU device-codegen and measured-profile runtime gate.
# @internal-note: plan-gpu-purity-runtime.
#
# Proves that a proven-parallel pfor worker re-emits on three device targets:
#   AMDGCN (--emit-gpu)        `llc -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100`
#                              lowers to a real gfx1100 code object that
#                              disassembles to sane GCN (s_endpgm + memory op).
#   NVPTX  (--emit-gpu-nvptx)  `llc -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90`
#                              lowers to PTX with a .visible .entry, the
#                              .maxntid launch bound, %tid.x/%ctaid.x SIMT reads
#                              and .global memory ops.
#   AGX     (--emit-gpu-agx)    directly emits G16X instructions. Canonical
#                              byte-goldens run everywhere; owned-queue execution
#                              and the Traveler-native IOKit runtime run on the
#                              measured M4 profile.
#
# Each leg skips cleanly if the local llc lacks that target; the gate fails only
# on a real lowering failure.
#
# Usage: ./run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

# --- Locate llc (same discovery as run.sh; honors $LLC) ---
find_llc() {
    if [ -n "${LLC:-}" ] && command -v "$LLC" &>/dev/null; then return; fi
    for p in \
        /opt/homebrew/opt/llvm@21/bin/llc \
        /usr/local/opt/llvm@21/bin/llc \
        /usr/lib/llvm-21/bin/llc \
        llc-21 \
        llc; do
        if command -v "$p" &>/dev/null; then LLC="$p"; return; fi
    done
    echo "FATAL: llc not found. Set LLC env var." >&2; exit 1
}
find_llc
# The objdump that ships beside llc (for the disasm sanity check).
OBJDUMP="$(dirname "$LLC")/llvm-objdump"

echo "=== GPU target and AGX runtime gate (tests/gpu) ==="

# Which device targets are built into this llc? Each leg needs its own; a
# missing target is a SKIP (not a failure), a lowering error is a FAIL.
TARGETS="$("$LLC" --version 2>/dev/null)"
HAVE_AMD=0; HAVE_NV=0
echo "$TARGETS" | grep -qiE '^\s*amdgcn'  && HAVE_AMD=1
echo "$TARGETS" | grep -qiE '^\s*nvptx64' && HAVE_NV=1

STAGE1="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$STAGE1" ]; then
    echo "  FATAL: tvc_self not built at $STAGE1 (run src/bootstrap/build.sh)"; exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
EXHIBIT="$REPO_DIR/examples/gpu_field_map.tv"
MONT_EXHIBIT="$REPO_DIR/examples/gpu_field_map_mont.tv"
WIDE_EXHIBIT="$REPO_DIR/examples/gpu_field_map_64.tv"
DEV="$TMP/gpu_dev.ll"
OBJ="$TMP/gpu_dev.o"
fail=0

# ============================== AGX leg (--emit-gpu-agx) ====================
# The compiler is the assembler: portable goldens pin every emitted byte. On the
# owned M4, the same bytes are injected into the Metal-free queue and executed.
echo "  -- AGX G16X (--emit-gpu-agx)"
AGX_DEV="$TMP/gpu_field_map.agx.hex"
AGX_MONT_DEV="$TMP/gpu_field_map_mont.agx.hex"
AGX_WIDE_DEV="$TMP/gpu_field_map_64.agx.hex"
AGX_WIDE_ID="$TMP/agx_field64_identity.hex"
AGX_WIDE_OPS="$TMP/agx_field64_ops.hex"
AGX_GOLD="$SCRIPT_DIR/golden/gpu_field_map.agx.hex"
AGX_MONT_GOLD="$SCRIPT_DIR/golden/gpu_field_map_mont.agx.hex"
AGX_WIDE_GOLD="$SCRIPT_DIR/golden/gpu_field_map_64.agx.hex"
AGX_REFUSE="$TMP/agx_refuse.hex"
AGX_RUNTIME_LL="$TMP/agx_runtime_gate.ll"
AGX_RUNTIME_OBJ="$TMP/agx_runtime_gate.o"
AGX_RUNTIME_EXE="$TMP/agx-runtime-gate"
AGX_BINARY_DEV="$TMP/agx_binary_map.agx.hex"
AGX_BINARY_LL="$TMP/agx_binary_runtime_gate.ll"
AGX_BINARY_OBJ="$TMP/agx_binary_runtime_gate.o"
AGX_BINARY_EXE="$TMP/agx-binary-runtime-gate"
AGX_DISPATCH_DEV="$TMP/agx_dispatch_gate.agx.hex"
AGX_DISPATCH_WRONG_DEV="$TMP/agx_dispatch_wrong.agx.hex"
AGX_DISPATCH_LL="$TMP/agx_dispatch_gate.ll"
AGX_DISPATCH_OBJ="$TMP/agx_dispatch_gate.o"
AGX_DISPATCH_EXE="$TMP/agx-dispatch-gate"
AGX_RNS_DEV="$TMP/agx_rns_matmul.agx.hex"
AGX_RNS_LL="$TMP/agx_rns_matmul.ll"
AGX_RNS_OBJ="$TMP/agx_rns_matmul.o"
AGX_RNS_EXE="$TMP/agx-rns-matmul"
AGX_REDUCE_DEV="$TMP/agx_rns_reduce8.agx.hex"
AGX_REDUCE_LL="$TMP/agx_rns_reduce8.ll"
AGX_REDUCE_OBJ="$TMP/agx_rns_reduce8.o"
AGX_REDUCE_EXE="$TMP/agx-rns-reduce8"
AGX_DOT_DEV="$TMP/agx_rns_dot.agx.hex"
AGX_DOT_LL="$TMP/agx_rns_dot.ll"
AGX_DOT_OBJ="$TMP/agx_rns_dot.o"
AGX_DOT_EXE="$TMP/agx-rns-dot"
AGX_DOT_LOOP_DEV="$TMP/agx_rns_dot_loop.agx.hex"
AGX_DOT_LOOP_LL="$TMP/agx_rns_dot_loop.ll"
AGX_DOT_LOOP_OBJ="$TMP/agx_rns_dot_loop.o"
AGX_DOT_LOOP_EXE="$TMP/agx-rns-dot-loop"
AGX_DOT_REFUSE_DEV="$TMP/agx_rns_dot_refuse.agx.hex"
AGX_DOT_REFUSE_LL="$TMP/agx_rns_dot_refuse.ll"
AGX_DOT_REFUSE_REPORT="$TMP/agx_rns_dot_refuse.report"

if ! "$STAGE1" --emit-gpu-agx "$EXHIBIT" -o "$AGX_DEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce the Mersenne artifact"; fail=1
elif ! "$STAGE1" --emit-gpu-agx "$MONT_EXHIBIT" -o "$AGX_MONT_DEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce the Montgomery artifact"; fail=1
elif ! "$STAGE1" --emit-gpu-agx "$WIDE_EXHIBIT" -o "$AGX_WIDE_DEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce the 64-bit artifact"; fail=1
elif ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_field64_identity.tv" -o "$AGX_WIDE_ID" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce the 64-bit identity artifact"; fail=1
elif ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_field64_ops.tv" -o "$AGX_WIDE_OPS" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce the 64-bit ops artifact"; fail=1
elif ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_refuse.tv" -o "$AGX_REFUSE" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-agx did not produce refusal records"; fail=1
else
    agxfail=0
    cmp -s "$AGX_DEV" "$AGX_GOLD" || { echo "  FAIL: Mersenne AGX bytes differ from golden"; agxfail=1; }
    cmp -s "$AGX_MONT_DEV" "$AGX_MONT_GOLD" || { echo "  FAIL: Montgomery AGX bytes differ from golden"; agxfail=1; }
    cmp -s "$AGX_WIDE_DEV" "$AGX_WIDE_GOLD" || { echo "  FAIL: 64-bit AGX bytes differ from golden"; agxfail=1; }
    grep -q '^profile agx-g16x-macos26.3$' "$AGX_DEV" || { echo "  FAIL: AGX profile absent"; agxfail=1; }
    grep -q '^bytes 368$' "$AGX_DEV" || { echo "  FAIL: wrong Mersenne byte count"; agxfail=1; }
    grep -q '^bytes 806$' "$AGX_MONT_DEV" || { echo "  FAIL: wrong Montgomery byte count"; agxfail=1; }
    grep -q '^bytes 3348$' "$AGX_WIDE_DEV" || { echo "  FAIL: wrong 64-bit byte count"; agxfail=1; }
    ntaps=$(grep -c '^tap ' "$AGX_WIDE_DEV" || true)
    if [ "$ntaps" -ne 16 ]; then echo "  FAIL: expected 16 AGX64 taps, saw $ntaps"; agxfail=1; fi
    grep -q '^  0e000000$' "$AGX_DEV" || { echo "  FAIL: Mersenne kernel has no stop"; agxfail=1; }
    grep -q '^  0e000000$' "$AGX_MONT_DEV" || { echo "  FAIL: Montgomery kernel has no stop"; agxfail=1; }
    nskip=$(grep -c '^skip __pfor_gpu_worker_.*reason=unsupported-agx0-worker$' "$AGX_REFUSE" || true)
    if [ "$nskip" -ne 3 ]; then echo "  FAIL: expected 3 AGX refusal records, saw $nskip"; agxfail=1; fi
    if [ "$agxfail" = "0" ]; then
        echo "  ok   direct machine code: 368-byte Mersenne + 806-byte Montgomery + 3348-byte 64-bit goldens"
    else
        fail=1
    fi

    # Compile the in-tree runtime on every host. Linking is Darwin-only because
    # its only non-libSystem dependency is the public IOKit framework.
    if ! "$STAGE1" "$SCRIPT_DIR/agx_runtime_gate.tv" -o "$AGX_RUNTIME_LL" 2>/dev/null \
       || ! "$LLC" -filetype=obj "$AGX_RUNTIME_LL" -o "$AGX_RUNTIME_OBJ" 2>/dev/null; then
        echo "  FAIL: Traveler-native AGX runtime did not compile"; fail=1
    else
        echo "  ok   Traveler-native IOKit runtime compiles without project C/Objective-C"
    fi

    # One source and one proven pfor provide both sides of each AGX-2
    # differential. The emitted files are portable; execution is hardware-gated.
    AGX_DET_READY=1
    for det in agx_determinism_mersenne agx_determinism_mont agx_determinism_64; do
        if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/$det.tv" -o "$TMP/$det.agx.hex" 2>/dev/null \
           || ! "$STAGE1" "$SCRIPT_DIR/$det.tv" -o "$TMP/$det.ll" 2>/dev/null \
           || ! grep -q 'call void @__parallel_for' "$TMP/$det.ll" \
           || ! "$LLC" -filetype=obj "$TMP/$det.ll" -o "$TMP/$det.o" 2>/dev/null; then
            echo "  FAIL: $det did not compile for both CPU pfor and AGX"; fail=1
            AGX_DET_READY=0
        fi
    done
    if [ "$AGX_DET_READY" = "1" ]; then
        echo "  ok   AGX determinism fixtures compile from one pfor for both targets"
    fi

    # AGX-3: two logical inputs remain on the measured two-binding graph by
    # packing them into binding 1. The opt-in host mode keeps CPU pfor fallback.
    AGX3_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_binary_map.tv" \
         -o "$AGX_BINARY_DEV" 2>/dev/null \
       || ! grep -q '^capture right slot 1 word-offset 768$' "$AGX_BINARY_DEV" \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_binary_runtime_gate.tv" \
         -o "$AGX_BINARY_LL" 2>/dev/null \
       || ! "$LLC" -filetype=obj "$AGX_BINARY_LL" -o "$AGX_BINARY_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX packed two-input runtime gate did not compile"; fail=1
        AGX3_READY=0
    fi
    if "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_binary_map.tv" \
         -o "$TMP/agx_dispatch_missing_runtime.ll" \
         >"$TMP/agx_dispatch_missing_runtime.out" \
         2>"$TMP/agx_dispatch_missing_runtime.err"; then
        echo "  FAIL: --agx-dispatch accepted a source without the runtime import"; fail=1
        AGX3_READY=0
    elif ! grep -q 'requires the AGX runtime import' \
         "$TMP/agx_dispatch_missing_runtime.err"; then
        echo "  FAIL: --agx-dispatch missing-runtime refusal changed"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_dispatch_gate.tv" \
          -o "$AGX_DISPATCH_DEV" 2>/dev/null \
       || ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_dispatch_wrong.tv" \
          -o "$AGX_DISPATCH_WRONG_DEV" 2>/dev/null \
       || ! grep -q '^field 2013265921$' "$AGX_DISPATCH_WRONG_DEV" \
       || ! grep -q '^grid 768$' "$AGX_DISPATCH_WRONG_DEV" \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_dispatch_gate.tv" \
         -o "$AGX_DISPATCH_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_DISPATCH_LL" || true)" -ne 1 ] \
       || ! grep -q 'call void @__parallel_for(ptr @__pfor_worker_0' "$AGX_DISPATCH_LL" \
       || ! python3 "$SCRIPT_DIR/agx_dispatch_pin.py" "$AGX_DISPATCH_LL" \
          "$AGX_DISPATCH_DEV" "$AGX_DISPATCH_WRONG_DEV" \
       || ! "$LLC" -filetype=obj "$AGX_DISPATCH_LL" -o "$AGX_DISPATCH_OBJ" 2>/dev/null; then
        echo "  FAIL: opt-in AGX/CPU runtime dispatch seam did not compile"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_matmul.tv" \
         -o "$AGX_RNS_DEV" 2>/dev/null \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_RNS_DEV" || true)" -ne 3 ] \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_matmul.tv" \
         -o "$AGX_RNS_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_RNS_LL" || true)" -ne 3 ] \
       || ! "$LLC" -filetype=obj "$AGX_RNS_LL" -o "$AGX_RNS_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX three-prime RNS matmul consumer did not compile"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_reduce8.tv" \
          -o "$AGX_REDUCE_DEV" 2>/dev/null \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_REDUCE_DEV" || true)" -ne 6 ] \
       || [ "$(grep -c '^output-words 128$' "$AGX_REDUCE_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input0-words 1024$' "$AGX_REDUCE_DEV" || true)" -ne 6 ] \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_reduce8.tv" \
          -o "$AGX_REDUCE_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_REDUCE_LL" || true)" -ne 6 ] \
       || ! "$LLC" -filetype=obj "$AGX_REDUCE_LL" -o "$AGX_REDUCE_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX product-tensor reduce-8 bridge did not compile"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_dot.tv" \
          -o "$AGX_DOT_DEV" 2>/dev/null \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_DOT_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^output-words 128$' "$AGX_DOT_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input0-words 32$' "$AGX_DOT_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input1-words 256$' "$AGX_DOT_DEV" || true)" -ne 3 ] \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_dot.tv" \
          -o "$AGX_DOT_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_DOT_LL" || true)" -ne 3 ] \
       || ! "$LLC" -filetype=obj "$AGX_DOT_LL" -o "$AGX_DOT_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX direct exact RNS dot did not compile"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_dot_loop.tv" \
           -o "$AGX_DOT_LOOP_DEV" 2>/dev/null \
       || ! cmp -s "$AGX_DOT_LOOP_DEV" "$AGX_DOT_DEV" \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_DOT_LOOP_DEV" || true)" -ne 3 ] \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_dot_loop.tv" \
           -o "$AGX_DOT_LOOP_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_DOT_LOOP_LL" || true)" -ne 3 ] \
       || ! "$LLC" -filetype=obj "$AGX_DOT_LOOP_LL" -o "$AGX_DOT_LOOP_OBJ" 2>/dev/null; then
        echo "  FAIL: canonical nested K=8 RNS dot did not match the unrolled artifact"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
           -o "$AGX_DOT_REFUSE_DEV" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$AGX_DOT_REFUSE_DEV" \
       || ! grep -q '^; no AGX-0 workers emitted$' "$AGX_DOT_REFUSE_DEV" \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
           -o "$AGX_DOT_REFUSE_LL" 2>/dev/null \
       || grep -q '^define internal void @__pfor_worker_' "$AGX_DOT_REFUSE_LL" \
       || ! "$STAGE1" --pfor-report "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
           >"$AGX_DOT_REFUSE_REPORT" 2>/dev/null \
       || [ "$(grep -c '"reason":"unsupported-stmt"' "$AGX_DOT_REFUSE_REPORT" || true)" -ne 2 ]; then
        echo "  FAIL: noncanonical nested RNS dots did not stay serial"; fail=1
        AGX3_READY=0
    fi
    if [ "$AGX3_READY" = "1" ]; then
        echo "  ok   AGX-3 packed binary, runtime dispatch, and RNS artifacts compile"
    fi
    if ! python3 "$SCRIPT_DIR/agx_control_probe.py" --check-only; then
        echo "  FAIL: AGX control specimens failed their portable structure gate"
        fail=1
    fi

    AGX_HARNESS="${AGX_HARNESS:-$REPO_DIR/../qwen35-cli-b1/agx_private}"
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] \
       && [ -x "$AGX_HARNESS/agx-own-queue" ] && command -v python3 >/dev/null; then
        if python3 "$SCRIPT_DIR/agx_execute.py" "$AGX_DEV" "$AGX_HARNESS" \
           && python3 "$SCRIPT_DIR/agx_execute.py" "$AGX_MONT_DEV" "$AGX_HARNESS" \
           && python3 "$SCRIPT_DIR/agx_execute.py" "$AGX_WIDE_DEV" "$AGX_HARNESS" \
           && python3 "$SCRIPT_DIR/agx64_taps.py" "$AGX_WIDE_DEV" "$AGX_HARNESS" \
           && python3 "$SCRIPT_DIR/agx_execute.py" "$AGX_WIDE_ID" "$AGX_HARNESS" --formula identity \
           && python3 "$SCRIPT_DIR/agx_execute.py" "$AGX_WIDE_OPS" "$AGX_HARNESS" --formula add-sub; then
            echo "  ok   owned G16X queue: narrow + 64-bit kernels and 16 limb taps exact"
        else
            echo "  FAIL: owned G16X execution mismatch"; fail=1
        fi
        if [ -f "$AGX_RUNTIME_OBJ" ] \
           && cc "$AGX_RUNTIME_OBJ" -framework IOKit -o "$AGX_RUNTIME_EXE" 2>/dev/null; then
            deps="$(otool -L "$AGX_RUNTIME_EXE" 2>/dev/null)"
            if echo "$deps" | grep -qE 'Metal|Foundation|IOGPU'; then
                echo "  FAIL: closed/private framework leaked into Traveler AGX runtime"; fail=1
            elif "$AGX_RUNTIME_EXE" "$AGX_HARNESS/dispatch.img" "$AGX_DEV" \
                 && "$AGX_RUNTIME_EXE" "$AGX_HARNESS/dispatch.img" "$AGX_MONT_DEV" \
                 && "$AGX_RUNTIME_EXE" "$AGX_HARNESS/dispatch.img" "$AGX_WIDE_DEV"; then
                echo "  ok   Traveler IOKit runtime: narrow + 64-bit compiler output exact; image has only IOKit + libSystem"
            else
                echo "  FAIL: Traveler-native IOKit submission mismatch"; fail=1
            fi
        else
            echo "  FAIL: could not link Traveler AGX runtime against IOKit"; fail=1
        fi
        if python3 "$SCRIPT_DIR/agx_control_probe.py" "$AGX_HARNESS"; then
            echo "  ok   G16X compare/select/forward-control/backedge contracts exact"
        else
            echo "  FAIL: G16X control-flow execution mismatch"; fail=1
        fi
        if [ "$AGX3_READY" = "1" ]; then
            agx3fail=0
            if ! cc "$AGX_BINARY_OBJ" -framework IOKit -o "$AGX_BINARY_EXE" 2>/dev/null \
               || [ "$("$AGX_BINARY_EXE" "$AGX_HARNESS/dispatch.img" \
                    "$AGX_BINARY_DEV")" != "1" ]; then
                echo "  FAIL: packed two-input AGX execution mismatch"
                agx3fail=1
            fi
            if ! cc "$AGX_DISPATCH_OBJ" -framework IOKit -o "$AGX_DISPATCH_EXE" 2>/dev/null; then
                echo "  FAIL: AGX runtime-dispatch gate did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_DISPATCH_EXE" >"$TMP/agx_dispatch.cpu.bin")
                dispatch_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_DISPATCH_DEV" \
                    "$AGX_DISPATCH_EXE" >"$TMP/agx_dispatch.gpu.bin"
                dispatch_gpu_status=$?
                if [ "$dispatch_cpu_status" -ne 0 ] || [ "$dispatch_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_dispatch.cpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_dispatch.cpu.bin" "$TMP/agx_dispatch.gpu.bin"; then
                    echo "  FAIL: runtime-selected AGX output differs from CPU pfor"
                    agx3fail=1
                fi
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_DISPATCH_WRONG_DEV" \
                TRAVELER_AGX_EXPECT_FALLBACK=1 \
                    "$AGX_DISPATCH_EXE" >"$TMP/agx_dispatch.wrong.bin"
                dispatch_wrong_status=$?
                if [ "$dispatch_wrong_status" -ne 0 ] \
                   || ! cmp -s "$TMP/agx_dispatch.cpu.bin" \
                       "$TMP/agx_dispatch.wrong.bin"; then
                    echo "  FAIL: same-shape wrong AGX artifact did not fall back to CPU"
                    agx3fail=1
                fi
            fi
            if ! cc "$AGX_RNS_OBJ" -framework IOKit -o "$AGX_RNS_EXE" 2>/dev/null; then
                echo "  FAIL: AGX RNS matmul consumer did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_RNS_EXE" >"$TMP/agx_rns.cpu.bin")
                rns_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_RNS_DEV" \
                    "$AGX_RNS_EXE" >"$TMP/agx_rns.gpu.bin"
                rns_gpu_status=$?
                if [ "$rns_cpu_status" -ne 0 ] || [ "$rns_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_rns.cpu.bin" | tr -d ' ')" -ne 1024 ] \
                   || ! cmp -s "$TMP/agx_rns.cpu.bin" "$TMP/agx_rns.gpu.bin"; then
                    echo "  FAIL: three-prime AGX RNS matmul differs from CPU/exact oracle"
                    agx3fail=1
                fi
            fi
            if ! cc "$AGX_REDUCE_OBJ" -framework IOKit -o "$AGX_REDUCE_EXE" 2>/dev/null; then
                echo "  FAIL: AGX reduce-8 bridge did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_REDUCE_EXE" >"$TMP/agx_reduce8.cpu.bin")
                reduce_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_REDUCE_DEV" \
                    "$AGX_REDUCE_EXE" >"$TMP/agx_reduce8.gpu.bin"
                reduce_gpu_status=$?
                if [ "$reduce_cpu_status" -ne 0 ] || [ "$reduce_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_reduce8.cpu.bin" | tr -d ' ')" -ne 1024 ] \
                   || ! cmp -s "$TMP/agx_reduce8.cpu.bin" "$TMP/agx_reduce8.gpu.bin"; then
                    echo "  FAIL: AGX product-tensor reduce-8 bridge differs from CPU"
                    agx3fail=1
                fi
            fi
            if ! cc "$AGX_DOT_OBJ" -framework IOKit -o "$AGX_DOT_EXE" 2>/dev/null; then
                echo "  FAIL: AGX direct RNS dot did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_DOT_EXE" >"$TMP/agx_dot.cpu.bin")
                dot_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_DOT_DEV" \
                    "$AGX_DOT_EXE" >"$TMP/agx_dot.gpu.bin"
                dot_gpu_status=$?
                if [ "$dot_cpu_status" -ne 0 ] || [ "$dot_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_dot.cpu.bin" | tr -d ' ')" -ne 1024 ] \
                   || ! cmp -s "$TMP/agx_dot.cpu.bin" "$TMP/agx_dot.gpu.bin" \
                   || ! cmp -s "$TMP/agx_reduce8.gpu.bin" "$TMP/agx_dot.gpu.bin"; then
                    echo "  FAIL: AGX direct RNS dot differs from bridge/CPU"
                    agx3fail=1
                fi
            fi
            if ! cc "$AGX_DOT_LOOP_OBJ" -framework IOKit -o "$AGX_DOT_LOOP_EXE" 2>/dev/null; then
                echo "  FAIL: canonical nested K=8 RNS dot did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_DOT_LOOP_EXE" >"$TMP/agx_dot_loop.cpu.bin")
                dot_loop_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_DOT_LOOP_DEV" \
                    "$AGX_DOT_LOOP_EXE" >"$TMP/agx_dot_loop.gpu.bin"
                dot_loop_gpu_status=$?
                if [ "$dot_loop_cpu_status" -ne 0 ] || [ "$dot_loop_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_dot_loop.cpu.bin" | tr -d ' ')" -ne 1024 ] \
                   || ! cmp -s "$TMP/agx_dot_loop.cpu.bin" "$TMP/agx_dot_loop.gpu.bin" \
                   || ! cmp -s "$TMP/agx_dot.gpu.bin" "$TMP/agx_dot_loop.gpu.bin"; then
                    echo "  FAIL: canonical nested K=8 RNS dot differs from unrolled/CPU"
                    agx3fail=1
                fi
            fi
            if [ "$agx3fail" = "0" ]; then
                echo "  ok   AGX RNS product, reduce-8, direct-dot, and nested K=8 paths are exact"
            else
                fail=1
            fi
        fi
        if [ "$AGX_DET_READY" = "1" ]; then
            agxdetfail=0
            for det in agx_determinism_mersenne agx_determinism_mont agx_determinism_64; do
                case "$det" in
                    agx_determinism_64) det_bytes=2048 ;;
                    *) det_bytes=1024 ;;
                esac
                if ! cc "$TMP/$det.o" -framework IOKit -o "$TMP/$det" 2>/dev/null; then
                    echo "  FAIL: $det did not link against IOKit"
                    agxdetfail=1
                    continue
                fi
                TRAVELER_THREADS=4 "$TMP/$det" cpu >"$TMP/$det.cpu.bin"
                cpu_status=$?
                "$TMP/$det" gpu "$AGX_HARNESS/dispatch.img" "$TMP/$det.agx.hex" \
                    >"$TMP/$det.gpu.bin"
                gpu_status=$?
                cpu_bytes=$(wc -c <"$TMP/$det.cpu.bin" | tr -d ' ')
                gpu_bytes=$(wc -c <"$TMP/$det.gpu.bin" | tr -d ' ')
                if [ "$cpu_status" -ne "$gpu_status" ] || [ "$cpu_status" -ne 0 ]; then
                    echo "  FAIL: $det CPU/GPU status differs ($cpu_status vs $gpu_status)"
                    agxdetfail=1
                elif [ "$cpu_bytes" -ne "$det_bytes" ] || [ "$gpu_bytes" -ne "$det_bytes" ]; then
                    echo "  FAIL: $det output size differs ($cpu_bytes vs $gpu_bytes; want $det_bytes)"
                    agxdetfail=1
                elif ! cmp -s "$TMP/$det.cpu.bin" "$TMP/$det.gpu.bin"; then
                    echo "  FAIL: $det CPU/GPU output is not byte-exact"
                    agxdetfail=1
                fi
            done
            if [ "$agxdetfail" = "0" ]; then
                echo "  ok   AGX randomized CPU/GPU output and status are byte-exact for all three profiles"
            else
                fail=1
            fi
        fi
        if [ "${AGX_FAULT_RECOVERY:-0}" = "1" ]; then
            if [ -x "$AGX_RUNTIME_EXE" ] \
               && "$AGX_RUNTIME_EXE" "$AGX_HARNESS/dispatch.img" "$AGX_DEV" recovery; then
                echo "  ok   Traveler queue recovery after controlled fault"
            else
                echo "  FAIL: Traveler queue recovery after controlled fault"; fail=1
            fi
        fi
    else
        echo "  SKIP: owned G16X execution harness unavailable"
    fi
fi

# ============================== AMDGCN leg (--emit-gpu) ======================
if [ "$HAVE_AMD" = "1" ]; then
echo "  -- AMDGCN (--emit-gpu)"

# A1. Emit the device module.
if ! "$STAGE1" --emit-gpu "$EXHIBIT" -o "$DEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu did not produce a module"; exit 1
fi

# A2. Structural asserts: an amdgpu_kernel with addrspace(1) global kernargs and
#     the SIMT workitem intrinsic (the loop→SIMT rewrite).
nk=$(grep -c "define amdgpu_kernel" "$DEV" || true)
if [ "$nk" -lt 1 ]; then echo "  FAIL: no amdgpu_kernel emitted"; fail=1; fi
grep -q "ptr addrspace(1)" "$DEV" || { echo "  FAIL: no addrspace(1) global kernargs"; fail=1; }
grep -q "llvm.amdgcn.workitem.id.x" "$DEV" || { echo "  FAIL: no SIMT workitem index"; fail=1; }
grep -q "target triple = \"amdgcn-amd-amdhsa\"" "$DEV" || { echo "  FAIL: wrong/absent device triple"; fail=1; }
grep -q "nvvm\|ptx_kernel" "$DEV" && { echo "  FAIL: NVPTX constructs leaked into the AMDGCN module"; fail=1; }
[ "$fail" = "0" ] && echo "  ok   device module: $nk amdgpu_kernel(s), addrspace(1) kernargs, SIMT index"

# A3. THE GATE: llc lowers it to a valid gfx1100 code object.
if "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj "$DEV" -o "$OBJ" 2>"$TMP/llc.err"; then
    sz=$(wc -c < "$OBJ" | tr -d ' ')
    if [ "$sz" -gt 0 ]; then
        echo "  ok   llc -mcpu=gfx1100 lowered to a valid code object ($sz bytes)"
    else
        echo "  FAIL: llc produced an empty object"; fail=1
    fi
else
    echo "  FAIL: llc could not lower the kernel to gfx1100:"; sed 's/^/       /' "$TMP/llc.err"; fail=1
fi

# A4. Disasm sanity: a real kernel epilogue + a global/flat memory op.
if [ "$fail" = "0" ] && [ -x "$OBJDUMP" ]; then
    dis="$("$OBJDUMP" -d "$OBJ" 2>/dev/null)"
    echo "$dis" | grep -q "s_endpgm" || { echo "  FAIL: no s_endpgm (not a real kernel)"; fail=1; }
    echo "$dis" | grep -qiE "flat_(load|store)|global_(load|store)" || { echo "  FAIL: no memory op in the kernel"; fail=1; }
    [ "$fail" = "0" ] && echo "  ok   GCN disasm: s_endpgm + flat memory ops (sane kernel)"
fi
else
    echo "  SKIP: no amdgcn target in this llc (AMDGCN leg)"
fi

# ============================ NVPTX leg (--emit-gpu-nvptx) ===================
# Same elementwise worker, NVIDIA target. NVPTX has no integrated assembler
# (object code is ptxas' job), so the gate is llc's PTX: a .visible .entry with
# the .maxntid launch bound, %tid.x/%ctaid.x SIMT reads and .global memory ops.
if [ "$HAVE_NV" = "1" ]; then
echo "  -- NVPTX (--emit-gpu-nvptx)"
NVDEV="$TMP/gpu_dev_nv.ll"
PTX="$TMP/gpu_dev_nv.ptx"

# N1. Emit the device module.
if ! "$STAGE1" --emit-gpu-nvptx "$EXHIBIT" -o "$NVDEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu-nvptx did not produce a module"; fail=1
else

# N2. Structural asserts: a ptx_kernel with addrspace(1) global kernargs, the
#     nvvm SIMT sreg reads, the NVIDIA triple and the maxntid launch bound.
nvfail=0
nnk=$(grep -c "define ptx_kernel" "$NVDEV" || true)
if [ "$nnk" -lt 1 ]; then echo "  FAIL: no ptx_kernel emitted"; nvfail=1; fi
grep -q "ptr addrspace(1)" "$NVDEV" || { echo "  FAIL: no addrspace(1) global kernargs"; nvfail=1; }
grep -q "llvm.nvvm.read.ptx.sreg.tid.x" "$NVDEV" || { echo "  FAIL: no SIMT thread index"; nvfail=1; }
grep -q "llvm.nvvm.read.ptx.sreg.ctaid.x" "$NVDEV" || { echo "  FAIL: no SIMT CTA index"; nvfail=1; }
grep -q "target triple = \"nvptx64-nvidia-cuda\"" "$NVDEV" || { echo "  FAIL: wrong/absent device triple"; nvfail=1; }
grep -q "\"nvvm.maxntid\"=\"256\"" "$NVDEV" || { echo "  FAIL: no maxntid launch bound"; nvfail=1; }
grep -q "amdgcn\|amdgpu" "$NVDEV" && { echo "  FAIL: AMDGCN constructs leaked into the NVPTX module"; nvfail=1; }
[ "$nvfail" = "0" ] && echo "  ok   device module: $nnk ptx_kernel(s), addrspace(1) kernargs, SIMT index"
[ "$nvfail" = "0" ] || fail=1

# N3. THE GATE: llc lowers it to valid sm_90 PTX.
if "$LLC" -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90 "$NVDEV" -o "$PTX" 2>"$TMP/llcnv.err"; then
    sz=$(wc -c < "$PTX" | tr -d ' ')
    if [ "$sz" -gt 0 ]; then
        echo "  ok   llc -mcpu=sm_90 lowered to PTX ($sz bytes)"
    else
        echo "  FAIL: llc produced empty PTX"; fail=1
    fi
else
    echo "  FAIL: llc could not lower the kernel to sm_90:"; sed 's/^/       /' "$TMP/llcnv.err"; fail=1
fi

# N4. PTX sanity: a real kernel entry with the launch bound, both SIMT reads and
#     global memory traffic (the .param .ptr .global proves the kernarg space).
if [ -s "$PTX" ]; then
    ptxfail=0
    grep -q "^\.visible \.entry __pfor_gpu_worker_" "$PTX" || { echo "  FAIL: no .visible .entry (not a real kernel)"; ptxfail=1; }
    grep -q "^\.maxntid 256" "$PTX" || { echo "  FAIL: .maxntid launch bound not honored"; ptxfail=1; }
    grep -q "%tid\.x" "$PTX"   || { echo "  FAIL: no %tid.x read in the PTX"; ptxfail=1; }
    grep -q "%ctaid\.x" "$PTX" || { echo "  FAIL: no %ctaid.x read in the PTX"; ptxfail=1; }
    grep -qE "(ld|st)\.global" "$PTX" || { echo "  FAIL: no global memory op in the kernel"; ptxfail=1; }
    grep -q "\.param \.u64 \.ptr \.global" "$PTX" || { echo "  FAIL: kernargs not in global param space"; ptxfail=1; }
    [ "$ptxfail" = "0" ] && echo "  ok   PTX: .visible .entry + .maxntid 256 + %tid.x/%ctaid.x + .global ops"
    [ "$ptxfail" = "0" ] || fail=1
fi

fi
else
    echo "  SKIP: no nvptx64 target in this llc (NVPTX leg)"
fi

echo ""
if [ "$fail" = "0" ]; then
    echo "  GPU: PASS"
    exit 0
else
    echo "  GPU: FAIL"
    exit 1
fi
