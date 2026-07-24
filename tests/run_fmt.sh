#!/bin/bash
# Formatter regression suite (roadmap B3a).
#
# tvfmt is a conservative, comment-preserving reindenter written in Traveler.
# The acceptance gate is IDEMPOTENCE on examples/*.tv: fmt(fmt(x)) == fmt(x).
# Idempotence is the formatter's core correctness property — a fixed point of
# the format function, mirroring the compiler's Stage2==Stage3 discipline.
#
# We also spot-check MEANING PRESERVATION on a few examples: format the file,
# compile both original and formatted with tvc_self, and require byte-identical
# runtime output. (A reindenter must never change program behavior.)
#
# Usage: ./run_fmt.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"

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

# --- Build the canonical compiler (Stage 1) ---
(cd "$SRC_DIR" && make tvc >/dev/null 2>&1) || {
    (cd "$SRC_DIR" && clang -O2 -Wall -Wextra -std=c99 -o tvc tvc.c) || exit 1
}
"$SRC_DIR/tvc" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/tvc_self.ll" 2>/dev/null || {
    echo "FATAL: Stage 1 compile failed" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvc_self.ll" -o "$TMPDIR/tvc_self.o" 2>/dev/null
clang $LINK_PIE "$TMPDIR/tvc_self.o" -o "$TMPDIR/tvc_self" 2>/dev/null
TVC_SELF="$TMPDIR/tvc_self"

# --- Build tvfmt ---
"$TVC_SELF" "$REPO_DIR/src/tools/tvfmt.tv" -o "$TMPDIR/tvfmt.ll" 2>/dev/null || {
    echo "FATAL: tvfmt compile failed" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvfmt.ll" -o "$TMPDIR/tvfmt.o" 2>/dev/null
clang $LINK_PIE "$TMPDIR/tvfmt.o" -o "$TMPDIR/tvfmt" 2>/dev/null
TVFMT="$TMPDIR/tvfmt"

PASS=0
FAIL=0
FAILURES=""

echo ""
echo "=== Formatter idempotence (tvfmt, examples + src/lib + src/tools + compiler) ==="
echo ""

# Format-check every .tv: demos (examples/), libraries (src/lib/**), tools
# (src/tools/), and the compiler itself (src/tvc_self.tv).
FMT_FILES=$( { ls "$EXAMPLES"/*.tv 2>/dev/null
               find "$REPO_DIR/src/lib" "$REPO_DIR/src/tools" -name '*.tv' 2>/dev/null
               echo "$REPO_DIR/src/tvc_self.tv"; } )
for f in $FMT_FILES; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    "$TVFMT" "$f" > "$TMPDIR/once" 2>/dev/null
    "$TVFMT" "$TMPDIR/once" > "$TMPDIR/twice" 2>/dev/null
    if diff -q "$TMPDIR/once" "$TMPDIR/twice" >/dev/null 2>&1; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        echo "  NOT IDEMPOTENT: $name"
    fi
done

# --- Meaning preservation spot-check ---
echo ""
echo "=== Formatter meaning-preservation (compile orig vs formatted) ==="
build_run() {
    "$TVC_SELF" "$1" -o "$TMPDIR/m.ll" 2>/dev/null || return 1
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/m.ll" -o "$TMPDIR/m.o" 2>/dev/null || return 1
    clang $LINK_PIE "$TMPDIR/m.o" -o "$TMPDIR/m" 2>/dev/null || return 1
    "$TMPDIR/m" 2>/dev/null
}
for name in field_basics edge_cases bitwise_ops enum_basics int_match poly_classify; do
    f="$EXAMPLES/${name}.tv"
    [ -f "$f" ] || continue
    "$TVFMT" "$f" > "$TMPDIR/fmt.tv" 2>/dev/null
    a=$(build_run "$f")
    b=$(build_run "$TMPDIR/fmt.tv")
    if [ "$a" = "$b" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES ${name}(meaning)"
        echo "  MEANING CHANGED: $name"
    fi
done

# --- --migrate codemod gate (syntax-modernization Phase 2) ---
# The codemod's four contracts: (1) rewrite matches the hand-written golden
# byte-for-byte; (2) the golden is a fixed point (idempotence); (3) the golden
# passes `--migrate --check` (already migrated + formatted); (4) input and
# golden compile to BYTE-IDENTICAL IR (the Phase-1 alias output-neutrality
# proof applied to the codemod's own fixture).
echo ""
echo "=== tvfmt --migrate (let mut -> var; field/binfield/extfield -> type) ==="
MIG_IN="$REPO_DIR/tests/fmt/migrate_input.tv"
MIG_GOLD="$REPO_DIR/tests/fmt/migrate_golden.tv"
"$TVFMT" --migrate "$MIG_IN" > "$TMPDIR/mig_out.tv" 2>/dev/null
if diff -q "$TMPDIR/mig_out.tv" "$MIG_GOLD" >/dev/null 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES migrate-golden"
    echo "  MIGRATE OUTPUT != GOLDEN"
fi
"$TVFMT" --migrate "$MIG_GOLD" > "$TMPDIR/mig_fix.tv" 2>/dev/null
if diff -q "$TMPDIR/mig_fix.tv" "$MIG_GOLD" >/dev/null 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES migrate-idempotent"
    echo "  MIGRATE NOT IDEMPOTENT ON GOLDEN"
fi
if "$TVFMT" --migrate --check "$MIG_GOLD" 2>/dev/null; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES migrate-check"
    echo "  --migrate --check FAILED on golden"
fi
"$TVC_SELF" "$MIG_IN" -o "$TMPDIR/mig_a.ll" >/dev/null 2>&1
"$TVC_SELF" "$MIG_GOLD" -o "$TMPDIR/mig_b.ll" >/dev/null 2>&1
if diff -q "$TMPDIR/mig_a.ll" "$TMPDIR/mig_b.ll" >/dev/null 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES migrate-ir"
    echo "  MIGRATED IR != ORIGINAL IR"
fi

# --- --check exit-code sanity ---
echo ""
echo "=== Formatter --check exit codes ==="
# A file formatted by tvfmt must pass --check (exit 0).
"$TVFMT" "$EXAMPLES/field_basics.tv" > "$TMPDIR/canon.tv" 2>/dev/null
if "$TVFMT" --check "$TMPDIR/canon.tv" 2>/dev/null; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES check-clean"
    echo "  --check FAILED on canonical file"
fi
# A deliberately mis-indented file must fail --check (exit 1).
printf 'fn main() {\nlet x: i32 = 1;\n        print(x);\n}\n' > "$TMPDIR/messy.tv"
if "$TVFMT" --check "$TMPDIR/messy.tv" 2>/dev/null; then
    FAIL=$((FAIL + 1)); FAILURES="$FAILURES check-dirty"
    echo "  --check PASSED on mis-indented file (should fail)"
else
    PASS=$((PASS + 1))
fi

echo ""
echo "============================================"
printf "  FMT: %d PASS, %d FAIL\n" "$PASS" "$FAIL"
if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
echo "============================================"
echo ""

[ "$FAIL" -eq 0 ]
