#!/usr/bin/env bash
# Codegen-diff gate (IR-graph Stage 0): hash the emitted LLVM IR of a curated
# corpus under tvc_self and compare against a committed golden manifest. Catches
# UNINTENDED codegen drift on real programs — the gap the fixed point does NOT
# cover (the fixed point proves the compiler reproduces ITSELF; this proves the
# compiler reproduces the SAME OUTPUT for arbitrary programs across changes).
#
# Emitted IR is deterministic (verified: same input -> byte-identical IR), so a
# hash manifest is a sound, tiny gate.
#
# Usage:
#   run.sh            check corpus hashes against golden.txt (CI/gate mode)
#   run.sh --update   re-bless golden.txt from current output (after an
#                     intentional codegen change; commit the new golden.txt)
#
# A flipped hash is NOT necessarily a bug — it means "codegen output changed."
# The gate's job is to make that change CONSCIOUS: either it was intended
# (re-bless) or it was a supposedly-output-neutral change that wasn't (a bug).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

CORPUS="$SCRIPT_DIR/corpus.txt"
GOLDEN="$SCRIPT_DIR/golden.txt"
SELF="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ ! -x "$SELF" ]; then
    echo "FATAL: tvc_self not built at $SELF (run src/bootstrap/build.sh)"; exit 1
fi

MODE="check"
if [ "${1:-}" = "--update" ]; then MODE="update"; fi

# Compute "path  hash" lines for the whole corpus into $TMP/current.txt.
: > "$TMP/current.txt"
fail=0
while IFS= read -r line; do
    # strip comments / blanks
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    if [ ! -f "$REPO_DIR/$line" ]; then
        echo "  MISSING SOURCE: $line"; fail=1; continue
    fi
    if ! "$SELF" "$REPO_DIR/$line" -o "$TMP/out.ll" >/dev/null 2>&1; then
        echo "  COMPILE FAILED: $line"; fail=1; continue
    fi
    h="$(md5 -q "$TMP/out.ll" 2>/dev/null || md5sum "$TMP/out.ll" | cut -d' ' -f1)"
    printf '%s  %s\n' "$h" "$line" >> "$TMP/current.txt"
done < "$CORPUS"

if [ "$MODE" = "update" ]; then
    sort -k2 "$TMP/current.txt" > "$GOLDEN"
    echo "=== Codegen-diff golden re-blessed ($(wc -l < "$GOLDEN" | tr -d ' ') entries) ==="
    echo "Review the diff and commit tests/codegen_diff/golden.txt."
    exit 0
fi

# check mode
echo "=== Codegen-diff gate (tests/codegen_diff) ==="
if [ ! -f "$GOLDEN" ]; then
    echo "  NO GOLDEN: run 'tests/codegen_diff/run.sh --update' to create it."; exit 1
fi
sort -k2 "$TMP/current.txt" > "$TMP/current.sorted"
sort -k2 "$GOLDEN" > "$TMP/golden.sorted"
drift=0
# Report per-entry status.
while IFS= read -r gline; do
    ghash="${gline%% *}"; gpath="${gline##* }"
    cur="$(grep " $gpath\$" "$TMP/current.sorted" 2>/dev/null | head -1)"
    if [ -z "$cur" ]; then
        printf "  GONE    %s\n" "$gpath"; drift=1; continue
    fi
    chash="${cur%% *}"
    if [ "$chash" != "$ghash" ]; then
        printf "  DRIFT   %s\n" "$gpath"; drift=1
    fi
done < "$TMP/golden.sorted"
# New entries not in golden?
while IFS= read -r cline; do
    cpath="${cline##* }"
    if ! grep -q " $cpath\$" "$TMP/golden.sorted"; then
        printf "  NEW     %s (run --update to bless)\n" "$cpath"; drift=1
    fi
done < "$TMP/current.sorted"

if [ "$fail" = "1" ]; then
    echo "  FAIL: corpus had missing/uncompilable sources"; exit 1
fi
if [ "$drift" = "0" ]; then
    echo "  CODEGEN-DIFF: PASS ($(wc -l < "$TMP/current.sorted" | tr -d ' ') entries, IR byte-stable)"
    exit 0
else
    echo "  CODEGEN-DIFF: DRIFT DETECTED."
    echo "  If this codegen change was INTENTIONAL: tests/codegen_diff/run.sh --update, then commit golden.txt."
    echo "  If it was supposed to be output-neutral: you have an unintended codegen bug."
    exit 1
fi
