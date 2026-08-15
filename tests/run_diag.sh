#!/usr/bin/env bash
# Diagnostics catalog (roadmap A5) — error-message regression suite.
#
# The structured diagnostics from A2.1/A2.2/A2.3 (file:line:col + caret,
# "expected X, found Y", recovery, error cap, single non-zero exit) live in
# the CANONICAL compiler tvc_self, not the frozen bootstrap. The existing
# negative suite in run.sh runs against the bootstrap, so these diagnostics
# were untested. This suite pins them against tvc_self.
#
# Each fixture in tests/diag/*.tv is a deliberately broken program. The
# matching tests/diag/expected/<name>.txt lists substrings (one per line)
# that MUST all appear in stderr. Every fixture must also exit non-zero
# (a diagnostic that doesn't fail the build is a silent miscompile).
#
# Usage: ./run_diag.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"
DIAG_DIR="$SCRIPT_DIR/diag"
EXPECTED="$DIAG_DIR/expected"

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

# --- host target: retarget IR objects + non-PIE link off-macOS ---
# Traveler-emitted IR text carries the canonical triple on every host (the
# byte-identity gates depend on that); execution overrides it at llc
# (-mtriple) and links with -no-pie where Linux defaults to PIE.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac

# Shared environment probe (tests/lib/env.sh): LINKER (link driver), LINK_PIE
# re-derived honoring TRAVELER_LINK_FLAGS, plus capability flags.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/env.sh"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# Portable timeout (gtimeout on macOS brew, timeout on Linux, else no-op). A
# diagnostic that HANGS the compiler (e.g. #49: `field` misused as an identifier
# spun the parser) must fail LOUDLY, not hang CI. DIAG_TIMEOUT is generous —
# every real fixture compiles in well under a second.
DIAG_TIMEOUT=10
if command -v gtimeout &>/dev/null; then
    TIMEOUT_CMD="gtimeout"
elif command -v timeout &>/dev/null; then
    TIMEOUT_CMD="timeout"
else
    TIMEOUT_CMD=""
fi
diag_compile() { # $1=timeout secs, rest = command
    local secs="$1"; shift
    if [ -n "$TIMEOUT_CMD" ]; then "$TIMEOUT_CMD" "$secs" "$@"; else "$@"; fi
}

# --- Build the canonical compiler (Stage 1: bootstrap -> tvc_self) ---
(cd "$SRC_DIR" && make tvc >/dev/null 2>&1) || {
    (cd "$SRC_DIR" && "$LINKER" -O2 -Wall -Wextra -std=c99 -o tvc tvc.c) || exit 1
}
"$SRC_DIR/tvc" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/tvc_self.ll" 2>/dev/null || {
    echo "FATAL: Stage 1 compile failed" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvc_self.ll" -o "$TMPDIR/tvc_self.o" 2>/dev/null || {
    echo "FATAL: Stage 1 llc failed" >&2; exit 1
}
"$LINKER" $LINK_PIE "$TMPDIR/tvc_self.o" -o "$TMPDIR/tvc_self" 2>/dev/null || {
    echo "FATAL: Stage 1 link failed" >&2; exit 1
}
TVC_SELF="$TMPDIR/tvc_self"

PASS=0
FAIL=0
TOTAL=0
FAILURES=""

echo ""
echo "=== Diagnostics catalog (tests/diag, vs tvc_self) ==="
echo ""

for tv in "$DIAG_DIR"/*.tv; do
    [ -f "$tv" ] || continue
    name="$(basename "$tv" .tv)"
    exp="$EXPECTED/${name}.txt"
    TOTAL=$((TOTAL + 1))

    if [ ! -f "$exp" ]; then
        printf "  [%2d] %-30s SKIP (no expected file)\n" "$TOTAL" "$name"
        continue
    fi

    # Capture stderr and exit code (timeout-guarded — #49: a diagnostic must
    # never HANG the compiler).
    stderr_out="$(diag_compile "$DIAG_TIMEOUT" "$TVC_SELF" "$tv" -o "$TMPDIR/${name}.ll" 2>&1 >/dev/null)"
    rc=$?

    # A timeout (GNU/coreutils exit 124) means the compiler hung — a HARD fail.
    if [ "$rc" -eq 124 ]; then
        printf "  [%2d] %-30s FAIL (compiler HUNG > %ss)\n" "$TOTAL" "$name" "$DIAG_TIMEOUT"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        continue
    fi

    # Every diagnostic fixture must fail the build.
    if [ "$rc" -eq 0 ]; then
        printf "  [%2d] %-30s FAIL (exit 0; diagnostic did not fail build)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        continue
    fi
    # A crash (segfault/abort) is never an acceptable diagnostic path.
    if [ "$rc" -eq 139 ] || [ "$rc" -eq 134 ] || [ "$rc" -eq 138 ]; then
        printf "  [%2d] %-30s FAIL (crash: exit %d)\n" "$TOTAL" "$name" "$rc"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        continue
    fi

    # Every expected substring must appear.
    missing=""
    while IFS= read -r pattern; do
        [ -z "$pattern" ] && continue
        if ! printf '%s' "$stderr_out" | grep -qF "$pattern"; then
            missing="$missing|$pattern"
        fi
    done < "$exp"

    if [ -z "$missing" ]; then
        printf "  [%2d] %-30s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-30s FAIL (missing: %s)\n" "$TOTAL" "$name" "${missing#|}"
        echo "        got: $stderr_out" >&2
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
done

echo ""
echo "============================================"
printf "  DIAG: %d PASS, %d FAIL  (of %d)\n" "$PASS" "$FAIL" "$TOTAL"
if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
echo "============================================"
echo ""

[ "$FAIL" -eq 0 ]
