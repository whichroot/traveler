#!/usr/bin/env bash
# Typed-pointer dialect gate (-target tpc). @internal-note: plan-typed-pointer-emission.
#
# Proves the TP-1/TP-2 contract with NO Gaudi hardware and no tpc_llvm build:
#   Leg 1 (confinement):  -target tpc output for a corpus spanning every ptr
#                         pattern class PARSES at an LLVM-14-era assembler with
#                         NO -opaque-pointers flag (the LLVM-12 dialect proxy),
#                         and no bare `ptr` token survives. Includes tvc_self.tv
#                         itself — the whole-compiler confinement proof.
#   Leg 2 (semantic):     typed module lowered by llvm@14 llc (host triple
#                         override) runs BYTE-IDENTICAL to the opaque module
#                         lowered by the suite llc. The rewrite annotates types;
#                         it must never change behavior.
#   Leg 3 (refusals):     -target tpc refuses to compose with --eval/--emit-gpu
#                         and refuses to run without -o.
#
# Pre-existing opaque defects (mini_parser/self_host_test varargs mis-spelling,
# documented in tests/codegen_diff/corpus.txt) are NOT in the corpus: the typed
# pass is a type audit that faithfully preserves an already-invalid module.
#
# Skips cleanly if no LLVM-14-era toolchain is found (typed pointers parse on
# stock LLVM 15 too, but 14 is the LLVM-12 dialect floor and the honest proxy).
#
# Usage: ./run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

# Shared environment probe (tests/lib/env.sh): LINKER (link driver) plus
# capability flags.
. "$SCRIPT_DIR/../lib/env.sh"

# --- Locate an LLVM-14-era toolchain (llvm-as + llc side by side) ---
LLC14=""
for p in \
    "${LLVM14:-}/bin" \
    /opt/homebrew/opt/llvm@14/bin \
    /usr/local/opt/llvm@14/bin \
    /usr/lib/llvm-14/bin; do
    if [ -x "$p/llvm-as" ] && [ -x "$p/llc" ]; then LLC14="$p"; break; fi
done
if [ -z "$LLC14" ]; then
    echo "=== typed-pointer dialect gate (tests/typedptr) ==="
    echo "  SKIP: no LLVM-14-era toolchain (set LLVM14 to an llvm@14 prefix)"
    exit 0
fi
AS14="$LLC14/llvm-as"
LLC14BIN="$LLC14/llc"
# Host triple for the semantic leg, from the toolchain itself.
HOST_TRIPLE="$("$LLC14BIN" --version 2>/dev/null | sed -n 's/^ *Default target: //p' | head -1)"
[ -z "$HOST_TRIPLE" ] && HOST_TRIPLE="arm64-apple-darwin"

# --- Suite llc (for the opaque side of the semantic leg) ---
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

STAGE1="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$STAGE1" ]; then
    echo "  FATAL: tvc_self not built at $STAGE1 (run src/bootstrap/build.sh)"; exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0

echo "=== typed-pointer dialect gate (tests/typedptr) ==="

# No bare `ptr` token may survive the rewrite.
bare_ptr() { grep -cE '(^|[^A-Za-z0-9_.%@])ptr([^A-Za-z0-9_]|$)' "$1"; }

# ---- Leg 1: confinement corpus parses at the LLVM-12 dialect floor ----------
echo "  -- Leg 1: confinement corpus (llvm-as, no -opaque-pointers)"
CORPUS="
field_basics closure_basics fn_pointer fn_pointer_fence predict_decide
enum_match_nested fs_basics net_loopback read_bytes_expr wide_acc_matmul
dyn_register_test crtsolve_test mem_arena_pool defer_cleanup alloc_cast_sizes
global_array_init gpu_field_map mobius_coeff
"
n1=0
for ex in $CORPUS; do
    if ! "$STAGE1" "examples/$ex.tv" -o "$TMP/$ex.tp.ll" -target tpc 2>"$TMP/$ex.err"; then
        echo "  FAIL: $ex: typed emission errored"; sed 's/^/        /' "$TMP/$ex.err" | head -3; fail=1; continue
    fi
    if ! "$AS14" "$TMP/$ex.tp.ll" -o /dev/null 2>"$TMP/$ex.aserr"; then
        echo "  FAIL: $ex: typed module rejected by LLVM-14-era parser"; sed 's/^/        /' "$TMP/$ex.aserr" | head -3; fail=1; continue
    fi
    bp=$(bare_ptr "$TMP/$ex.tp.ll")
    if [ "$bp" != "0" ]; then
        echo "  FAIL: $ex: $bp bare ptr token(s) survive"; fail=1; continue
    fi
    n1=$((n1+1))
