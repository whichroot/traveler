#!/bin/bash
# Dynamic-field test (Phases 1+2+3).
#
# Phase 2 landed the .tv surface: field(p) builtin, instantiate <dyn>,
# @field_dyn_* dispatch in generic bodies. This test compiles
# examples/dyn_kernel_test.tv — the four operations dyn-instantiated,
# plus the Phase 2 gate: pc_sum<dyn> element-identical to a faithful
# copy of the codec's pc_forward_sum (i32 + FieldCtx path) across
# three primes {251, 65521, 2013265921}, with width coverage through
# 16-bit and 32-bit primes.
#
# Phase 3: Register<dyn, d> — fat register carrying its own Field carrier.
# examples/dyn_register_test.tv — explicit carrier, method syntax,
# one-source-two-paths parity, regime detection over dyn, width coverage.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
SRC_DIR="$REPO_DIR/src-legacy"

# Resolve a .tv by basename across the demo + library + tool trees. Demos live
# in examples/, reusable kernels in src/lib/<subsys>/, tools in src/tools/.
tv() {
    local name="$1"
    if [ -f "$REPO_DIR/examples/${name}.tv" ]; then echo "$REPO_DIR/examples/${name}.tv"; return 0; fi
    local f
    f=$(find "$REPO_DIR/src/lib" "$REPO_DIR/src/tools" -name "${name}.tv" 2>/dev/null | head -1)
    if [ -n "$f" ]; then echo "$f"; return 0; fi
    echo "$REPO_DIR/examples/${name}.tv"
    return 0
}

find_llc() {
    if [ -n "${LLC:-}" ] && command -v "$LLC" &>/dev/null; then return; fi
    for p in /opt/homebrew/opt/llvm@21/bin/llc /usr/local/opt/llvm@21/bin/llc \
             /usr/lib/llvm-21/bin/llc llc-21 llc; do
        if command -v "$p" &>/dev/null; then LLC="$p"; return; fi
    done
    echo "FATAL: llc not found" >&2; exit 1
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

(cd "$SRC_DIR" && make tvc >/dev/null 2>&1) || exit 1
TVC="$SRC_DIR/tvc"

TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

FAILURES=""

# ---- Phase 2 gate: dyn_kernel_test ----

EXPECTED_DKT="10
13
17
22
73
73
3
10
3
1
4
1
1
1
54479
705032709"

"$TVC" "$REPO_DIR/examples/dyn_kernel_test.tv" -o "$TMP/dkt.ll" >/dev/null 2>&1 || {
    echo "dynfield-kernel: FAIL (compile)"; FAILURES="$FAILURES kernel"; }
if [ -z "$FAILURES" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dkt.ll" -o "$TMP/dkt.o" 2>/dev/null || {
    echo "dynfield-kernel: FAIL (llc)"; FAILURES="$FAILURES kernel"; }
fi
if [ -z "$FAILURES" ]; then
clang $LINK_PIE "$TMP/dkt.o" -o "$TMP/dkt" 2>/dev/null || {
    echo "dynfield-kernel: FAIL (link)"; FAILURES="$FAILURES kernel"; }
fi
if [ -z "$FAILURES" ]; then
    GOT=$("$TMP/dkt")
    if [ "$GOT" = "$EXPECTED_DKT" ]; then
        echo "dynfield-kernel: PASS (4 operations dyn-instantiated; pc_forward_sum parity x3 primes)"
    else
        echo "dynfield-kernel: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DKT"
        echo "--- got ---"; echo "$GOT"
        FAILURES="$FAILURES kernel"
    fi
fi

# ---- Phase 3 gate: dyn_register_test ----

EXPECTED_DRT="10
14
20
28
38
50
10
14
20
28
38
50
1
4
50
53
56
54430
705032619"

"$TVC" "$REPO_DIR/examples/dyn_register_test.tv" -o "$TMP/drt.ll" >/dev/null 2>&1 || {
    echo "dynfield-register: FAIL (compile)"; FAILURES="$FAILURES register"; }
if echo "$FAILURES" | grep -qv register 2>/dev/null; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/drt.ll" -o "$TMP/drt.o" 2>/dev/null || {
    echo "dynfield-register: FAIL (llc)"; FAILURES="$FAILURES register"; }
fi
if echo "$FAILURES" | grep -qv register 2>/dev/null; then
clang $LINK_PIE "$TMP/drt.o" -o "$TMP/drt" 2>/dev/null || {
    echo "dynfield-register: FAIL (link)"; FAILURES="$FAILURES register"; }
fi
if echo "$FAILURES" | grep -qv register 2>/dev/null; then
    GOT=$("$TMP/drt")
    if [ "$GOT" = "$EXPECTED_DRT" ]; then
        echo "dynfield-register: PASS (Register<dyn,d> explicit+method+parity+regime+width)"
    else
        echo "dynfield-register: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DRT"
        echo "--- got ---"; echo "$GOT"
        FAILURES="$FAILURES register"
    fi
fi

# ---- Phase 5a gate: dyn_nested_test ----
# Nested generic calls inside a dyn instance + field(p, data_max)
# arity-2 + carrier member access (f.p/half_p/elem_bytes/data_bytes).

EXPECTED_DNT="216
43445
251
125
1
1
65537
32768
4
2"

"$TVC" "$REPO_DIR/examples/dyn_nested_test.tv" -o "$TMP/dnt.ll" >/dev/null 2>&1 || {
    echo "dynfield-nested: FAIL (compile)"; FAILURES="$FAILURES nested"; }
if echo "$FAILURES" | grep -qv nested 2>/dev/null; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dnt.ll" -o "$TMP/dnt.o" 2>/dev/null || {
    echo "dynfield-nested: FAIL (llc)"; FAILURES="$FAILURES nested"; }
fi
if echo "$FAILURES" | grep -qv nested 2>/dev/null; then
clang $LINK_PIE "$TMP/dnt.o" -o "$TMP/dnt" 2>/dev/null || {
    echo "dynfield-nested: FAIL (link)"; FAILURES="$FAILURES nested"; }
fi
if echo "$FAILURES" | grep -qv nested 2>/dev/null; then
    GOT=$("$TMP/dnt")
    if [ "$GOT" = "$EXPECTED_DNT" ]; then
        echo "dynfield-nested: PASS (nested dyn calls + field(p,data_max) + carrier members)"
    else
        echo "dynfield-nested: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DNT"
        echo "--- got ---"; echo "$GOT"
        FAILURES="$FAILURES nested"
    fi
fi

# ---- Phase 7a gate: dyn_ntt_test (tvc_self ONLY) ----
# Dynamic-field NTT is the first capability with no bootstrap counterpart:
# the NTT root chain is computed at runtime by @__ntt_init and travels in
# the Field carrier.  tvc.c is frozen and has no dyn-NTT support, so this
# gate builds Stage 1 tvc_self and compiles with it.

EXPECTED_DYN_NTT="1
1
1
1
1"

NTT_OK=1
# Build Stage 1 tvc_self (seed compiles the canonical compiler).
"$TVC" "$REPO_DIR/src/tvc_self.tv" -o "$TMP/tvc_self.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/tvc_self.ll" -o "$TMP/tvc_self.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/tvc_self.o" -o "$TMP/tvc_self" 2>/dev/null || {
    echo "dynfield-ntt: FAIL (Stage 1 tvc_self build)"; NTT_OK=0; }

