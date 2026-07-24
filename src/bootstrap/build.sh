#!/bin/bash
# bootstrap/build.sh — build the Traveler compiler with NO C compiler in the
# trust chain (B4: drop the C seed).
#
# The trust root is bootstrap/tvc_self.boot.ll: LLVM IR produced BY Traveler
# compiling Traveler (the Stage-2 self-hosting fixed point — see refresh.sh).
# This is the standard self-hosting bootstrap (cf. rustc's committed snapshots):
# the compiler is written in its own language, and the first binary is built
# from a checked-in artifact of itself, not from a foreign-language seed.
#
# Pipeline (clang is used ONLY as the system linker — it compiles no source):
#
#   tvc_self.boot.ll  --llc-->  .o  --link-->  stage0   (the booted compiler)
#   stage0  compiles  src/tvc_self.tv  -->  stage1.ll  --llc/link-->  stage1
#   stage1  compiles  src/tvc_self.tv  -->  stage2.ll
#   assert stage1.ll == stage2.ll          (self-hosting fixed point reached
#                                            from the committed IR, no C)
#
# The result is bootstrap/out/stage1 — the canonical compiler, identical to the
# one the C seed would have produced. src-legacy/tvc.c remains in the tree purely as an
# optional provenance/audit path (see bootstrap/PROVENANCE.md); it is not used
# here and is not required to build Traveler.
#
# Usage:
#   bootstrap/build.sh                 # build for the host
#   bootstrap/build.sh --target TRIPLE # cross-build (retargets the boot IR)
#   bootstrap/build.sh --check         # also assert the committed IR is fresh
#
# Env: LLC (path to llvm-21 llc), LINK (linker driver, default: cc).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"   # .../src/bootstrap
SRC_DIR="$(dirname "$SCRIPT_DIR")"            # .../src
REPO_DIR="$(dirname "$SRC_DIR")"              # repo root
BOOT_IR="$SCRIPT_DIR/tvc_self.boot.ll"
SRC_TV="$REPO_DIR/src/tvc_self.tv"       # the compiler source
OUT="$SCRIPT_DIR/out"

TARGET=""
CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --check)  CHECK=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- locate llc (LLVM 21) ---
find_llc() {
    if [ -n "${LLC:-}" ] && command -v "$LLC" &>/dev/null; then return; fi
    for p in \
        /opt/homebrew/opt/llvm@21/bin/llc \
        /usr/local/opt/llvm@21/bin/llc \
        /usr/lib/llvm-21/bin/llc \
        llc-21 llc; do
        if command -v "$p" &>/dev/null; then LLC="$p"; return; fi
    done
    echo "FATAL: llc (LLVM 21) not found. Set \$LLC." >&2; exit 1
}
find_llc

# Linker driver: any C toolchain's cc works purely as a linker (it links .o ->
# executable; it compiles no source). cc/clang/gcc are interchangeable here.
LINK="${LINK:-cc}"
command -v "$LINK" >/dev/null 2>&1 || { echo "FATAL: linker '$LINK' not found." >&2; exit 1; }

[ -f "$BOOT_IR" ] || { echo "FATAL: missing $BOOT_IR" >&2; exit 1; }

mkdir -p "$OUT"

# --- host/target selection ---
# All Traveler-emitted IR (and the committed snapshot) carries the canonical
# triple; the byte-identity gates depend on that text being host-independent.
# Execution retargets at llc: on a non-macOS host we auto-select the host
# triple (or honor an explicit --target) and override with -mtriple. Linux
# links need -no-pie (Traveler codegen is non-PIC; distro cc defaults to PIE).
EXPLICIT_TARGET="$TARGET"
LINK_PIE=""
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  [ -z "$TARGET" ] && TARGET="x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) [ -z "$TARGET" ] && TARGET="aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
esac

# --- prepare the boot IR for this host ---
# The committed IR pins a triple for direct dev-platform use. For a foreign
# host, strip the triple and let -mtriple drive codegen (the IR has no
# datalayout line, so it is host-neutral once the triple is removed).
BOOT_USE="$OUT/boot.ll"
MTRIPLE_FLAG=""
if [ -n "$TARGET" ]; then
    grep -v '^target triple' "$BOOT_IR" > "$BOOT_USE"
    MTRIPLE_FLAG="-mtriple=$TARGET"
    if [ -n "$EXPLICIT_TARGET" ]; then
        echo "[build] retargeting boot IR -> $TARGET"
    else
        echo "[build] host target -> $TARGET (auto-detected)"
    fi
else
    cp "$BOOT_IR" "$BOOT_USE"
fi

echo "[build] llc: $LLC"
echo "[build] link: $LINK (linker only — compiles no source)"

# --- stage0: the booted compiler, straight from the committed IR ---
echo "[build] stage0: committed IR -> native (no C source compiled)"
"$LLC" $MTRIPLE_FLAG -filetype=obj "$BOOT_USE" -o "$OUT/stage0.o" || exit 1
"$LINK" $LINK_PIE "$OUT/stage0.o" -o "$OUT/stage0" || exit 1

# --- stage1: stage0 compiles the current source ---
# stage1.ll keeps the canonical triple text (stage0 is not passed -target);
# $MTRIPLE_FLAG overrides at codegen so the binary is host-native.
echo "[build] stage1: stage0 compiles src/tvc_self.tv"
"$OUT/stage0" "$SRC_TV" -o "$OUT/stage1.ll" >/dev/null 2>&1 || {
    echo "FATAL: stage0 failed to compile $SRC_TV" >&2; exit 1; }
"$LLC" $MTRIPLE_FLAG -filetype=obj "$OUT/stage1.ll" -o "$OUT/stage1.o" || exit 1
"$LINK" $LINK_PIE "$OUT/stage1.o" -o "$OUT/stage1" || exit 1

# --- stage2: stage1 compiles the current source; assert fixed point ---
echo "[build] stage2: stage1 compiles itself; asserting fixed point"
"$OUT/stage1" "$SRC_TV" -o "$OUT/stage2.ll" >/dev/null 2>&1 || {
    echo "FATAL: stage1 failed to compile $SRC_TV" >&2; exit 1; }

if ! diff -q "$OUT/stage1.ll" "$OUT/stage2.ll" >/dev/null; then
    echo "FATAL: fixed point NOT reached (stage1 != stage2)." >&2
    echo "  The committed boot IR may be incompatible with current source." >&2
    echo "  Run bootstrap/refresh.sh after intended source changes." >&2
    exit 1
fi
echo "[build] OK: fixed point reached with ZERO C source compiled."
echo "[build] canonical compiler -> $OUT/stage1"

# --- optional: assert the committed boot IR is itself fresh ---
if [ "$CHECK" -eq 1 ]; then
    # The booted compiler, compiling current source, must reproduce the
    # committed IR exactly. The emitted TEXT carries the canonical triple on
    # every host (only codegen is retargeted), so the freshness diff is valid
    # under host auto-detection; it is skipped only for an explicit --target
    # cross-build request.
    if [ -n "$EXPLICIT_TARGET" ]; then
        echo "[check] skipped freshness diff under --target (triple differs)"
    else
        if diff -q "$OUT/stage1.ll" "$BOOT_IR" >/dev/null; then
            echo "[check] OK: committed boot IR is fresh (== current source output)."
        else
            echo "FATAL: committed boot IR is STALE." >&2
            echo "  bootstrap/tvc_self.boot.ll != stage0(src/tvc_self.tv)." >&2
            echo "  Run bootstrap/refresh.sh and commit the result." >&2
            exit 1
        fi
    fi
fi
