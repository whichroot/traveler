#!/usr/bin/env bash
# GPU device-codegen and measured-profile runtime gate.
# @internal-note: plan-gpu-purity-runtime.
#
# Proves that a proven-parallel pfor worker re-emits on four device targets:
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
#   Vulkan (--emit-gpu-vulkan) emits a closed GLSL artifact from the same
#                              worker proof. glslang assembles SPIR-V when
#                              present; Traveler owns public-ABI submission.
#
# Each leg skips cleanly if the local llc lacks that target; the gate fails only
# on a real lowering failure.
#
# Usage: ./run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

# --- --goldens-only: the llc-free floor ------------------------------------
# The canonical AGX byte goldens run everywhere (stage1 + cmp, no LLVM, no
# hardware). This mode is what environments without an LLVM toolchain can
# still prove; the full gate below needs llc (and, for owned-device legs,
# the measured M4 profile).
if [ "${1:-}" = "--goldens-only" ]; then
    STAGE1_G="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"
    if [ ! -x "$STAGE1_G" ]; then
        echo "FATAL: tvc_self not built at $STAGE1_G (run src/bootstrap/build.sh)" >&2
        exit 1
    fi
    echo "=== AGX byte goldens (tests/gpu --goldens-only) ==="
    gfail=0
    TMPG="$(mktemp -d)"; trap 'rm -rf "$TMPG"' EXIT
    for m in gpu_field_map gpu_field_map_64 gpu_field_map_mont; do
        if ! "$STAGE1_G" --emit-gpu-agx "$REPO_DIR/examples/$m.tv" -o "$TMPG/$m.hex" 2>/dev/null; then
            echo "  FAIL: $m did not emit AGX bytes"; gfail=1
        elif ! cmp -s "$TMPG/$m.hex" "$SCRIPT_DIR/golden/$m.agx.hex"; then
            echo "  FAIL: $m AGX bytes differ from golden"; gfail=1
        else
            echo "  ok   $m AGX bytes match golden"
        fi
    done
    if [ "$gfail" = "0" ]; then echo "  GOLDENS: PASS"; else echo "  GOLDENS: FAIL"; fi
    exit "$gfail"
fi

# Shared environment probe (tests/lib/env.sh): LINKER (link driver) plus
# capability flags.
. "$SCRIPT_DIR/../lib/env.sh"

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
    echo "FATAL: llc not found. Set LLC env var (or run: $0 --goldens-only)." >&2; exit 1
}
find_llc
# Resolve to an absolute path: llvm-objdump is derived via dirname below.
LLC="$(command -v "$LLC")"
# The objdump that ships beside llc (for the disasm sanity check).
OBJDUMP="$(dirname "$LLC")/llvm-objdump"
HOST_MTRIPLE=""
HOST_LINK_PIE=""
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  HOST_MTRIPLE="-mtriple=x86_64-linux-gnu";  HOST_LINK_PIE="-no-pie" ;;
    Linux-aarch64) HOST_MTRIPLE="-mtriple=aarch64-linux-gnu"; HOST_LINK_PIE="-no-pie" ;;
esac

echo "=== GPU target and AGX runtime gate (tests/gpu) ==="

# Which device targets are built into this llc? Each leg needs its own; a
# missing target is a SKIP (not a failure), a lowering error is a FAIL.
TARGETS="$("$LLC" --version 2>/dev/null)"
HAVE_AMD=0; HAVE_NV=0
echo "$TARGETS" | grep -qiE '^\s*amdgcn'  && HAVE_AMD=1
echo "$TARGETS" | grep -qiE '^\s*nvptx64' && HAVE_NV=1

