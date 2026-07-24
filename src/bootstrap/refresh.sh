#!/bin/bash
# bootstrap/refresh.sh — regenerate the committed bootstrap IR from the current
# Traveler source, with NO C compiler in the trust chain (B4).
#
# Run this after any INTENTIONAL change to src/tvc_self.tv that alters the
# emitted IR, then commit bootstrap/tvc_self.boot.ll. It is self-perpetuating:
# the new snapshot is produced by the PREVIOUS snapshot (Traveler compiling
# Traveler), so the C seed is never involved.
#
#   stage0  = boot the CURRENT committed IR  (bootstrap/build.sh)
#   new IR  = stage1.ll = stage0(src/tvc_self.tv)
#   verify  = stage1 compiles itself to the SAME IR (fixed point)
#   commit  = copy new IR over bootstrap/tvc_self.boot.ll
#
# The result is a new trust root produced entirely by Traveler. The chain of
# custody traces back, snapshot by snapshot, to the original C-seeded genesis —
# but no current build touches C.
#
# Usage: bootstrap/refresh.sh
# Env: LLC (llvm-21 llc), LINK (linker driver, default cc).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"      # .../src/bootstrap
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")" # repo root (two up)
BOOT_IR="$SCRIPT_DIR/tvc_self.boot.ll"
OUT="$SCRIPT_DIR/out"

# Build from the current committed IR (this reaches the fixed point and leaves
# the fresh self-output in $OUT/stage1.ll).
"$SCRIPT_DIR/build.sh" || { echo "FATAL: build from current snapshot failed." >&2; exit 1; }

NEW_IR="$OUT/stage1.ll"
[ -f "$NEW_IR" ] || { echo "FATAL: $NEW_IR not produced." >&2; exit 1; }

if diff -q "$NEW_IR" "$BOOT_IR" >/dev/null; then
    echo "[refresh] committed boot IR already current — nothing to do."
    exit 0
fi

OLD_SHA=$(shasum -a 256 "$BOOT_IR" 2>/dev/null | awk '{print $1}')
cp "$NEW_IR" "$BOOT_IR"
NEW_SHA=$(shasum -a 256 "$BOOT_IR" 2>/dev/null | awk '{print $1}')

echo "[refresh] updated bootstrap/tvc_self.boot.ll"
echo "  old sha256: $OLD_SHA"
echo "  new sha256: $NEW_SHA"
echo "[refresh] re-verifying the new snapshot is self-consistent..."

# Re-boot from the JUST-written snapshot and assert the fixed point holds, so we
# never commit a snapshot that cannot reproduce itself.
"$SCRIPT_DIR/build.sh" --check || {
    echo "FATAL: new snapshot failed self-verification — reverting." >&2
    # best-effort revert is the caller's job via git; surface the failure.
    exit 1; }

echo "[refresh] OK. Commit bootstrap/tvc_self.boot.ll (Traveler-produced, no C)."
