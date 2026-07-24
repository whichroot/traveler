#!/bin/bash
# Stage-0 GPU device-codegen gate (plan-gpu-purity-runtime).
#
# Proves that a proven-parallel pfor worker re-emits as a valid AMDGPU kernel,
# with NO GPU hardware: the gate is that `llc -mtriple=amdgcn-amd-amdhsa
# -mcpu=gfx1100` lowers the --emit-gpu device module to a real gfx1100 code
# object and it disassembles to sane GCN (s_endpgm + a memory op). Mirrors the
# IR-graph Stage-0 discipline — pure codegen, zero hardware.
#
# Skips cleanly (exit 0) if the local llc lacks the amdgcn target.
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

echo "=== Stage-0 GPU device codegen gate (tests/gpu) ==="

# The AMDGPU target must be built into this llc, else skip (not a failure).
if ! "$LLC" --version 2>/dev/null | grep -qiE '^\s*amdgcn'; then
    echo "  SKIP: this llc has no amdgcn target (Stage-0 gate needs it)."
    exit 0
fi

STAGE1="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$STAGE1" ]; then
    echo "  FATAL: tvc_self not built at $STAGE1 (run src/bootstrap/build.sh)"; exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
EXHIBIT="$REPO_DIR/examples/gpu_field_map.tv"
DEV="$TMP/gpu_dev.ll"
OBJ="$TMP/gpu_dev.o"
fail=0

# 1. Emit the device module.
if ! "$STAGE1" --emit-gpu "$EXHIBIT" -o "$DEV" 2>/dev/null; then
    echo "  FAIL: --emit-gpu did not produce a module"; exit 1
fi

# 2. Structural asserts: an amdgpu_kernel with addrspace(1) global kernargs and
#    the SIMT workitem intrinsic (the loop→SIMT rewrite).
nk=$(grep -c "define amdgpu_kernel" "$DEV" || true)
if [ "$nk" -lt 1 ]; then echo "  FAIL: no amdgpu_kernel emitted"; fail=1; fi
grep -q "ptr addrspace(1)" "$DEV" || { echo "  FAIL: no addrspace(1) global kernargs"; fail=1; }
grep -q "llvm.amdgcn.workitem.id.x" "$DEV" || { echo "  FAIL: no SIMT workitem index"; fail=1; }
grep -q "target triple = \"amdgcn-amd-amdhsa\"" "$DEV" || { echo "  FAIL: wrong/absent device triple"; fail=1; }
[ "$fail" = "0" ] && echo "  ok   device module: $nk amdgpu_kernel(s), addrspace(1) kernargs, SIMT index"

# 3. THE GATE: llc lowers it to a valid gfx1100 code object.
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

# 4. Disasm sanity: a real kernel epilogue + a global/flat memory op.
if [ "$fail" = "0" ] && [ -x "$OBJDUMP" ]; then
    dis="$("$OBJDUMP" -d "$OBJ" 2>/dev/null)"
    echo "$dis" | grep -q "s_endpgm" || { echo "  FAIL: no s_endpgm (not a real kernel)"; fail=1; }
    echo "$dis" | grep -qiE "flat_(load|store)|global_(load|store)" || { echo "  FAIL: no memory op in the kernel"; fail=1; }
    [ "$fail" = "0" ] && echo "  ok   GCN disasm: s_endpgm + flat memory ops (sane kernel)"
fi

echo ""
if [ "$fail" = "0" ]; then
    echo "  GPU-STAGE0: PASS"
    exit 0
else
    echo "  GPU-STAGE0: FAIL"
    exit 1
fi
