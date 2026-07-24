#!/bin/bash
# Doc-generator gate (roadmap B3b) — tvdoc.tv.
#
# tvdoc is a conservative, line-oriented Markdown API doc generator written in
# Traveler (model: tvfmt.tv). It walks top-level declarations and emits one
# section per fn/struct/enum/trait/field with its signature + preceding
# doc-comment block. This gate pins:
#   - it builds and runs through tvc_self,
#   - output is non-empty Markdown with the file title header,
#   - expected declaration names + signatures appear,
#   - doc-comment bodies are attached to the right declaration,
#   - it is stable (running twice yields identical output).
#
# Usage: ./run_doc.sh
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

# --- Build tvdoc ---
"$TVC_SELF" "$REPO_DIR/src/tools/tvdoc.tv" -o "$TMPDIR/tvdoc.ll" 2>/dev/null || {
    echo "FATAL: tvdoc compile failed" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvdoc.ll" -o "$TMPDIR/tvdoc.o" 2>/dev/null
clang $LINK_PIE "$TMPDIR/tvdoc.o" -o "$TMPDIR/tvdoc" 2>/dev/null || {
    echo "FATAL: tvdoc link failed" >&2; exit 1
}
TVDOC="$TMPDIR/tvdoc"

PASS=0
FAIL=0
TOTAL=0
FAILURES=""

check() {
    local name="$1"; local result="$2"
    TOTAL=$((TOTAL + 1))
    if [ "$result" -eq 1 ]; then
        printf "  [%2d] %-44s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-44s FAIL\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
}

echo ""
echo "=== Doc generator gate (tvdoc, vs tvc_self) ==="
echo ""

# --- 1. poly_core: fn signatures + doc bodies ---
out="$TMPDIR/poly_core.md"
"$TVDOC" "$REPO_DIR/src/lib/core/poly_core.tv" >"$out" 2>/dev/null
ok=1
[ -s "$out" ] || ok=0
grep -q '^# ' "$out" || ok=0                                   # title header
grep -q '### fn mod_add' "$out" || ok=0                         # decl heading
grep -q 'fn mod_add(a: i32, b: i32, p: i32) -> i32' "$out" || ok=0  # signature
grep -q 'Modular addition' "$out" || ok=0                       # doc body attached
check "doc:poly_core (headings + signatures + bodies)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 2. struct/enum/trait kinds ---
out="$TMPDIR/trait.md"
"$TVDOC" "$EXAMPLES/trait_dispatch.tv" >"$out" 2>/dev/null
ok=1
[ -s "$out" ] || ok=0
grep -q '### struct Rect' "$out" || ok=0
grep -q '### trait Shape' "$out" || ok=0
grep -q 'fn describe<T: Shape>' "$out" || ok=0   # generic signature preserved
check "doc:trait_dispatch (struct/trait/generic)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 3. field declarations ---
out="$TMPDIR/field.md"
"$TVDOC" "$EXAMPLES/field_basics.tv" >"$out" 2>/dev/null
ok=1
[ -s "$out" ] || ok=0
grep -q '### field F' "$out" || ok=0
grep -q '### fn main' "$out" || ok=0
check "doc:field_basics (field + fn)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 4. stability: running twice yields identical output ---
out1="$TMPDIR/stab1.md"; out2="$TMPDIR/stab2.md"
"$TVDOC" "$REPO_DIR/src/lib/core/poly_core.tv" >"$out1" 2>/dev/null
"$TVDOC" "$REPO_DIR/src/lib/core/poly_core.tv" >"$out2" 2>/dev/null
if diff -q "$out1" "$out2" >/dev/null; then
    check "doc:stability (deterministic output)" 1
else
    check "doc:stability (deterministic output)" 0
fi

# --- 5. no crash across a broad sample of examples + libraries ---
sample_ok=1
for tv in field_basics edge_cases poly_core struct_basics enum_basics \
          trait_dispatch method_basics ntt poseidon2; do
    f="$EXAMPLES/${tv}.tv"
    if [ ! -f "$f" ]; then
        f=$(find "$REPO_DIR/src/lib" "$REPO_DIR/src/tools" -name "${tv}.tv" 2>/dev/null | head -1)
    fi
    [ -n "$f" ] && [ -f "$f" ] || continue
    "$TVDOC" "$f" >/dev/null 2>&1
    rc=$?
    if [ "$rc" -eq 139 ] || [ "$rc" -eq 134 ] || [ "$rc" -eq 138 ]; then
        sample_ok=0
        echo "        crash (exit $rc) on $tv" >&2
    fi
done
check "doc:no-crash across sample" "$sample_ok"

echo ""
echo "============================================"
printf "  DOC: %d PASS, %d FAIL  (of %d)\n" "$PASS" "$FAIL" "$TOTAL"
if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
echo "============================================"
echo ""

[ "$FAIL" -eq 0 ]
