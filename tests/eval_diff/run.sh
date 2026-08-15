#!/usr/bin/env bash
# Eval-diff gate (eval-engine E1): the differential semantic oracle.
# For every corpus program:   eval(prog) == run(compile(prog))
# byte-exact on stdout AND equal on exit status. This is the fixed-point
# discipline applied to SEMANTICS — the evaluator is only trustworthy
# because this gate compares it against the compiled truth on every run.
#
# The evaluator's cardinal rule (refuse loudly, never misevaluate) keeps
# this gate honest: an uncovered construct refuses (exit 97) instead of
# guessing, so a refusal on a CORPUS entry is a coverage regression and
# fails the gate. Where tests/expected/<name>.txt exists it is also
# cross-checked against the compiled output (tripwire).
#
# @internal-note: plan-eval-engine (E1).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

CORPUS="$SCRIPT_DIR/corpus.txt"
SELF="$REPO_DIR/src/bootstrap/out/stage1"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ ! -x "$SELF" ]; then
    echo "FATAL: tvc_self not built at $SELF (run src/bootstrap/build.sh)"; exit 1
fi

# Locate llc (LLC env wins; then llvm@21, llc-21, llc).
find_llc() {
    if [ -n "${LLC:-}" ] && [ -x "$LLC" ]; then echo "$LLC"; return; fi
    for c in /opt/homebrew/opt/llvm@21/bin/llc /usr/local/opt/llvm@21/bin/llc \
             /opt/homebrew/opt/llvm/bin/llc llc-21 llc; do
        if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
    done
    echo ""
}
LLC_BIN="$(find_llc)"

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
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/env.sh"
if [ -z "$LLC_BIN" ]; then
    echo "FATAL: llc not found (set LLC=<path>)"; exit 1
fi

echo "=== Eval-diff gate (tests/eval_diff) ==="
pass=0; fail=0
while IFS= read -r line; do
    line="${line%%#*}"
    # Optional program args (E2a, main-with-params entries): `path | args`.
    # Args are word-split and passed identically to the compiled binary and
    # to --eval (which forwards post-source args to the program's main).
    args=""
    case "$line" in
        *\|*) args="${line#*|}"; line="${line%%|*}" ;;
    esac
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    name="$(basename "$line" .tv)"
    if [ ! -f "$REPO_DIR/$line" ]; then
        echo "  MISSING SOURCE: $line"; fail=$((fail+1)); continue
    fi
    # Compiled truth.
    if ! "$SELF" "$REPO_DIR/$line" -o "$TMP/out.ll" >/dev/null 2>&1; then
        echo "  COMPILE FAILED: $line"; fail=$((fail+1)); continue
    fi
    if ! "$LLC_BIN" $LLC_TARGET -filetype=obj "$TMP/out.ll" -o "$TMP/out.o" 2>/dev/null; then
        echo "  LLC FAILED: $line"; fail=$((fail+1)); continue
    fi
    if ! "$LINKER" $LINK_PIE "$TMP/out.o" -o "$TMP/out.bin" 2>/dev/null; then
        echo "  LINK FAILED: $line"; fail=$((fail+1)); continue
    fi
    # shellcheck disable=SC2086  # args is intentionally word-split
    "$TMP/out.bin" $args > "$TMP/compiled.txt" 2>/dev/null < /dev/null
    c_rc=$?
    # Evaluated.
    # shellcheck disable=SC2086
    "$SELF" --eval "$REPO_DIR/$line" $args > "$TMP/eval.txt" 2>"$TMP/eval_err.txt" < /dev/null
    e_rc=$?
    if [ "$e_rc" = "97" ]; then
        reason="$(grep -o 'eval-refused: [^ ]*' "$TMP/eval_err.txt" | head -1)"
        echo "  REFUSED (coverage regression): $line  ${reason#eval-refused: }"
        fail=$((fail+1)); continue
    fi
    if [ "$e_rc" = "98" ]; then
        echo "  EVAL-INTERNAL: $line  $(tail -1 "$TMP/eval_err.txt")"
        fail=$((fail+1)); continue
    fi
    if ! cmp -s "$TMP/eval.txt" "$TMP/compiled.txt"; then
        echo "  OUTPUT MISMATCH: $line (eval != compiled — the evaluator lied or drifted)"
        diff "$TMP/compiled.txt" "$TMP/eval.txt" | head -6 | sed 's/^/    /'
        fail=$((fail+1)); continue
    fi
    if [ "$e_rc" != "$c_rc" ]; then
        echo "  EXIT MISMATCH: $line (eval=$e_rc compiled=$c_rc)"
        fail=$((fail+1)); continue
    fi
    # Tripwire: compiled output must still match the committed golden.
    if [ -f "$REPO_DIR/tests/expected/$name.txt" ]; then
        if ! cmp -s "$TMP/compiled.txt" "$REPO_DIR/tests/expected/$name.txt"; then
            echo "  GOLDEN MISMATCH: $line (compiled output drifted from tests/expected)"
            fail=$((fail+1)); continue
        fi
    fi
    pass=$((pass+1))
done < "$CORPUS"

if [ "$fail" = "0" ]; then
    echo "  EVAL-DIFF: PASS ($pass programs, eval == compiled byte-exact)"
    exit 0
else
    echo "  EVAL-DIFF: FAIL ($fail failing, $pass passing)"
    exit 1
fi
