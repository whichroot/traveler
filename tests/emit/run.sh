#!/bin/bash
# --emit driver gate (plan-self-improving-lab, the one-shot compile slice).
#
# Proves that `tvc x.tv -o x --emit exe` turns source into a runnable native
# binary in ONE call (obj = stops at the object; ir = the default IR-only path,
# byte-unchanged). The internal llc/cc leg host-retargets (#58): on Linux the
# driver auto-adds -mtriple=<host> and -no-pie (the preamble pins the canonical
# darwin triple; Traveler codegen is non-PIC). The toolchain path is a flag
# (-llc), never getenv (no preamble-collision).
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

# The internal --emit driver now host-retargets (#58): on Linux the object is
# host-native and links. Linux links still need -no-pie for the manual link in
# step 3 (the driver adds it itself for --emit exe).
LINK_PIE=""
if [ "$(uname -s)" = "Linux" ]; then LINK_PIE="-no-pie"; fi

STAGE1="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$STAGE1" ]; then
    echo "  FATAL: tvc_self not built at $STAGE1 (run src/bootstrap/build.sh)"; exit 1
fi

echo "=== --emit driver gate (tests/emit) ==="

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SRC="$REPO_DIR/examples/field_basics.tv"
WANT="$(cat "$REPO_DIR/tests/expected/field_basics.txt")"
fail=0

# 1. --emit exe: one call, source -> runnable binary, correct output.
if "$STAGE1" "$SRC" -o "$TMP/fb" --emit exe -llc "$LLC" 2>"$TMP/exe.err"; then
    got="$("$TMP/fb" 2>/dev/null)"
    if [ "$got" = "$WANT" ]; then
        echo "  ok   --emit exe: one-shot binary runs, output matches"
    else
        echo "  FAIL: --emit exe output mismatch"; echo "    want: $WANT"; echo "    got:  $got"; fail=1
    fi
else
    echo "  FAIL: --emit exe did not produce a binary:"; sed 's/^/       /' "$TMP/exe.err"; fail=1
fi

# 2. Intermediates are cleaned up (no derived .tvctmp.* left behind).
if ls "$TMP"/fb.tvctmp.* >/dev/null 2>&1; then
    echo "  FAIL: --emit exe left intermediates behind"; fail=1
else
    echo "  ok   intermediates cleaned"
fi

# 3. --emit obj: stops at a nonempty object that links + runs.
if "$STAGE1" "$SRC" -o "$TMP/fb.o" --emit obj -llc "$LLC" 2>"$TMP/obj.err"; then
    if [ -s "$TMP/fb.o" ] && cc $LINK_PIE "$TMP/fb.o" -o "$TMP/fb_obj" 2>/dev/null && \
       [ "$("$TMP/fb_obj" 2>/dev/null)" = "$WANT" ]; then
        echo "  ok   --emit obj: object links and runs"
    else
        echo "  FAIL: --emit obj object did not link/run"; fail=1
    fi
else
    echo "  FAIL: --emit obj did not produce an object:"; sed 's/^/       /' "$TMP/obj.err"; fail=1
fi

# 4. Default (--emit ir) is unchanged: writes an IR module to -o.
if "$STAGE1" "$SRC" -o "$TMP/fb.ll" 2>/dev/null && \
   head -1 "$TMP/fb.ll" | grep -q "Traveler compiler output"; then
    echo "  ok   --emit ir (default): IR module written, unchanged"
else
    echo "  FAIL: default IR emission changed"; fail=1
fi

echo ""
if [ "$fail" = "0" ]; then
    echo "  EMIT: PASS"
    exit 0
else
    echo "  EMIT: FAIL"
    exit 1
fi
