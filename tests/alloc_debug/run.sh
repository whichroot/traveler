#!/usr/bin/env bash
# alloc-debug gate (memory model M5): the --alloc-debug redzone runtime.
#
# Two properties pinned:
#   1. A correct program runs to completion under the redzone runtime (no
#      false positives on alloc / realloc / full-range write / free).
#   2. A deliberate one-element overflow is caught at free() — the trailer
#      canary is smeared, the program aborts (SIGABRT) with the named message
#      instead of corrupting a neighbor. This is the #72-class heap smash the
#      runtime exists to catch on the first run instead of a 90-minute
#      bisection.
#
# Flag-off byte-identity is proven separately by the codegen-diff gate (which
# runs without --alloc-debug and stays byte-stable).
#
# Usage: ./run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
SELF="$REPO_DIR/src/bootstrap/out/stage1"

find_llc() {
    if [ -n "${LLC:-}" ] && command -v "$LLC" &>/dev/null; then return; fi
    for p in /opt/homebrew/opt/llvm@21/bin/llc /usr/local/opt/llvm@21/bin/llc \
             /usr/lib/llvm-21/bin/llc llc-21 llc; do
        if command -v "$p" &>/dev/null; then LLC="$p"; return; fi
    done
    echo "FATAL: llc not found. Set LLC env var." >&2; exit 1
}
find_llc

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac

# Shared environment probe (tests/lib/env.sh): LINKER (link driver), LINK_PIE
# re-derived honoring TRAVELER_LINK_FLAGS, plus capability flags.
. "/../lib/env.sh"

TMPD=$(mktemp -d)
trap "rm -rf $TMPD" EXIT
PASS=0; FAIL=0

# --- 1. correct program runs clean under redzones ---
"$SELF" --alloc-debug "$SCRIPT_DIR/ok.tv" -o "$TMPD/ok.ll" 2>/dev/null || { echo "FAIL: ok.tv compile"; exit 1; }
"$LLC" $LLC_TARGET -filetype=obj "$TMPD/ok.ll" -o "$TMPD/ok.o" 2>/dev/null || { echo "FAIL: ok.tv llc"; exit 1; }
"$LINKER" $LINK_PIE "$TMPD/ok.o" -o "$TMPD/ok" 2>/dev/null || { echo "FAIL: ok.tv link"; exit 1; }
got=$("$TMPD/ok" 2>/dev/null)
if [ "$got" = "1
4
99
7" ]; then
    echo "  [ok]      PASS (clean run under redzones)"; PASS=$((PASS+1))
else
    echo "  [ok]      FAIL (got: $got)"; FAIL=$((FAIL+1))
fi

# --- 2. deliberate overflow aborts at free with the canary message ---
"$SELF" --alloc-debug "$SCRIPT_DIR/smash.tv" -o "$TMPD/smash.ll" 2>/dev/null || { echo "FAIL: smash.tv compile"; exit 1; }
"$LLC" $LLC_TARGET -filetype=obj "$TMPD/smash.ll" -o "$TMPD/smash.o" 2>/dev/null || { echo "FAIL: smash.tv llc"; exit 1; }
"$LINKER" $LINK_PIE "$TMPD/smash.o" -o "$TMPD/smash" 2>/dev/null || { echo "FAIL: smash.tv link"; exit 1; }
smout=$("$TMPD/smash" 2>&1)
smrc=$?
# Must: print the in-bounds read, then abort (nonzero) naming the smear.
if [ "$smrc" -ne 0 ] && printf '%s' "$smout" | grep -q 'trailer canary smeared' && ! printf '%s' "$smout" | grep -q '999'; then
    echo "  [smash]   PASS (overflow trapped at free: $(printf '%s' "$smout" | grep -m1 canary))"; PASS=$((PASS+1))
else
    echo "  [smash]   FAIL (rc=$smrc; out: $smout)"; FAIL=$((FAIL+1))
fi

# --- 3. eval provenance: double free refused by the oracle (exit 97) ---
evout=$("$SELF" --eval "$SCRIPT_DIR/eval_double_free.tv" 2>&1)
evrc=$?
if [ "$evrc" -eq 97 ] && printf '%s' "$evout" | grep -q 'eval-memory: double free' && ! printf '%s' "$evout" | grep -q '999'; then
    echo "  [eval-dblfree] PASS (oracle refused the double free)"; PASS=$((PASS+1))
else
    echo "  [eval-dblfree] FAIL (rc=$evrc; out: $evout)"; FAIL=$((FAIL+1))
fi

echo ""
echo "  ALLOC-DEBUG: $PASS PASS, $FAIL FAIL"
[ "$FAIL" -eq 0 ]
