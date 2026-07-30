#!/bin/bash
# Standing probe for issue #15 (RESOLVED 2026-07-04).
#
# Originally filed as a multi-object -O2 "ICF fold hazard": two objects each emit
# a file-local `internal` field runtime, and -O2 identical-code-folding was
# hypothesized to mis-merge them and drop a reduction (Goldilocks 0-v wrong by
# 2^32-1). A cross-arch forensic sweep (aarch64+x86_64 x LLVM 15/18/19/20/21,
# incl. forced --icf=all) found that fold does NOT reproduce anywhere. The REAL
# defect was a `udiv i576` in __field_wide_init computing the i512 Barrett factor:
# llc lowers it to __udivei4 (arbitrary-width _BitInt division) on Linux/LLVM<=15,
# which no shipped libgcc/compiler-rt provides -> a HARD LINK FAILURE across the
# whole dyn-field surface (the dead wide-init is emitted into every program and
# never DCE'd on the `.tv -> llc -> .o` path). macOS/LLVM-21 lowered it to
# __udivti3 (Apple provides it), hence "latent on 21.x". Fixed by making the
# wide-init division-free (bit-serial long division; tvc_self.tv emit_widefield_init).
#
# This script is now a REGRESSION GUARD. Its load-bearing checks are toolchain-
# independent: assert the emitted construction path carries NO wide division at
# either tier — no `udiv/urem i128` in __field_init/__mr_powmod (they lower to the
# universally-provided __udivti3/__umodti3; removed by symmetry, #15 follow-up) and
# no `udiv i576` in __field_wide_init (it lowers to the __udivei4 link wall on
# Linux, invisible to a value/link check on macOS). It also builds BOTH ways per
# tier:
#   - candidate: separate `llc -O2` objects, linked with `cc -O2`.
#   - safe:      `llvm-link` the IR first (one definition), then llc -O2.
# Correct: 0-1 mod Goldilocks = p-1 (signed i64 -4294967296); wide BN254 = 1.
# FAILS if the SAFE path is wrong, if the wide-init regains a wide udiv, or if a
# candidate fails to LINK (a link wall, distinguished from a value divergence).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

# Toolchain
LLC="${LLC:-/opt/homebrew/opt/llvm@21/bin/llc}"
LLVM_LINK="${LLVM_LINK:-$(dirname "$LLC")/llvm-link}"

# --- host target: retarget IR objects + non-PIE link off-macOS ---
# Traveler-emitted IR text carries the canonical triple on every host (the
# byte-identity gates depend on that); execution overrides it at llc
# (-mtriple) and links with -no-pie where Linux defaults to PIE.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac
TVC_SELF="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$TVC_SELF" ]; then
  echo "FATAL: need src/bootstrap/out/stage1 (run src/bootstrap/build.sh)"; exit 1
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
GOLD_M1="-4294967296"   # p-1 as signed i64

echo "=== issue #15 fold probe (LLVM: $("$LLC" --version | sed -n 's/.*version //p' | head -1)) ==="

"$TVC_SELF" tests/foldbug/fold_a.tv -o "$T/a.ll" >/dev/null 2>&1
"$TVC_SELF" tests/foldbug/fold_b.tv -o "$T/b.ll" >/dev/null 2>&1

# Precondition: both objects emit the internal runtime (else the repro is moot).
ca=$(grep -c 'define internal i64 @field_dyn_sub' "$T/a.ll" || true)
cb=$(grep -c 'define internal i64 @field_dyn_sub' "$T/b.ll" || true)
echo "precondition: internal @field_dyn_sub in a=$ca b=$cb (both must be 1)"

# SAFE reference: llvm-link IR first.
"$LLVM_LINK" "$T/a.ll" "$T/b.ll" -o "$T/m.bc" >/dev/null 2>&1
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/m.bc" -o "$T/m.o" >/dev/null 2>&1
cc $LINK_PIE "$T/m.o" -o "$T/safe" >/dev/null 2>&1
safe_out="$("$T/safe" 2>/dev/null | tr '\n' ' ')"

# BUGGY candidate: separate -O2 objects + cc -O2 link.
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/a.ll" -o "$T/a.o" >/dev/null 2>&1
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/b.ll" -o "$T/b.o" >/dev/null 2>&1
cc -O2 $LINK_PIE "$T/a.o" "$T/b.o" -o "$T/cand" >/dev/null 2>&1
cand_out="$("$T/cand" 2>/dev/null | tr '\n' ' ')"

echo "safe (llvm-link first): $safe_out"
echo "cand (-O2 separate):    $cand_out"

rc=0
# Hard requirement: the SAFE path must be correct.
if [ "$safe_out" = "$GOLD_M1 $GOLD_M1 " ]; then
  echo "PASS: i128 safe path correct (0-1 == p-1 twice)"
else
  echo "FAIL: i128 safe path WRONG (expected '$GOLD_M1 $GOLD_M1')"; rc=1
fi
# Soft signal: the candidate path diverging means the fold hazard is live again.
if [ "$cand_out" = "$safe_out" ]; then
  echo "OK: i128 -O2 separate-object path matches safe path (fold hazard NOT present)"
