#!/bin/bash
# Stage-0 GPU device-codegen gate (plan-gpu-purity-runtime).
#
# Proves that a proven-parallel pfor worker re-emits as a valid device kernel on
# BOTH device targets, with NO GPU hardware:
#   AMDGCN (--emit-gpu)        `llc -mtriple=amdgcn-amd-amdhsa -mcpu=gfx1100`
#                              lowers to a real gfx1100 code object that
#                              disassembles to sane GCN (s_endpgm + memory op).
#   NVPTX  (--emit-gpu-nvptx)  `llc -mtriple=nvptx64-nvidia-cuda -mcpu=sm_90`
#                              lowers to PTX with a .visible .entry, the
#                              .maxntid launch bound, %tid.x/%ctaid.x SIMT reads
#                              and .global memory ops.
# Mirrors the IR-graph Stage-0 discipline — pure codegen, zero hardware.
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

echo "=== Stage-0 GPU device codegen gate (tests/gpu) ==="

# Which device targets are built into this llc? Each leg needs its own; a
# missing target is a SKIP (not a failure), a lowering error is a FAIL.
TARGETS="$("$LLC" --version 2>/dev/null)"
HAVE_AMD=0; HAVE_NV=0
echo "$TARGETS" | grep -qiE '^\s*amdgcn'  && HAVE_AMD=1
echo "$TARGETS" | grep -qiE '^\s*nvptx64' && HAVE_NV=1
if [ "$HAVE_AMD" = "0" ] && [ "$HAVE_NV" = "0" ]; then
    echo "  SKIP: this llc has neither the amdgcn nor the nvptx64 target."
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
    echo "  GPU-STAGE0: PASS"
    exit 0
else
    echo "  GPU-STAGE0: FAIL"
    exit 1
fi