STAGE1="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"
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
AGX_DOT_GENERAL_DEV="$TMP/agx_rns_dot_general.agx.hex"
AGX_DOT_GENERAL_LL="$TMP/agx_rns_dot_general.ll"
AGX_DOT_GENERAL_OBJ="$TMP/agx_rns_dot_general.o"
AGX_DOT_GENERAL_EXE="$TMP/agx-rns-dot-general"
AGX_DOT_REFUSE_DEV="$TMP/agx_rns_dot_refuse.agx.hex"
AGX_DOT_REFUSE_LL="$TMP/agx_rns_dot_refuse.ll"
AGX_DOT_REFUSE_REPORT="$TMP/agx_rns_dot_refuse.report"
AGX_LANG0_DEV="$TMP/agx_lang0.agx.hex"
AGX_LANG0_LL="$TMP/agx_lang0.ll"
AGX_LANG0_OBJ="$TMP/agx_lang0.o"
AGX_LANG0_EXE="$TMP/agx-lang0"
AGX_LANG0_EVAL_LL="$TMP/agx_lang0_eval.ll"
AGX_LANG0_EVAL_OBJ="$TMP/agx_lang0_eval.o"
AGX_LANG0_EVAL_EXE="$TMP/agx-lang0-eval"
AGX_LANG0_REFUSE="$TMP/agx_lang0_refuse.agx.hex"
AGX_LANG0_DEVICE_REFUSE="$TMP/agx_lang0_device_refuse.agx.hex"
AGX_GRID_BOUNDARY="$TMP/agx_grid_boundary.agx.hex"
AGX_LANG0_SHADOW_REFUSE="$TMP/agx_lang0_shadow_refuse.agx.hex"
AGX_LANG0_SHADOW_LL="$TMP/agx_lang0_shadow_refuse.ll"
AGX_LANG1_STRUCT_DEV="$TMP/agx_lang1_struct.agx.hex"
AGX_LANG1_STRUCT_LL="$TMP/agx_lang1_struct.ll"
AGX_LANG1_STRUCT_OBJ="$TMP/agx_lang1_struct.o"
AGX_LANG1_STRUCT_EXE="$TMP/agx-lang1-struct"
AGX_LANG1_STRUCT_EVAL_LL="$TMP/agx_lang1_struct_eval.ll"
AGX_LANG1_STRUCT_EVAL_OBJ="$TMP/agx_lang1_struct_eval.o"
AGX_LANG1_STRUCT_EVAL_EXE="$TMP/agx-lang1-struct-eval"
AGX_LANG1_MATCH_DEV="$TMP/agx_lang1_match.agx.hex"
AGX_LANG1_MATCH_LL="$TMP/agx_lang1_match.ll"
AGX_LANG1_MATCH_OBJ="$TMP/agx_lang1_match.o"
AGX_LANG1_MATCH_EXE="$TMP/agx-lang1-match"
AGX_LANG1_MATCH_EVAL_LL="$TMP/agx_lang1_match_eval.ll"
AGX_LANG1_MATCH_EVAL_OBJ="$TMP/agx_lang1_match_eval.o"
AGX_LANG1_MATCH_EVAL_EXE="$TMP/agx-lang1-match-eval"
AGX_LANG1_ARRAY_DEV="$TMP/agx_lang1_array.agx.hex"
AGX_LANG1_ARRAY_LL="$TMP/agx_lang1_array.ll"
AGX_LANG1_ARRAY_OBJ="$TMP/agx_lang1_array.o"
AGX_LANG1_ARRAY_EXE="$TMP/agx-lang1-array"
AGX_LANG1_ARRAY_EVAL_LL="$TMP/agx_lang1_array_eval.ll"
AGX_LANG1_ARRAY_EVAL_OBJ="$TMP/agx_lang1_array_eval.o"
AGX_LANG1_ARRAY_EVAL_EXE="$TMP/agx-lang1-array-eval"
AGX_LANG1_CALL_DEV="$TMP/agx_lang1_call.agx.hex"
AGX_LANG1_CALL_LL="$TMP/agx_lang1_call.ll"
AGX_LANG1_CALL_OBJ="$TMP/agx_lang1_call.o"
AGX_LANG1_CALL_EXE="$TMP/agx-lang1-call"
AGX_LANG1_CALL_EVAL_LL="$TMP/agx_lang1_call_eval.ll"
AGX_LANG1_CALL_EVAL_OBJ="$TMP/agx_lang1_call_eval.o"
AGX_LANG1_CALL_EVAL_EXE="$TMP/agx-lang1-call-eval"
AGX_LANG1_OPERATOR_DEV="$TMP/agx_lang1_operator.agx.hex"
AGX_LANG1_OPERATOR_LL="$TMP/agx_lang1_operator.ll"
AGX_LANG1_OPERATOR_OBJ="$TMP/agx_lang1_operator.o"
AGX_LANG1_OPERATOR_EXE="$TMP/agx-lang1-operator"
AGX_LANG1_OPERATOR_EVAL_LL="$TMP/agx_lang1_operator_eval.ll"
AGX_LANG1_OPERATOR_EVAL_OBJ="$TMP/agx_lang1_operator_eval.o"
AGX_LANG1_OPERATOR_EVAL_EXE="$TMP/agx-lang1-operator-eval"
AGX_LANG1_CLOSURE_DEV="$TMP/agx_lang1_closure.agx.hex"
AGX_LANG1_CLOSURE_LL="$TMP/agx_lang1_closure.ll"
AGX_LANG1_CLOSURE_OBJ="$TMP/agx_lang1_closure.o"
AGX_LANG1_CLOSURE_EXE="$TMP/agx-lang1-closure"
AGX_LANG1_CLOSURE_EVAL_LL="$TMP/agx_lang1_closure_eval.ll"
AGX_LANG1_CLOSURE_EVAL_OBJ="$TMP/agx_lang1_closure_eval.o"
AGX_LANG1_CLOSURE_EVAL_EXE="$TMP/agx-lang1-closure-eval"

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
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_DOT_LOOP_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input-layout 1$' "$AGX_DOT_LOOP_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^bytes 924$' "$AGX_DOT_LOOP_DEV" || true)" -ne 3 ] \
       || ! python3 "$SCRIPT_DIR/agx_counted_dot_probe.py" \
           --check-artifact "$AGX_DOT_LOOP_DEV" \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_dot_loop.tv" \
           -o "$AGX_DOT_LOOP_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_DOT_LOOP_LL" || true)" -ne 3 ] \
       || ! "$LLC" -filetype=obj "$AGX_DOT_LOOP_LL" -o "$AGX_DOT_LOOP_OBJ" 2>/dev/null; then
        echo "  FAIL: canonical nested K=8 RNS dot did not match counted evidence"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_dot_general.tv" \
            -o "$AGX_DOT_GENERAL_DEV" 2>/dev/null \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' "$AGX_DOT_GENERAL_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^grid 1024$' "$AGX_DOT_GENERAL_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^output-words 1024$' "$AGX_DOT_GENERAL_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input0-words 8$' "$AGX_DOT_GENERAL_DEV" || true)" -ne 3 ] \
       || [ "$(grep -c '^input1-words 8192$' "$AGX_DOT_GENERAL_DEV" || true)" -ne 3 ] \
       || ! python3 "$SCRIPT_DIR/agx_counted_dot_probe.py" \
            --check-artifact "$AGX_DOT_GENERAL_DEV" 1024 8 8192 \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_rns_dot_general.tv" \
            -o "$AGX_DOT_GENERAL_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_DOT_GENERAL_LL" || true)" -ne 3 ] \
       || ! "$LLC" -filetype=obj "$AGX_DOT_GENERAL_LL" \
            -o "$AGX_DOT_GENERAL_OBJ" 2>/dev/null; then
        echo "  FAIL: generalized 1x8x1024 counted dot did not compile"; fail=1
        AGX3_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
           -o "$AGX_DOT_REFUSE_DEV" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$AGX_DOT_REFUSE_DEV" \
       || ! grep -q '^; no AGX-0 workers emitted$' "$AGX_DOT_REFUSE_DEV" \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
            -o "$AGX_DOT_REFUSE_LL" 2>/dev/null \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                     "$AGX_DOT_REFUSE_LL" || true)" -ne 3 ] \
       || [ "$(grep -c 'call void @__parallel_for(' \
                     "$AGX_DOT_REFUSE_LL" || true)" -ne 3 ] \
       || ! "$STAGE1" --pfor-report "$SCRIPT_DIR/agx_rns_dot_refuse.tv" \
            >"$AGX_DOT_REFUSE_REPORT" 2>/dev/null \
        || [ "$(grep -c '"dispatched":1' "$AGX_DOT_REFUSE_REPORT" || true)" -ne 3 ] \
       || [ "$(grep -c '"reason":"assign-carried"' \
                     "$AGX_DOT_REFUSE_REPORT" || true)" -ne 3 ]; then
        echo "  FAIL: noncanonical nested RNS dot CPU/AGX boundary changed"; fail=1
        AGX3_READY=0
    fi
    if [ "$AGX3_READY" = "1" ]; then
        echo "  ok   AGX runtime dispatch, RNS, and counted-dot artifacts compile"
    fi

    # IR0 lowers every admitted worker shape through typed EIR + a private CFG,
    # validates it, and discards it. Host IR and direct AGX bytes must not move.
    IR0_READY=1
    if ! "$STAGE1" --emit-gpu-agx --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_dispatch_gate.tv" -o "$TMP/agx_ir0_k1.hex" 2>/dev/null \
       || ! "$STAGE1" --agx-dispatch --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_dispatch_gate.tv" -o "$TMP/agx_ir0_k1.ll" 2>/dev/null \
       || ! cmp -s "$AGX_DISPATCH_DEV" "$TMP/agx_ir0_k1.hex" \
       || ! cmp -s "$AGX_DISPATCH_LL" "$TMP/agx_ir0_k1.ll"; then
        echo "  FAIL: AGX IR0 map shadow changed output or failed validation"; fail=1
        IR0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_reduce8.tv" -o "$TMP/agx_ir0_k2.hex" 2>/dev/null \
       || ! "$STAGE1" --agx-dispatch --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_reduce8.tv" -o "$TMP/agx_ir0_k2.ll" 2>/dev/null \
       || ! cmp -s "$AGX_REDUCE_DEV" "$TMP/agx_ir0_k2.hex" \
       || ! cmp -s "$AGX_REDUCE_LL" "$TMP/agx_ir0_k2.ll"; then
        echo "  FAIL: AGX IR0 reduce-8 shadow changed output or failed validation"; fail=1
        IR0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_dot.tv" -o "$TMP/agx_ir0_k3.hex" 2>/dev/null \
       || ! "$STAGE1" --agx-dispatch --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_dot.tv" -o "$TMP/agx_ir0_k3.ll" 2>/dev/null \
       || ! cmp -s "$AGX_DOT_DEV" "$TMP/agx_ir0_k3.hex" \
       || ! cmp -s "$AGX_DOT_LL" "$TMP/agx_ir0_k3.ll"; then
        echo "  FAIL: AGX IR0 unrolled-dot shadow changed output or failed validation"; fail=1
        IR0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_dot_loop.tv" -o "$TMP/agx_ir0_k4.hex" 2>/dev/null \
       || ! "$STAGE1" --agx-dispatch --agx-ir0-shadow \
            "$SCRIPT_DIR/agx_rns_dot_loop.tv" -o "$TMP/agx_ir0_k4.ll" 2>/dev/null \
       || ! cmp -s "$AGX_DOT_LOOP_DEV" "$TMP/agx_ir0_k4.hex" \
       || ! cmp -s "$AGX_DOT_LOOP_LL" "$TMP/agx_ir0_k4.ll"; then
        echo "  FAIL: AGX IR0 counted-dot shadow changed output or failed validation"; fail=1
        IR0_READY=0
    fi
    if "$STAGE1" --agx-ir0-shadow "$SCRIPT_DIR/agx_binary_map.tv" \
            >"$TMP/agx_ir0_no_output.out" 2>"$TMP/agx_ir0_no_output.err"; then
        echo "  FAIL: AGX IR0 shadow silently skipped without -o"; fail=1
        IR0_READY=0
    elif ! grep -q -- '--agx-ir0-shadow requires -o' \
            "$TMP/agx_ir0_no_output.err"; then
        echo "  FAIL: AGX IR0 missing-output refusal changed"; fail=1
        IR0_READY=0
    fi
    if [ "$IR0_READY" = "1" ]; then
        echo "  ok   AGX IR0 validates shapes 1-4 with byte-identical host/device output"
    fi

    # RA0 keeps both legacy allocators authoritative, but independently proves
    # CFG liveness/copies and exact machine-resource feasibility or refusal.
    RA0_READY=1
    if ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_dispatch_gate.tv" -o "$TMP/agx_ra0_k1.hex" \
            2>"$TMP/agx_ra0_k1.report" \
       || ! "$STAGE1" --agx-dispatch --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_dispatch_gate.tv" -o "$TMP/agx_ra0_k1.ll" \
            2>"$TMP/agx_ra0_k1_host.report" \
       || ! cmp -s "$AGX_DISPATCH_DEV" "$TMP/agx_ra0_k1.hex" \
       || ! cmp -s "$AGX_DISPATCH_LL" "$TMP/agx_ra0_k1.ll" \
       || ! grep -qF 'agx-ra0: shape=1 blocks=1 iterations=2 copies=0 repairs=9 slot-regs=1 live-regs=1 vm-regs=7 pairs=8 loads=2 outcome=1' \
            "$TMP/agx_ra0_k1.report" \
       || ! grep -qF 'agx-ra0: shape=1 blocks=1 iterations=2 copies=0 repairs=9 slot-regs=1 live-regs=1 vm-regs=7 pairs=8 loads=2 outcome=1' \
            "$TMP/agx_ra0_k1_host.report"; then
        echo "  FAIL: AGX RA0 map allocation or byte stability changed"; fail=1
        RA0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_reduce8.tv" -o "$TMP/agx_ra0_k2.hex" \
            2>"$TMP/agx_ra0_k2.report" \
       || ! "$STAGE1" --agx-dispatch --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_reduce8.tv" -o "$TMP/agx_ra0_k2.ll" \
            2>"$TMP/agx_ra0_k2_host.report" \
       || ! cmp -s "$AGX_REDUCE_DEV" "$TMP/agx_ra0_k2.hex" \
       || ! cmp -s "$AGX_REDUCE_LL" "$TMP/agx_ra0_k2.ll" \
       || [ "$(grep -cF 'agx-ra0: shape=2 blocks=1 iterations=2 copies=0 repairs=8 slot-regs=1 live-regs=1 vm-regs=6 pairs=7 loads=8 outcome=1' "$TMP/agx_ra0_k2.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 reduce-8 allocation or byte stability changed"; fail=1
        RA0_READY=0
    elif [ "$(grep -cF 'agx-ra0: shape=2 blocks=1 iterations=2 copies=0 repairs=8 slot-regs=1 live-regs=1 vm-regs=6 pairs=7 loads=8 outcome=1' "$TMP/agx_ra0_k2_host.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 reduce-8 allocation or byte stability changed"; fail=1
        RA0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_dot.tv" -o "$TMP/agx_ra0_k3.hex" \
            2>"$TMP/agx_ra0_k3.report" \
       || ! "$STAGE1" --agx-dispatch --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_dot.tv" -o "$TMP/agx_ra0_k3.ll" \
            2>"$TMP/agx_ra0_k3_host.report" \
       || ! cmp -s "$AGX_DOT_DEV" "$TMP/agx_ra0_k3.hex" \
       || ! cmp -s "$AGX_DOT_LL" "$TMP/agx_ra0_k3.ll" \
       || [ "$(grep -cF 'agx-ra0: shape=3 blocks=1 iterations=2 copies=0 repairs=77 slot-regs=1 live-regs=1 vm-regs=10 pairs=71 loads=16 outcome=1' "$TMP/agx_ra0_k3.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 unrolled-dot allocation or byte stability changed"; fail=1
        RA0_READY=0
    elif [ "$(grep -cF 'agx-ra0: shape=3 blocks=1 iterations=2 copies=0 repairs=77 slot-regs=1 live-regs=1 vm-regs=10 pairs=71 loads=16 outcome=1' "$TMP/agx_ra0_k3_host.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 unrolled-dot allocation or byte stability changed"; fail=1
        RA0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_dot_loop.tv" -o "$TMP/agx_ra0_k4.hex" \
            2>"$TMP/agx_ra0_k4.report" \
       || ! "$STAGE1" --agx-dispatch --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_rns_dot_loop.tv" -o "$TMP/agx_ra0_k4.ll" \
            2>"$TMP/agx_ra0_k4_host.report" \
       || ! cmp -s "$AGX_DOT_LOOP_DEV" "$TMP/agx_ra0_k4.hex" \
       || ! cmp -s "$AGX_DOT_LOOP_LL" "$TMP/agx_ra0_k4.ll" \
       || [ "$(grep -cF 'agx-ra0: shape=4 blocks=5 iterations=3 copies=4 repairs=23 slot-regs=3 live-regs=3 vm-regs=7 pairs=9 loads=2 outcome=1' "$TMP/agx_ra0_k4.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 counted-dot allocation or byte stability changed"; fail=1
        RA0_READY=0
    elif [ "$(grep -cF 'agx-ra0: shape=4 blocks=5 iterations=3 copies=4 repairs=23 slot-regs=3 live-regs=3 vm-regs=7 pairs=9 loads=2 outcome=1' "$TMP/agx_ra0_k4_host.report" || true)" -ne 3 ]; then
        echo "  FAIL: AGX RA0 counted-dot allocation or byte stability changed"; fail=1
        RA0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$WIDE_EXHIBIT" -o "$TMP/agx_ra0_wide.hex" \
            2>"$TMP/agx_ra0_wide.report" \
       || ! cmp -s "$AGX_WIDE_DEV" "$TMP/agx_ra0_wide.hex" \
       || ! grep -qF 'agx-ra0: shape=1 blocks=1 iterations=2 copies=0 repairs=80 slot-regs=1 live-regs=1 vm-regs=12 pairs=12 loads=2 outcome=1' \
            "$TMP/agx_ra0_wide.report"; then
        echo "  FAIL: AGX RA0 64-bit pair allocation changed"; fail=1
        RA0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_ra0_pressure.tv" \
            -o "$TMP/agx_ra0_pressure_base.hex" 2>/dev/null \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_ra0_pressure.tv" \
            -o "$TMP/agx_ra0_pressure.hex" 2>"$TMP/agx_ra0_pressure.report" \
       || ! cmp -s "$TMP/agx_ra0_pressure_base.hex" "$TMP/agx_ra0_pressure.hex" \
       || grep -q '^worker __pfor_gpu_worker_' "$TMP/agx_ra0_pressure.hex" \
       || ! grep -q '^skip __pfor_gpu_worker_0 reason=unsupported-agx0-worker$' \
            "$TMP/agx_ra0_pressure.hex" \
       || ! grep -qF 'agx-ra0: shape=1 blocks=1 iterations=2 copies=0 repairs=0 slot-regs=1 live-regs=1 vm-regs=30 pairs=0 loads=0 outcome=0' \
            "$TMP/agx_ra0_pressure.report"; then
        echo "  FAIL: AGX RA0 pressure did not refuse without spilling"; fail=1
        RA0_READY=0
    fi
    if "$STAGE1" --agx-ra0-shadow "$SCRIPT_DIR/agx_binary_map.tv" \
            >"$TMP/agx_ra0_no_output.out" 2>"$TMP/agx_ra0_no_output.err"; then
        echo "  FAIL: AGX RA0 shadow silently skipped without -o"; fail=1
        RA0_READY=0
    elif ! grep -q -- '--agx-ra0-shadow requires -o' \
            "$TMP/agx_ra0_no_output.err"; then
        echo "  FAIL: AGX RA0 missing-output refusal changed"; fail=1
        RA0_READY=0
    fi
    if [ "$RA0_READY" = "1" ]; then
        echo "  ok   AGX RA0 liveness, copies, pairs, loads, and pressure refusal are pinned"
    fi

    # LANG0 is the first authoritative IR0/RA0 path. Its closed AGX-only proof
    # admits structured scalar control; PROOF1 independently owns CPU dispatch.
    LANG0_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang0_dispatch.tv" \
            -o "$AGX_LANG0_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang0_probe.py" \
            --check-artifact "$AGX_LANG0_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang0_dispatch.tv" \
            -o "$TMP/agx_lang0_ra.hex" 2>"$TMP/agx_lang0_ra.report" \
       || ! cmp -s "$AGX_LANG0_DEV" "$TMP/agx_lang0_ra.hex" \
       || [ "$(grep -c '^agx-ra0: shape=5 ' "$TMP/agx_lang0_ra.report" || true)" -ne 4 ] \
       || ! grep -qF 'agx-ra0: shape=5 blocks=4 iterations=2 copies=2 repairs=0 slot-regs=4 live-regs=3 vm-regs=0 pairs=0 loads=2 outcome=1' "$TMP/agx_lang0_ra.report" \
       || ! grep -qF 'agx-ra0: shape=5 blocks=11 iterations=4 copies=6 repairs=2 slot-regs=6 live-regs=5 vm-regs=0 pairs=0 loads=2 outcome=1' "$TMP/agx_lang0_ra.report" \
       || ! grep -qF 'agx-ra0: shape=5 blocks=15 iterations=6 copies=10 repairs=4 slot-regs=7 live-regs=6 vm-regs=0 pairs=0 loads=2 outcome=1' "$TMP/agx_lang0_ra.report" \
       || ! grep -qF 'agx-ra0: shape=5 blocks=11 iterations=5 copies=8 repairs=2 slot-regs=6 live-regs=5 vm-regs=0 pairs=0 loads=2 outcome=1' "$TMP/agx_lang0_ra.report"; then
        echo "  FAIL: AGX LANG0 artifact, allocation report, or byte stability changed"; fail=1
        LANG0_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang0_dispatch.tv" \
            -o "$AGX_LANG0_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' "$AGX_LANG0_LL" || true)" -ne 4 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' "$AGX_LANG0_LL" || true)" -ne 4 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG0_LL" -o "$AGX_LANG0_OBJ" 2>/dev/null \
       || ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang0_eval.tv" \
            >"$TMP/agx_lang0.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang0_eval.tv" \
            -o "$AGX_LANG0_EVAL_LL" 2>/dev/null \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                    "$AGX_LANG0_EVAL_LL" || true)" -ne 4 ] \
       || [ "$(grep -c 'call void @__parallel_for(' \
                    "$AGX_LANG0_EVAL_LL" || true)" -ne 4 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG0_EVAL_LL" \
             -o "$AGX_LANG0_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG0_EVAL_OBJ" \
             -o "$AGX_LANG0_EVAL_EXE" 2>/dev/null \
       || ! env TRAVELER_THREADS=1 "$AGX_LANG0_EVAL_EXE" \
             >"$TMP/agx_lang0.t1.bin" \
       || ! env TRAVELER_THREADS=4 "$AGX_LANG0_EVAL_EXE" \
             >"$TMP/agx_lang0.t4.bin" \
       || ! env TRAVELER_THREADS=32 "$AGX_LANG0_EVAL_EXE" \
             >"$TMP/agx_lang0.t32.bin" \
       || ! cmp -s "$TMP/agx_lang0.eval.bin" "$TMP/agx_lang0.t1.bin" \
       || ! cmp -s "$TMP/agx_lang0.eval.bin" "$TMP/agx_lang0.t4.bin" \
       || ! cmp -s "$TMP/agx_lang0.eval.bin" "$TMP/agx_lang0.t32.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang0_probe.py" \
             --check-output "$TMP/agx_lang0.eval.bin"; then
        echo "  FAIL: AGX LANG0 eval/compiled/CPU-pfor substrate changed"; fail=1
        LANG0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang0_refuse.tv" \
            -o "$AGX_LANG0_REFUSE" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$AGX_LANG0_REFUSE" \
       || ! grep -q '^skip __pfor_gpu_worker_0 reason=unsupported-agx0-worker$' \
            "$AGX_LANG0_REFUSE"; then
        echo "  FAIL: AGX LANG0 dynamic-loop refusal changed"; fail=1
        LANG0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang0_device_refuse.tv" \
            -o "$AGX_LANG0_DEVICE_REFUSE" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$AGX_LANG0_DEVICE_REFUSE" \
       || [ "$(grep -c '^skip __pfor_gpu_worker_.* reason=unsupported-agx0-worker$' \
                     "$AGX_LANG0_DEVICE_REFUSE" || true)" -ne 6 ]; then
        echo "  FAIL: AGX LANG0 iterator/cast/width/store refusals changed"; fail=1
        LANG0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_grid_boundary.tv" \
            -o "$AGX_GRID_BOUNDARY" 2>/dev/null \
       || [ "$(grep -c '^worker __pfor_gpu_worker_' \
                    "$AGX_GRID_BOUNDARY" || true)" -ne 1 ] \
       || ! grep -q '^grid 65535$' "$AGX_GRID_BOUNDARY" \
       || grep -q '^skip __pfor_gpu_worker_' "$AGX_GRID_BOUNDARY"; then
        echo "  FAIL: AGX inclusive 65,535-element grid boundary changed"; fail=1
        LANG0_READY=0
    fi
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang0_shadow_refuse.tv" \
            -o "$AGX_LANG0_SHADOW_REFUSE" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$AGX_LANG0_SHADOW_REFUSE" \
       || grep -q '^skip __pfor_gpu_worker_' "$AGX_LANG0_SHADOW_REFUSE" \
       || ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang0_shadow_refuse.tv" \
            -o "$AGX_LANG0_SHADOW_LL" 2>/dev/null \
       || grep -q 'call i32 @agx_try_parallel_for' "$AGX_LANG0_SHADOW_LL" \
       || grep -q '^define internal void @__pfor_worker_' "$AGX_LANG0_SHADOW_LL"; then
        echo "  FAIL: AGX LANG0 shadowed iterator entered the pfor proof"; fail=1
        LANG0_READY=0
    fi
    if [ "$LANG0_READY" = "1" ]; then
        echo "  ok   AGX LANG0 structured scalar artifact and eval oracle are pinned"
    fi
    LANG0_HOST_READY=0
    if [ "$LANG0_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG0_OBJ" -framework IOKit \
                -o "$AGX_LANG0_EXE" 2>/dev/null; then
            echo "  FAIL: AGX LANG0 CPU-fallback differential did not link"; fail=1
            LANG0_READY=0
        else
            (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
             TRAVELER_THREADS=4 "$AGX_LANG0_EXE" >"$TMP/agx_lang0.cpu.bin")
            lang0_cpu_status=$?
            if [ "$lang0_cpu_status" -ne 0 ] \
               || ! cmp -s "$TMP/agx_lang0.eval.bin" "$TMP/agx_lang0.cpu.bin" \
               || ! python3 "$SCRIPT_DIR/agx_lang0_probe.py" \
                    --check-output "$TMP/agx_lang0.cpu.bin"; then
                echo "  FAIL: AGX LANG0 serial/CPU-pfor differential changed"; fail=1
                LANG0_READY=0
            else
                LANG0_HOST_READY=1
                echo "  ok   AGX LANG0 serial and four-thread CPU pfor are byte-exact"
            fi
        fi
    fi

    # LANG1-H1 normalizes a private flat struct to scalar EIR slots before CFG
    # and RA. The ordinary CPU proof remains conservative; --agx-dispatch owns
    # the recursive CPU fallback for this newly admitted AGX-only shape.
    LANG1_STRUCT_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_struct.tv" \
            -o "$AGX_LANG1_STRUCT_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-artifact "$AGX_LANG1_STRUCT_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_struct.tv" \
            -o "$TMP/agx_lang1_struct.ra.hex" \
            2>"$TMP/agx_lang1_struct.ra.report" \
       || ! cmp -s "$AGX_LANG1_STRUCT_DEV" "$TMP/agx_lang1_struct.ra.hex" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=6 iterations=2 copies=4 repairs=0 slot-regs=6 live-regs=5 vm-regs=0 pairs=0 loads=2 outcome=1' \
            "$TMP/agx_lang1_struct.ra.report"; then
        echo "  FAIL: AGX LANG1 struct normalization/artifact changed"; fail=1
        LANG1_STRUCT_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_struct_eval.tv" \
            >"$TMP/agx_lang1_struct.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_struct_eval.tv" \
            -o "$AGX_LANG1_STRUCT_EVAL_LL" 2>/dev/null \
       || grep -q 'call void @__parallel_for(' "$AGX_LANG1_STRUCT_EVAL_LL" \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_STRUCT_EVAL_LL" \
            -o "$AGX_LANG1_STRUCT_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_STRUCT_EVAL_OBJ" \
            -o "$AGX_LANG1_STRUCT_EVAL_EXE" 2>/dev/null \
       || ! "$AGX_LANG1_STRUCT_EVAL_EXE" \
            >"$TMP/agx_lang1_struct.serial.bin" \
       || ! cmp -s "$TMP/agx_lang1_struct.eval.bin" \
            "$TMP/agx_lang1_struct.serial.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-output "$TMP/agx_lang1_struct.eval.bin"; then
        echo "  FAIL: AGX LANG1 eval/serial struct oracle changed"; fail=1
        LANG1_STRUCT_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_struct_dispatch.tv" \
            -o "$AGX_LANG1_STRUCT_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                   "$AGX_LANG1_STRUCT_LL" || true)" -ne 1 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                   "$AGX_LANG1_STRUCT_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_STRUCT_LL" \
            -o "$AGX_LANG1_STRUCT_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 dispatch/CPU-fallback artifact changed"; fail=1
        LANG1_STRUCT_READY=0
    fi
    if [ "$LANG1_STRUCT_READY" = "1" ]; then
        echo "  ok   AGX LANG1 flat struct scalarization and recursive fallback are pinned"
    fi
    LANG1_STRUCT_HOST_READY=0
    if [ "$LANG1_STRUCT_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_STRUCT_OBJ" -framework IOKit \
                -o "$AGX_LANG1_STRUCT_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_STRUCT_EXE" \
                >"$TMP/agx_lang1_struct.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_STRUCT_EXE" \
                >"$TMP/agx_lang1_struct.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_STRUCT_EXE" \
                >"$TMP/agx_lang1_struct.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_struct.eval.bin" \
                "$TMP/agx_lang1_struct.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_struct.eval.bin" \
                "$TMP/agx_lang1_struct.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_struct.eval.bin" \
                "$TMP/agx_lang1_struct.t32.bin"; then
            echo "  FAIL: AGX LANG1 recursive CPU fallback differs at T=1/4/32"
            fail=1; LANG1_STRUCT_READY=0
        else
            LANG1_STRUCT_HOST_READY=1
            echo "  ok   AGX LANG1 eval, serial, and recursive CPU fallback agree at T=1/4/32"
        fi
    fi
    LANG1_MATCH_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_match.tv" \
            -o "$AGX_LANG1_MATCH_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-match-artifact "$AGX_LANG1_MATCH_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_match.tv" \
            -o "$TMP/agx_lang1_match.ra.hex" \
            2>"$TMP/agx_lang1_match.ra.report" \
       || ! cmp -s "$AGX_LANG1_MATCH_DEV" "$TMP/agx_lang1_match.ra.hex" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=6 iterations=3 copies=4 repairs=0 slot-regs=4 live-regs=4 vm-regs=0 pairs=0 loads=2 outcome=1' \
            "$TMP/agx_lang1_match.ra.report" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=11 iterations=3 copies=8 repairs=0 slot-regs=8 live-regs=6 vm-regs=0 pairs=0 loads=2 outcome=1' \
            "$TMP/agx_lang1_match.ra.report"; then
        echo "  FAIL: AGX LANG1 integer/enum match normalization changed"; fail=1
        LANG1_MATCH_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_match_eval.tv" \
            >"$TMP/agx_lang1_match.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_match_eval.tv" \
            -o "$AGX_LANG1_MATCH_EVAL_LL" 2>/dev/null \
       || grep -q 'call void @__parallel_for(' "$AGX_LANG1_MATCH_EVAL_LL" \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_MATCH_EVAL_LL" \
            -o "$AGX_LANG1_MATCH_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_MATCH_EVAL_OBJ" \
            -o "$AGX_LANG1_MATCH_EVAL_EXE" 2>/dev/null \
       || ! "$AGX_LANG1_MATCH_EVAL_EXE" \
            >"$TMP/agx_lang1_match.serial.bin" \
       || ! cmp -s "$TMP/agx_lang1_match.eval.bin" \
            "$TMP/agx_lang1_match.serial.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-match-output "$TMP/agx_lang1_match.eval.bin"; then
        echo "  FAIL: AGX LANG1 match eval/serial oracle changed"; fail=1
        LANG1_MATCH_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_match_dispatch.tv" \
            -o "$AGX_LANG1_MATCH_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                   "$AGX_LANG1_MATCH_LL" || true)" -ne 2 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                   "$AGX_LANG1_MATCH_LL" || true)" -ne 2 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_MATCH_LL" \
            -o "$AGX_LANG1_MATCH_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 match dispatch/CPU fallback changed"; fail=1
        LANG1_MATCH_READY=0
    fi
    if [ "$LANG1_MATCH_READY" = "1" ]; then
        echo "  ok   AGX LANG1 integer/enum match artifacts and fallbacks are pinned"
    fi
    LANG1_MATCH_HOST_READY=0
    if [ "$LANG1_MATCH_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_MATCH_OBJ" -framework IOKit \
                -o "$AGX_LANG1_MATCH_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_MATCH_EXE" \
                >"$TMP/agx_lang1_match.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_MATCH_EXE" \
                >"$TMP/agx_lang1_match.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_MATCH_EXE" \
                >"$TMP/agx_lang1_match.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_match.eval.bin" \
                "$TMP/agx_lang1_match.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_match.eval.bin" \
                "$TMP/agx_lang1_match.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_match.eval.bin" \
                "$TMP/agx_lang1_match.t32.bin"; then
            echo "  FAIL: AGX LANG1 match fallback differs at T=1/4/32"
            fail=1; LANG1_MATCH_READY=0
        else
            LANG1_MATCH_HOST_READY=1
            echo "  ok   AGX LANG1 match eval, serial, and CPU fallback agree at T=1/4/32"
        fi
    fi
    LANG1_ARRAY_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_array.tv" \
            -o "$AGX_LANG1_ARRAY_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-array-artifact "$AGX_LANG1_ARRAY_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_array.tv" \
            -o "$TMP/agx_lang1_array.ra.hex" \
            2>"$TMP/agx_lang1_array.ra.report" \
       || ! cmp -s "$AGX_LANG1_ARRAY_DEV" "$TMP/agx_lang1_array.ra.hex" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=3 iterations=2 copies=2 repairs=0 slot-regs=8 live-regs=3 vm-regs=0 pairs=0 loads=2 outcome=1' \
            "$TMP/agx_lang1_array.ra.report" \
       || ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_array_refuse.tv" \
            -o "$TMP/agx_lang1_array_refuse.hex" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$TMP/agx_lang1_array_refuse.hex" \
       || [ "$(grep -c '^skip __pfor_gpu_worker_' \
                    "$TMP/agx_lang1_array_refuse.hex" || true)" -ne 4 ]; then
        echo "  FAIL: AGX LANG1 fixed-array artifact/refusal changed"; fail=1
        LANG1_ARRAY_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_array_eval.tv" \
            >"$TMP/agx_lang1_array.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_array_eval.tv" \
            -o "$AGX_LANG1_ARRAY_EVAL_LL" 2>/dev/null \
       || grep -q 'call void @__parallel_for(' "$AGX_LANG1_ARRAY_EVAL_LL" \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_ARRAY_EVAL_LL" \
            -o "$AGX_LANG1_ARRAY_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_ARRAY_EVAL_OBJ" \
            -o "$AGX_LANG1_ARRAY_EVAL_EXE" 2>/dev/null \
       || ! "$AGX_LANG1_ARRAY_EVAL_EXE" \
            >"$TMP/agx_lang1_array.serial.bin" \
       || ! cmp -s "$TMP/agx_lang1_array.eval.bin" \
            "$TMP/agx_lang1_array.serial.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-array-output "$TMP/agx_lang1_array.eval.bin"; then
        echo "  FAIL: AGX LANG1 array eval/serial oracle changed"; fail=1
        LANG1_ARRAY_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_array_dispatch.tv" \
            -o "$AGX_LANG1_ARRAY_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                   "$AGX_LANG1_ARRAY_LL" || true)" -ne 1 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                   "$AGX_LANG1_ARRAY_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_ARRAY_LL" \
            -o "$AGX_LANG1_ARRAY_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 array dispatch/CPU fallback changed"; fail=1
        LANG1_ARRAY_READY=0
    fi
    if [ "$LANG1_ARRAY_READY" = "1" ]; then
        echo "  ok   AGX LANG1 fixed-array scalarization and remaining index refusals are pinned"
    fi
    LANG1_ARRAY_HOST_READY=0
    if [ "$LANG1_ARRAY_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_ARRAY_OBJ" -framework IOKit \
                -o "$AGX_LANG1_ARRAY_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_ARRAY_EXE" \
                >"$TMP/agx_lang1_array.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_ARRAY_EXE" \
                >"$TMP/agx_lang1_array.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_ARRAY_EXE" \
                >"$TMP/agx_lang1_array.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_array.eval.bin" \
                "$TMP/agx_lang1_array.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_array.eval.bin" \
                "$TMP/agx_lang1_array.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_array.eval.bin" \
                "$TMP/agx_lang1_array.t32.bin"; then
            echo "  FAIL: AGX LANG1 array fallback differs at T=1/4/32"
            fail=1; LANG1_ARRAY_READY=0
        else
            LANG1_ARRAY_HOST_READY=1
            echo "  ok   AGX LANG1 array eval, serial, and CPU fallback agree at T=1/4/32"
        fi
    fi
    LANG1_CALL_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_call.tv" \
            -o "$AGX_LANG1_CALL_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-call-artifact "$AGX_LANG1_CALL_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_call.tv" \
            -o "$TMP/agx_lang1_call.ra.hex" \
            2>"$TMP/agx_lang1_call.ra.report" \
       || ! cmp -s "$AGX_LANG1_CALL_DEV" "$TMP/agx_lang1_call.ra.hex" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=3 iterations=2 copies=2 repairs=0 slot-regs=2 live-regs=2 vm-regs=0 pairs=0 loads=4 outcome=1' \
            "$TMP/agx_lang1_call.ra.report"; then
        echo "  FAIL: AGX LANG1 direct/generic call artifact changed"; fail=1
        LANG1_CALL_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_call_eval.tv" \
            >"$TMP/agx_lang1_call.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_call_eval.tv" \
            -o "$AGX_LANG1_CALL_EVAL_LL" 2>/dev/null \
       || [ "$(grep -c 'call void @__parallel_for(' \
                    "$AGX_LANG1_CALL_EVAL_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_CALL_EVAL_LL" \
            -o "$AGX_LANG1_CALL_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_CALL_EVAL_OBJ" \
            -o "$AGX_LANG1_CALL_EVAL_EXE" 2>/dev/null \
       || ! env TRAVELER_THREADS=4 "$AGX_LANG1_CALL_EVAL_EXE" \
            >"$TMP/agx_lang1_call.cpu.bin" \
       || ! cmp -s "$TMP/agx_lang1_call.eval.bin" \
            "$TMP/agx_lang1_call.cpu.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-call-output "$TMP/agx_lang1_call.eval.bin"; then
        echo "  FAIL: AGX LANG1 direct/generic call oracle changed"; fail=1
        LANG1_CALL_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_call_dispatch.tv" \
            -o "$AGX_LANG1_CALL_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                    "$AGX_LANG1_CALL_LL" || true)" -ne 1 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                    "$AGX_LANG1_CALL_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_CALL_LL" \
            -o "$AGX_LANG1_CALL_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 direct/generic dispatch artifact changed"; fail=1
        LANG1_CALL_READY=0
    fi
    LANG1_CALL_HOST_READY=0
    if [ "$LANG1_CALL_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_CALL_OBJ" -framework IOKit \
                -o "$AGX_LANG1_CALL_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_CALL_EXE" \
                >"$TMP/agx_lang1_call.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_CALL_EXE" \
                >"$TMP/agx_lang1_call.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_CALL_EXE" \
                >"$TMP/agx_lang1_call.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_call.eval.bin" \
                "$TMP/agx_lang1_call.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_call.eval.bin" \
                "$TMP/agx_lang1_call.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_call.eval.bin" \
                "$TMP/agx_lang1_call.t32.bin"; then
            echo "  FAIL: AGX LANG1 call CPU dispatch differs at T=1/4/32"
            fail=1; LANG1_CALL_READY=0
        else
            LANG1_CALL_HOST_READY=1
            echo "  ok   AGX LANG1 direct/generic calls agree at T=1/4/32"
        fi
    fi
    LANG1_OPERATOR_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_operator.tv" \
            -o "$AGX_LANG1_OPERATOR_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-operator-artifact "$AGX_LANG1_OPERATOR_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_operator.tv" \
            -o "$TMP/agx_lang1_operator.ra.hex" \
            2>"$TMP/agx_lang1_operator.ra.report" \
       || ! cmp -s "$AGX_LANG1_OPERATOR_DEV" \
            "$TMP/agx_lang1_operator.ra.hex" \
       || ! grep -qF 'agx-ra0: shape=6 blocks=3 iterations=2 copies=2 repairs=0 slot-regs=6 live-regs=3 vm-regs=0 pairs=0 loads=2 outcome=1' \
            "$TMP/agx_lang1_operator.ra.report"; then
        echo "  FAIL: AGX LANG1 trait/operator artifact changed"; fail=1
        LANG1_OPERATOR_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_operator_eval.tv" \
            >"$TMP/agx_lang1_operator.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_operator_eval.tv" \
            -o "$AGX_LANG1_OPERATOR_EVAL_LL" 2>/dev/null \
       || [ "$(grep -c 'call void @__parallel_for(' \
                    "$AGX_LANG1_OPERATOR_EVAL_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_OPERATOR_EVAL_LL" \
            -o "$AGX_LANG1_OPERATOR_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_OPERATOR_EVAL_OBJ" \
            -o "$AGX_LANG1_OPERATOR_EVAL_EXE" 2>/dev/null \
       || ! env TRAVELER_THREADS=4 "$AGX_LANG1_OPERATOR_EVAL_EXE" \
            >"$TMP/agx_lang1_operator.cpu.bin" \
       || ! cmp -s "$TMP/agx_lang1_operator.eval.bin" \
            "$TMP/agx_lang1_operator.cpu.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-operator-output "$TMP/agx_lang1_operator.eval.bin"; then
        echo "  FAIL: AGX LANG1 trait/operator oracle changed"; fail=1
        LANG1_OPERATOR_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_operator_dispatch.tv" \
            -o "$AGX_LANG1_OPERATOR_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                    "$AGX_LANG1_OPERATOR_LL" || true)" -ne 1 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                    "$AGX_LANG1_OPERATOR_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_OPERATOR_LL" \
            -o "$AGX_LANG1_OPERATOR_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 trait/operator dispatch artifact changed"; fail=1
        LANG1_OPERATOR_READY=0
    fi
    LANG1_OPERATOR_HOST_READY=0
    if [ "$LANG1_OPERATOR_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_OPERATOR_OBJ" -framework IOKit \
                -o "$AGX_LANG1_OPERATOR_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_OPERATOR_EXE" \
                >"$TMP/agx_lang1_operator.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_OPERATOR_EXE" \
                >"$TMP/agx_lang1_operator.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_OPERATOR_EXE" \
                >"$TMP/agx_lang1_operator.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_operator.eval.bin" \
                "$TMP/agx_lang1_operator.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_operator.eval.bin" \
                "$TMP/agx_lang1_operator.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_operator.eval.bin" \
                "$TMP/agx_lang1_operator.t32.bin"; then
            echo "  FAIL: AGX LANG1 operator CPU dispatch differs at T=1/4/32"
            fail=1; LANG1_OPERATOR_READY=0
        else
            LANG1_OPERATOR_HOST_READY=1
            echo "  ok   AGX LANG1 trait/operator calls agree at T=1/4/32"
        fi
    fi
    LANG1_CLOSURE_READY=1
    if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_closure.tv" \
            -o "$AGX_LANG1_CLOSURE_DEV" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-closure-artifact "$AGX_LANG1_CLOSURE_DEV" \
       || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
            "$SCRIPT_DIR/agx_lang1_closure.tv" \
            -o "$TMP/agx_lang1_closure.ra.hex" \
            2>"$TMP/agx_lang1_closure.ra.report" \
        || ! cmp -s "$AGX_LANG1_CLOSURE_DEV" \
             "$TMP/agx_lang1_closure.ra.hex" \
        || ! grep -qF 'agx-ra0: shape=6 blocks=1 iterations=2 copies=0 repairs=0 slot-regs=5 live-regs=1 vm-regs=0 pairs=0 loads=2 outcome=1' \
             "$TMP/agx_lang1_closure.ra.report"; then
        echo "  FAIL: AGX LANG1 closure artifact changed"; fail=1
        LANG1_CLOSURE_READY=0
    fi
    if ! "$STAGE1" --eval "$SCRIPT_DIR/agx_lang1_closure_eval.tv" \
            >"$TMP/agx_lang1_closure.eval.bin" 2>/dev/null \
       || ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_closure_eval.tv" \
            -o "$AGX_LANG1_CLOSURE_EVAL_LL" 2>/dev/null \
       || [ "$(grep -c 'call void @__parallel_for(' \
                    "$AGX_LANG1_CLOSURE_EVAL_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_CLOSURE_EVAL_LL" \
            -o "$AGX_LANG1_CLOSURE_EVAL_OBJ" 2>/dev/null \
       || ! "$LINKER" $HOST_LINK_PIE "$AGX_LANG1_CLOSURE_EVAL_OBJ" \
            -o "$AGX_LANG1_CLOSURE_EVAL_EXE" 2>/dev/null \
       || ! env TRAVELER_THREADS=4 "$AGX_LANG1_CLOSURE_EVAL_EXE" \
            >"$TMP/agx_lang1_closure.cpu.bin" \
       || ! cmp -s "$TMP/agx_lang1_closure.eval.bin" \
            "$TMP/agx_lang1_closure.cpu.bin" \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-closure-output "$TMP/agx_lang1_closure.eval.bin"; then
        echo "  FAIL: AGX LANG1 closure oracle changed"; fail=1
        LANG1_CLOSURE_READY=0
    fi
    if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/agx_lang1_closure_dispatch.tv" \
            -o "$AGX_LANG1_CLOSURE_LL" 2>/dev/null \
       || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                    "$AGX_LANG1_CLOSURE_LL" || true)" -ne 1 ] \
       || [ "$(grep -c '^define internal void @__pfor_worker_' \
                    "$AGX_LANG1_CLOSURE_LL" || true)" -ne 1 ] \
       || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$AGX_LANG1_CLOSURE_LL" \
            -o "$AGX_LANG1_CLOSURE_OBJ" 2>/dev/null; then
        echo "  FAIL: AGX LANG1 closure dispatch artifact changed"; fail=1
        LANG1_CLOSURE_READY=0
    fi
    LANG1_CLOSURE_HOST_READY=0
    if [ "$LANG1_CLOSURE_READY" = "1" ] && [ "$(uname -s)" = "Darwin" ] \
       && [ "$(uname -m)" = "arm64" ]; then
        if ! "$LINKER" "$AGX_LANG1_CLOSURE_OBJ" -framework IOKit \
                -o "$AGX_LANG1_CLOSURE_EXE" 2>/dev/null \
           || ! env TRAVELER_THREADS=1 "$AGX_LANG1_CLOSURE_EXE" \
                >"$TMP/agx_lang1_closure.t1.bin" \
           || ! env TRAVELER_THREADS=4 "$AGX_LANG1_CLOSURE_EXE" \
                >"$TMP/agx_lang1_closure.t4.bin" \
           || ! env TRAVELER_THREADS=32 "$AGX_LANG1_CLOSURE_EXE" \
                >"$TMP/agx_lang1_closure.t32.bin" \
           || ! cmp -s "$TMP/agx_lang1_closure.eval.bin" \
                "$TMP/agx_lang1_closure.t1.bin" \
           || ! cmp -s "$TMP/agx_lang1_closure.eval.bin" \
                "$TMP/agx_lang1_closure.t4.bin" \
           || ! cmp -s "$TMP/agx_lang1_closure.eval.bin" \
                "$TMP/agx_lang1_closure.t32.bin"; then
            echo "  FAIL: AGX LANG1 closure CPU dispatch differs at T=1/4/32"
            fail=1; LANG1_CLOSURE_READY=0
        else
            LANG1_CLOSURE_HOST_READY=1
            echo "  ok   AGX LANG1 closures agree at T=1/4/32"
        fi
    fi

    LANG1_C1_READY=1
    LANG1_C1_HOST_READY=1
    run_lang1_c1_gate() {
        local label="$1" source="$2" eval_source="$3" dispatch_source="$4"
        local artifact_check="$5" output_check="$6" ra_line="$7"
        local normal_pfor="${8:-1}"
        local dev="$TMP/agx_lang1_${label}.agx.hex"
        local shadow="$TMP/agx_lang1_${label}.shadow.hex"
        local report="$TMP/agx_lang1_${label}.ra.report"
        local eval_bin="$TMP/agx_lang1_${label}.eval.bin"
        local eval_ll="$TMP/agx_lang1_${label}.eval.ll"
        local eval_obj="$TMP/agx_lang1_${label}.eval.o"
        local eval_exe="$TMP/agx-lang1-${label}-eval"
        local dispatch_ll="$TMP/agx_lang1_${label}.ll"
        local dispatch_obj="$TMP/agx_lang1_${label}.o"
        local dispatch_exe="$TMP/agx-lang1-${label}"
        if ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/$source" \
                -o "$dev" 2>/dev/null \
           || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                "$artifact_check" "$dev" \
           || ! "$STAGE1" --emit-gpu-agx --agx-ra0-shadow \
                "$SCRIPT_DIR/$source" -o "$shadow" 2>"$report" \
           || ! cmp -s "$dev" "$shadow" \
           || ! grep -qF "$ra_line" "$report"; then
            echo "  FAIL: AGX LANG1-C1 $label artifact/RA changed"
            fail=1; LANG1_C1_READY=0; LANG1_C1_HOST_READY=0; return
        fi
        if ! "$STAGE1" --eval "$SCRIPT_DIR/$eval_source" \
                >"$eval_bin" 2>/dev/null \
           || ! "$STAGE1" "$SCRIPT_DIR/$eval_source" -o "$eval_ll" 2>/dev/null \
           || [ "$(grep -c 'call void @__parallel_for(' "$eval_ll" || true)" \
                -ne "$normal_pfor" ] \
           || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$eval_ll" \
                -o "$eval_obj" 2>/dev/null \
           || ! "$LINKER" $HOST_LINK_PIE "$eval_obj" -o "$eval_exe" 2>/dev/null \
           || ! env TRAVELER_THREADS=4 "$eval_exe" \
                >"$TMP/agx_lang1_${label}.cpu.bin" \
           || ! cmp -s "$eval_bin" "$TMP/agx_lang1_${label}.cpu.bin" \
           || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                "$output_check" "$eval_bin"; then
            echo "  FAIL: AGX LANG1-C1 $label eval/CPU oracle changed"
            fail=1; LANG1_C1_READY=0; LANG1_C1_HOST_READY=0; return
        fi
        if ! "$STAGE1" --agx-dispatch "$SCRIPT_DIR/$dispatch_source" \
                -o "$dispatch_ll" 2>/dev/null \
           || [ "$(grep -c 'call i32 @agx_try_parallel_for' \
                    "$dispatch_ll" || true)" -ne 1 ] \
           || [ "$(grep -c '^define internal void @__pfor_worker_' \
                    "$dispatch_ll" || true)" -ne 1 ] \
           || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$dispatch_ll" \
                -o "$dispatch_obj" 2>/dev/null; then
            echo "  FAIL: AGX LANG1-C1 $label dispatch artifact changed"
            fail=1; LANG1_C1_READY=0; LANG1_C1_HOST_READY=0; return
        fi
        if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
            if ! "$LINKER" "$dispatch_obj" -framework IOKit -o "$dispatch_exe" 2>/dev/null \
               || ! env TRAVELER_THREADS=1 "$dispatch_exe" \
                    >"$TMP/agx_lang1_${label}.t1.bin" \
               || ! env TRAVELER_THREADS=4 "$dispatch_exe" \
                    >"$TMP/agx_lang1_${label}.t4.bin" \
               || ! env TRAVELER_THREADS=32 "$dispatch_exe" \
                    >"$TMP/agx_lang1_${label}.t32.bin" \
               || ! cmp -s "$eval_bin" "$TMP/agx_lang1_${label}.t1.bin" \
               || ! cmp -s "$eval_bin" "$TMP/agx_lang1_${label}.t4.bin" \
               || ! cmp -s "$eval_bin" "$TMP/agx_lang1_${label}.t32.bin"; then
                echo "  FAIL: AGX LANG1-C1 $label differs at T=1/4/32"
                fail=1; LANG1_C1_READY=0; LANG1_C1_HOST_READY=0; return
            fi
        else
            LANG1_C1_HOST_READY=0
        fi
        echo "  ok   AGX LANG1-C1 $label artifact and CPU differential are pinned"
    }
    run_lang1_c1_gate c1 agx_lang1_c1.tv agx_lang1_c1_eval.tv \
        agx_lang1_c1_dispatch.tv --check-c1-artifact --check-c1-output \
        'agx-ra0: shape=6 blocks=12 iterations=3 copies=8 repairs=0 slot-regs=31 live-regs=4 vm-regs=0 pairs=0 loads=2 outcome=1'
    run_lang1_c1_gate c1_operator agx_lang1_c1_operator.tv \
        agx_lang1_c1_operator_eval.tv agx_lang1_c1_operator_dispatch.tv \
        --check-c1-operator-artifact --check-c1-operator-output \
        'agx-ra0: shape=6 blocks=9 iterations=2 copies=6 repairs=0 slot-regs=12 live-regs=5 vm-regs=0 pairs=0 loads=2 outcome=1'
    run_lang1_c1_gate c1_closure agx_lang1_c1_closure.tv \
        agx_lang1_c1_closure_eval.tv agx_lang1_c1_closure_dispatch.tv \
        --check-c1-closure-artifact --check-c1-closure-output \
        'agx-ra0: shape=6 blocks=4 iterations=2 copies=2 repairs=0 slot-regs=7 live-regs=3 vm-regs=0 pairs=0 loads=2 outcome=1'
    run_lang1_c1_gate c1_harden agx_lang1_c1_harden.tv \
        agx_lang1_c1_harden_eval.tv agx_lang1_c1_harden_dispatch.tv \
        --check-c1-harden-artifact --check-c1-harden-output \
        'agx-ra0: shape=6 blocks=20 iterations=4 copies=14 repairs=0 slot-regs=19 live-regs=4 vm-regs=0 pairs=0 loads=5 outcome=1'
    run_lang1_c1_gate dynamic_array agx_lang1_dynamic_array.tv \
        agx_lang1_dynamic_array_eval.tv agx_lang1_dynamic_array_dispatch.tv \
        --check-dynamic-array-artifact --check-dynamic-array-output \
        'agx-ra0: shape=6 blocks=40 iterations=2 copies=42 repairs=0 slot-regs=25 live-regs=11 vm-regs=0 pairs=0 loads=2 outcome=1' 0
    if ! "$STAGE1" --pfor-proof0-report \
            "$SCRIPT_DIR/agx_lang1_i1_matrix.tv" \
            >"$TMP/agx_lang1_i1_matrix.jsonl" 2>/dev/null \
       || [ "$(grep -c '"lang1_candidate":1' \
                    "$TMP/agx_lang1_i1_matrix.jsonl" || true)" -ne 4 ] \
       || ! "$STAGE1" --emit-gpu-agx \
            "$SCRIPT_DIR/agx_lang1_i1_matrix.tv" \
            -o "$TMP/agx_lang1_i1_matrix.hex" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$TMP/agx_lang1_i1_matrix.hex" \
       || [ "$(grep -c '^skip __pfor_gpu_worker_' \
                    "$TMP/agx_lang1_i1_matrix.hex" || true)" -ne 4 ]; then
        echo "  FAIL: AGX LANG1-I1 element/length claim matrix changed"
        fail=1; LANG1_C1_READY=0
    else
        echo "  ok   AGX LANG1-I1 bool/u32/field and length-16 boundaries fail closed"
    fi
    if ! "$STAGE1" "$SCRIPT_DIR/agx_lang1_c1_refuse.tv" \
            --pfor-proof0-report >"$TMP/agx_lang1_c1_refuse.jsonl" 2>/dev/null \
       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --check-c1-refusals "$TMP/agx_lang1_c1_refuse.jsonl" \
       || ! "$STAGE1" --emit-gpu-agx "$SCRIPT_DIR/agx_lang1_c1_refuse.tv" \
            -o "$TMP/agx_lang1_c1_refuse.hex" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$TMP/agx_lang1_c1_refuse.hex"; then
        echo "  FAIL: AGX LANG1-C1 negative catalogue changed"
        fail=1; LANG1_C1_READY=0
    else
        echo "  ok   AGX LANG1-C1 negative catalogue remains CPU-only"
    fi
    if ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
            --write-c1-hardening-refusals "$TMP/agx_lang1_c1_limits.tv" \
       || ! "$STAGE1" --emit-gpu-agx "$TMP/agx_lang1_c1_limits.tv" \
            -o "$TMP/agx_lang1_c1_limits.hex" 2>/dev/null \
       || grep -q '^worker __pfor_gpu_worker_' "$TMP/agx_lang1_c1_limits.hex" \
       || [ "$(grep -c '^skip __pfor_gpu_worker_' \
                    "$TMP/agx_lang1_c1_limits.hex" || true)" -ne 3 ]; then
        echo "  FAIL: AGX LANG1 depth/capacity fences changed"
        fail=1; LANG1_C1_READY=0
    else
        echo "  ok   AGX LANG1 call depth and C1/index capacity fences fail closed"
    fi
    if ! python3 "$SCRIPT_DIR/agx_control_probe.py" --check-only; then
        echo "  FAIL: AGX control specimens failed their portable structure gate"
        fail=1
    fi
    if ! python3 "$SCRIPT_DIR/agx_cf0_probe.py" --check-only; then
        echo "  FAIL: AGX CF0 compare/select encoder failed its portable gate"
        fail=1
    fi
    if ! python3 "$SCRIPT_DIR/agx_cf1_probe.py" --check-only; then
        echo "  FAIL: AGX CF1 structured mask encoder failed its portable gate"
        fail=1
    fi
    if ! python3 "$SCRIPT_DIR/agx_cf2_probe.py" --check-only; then
        echo "  FAIL: AGX CF2 loop and exit encoder failed its portable gate"
        fail=1
    fi
    if ! python3 "$SCRIPT_DIR/agx_counted_dot_probe.py" --check-only; then
        echo "  FAIL: AGX counted-dot specimen failed its portable structure gate"
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
           && "$LINKER" "$AGX_RUNTIME_OBJ" -framework IOKit -o "$AGX_RUNTIME_EXE" 2>/dev/null; then
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
        if python3 "$SCRIPT_DIR/agx_cf0_probe.py" "$AGX_HARNESS"; then
            echo "  ok   G16X CF0 authored compare/select fields exact"
        else
            echo "  FAIL: G16X CF0 compare/select execution mismatch"; fail=1
        fi
        if python3 "$SCRIPT_DIR/agx_cf1_probe.py" "$AGX_HARNESS"; then
            echo "  ok   G16X CF1 authored structured mask stack exact"
        else
            echo "  FAIL: G16X CF1 structured mask execution mismatch"; fail=1
        fi
        if python3 "$SCRIPT_DIR/agx_cf2_probe.py" "$AGX_HARNESS"; then
            echo "  ok   G16X CF2 authored loops and local exits exact"
        else
            echo "  FAIL: G16X CF2 loop and exit execution mismatch"; fail=1
        fi
        if python3 "$SCRIPT_DIR/agx_counted_dot_probe.py" "$AGX_HARNESS"; then
            echo "  ok   G16X fixed-K counted Montgomery dots exact"
        else
            echo "  FAIL: G16X counted Montgomery dot mismatch"; fail=1
        fi
        if [ "$LANG0_READY" = "1" ]; then
            lang0fail=0
            if [ "$LANG0_HOST_READY" != "1" ]; then
                echo "  FAIL: AGX LANG0 runtime differential is not host-ready"
                lang0fail=1
            elif ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG0 pre-canary failed"
                lang0fail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG0_DEV" \
                    "$AGX_LANG0_EXE" >"$TMP/agx_lang0.gpu.bin"
                lang0_gpu_status=$?
                if [ "$lang0_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang0.gpu.bin" | tr -d ' ')" -ne 12288 ] \
                   || ! cmp -s "$TMP/agx_lang0.cpu.bin" \
                        "$TMP/agx_lang0.gpu.bin" \
                   || ! cmp -s "$TMP/agx_lang0.eval.bin" \
                        "$TMP/agx_lang0.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang0_probe.py" \
                        --check-output "$TMP/agx_lang0.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG0 eval/CPU/G16X differential or canary failed"
                    lang0fail=1
                fi
            fi
            if [ "$lang0fail" = "0" ]; then
                echo "  ok   AGX LANG0 eval, CPU pfor, G16X, and independent oracle are byte-exact"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_STRUCT_READY" = "1" ]; then
            lang1fail=0
            if [ "$LANG1_STRUCT_HOST_READY" != "1" ]; then
                echo "  FAIL: AGX LANG1 struct runtime differential is not host-ready"
                lang1fail=1
            elif ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 struct pre-canary failed"
                lang1fail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_STRUCT_DEV" \
                    "$AGX_LANG1_STRUCT_EXE" >"$TMP/agx_lang1_struct.gpu.bin"
                lang1_gpu_status=$?
                if [ "$lang1_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_struct.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_lang1_struct.eval.bin" \
                        "$TMP/agx_lang1_struct.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-output "$TMP/agx_lang1_struct.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 struct eval/CPU/G16X differential or canary failed"
                    lang1fail=1
                fi
            fi
            if [ "$lang1fail" = "0" ]; then
                echo "  ok   AGX LANG1 struct eval, recursive CPU fallback, G16X, and oracle are byte-exact"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_MATCH_READY" = "1" ]; then
            lang1matchfail=0
            if [ "$LANG1_MATCH_HOST_READY" != "1" ]; then
                echo "  FAIL: AGX LANG1 match runtime differential is not host-ready"
                lang1matchfail=1
            elif ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 match pre-canary failed"
                lang1matchfail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_MATCH_DEV" \
                    "$AGX_LANG1_MATCH_EXE" >"$TMP/agx_lang1_match.gpu.bin"
                lang1_match_gpu_status=$?
                if [ "$lang1_match_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_match.gpu.bin" | tr -d ' ')" -ne 6144 ] \
                   || ! cmp -s "$TMP/agx_lang1_match.eval.bin" \
                        "$TMP/agx_lang1_match.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-match-output "$TMP/agx_lang1_match.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 match eval/CPU/G16X differential or canary failed"
                    lang1matchfail=1
                fi
            fi
            if [ "$lang1matchfail" = "0" ]; then
                echo "  ok   AGX LANG1 integer/enum match is exact on eval, CPU, G16X, and oracle"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_ARRAY_READY" = "1" ]; then
            lang1arrayfail=0
            if [ "$LANG1_ARRAY_HOST_READY" != "1" ]; then
                echo "  FAIL: AGX LANG1 array runtime differential is not host-ready"
                lang1arrayfail=1
            elif ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 array pre-canary failed"
                lang1arrayfail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_ARRAY_DEV" \
                    "$AGX_LANG1_ARRAY_EXE" >"$TMP/agx_lang1_array.gpu.bin"
                lang1_array_gpu_status=$?
                if [ "$lang1_array_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_array.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_lang1_array.eval.bin" \
                        "$TMP/agx_lang1_array.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-array-output "$TMP/agx_lang1_array.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 array eval/CPU/G16X differential or canary failed"
                    lang1arrayfail=1
                fi
            fi
            if [ "$lang1arrayfail" = "0" ]; then
                echo "  ok   AGX LANG1 fixed array is exact on eval, CPU, G16X, and oracle"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_CALL_READY" = "1" ]; then
            lang1callfail=0
            if [ "$LANG1_CALL_HOST_READY" != "1" ] \
               || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 call runtime differential is not host-ready"
                lang1callfail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_CALL_DEV" \
                    "$AGX_LANG1_CALL_EXE" >"$TMP/agx_lang1_call.gpu.bin"
                lang1_call_gpu_status=$?
                if [ "$lang1_call_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_call.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_lang1_call.eval.bin" \
                        "$TMP/agx_lang1_call.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-call-output "$TMP/agx_lang1_call.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 call eval/CPU/G16X differential failed"
                    lang1callfail=1
                fi
            fi
            if [ "$lang1callfail" = "0" ]; then
                echo "  ok   AGX LANG1 direct/generic calls are exact on eval, CPU, and G16X"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_OPERATOR_READY" = "1" ]; then
            lang1operatorfail=0
            if [ "$LANG1_OPERATOR_HOST_READY" != "1" ] \
               || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 operator runtime differential is not host-ready"
                lang1operatorfail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_OPERATOR_DEV" \
                    "$AGX_LANG1_OPERATOR_EXE" >"$TMP/agx_lang1_operator.gpu.bin"
                lang1_operator_gpu_status=$?
                if [ "$lang1_operator_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_operator.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_lang1_operator.eval.bin" \
                        "$TMP/agx_lang1_operator.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-operator-output "$TMP/agx_lang1_operator.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 operator eval/CPU/G16X differential failed"
                    lang1operatorfail=1
                fi
            fi
            if [ "$lang1operatorfail" = "0" ]; then
                echo "  ok   AGX LANG1 trait/operator calls are exact on eval, CPU, and G16X"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_CLOSURE_READY" = "1" ]; then
            lang1closurefail=0
            if [ "$LANG1_CLOSURE_HOST_READY" != "1" ] \
               || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                    "$AGX_HARNESS" >/dev/null; then
                echo "  FAIL: AGX LANG1 closure runtime differential is not host-ready"
                lang1closurefail=1
            else
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_LANG1_CLOSURE_DEV" \
                    "$AGX_LANG1_CLOSURE_EXE" >"$TMP/agx_lang1_closure.gpu.bin"
                lang1_closure_gpu_status=$?
                if [ "$lang1_closure_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_lang1_closure.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                   || ! cmp -s "$TMP/agx_lang1_closure.eval.bin" \
                        "$TMP/agx_lang1_closure.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                        --check-closure-output "$TMP/agx_lang1_closure.gpu.bin" \
                   || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    echo "  FAIL: AGX LANG1 closure eval/CPU/G16X differential failed"
                    lang1closurefail=1
                fi
            fi
            if [ "$lang1closurefail" = "0" ]; then
                echo "  ok   AGX LANG1 closures are exact on eval, CPU, and G16X"
            else
                fail=1
            fi
        fi
        if [ "$LANG1_C1_READY" = "1" ]; then
            lang1c1fail=0
            if [ "$LANG1_C1_HOST_READY" != "1" ]; then
                echo "  FAIL: AGX LANG1-C1 runtime differential is not host-ready"
                lang1c1fail=1
            fi
            for c1_label in c1 c1_operator c1_closure c1_harden dynamic_array; do
                case "$c1_label" in
                    c1) c1_output_check=--check-c1-output ;;
                    c1_operator) c1_output_check=--check-c1-operator-output ;;
                    c1_closure) c1_output_check=--check-c1-closure-output ;;
                    c1_harden) c1_output_check=--check-c1-harden-output ;;
                    *) c1_output_check=--check-dynamic-array-output ;;
                esac
                if [ "$lang1c1fail" = "0" ] \
                   && python3 "$SCRIPT_DIR/agx_control_probe.py" \
                        "$AGX_HARNESS" >/dev/null; then
                    TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                    TRAVELER_AGX_ARTIFACT="$TMP/agx_lang1_${c1_label}.agx.hex" \
                        "$TMP/agx-lang1-${c1_label}" \
                        >"$TMP/agx_lang1_${c1_label}.gpu.bin"
                    c1_status=$?
                    if [ "$c1_status" -ne 0 ] \
                       || [ "$(wc -c <"$TMP/agx_lang1_${c1_label}.gpu.bin" | tr -d ' ')" -ne 3072 ] \
                       || ! cmp -s "$TMP/agx_lang1_${c1_label}.eval.bin" \
                            "$TMP/agx_lang1_${c1_label}.gpu.bin" \
                       || ! python3 "$SCRIPT_DIR/agx_lang1_probe.py" \
                            "$c1_output_check" \
                            "$TMP/agx_lang1_${c1_label}.gpu.bin" \
                       || ! python3 "$SCRIPT_DIR/agx_control_probe.py" \
                            "$AGX_HARNESS" >/dev/null; then
                        echo "  FAIL: AGX LANG1-C1 $c1_label eval/CPU/G16X differential failed"
                        lang1c1fail=1
                    fi
                else
                    lang1c1fail=1
                fi
            done
            if [ "$lang1c1fail" = "0" ]; then
                echo "  ok   AGX LANG1-C1 hardening and dynamic private indices are exact"
            else
                fail=1
            fi
        fi
        if [ "$AGX3_READY" = "1" ]; then
            agx3fail=0
            if ! "$LINKER" "$AGX_BINARY_OBJ" -framework IOKit -o "$AGX_BINARY_EXE" 2>/dev/null \
               || [ "$("$AGX_BINARY_EXE" "$AGX_HARNESS/dispatch.img" \
                    "$AGX_BINARY_DEV")" != "1" ]; then
                echo "  FAIL: packed two-input AGX execution mismatch"
                agx3fail=1
            fi
            if ! "$LINKER" "$AGX_DISPATCH_OBJ" -framework IOKit -o "$AGX_DISPATCH_EXE" 2>/dev/null; then
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
            if ! "$LINKER" "$AGX_RNS_OBJ" -framework IOKit -o "$AGX_RNS_EXE" 2>/dev/null; then
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
            if ! "$LINKER" "$AGX_REDUCE_OBJ" -framework IOKit -o "$AGX_REDUCE_EXE" 2>/dev/null; then
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
            if ! "$LINKER" "$AGX_DOT_OBJ" -framework IOKit -o "$AGX_DOT_EXE" 2>/dev/null; then
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
            if ! "$LINKER" "$AGX_DOT_LOOP_OBJ" -framework IOKit -o "$AGX_DOT_LOOP_EXE" 2>/dev/null; then
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
            if ! "$LINKER" "$AGX_DOT_GENERAL_OBJ" -framework IOKit \
                    -o "$AGX_DOT_GENERAL_EXE" 2>/dev/null; then
                echo "  FAIL: generalized 1x8x1024 counted dot did not link"
                agx3fail=1
            else
                (unset TRAVELER_AGX_PROFILE TRAVELER_AGX_ARTIFACT
                 TRAVELER_THREADS=4 "$AGX_DOT_GENERAL_EXE" \
                    >"$TMP/agx_dot_general.cpu.bin")
                dot_general_cpu_status=$?
                TRAVELER_AGX_PROFILE="$AGX_HARNESS/dispatch.img" \
                TRAVELER_AGX_ARTIFACT="$AGX_DOT_GENERAL_DEV" \
                    "$AGX_DOT_GENERAL_EXE" >"$TMP/agx_dot_general.gpu.bin"
                dot_general_gpu_status=$?
                if [ "$dot_general_cpu_status" -ne 0 ] \
                   || [ "$dot_general_gpu_status" -ne 0 ] \
                   || [ "$(wc -c <"$TMP/agx_dot_general.cpu.bin" | tr -d ' ')" -ne 8192 ] \
                   || ! cmp -s "$TMP/agx_dot_general.cpu.bin" \
                        "$TMP/agx_dot_general.gpu.bin"; then
                    echo "  FAIL: generalized 1x8x1024 counted dot differs from CPU"
                    agx3fail=1
                fi
            fi
            if [ "$agx3fail" = "0" ]; then
                echo "  ok   AGX RNS product, reduce-8, and generalized K=8 dot paths are exact"
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
                if ! "$LINKER" "$TMP/$det.o" -framework IOKit -o "$TMP/$det" 2>/dev/null; then
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
K8_SRC="$REPO_DIR/tests/gpu/gpu_k8_private_dot.tv"
PRIVATE_REFUSE_SRC="$REPO_DIR/tests/gpu/gpu_private_refuse.tv"
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

# A5. The proved private K=8 dot lowers as unrolled SSA: no alloca and a
# production-valid gfx1100 object.
K8_DEV="$TMP/gpu_k8_private_dot_amd.ll"
K8_OBJ="$TMP/gpu_k8_private_dot_amd.o"
if ! "$STAGE1" --emit-gpu "$K8_SRC" -o "$K8_DEV" 2>/dev/null; then
    echo "  FAIL: AMD private K=8 dot did not produce a module"; fail=1
else
    grep -q "define amdgpu_kernel" "$K8_DEV" \
        || { echo "  FAIL: AMD private K=8 dot emitted no kernel"; fail=1; }
    if grep -q "alloca" "$K8_DEV"; then
        echo "  FAIL: AMD private K=8 dot spilled through alloca"; fail=1
    fi
    k8mul=$(grep -c " = mul i64 " "$K8_DEV" || true)
    [ "$k8mul" -ge 8 ] \
        || { echo "  FAIL: AMD private K=8 dot was not fully lowered"; fail=1; }
    if ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
            "$K8_DEV" -o "$K8_OBJ" 2>"$TMP/k8-llc.err"; then
        echo "  FAIL: AMD private K=8 dot did not lower for gfx1100"; fail=1
    else
        echo "  ok   AMD private K=8 dot: proved, alloca-free, gfx1100-lowered"
    fi
fi

# A5b. The dot must stay a mul/add chain in silicon. gfx1100 dot4 is
# unsigned on both operands. @internal-note: known-issues #7a.
if [ "$fail" = "0" ] && [ -x "$OBJDUMP" ] && [ -f "$K8_OBJ" ]; then
    k8dis="$("$OBJDUMP" -d "$K8_OBJ" 2>/dev/null)"
    if echo "$k8dis" | grep -qE "v_dot[48]_"; then
        echo "  FAIL: gfx1100 object contains a v_dot4/v_dot8 -- unsigned on"
        echo "        gfx11; exactness needs the x ^ 0x80 bias re-proven first"
        fail=1
    else
        echo "  ok   gfx1100 K=8 dot lowers as plain mul/add (no v_dot[48])"
    fi
fi

# A8. Stage-1b wave map: the K=32 loop lowers across the wave with a
#     ds.swizzle xor butterfly (pattern (off<<10)|0x1F — gfx11 SWAP; the
#     gfx9 0xFC00|off encoding is FFT bit-rotation on gfx11, probed).
WAVE_SRC="$SCRIPT_DIR/gpu_wave_dot.tv"
WAVE_DEV="$TMP/gpu_wave_dot_amd.ll"
WAVE_OBJ="$TMP/gpu_wave_dot_amd.o"
if ! "$STAGE1" --emit-gpu "$WAVE_SRC" -o "$WAVE_DEV" 2>/dev/null; then
    echo "  FAIL: AMD wave dot did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$WAVE_DEV"; then
    echo "  FAIL: AMD wave dot emitted no kernel"; fail=1
elif grep -q "alloca" "$WAVE_DEV"; then
    echo "  FAIL: AMD wave dot spilled through alloca"; fail=1
elif [ "$(grep -c 'llvm.amdgcn.ds.swizzle' "$WAVE_DEV")" -lt 6 ]; then
    echo "  FAIL: AMD wave dot lost its ds.swizzle butterfly"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$WAVE_DEV" -o "$WAVE_OBJ" 2>"$TMP/wave-llc.err"; then
    echo "  FAIL: AMD wave dot did not lower for gfx1100:"; \
        sed 's/^/       /' "$TMP/wave-llc.err"; fail=1
else
    wavedis=""
    if [ -x "$OBJDUMP" ]; then
        wavedis="$("$OBJDUMP" -d "$WAVE_OBJ" 2>/dev/null || true)"
    fi
    if [ -n "$wavedis" ] && ! echo "$wavedis" \
            | grep -q "ds_swizzle_b32.*SWAP"; then
        echo "  FAIL: AMD wave dot butterfly is not the gfx11 SWAP xor-permute"; \
            fail=1
    else
        echo "  ok   AMD Stage-1b wave map: ds.swizzle SWAP butterfly, gfx1100-lowered"
    fi
fi

# A9. The udot4 builtin lowers to a REAL v_dot4_u32_u8 (the deliberate,
#     honest unsigned spelling — A5b's mul/add fixture must still NEVER
#     form one accidentally).
UDOT_SRC="$SCRIPT_DIR/gpu_udot4.tv"
UDOT_DEV="$TMP/gpu_udot4_amd.ll"
UDOT_OBJ="$TMP/gpu_udot4_amd.o"
if ! "$STAGE1" --emit-gpu "$UDOT_SRC" -o "$UDOT_DEV" 2>/dev/null; then
    echo "  FAIL: AMD udot4 did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$UDOT_DEV"; then
    echo "  FAIL: AMD udot4 emitted no kernel"; fail=1
elif ! grep -q "call i32 @llvm.amdgcn.udot4" "$UDOT_DEV"; then
    echo "  FAIL: AMD udot4 did not emit the udot4 intrinsic"; fail=1
elif ! grep -q "declare i32 @llvm.amdgcn.udot4" "$UDOT_DEV"; then
    echo "  FAIL: AMD udot4 module lost the intrinsic declare"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$UDOT_DEV" -o "$UDOT_OBJ" 2>"$TMP/udot-llc.err"; then
    echo "  FAIL: AMD udot4 did not lower for gfx1100:"; \
        sed 's/^/       /' "$TMP/udot-llc.err"; fail=1
else
    udotdis=""
    if [ -x "$OBJDUMP" ]; then
        udotdis="$("$OBJDUMP" -d "$UDOT_OBJ" 2>/dev/null || true)"
    fi
    if [ -n "$udotdis" ] && ! echo "$udotdis" \
            | grep -q "v_dot4_u32_u8"; then
        echo "  FAIL: AMD udot4 builtin did not become v_dot4_u32_u8"; fail=1
    else
        echo "  ok   AMD udot4 builtin: v_dot4_u32_u8, gfx1100-lowered"
    fi
fi

# A10. Stage-1c per-row accumulators: R=2 phi-carried accumulators, R
#      wave reductions, R stores.
BATCH_SRC="$SCRIPT_DIR/gpu_batch_dot.tv"
BATCH_DEV="$TMP/gpu_batch_dot_amd.ll"
BATCH_OBJ="$TMP/gpu_batch_dot_amd.o"
if ! "$STAGE1" --emit-gpu "$BATCH_SRC" -o "$BATCH_DEV" 2>/dev/null; then
    echo "  FAIL: AMD batch dot did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$BATCH_DEV"; then
    echo "  FAIL: AMD batch dot emitted no kernel"; fail=1
elif grep -q "alloca" "$BATCH_DEV"; then
    echo "  FAIL: AMD batch dot spilled through alloca"; fail=1
elif [ "$(grep -c ' = phi i64 ' "$BATCH_DEV")" -lt 2 ]; then
    echo "  FAIL: AMD batch dot lost its per-row accumulator phis"; fail=1
elif [ "$(grep -c 'store i64' "$BATCH_DEV")" -lt 2 ]; then
    echo "  FAIL: AMD batch dot lost its per-row stores"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$BATCH_DEV" -o "$BATCH_OBJ" 2>"$TMP/batch-llc.err"; then
    echo "  FAIL: AMD batch dot did not lower for gfx1100:"; \
        sed 's/^/       /' "$TMP/batch-llc.err"; fail=1
else
    echo "  ok   AMD Stage-1c batch dot: per-row phis + stores, gfx1100-lowered"
fi
# A6. Other proved private-mutable bodies stay outside the closed device class.
PRIVATE_REFUSE_AMD="$TMP/gpu_private_refuse_amd.ll"
if ! "$STAGE1" --emit-gpu "$PRIVATE_REFUSE_SRC" \
        -o "$PRIVATE_REFUSE_AMD" 2>/dev/null; then
    echo "  FAIL: AMD private-mutable refusal did not produce a module"; fail=1
elif grep -q "define amdgpu_kernel" "$PRIVATE_REFUSE_AMD"; then
    echo "  FAIL: AMD admitted a general private-mutable body"; fail=1
elif ! grep -q "not the Stage-0 elementwise/private-K8 class" \
        "$PRIVATE_REFUSE_AMD"; then
    echo "  FAIL: AMD private-mutable refusal record absent"; fail=1
else
    echo "  ok   AMD general private-mutable body remains device-refused"
fi

# A7. Stage-1 blocked private dot: mapped multiplicands (dequant expressions
#     as dot operands), a rolled outer block loop with loop-carried SSA phis,
#     unrolled inner K-loops, no allocas. This is the fused Q4_K shape.
BLOCKED_SRC="$SCRIPT_DIR/gpu_blocked_dot.tv"
BLOCKED_DEV="$TMP/gpu_blocked_dot_amd.ll"
BLOCKED_OBJ="$TMP/gpu_blocked_dot_amd.o"
if ! "$STAGE1" --emit-gpu "$BLOCKED_SRC" -o "$BLOCKED_DEV" 2>/dev/null; then
    echo "  FAIL: AMD blocked dot did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$BLOCKED_DEV"; then
    echo "  FAIL: AMD blocked dot emitted no kernel"; fail=1
elif grep -q "alloca" "$BLOCKED_DEV"; then
    echo "  FAIL: AMD blocked dot spilled through alloca"; fail=1
elif ! grep -q " = phi i64 " "$BLOCKED_DEV"; then
    echo "  FAIL: AMD blocked dot lost its loop-carried accumulator phi"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$BLOCKED_DEV" -o "$BLOCKED_OBJ" 2>"$TMP/blocked-llc.err"; then
    echo "  FAIL: AMD blocked dot did not lower for gfx1100:"; \
        sed 's/^/       /' "$TMP/blocked-llc.err"; fail=1
elif [ -x "$OBJDUMP" ] && "$OBJDUMP" -d "$BLOCKED_OBJ" 2>/dev/null \
        | grep -qE "v_dot[48]_"; then
    echo "  FAIL: AMD blocked dot formed a v_dot4/v_dot8 (unsigned on gfx11)"; \
        fail=1
else
    echo "  ok   AMD Stage-1 blocked dot: mapped multiplicands, phi-carried, gfx1100-lowered"
fi

# A11. Capacity cliffs stay admitted: 51 lets / 26 captures emit a real
# kernel. The old limits refused silently. @internal-design: stage1-dot.
CLIFF_SRC="$SCRIPT_DIR/gpu_capacity_cliffs.tv"
CLIFF_DEV="$TMP/gpu_capacity_cliffs_amd.ll"
CLIFF_OBJ="$TMP/gpu_capacity_cliffs_amd.o"
if ! "$STAGE1" --emit-gpu "$CLIFF_SRC" -o "$CLIFF_DEV" 2>/dev/null; then
    echo "  FAIL: capacity-cliff fixture did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$CLIFF_DEV"; then
    echo "  FAIL: capacity-cliff fixture emitted no kernel (limit regressed)"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$CLIFF_DEV" -o "$CLIFF_OBJ" 2>"$TMP/cliff-llc.err"; then
    echo "  FAIL: capacity-cliff kernel did not lower for gfx1100"; fail=1
else
    echo "  ok   capacity cliffs admitted: 51 lets / 26 captures emit + lower"
fi

# A12. Carried-fetch prefetch: pre is phi-carried and the reassign is
# guarded on the final iteration. @internal-design: stage1-dot.
PF_SRC="$SCRIPT_DIR/gpu_prefetch_dot.tv"
PF_DEV="$TMP/gpu_prefetch_dot_amd.ll"
PF_OBJ="$TMP/gpu_prefetch_dot_amd.o"
if ! "$STAGE1" --emit-gpu "$PF_SRC" -o "$PF_DEV" 2>/dev/null; then
    echo "  FAIL: prefetch dot did not produce a module"; fail=1
elif ! grep -q "define amdgpu_kernel" "$PF_DEV"; then
    echo "  FAIL: prefetch dot emitted no kernel"; fail=1
elif grep -q "alloca" "$PF_DEV"; then
    echo "  FAIL: prefetch dot spilled through alloca"; fail=1
elif [ "$(grep -c ' = phi i64 ' "$PF_DEV")" -lt 3 ]; then
    echo "  FAIL: prefetch dot lost a carried phi (want pre + acc + join)"; fail=1
elif ! grep -q "icmp slt i32 .*, 3$" "$PF_DEV"; then
    echo "  FAIL: prefetch dot lost the last-iteration guard"; fail=1
elif ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
        "$PF_DEV" -o "$PF_OBJ" 2>"$TMP/pf-llc.err"; then
    echo "  FAIL: prefetch dot did not lower for gfx1100:"; \
        sed 's/^/       /' "$TMP/pf-llc.err"; fail=1
else
    echo "  ok   AMD carried-fetch prefetch: carried-fetch phi, guarded reassign, gfx1100-lowered"
fi

# A13. Prefetch negatives: a mixed-assign let and a stored carried fetch
# must stay off the device.
PFN_DEV="$TMP/gpu_prefetch_refuse_amd.ll"
if ! "$STAGE1" --emit-gpu "$SCRIPT_DIR/gpu_prefetch_refuse.tv" \
        -o "$PFN_DEV" 2>/dev/null; then
    echo "  FAIL: prefetch negative catalogue did not produce a module"; fail=1
elif grep -q "define amdgpu_kernel" "$PFN_DEV"; then
    echo "  FAIL: prefetch negative catalogue reached the device"; fail=1
else
    echo "  ok   prefetch negatives (mixed assign, stored prefetch) stay refused"
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

# N5. The same private reduction class stays target-neutral.
K8_NVDEV="$TMP/gpu_k8_private_dot_nv.ll"
K8_PTX="$TMP/gpu_k8_private_dot_nv.ptx"
if ! "$STAGE1" --emit-gpu-nvptx "$K8_SRC" -o "$K8_NVDEV" 2>/dev/null; then
    echo "  FAIL: NVPTX private K=8 dot did not produce a module"; fail=1
elif grep -q "alloca" "$K8_NVDEV"; then
    echo "  FAIL: NVPTX private K=8 dot spilled through alloca"; fail=1
elif ! "$LLC" -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90 \
        "$K8_NVDEV" -o "$K8_PTX" 2>"$TMP/k8-llcnv.err"; then
    echo "  FAIL: NVPTX private K=8 dot did not lower for sm_90"; fail=1
else
    echo "  ok   NVPTX private K=8 dot: proved, alloca-free, sm_90-lowered"
fi

PRIVATE_REFUSE_NV="$TMP/gpu_private_refuse_nv.ll"
if ! "$STAGE1" --emit-gpu-nvptx "$PRIVATE_REFUSE_SRC" \
        -o "$PRIVATE_REFUSE_NV" 2>/dev/null; then
    echo "  FAIL: NVPTX private-mutable refusal did not produce a module"; fail=1
elif grep -q "define ptx_kernel" "$PRIVATE_REFUSE_NV"; then
    echo "  FAIL: NVPTX admitted a general private-mutable body"; fail=1
elif ! grep -q "not the Stage-0 elementwise/private-K8 class" \
        "$PRIVATE_REFUSE_NV"; then
    echo "  FAIL: NVPTX private-mutable refusal record absent"; fail=1
else
    echo "  ok   NVPTX general private-mutable body remains device-refused"
fi

# N6. The Stage-1 blocked dot stays target-neutral.
BLOCKED_NVDEV="$TMP/gpu_blocked_dot_nv.ll"
BLOCKED_PTX="$TMP/gpu_blocked_dot_nv.ptx"
if ! "$STAGE1" --emit-gpu-nvptx "$SCRIPT_DIR/gpu_blocked_dot.tv" \
        -o "$BLOCKED_NVDEV" 2>/dev/null; then
    echo "  FAIL: NVPTX blocked dot did not produce a module"; fail=1
elif grep -q "alloca" "$BLOCKED_NVDEV"; then
    echo "  FAIL: NVPTX blocked dot spilled through alloca"; fail=1
elif ! grep -q " = phi i64 " "$BLOCKED_NVDEV"; then
    echo "  FAIL: NVPTX blocked dot lost its loop-carried accumulator phi"; fail=1
elif ! "$LLC" -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90 \
        "$BLOCKED_NVDEV" -o "$BLOCKED_PTX" 2>"$TMP/blocked-llcnv.err"; then
    echo "  FAIL: NVPTX blocked dot did not lower for sm_90"; fail=1
else
    echo "  ok   NVPTX Stage-1 blocked dot: mapped multiplicands, phi-carried, sm_90-lowered"
fi

# N8. The carried-fetch prefetch stays target-neutral.
PF_NVDEV="$TMP/gpu_prefetch_dot_nv.ll"
PF_PTX="$TMP/gpu_prefetch_dot_nv.ptx"
if ! "$STAGE1" --emit-gpu-nvptx "$SCRIPT_DIR/gpu_prefetch_dot.tv" \
        -o "$PF_NVDEV" 2>/dev/null; then
    echo "  FAIL: NVPTX prefetch dot did not produce a module"; fail=1
elif ! grep -q "icmp slt i32 .*, 3$" "$PF_NVDEV"; then
    echo "  FAIL: NVPTX prefetch dot lost the last-iteration guard"; fail=1
elif ! "$LLC" -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90 \
        "$PF_NVDEV" -o "$PF_PTX" 2>"$TMP/pf-llcnv.err"; then
    echo "  FAIL: NVPTX prefetch dot did not lower for sm_90"; fail=1
else
    echo "  ok   NVPTX carried-fetch prefetch: guarded carried-fetch, sm_90-lowered"
fi

# N7. The Stage-1b wave map lowers through shfl.sync.bfly on NVPTX.
WAVE_NVDEV="$TMP/gpu_wave_dot_nv.ll"
WAVE_PTX="$TMP/gpu_wave_dot_nv.ptx"
if ! "$STAGE1" --emit-gpu-nvptx "$SCRIPT_DIR/gpu_wave_dot.tv" \
        -o "$WAVE_NVDEV" 2>/dev/null; then
    echo "  FAIL: NVPTX wave dot did not produce a module"; fail=1
elif grep -q "alloca" "$WAVE_NVDEV"; then
    echo "  FAIL: NVPTX wave dot spilled through alloca"; fail=1
elif ! grep -q "llvm.nvvm.shfl.sync.i32" "$WAVE_NVDEV"; then
    echo "  FAIL: NVPTX wave dot lost its shfl.sync butterfly"; fail=1
elif ! "$LLC" -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90 \
        "$WAVE_NVDEV" -o "$WAVE_PTX" 2>"$TMP/wave-llcnv.err"; then
    echo "  FAIL: NVPTX wave dot did not lower for sm_90"; fail=1
elif ! grep -q "shfl.sync" "$WAVE_PTX"; then
    echo "  FAIL: NVPTX wave dot PTX lost the shfl.sync butterfly"; fail=1
else
    echo "  ok   NVPTX Stage-1b wave map: shfl.sync.bfly, sm_90-lowered"
fi

fi
else
    echo "  SKIP: no nvptx64 target in this llc (NVPTX leg)"
fi

# ========================== Vulkan/HIP runtime ownership =====================
echo "  -- Vulkan shader and Traveler-owned AMD runtimes"
VK_GATE_SRC="$SCRIPT_DIR/vulkan_runtime_gate.tv"
VK_SHADER="$TMP/vulkan_runtime_gate.comp"
VK_SPV="$TMP/vulkan_runtime_gate.spv"
VK_HOST_LL="$TMP/vulkan_runtime_gate.ll"
VK_HOST_OBJ="$TMP/vulkan_runtime_gate.o"
VK_HOST_EXE="$TMP/vulkan-runtime-gate"
VK_READY=1
if ! "$STAGE1" --emit-gpu-vulkan "$VK_GATE_SRC" -o "$VK_SHADER" 2>/dev/null \
   || ! grep -q '^#version 460$' "$VK_SHADER" \
   || ! grep -q 'binding = 0.*readonly buffer TvInput0' "$VK_SHADER" \
   || ! grep -q 'binding = 2.*writeonly buffer TvOutput' "$VK_SHADER" \
   || ! grep -q 'umulExtended' "$VK_SHADER"; then
    echo "  FAIL: closed Vulkan exact-map shader contract changed"; fail=1
    VK_READY=0
fi
if ! "$STAGE1" "$VK_GATE_SRC" -o "$VK_HOST_LL" 2>/dev/null \
   || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$VK_HOST_LL" \
        -o "$VK_HOST_OBJ" 2>/dev/null; then
    echo "  FAIL: Traveler-owned Vulkan runtime did not compile"; fail=1
    VK_READY=0
else
    echo "  ok   Traveler-owned Vulkan runtime compiles without project C"
fi
if [ "$VK_READY" = "1" ] && [ "$HAVE_GLSLANG" = "1" ]; then
    if ! "$GLSLANG_VALIDATOR" -V "$VK_SHADER" -o "$VK_SPV" >/dev/null 2>&1; then
        echo "  FAIL: Vulkan exact-map shader did not assemble to SPIR-V"; fail=1
        VK_READY=0
    else
        echo "  ok   closed Vulkan exact-map shader assembles to SPIR-V"
    fi
else
    echo "  SKIP: glslangValidator unavailable (SPIR-V assembly)"
fi
if [ "$VK_READY" = "1" ] && [ "$HAVE_VULKAN" = "1" ] \
   && [ "$HAVE_LINKER" = "1" ]; then
    if ! "$LINKER" $HOST_LINK_PIE -pthread "$VK_HOST_OBJ" \
            $(pkg-config --libs vulkan) -o "$VK_HOST_EXE" 2>/dev/null \
       || [ "$("$VK_HOST_EXE" "$VK_SPV")" != "1" ]; then
        echo "  FAIL: Traveler-owned Vulkan exact-map runtime parity"; fail=1
    else
        echo "  ok   Traveler-owned Vulkan exact-map runtime is CPU-byte-exact"
    fi
else
    echo "  SKIP: Vulkan loader/render node unavailable (runtime execution)"
fi

# Projection-shaped closure: eight signed Q8xQ4 terms across Splice's 17,408
# FFN channels. One source owns CPU, AMDGCN, Vulkan, and host runtime semantics.
PROJ_SRC="$SCRIPT_DIR/amd_projection_compare.tv"
PROJ_AMD_LL="$TMP/amd_projection_compare.amd.ll"
PROJ_AMD_OBJ="$TMP/amd_projection_compare.amd.o"
PROJ_HSACO="$TMP/amd_projection_compare.hsaco"
PROJ_VK="$TMP/amd_projection_compare.comp"
PROJ_SPV="$TMP/amd_projection_compare.spv"
PROJ_HOST_LL="$TMP/amd_projection_compare.ll"
PROJ_HOST_OBJ="$TMP/amd_projection_compare.o"
PROJ_EXE="$TMP/amd-projection-compare"
PROJ_READY=1
if ! "$STAGE1" --emit-gpu-vulkan "$PROJ_SRC" -o "$PROJ_VK" 2>/dev/null \
   || ! grep -q 'GL_EXT_shader_explicit_arithmetic_types_int64' "$PROJ_VK" \
   || ! grep -q 'tv_k < 8u' "$PROJ_VK" \
   || ! grep -q '17408u' "$PROJ_VK"; then
    echo "  FAIL: Vulkan exact Q8xQ4 projection artifact changed"; fail=1
    PROJ_READY=0
fi
if ! "$STAGE1" "$PROJ_SRC" -o "$PROJ_HOST_LL" 2>/dev/null \
   || ! "$LLC" $HOST_MTRIPLE -filetype=obj "$PROJ_HOST_LL" \
        -o "$PROJ_HOST_OBJ" 2>/dev/null; then
    echo "  FAIL: same-source AMD projection host runtime did not compile"; fail=1
    PROJ_READY=0
else
    echo "  ok   same-source exact Q8xQ4 projection runtime compiles"
fi
if [ "$HAVE_AMD" = "1" ]; then
    if ! "$STAGE1" --emit-gpu "$PROJ_SRC" -o "$PROJ_AMD_LL" 2>/dev/null \
       || ! grep -q 'define amdgpu_kernel void @__pfor_gpu_worker_0' \
            "$PROJ_AMD_LL"; then
        echo "  FAIL: exact Q8xQ4 projection did not emit AMDGCN"; fail=1
        PROJ_READY=0
    fi
else
    PROJ_READY=0
    echo "  SKIP: no amdgcn target (projection runtime comparison)"
fi

if [ "$PROJ_READY" = "1" ] && [ "$HAVE_GLSLANG" = "1" ] \
   && [ "$HAVE_VULKAN" = "1" ] && [ "$HAVE_HIP" = "1" ] \
   && [ "$HAVE_LINKER" = "1" ] && command -v ld.lld >/dev/null 2>&1; then
    HIP_PATH="$(hipconfig -p)"
    if ! "$LLC" -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100 -filetype=obj \
            "$PROJ_AMD_LL" -o "$PROJ_AMD_OBJ" 2>/dev/null \
       || ! ld.lld -shared "$PROJ_AMD_OBJ" -o "$PROJ_HSACO" 2>/dev/null \
       || ! "$GLSLANG_VALIDATOR" -V "$PROJ_VK" -o "$PROJ_SPV" \
            >/dev/null 2>&1 \
       || ! "$LINKER" $HOST_LINK_PIE -pthread "$PROJ_HOST_OBJ" \
            -L"$HIP_PATH/lib" -Wl,-rpath,"$HIP_PATH/lib" -lamdhip64 \
            $(pkg-config --libs vulkan) -o "$PROJ_EXE" 2>/dev/null; then
        echo "  FAIL: exact AMD projection comparison did not build"; fail=1
    else
        mapfile -t projection_metrics < <("$PROJ_EXE" "$PROJ_HSACO" "$PROJ_SPV")
        if [ "${projection_metrics[0]:-0}" != "1" ]; then
            echo "  FAIL: HIP/Vulkan exact Q8xQ4 projection parity"; fail=1
        else
            echo "  ok   HIP/Vulkan exact Q8xQ4 projection parity (17,408 channels)"
        fi
    fi
else
    echo "  SKIP: HIP+Vulkan hardware toolchain unavailable (projection execution)"
fi

# PROOF1 changes ordinary CPU authority only. This source gains three CPU
# workers, so its pre-handoff device artifacts pin the mode-selection fence.
echo "  -- PROOF1 device-authority fence"
PROOF1_DEVICE_SRC="$REPO_DIR/examples/poly_core_generic_test.tv"
PROOF1_DEVICE_MODES=("--emit-gpu" "--emit-gpu-nvptx" "--emit-gpu-agx")
PROOF1_DEVICE_HASHES=(
    "40ad76d7a19b0c595c6fa039b20d2c6de1bbe8f4887b301227a4c96b2129081c"
    "95cf24e57e37f703060d52d7a0d5b57249846619754cc4ae0840d58428963111"
    "713440392fe735167eca32a9cba5e60de0a3bef56364c8589c595ddf11197b9b"
)
for pi in 0 1 2; do
    proof1_out="$TMP/proof1-device-$pi.out"
    if ! "$STAGE1" "${PROOF1_DEVICE_MODES[$pi]}" "$PROOF1_DEVICE_SRC" \
            -o "$proof1_out" 2>/dev/null; then
        echo "  FAIL: PROOF1 device fence compile ${PROOF1_DEVICE_MODES[$pi]}"; fail=1
        continue
    fi
    proof1_hash="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$proof1_out")"
    if [ "$proof1_hash" != "${PROOF1_DEVICE_HASHES[$pi]}" ]; then
        echo "  FAIL: PROOF1 changed ${PROOF1_DEVICE_MODES[$pi]} admission/bytes"; fail=1
    else
        echo "  ok   ${PROOF1_DEVICE_MODES[$pi]} unchanged across CPU authority handoff"
    fi
done

# Static closure identity restores CPU prove-through, but device modules do not
# contain lifted closure bodies. Keep that context strictly outside all targets.
PROOF1_CLOSURE_SRC="$REPO_DIR/examples/closure_prove_through.tv"
if ! "$STAGE1" --emit-gpu "$PROOF1_CLOSURE_SRC" \
        -o "$TMP/proof1-closure-amd.ll" 2>/dev/null \
   || grep -q 'define amdgpu_kernel' "$TMP/proof1-closure-amd.ll" \
   || ! "$STAGE1" --emit-gpu-nvptx "$PROOF1_CLOSURE_SRC" \
        -o "$TMP/proof1-closure-nv.ll" 2>/dev/null \
   || grep -q 'define ptx_kernel' "$TMP/proof1-closure-nv.ll" \
   || ! "$STAGE1" --emit-gpu-agx "$PROOF1_CLOSURE_SRC" \
        -o "$TMP/proof1-closure-agx.hex" 2>/dev/null \
   || grep -q '^worker __pfor_gpu_worker_' "$TMP/proof1-closure-agx.hex"; then
    echo "  FAIL: PROOF1 closure context crossed a device boundary"; fail=1
else
    echo "  ok   static closure contexts remain CPU-only on AMD/NV/AGX"
fi

echo ""
if [ "$fail" = "0" ]; then
    echo "  GPU: PASS"
    exit 0
else
    echo "  GPU: FAIL"
    exit 1
fi