done
[ "$fail" = "0" ] && echo "  ok   $n1 examples: typed emission parses at LLVM-14 no-flag, zero bare ptr"

# The whole-compiler confinement proof: the compiler's own module.
if ! "$STAGE1" src/tvc_self.tv -o "$TMP/self.tp.ll" -target tpc 2>"$TMP/self.err"; then
    echo "  FAIL: tvc_self.tv typed emission errored"; sed 's/^/        /' "$TMP/self.err" | head -3; fail=1
elif ! "$AS14" "$TMP/self.tp.ll" -o /dev/null 2>"$TMP/self.aserr"; then
    echo "  FAIL: tvc_self.tv typed module rejected"; sed 's/^/        /' "$TMP/self.aserr" | head -3; fail=1
elif [ "$(bare_ptr "$TMP/self.tp.ll")" != "0" ]; then
    echo "  FAIL: tvc_self.tv: bare ptr token(s) survive"; fail=1
else
    echo "  ok   tvc_self.tv itself: typed module parses, zero bare ptr"
fi

# ---- Leg 2: semantic equality (typed @ llvm14 host vs opaque @ suite llc) ---
echo "  -- Leg 2: semantic equality (typed == opaque, byte-exact output)"
SEMCORPUS="
field_basics closure_basics enum_match_nested fs_basics wide_acc_matmul
crtsolve_test defer_cleanup mem_arena_pool alloc_cast_sizes global_array_init
gpu_field_map
"
n2=0
for ex in $SEMCORPUS; do
    if ! "$STAGE1" "examples/$ex.tv" -o "$TMP/$ex.op.ll" 2>/dev/null; then
        echo "  FAIL: $ex: opaque emission errored"; fail=1; continue
    fi
    if ! "$LLC" -filetype=obj "$TMP/$ex.op.ll" -o "$TMP/$ex.op.o" 2>/dev/null \
       || ! "$LINKER" "$TMP/$ex.op.o" -o "$TMP/$ex.op.bin" 2>/dev/null; then
        echo "  FAIL: $ex: opaque module did not lower"; fail=1; continue
    fi
    if ! "$LLC14BIN" -mtriple="$HOST_TRIPLE" -filetype=obj "$TMP/$ex.tp.ll" -o "$TMP/$ex.tp.o" 2>"$TMP/$ex.lerr" \
       || ! "$LINKER" "$TMP/$ex.tp.o" -o "$TMP/$ex.tp.bin" 2>/dev/null; then
        echo "  FAIL: $ex: typed module did not lower for host"; sed 's/^/        /' "$TMP/$ex.lerr" | head -3; fail=1; continue
    fi
    ( cd "$TMP" && "./$ex.op.bin" > op.out 2>/dev/null )
    ( cd "$TMP" && "./$ex.tp.bin" > tp.out 2>/dev/null )
    if ! diff -q "$TMP/op.out" "$TMP/tp.out" > /dev/null; then
        echo "  FAIL: $ex: typed output differs from opaque"; fail=1; continue
    fi
    n2=$((n2+1))
done
[ "$fail" = "0" ] && echo "  ok   $n2 examples: typed-lowered output byte-identical to opaque"

# ---- Leg 3: refusals --------------------------------------------------------
echo "  -- Leg 3: mode refusals"
r3=0
if "$STAGE1" examples/field_basics.tv -o "$TMP/r1.ll" -target tpc --eval 2>/dev/null; then
    echo "  FAIL: -target tpc --eval did not refuse"; fail=1
else r3=$((r3+1)); fi
if "$STAGE1" examples/field_basics.tv -target tpc 2>/dev/null; then
    echo "  FAIL: -target tpc without -o did not refuse"; fail=1
else r3=$((r3+1)); fi
if "$STAGE1" examples/field_basics.tv -o "$TMP/r3.ll" -target tpc --emit-gpu 2>/dev/null; then
    echo "  FAIL: -target tpc --emit-gpu did not refuse"; fail=1
else r3=$((r3+1)); fi
[ "$r3" = "3" ] && echo "  ok   3 refusals: --eval / no--o / --emit-gpu all refuse"

echo ""
if [ "$fail" = "0" ]; then
    echo "  TYPEDPTR: PASS"
    exit 0
else
    echo "  TYPEDPTR: FAIL"
    exit 1
fi
