#!/bin/bash
# run_bootstrap.sh — the B4 gate: Traveler builds itself with NO C in the trust
# chain, the committed snapshot is fresh, and the C-free path is byte-identical
# to the historical C-seed path (so dropping C changes nothing).
#
# Four assertions:
#   1. C-FREE BUILD: bootstrap/build.sh reaches the self-hosting fixed point
#      using only the committed Traveler-produced IR + llc + a linker (no C
#      source compiled).
#   2. FRESHNESS: the committed bootstrap/tvc_self.boot.ll equals the IR the
#      booted compiler emits for the current source (the snapshot is not stale).
#   3. CORRECTNESS: the C-free-built compiler compiles a real example to a
#      binary with the expected output.
#   4. EQUIVALENCE: the C-free-built compiler and the C-seed-built compiler emit
#      BYTE-IDENTICAL IR for tvc_self.tv — proof the C seed is redundant, not
#      merely unused. (This is the only assertion that touches tvc.c, and only
#      to prove it can be dropped.)
#
# Usage: ./run_bootstrap.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"
BOOTSTRAP="$REPO_DIR/src/bootstrap"

find_llc() {
    if [ -n "${LLC:-}" ] && command -v "$LLC" &>/dev/null; then return; fi
    for p in \
        /opt/homebrew/opt/llvm@21/bin/llc \
        /usr/local/opt/llvm@21/bin/llc \
        /usr/lib/llvm-21/bin/llc \
        llc-21 llc; do
        if command -v "$p" &>/dev/null; then LLC="$p"; return; fi
    done
    echo "FATAL: llc not found. Set LLC." >&2; exit 1
}
find_llc
export LLC

# --- host target: retarget IR objects + non-PIE link off-macOS ---
# Traveler-emitted IR text carries the canonical triple on every host (the
# byte-identity gates depend on that); execution overrides it at llc
# (-mtriple) and links with -no-pie where Linux defaults to PIE.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

PASS=0; FAIL=0; TOTAL=0; FAILURES=""
check() {
    local name="$1"; local ok="$2"
    TOTAL=$((TOTAL + 1))
    if [ "$ok" -eq 1 ]; then
        printf "  [%d] %-46s PASS\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%d] %-46s FAIL\n" "$TOTAL" "$name"; FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
}

echo ""
echo "=== B4 bootstrap gate (Traveler builds itself, no C in the chain) ==="
echo ""

# --- 1 + 2: C-free build to fixed point + freshness ---
if "$BOOTSTRAP/build.sh" --check >"$TMPDIR/build.log" 2>&1; then
    check "C-free build reaches fixed point (no C source)" 1
    if grep -q "committed boot IR is fresh" "$TMPDIR/build.log"; then
        check "committed boot IR is fresh (== current source)" 1
    else
        check "committed boot IR is fresh (== current source)" 0
        cat "$TMPDIR/build.log" >&2
    fi
else
    check "C-free build reaches fixed point (no C source)" 0
    check "committed boot IR is fresh (== current source)" 0
    cat "$TMPDIR/build.log" >&2
fi

STAGE1="$BOOTSTRAP/out/stage1"

# --- 3: the C-free-built compiler produces correct output ---
ok=1
if [ -x "$STAGE1" ]; then
    "$STAGE1" "$EXAMPLES/field_basics.tv" -o "$TMPDIR/fb.ll" 2>/dev/null || ok=0
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/fb.ll" -o "$TMPDIR/fb.o" 2>/dev/null || ok=0
    cc $LINK_PIE "$TMPDIR/fb.o" -o "$TMPDIR/fb" 2>/dev/null || ok=0
    got=$("$TMPDIR/fb" 2>/dev/null | tr '\n' ' ')
    [ "$got" = "49 100 171 2 123 1 " ] || ok=0
else
    ok=0
fi
check "C-free compiler emits correct native output" "$ok"

# --- 4: equivalence to the C-seed path (proves the seed is redundant) ---
# Build the seed (this is the ONLY C compilation in the gate, and only to prove
# it produces the same compiler the C-free path does).
ok=1
if (cd "$SRC_DIR" && make tvc >/dev/null 2>&1); then
    "$SRC_DIR/tvc" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/seed_s1.ll" 2>/dev/null || ok=0
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/seed_s1.ll" -o "$TMPDIR/seed_s1.o" 2>/dev/null || ok=0
    cc $LINK_PIE "$TMPDIR/seed_s1.o" -o "$TMPDIR/seed_s1" 2>/dev/null || ok=0
    # Seed-built compiler emits IR for tvc_self.tv.
    "$TMPDIR/seed_s1" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/seed_out.ll" 2>/dev/null || ok=0
    # C-free-built compiler emits IR for tvc_self.tv.
    "$STAGE1" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/free_out.ll" 2>/dev/null || ok=0
    if [ "$ok" -eq 1 ] && diff -q "$TMPDIR/seed_out.ll" "$TMPDIR/free_out.ll" >/dev/null; then
        ok=1
    else
        ok=0
    fi
else
    ok=0
fi
check "C-free path == C-seed path (seed is redundant)" "$ok"

echo ""
echo "============================================"
printf "  BOOTSTRAP: %d PASS, %d FAIL (of %d)\n" "$PASS" "$FAIL" "$TOTAL"
[ -n "$FAILURES" ] && echo "  FAILED:$FAILURES"
echo "============================================"
echo ""
[ "$FAIL" -eq 0 ]