else
  echo "WARN: i128 -O2 separate-object path DIVERGES — issue #15 fold hazard is LIVE."
  echo "      Workaround: llvm-link IR modules before llc -O2 (issue #15)."
fi

# --- #15 narrow-tier regression guard (toolchain-independent) ------------------
# Symmetric to the i512 wide-init guard below. The dyn-field CONSTRUCTION path
# (__field_init's Barrett factor + __mr_powmod's Miller-Rabin modmul) previously
# used udiv/urem i128, which llc lowers to the __udivti3/__umodti3 C division
# libcalls. Those are universally provided (never the #15 __udivei4 link wall),
# but the path was made libcall-free by symmetry (@internal-note: known-issues
# #15 follow-up) via the shared @__udivrem_128 bit-serial long division. A
# reintroduced i128 udiv/urem here would resurrect those libcalls; assert zero,
# and that the division-free helper loop (dv.head:) is present. The runtime is
# emitted into every program, so a.ll carries the full construction path.
ndiv=$(grep -Ec 'udiv i128|urem i128' "$T/a.ll" || true)
nloop=$(grep -c 'dv.head:' "$T/a.ll" || true)
if [ "$ndiv" = "0" ] && [ "$nloop" -ge 1 ]; then
  echo "PASS: i128 construction path division-free (no udiv/urem i128; @__udivrem_128 long-division loop present)"
else
  echo "FAIL: i128 construction path emits i128 udiv/urem (count=$ndiv loop=$nloop) — __udivti3/__umodti3 libcalls REINTRODUCED."; rc=1
fi

# --- i512 (multi-limb / BN254 Fr) tier: the hypothesized one-width-up fold ---
echo ""
echo "--- i512 wide tier (BN254 Fr) ---"
"$TVC_SELF" tests/foldbug/fold_wide_a.tv -o "$T/wa.ll" >/dev/null 2>&1
"$TVC_SELF" tests/foldbug/fold_wide_b.tv -o "$T/wb.ll" >/dev/null 2>&1
wca=$(grep -c 'define internal void @field_wide_sub' "$T/wa.ll" || true)
wcb=$(grep -c 'define internal void @field_wide_sub' "$T/wb.ll" || true)
echo "precondition: internal @field_wide_sub in a=$wca b=$wcb (both must be 1)"

# --- #15 regression guard (toolchain-independent) -----------------------------
# The wide-field carrier init MUST be division-free. A `udiv i576` in
# __field_wide_init lowers (via llc) to the __udivei4 arbitrary-width division
# libcall, which NO shipped Linux libgcc/compiler-rt provides -> the original
# #15 link failure across the whole dyn-field surface. macOS/LLVM-21 masks this
# (it lowers to __udivti3, which Apple provides), so the value/link checks below
# CANNOT see the regression on this host — this IR assertion is the real guard.
wdiv=$(grep -c 'udiv i576' "$T/wa.ll" || true)
wloop=$(grep -c 'ld.head:' "$T/wa.ll" || true)
if [ "$wdiv" = "0" ] && [ "$wloop" -ge 1 ]; then
  echo "PASS: i512 wide-init division-free (no udiv i576; long-division loop present)"
else
  echo "FAIL: i512 wide-init emits a wide udiv (udiv_i576=$wdiv loop=$wloop) — #15 __udivei4 link hazard REINTRODUCED."; rc=1
fi

"$LLVM_LINK" "$T/wa.ll" "$T/wb.ll" -o "$T/wm.bc" >/dev/null 2>&1
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/wm.bc" -o "$T/wm.o" >/dev/null 2>&1
cc $LINK_PIE "$T/wm.o" -o "$T/wsafe" >/dev/null 2>&1
wsafe_out="$("$T/wsafe" 2>/dev/null | tr '\n' ' ')"
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/wa.ll" -o "$T/wa.o" >/dev/null 2>&1
"$LLC" $LLC_TARGET -filetype=obj -O2 "$T/wb.ll" -o "$T/wb.o" >/dev/null 2>&1
cc -O2 $LINK_PIE "$T/wa.o" "$T/wb.o" -o "$T/wcand" >/dev/null 2>&1
wcand_out="$("$T/wcand" 2>/dev/null | tr '\n' ' ')"
echo "safe (llvm-link first): $wsafe_out"
echo "cand (-O2 separate):    $wcand_out"
if [ "$wsafe_out" = "1 1 " ]; then
  echo "PASS: i512 safe path correct (0-1 == Fr-1 twice, oracle-checked)"
else
  echo "FAIL: i512 safe path WRONG (expected '1 1')"; rc=1
fi
if [ ! -x "$T/wcand" ]; then
  echo "FAIL: i512 candidate FAILED TO LINK (a link wall, not a value fold) — see #15."; rc=1
elif [ "$wcand_out" = "$wsafe_out" ]; then
  echo "OK: i512 -O2 separate-object path links + matches safe path"
else
  echo "WARN: i512 -O2 separate-object path DIVERGES in value — unexpected; investigate."
fi
exit $rc
