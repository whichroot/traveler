#!/usr/bin/env bash
# REPL session gate (eval-engine E3): scripted-transcript goldens.
#
# Each cases/<name>.in is fed to `tvc_self --repl` on stdin and the COMBINED
# stdout+stderr must equal cases/<name>.out byte-for-byte.
#
# Two properties this gate is really pinning:
#
#   1. PERSISTENCE — a binding made in one cell is visible in the next, and a
#      side effect runs exactly ONCE. The refused alternative (re-running an
#      accumulated buffer each cell) would re-print every earlier line, which
#      these transcripts would immediately expose.
#   2. CONTAINMENT — a bad cell (parse error, undefined name, refused call,
#      duplicate definition) reports and leaves the session intact. Each error
#      case is followed by a live expression whose value proves the earlier
#      bindings survived.
#
# Determinism note: values go to stdout through libc's buffer (E2a's stdio
# law), diagnostics go to stderr from the VALIDATING CHILD. The order is
# reproducible because the session flushes stdout before every fork and the
# parent waits for the child, so the two streams never race. The gate asserts
# that byte-exactly; an unstable interleave shows up as a failure here.
#
# Piped stdin is not a TTY, so no prompt is printed — the transcript is exactly
# the program's own output.
#
# @internal-note: plan-eval-engine (E3).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

SELF="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$SELF" ]; then
    LLC="${LLC:-}" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$SELF" ]; then
    echo "  FATAL: tvc_self not built at $SELF (run src/bootstrap/build.sh)"; exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "=== REPL session gate (tests/repl) ==="
pass=0; fail=0

for in_file in "$SCRIPT_DIR"/cases/*.in; do
    [ -e "$in_file" ] || continue
    name="$(basename "$in_file" .in)"
    golden="${in_file%.in}.out"
    if [ ! -f "$golden" ]; then
        echo "  [$name] NO GOLDEN (expected $golden)"
        fail=$((fail + 1)); continue
    fi

    "$SELF" --repl < "$in_file" > "$TMP/$name.got" 2>&1
    rc=$?
    # A session must ALWAYS exit cleanly: EOF on stdin is a normal end, and no
    # cell — however broken — may take the process down with it.
    if [ "$rc" != "0" ]; then
        echo "  [$name] FAIL (session exited $rc; a cell killed the session)"
        fail=$((fail + 1)); continue
    fi
    if ! cmp -s "$TMP/$name.got" "$golden"; then
        echo "  [$name] FAIL (transcript differs)"
        diff "$golden" "$TMP/$name.got" | head -12 | sed 's/^/      /'
        fail=$((fail + 1)); continue
    fi

    # Re-run: the transcript must be reproducible byte-for-byte (the stdout /
    # stderr interleave is a property, not an accident).
    "$SELF" --repl < "$in_file" > "$TMP/$name.got2" 2>&1
    if ! cmp -s "$TMP/$name.got" "$TMP/$name.got2"; then
        echo "  [$name] FAIL (nondeterministic transcript across runs)"
        fail=$((fail + 1)); continue
    fi

    echo "  [$name] PASS"
    pass=$((pass + 1))
done

if [ "$fail" = "0" ]; then
    echo "  REPL: $pass PASS, 0 FAIL"
    exit 0
else
    echo "  REPL: $pass PASS, $fail FAIL"
    exit 1
fi