if [ "$NTT_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_ntt_test.tv" -o "$TMP/dntt.ll" >/dev/null 2>&1 || {
    echo "dynfield-ntt: FAIL (compile)"; NTT_OK=0; }
fi
if [ "$NTT_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dntt.ll" -o "$TMP/dntt.o" 2>/dev/null || {
    echo "dynfield-ntt: FAIL (llc)"; NTT_OK=0; }
fi
if [ "$NTT_OK" = "1" ]; then
clang $LINK_PIE "$TMP/dntt.o" -o "$TMP/dntt" 2>/dev/null || {
    echo "dynfield-ntt: FAIL (link)"; NTT_OK=0; }
fi
if [ "$NTT_OK" = "1" ]; then
    GOT=$("$TMP/dntt")
    if [ "$GOT" = "$EXPECTED_DYN_NTT" ]; then
        echo "dynfield-ntt: PASS (dyn NTT round-trip + poly_mul x2 runtime primes; roots in carrier)"
    else
        echo "dynfield-ntt: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DYN_NTT"
        echo "--- got ---"; echo "$GOT"
        NTT_OK=0
    fi
fi
if [ "$NTT_OK" = "0" ]; then FAILURES="$FAILURES ntt"; fi

# ---- E(a-ii) gate: dyn_mul_boundary_test (tvc_self ONLY) ----
# @field_dyn_mul narrow-prime fast path: for p <= 2^32, a*b < 2^64 fits i64
# exactly, so one urem replaces the i256 Barrett (the codec hot path).  This
# via-negativa probe pins the boundary: the LARGEST narrow prime (2^32-5,
# tightest non-overflowing square) cross-validates mul against the untouched
# add path, the (p-1)^2==1 corner exposes any top-bit error, and a wide prime
# (Goldilocks) confirms the i256 path is unchanged.  Reuses Stage 1 tvc_self.

EXPECTED_DMB="4294966291
4294966291
1
1
18446744069414584319
171
52590"

MB_OK=1
if [ ! -x "$TMP/tvc_self" ]; then MB_OK=0; fi
if [ "$MB_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_mul_boundary_test.tv" -o "$TMP/dmb.ll" >/dev/null 2>&1 || {
    echo "dynfield-mul-boundary: FAIL (compile)"; MB_OK=0; }
fi
if [ "$MB_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dmb.ll" -o "$TMP/dmb.o" 2>/dev/null || {
    echo "dynfield-mul-boundary: FAIL (llc)"; MB_OK=0; }
fi
if [ "$MB_OK" = "1" ]; then
clang $LINK_PIE "$TMP/dmb.o" -o "$TMP/dmb" 2>/dev/null || {
    echo "dynfield-mul-boundary: FAIL (link)"; MB_OK=0; }
fi
if [ "$MB_OK" = "1" ]; then
    GOT=$("$TMP/dmb")
    if [ "$GOT" = "$EXPECTED_DMB" ]; then
        echo "dynfield-mul-boundary: PASS (narrow fast path == add-path at 2^32-5; (p-1)^2=1 corner; wide i256 unchanged)"
    else
        echo "dynfield-mul-boundary: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DMB"
        echo "--- got ---"; echo "$GOT"
        MB_OK=0
    fi
fi
if [ "$MB_OK" = "0" ]; then FAILURES="$FAILURES mul-boundary"; fi

# ---- Phase 7b gate: dyn_poseidon2_test (tvc_self ONLY) ----
# Dynamic-field Poseidon2 permutation: the generic core (S-box x^7,
# external/internal MDS) compiled <dyn>, run over field(Goldilocks) with the
# Plonky3 round constants, reproducing the Plonky3 test vector exactly.
# Reuses the Stage 1 tvc_self built above.

EXPECTED_DYN_P2="1
1
1"

P2_OK=1
if [ ! -x "$TMP/tvc_self" ]; then P2_OK=0; fi   # depends on Stage 1 build above
if [ "$P2_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_poseidon2_test.tv" -o "$TMP/dp2.ll" >/dev/null 2>&1 || {
    echo "dynfield-poseidon2: FAIL (compile)"; P2_OK=0; }
fi
if [ "$P2_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dp2.ll" -o "$TMP/dp2.o" 2>/dev/null || {
    echo "dynfield-poseidon2: FAIL (llc)"; P2_OK=0; }
fi
if [ "$P2_OK" = "1" ]; then
clang $LINK_PIE "$TMP/dp2.o" -o "$TMP/dp2" 2>/dev/null || {
    echo "dynfield-poseidon2: FAIL (link)"; P2_OK=0; }
fi
if [ "$P2_OK" = "1" ]; then
    GOT=$("$TMP/dp2")
    if [ "$GOT" = "$EXPECTED_DYN_P2" ]; then
        echo "dynfield-poseidon2: PASS (dyn permutation + field-term diag match Plonky3 Goldilocks vector)"
    else
        echo "dynfield-poseidon2: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DYN_P2"
        echo "--- got ---"; echo "$GOT"
        P2_OK=0
    fi
fi
if [ "$P2_OK" = "0" ]; then FAILURES="$FAILURES poseidon2"; fi

# ---- B-Grain gate: grain_lfsr_test (tvc_self ONLY, 2-file link) ----
# Runtime Grain-LFSR Poseidon2 round-constant generator: replaces the 86
# hardcoded Goldilocks constants with a generator over ANY runtime prime.
# Verified bit-for-bit against Plonky3 over TWO primes that exercise the
# rejection-sampling gap differently:
#   Goldilocks (n=64): reject prob ~2^-32 — rejection NEVER fires (blind to the
#     prime-dependent path; also the case that needs UNSIGNED v<p, since valid
#     elements in [2^63,p) are negative as signed i64).
#   BabyBear (n=31): reject prob 6.25% — rejection FIRES (the load-bearing test
#     that the prime-dependent draw is honest, which Goldilocks cannot show).
# Each prime: ext_init[0] + internal[0] (internal[0]=raw[(RF/2)*t], testing the
# raw-stream layout). Expected 1 1 1 1.

EXPECTED_GRAIN="1
1
1
1"

GR_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GR_OK=0; fi
if [ "$GR_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/gl.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/grain_lfsr_test.tv" -o "$TMP/glt.ll" >/dev/null 2>&1 || {
    echo "dynfield-grain: FAIL (compile)"; GR_OK=0; }
fi
if [ "$GR_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gl.ll" -o "$TMP/gl.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/glt.ll" -o "$TMP/glt.o" 2>/dev/null || {
    echo "dynfield-grain: FAIL (llc)"; GR_OK=0; }
fi
if [ "$GR_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gl.o" "$TMP/glt.o" -o "$TMP/glt" 2>/dev/null || {
    echo "dynfield-grain: FAIL (link)"; GR_OK=0; }
fi
if [ "$GR_OK" = "1" ]; then
    GOT=$("$TMP/glt")
    if [ "$GOT" = "$EXPECTED_GRAIN" ]; then
        echo "dynfield-grain: PASS (Grain-LFSR RC generator == Plonky3 over Goldilocks no-reject + BabyBear 6.25%-reject)"
    else
        echo "dynfield-grain: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GRAIN"
        echo "--- got ---"; echo "$GOT"
        GR_OK=0
    fi
fi
if [ "$GR_OK" = "0" ]; then FAILURES="$FAILURES grain"; fi

# ---- B-Grain functional gate: grain_poseidon2_test (tvc_self ONLY, 2-file) ----
# END-TO-END: generate the Goldilocks RC at runtime via Grain-LFSR, feed them
# through the dyn Poseidon2 permutation, match the Plonky3 width-8 test vector.
# This proves the generator can REPLACE the hardcoded fill_rc with zero
# behaviour change (bit-match + functional-match). Expected 1.

EXPECTED_GP2="1"

GP2_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GP2_OK=0; fi
if [ "$GP2_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/gpl.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/grain_poseidon2_test.tv" -o "$TMP/gp2.ll" >/dev/null 2>&1 || {
    echo "dynfield-grain-poseidon2: FAIL (compile)"; GP2_OK=0; }
fi
if [ "$GP2_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gpl.ll" -o "$TMP/gpl.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gp2.ll" -o "$TMP/gp2.o" 2>/dev/null || {
    echo "dynfield-grain-poseidon2: FAIL (llc)"; GP2_OK=0; }
fi
if [ "$GP2_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gpl.o" "$TMP/gp2.o" -o "$TMP/gp2" 2>/dev/null || {
    echo "dynfield-grain-poseidon2: FAIL (link)"; GP2_OK=0; }
fi
if [ "$GP2_OK" = "1" ]; then
    GOT=$("$TMP/gp2")
    if [ "$GOT" = "$EXPECTED_GP2" ]; then
        echo "dynfield-grain-poseidon2: PASS (runtime-generated RC drive dyn Poseidon2 to Plonky3 vector; fill_rc replaceable)"
    else
        echo "dynfield-grain-poseidon2: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GP2"
        echo "--- got ---"; echo "$GOT"
        GP2_OK=0
    fi
fi
if [ "$GP2_OK" = "0" ]; then FAILURES="$FAILURES grain-poseidon2"; fi

# ---- B-MDS gate: mds_check_test (tvc_self ONLY, 2-file link) ----
# Poseidon2 internal-matrix MDS validity over a runtime prime + a generator that
# finds a valid diagonal for ANY prime. Completes parameter-genericity: B-Grain
# made the round CONSTANTS prime-generic; this makes the internal DIAGONAL
# prime-generic. THE FINDING: the repo's hardcoded Goldilocks diagonal is
# MDS-valid over Goldilocks but NOT over BabyBear (it fails the invariant-
# subspace condition at the first power) — the dyn stack transported a
# Goldilocks-specific parameter as if it were generic. Validity is a ~12.5%
# Chebotarev sieve, not a threshold. The generator (grain-sampled + rejection
# until the check passes) repairs it. Pure dyn poly/matrix machinery (schoolbook
# poly mul — NOT poly_mul_ntt, which needs NTT-friendly primes; degree <= 2t
# works over any prime), Ben-Or irreducibility via UNSIGNED Frobenius (same
# width boundary as dyn-mul / grain). Expected 1 0 1 1 1.

EXPECTED_MDS="1
0
1
1
1"

MDS_OK=1
if [ ! -x "$TMP/tvc_self" ]; then MDS_OK=0; fi
if [ "$MDS_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/mds_check.tv" -o "$TMP/mds.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/mgl.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/mds_check_test.tv" -o "$TMP/mct.ll" >/dev/null 2>&1 || {
    echo "dynfield-mds: FAIL (compile)"; MDS_OK=0; }
fi
if [ "$MDS_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/mds.ll" -o "$TMP/mds.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mgl.ll" -o "$TMP/mgl.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mct.ll" -o "$TMP/mct.o" 2>/dev/null || {
    echo "dynfield-mds: FAIL (llc)"; MDS_OK=0; }
fi
if [ "$MDS_OK" = "1" ]; then
clang $LINK_PIE "$TMP/mds.o" "$TMP/mgl.o" "$TMP/mct.o" -o "$TMP/mct" 2>/dev/null || {
    echo "dynfield-mds: FAIL (link)"; MDS_OK=0; }
fi
if [ "$MDS_OK" = "1" ]; then
    GOT=$("$TMP/mct")
    if [ "$GOT" = "$EXPECTED_MDS" ]; then
        echo "dynfield-mds: PASS (repo diag valid@Goldilocks/INVALID@BabyBear; generator repairs; official BB w16 sound; generated!=repo)"
    else
        echo "dynfield-mds: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_MDS"
        echo "--- got ---"; echo "$GOT"
        MDS_OK=0
    fi
fi
if [ "$MDS_OK" = "0" ]; then FAILURES="$FAILURES mds"; fi

# ---- B-MDS LIVE gate: mds_live_test (tvc_self ONLY, 3-file link) ----
# The validate-then-generate diagonal selector (poseidon2_diag_checked) on the
# ACTUAL dyn Poseidon2 permutation path. Closes the suite's blind spot: every
# other dyn crypto gate asserts prove/verify SELF-CONSISTENCY (1 2 1 2), which
# holds for ANY diagonal because MDS is a SOUNDNESS property, not a correctness
# one — so those gates pass over BabyBear even WITH the broken non-MDS canonical
# diagonal. This gate routes the diagonal through the name-blind selector (derive
# canonical tuple -> MDS-check -> use verbatim if valid, else grain-repair) and
# asserts the SELECTED diagonal is MDS-valid AND drives a real permutation.
# Goldilocks: canonical path (0 tries), permutation matches Plonky3 width-8
# vector byte-for-byte (the old behaviour is preserved exactly). BabyBear: repair
# path (>0 tries), repaired diagonal is MDS-valid + differs from canonical +
# drives a permutation to completion. The algebra (the ~12.5% irreducibility
# sieve), not the prime's name, decides which path is taken. Expected
# 1 0 1 1 1 1 1 1.

EXPECTED_MDS_LIVE="1
0
1
1
1
1
1
1"

MDSL_OK=1
if [ ! -x "$TMP/tvc_self" ]; then MDSL_OK=0; fi
# Reuse mds.o + mgl.o (grain_lfsr) built by the mds gate above if present.
if [ "$MDSL_OK" = "1" ] && [ ! -f "$TMP/mds.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/mds_check.tv" -o "$TMP/mds.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mds.ll" -o "$TMP/mds.o" 2>/dev/null || MDSL_OK=0
fi
if [ "$MDSL_OK" = "1" ] && [ ! -f "$TMP/mgl.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/mgl.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mgl.ll" -o "$TMP/mgl.o" 2>/dev/null || MDSL_OK=0
fi
if [ "$MDSL_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/mds_live_test.tv" -o "$TMP/mlt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mlt.ll" -o "$TMP/mlt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/mds.o" "$TMP/mgl.o" "$TMP/mlt.o" -o "$TMP/mlt" 2>/dev/null || {
    echo "dynfield-mds-live: FAIL (build)"; MDSL_OK=0; }
fi
if [ "$MDSL_OK" = "1" ]; then
    GOT=$("$TMP/mlt")
    if [ "$GOT" = "$EXPECTED_MDS_LIVE" ]; then
        echo "dynfield-mds-live: PASS (selector drives real permutation; Goldilocks canonical==Plonky3 vector, BabyBear repaired+MDS+differs)"
    else
        echo "dynfield-mds-live: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_MDS_LIVE"
        echo "--- got ---"; echo "$GOT"
        MDSL_OK=0
    fi
fi
if [ "$MDSL_OK" = "0" ]; then FAILURES="$FAILURES mds-live"; fi

# ---- B-Width gate (Stage A): poseidon2_wide_test (tvc_self ONLY, 4-file link) ----
# The width-generic dyn Poseidon2 permutation (poseidon2_wide.tv) exercised at two
# widths. The prime-genericity arc (B-Grain constants, B-MDS diagonal) made the
# permutation PARAMETER-generic over the field; this makes it WIDTH-generic, with
# the width threaded as a RUNTIME i32 (the language has no numeric const generics,
# so <dyn,16> is inexpressible — width travels as an argument). The external linear
# layer is refactored from the hand-unrolled mds_external_8 into Plonky3's
# mds_light_permutation form (MDSMat4 per 4-chunk + outer circulant). Test 1 proves
# that refactor is byte-exact at t=8 (reproduces the Plonky3 Goldilocks vector).
# Test 2 is the math-generic width claim: a generated MDS-valid width-16 diagonal
# over BabyBear (R_P=13) drives the width-16 permutation to completion with a
# provably MDS-sound internal layer. (Plonky3-specific BabyBear-16 interop is the
# separate Stage B gate.) Expected 1 1 1.

EXPECTED_PWIDE="1
1
1"

PW_OK=1
if [ ! -x "$TMP/tvc_self" ]; then PW_OK=0; fi
# Reuse mds.o + mgl.o (grain_lfsr) built by the mds gates above if present.
if [ "$PW_OK" = "1" ] && [ ! -f "$TMP/mds.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/mds_check.tv" -o "$TMP/mds.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mds.ll" -o "$TMP/mds.o" 2>/dev/null || PW_OK=0
fi
if [ "$PW_OK" = "1" ] && [ ! -f "$TMP/mgl.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/mgl.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mgl.ll" -o "$TMP/mgl.o" 2>/dev/null || PW_OK=0
fi
if [ "$PW_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/poseidon2_wide.tv" -o "$TMP/pw.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pw.ll" -o "$TMP/pw.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/poseidon2_wide_test.tv" -o "$TMP/pwt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pwt.ll" -o "$TMP/pwt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/pw.o" "$TMP/mds.o" "$TMP/mgl.o" "$TMP/pwt.o" -o "$TMP/pwt" 2>/dev/null || {
    echo "dynfield-poseidon2-wide: FAIL (build)"; PW_OK=0; }
fi
if [ "$PW_OK" = "1" ]; then
    GOT=$("$TMP/pwt")
    if [ "$GOT" = "$EXPECTED_PWIDE" ]; then
        echo "dynfield-poseidon2-wide: PASS (width-generic permute: t=8 byte-exact vs Plonky3 Goldilocks; t=16 BabyBear MDS-sound liveness, R_P=13)"
    else
        echo "dynfield-poseidon2-wide: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_PWIDE"
        echo "--- got ---"; echo "$GOT"
        PW_OK=0
    fi
fi
if [ "$PW_OK" = "0" ]; then FAILURES="$FAILURES poseidon2-wide"; fi

# ---- B-Width gate (Stage B): poseidon2_babybear_test (tvc_self ONLY, 4-file) ----
# INTEROP-compatible BabyBear width-16 Poseidon2, pinned bit-for-bit to the
# canonical Plonky3 reference (baby-bear/src/poseidon2.rs,
# test_default_babybear_poseidon2_width_16). Stage A proved the width-generic
# permutation RUNS at t=16 with a math-generic diagonal; Stage B proves it
# INTEROPERATES — with Plonky3's specific published constants and diagonal, the
# SAME poseidon2_permute_w reproduces Plonky3's published test vector. The
# reference is cross-checked by @internal-oracle: poseidon2_t16, which
# pins TWO independent facts against the Rust source (grain == published RC_16,
# permutation == published vector). The t=16 diagonal has a closed form (powers-
# of-2 inverses: 1/2^8, 1/4, ... ), so the gate DERIVES it in-field, asserts it
# equals the published values AND is MDS-valid, then matches the permutation
# output bit-for-bit. Expected 1 1 1.

EXPECTED_PBB="1
1
1"

PBB_OK=1
if [ ! -x "$TMP/tvc_self" ]; then PBB_OK=0; fi
# Reuse pw.o (poseidon2_wide), mds.o, mgl.o (grain_lfsr) from the gates above.
if [ "$PBB_OK" = "1" ] && [ ! -f "$TMP/pw.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/poseidon2_wide.tv" -o "$TMP/pw.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pw.ll" -o "$TMP/pw.o" 2>/dev/null || PBB_OK=0
fi
if [ "$PBB_OK" = "1" ] && [ ! -f "$TMP/mds.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/mds_check.tv" -o "$TMP/mds.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mds.ll" -o "$TMP/mds.o" 2>/dev/null || PBB_OK=0
fi
if [ "$PBB_OK" = "1" ] && [ ! -f "$TMP/mgl.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/grain_lfsr.tv" -o "$TMP/mgl.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mgl.ll" -o "$TMP/mgl.o" 2>/dev/null || PBB_OK=0
fi
if [ "$PBB_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/poseidon2_babybear_test.tv" -o "$TMP/pbt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pbt.ll" -o "$TMP/pbt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/pw.o" "$TMP/mds.o" "$TMP/mgl.o" "$TMP/pbt.o" -o "$TMP/pbt" 2>/dev/null || {
    echo "dynfield-poseidon2-babybear: FAIL (build)"; PBB_OK=0; }
fi
if [ "$PBB_OK" = "1" ]; then
    GOT=$("$TMP/pbt")
    if [ "$GOT" = "$EXPECTED_PBB" ]; then
        echo "dynfield-poseidon2-babybear: PASS (interop: derived closed-form diag == published BB16 + MDS-valid; permute_w == Plonky3 published width-16 vector bit-for-bit)"
    else
        echo "dynfield-poseidon2-babybear: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_PBB"
        echo "--- got ---"; echo "$GOT"
        PBB_OK=0
    fi
fi
if [ "$PBB_OK" = "0" ]; then FAILURES="$FAILURES poseidon2-babybear"; fi

# ---- Thread-1A gate: zk_range_test (tvc_self ONLY, 3-file link) ----
# The FIRST ordering primitive in the dyn ZK stack. Everything prior is
# equality/nonzero (boundary = err*inv==1, MDS = irreducibility, FIT = difference
# vanishing); a finite field has no order, so a <= b needs bit-decomposition. The
# range gadget (zk_range.tv) hand-builds a PLONK circuit proving 0 <= x < 2^nbits
# via boolean-constraint gates (b_j*(b_j-1)=0) + a reconstruction chain bound to
# x, then leq(a,b) := range-prove (b-a). Built the runtime_circuit.tv way (gates
# + union-find sigma into caller arrays, handed to plonk_prove/verify_dyn) — ZERO
# compiler change. Cross-checked by @internal-oracle: zk_range (gate
# semantics + leq truth table over both primes). Per prime: honest range proof of
# 100 verifies (1), witness tamper rejected (2 = dyn PLONK gate-eq fail code),
# leq(3,5) verifies (1), leq(5,3) out-of-range rejected (0). Expected 1 2 1 0 x2.

EXPECTED_ZKRANGE="1
2
1
0
1
2
1
0"

ZR_OK=1
if [ ! -x "$TMP/tvc_self" ]; then ZR_OK=0; fi
# Reuse plonk_dyn.o if a prior gate built it; else build it.
if [ "$ZR_OK" = "1" ] && [ ! -f "$TMP/plonk_dyn.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/plonk_dyn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/plonk_dyn.ll" -o "$TMP/plonk_dyn.o" 2>/dev/null || ZR_OK=0
fi
if [ "$ZR_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/zk/zk_range.tv" -o "$TMP/zkr.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zkr.ll" -o "$TMP/zkr.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/zk_range_test.tv" -o "$TMP/zkrt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zkrt.ll" -o "$TMP/zkrt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/plonk_dyn.o" "$TMP/zkr.o" "$TMP/zkrt.o" -o "$TMP/zkrt" 2>/dev/null || {
    echo "dynfield-zk-range: FAIL (build)"; ZR_OK=0; }
fi
if [ "$ZR_OK" = "1" ]; then
    GOT=$("$TMP/zkrt")
    if [ "$GOT" = "$EXPECTED_ZKRANGE" ]; then
        echo "dynfield-zk-range: PASS (first ZK ordering primitive: bit-decomp range proof + leq, honest verify / tamper-reject / out-of-range-reject, Goldilocks+BabyBear)"
    else
        echo "dynfield-zk-range: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_ZKRANGE"
        echo "--- got ---"; echo "$GOT"
        ZR_OK=0
    fi
fi
if [ "$ZR_OK" = "0" ]; then FAILURES="$FAILURES zk-range"; fi

# ---- Thread-1B gate: genus_global_certify (tvc_self ONLY, 5-file link) ----
# GLOBAL MDL-optimality — closes the deferred claim of spec 17.11.4. The genus
# adaptive certificate proves FIT + LOCAL stability but stops short of global
# optimality: local stability is necessary but not sufficient (3,752 locally-
# stable non-optima). Global optimality needs an ORDERING primitive (Thread 1A's
# leq). By Bellman optimality, the DP cost[n] is the global minimum description
# length iff, for every endpoint e, DOMINANCE (cost[e] <= cost[s]+c(s,e) for all
# feasible s) and ACHIEVING (equality at par[e]) hold. DOMINANCE rules out every
# cheaper partition — including all 3,752 non-optima. Each dominance inequality
# is a real ZK leq proof (zk_range.tv) over the runtime prime; c(s,e) is bound to
# the data by the FIT machinery (seg_fits). Composition of small leqs (each
# padded_n<=32), buffer-safe — the empirically-measured way to stay within the
# FRI buffers rather than a fragile monolith. Cross-checked by
# @internal-oracle: genus_global (DP cost[n] == brute-force global over
# ALL partitions; every dominance inequality holds). Per shape:
#   nseg  cost[n]  ndom  all_dominance_verified  achieving_ok  tamper_rejected.
# Expected 2 3 18 1 1 1 / 2 5 18 1 1 1 / 3 6 30 1 1 1.

EXPECTED_GGLOBAL="2
3
18
1
1
1
2
5
18
1
1
1
3
6
30
1
1
1"

GG_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GG_OK=0; fi
if [ "$GG_OK" = "1" ] && [ ! -f "$TMP/plonk_dyn.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/plonk_dyn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/plonk_dyn.ll" -o "$TMP/plonk_dyn.o" 2>/dev/null || GG_OK=0
fi
if [ "$GG_OK" = "1" ] && [ ! -f "$TMP/zkr.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/zk/zk_range.tv" -o "$TMP/zkr.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zkr.ll" -o "$TMP/zkr.o" 2>/dev/null || GG_OK=0
fi
if [ "$GG_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/rfri.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rfri.ll" -o "$TMP/rfri.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_adaptive.tv" -o "$TMP/gad.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gad.ll" -o "$TMP/gad.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_global_certify.tv" -o "$TMP/ggc.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/ggc.ll" -o "$TMP/ggc.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/plonk_dyn.o" "$TMP/rfri.o" "$TMP/gad.o" "$TMP/zkr.o" "$TMP/ggc.o" -o "$TMP/ggc" 2>/dev/null || {
    echo "dynfield-genus-global: FAIL (build)"; GG_OK=0; }
fi
if [ "$GG_OK" = "1" ]; then
    GOT=$("$TMP/ggc")
    if [ "$GOT" = "$EXPECTED_GGLOBAL" ]; then
        echo "dynfield-genus-global: PASS (global MDL-optimality: DP dominance inequalities ZK-proven via leq, rules out the 3,752 locally-stable non-optima; achieving + tamper-reject)"
    else
        echo "dynfield-genus-global: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GGLOBAL"
        echo "--- got ---"; echo "$GOT"
        GG_OK=0
    fi
fi
if [ "$GG_OK" = "0" ]; then FAILURES="$FAILURES genus-global"; fi

# ---- Thread-4-Min gate: zk_describe_test (tvc_self ONLY, 3-file link) ----
# circuit_describe for #[zk] dyn functions. The PLONK circuit a #[zk] function
# compiles to (selectors + copy-constraint sigma) used to live ONLY inside the
# prover companion; an external party got the proof but could not re-derive the
# circuit. The compiler now also emits <fn>_zk_describe_dyn, which writes the
# compile-time-known selectors + omega-encoded sigma into caller buffers and
# reports [padded_n, log_n], rebuilding the topology identically to the prover.
# This gate runs the full external loop on the #[zk] cubic (x^3+x+5): prover
# generates a proof, describe hands back the circuit, and plonk_verify_dyn —
# called DIRECTLY by the driver — checks the proof against the described circuit;
# a tampered selector is rejected. Per prime: native 35, prove 1, external verify
# 1, tamper-reject 0. Expected 35 1 1 0 x2. The one compiler change in the
# 1A/1B/4-Min arc (Stage2==Stage3 re-verified).

EXPECTED_ZKDESC="35
1
1
0
35
1
1
0"

ZD_OK=1
if [ ! -x "$TMP/tvc_self" ]; then ZD_OK=0; fi
if [ "$ZD_OK" = "1" ] && [ ! -f "$TMP/plonk_dyn.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/plonk_dyn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/plonk_dyn.ll" -o "$TMP/plonk_dyn.o" 2>/dev/null || ZD_OK=0
fi
if [ "$ZD_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/zk/zk_cubic_dyn.tv" -o "$TMP/zcd.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zcd.ll" -o "$TMP/zcd.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/zk_describe_test.tv" -o "$TMP/zdt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zdt.ll" -o "$TMP/zdt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/plonk_dyn.o" "$TMP/zcd.o" "$TMP/zdt.o" -o "$TMP/zdt" 2>/dev/null || {
    echo "dynfield-zk-describe: FAIL (build)"; ZD_OK=0; }
fi
if [ "$ZD_OK" = "1" ]; then
    GOT=$("$TMP/zdt")
    if [ "$GOT" = "$EXPECTED_ZKDESC" ]; then
        echo "dynfield-zk-describe: PASS (circuit_describe: external party re-derives #[zk] circuit selectors+sigma, independently verifies the proof, rejects a tampered selector)"
    else
        echo "dynfield-zk-describe: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_ZKDESC"
        echo "--- got ---"; echo "$GOT"
        ZD_OK=0
    fi
fi
if [ "$ZD_OK" = "0" ]; then FAILURES="$FAILURES zk-describe"; fi

# ---- W1 gate: wide_field_test (tvc_self ONLY) ----
# Multi-limb (>2^64) field arithmetic over the BN254 scalar field (Fr, 254 bits)
# — the first SNARK-class prime Traveler can compute over. The dyn field capped
# at primes < 2^64 (the carrier and the whole field_dyn_* ABI are i64); the wide
# ABI carries elements as *i64 4-limb (i256) buffers, with field_wide(l0..l3)
# building the carrier (prime as 4 limbs, Barrett m = floor(2^512/p) computed at
# runtime via an i576 udiv) and field_wide_{add,sub,mul,pow,inv,div} operating
# on them. mul reduces an i512 product through i1024 Barrett (the i256-Barrett
# template one width up). Cross-checked by @internal-oracle: wide_field.
# Self-checking flags: add=R-8, sub both directions, mul=15 (508-bit product),
# Fermat inv round-trip, division round-trip. Expected 1 1 1 1 1 1.

EXPECTED_WIDE="1
1
1
1
1
1"

WF_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WF_OK=0; fi
if [ "$WF_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_field_test.tv" -o "$TMP/wft.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wft.ll" -o "$TMP/wft.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wft.o" -o "$TMP/wft" 2>/dev/null || {
    echo "dynfield-wide-field: FAIL (build)"; WF_OK=0; }
fi
if [ "$WF_OK" = "1" ]; then
    GOT=$("$TMP/wft")
    if [ "$GOT" = "$EXPECTED_WIDE" ]; then
        echo "dynfield-wide-field: PASS (multi-limb BN254 Fr: add/sub/mul/inv/div over a 254-bit prime, i512 product + i1024 Barrett, oracle-checked)"
    else
        echo "dynfield-wide-field: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WIDE"
        echo "--- got ---"; echo "$GOT"
        WF_OK=0
    fi
fi
if [ "$WF_OK" = "0" ]; then FAILURES="$FAILURES wide-field"; fi

# ---- WP2 gate: wide_poseidon2_test (tvc_self ONLY) ----
# The Poseidon2 permutation over BN254 Fr (254 bits) — the first crypto
# primitive in the stack to run over a real SNARK prime. The dyn Poseidon2 used
# i64-backed fields (Goldilocks/BabyBear, x^7); this lifts it to multi-limb
# 254-bit elements (x^5), state lanes carried as 4-limb (i256) buffers operated
# on by field_wide_* (the W1 wide arithmetic). Config t=3, alpha=5, R_F=8,
# R_P=56; round constants + diagonal derived in-field, matching
# @internal-oracle: wide_poseidon2. Mechanism proof (oracle correctness +
# tamper reject), mirroring how the width-generic Stage A preceded width-16
# BabyBear interop. Expected 1 1 (permutation matches oracle / tamper rejected).

EXPECTED_WP2="1
1"

WP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WP_OK=0; fi
if [ "$WP_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_poseidon2_test.tv" -o "$TMP/wp.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wp.ll" -o "$TMP/wp.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wp.o" -o "$TMP/wp" 2>/dev/null || {
    echo "dynfield-wide-poseidon2: FAIL (build)"; WP_OK=0; }
fi
if [ "$WP_OK" = "1" ]; then
    GOT=$("$TMP/wp")
    if [ "$GOT" = "$EXPECTED_WP2" ]; then
        echo "dynfield-wide-poseidon2: PASS (Poseidon2 over BN254 Fr, x^5, t=3 R_F=8 R_P=56; multi-limb state, oracle-matched, tamper-rejected)"
    else
        echo "dynfield-wide-poseidon2: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WP2"
        echo "--- got ---"; echo "$GOT"
        WP_OK=0
    fi
fi
if [ "$WP_OK" = "0" ]; then FAILURES="$FAILURES wide-poseidon2"; fi

# ---- WMerkle gate: wide_merkle_test (tvc_self ONLY) ----
# A binary Merkle tree over BN254 Fr built on the wide Poseidon2 permutation:
# compress(l,r) = permute([l,r,0])[0] (t=3 sponge, 2-to-1). Composes directly on
# W1 wide arithmetic + WP2 — no new compiler machinery. Cross-checked against
# @internal-oracle: wide_poseidon2 (merkle mode): 4-leaf tree
# [11,22,33,44]. Tests: root matches oracle / Merkle opening of leaf 0 verifies /
# tampered leaf rejected. Expected 1 1 1.

EXPECTED_WM="1
1
1"

WM_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WM_OK=0; fi
if [ "$WM_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_merkle_test.tv" -o "$TMP/wm.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wm.ll" -o "$TMP/wm.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wm.o" -o "$TMP/wm" 2>/dev/null || {
    echo "dynfield-wide-merkle: FAIL (build)"; WM_OK=0; }
fi
if [ "$WM_OK" = "1" ]; then
    GOT=$("$TMP/wm")
    if [ "$GOT" = "$EXPECTED_WM" ]; then
        echo "dynfield-wide-merkle: PASS (Merkle over BN254 Fr via wide Poseidon2 2-to-1: root oracle-matched, opening verifies, tamper rejected)"
    else
        echo "dynfield-wide-merkle: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WM"
        echo "--- got ---"; echo "$GOT"
        WM_OK=0
    fi
fi
if [ "$WM_OK" = "0" ]; then FAILURES="$FAILURES wide-merkle"; fi

# ---- WNTT gate: wide_ntt_test (tvc_self ONLY) ----
# A Number Theoretic Transform over BN254 Fr — the load-bearing layer for FRI/
# PLONK over a real SNARK prime. BN254 Fr has 2-adicity 28, so roots of unity up
# to 2^28 exist. Same DIT Cooley-Tukey structure as the dyn NTT (bit-reversal +
# small-to-large butterflies, natural-order output) but with 4-limb (i256) state
# operated on by field_wide_*. omega8 + n_inv supplied as wide constants from
# @internal-oracle: wide_ntt (BN254 generator g=5). Tests: round-trip
# inverse(forward([1..8]))==[1..8], and poly mul (1+2x)(3+4x)=3+10x+8x^2 via
# pointwise NTT. Expected 1 1.

EXPECTED_WN="1
1"

WN_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WN_OK=0; fi
if [ "$WN_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_ntt_test.tv" -o "$TMP/wn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wn.ll" -o "$TMP/wn.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wn.o" -o "$TMP/wn" 2>/dev/null || {
    echo "dynfield-wide-ntt: FAIL (build)"; WN_OK=0; }
fi
if [ "$WN_OK" = "1" ]; then
    GOT=$("$TMP/wn")
    if [ "$GOT" = "$EXPECTED_WN" ]; then
        echo "dynfield-wide-ntt: PASS (NTT over BN254 Fr: round-trip + poly mul via pointwise products, multi-limb roots, oracle-checked)"
    else
        echo "dynfield-wide-ntt: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WN"
        echo "--- got ---"; echo "$GOT"
        WN_OK=0
    fi
fi
if [ "$WN_OK" = "0" ]; then FAILURES="$FAILURES wide-ntt"; fi

# ---- WFRI gate: wide_fri_test (tvc_self ONLY) ----
# A FRI low-degree / polynomial-commitment proof over BN254 Fr — the first DEEP
# composition of the wide tower, stacking every wide primitive: coset NTT (LDE),
# Poseidon2 t=3 permutation, binary Merkle, a Fiat-Shamir transcript (t=3 sponge,
# rate 2), and the FRI fold + query/Merkle-opening argument, all over a real SNARK
# prime via field_wide_* (ZERO compiler change). Two adaptations from the i64 dyn
# FRI: t=3 sponge (not t=8) and SINGLE-element 254-bit digests (not 4 lanes). The
# NTT roots are derived at RUNTIME by a GENERIC wide root-chain (read p from the
# carrier, count 2-adicity via limb shift, find a primitive 2^ln-th root by Euler
# trial — no factoring). Cross-checked by @internal-oracle: wide_fri.
# Tests: prove+verify f(x)=1+2x+3x^2+4x^3 -> 1, tamper final fold -> 0. Expected 1 0.

EXPECTED_WFRI="1
0"

WFRI_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WFRI_OK=0; fi
if [ "$WFRI_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_fri_test.tv" -o "$TMP/wfri.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wfri.ll" -o "$TMP/wfri.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wfri.o" -o "$TMP/wfri" 2>/dev/null || {
    echo "dynfield-wide-fri: FAIL (build)"; WFRI_OK=0; }
fi
if [ "$WFRI_OK" = "1" ]; then
    GOT=$("$TMP/wfri")
    if [ "$GOT" = "$EXPECTED_WFRI" ]; then
        echo "dynfield-wide-fri: PASS (FRI over BN254 Fr: coset LDE + t=3 sponge transcript + Merkle openings + fold, runtime generic root-chain, oracle-checked, tamper rejected)"
    else
        echo "dynfield-wide-fri: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WFRI"
        echo "--- got ---"; echo "$GOT"
        WFRI_OK=0
    fi
fi
if [ "$WFRI_OK" = "0" ]; then FAILURES="$FAILURES wide-fri"; fi

# ---- WPLONK gate: wide_plonk_test (tvc_self ONLY) ----
# The SUMMIT of the wide tower: a full PLONK proving stack over BN254 Fr — "this
# computation ran correctly" over a real SNARK prime. Composes every wide
# primitive (coset NTT forward+inverse, Poseidon2 t=3, Merkle, t=3 sponge
# transcript, FRI) plus the grand-product permutation argument, gate equation,
# and 4n-coset quotient, all via field_wide_* (ZERO compiler change). Inherits
# the wide-FRI adaptations: t=3 sponge, single 254-bit digests, generic runtime
# root-chain (no factoring). BN254-FLAVORED circuit: same 8-gate cubic topology
# as plonk_dyn (x^3+x+5=OUT) but the witness x is a genuine ~200-bit element —
# a value no i64 field can hold. Cross-checked by wide_plonk_oracle.py. Tests:
# valid proof -> 1, tampered eval -> 2 (gate equation fails). Expected 1 2.

EXPECTED_WPLONK="1
2"

WPLONK_OK=1
if [ ! -x "$TMP/tvc_self" ]; then WPLONK_OK=0; fi
if [ "$WPLONK_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/wide_plonk_test.tv" -o "$TMP/wp.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/wp.ll" -o "$TMP/wp.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/wp.o" -o "$TMP/wp" 2>/dev/null || {
    echo "dynfield-wide-plonk: FAIL (build)"; WPLONK_OK=0; }
fi
if [ "$WPLONK_OK" = "1" ]; then
    GOT=$("$TMP/wp")
    if [ "$GOT" = "$EXPECTED_WPLONK" ]; then
        echo "dynfield-wide-plonk: PASS (full PLONK over BN254 Fr: grand-product permutation + gate equation + 4n-coset quotient + FRI, t=3 sponge, 254-bit witness, runtime generic root-chain, oracle-checked, tamper rejected)"
    else
        echo "dynfield-wide-plonk: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_WPLONK"
        echo "--- got ---"; echo "$GOT"
        WPLONK_OK=0
    fi
fi
if [ "$WPLONK_OK" = "0" ]; then FAILURES="$FAILURES wide-plonk"; fi

# ---- Phase 7c gate: dyn_merkle_test (tvc_self ONLY) ----
# Dynamic-field Merkle composing onto dyn Poseidon2: generic build/open/verify
# instantiated <dyn>, full tree + tamper cycle over a runtime prime.

EXPECTED_DYN_MERKLE="1
0
1"

MK_OK=1
if [ ! -x "$TMP/tvc_self" ]; then MK_OK=0; fi
if [ "$MK_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_merkle_test.tv" -o "$TMP/dm.ll" >/dev/null 2>&1 || {
    echo "dynfield-merkle: FAIL (compile)"; MK_OK=0; }
fi
if [ "$MK_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dm.ll" -o "$TMP/dm.o" 2>/dev/null || {
    echo "dynfield-merkle: FAIL (llc)"; MK_OK=0; }
fi
if [ "$MK_OK" = "1" ]; then
clang $LINK_PIE "$TMP/dm.o" -o "$TMP/dm" 2>/dev/null || {
    echo "dynfield-merkle: FAIL (link)"; MK_OK=0; }
fi
if [ "$MK_OK" = "1" ]; then
    GOT=$("$TMP/dm")
    if [ "$GOT" = "$EXPECTED_DYN_MERKLE" ]; then
        echo "dynfield-merkle: PASS (dyn Merkle build/open/verify/tamper over runtime prime)"
    else
        echo "dynfield-merkle: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DYN_MERKLE"
        echo "--- got ---"; echo "$GOT"
        MK_OK=0
    fi
fi
if [ "$MK_OK" = "0" ]; then FAILURES="$FAILURES merkle"; fi

# ---- Phase 7c gate: dyn_fri_test (tvc_self ONLY) ----
# Dynamic-field FRI — the general low-degree / data-consistency proving
# primitive over a runtime prime. Full stack (coset NTT, Poseidon2, Merkle,
# Fiat-Shamir transcript, fold, prove, verify) instantiated <dyn>.
# Commit to a degree-3 polynomial, prove+verify, reject a tampered proof.

EXPECTED_DYN_FRI="1
0"

FRI_OK=1
if [ ! -x "$TMP/tvc_self" ]; then FRI_OK=0; fi
if [ "$FRI_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_fri_test.tv" -o "$TMP/dfri.ll" >/dev/null 2>&1 || {
    echo "dynfield-fri: FAIL (compile)"; FRI_OK=0; }
fi
if [ "$FRI_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/dfri.ll" -o "$TMP/dfri.o" 2>/dev/null || {
    echo "dynfield-fri: FAIL (llc)"; FRI_OK=0; }
fi
if [ "$FRI_OK" = "1" ]; then
clang $LINK_PIE "$TMP/dfri.o" -o "$TMP/dfri" 2>/dev/null || {
    echo "dynfield-fri: FAIL (link)"; FRI_OK=0; }
fi
if [ "$FRI_OK" = "1" ]; then
    GOT=$("$TMP/dfri")
    if [ "$GOT" = "$EXPECTED_DYN_FRI" ]; then
        echo "dynfield-fri: PASS (dyn FRI commit/prove/verify/tamper over runtime prime)"
    else
        echo "dynfield-fri: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DYN_FRI"
        echo "--- got ---"; echo "$GOT"
        FRI_OK=0
    fi
fi
if [ "$FRI_OK" = "0" ]; then FAILURES="$FAILURES fri"; fi

# ---- dyn regime-proof gate: dyn_regime_proof_test (tvc_self ONLY)
#      (@internal-note: plan-phase7-dyn-crypto, 7d-L1) ----
# Regime boundary proof: a segmentation is canonical iff each segment is
# low-degree (d-th forward difference vanishes) AND each boundary is FORCED
# (d-th difference across the boundary is nonzero, witnessed by err*inv==1).
# Soundness is the field's algebra: zero has no inverse, so a false boundary
# (err=0 in the smooth interior) cannot be proven. Field-invariant.

EXPECTED_DYN_RP="1
1
1
0"

RP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then RP_OK=0; fi
if [ "$RP_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/dyn_regime_proof_test.tv" -o "$TMP/drp.ll" >/dev/null 2>&1 || {
    echo "dynfield-regime-proof: FAIL (compile)"; RP_OK=0; }
fi
if [ "$RP_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/drp.ll" -o "$TMP/drp.o" 2>/dev/null || {
    echo "dynfield-regime-proof: FAIL (llc)"; RP_OK=0; }
fi
if [ "$RP_OK" = "1" ]; then
clang $LINK_PIE "$TMP/drp.o" -o "$TMP/drp" 2>/dev/null || {
    echo "dynfield-regime-proof: FAIL (link)"; RP_OK=0; }
fi
if [ "$RP_OK" = "1" ]; then
    GOT=$("$TMP/drp")
    if [ "$GOT" = "$EXPECTED_DYN_RP" ]; then
        echo "dynfield-regime-proof: PASS (canonical boundary proof; false boundary fails via no-inverse)"
    else
        echo "dynfield-regime-proof: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_DYN_RP"
        echo "--- got ---"; echo "$GOT"
        RP_OK=0
    fi
fi
if [ "$RP_OK" = "0" ]; then FAILURES="$FAILURES regime-proof"; fi

# ---- Phase 7d-L1 generalize gate: regime_fri_test (tvc_self ONLY, 2-file) ----
# "It generalizes": the SAME canonical-segmentation primitive (regime_fri.tv)
# run over TWO unrelated domains — a real cascade-v4 MES futures price path and
# a synthetic piecewise-linear signal. Each: prove canonical segmentation,
# verify (fit + forced boundary + partition), reject a tampered certificate.
# nsegA=16 (real market structure), nsegB=3 (the three synthetic runs).

EXPECTED_REGIME_FRI="16
1
0
3
1
0"

GEN_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GEN_OK=0; fi
if [ "$GEN_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/rf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/regime_fri_test.tv" -o "$TMP/rft.ll" >/dev/null 2>&1 || {
    echo "dynfield-regime-fri: FAIL (compile)"; GEN_OK=0; }
fi
if [ "$GEN_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/rf.ll" -o "$TMP/rf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rft.ll" -o "$TMP/rft.o" 2>/dev/null || {
    echo "dynfield-regime-fri: FAIL (llc)"; GEN_OK=0; }
fi
if [ "$GEN_OK" = "1" ]; then
clang $LINK_PIE "$TMP/rf.o" "$TMP/rft.o" -o "$TMP/rft" 2>/dev/null || {
    echo "dynfield-regime-fri: FAIL (link)"; GEN_OK=0; }
fi
if [ "$GEN_OK" = "1" ]; then
    GOT=$("$TMP/rft")
    if [ "$GOT" = "$EXPECTED_REGIME_FRI" ]; then
        echo "dynfield-regime-fri: PASS (canonical segmentation generalizes: candlestick + synthetic, tamper rejected)"
    else
        echo "dynfield-regime-fri: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_REGIME_FRI"
        echo "--- got ---"; echo "$GOT"
        GEN_OK=0
    fi
fi
if [ "$GEN_OK" = "0" ]; then FAILURES="$FAILURES regime-fri"; fi

# ---- Phase 7d-L1 ZK composition gates: 3 domains x shared engine (tvc_self ONLY) ----
# Full ZK regime composition: bind the ALGEBRAIC canonical-segmentation
# certificate to the CRYPTOGRAPHIC dyn-FRI commitment stack. The domain-agnostic
# engine is src/lib/regime/regime_zk.tv (exports run_zk_domain: commit data via Merkle
# -> regime_prove -> Fiat-Shamir freeze (data_root + segmentation descriptor ->
# binding fingerprint) -> per-segment FRI low-degree proof; verification replays
# all four layers; three tampers — data/descriptor/proof — each rejected).
# Three thin drivers embed real data from UNRELATED domains and call the SAME
# engine, proving the proof generalizes:
#   candlestick (cascade-v4 MES futures, n=32) -> nseg=16  (trivial: market
#       path has no exact piecewise-linear structure; sound but degenerate)
#   MNIST (digit row 14, padded n=32)          -> nseg=4   (GENUINE structure:
#       long zero-background runs + stroke; segments longer than the window)
#   audio (Lena Raine - Rubedo, n=16)          -> nseg=8   (HONEST failure: full-
#       bandwidth music never fits exactly, so nseg saturates at n/2; still sound)
# Each: prove+verify=1, three tampers=0. Engine built once, linked per driver.

ZK_OK=1
if [ ! -x "$TMP/tvc_self" ]; then ZK_OK=0; fi
if [ "$ZK_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_zk.tv" -o "$TMP/rzk.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rzk.ll" -o "$TMP/rzk.o" 2>/dev/null || {
    echo "dynfield-regime-zk: FAIL (engine build)"; ZK_OK=0; }
fi

run_zk_domain_gate() {
    # $1 = driver basename, $2 = gate name, $3 = expected output, $4 = pass note
    local drv="$1" name="$2" expected="$3" note="$4"
    if [ "$ZK_OK" != "1" ]; then FAILURES="$FAILURES $name"; return; fi
    local ok=1
    "$TMP/tvc_self" "$(tv "$drv")" -o "$TMP/$drv.ll" >/dev/null 2>&1 || {
        echo "$name: FAIL (compile)"; ok=0; }
    if [ "$ok" = "1" ]; then
    "$LLC" $LLC_TARGET -filetype=obj "$TMP/$drv.ll" -o "$TMP/$drv.o" 2>/dev/null || {
        echo "$name: FAIL (llc)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
    clang $LINK_PIE "$TMP/rzk.o" "$TMP/$drv.o" -o "$TMP/$drv" 2>/dev/null || {
        echo "$name: FAIL (link)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
        local got; got=$("$TMP/$drv")
        if [ "$got" = "$expected" ]; then
            echo "$name: PASS ($note)"
        else
            echo "$name: FAIL"
            echo "--- expected ---"; echo "$expected"
            echo "--- got ---"; echo "$got"
            ok=0
        fi
    fi
    if [ "$ok" = "0" ]; then FAILURES="$FAILURES $name"; fi
}

run_zk_domain_gate "regime_fri_zk_test"   "dynfield-regime-zk-candle" \
    "16
1
0
0
0" "candlestick nseg=16 bound to dyn FRI; data/descriptor/proof tamper rejected"

run_zk_domain_gate "regime_mnist_zk_test" "dynfield-regime-zk-mnist" \
    "4
1
0
0
0" "MNIST digit row nseg=4 (genuine structure) through SAME engine; tamper rejected"

run_zk_domain_gate "regime_wav_zk_test"   "dynfield-regime-zk-wav" \
    "8
1
0
0
0" "real audio nseg=8 (trivial saturation, honest negative) through SAME engine; sound, tamper rejected"

# ---- Phase 7d-L0 gate: dyn PLONK (the arbitrary-computation axis) ----
# Dynamic-field PLONK: "this computation ran correctly" over a runtime prime.
# Same 8-gate x^3+x+5=35 circuit as baked plonk_test, but every field op routes
# through the runtime carrier and the trace root of unity comes from the carrier
# root chain (plonk_omega -> __ntt_root). Two-file link: plonk_dyn.o + driver.o.
# Proves+verifies over Goldilocks AND BabyBear, rejects a tampered evaluation.

EXPECTED_PLONK_DYN="1
2
1
2"

PD_OK=1
if [ ! -x "$TMP/tvc_self" ]; then PD_OK=0; fi
if [ "$PD_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/pd.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/plonk_dyn_test.tv" -o "$TMP/pdt.ll" >/dev/null 2>&1 || {
    echo "dynfield-plonk-dyn: FAIL (compile)"; PD_OK=0; }
fi
if [ "$PD_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/pd.ll" -o "$TMP/pd.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pdt.ll" -o "$TMP/pdt.o" 2>/dev/null || {
    echo "dynfield-plonk-dyn: FAIL (llc)"; PD_OK=0; }
fi
if [ "$PD_OK" = "1" ]; then
clang $LINK_PIE "$TMP/pd.o" "$TMP/pdt.o" -o "$TMP/pdt" 2>/dev/null || {
    echo "dynfield-plonk-dyn: FAIL (link)"; PD_OK=0; }
fi
if [ "$PD_OK" = "1" ]; then
    GOT=$("$TMP/pdt")
    if [ "$GOT" = "$EXPECTED_PLONK_DYN" ]; then
        echo "dynfield-plonk-dyn: PASS (dyn PLONK proves x^3+x+5=35 over Goldilocks+BabyBear; tampered eval rejected)"
    else
        echo "dynfield-plonk-dyn: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_PLONK_DYN"
        echo "--- got ---"; echo "$GOT"
        PD_OK=0
    fi
fi
if [ "$PD_OK" = "0" ]; then FAILURES="$FAILURES plonk-dyn"; fi

# ---- Phase 7d-L0 gates: dyn #[zk] (compiler-generated dyn PLONK companions) ----
# A #[zk] generic fn instantiated <dyn> auto-emits both native and dyn-PLONK-
# prover companions. The compiler (codegen_zk_fn_dyn) builds the circuit, routes
# witness arithmetic through @field_dyn_*, canonicalizes selectors from the
# carrier, derives omega from the carrier root chain, and calls plonk_prove_dyn.
# Three-file link: plonk_dyn.o + <circuit>.o + <driver>.o.

run_zk_dyn_gate() {
    # $1 = circuit basename, $2 = driver basename, $3 = gate name,
    # $4 = expected, $5 = pass note
    local circ="$1" drv="$2" name="$3" expected="$4" note="$5"
    if [ ! -x "$TMP/tvc_self" ]; then FAILURES="$FAILURES $name"; return; fi
    local ok=1
    "$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/zpd.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$(tv "$circ")" -o "$TMP/$circ.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$(tv "$drv")" -o "$TMP/$drv.ll" >/dev/null 2>&1 || {
        echo "$name: FAIL (compile)"; ok=0; }
    if [ "$ok" = "1" ]; then
    "$LLC" $LLC_TARGET -filetype=obj "$TMP/zpd.ll" -o "$TMP/zpd.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/$circ.ll" -o "$TMP/$circ.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/$drv.ll" -o "$TMP/$drv.o" 2>/dev/null || {
        echo "$name: FAIL (llc)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
    clang $LINK_PIE "$TMP/zpd.o" "$TMP/$circ.o" "$TMP/$drv.o" -o "$TMP/$drv.bin" 2>/dev/null || {
        echo "$name: FAIL (link)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
        local got; got=$("$TMP/$drv.bin")
        if [ "$got" = "$expected" ]; then
            echo "$name: PASS ($note)"
        else
            echo "$name: FAIL"
            echo "--- expected ---"; echo "$expected"
            echo "--- got ---"; echo "$got"
            ok=0
        fi
    fi
    if [ "$ok" = "0" ]; then FAILURES="$FAILURES $name"; fi
}

run_zk_dyn_gate "zk_cubic_dyn" "zk_cubic_dyn_test" "dynfield-zk-cubic-dyn" \
    "35
1
35
1" "dyn #[zk] cubic x^3+x+5=35 native+proof over Goldilocks+BabyBear"

run_zk_dyn_gate "zk_forward_sum_dyn" "zk_forward_sum_dyn_test" "dynfield-zk-fsum-dyn" \
    "62
1
62
1" "dyn #[zk] codec kernel forward_sum=62 native+proof over Goldilocks+BabyBear"

run_zk_dyn_gate "zk_if_dyn" "zk_if_dyn_test" "dynfield-zk-if-dyn" \
    "42
1
42
1" "dyn #[zk] conditional select=42 native+proof over Goldilocks+BabyBear"

run_zk_dyn_gate "zk_loop_dyn" "zk_loop_dyn_test" "dynfield-zk-loop-dyn" \
    "62
1
62
1" "dyn #[zk] for-loop forward_sum=62 native+proof over Goldilocks+BabyBear"

# ---- Phase A gate: runtime circuit topology (7d-L1) ----
# The circuit SHAPE is built from a regime descriptor AT RUNTIME (rc_build:
# difference-tableau gates + witness + union-find permutation in heap arrays),
# then handed to the SAME dyn PLONK prover/verifier (plonk_dyn.o) with ZERO
# compiler change. The circuit proves a degree-d segment fits a degree-d
# polynomial (top-level d-th differences forced equal by the permutation); the
# wiring topology IS the segmentation, gate count = f(regimes) = runtime.
# A.2: (a) descriptor DISCOVERED FROM DATA via regime_prove<dyn>; (b) data bound
# by Merkle root + Fiat-Shamir fingerprint; (c) three soundness tampers
# (witness, descriptor, field) each rejected -> 2 1 0 0 0; plus a degree-2
# quadratic segment proving the topology varies with degree -> 1 2.
# Three-file link: plonk_dyn.o + regime_fri.o + runtime_circuit.o (tvc_self-only).

EXPECTED_RC="2
1
0
0
0
1
2"

RC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then RC_OK=0; fi
if [ "$RC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/rcpd.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/rcrf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/runtime_circuit.tv" -o "$TMP/rc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/runtime_circuit_test.tv" -o "$TMP/rct.ll" >/dev/null 2>&1 || {
    echo "dynfield-runtime-circuit: FAIL (compile)"; RC_OK=0; }
fi
if [ "$RC_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/rcpd.ll" -o "$TMP/rcpd.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rcrf.ll" -o "$TMP/rcrf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rc.ll" -o "$TMP/rc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rct.ll" -o "$TMP/rct.o" 2>/dev/null || {
    echo "dynfield-runtime-circuit: FAIL (llc)"; RC_OK=0; }
fi
if [ "$RC_OK" = "1" ]; then
clang $LINK_PIE "$TMP/rcpd.o" "$TMP/rcrf.o" "$TMP/rc.o" "$TMP/rct.o" -o "$TMP/rc" 2>/dev/null || {
    echo "dynfield-runtime-circuit: FAIL (link)"; RC_OK=0; }
fi
if [ "$RC_OK" = "1" ]; then
    GOT=$("$TMP/rc")
    if [ "$GOT" = "$EXPECTED_RC" ]; then
        echo "dynfield-runtime-circuit: PASS (data-driven runtime circuit; descriptor from regime_prove, data-bound, 3 tampers rejected; deg-2 topology)"
    else
        echo "dynfield-runtime-circuit: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_RC"
        echo "--- got ---"; echo "$GOT"
        RC_OK=0
    fi
fi
if [ "$RC_OK" = "0" ]; then FAILURES="$FAILURES runtime-circuit"; fi

# ---- Phase A.3 gates: the genus signature (the instrument) ----
# Traveler measures the genus signature (d*, nseg@d*) of a data series: d* =
# onset degree (minimal degree at which exact polynomial structure appears),
# nseg = canonical regime count at d*. genus_probe.tv wraps regime_prove<dyn>;
# the signature is field-invariant + affine-invariant (a property of the SHAPE).
# Three-file link: regime_fri.o + genus_probe.o + <driver>.o.

run_genus_gate() {
    # $1 = driver basename, $2 = gate name, $3 = expected, $4 = pass note
    local drv="$1" name="$2" expected="$3" note="$4"
    if [ ! -x "$TMP/tvc_self" ]; then FAILURES="$FAILURES $name"; return; fi
    local ok=1
    "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_probe.tv" -o "$TMP/gp.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$(tv "$drv")" -o "$TMP/$drv.ll" >/dev/null 2>&1 || {
        echo "$name: FAIL (compile)"; ok=0; }
    if [ "$ok" = "1" ]; then
    "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gp.ll" -o "$TMP/gp.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/$drv.ll" -o "$TMP/$drv.o" 2>/dev/null || {
        echo "$name: FAIL (llc)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
    clang $LINK_PIE "$TMP/grf.o" "$TMP/gp.o" "$TMP/$drv.o" -o "$TMP/$drv.bin" 2>/dev/null || {
        echo "$name: FAIL (link)"; ok=0; }
    fi
    if [ "$ok" = "1" ]; then
        local got; got=$("$TMP/$drv.bin")
        if [ "$got" = "$expected" ]; then
            echo "$name: PASS ($note)"
        else
            echo "$name: FAIL"; echo "--- expected ---"; echo "$expected"
            echo "--- got ---"; echo "$got"; ok=0
        fi
    fi
    if [ "$ok" = "0" ]; then FAILURES="$FAILURES $name"; fi
}

run_genus_gate "genus_probe_test" "dynfield-genus-probe" \
    "1
4
1
8
0
8
2
4
3
1" "genus signature (d*,nseg): saw=1,4 tri=1,8 sq=0,8 parab=2,4 cubic=3,1"

run_genus_gate "genus_invariance_test" "dynfield-genus-invariance" \
    "1
1
1
1
1
1
1
1" "genus signature invariant under field (x3 primes) + affine (+C,*k)"

# ---- genus-blend gate: the global<->local polynomial blend instrument ----
# Validates the nseg(d) degree-sweep instrument (companion to the MNIST contour
# census, examples/mnist_genus_census.tv) on synthetic controls with KNOWN
# answers, field-invariant across Goldilocks/BabyBear/65537: a single parabola
# collapses to nseg=1 at d>=2; two glued arcs plateau at nseg=2 (never collapse);
# a line collapses at d=1; structure-free noise tracks the trivial bound
# ceil(m/(d+1)). Proves the instrument resolves global vs local BEFORE trusting
# it on real data. Two-file link: regime_fri.o + genus_blend_test.o.

EXPECTED_GBLEND="1
1
1
1
1
1"

GBLEND_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GBLEND_OK=0; fi
if [ "$GBLEND_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_blend_test.tv" -o "$TMP/gbt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gbt.ll" -o "$TMP/gbt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/gbt.o" -o "$TMP/gbt" 2>/dev/null || {
    echo "dynfield-genus-blend: FAIL (build)"; GBLEND_OK=0; }
fi
if [ "$GBLEND_OK" = "1" ]; then
    GOT=$("$TMP/gbt")
    if [ "$GOT" = "$EXPECTED_GBLEND" ]; then
        echo "dynfield-genus-blend: PASS (nseg(d) sweep resolves global vs local: parab->1@d2, twoarc->plateau2, line->1@d1, noise->trivial bound; field-invariant x3 primes)"
    else
        echo "dynfield-genus-blend: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GBLEND"
        echo "--- got ---"; echo "$GOT"
        GBLEND_OK=0
    fi
fi
if [ "$GBLEND_OK" = "0" ]; then FAILURES="$FAILURES genus-blend"; fi

# ---- convergence gate: finite<->continuous limit (the deep alignment) ----
# Renders a TRANSCENDENTAL circle arc y=isqrt(r^2-x^2) (never polynomial) plus
# polynomial controls (parabola d2, line d1) at r=8..256, and contrasts two
# segmenters: the EXACT dyn segmenter (regime_prove_dyn, ZK-sound) SHATTERS the
# circle (nseg strictly grows: 3,5,10,17,33,61 at d2 — refuses to call a rounded
# circle polynomial) while the THRESHOLD segmenter (regime_detect, eps=1, raw
# integer) CONVERGES (nseg/r fraction non-increasing). The gap between the ladders
# IS the finite/continuous boundary, measured. Exact path is field-invariant over
# Goldilocks/BabyBear/65537. Controls: parabola/line collapse to nseg=1 at all r.
# Cross-checked by @internal-oracle: convergence.
# Three-file link: regime_fri.o + poly_core.o + convergence_test.o.

EXPECTED_CONV="1
1
1
1
1
1"

CONV_OK=1
if [ ! -x "$TMP/tvc_self" ]; then CONV_OK=0; fi
if [ "$CONV_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/core/poly_core.tv" -o "$TMP/pc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/convergence_test.tv" -o "$TMP/cvt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pc.ll" -o "$TMP/pc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/cvt.ll" -o "$TMP/cvt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/pc.o" "$TMP/cvt.o" -o "$TMP/cvt" 2>/dev/null || {
    echo "dynfield-convergence: FAIL (build)"; CONV_OK=0; }
fi
if [ "$CONV_OK" = "1" ]; then
    GOT=$("$TMP/cvt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_CONV" ]; then
        echo "dynfield-convergence: PASS (finite<->continuous limit: exact segmenter shatters circle, threshold converges, polynomial controls collapse, field-invariant x3 primes)"
    else
        echo "dynfield-convergence: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_CONV"
        echo "--- got ---"; echo "$GOT"
        CONV_OK=0
    fi
fi
if [ "$CONV_OK" = "0" ]; then FAILURES="$FAILURES convergence"; fi

# ---- glyph-convergence gate: the genus fingerprint is a property of the SHAPE ----
# The bridge between the convergence law (geometric primitives) and the MNIST
# genus fingerprint. Renders hand-coded VECTOR digit glyphs (exact continuous
# curves) at increasing resolution and watches the contour nseg(d) fingerprint
# converge. Three glyphs span the spectrum: "1" (rectangle) -> exact nseg CONSTANT
# (==edge count, resolution-independent); "7" (polygon) -> exact nseg BOUNDED
# (converges, corner jitter); "0" (annulus, transcendental) -> exact SHATTERS
# while threshold CONVERGES (the circle contrast, now in a glyph). Field-invariant
# over Goldilocks/BabyBear/65537. Cross-checked by glyph_convergence_oracle.py.
# Three-file link: regime_fri.o + poly_core.o + glyph_convergence_test.o.

EXPECTED_GLYPH="1
1
1
1
1"

GLYPH_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GLYPH_OK=0; fi
if [ "$GLYPH_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/core/poly_core.tv" -o "$TMP/pc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/glyph_convergence_test.tv" -o "$TMP/gct.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pc.ll" -o "$TMP/pc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gct.ll" -o "$TMP/gct.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/pc.o" "$TMP/gct.o" -o "$TMP/gct" 2>/dev/null || {
    echo "dynfield-glyph-convergence: FAIL (build)"; GLYPH_OK=0; }
fi
if [ "$GLYPH_OK" = "1" ]; then
    GOT=$("$TMP/gct" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_GLYPH" ]; then
        echo "dynfield-glyph-convergence: PASS (genus fingerprint is a shape property: '1' nseg constant==edge count, '7' bounded, '0' shatters+threshold-converges, field-invariant x3 primes)"
    else
        echo "dynfield-glyph-convergence: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GLYPH"
        echo "--- got ---"; echo "$GOT"
        GLYPH_OK=0
    fi
fi
if [ "$GLYPH_OK" = "0" ]; then FAILURES="$FAILURES glyph-convergence"; fi

# ---- multiview gate: Level 1 of the verifiable classifier (the Mobius lesson) ----
# Multi-ANGLE fingerprints: a shape's nseg(d) measured across five views of its
# contour (x, y, x+y, x-y, and r(theta) = squared radial distance ordered by angle
# via integer octant + cross-product, NO trig). Synthetic controls with KNOWN
# answers: r(theta) is REORIENTATION-INVARIANT ("7" vs its transpose give the same
# r fingerprint while x changes); a square is 4-fold on its natural axes while a
# diamond (square rotated 45) SCRAMBLES in Cartesian but RECOVERS the 4-fold in a
# 45-deg view. The right angle reveals structure a single view hides. Field-
# invariant over Goldilocks/BabyBear/65537. Cross-checked by multiview_oracle.py.
# Two-file link: regime_fri.o + multiview_test.o.

EXPECTED_MV="1
1
1
1
1
1"

MV_OK=1
if [ ! -x "$TMP/tvc_self" ]; then MV_OK=0; fi
if [ "$MV_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/multiview_test.tv" -o "$TMP/mvt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/mvt.ll" -o "$TMP/mvt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/mvt.o" -o "$TMP/mvt" 2>/dev/null || {
    echo "dynfield-multiview: FAIL (build)"; MV_OK=0; }
fi
if [ "$MV_OK" = "1" ]; then
    GOT=$("$TMP/mvt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_MV" ]; then
        echo "dynfield-multiview: PASS (multi-angle fingerprints: r(theta) reorientation-invariant, diamond recovers 4-fold in rotated view, field-invariant x3 primes)"
    else
        echo "dynfield-multiview: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_MV"
        echo "--- got ---"; echo "$GOT"
        MV_OK=0
    fi
fi
if [ "$MV_OK" = "0" ]; then FAILURES="$FAILURES multiview"; fi

# ---- relational gate: Level 2 of the verifiable classifier (the attention lesson) ----
# RELATIONAL structure: a per-view nseg COUNT (Level 1) throws away HOW the arcs
# of a contour CONNECT. Level 2 builds the all-pairs arc-relation matrix M[i][j]
# (attention without softmax) and reads ordering-invariant spectral moments
# tr(M^k) over a dyn field. Three symmetric {0,1} relation graphs (length-differ,
# co-turning corner class, endpoint-proximity at an intrinsic scale). MEASURED
# finding: corner-cut moments are Level-1-EQUIVALENT (count-like) — U and T share
# an IDENTICAL corner-cut signature; the radial-cut (angle-ordered) relations
# SEPARATE them. The honest Level-2 tension: the corner-cut is reorientation-
# (transpose-)invariant but blind; the radial-cut is discriminative but not
# invariant. Field-invariant Goldilocks==BabyBear. Cross-checked by
# relational_oracle.py. Three-file link: regime_fri.o + relational.o + relational_test.o.

EXPECTED_REL="1
1
1
1
1
1"

REL_OK=1
if [ ! -x "$TMP/tvc_self" ]; then REL_OK=0; fi
if [ "$REL_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv"      -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/features/relational.tv"      -o "$TMP/rel.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/relational_test.tv" -o "$TMP/rlt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rel.ll" -o "$TMP/rel.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/rlt.ll" -o "$TMP/rlt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/rel.o" "$TMP/rlt.o" -o "$TMP/rlt" 2>/dev/null || {
    echo "dynfield-relational: FAIL (build)"; REL_OK=0; }
fi
if [ "$REL_OK" = "1" ]; then
    GOT=$("$TMP/rlt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_REL" ]; then
        echo "dynfield-relational: PASS (Level 2 relational: corner-cut Level-1-blind (U==T), radial-cut turn+adj moments SEPARATE, transpose-invariance in corner view, field-invariant x2 primes)"
    else
        echo "dynfield-relational: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_REL"
        echo "--- got ---"; echo "$GOT"
        REL_OK=0
    fi
fi
if [ "$REL_OK" = "0" ]; then FAILURES="$FAILURES relational"; fi

# ---- turning gate: Level 3 of the verifiable classifier (the turning number) ----
# (Inside banner: the "holonomy / persistence" rung; faithful term = turning
# number / rotation index, not differential-geometry holonomy.)
# Level 1 (count) and Level 2 (all-pairs relation) cannot separate 3 from 5 (same
# arcs AND same pairwise relations). What is LEFT is ORIENTATION — the signed
# turning around the contour loop. Instrument H: per-vertex integer turn (sign of
# cross product, no trig); turning number = field-reduced sum via signed_reduce;
# signed PROFILE = half-asymmetry + quartile signed sums. Instrument P: nseg(d)
# persistence spectrum. THE CONTROL (shape vs horizontal mirror): the Level-2
# INVARIANT corner-cut view is BLIND to the mirror; the signed turning profile
# SEES chirality for CHIRAL shapes (C: asym sign-flips -4<->+4) and correctly
# reports NO handedness on ACHIRAL shapes (U: asym equal). De-risked on real
# MNIST: signed profile lifts 3-vs-5 from L1=217 (naive) to L1=1886. Field-carried
# turning invariant Goldilocks==BabyBear (wide prime > i64 handled by
# residuals-fit). Cross-checked by the turning oracle.
# Three-file link: regime_fri.o + turning.o + turning_test.o.

EXPECTED_HOL="1
1
1
1
1
1"

HOL_OK=1
if [ ! -x "$TMP/tvc_self" ]; then HOL_OK=0; fi
if [ "$HOL_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv"     -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/features/turning.tv"      -o "$TMP/hol.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/turning_test.tv" -o "$TMP/hlt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/hol.ll" -o "$TMP/hol.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/hlt.ll" -o "$TMP/hlt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/hol.o" "$TMP/hlt.o" -o "$TMP/hlt" 2>/dev/null || {
    echo "dynfield-turning: FAIL (build)"; HOL_OK=0; }
fi
if [ "$HOL_OK" = "1" ]; then
    GOT=$("$TMP/hlt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_HOL" ]; then
        echo "dynfield-turning: PASS (Level 3 turning number: C chiral (asym sign-flips, corner-cut blind), U achiral, oracle integers, field-carried turning Gold==Baby, persistence spectrum)"
    else
        echo "dynfield-turning: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_HOL"
        echo "--- got ---"; echo "$GOT"
        HOL_OK=0
    fi
fi
if [ "$HOL_OK" = "0" ]; then FAILURES="$FAILURES turning"; fi

# ---- classifier gate: PHASE 2 — the verifiable genus classifier ----
# The deliverable of the whole L1->L2->L3 ladder: a nearest-signature MNIST
# classifier over the full feature vector (8 multi-angle nseg counts + 5 turning
# signed-profile ints), where each inference emits a ZK certificate. The DECISION
# (nearest reference signature) is proven IN-CIRCUIT as a chain of 9 leq dominance
# inequalities (winner is argmin: dist[winner] <= dist[c] for all c), each a real
# ZK leq range proof (zk_range.tv) over the runtime prime — the genus_global
# pattern. Hermetic: embeds MNIST test image 0 (true label 7), traces the contour,
# extracts features (matching classifier_oracle.py EXACTLY), classifies (= 7,
# correct), runs the argmin certificate (honest verify=1, tampered claim=0), and
# checks field-invariance (Goldilocks==BabyBear). HONEST ACCURACY ~50% — the
# deliverable is the AUDITABLE certificate, not the accuracy number.
# NOTE: x-y view is shifted +28 for field-safety (negative coords make the in-field
# forward-difference vanishing test field-dependent; found via this parity gate).
# Five-file link: plonk_dyn.o + regime_fri.o + zk_range.o + genus_classifier.o +
#                 genus_classify_test.o.

EXPECTED_GCL="1
1
1
1
0
1"

GCL_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GCL_OK=0; fi
# shared objects (reuse if a prior gate built them, else build)
if [ "$GCL_OK" = "1" ] && [ ! -f "$TMP/plonk_dyn.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/plonk_dyn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/plonk_dyn.ll" -o "$TMP/plonk_dyn.o" 2>/dev/null || GCL_OK=0
fi
if [ "$GCL_OK" = "1" ] && [ ! -f "$TMP/zkr.o" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/zk/zk_range.tv" -o "$TMP/zkr.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/zkr.ll" -o "$TMP/zkr.o" 2>/dev/null || GCL_OK=0
fi
if [ "$GCL_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_classifier.tv" -o "$TMP/gcl.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gcl.ll" -o "$TMP/gcl.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_classify_test.tv" -o "$TMP/gclt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gclt.ll" -o "$TMP/gclt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/plonk_dyn.o" "$TMP/grf.o" "$TMP/zkr.o" "$TMP/gcl.o" "$TMP/gclt.o" -o "$TMP/gclt" 2>/dev/null || {
    echo "dynfield-classifier: FAIL (build)"; GCL_OK=0; }
fi
if [ "$GCL_OK" = "1" ]; then
    GOT=$("$TMP/gclt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_GCL" ]; then
        echo "dynfield-classifier: PASS (Phase 2 verifiable classifier: L1+L3 features match oracle, nearest-signature decision=7, in-circuit argmin certificate (9 leq) verifies + tampered decision rejected, field-invariant)"
    else
        echo "dynfield-classifier: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GCL"
        echo "--- got ---"; echo "$GOT"
        GCL_OK=0
    fi
fi
if [ "$GCL_OK" = "0" ]; then FAILURES="$FAILURES classifier"; fi

# ---- neuron-read gate: PROBE P1 — reading a neuron with the circle ----
# The matmul wall guards CONSTRUCTION (learning + in-field compute), not
# OBSERVATION. A trained neuron sigma(w.x+b) is a RIDGE FUNCTION — a 1-D curve in
# n-D space, constant orthogonal to w. Sampled along the w-direction it is a 1-D
# function the exact regime segmenter reads natively. A ReLU is BUILT with max (an
# ORDER op — the line) but READ with forward differences (subtraction — the
# circle). Embeds the exact integer activation arrays from neuron_read_oracle.py
# (one ReLU ridge with kink on sample 8, one smooth tanh ridge). STRATIFIED: the
# ReLU reads as EXACTLY 2 segments with the boundary RECOVERING the kink (=>bias),
# field-invariant; the smooth unit SHATTERS under the exact segmenter but
# CONVERGES under the threshold segmenter (the glyph law) — quantifying the
# transcendental residual. Cross-checked by neuron_read_oracle.py (which also
# reads a REAL torch-trained ReLU unit -> nseg=2). Two-file link: regime_fri.o +
# neuron_read_test.o.

EXPECTED_NR="1
1
1
1
1"

NR_OK=1
if [ ! -x "$TMP/tvc_self" ]; then NR_OK=0; fi
if [ "$NR_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv"       -o "$TMP/grf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/neuron_read_test.tv" -o "$TMP/nrt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/grf.ll" -o "$TMP/grf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/nrt.ll" -o "$TMP/nrt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/grf.o" "$TMP/nrt.o" -o "$TMP/nrt" 2>/dev/null || {
    echo "dynfield-neuron-read: FAIL (build)"; NR_OK=0; }
fi
if [ "$NR_OK" = "1" ]; then
    GOT=$("$TMP/nrt" 2>/dev/null)
    if [ "$GOT" = "$EXPECTED_NR" ]; then
        echo "dynfield-neuron-read: PASS (the circle reads a neuron: ReLU ridge -> nseg=2 boundary recovers the kink, field-invariant; smooth ridge shatters@exact / converges@threshold = measured transcendental residual)"
    else
        echo "dynfield-neuron-read: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_NR"
        echo "--- got ---"; echo "$GOT"
        NR_OK=0
    fi
fi
if [ "$NR_OK" = "0" ]; then FAILURES="$FAILURES neuron-read"; fi

# ---- Phase A.3c gate: the genus certificate taxonomy (the A<->D bridge) ----
# For each shape: MEASURE (d*, nseg), BUILD the runtime PLONK circuit AT d* (so
# the circuit topology IS the genus signature), BIND the data, PROVE + VERIFY,
# reject 3 tampers. line d*=1 nseg=1; sawtooth d*=1 nseg=2 (same genus, broken
# partition); quadratic d*=2 nseg=1 (genus axis moves). Per shape:
#   d* nseg verify(1) wtamper(0) dtamper(0) ftamper(0).
# Five-file link: plonk_dyn.o + regime_fri.o + genus_probe.o + runtime_circuit.o
# + genus_certify.o.

GC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GC_OK=0; fi
if [ "$GC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/gcpd.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/gcrf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_probe.tv" -o "$TMP/gcgp.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/runtime_circuit.tv" -o "$TMP/gcrc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_certify.tv" -o "$TMP/gc.ll" >/dev/null 2>&1 || {
    echo "dynfield-genus-certify: FAIL (compile)"; GC_OK=0; }
fi
if [ "$GC_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gcpd.ll" -o "$TMP/gcpd.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gcrf.ll" -o "$TMP/gcrf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gcgp.ll" -o "$TMP/gcgp.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gcrc.ll" -o "$TMP/gcrc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gc.ll" -o "$TMP/gc.o" 2>/dev/null || {
    echo "dynfield-genus-certify: FAIL (llc)"; GC_OK=0; }
fi
if [ "$GC_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gcpd.o" "$TMP/gcrf.o" "$TMP/gcgp.o" "$TMP/gcrc.o" "$TMP/gc.o" -o "$TMP/gc" 2>/dev/null || {
    echo "dynfield-genus-certify: FAIL (link)"; GC_OK=0; }
fi
EXPECTED_GC="1
1
1
0
0
0
1
2
1
0
0
0
2
1
1
0
0
0"
if [ "$GC_OK" = "1" ]; then
    GOT=$("$TMP/gc")
    if [ "$GOT" = "$EXPECTED_GC" ]; then
        echo "dynfield-genus-certify: PASS (genus certificate taxonomy: line=1,1 saw=1,2 quad=2,1; each proved at d*, 3 tampers rejected)"
    else
        echo "dynfield-genus-certify: FAIL"
        echo "--- expected ---"; echo "$EXPECTED_GC"
        echo "--- got ---"; echo "$GOT"
        GC_OK=0
    fi
fi
if [ "$GC_OK" = "0" ]; then FAILURES="$FAILURES genus-certify"; fi

# ---- Phase A.3d gate: the honest edge (test for what does NOT exist) ----
# A synthesized sawtooth resolves to an exact genus (onset=1); a real slice of
# the Nether soundtrack (Pigstep) does NOT resolve at any degree up to d_max=12
# (onset sentinel = 13), nseg saturates at the trivial maximum (4). The no-onset
# result is a first-class measurement — the boundary where exact-structure
# methods run out on real recorded audio. Three-file link: regime_fri.o +
# genus_probe.o + genus_edge_test.o.

run_genus_gate "genus_edge_test" "dynfield-genus-edge" \
    "1
13
4" "honest edge: sawtooth resolves (onset=1); Pigstep does NOT (no onset<=12, nseg=4)"

# ---- Phase D.2 gate: the Newton-Degree Aliasing Theorem, certified ----
# The ARITHMETIC boundary of the genus program. A parabola whose 2nd difference
# is exactly 251 reads as a LINE (d*=1) over Field<251> but a PARABOLA (d*=2)
# over Field<65537): the field can only LOWER the Newton degree (one-sided
# aliasing, p | witnessing difference). genus_certify_crt2 DETECTS the alias
# (soundness, -> -1); genus_certify_crt3 RESOLVES to the integer truth via
# one-sided max-vote (completeness, -> 2); genus_safe_prime_bound = 2*max|fdiff|+1
# predicts which primes are safe. Four-file link: regime_fri.o + genus_probe.o +
# genus_alias.o + genus_alias_test.o.

GA_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GA_OK=0; fi
if [ "$GA_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/garf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_probe.tv" -o "$TMP/gagp.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_alias.tv" -o "$TMP/ga.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_alias_test.tv" -o "$TMP/gat.ll" >/dev/null 2>&1 || {
    echo "dynfield-genus-alias: FAIL (compile)"; GA_OK=0; }
fi
if [ "$GA_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/garf.ll" -o "$TMP/garf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gagp.ll" -o "$TMP/gagp.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/ga.ll" -o "$TMP/ga.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gat.ll" -o "$TMP/gat.o" 2>/dev/null || {
    echo "dynfield-genus-alias: FAIL (llc)"; GA_OK=0; }
fi
if [ "$GA_OK" = "1" ]; then
clang $LINK_PIE "$TMP/garf.o" "$TMP/gagp.o" "$TMP/ga.o" "$TMP/gat.o" -o "$TMP/gat" 2>/dev/null || {
    echo "dynfield-genus-alias: FAIL (link)"; GA_OK=0; }
fi
EXPECTED_GA="1
2
-1
2
5031
2
2
71"
if [ "$GA_OK" = "1" ]; then
    GOT=$("$TMP/gat")
    if [ "$GOT" = "$EXPECTED_GA" ]; then
        echo "dynfield-genus-alias: PASS (Newton-degree aliasing: 251 sees false d*=1; crt2 detects, crt3 resolves to 2; safe bound 5031)"
    else
        echo "dynfield-genus-alias: FAIL"; echo "--- expected ---"; echo "$EXPECTED_GA"
        echo "--- got ---"; echo "$GOT"; GA_OK=0
    fi
fi
if [ "$GA_OK" = "0" ]; then FAILURES="$FAILURES genus-alias"; fi

# ---- Phase D.4 gate: the degree filtration + the exact<->soft boundary ----
# The canonical genus invariant is the PROFILE nseg(d), not the point (d*,nseg).
# line+parab and parab+line yield the IDENTICAL filtration [12,4,2,2,2] (order-
# independent), and greedy == global at every degree (difference-fit is
# infix-closed -> canonically greedy; no exact<->soft gap within the difference
# family — the boundary is the FIT CRITERION, not the data). Three-file link:
# regime_fri.o + genus_profile.o + genus_soft_test.o.

GS_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GS_OK=0; fi
if [ "$GS_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/gsrf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_profile.tv" -o "$TMP/gspr.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_soft_test.tv" -o "$TMP/gst.ll" >/dev/null 2>&1 || {
    echo "dynfield-genus-soft: FAIL (compile)"; GS_OK=0; }
fi
if [ "$GS_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gsrf.ll" -o "$TMP/gsrf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gspr.ll" -o "$TMP/gspr.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gst.ll" -o "$TMP/gst.o" 2>/dev/null || {
    echo "dynfield-genus-soft: FAIL (llc)"; GS_OK=0; }
fi
if [ "$GS_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gsrf.o" "$TMP/gspr.o" "$TMP/gst.o" -o "$TMP/gst" 2>/dev/null || {
    echo "dynfield-genus-soft: FAIL (link)"; GS_OK=0; }
fi
EXPECTED_GS="12
4
2
2
2
12
4
2
2
2
1
1"
if [ "$GS_OK" = "1" ]; then
    GOT=$("$TMP/gst")
    if [ "$GOT" = "$EXPECTED_GS" ]; then
        echo "dynfield-genus-soft: PASS (degree filtration order-independent [12,4,2,2,2]; greedy==global, difference-fit infix-closed)"
    else
        echo "dynfield-genus-soft: FAIL"; echo "--- expected ---"; echo "$EXPECTED_GS"
        echo "--- got ---"; echo "$GOT"; GS_OK=0
    fi
fi
if [ "$GS_OK" = "0" ]; then FAILURES="$FAILURES genus-soft"; fi

# ---- Phase Z.3 gate: the ZK adaptive-degree (genus) certificate ----
# "What degree is this shape?" -> the MDL-optimal adaptive segmentation (per-
# segment minimal degree), ZK-certified. FIT: one PLONK proof over the whole
# mixed-degree descriptor (each piece is exactly degree d_i; deg-0 pieces rest on
# the data-binding). STABILITY: every interior boundary is merge-forced (necessary
# for MDL-optimality, claimed exactly, not "globally optimal"). Safe prime (D.2)
# so the degrees are integer-true. line+quad [1,2], quad+cubic [2,3], const+lin+
# quad [0,1,2] (d=0 first-class), single cubic [3] (FIT only). 3 tampers rejected.
# Six-file link: plonk_dyn + regime_fri + genus_probe + genus_adaptive +
# runtime_circuit + genus_alias + genus_adaptive_certify.

GAC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GAC_OK=0; fi
if [ "$GAC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/gacpd.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/gacrf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_probe.tv" -o "$TMP/gacgp.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_alias.tv" -o "$TMP/gacga.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_adaptive.tv" -o "$TMP/gacgad.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/runtime_circuit.tv" -o "$TMP/gacrc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_adaptive_certify.tv" -o "$TMP/gac.ll" >/dev/null 2>&1 || {
    echo "dynfield-genus-adaptive: FAIL (compile)"; GAC_OK=0; }
fi
if [ "$GAC_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gacpd.ll" -o "$TMP/gacpd.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gacrf.ll" -o "$TMP/gacrf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gacgp.ll" -o "$TMP/gacgp.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gacga.ll" -o "$TMP/gacga.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gacgad.ll" -o "$TMP/gacgad.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gacrc.ll" -o "$TMP/gacrc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gac.ll" -o "$TMP/gac.o" 2>/dev/null || {
    echo "dynfield-genus-adaptive: FAIL (llc)"; GAC_OK=0; }
fi
if [ "$GAC_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gacpd.o" "$TMP/gacrf.o" "$TMP/gacgp.o" "$TMP/gacga.o" "$TMP/gacgad.o" \
      "$TMP/gacrc.o" "$TMP/gac.o" -o "$TMP/gac" 2>/dev/null || {
    echo "dynfield-genus-adaptive: FAIL (link)"; GAC_OK=0; }
fi
EXPECTED_GAC="2
1
2
1
1
0
0
0
2
2
3
1
1
0
0
0
3
0
1
2
1
1
0
0
0
1
3
1
1
0
0
0"
if [ "$GAC_OK" = "1" ]; then
    GOT=$("$TMP/gac")
    if [ "$GOT" = "$EXPECTED_GAC" ]; then
        echo "dynfield-genus-adaptive: PASS (MDL adaptive degree: [1,2] [2,3] [0,1,2] [3]; FIT+STABILITY proven, 3 tampers rejected)"
    else
        echo "dynfield-genus-adaptive: FAIL"; echo "--- expected ---"; echo "$EXPECTED_GAC"
        echo "--- got ---"; echo "$GOT"; GAC_OK=0
    fi
fi
if [ "$GAC_OK" = "0" ]; then FAILURES="$FAILURES genus-adaptive"; fi

# ---- Phase Z.4 gate: the ZK fixed-degree (crisp) genus certificate ----
# The companion to Z.3. For a single-degree shape, certify the degree FILTRATION
# nseg(d) + its MEANINGFUL PLATEAU (value P over [d*,d_end] where maxseg>d+1) +
# greedy==global (infix-closure, D.4) + a PLONK proof of the circuit at d*. line
# -> [8,1,1,1,1] plateau d*=1..4 P=1; single quad -> [8,4,1,1,1] plateau d*=2..4
# P=1. 3 tampers rejected. Z.3 (soft/adaptive) + Z.4 (crisp/fixed) certify the
# genus from BOTH D.4 boundaries. Four-file link: plonk_dyn + regime_fri +
# genus_profile + runtime_circuit + genus_profile_certify.

GPC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GPC_OK=0; fi
if [ "$GPC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/crypto/plonk_dyn.tv" -o "$TMP/gpcpd.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/gpcrf.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_profile.tv" -o "$TMP/gpcgp.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/runtime_circuit.tv" -o "$TMP/gpcrc.ll" >/dev/null 2>&1 \
  && "$TMP/tvc_self" "$REPO_DIR/examples/genus_profile_certify.tv" -o "$TMP/gpc.ll" >/dev/null 2>&1 || {
    echo "dynfield-genus-profile-cert: FAIL (compile)"; GPC_OK=0; }
fi
if [ "$GPC_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gpcpd.ll" -o "$TMP/gpcpd.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gpcrf.ll" -o "$TMP/gpcrf.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gpcgp.ll" -o "$TMP/gpcgp.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gpcrc.ll" -o "$TMP/gpcrc.o" 2>/dev/null \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gpc.ll" -o "$TMP/gpc.o" 2>/dev/null || {
    echo "dynfield-genus-profile-cert: FAIL (llc)"; GPC_OK=0; }
fi
if [ "$GPC_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gpcpd.o" "$TMP/gpcrf.o" "$TMP/gpcgp.o" "$TMP/gpcrc.o" "$TMP/gpc.o" -o "$TMP/gpc" 2>/dev/null || {
    echo "dynfield-genus-profile-cert: FAIL (link)"; GPC_OK=0; }
fi
EXPECTED_GPC="8
1
1
1
1
1
4
1
1
1
0
0
0
8
4
1
1
1
2
4
1
1
1
0
0
0"
if [ "$GPC_OK" = "1" ]; then
    GOT=$("$TMP/gpc")
    if [ "$GOT" = "$EXPECTED_GPC" ]; then
        echo "dynfield-genus-profile-cert: PASS (fixed-degree filtration + plateau: line [8,1,1,1,1] d*1; quad [8,4,1,1,1] d*2; greedy==global, proven, 3 tampers)"
    else
        echo "dynfield-genus-profile-cert: FAIL"; echo "--- expected ---"; echo "$EXPECTED_GPC"
        echo "--- got ---"; echo "$GOT"; GPC_OK=0
    fi
fi
if [ "$GPC_OK" = "0" ]; then FAILURES="$FAILURES genus-profile-cert"; fi

# ---- Phase A.3d (live): the WAV-reading instrument, when the corpus is present.
# genus_wav.tv reads a real 16-bit PCM WAV and measures the genus signature live.
# Guarded by file existence so the suite stays portable. Confirms the embedded
# edge gate above matches a live read of the same Pigstep track.
PIGSTEP="$REPO_DIR/../wav-test/04. Lena Raine - Pigstep (Mono Mix).wav"
if [ -f "$PIGSTEP" ] && [ -x "$TMP/tvc_self" ]; then
    GW_OK=1
    "$TMP/tvc_self" "$REPO_DIR/src/lib/regime/regime_fri.tv" -o "$TMP/gwrf.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$REPO_DIR/src/lib/genus/genus_probe.tv" -o "$TMP/gwgp.ll" >/dev/null 2>&1 \
      && "$TMP/tvc_self" "$REPO_DIR/examples/genus_wav.tv" -o "$TMP/gw.ll" >/dev/null 2>&1 \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gwrf.ll" -o "$TMP/gwrf.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gwgp.ll" -o "$TMP/gwgp.o" 2>/dev/null \
      && "$LLC" $LLC_TARGET -filetype=obj "$TMP/gw.ll" -o "$TMP/gw.o" 2>/dev/null \
      && clang $LINK_PIE "$TMP/gwrf.o" "$TMP/gwgp.o" "$TMP/gw.o" -o "$TMP/gw" 2>/dev/null || GW_OK=0
    if [ "$GW_OK" = "1" ]; then
        GOT=$("$TMP/gw" "$PIGSTEP")
        EXP="44100
12
13
4"
        if [ "$GOT" = "$EXP" ]; then
            echo "dynfield-genus-wav-live: PASS (live WAV read of Pigstep: 44100Hz, no onset<=12, nseg=4 — matches embedded edge gate)"
        else
            echo "dynfield-genus-wav-live: FAIL"; echo "--- expected ---"; echo "$EXP"
            echo "--- got ---"; echo "$GOT"; FAILURES="$FAILURES genus-wav-live"
        fi
    else
        echo "dynfield-genus-wav-live: FAIL (build)"; FAILURES="$FAILURES genus-wav-live"
    fi
else
    echo "dynfield-genus-wav-live: SKIP (wav-test corpus not present)"
fi

# ---- B-paramtypes Slice 2 gate: generic structs (tvc_self ONLY) ----
# Generic structs (struct Vec<T> {...}) have no bootstrap counterpart —
# src/tvc.c's StructInfo carries no generic metadata. This gate compiles
# the generic_struct example through Stage 1 tvc_self: single/multi-param
# templates, pointer fields, method dispatch, and heap arrays of instances.
GS_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GS_OK=0; fi
if [ "$GS_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/generic_struct.tv" -o "$TMP/gstruct.ll" >/dev/null 2>&1 || {
    echo "dynfield-generic-struct: FAIL (compile)"; GS_OK=0; }
fi
if [ "$GS_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/gstruct.ll" -o "$TMP/gstruct.o" 2>/dev/null || {
    echo "dynfield-generic-struct: FAIL (llc)"; GS_OK=0; }
fi
if [ "$GS_OK" = "1" ]; then
clang $LINK_PIE "$TMP/gstruct.o" -o "$TMP/gstruct" 2>/dev/null || {
    echo "dynfield-generic-struct: FAIL (link)"; GS_OK=0; }
fi
if [ "$GS_OK" = "1" ]; then
    GOT=$("$TMP/gstruct")
    EXP="42
7
2
4
100
200
1000
5
2
30
40"
    if [ "$GOT" = "$EXP" ]; then
        echo "dynfield-generic-struct: PASS (Box<T>/Vec<T>/Pair<A,B> templates, ptr fields, method dispatch, heap instance arrays)"
    else
        echo "dynfield-generic-struct: FAIL"; echo "--- expected ---"; echo "$EXP"
        echo "--- got ---"; echo "$GOT"; GS_OK=0
    fi
fi
if [ "$GS_OK" = "0" ]; then FAILURES="$FAILURES generic-struct"; fi

# ---- B-paramtypes Slice 3 gate: const generics (tvc_self ONLY) ----
# Const generic params (struct Buf<T, const N>) substitute a compile-time
# integer into array sizes [T; N]. No bootstrap counterpart. This gate
# compiles the const_generic example: array-size substitution, distinct
# instances by const value + type, alloc sizing, function over instance.
CG_OK=1
if [ ! -x "$TMP/tvc_self" ]; then CG_OK=0; fi
if [ "$CG_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/const_generic.tv" -o "$TMP/cgen.ll" >/dev/null 2>&1 || {
    echo "dynfield-const-generic: FAIL (compile)"; CG_OK=0; }
fi
if [ "$CG_OK" = "1" ]; then
"$LLC" $LLC_TARGET -filetype=obj "$TMP/cgen.ll" -o "$TMP/cgen.o" 2>/dev/null || {
    echo "dynfield-const-generic: FAIL (llc)"; CG_OK=0; }
fi
if [ "$CG_OK" = "1" ]; then
clang $LINK_PIE "$TMP/cgen.o" -o "$TMP/cgen" 2>/dev/null || {
    echo "dynfield-const-generic: FAIL (link)"; CG_OK=0; }
fi
if [ "$CG_OK" = "1" ]; then
    GOT=$("$TMP/cgen")
    EXP="3
100
200
3
40
9"
    if [ "$GOT" = "$EXP" ]; then
        echo "dynfield-const-generic: PASS (Buf<T,const N> array-size substitution, distinct instances by value+type, alloc sizing)"
    else
        echo "dynfield-const-generic: FAIL"; echo "--- expected ---"; echo "$EXP"
        echo "--- got ---"; echo "$GOT"; CG_OK=0
    fi
fi
if [ "$CG_OK" = "0" ]; then FAILURES="$FAILURES const-generic"; fi

# ---- B1a Slice 1 gate: vec_test (tvc_self ONLY, self-contained) ----
# Generic growable array Vec<T>.  Via-negativa probes: Vec<i32> realloc-
# doubling (5 pushes force 4->8), pop/len/get; method-vs-free parity
# (v.get == vec_get); Vec<Pt> struct element (ctx_elem_size must size
# sizeof(Pt) under a generic-substituted T inside vec_push).  Surfaced and
# fixed five compiler gaps: optional fn generic bound, structural generic
# inference through *Vec<T> args + Vec<T> return, generic-app type
# substitution (*Vec<T> -> *Vec<i32> in the body, no %Vec_T leak), struct-
# value let alloca, and struct-element index-assign load.
EXPECTED_VEC="5
40
40
4
10
1
30
40"

VEC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then VEC_OK=0; fi
if [ "$VEC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/vec_test.tv" -o "$TMP/vec_test.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/vec_test.ll" -o "$TMP/vec_test.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/vec_test.o" -o "$TMP/vec_test" 2>/dev/null || {
    echo "dynfield-vec: FAIL (build)"; VEC_OK=0; }
fi
if [ "$VEC_OK" = "1" ]; then
    GOT=$("$TMP/vec_test")
    if [ "$GOT" = "$EXPECTED_VEC" ]; then
        echo "dynfield-vec: PASS (Vec<T> realloc-doubling, pop/len/get, method==free parity, Vec<Pt> struct element)"
    else
        echo "dynfield-vec: FAIL"; echo "--- expected ---"; echo "$EXPECTED_VEC"
        echo "--- got ---"; echo "$GOT"; VEC_OK=0
    fi
fi
if [ "$VEC_OK" = "0" ]; then FAILURES="$FAILURES vec"; fi

# ---- B1a Slice 1 gate: vec_export_test (tvc_self ONLY, 2-file link) ----
# #[export]/instantiate ABI verify: vec.tv emits dso_local monomorphized
# instances (vec_new_i32, ...) via `instantiate vec_*<i32>;`; the caller
# links them via extern decls.  Probe A (struct-redeclare): names Vec<i32>
# in extern sigs + by-value struct return crosses the boundary.  Probe B
# (opaque heap handle): pointer-only boundary.  THE B1b FINDING: generic-
# struct layouts ARE ABI-stable across object boundaries (greenlights a
# future .dylib stdlib).
EXPECTED_VEC_EXP="100
200
100
200"

VEXP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then VEXP_OK=0; fi
if [ "$VEXP_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/src/lib/collections/vec.tv" -o "$TMP/vec.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/vec.ll" -o "$TMP/vec.o" 2>/dev/null \
  && "$TMP/tvc_self" "$REPO_DIR/examples/vec_export_test.tv" -o "$TMP/vet.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/vet.ll" -o "$TMP/vet.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/vec.o" "$TMP/vet.o" -o "$TMP/vet" 2>/dev/null || {
    echo "dynfield-vec-export: FAIL (build)"; VEXP_OK=0; }
fi
if [ "$VEXP_OK" = "1" ]; then
    GOT=$("$TMP/vet")
    if [ "$GOT" = "$EXPECTED_VEC_EXP" ]; then
        echo "dynfield-vec-export: PASS (generic-struct ABI crosses object boundary: by-value return + opaque handle)"
    else
        echo "dynfield-vec-export: FAIL"; echo "--- expected ---"; echo "$EXPECTED_VEC_EXP"
        echo "--- got ---"; echo "$GOT"; VEXP_OK=0
    fi
fi
if [ "$VEXP_OK" = "0" ]; then FAILURES="$FAILURES vec-export"; fi

# ---- B1a Slice 2 gate: string_test (tvc_self ONLY, self-contained) ----
# Owned length-carrying byte string Str (its own concrete struct, same
# realloc-doubling as Vec but monomorphic).  build+concat, str_eq, and the
# VIA-NEGATIVA embedded-NUL probe: {a,NUL,x} vs {a,NUL,y} compare UNEQUAL
# (length-based), where a NUL-terminated str_eq would wrongly say equal.
EXPECTED_STR="3
104
1
0"

STR_OK=1
if [ ! -x "$TMP/tvc_self" ]; then STR_OK=0; fi
if [ "$STR_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/string_test.tv" -o "$TMP/string_test.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/string_test.ll" -o "$TMP/string_test.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/string_test.o" -o "$TMP/string_test" 2>/dev/null || {
    echo "dynfield-string: FAIL (build)"; STR_OK=0; }
fi
if [ "$STR_OK" = "1" ]; then
    GOT=$("$TMP/string_test")
    if [ "$GOT" = "$EXPECTED_STR" ]; then
        echo "dynfield-string: PASS (Str build/concat/byte, length-first str_eq, embedded-NUL probe beats NUL-terminated compare)"
    else
        echo "dynfield-string: FAIL"; echo "--- expected ---"; echo "$EXPECTED_STR"
        echo "--- got ---"; echo "$GOT"; STR_OK=0
    fi
fi
if [ "$STR_OK" = "0" ]; then FAILURES="$FAILURES string"; fi

# ---- B1a Slice 3 gate: hashmap_test (tvc_self ONLY, self-contained) ----
# Generic open-addressed HashMap<K,V> (Option B: kw-byte hashing, key width
# as runtime arg; DJB2 general keys + value-direct field keys; fixed cap=16,
# no rehash, loud abort when full).  Via-negativa probes: int key get,
# FORCED collision (42 & 58 share a bucket at cap 16, both retrievable),
# overwrite (new value, len unchanged), absent key (probe-to-empty), struct
# key Pt (kw=8 heterogeneous width), and the field-key sub-probe (mode 1
# value-direct hash, sound by gcd(p,2^k)=1).  HashMap surfaced NO new
# compiler walls — Slice 1's generic machinery covered <K,V> inference,
# *HashMap<K,V> args, and struct-value returns.
EXPECTED_HM="77
1
88
2
0
1
1"

HM_OK=1
if [ ! -x "$TMP/tvc_self" ]; then HM_OK=0; fi
if [ "$HM_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/hashmap_test.tv" -o "$TMP/hashmap_test.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/hashmap_test.ll" -o "$TMP/hashmap_test.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/hashmap_test.o" -o "$TMP/hashmap_test" 2>/dev/null || {
    echo "dynfield-hashmap: FAIL (build)"; HM_OK=0; }
fi
if [ "$HM_OK" = "1" ]; then
    GOT=$("$TMP/hashmap_test")
    if [ "$GOT" = "$EXPECTED_HM" ]; then
        echo "dynfield-hashmap: PASS (HashMap<K,V> Option B: int/struct/field keys, collision, overwrite, absent, value-direct field hash)"
    else
        echo "dynfield-hashmap: FAIL"; echo "--- expected ---"; echo "$EXPECTED_HM"
        echo "--- got ---"; echo "$GOT"; HM_OK=0
    fi
fi
if [ "$HM_OK" = "0" ]; then FAILURES="$FAILURES hashmap"; fi

# ---- B1b Slice 1 gate: import (tvc_self ONLY, Model A source inclusion) ----
# `import "path";` splices imported files' tokens into one compilation unit,
# so cross-file functions — INCLUDING generics — resolve with no extern decls
# and no instantiate (the B1a pain point, healed). import_test.tv imports
# import_mathlib.tv and calls ml_add/ml_square + generic ml_scale over both
# i32 and Field<251>.  Path resolves relative to the importing file.
EXPECTED_IMP="7
25
30
149"

IMP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then IMP_OK=0; fi
if [ "$IMP_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/import_test.tv" -o "$TMP/import_test.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/import_test.ll" -o "$TMP/import_test.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/import_test.o" -o "$TMP/import_test" 2>/dev/null || {
    echo "dynfield-import: FAIL (build)"; IMP_OK=0; }
fi
if [ "$IMP_OK" = "1" ]; then
    GOT=$("$TMP/import_test")
    if [ "$GOT" = "$EXPECTED_IMP" ]; then
        echo "dynfield-import: PASS (Model A source inclusion: cross-file fns + generics, transitive/diamond/cycle-safe, no extern/instantiate)"
    else
        echo "dynfield-import: FAIL"; echo "--- expected ---"; echo "$EXPECTED_IMP"
        echo "--- got ---"; echo "$GOT"; IMP_OK=0
    fi
fi
if [ "$IMP_OK" = "0" ]; then FAILURES="$FAILURES import"; fi

# ---- B1b Slice 1 negative: duplicate-definition guard ----
# Two files defining the same fn name must fail with a CLEAN compiler
# diagnostic (file:line "duplicate function definition"), not a cryptic llc
# "invalid redefinition" downstream.  Built inline in $TMP.
DUP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then DUP_OK=0; fi
if [ "$DUP_OK" = "1" ]; then
    printf 'fn dup_helper() -> i32 { return 1; }\n' > "$TMP/dup_lib.tv"
    printf 'import "dup_lib.tv";\nfn dup_helper() -> i32 { return 2; }\nfn main() -> i32 { print(dup_helper()); return 0; }\n' > "$TMP/dup_main.tv"
    DUP_OUT=$("$TMP/tvc_self" "$TMP/dup_main.tv" -o "$TMP/dup_main.ll" 2>&1)
    DUP_RC=$?
    if [ "$DUP_RC" != "0" ] && echo "$DUP_OUT" | grep -q "duplicate function definition"; then
        echo "dynfield-import-dup: PASS (duplicate definition across import caught with clean diagnostic, exit nonzero)"
    else
        echo "dynfield-import-dup: FAIL (rc=$DUP_RC)"; echo "$DUP_OUT" | tail -3; DUP_OK=0
    fi
fi
if [ "$DUP_OK" = "0" ]; then FAILURES="$FAILURES import-dup"; fi

# ---- B1b Slice 2 gate: import the real Vec<T> stdlib (the B1a->B1b loop) ----
# import_vec_test.tv consumes src/lib/collections/vec.tv via `import`, with the SAME
# vec_new/push/get/pop calls that previously needed inlined defs (vec_test)
# or hand-mangled externs + multi-object link (vec_export_test). Now one
# import, one unit, generics monomorphize in place. Confirms instantiate
# directives in the imported lib coexist with call-site monomorphization
# (mono cache dedups — single vec_new_i32 body, no duplication).
EXPECTED_IVEC="3
20
20
2
100"

IVEC_OK=1
if [ ! -x "$TMP/tvc_self" ]; then IVEC_OK=0; fi
if [ "$IVEC_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/import_vec_test.tv" -o "$TMP/ivt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/ivt.ll" -o "$TMP/ivt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/ivt.o" -o "$TMP/ivt" 2>/dev/null || {
    echo "dynfield-import-vec: FAIL (build)"; IVEC_OK=0; }
fi
if [ "$IVEC_OK" = "1" ]; then
    GOT=$("$TMP/ivt")
    if [ "$GOT" = "$EXPECTED_IVEC" ]; then
        echo "dynfield-import-vec: PASS (real Vec<T> stdlib via import — B1a->B1b loop closed, instantiate+call-site mono coexist)"
    else
        echo "dynfield-import-vec: FAIL"; echo "--- expected ---"; echo "$EXPECTED_IVEC"
        echo "--- got ---"; echo "$GOT"; IVEC_OK=0
    fi
fi
if [ "$IVEC_OK" = "0" ]; then FAILURES="$FAILURES import-vec"; fi

# ---- B2a Slice 0 gate: generic enums (tvc_self ONLY) ----
# Option<T> / Result<T,E> monomorphize per instantiation (%Option_i32,
# %Option_i64, %Option_Pt, %Result_i32_i32), each recomputing payload size
# from substituted variant types. Payload T travels through construction,
# match destructuring, and function return/parameter positions. Retires the
# B1a Option<V> requirement (a fn can return "found V or nothing"). Pure
# parametric polymorphism — no dispatch, consistent with the field thesis.
EXPECTED_GE="42
99
7
13
3
4
5
-1"

GE_OK=1
if [ ! -x "$TMP/tvc_self" ]; then GE_OK=0; fi
if [ "$GE_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/generic_enum.tv" -o "$TMP/ge.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/ge.ll" -o "$TMP/ge.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/ge.o" -o "$TMP/ge" 2>/dev/null || {
    echo "dynfield-generic-enum: FAIL (build)"; GE_OK=0; }
fi
if [ "$GE_OK" = "1" ]; then
    GOT=$("$TMP/ge")
    if [ "$GOT" = "$EXPECTED_GE" ]; then
        echo "dynfield-generic-enum: PASS (Option<T>/Result<T,E>: construct/match/destructure, struct+wide payloads, fn return/param)"
    else
        echo "dynfield-generic-enum: FAIL"; echo "--- expected ---"; echo "$EXPECTED_GE"
        echo "--- got ---"; echo "$GOT"; GE_OK=0
    fi
fi
if [ "$GE_OK" = "0" ]; then FAILURES="$FAILURES generic-enum"; fi

# ---- #31 gate: nested match + aggregate enum payloads (tvc_self ONLY) ----
# Pins two fixed defects: (1) the match-arm arena bug — a nested match inside
# an arm body clobbered/interleaved the outer match's arm records (bindings
# vanished, arms mis-dispatched); (2) enum payload sizing — a >8-byte struct
# payload (and enum-typed payloads) truncated on construction and by-value
# returns because sizing guessed 4/8 bytes for aggregates. payload_field_size
# now shares one computation across register/instantiate/offsets.
EXPECTED_EMN="21
8
500
510
9
1
65"

EMN_OK=1
if [ ! -x "$TMP/tvc_self" ]; then EMN_OK=0; fi
if [ "$EMN_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/enum_match_nested.tv" -o "$TMP/emn.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/emn.ll" -o "$TMP/emn.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/emn.o" -o "$TMP/emn" 2>/dev/null || {
    echo "dynfield-enum-match-nested: FAIL (build)"; EMN_OK=0; }
fi
if [ "$EMN_OK" = "1" ]; then
    GOT=$("$TMP/emn")
    if [ "$GOT" = "$EXPECTED_EMN" ]; then
        echo "dynfield-enum-match-nested: PASS (nested match arms survive; >8-byte struct + enum payloads round-trip; multi-field offsets)"
    else
        echo "dynfield-enum-match-nested: FAIL"; echo "--- expected ---"; echo "$EXPECTED_EMN"
        echo "--- got ---"; echo "$GOT"; EMN_OK=0
    fi
fi
if [ "$EMN_OK" = "0" ]; then FAILURES="$FAILURES enum-match-nested"; fi

# ---- B2a Slice 1 gate: function pointers (tvc_self ONLY) ----
# The language's first code pointer — the predictive-layer primitive. A
# strategy travels as a runtime value (apply(&double,21), dispatch selecting
# &add/&mul/&sub at runtime). fn(T)->R type, address-of-function, indirect
# call.
EXPECTED_FNP="42
42
10
30
200
10"

FNP_OK=1
if [ ! -x "$TMP/tvc_self" ]; then FNP_OK=0; fi
if [ "$FNP_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/fn_pointer.tv" -o "$TMP/fnp.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/fnp.ll" -o "$TMP/fnp.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/fnp.o" -o "$TMP/fnp" 2>/dev/null || {
    echo "dynfield-fn-pointer: FAIL (build)"; FNP_OK=0; }
fi
if [ "$FNP_OK" = "1" ]; then
    GOT=$("$TMP/fnp")
    if [ "$GOT" = "$EXPECTED_FNP" ]; then
        echo "dynfield-fn-pointer: PASS (fn(T)->R values, address-of-fn, indirect call, runtime strategy dispatch)"
    else
        echo "dynfield-fn-pointer: FAIL"; echo "--- expected ---"; echo "$EXPECTED_FNP"
        echo "--- got ---"; echo "$GOT"; FNP_OK=0
    fi
fi
if [ "$FNP_OK" = "0" ]; then FAILURES="$FAILURES fn-pointer"; fi

# ---- B2a Slice 1 THESIS gate: the function-pointer parallelization fence ----
# An indirect call is an unprovable callee -> the loop stays sequential, while
# a structurally identical direct pure-field loop parallelizes. Asserts EXACTLY
# 1 worker: direct_loop=1 (parallel), map_loop=0 (fenced). Crossing into a
# function pointer costs parallelism, never soundness — the field thesis stays
# intact because the code pointer is fenced out of the auto-parallel regime.
FENCE_OK=1
if [ ! -x "$TMP/tvc_self" ]; then FENCE_OK=0; fi
if [ "$FENCE_OK" = "1" ]; then
    "$TMP/tvc_self" "$REPO_DIR/examples/fn_pointer_fence.tv" -o "$TMP/fence.ll" >/dev/null 2>&1 || {
        echo "dynfield-fn-pointer-fence: FAIL (compile)"; FENCE_OK=0; }
fi
if [ "$FENCE_OK" = "1" ]; then
    NW=$(grep -c "define internal void @__pfor_worker" "$TMP/fence.ll" || true)
    if [ "$NW" = "1" ]; then
        echo "dynfield-fn-pointer-fence: PASS (indirect-call loop stays sequential; pure-field loop parallelizes — 1 worker)"
    else
        echo "dynfield-fn-pointer-fence: FAIL (want 1 worker, got $NW)"; FENCE_OK=0
    fi
fi
if [ "$FENCE_OK" = "0" ]; then FAILURES="$FAILURES fn-pointer-fence"; fi

# ---- B2b Slice 2 gate: traits + impl blocks (tvc_self ONLY) ----
# The decision-layer foundation. A trait is a compile-time contract; an impl
# binds it to a type. Methods are statically monomorphized functions (NO
# vtable — the Rubicon is not crossed). Registered under the trait-qualified
# mangled name Trait__Type__method and called explicitly by it (type-directed
# resolution is Slice 3). The core win: two impls of `area` (Rect, Circle)
# plus Rect also impl'ing Eq — all coexist with ZERO collision (the
# name-keyed-dispatch wound healed). self -> &Type (by-ref receiver).
EXPECTED_TI="200
78
30
1"

TI_OK=1
if [ ! -x "$TMP/tvc_self" ]; then TI_OK=0; fi
if [ "$TI_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/trait_impl.tv" -o "$TMP/ti.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/ti.ll" -o "$TMP/ti.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/ti.o" -o "$TMP/ti" 2>/dev/null || {
    echo "dynfield-trait-impl: FAIL (build)"; TI_OK=0; }
fi
if [ "$TI_OK" = "1" ]; then
    GOT=$("$TMP/ti")
    if [ "$GOT" = "$EXPECTED_TI" ]; then
        echo "dynfield-trait-impl: PASS (trait decl + impl...for, two coexisting impls + multi-trait, trait-qualified mangling, self->&Type)"
    else
        echo "dynfield-trait-impl: FAIL"; echo "--- expected ---"; echo "$EXPECTED_TI"
        echo "--- got ---"; echo "$GOT"; TI_OK=0
    fi
fi
if [ "$TI_OK" = "0" ]; then FAILURES="$FAILURES trait-impl"; fi

# ---- B2b Slice 3 gate: type-directed dispatch + bound enforcement ----
# Makes traits USABLE: r.area() resolves to Shape__Rect__area through the
# receiver type (type-directed, not name-keyed), and fn<T: Shape> dispatches
# x.area() through the monomorphized concrete type. All statically
# monomorphized — no vtable. g_gen_bounds (write-only since B1a) is now read.
EXPECTED_TD="200
78
60
200
78"

TD_OK=1
if [ ! -x "$TMP/tvc_self" ]; then TD_OK=0; fi
if [ "$TD_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/trait_dispatch.tv" -o "$TMP/td.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/td.ll" -o "$TMP/td.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/td.o" -o "$TMP/td" 2>/dev/null || {
    echo "dynfield-trait-dispatch: FAIL (build)"; TD_OK=0; }
fi
if [ "$TD_OK" = "1" ]; then
    GOT=$("$TMP/td")
    if [ "$GOT" = "$EXPECTED_TD" ]; then
        echo "dynfield-trait-dispatch: PASS (type-directed x.method() + fn<T: Trait> bound dispatch, monomorphized, no vtable)"
    else
        echo "dynfield-trait-dispatch: FAIL"; echo "--- expected ---"; echo "$EXPECTED_TD"
        echo "--- got ---"; echo "$GOT"; TD_OK=0
    fi
fi
if [ "$TD_OK" = "0" ]; then FAILURES="$FAILURES trait-dispatch"; fi

# ---- B2b Slice 3 NEGATIVE: trait-bound violation must fail to compile ----
# NoShape does not impl Shape but is passed to fn<T: Shape>. Compilation MUST
# fail with the bound diagnostic — the enforcement that makes <T: Trait> mean
# something (reads g_gen_bounds).
TBV_OK=1
if [ ! -x "$TMP/tvc_self" ]; then TBV_OK=0; fi
if [ "$TBV_OK" = "1" ]; then
    TBV_OUT=$("$TMP/tvc_self" "$REPO_DIR/examples/trait_bound_violation.tv" -o "$TMP/tbv.ll" 2>&1)
    TBV_RC=$?
    if [ "$TBV_RC" != "0" ] && echo "$TBV_OUT" | grep -q "does not implement the required trait bound"; then
        echo "dynfield-trait-bound-violation: PASS (unsatisfied <T: Shape> bound rejected with clean diagnostic, nonzero exit)"
    else
        echo "dynfield-trait-bound-violation: FAIL (rc=$TBV_RC)"; echo "$TBV_OUT" | tail -3; TBV_OK=0
    fi
fi
if [ "$TBV_OK" = "0" ]; then FAILURES="$FAILURES trait-bound-violation"; fi

# ---- B2b Slice 4 gate: operator overloading (tvc_self ONLY) ----
# a OP b on a user struct implementing the operator's trait desugars to the
# trait method (+ -> Add, - -> Sub, * -> Mul, == -> Eq), statically resolved
# through the receiver type (no vtable). Gated on the impl — scalar/field
# arithmetic is untouched. Retires the last B1a wound: operators become
# type-directed. self -> &receiver, RHS by value.
EXPECTED_OO="11
22
9
18
10
40
1
0"

OO_OK=1
if [ ! -x "$TMP/tvc_self" ]; then OO_OK=0; fi
if [ "$OO_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/operator_overload.tv" -o "$TMP/oo.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/oo.ll" -o "$TMP/oo.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/oo.o" -o "$TMP/oo" 2>/dev/null || {
    echo "dynfield-operator-overload: FAIL (build)"; OO_OK=0; }
fi
if [ "$OO_OK" = "1" ]; then
    GOT=$("$TMP/oo")
    if [ "$GOT" = "$EXPECTED_OO" ]; then
        echo "dynfield-operator-overload: PASS (+/-/*/== on user struct -> trait method, type-directed, scalar arith untouched)"
    else
        echo "dynfield-operator-overload: FAIL"; echo "--- expected ---"; echo "$EXPECTED_OO"
        echo "--- got ---"; echo "$GOT"; OO_OK=0
    fi
fi
if [ "$OO_OK" = "0" ]; then FAILURES="$FAILURES operator-overload"; fi

# ---- B2 CAPSTONE gate: the predict/decide architecture (tvc_self ONLY) ----
# The mantis-shaped demonstrator the whole B2 design was built toward. Three
# layers with a VISIBLE seam: TERRAIN (parameter-free net_edge, fixed basis),
# PREDICTION (runtime-swappable fn(i32,i32)->i32 strategies — the fenced code
# pointer), DECISION (a trait Gate with hard compile-time-resolved rules, no
# vtable). run_bot<G: Gate>(predict: fn..., gate: G) composes all three. Proves
# fn pointers (predictive layer) and static traits (decision layer) coexist
# and compose — the trading-bot principle realized in the language.
EXPECTED_PD="25
1
0
1
0
1
62"

PD_OK=1
if [ ! -x "$TMP/tvc_self" ]; then PD_OK=0; fi
if [ "$PD_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/predict_decide.tv" -o "$TMP/pd.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/pd.ll" -o "$TMP/pd.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/pd.o" -o "$TMP/pd" 2>/dev/null || {
    echo "dynfield-predict-decide: FAIL (build)"; PD_OK=0; }
fi
if [ "$PD_OK" = "1" ]; then
    GOT=$("$TMP/pd")
    if [ "$GOT" = "$EXPECTED_PD" ]; then
        echo "dynfield-predict-decide: PASS (capstone: fn-pointer prediction + trait decision + bound generic bot, visible predict/decide seam)"
    else
        echo "dynfield-predict-decide: FAIL"; echo "--- expected ---"; echo "$EXPECTED_PD"
        echo "--- got ---"; echo "$GOT"; PD_OK=0
    fi
fi
if [ "$PD_OK" = "0" ]; then FAILURES="$FAILURES predict-decide"; fi

# ---- B2-closures Slice 1 gate: stack-only monomorphized closures (tvc_self ONLY) ----
# A closure literal `|params| body` captures its environment BY VALUE into a
# stack struct and lowers to a lifted fn @__closure_N(ptr %__env, params) —
# monomorphized, statically identified, NOT a dyn Fn (no vtable, no heap box).
# Covers: non-capturing, capturing-by-value, generic HOF (apply<C> monomorphized
# per closure identity — the thesis-faithful way to pass a closure), and a block
# body with control flow. The unnameable closure type forces composition through
# the generic machinery, never a uniform `closure` type (which would be erasure).
EXPECTED_CLO="42
105
11
42
9"

CLO_OK=1
if [ ! -x "$TMP/tvc_self" ]; then CLO_OK=0; fi
if [ "$CLO_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/closure_basics.tv" -o "$TMP/clo.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/clo.ll" -o "$TMP/clo.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/clo.o" -o "$TMP/clo" 2>/dev/null || {
    echo "dynfield-closure-basics: FAIL (build)"; CLO_OK=0; }
fi
if [ "$CLO_OK" = "1" ]; then
    GOT=$("$TMP/clo")
    if [ "$GOT" = "$EXPECTED_CLO" ]; then
        echo "dynfield-closure-basics: PASS (stack closures: non-capturing + by-value capture + generic HOF + block body; monomorphized, no dyn Fn)"
    else
        echo "dynfield-closure-basics: FAIL"; echo "--- expected ---"; echo "$EXPECTED_CLO"
        echo "--- got ---"; echo "$GOT"; CLO_OK=0
    fi
fi
if [ "$CLO_OK" = "0" ]; then FAILURES="$FAILURES closure-basics"; fi

# ---- B2-closures Slice 2 THESIS gate: the prove-through fence (tvc_self ONLY) ----
# The instrument that locates the Rubicon. A loop calling a CLOSURE of
# statically-known identity + proven-pure body PARALLELIZES (the compiler proves
# what runs); a structurally identical loop calling a FUNCTION POINTER stays
# SEQUENTIAL (unprovable target). Asserts EXACTLY 1 worker function: closure_map
# parallel (1), fnptr_map fenced (0). A closure parallelizes precisely WHEN its
# identity is provable — the first construct that obscures identity (Slice 3,
# the Rubicon) fences again. Crossing an unprovable callee costs parallelism,
# never soundness. Also checks determinism across TRAVELER_THREADS.
EXPECTED_CPT="16381
16381"

CPT_OK=1
if [ ! -x "$TMP/tvc_self" ]; then CPT_OK=0; fi
if [ "$CPT_OK" = "1" ]; then
"$TMP/tvc_self" "$REPO_DIR/examples/closure_prove_through.tv" -o "$TMP/cpt.ll" >/dev/null 2>&1 \
  && "$LLC" $LLC_TARGET -filetype=obj "$TMP/cpt.ll" -o "$TMP/cpt.o" 2>/dev/null \
  && clang $LINK_PIE "$TMP/cpt.o" -o "$TMP/cpt" 2>/dev/null || {
    echo "dynfield-closure-prove-through: FAIL (build)"; CPT_OK=0; }
fi
if [ "$CPT_OK" = "1" ]; then
    NW=$(grep -c "define internal void @__pfor_worker" "$TMP/cpt.ll" || true)
    GOT=$("$TMP/cpt")
    GOT1=$(TRAVELER_THREADS=1 "$TMP/cpt")
    GOT32=$(TRAVELER_THREADS=32 "$TMP/cpt")
    if [ "$NW" = "1" ] && [ "$GOT" = "$EXPECTED_CPT" ] && [ "$GOT1" = "$EXPECTED_CPT" ] && [ "$GOT32" = "$EXPECTED_CPT" ]; then
        echo "dynfield-closure-prove-through: PASS (pure closure-map parallelizes [1 worker], fn-pointer-map fenced [0]; deterministic — provable identity IS the line)"
    else
        echo "dynfield-closure-prove-through: FAIL (workers=$NW want 1; out=$GOT/$GOT1/$GOT32 want $EXPECTED_CPT)"; CPT_OK=0
    fi
fi
if [ "$CPT_OK" = "0" ]; then FAILURES="$FAILURES closure-prove-through"; fi

# ---- B2-closures Slice 3 THESIS gate: the Rubicon, via negativa (tvc_self ONLY) ----
# The thesis ("dispatch over data, never code") is the NEGATIVE SPACE defined by
# the closures we REJECT — exactly as auto-parallel soundness is defined by the
# races refused. Each construct below would force type erasure (dyn Fn); each
# must produce a CLEAN, SPECIFIC compile error (exit nonzero + diagnostic), NOT
# a crash and NOT a silent miscompile. We never implement dyn Fn — the rejection
# IS the result. (N3 == N4 mechanically: heterogeneous storage is reassignment.)
#   N1 reject_return       — closure cannot escape via return
#   N2 reject_struct_field — closure cannot be stored in an (escaping) struct
#   N4 reject_reassign     — closure var cannot change identity
#   N6 reject_fnptr_coerce — capturing closure is not a fn pointer
CLO_NEG_OK=1
if [ ! -x "$TMP/tvc_self" ]; then CLO_NEG_OK=0; fi
check_reject() {
    # $1 = fixture base, $2 = required error substring
    local base="$1"; local want="$2"
    local out
    out=$("$TMP/tvc_self" "$REPO_DIR/tests/dynfield/closures/${base}.tv" -o "$TMP/${base}.ll" 2>&1)
    local rc=$?
    # Must NOT crash (segfault/abort) and must NOT exit 0 (silent accept).
    if [ "$rc" -eq 139 ] || [ "$rc" -eq 134 ] || [ "$rc" -eq 138 ]; then
        echo "dynfield-closure-reject-${base}: FAIL (crash rc=$rc)"; return 1
    fi
    if [ "$rc" -eq 0 ]; then
        echo "dynfield-closure-reject-${base}: FAIL (silently accepted — Rubicon crossed!)"; return 1
    fi
    if echo "$out" | grep -qF "$want"; then
        echo "dynfield-closure-reject-${base}: PASS (clean reject: $want)"
        return 0
    fi
    echo "dynfield-closure-reject-${base}: FAIL (wrong error)"; echo "  got: $out"; return 1
}
if [ "$CLO_NEG_OK" = "1" ]; then
    check_reject reject_return       "closure cannot escape its defining scope" || CLO_NEG_OK=0
    check_reject reject_struct_field "closure cannot be stored in a struct field" || CLO_NEG_OK=0
    check_reject reject_reassign     "closure variable cannot be reassigned" || CLO_NEG_OK=0
    check_reject reject_fnptr_coerce "closure is not a function pointer" || CLO_NEG_OK=0
fi
if [ "$CLO_NEG_OK" = "0" ]; then FAILURES="$FAILURES closure-reject"; fi

# ---- Summary ----

if [ -z "$FAILURES" ]; then
    echo "dynfield: PASS (all phases)"
    exit 0
else
    echo "dynfield: FAIL ($FAILURES)"
    exit 1
fi
