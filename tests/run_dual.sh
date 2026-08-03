#!/bin/bash
# Traveler dual-compiler parity test
# Runs every test through both bootstrap and tvc_self, diffs output.
# Invoke manually or in CI — slower than run.sh due to Stage 1 build.
#
# Usage: ./run_dual.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"
EXPECTED="$SCRIPT_DIR/expected"

# Resolve a .tv by basename across the demo + library + tool trees.
resolve_tv() {
    local name="$1"
    if [ -f "$EXAMPLES/${name}.tv" ]; then echo "$EXAMPLES/${name}.tv"; return 0; fi
    local f
    f=$(find "$REPO_DIR/src/lib" "$REPO_DIR/src/tools" -name "${name}.tv" 2>/dev/null | head -1)
    if [ -n "$f" ]; then echo "$f"; return 0; fi
    echo "$EXAMPLES/${name}.tv"
    return 0
}

# Timeouts
TIMEOUT_SINGLE=15
TIMEOUT_MULTI=30
TIMEOUT_STAGE1=15
TIMEOUT_STAGE2=30

# Portable timeout
if command -v gtimeout &>/dev/null; then
    TIMEOUT_CMD="gtimeout"
elif command -v timeout &>/dev/null; then
    TIMEOUT_CMD="timeout"
else
    TIMEOUT_CMD=""
fi

run_with_timeout() {
    local secs="$1"; shift
    if [ -n "$TIMEOUT_CMD" ]; then
        "$TIMEOUT_CMD" "$secs" "$@"
    else
        "$@"
    fi
}

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0
FAILURES=""

# Known-hanging through tvc_self — skip in dual mode until diagnosed
SKIP_LIST=""

# --- Platform detection ---
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

# --- Build bootstrap ---
echo "Building bootstrap compiler..."
(cd "$SRC_DIR" && clang -O2 -Wall -Wextra -std=c99 -o tvc tvc.c 2>&1) || {
    echo "FATAL: bootstrap build failed" >&2; exit 1
}
TVC="$SRC_DIR/tvc"

# --- Temp directory ---
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# --- Step 1: Run bootstrap tests ---
echo ""
echo "=== Step 1: Bootstrap regression (run.sh) ==="
echo ""
"$SCRIPT_DIR/run.sh" || {
    echo "FATAL: bootstrap regression failed — fix before running dual tests" >&2
    exit 1
}

# --- Step 2: Build Stage 1 (bootstrap -> tvc_self) ---
echo ""
echo "=== Step 2: Build Stage 1 ==="
echo ""
START_S1=$(python3 -c "import time; print(int(time.time()*1000))")

run_with_timeout "$TIMEOUT_STAGE1" "$TVC" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/tvc_self.ll" 2>/dev/null || {
    echo "FATAL: Stage 1 compile failed/timed out" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvc_self.ll" -o "$TMPDIR/tvc_self.o" 2>/dev/null || {
    echo "FATAL: Stage 1 llc failed" >&2; exit 1
}
clang $LINK_PIE "$TMPDIR/tvc_self.o" -o "$TMPDIR/tvc_self" 2>/dev/null || {
    echo "FATAL: Stage 1 link failed" >&2; exit 1
}
TVC_SELF="$TMPDIR/tvc_self"

END_S1=$(python3 -c "import time; print(int(time.time()*1000))")
echo "Stage 1 built in $((END_S1 - START_S1))ms"

# --- Step 3: Parity tests ---
echo ""
echo "=== Step 3: Dual-compiler parity ==="
echo ""

is_skipped() {
    local name="$1"
    for s in $SKIP_LIST; do
        if [ "$name" = "$s" ]; then return 0; fi
    done
    return 1
}

# Single-file parity test
parity_single() {
    local name="$1"
    TOTAL=$((TOTAL + 1))

    if is_skipped "$name"; then
        printf "  [%2d] %-35s SKIP (known issue)\n" "$TOTAL" "$name"
        SKIP=$((SKIP + 1))
        return
    fi

    if [ ! -f "$EXPECTED/${name}.txt" ]; then
        printf "  [%2d] %-35s SKIP (no baseline)\n" "$TOTAL" "$name"
        SKIP=$((SKIP + 1))
        return
    fi

    # Compile through tvc_self
    run_with_timeout "$TIMEOUT_SINGLE" "$TVC_SELF" "$(resolve_tv "$name")" -o "$TMPDIR/${name}_dual.ll" 2>/dev/null || {
        printf "  [%2d] %-35s FAIL (tvc_self compile/timeout)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}_dual.ll" -o "$TMPDIR/${name}_dual.o" 2>/dev/null || {
        printf "  [%2d] %-35s FAIL (tvc_self llc)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }
    clang $LINK_PIE "$TMPDIR/${name}_dual.o" -o "$TMPDIR/${name}_dual" 2>/dev/null || {
        printf "  [%2d] %-35s FAIL (tvc_self link)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }

    local actual expected
    actual=$(run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/${name}_dual" 2>/dev/null) || {
        printf "  [%2d] %-35s FAIL (tvc_self crash/timeout)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }
    expected=$(cat "$EXPECTED/${name}.txt")

    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (parity mismatch)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
    fi
}

# Multi-file parity test.
#   $1 = test name (also the baseline name in expected/)
#   $2..$N = the .tv modules to compile through tvc_self and link, IN
#            LINK ORDER (the last is conventionally the test driver).
# Each module is compiled through tvc_self → object; all objects are
# linked; the binary is run and its output diffed against the baseline.
# The link step itself verifies mangled-name parity across compilation
# units (e.g. ntt_forward_Field18446744069414584321).
parity_multi() {
    local name="$1"; shift
    local modules="$*"
    TOTAL=$((TOTAL + 1))

    if [ ! -f "$EXPECTED/${name}.txt" ]; then
        printf "  [%2d] %-35s SKIP (no baseline)\n" "$TOTAL" "$name"
        SKIP=$((SKIP + 1))
        return
    fi

    local objs=""
    local m
    for m in $modules; do
        if ! run_with_timeout "$TIMEOUT_MULTI" "$TVC_SELF" "$(resolve_tv "$m")" -o "$TMPDIR/${m}_dual.ll" 2>/dev/null; then
            printf "  [%2d] %-35s FAIL (tvc_self compile %s)\n" "$TOTAL" "$name" "$m"
            FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"; return
        fi
        if ! "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${m}_dual.ll" -o "$TMPDIR/${m}_dual.o" 2>/dev/null; then
            printf "  [%2d] %-35s FAIL (llc %s)\n" "$TOTAL" "$name" "$m"
            FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"; return
        fi
        objs="$objs $TMPDIR/${m}_dual.o"
    done

    if ! clang $LINK_PIE $objs -o "$TMPDIR/${name}_dual" 2>/dev/null; then
        printf "  [%2d] %-35s FAIL (link)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"; return
    fi

    local actual expected
    actual=$(run_with_timeout "$TIMEOUT_MULTI" "$TMPDIR/${name}_dual" 2>/dev/null) || {
        printf "  [%2d] %-35s FAIL (crash/timeout)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"; return
    }
    expected=$(cat "$EXPECTED/${name}.txt")

    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (parity mismatch)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
    fi
}

# Tier 1 parity
parity_single field_basics
parity_single edge_cases
parity_single algebraic_loops
parity_single builtin_shadow
parity_single assign_widen
parity_single binfield_basics
parity_single binfield_newton
parity_single extfield_basics
parity_single generic_test
parity_single generic_multi
parity_single generic_export
parity_single enum_basics
parity_single struct_basics
parity_single address_of
parity_single addr_scalar
parity_single pointer_basics
parity_single int_match
parity_single match_nested_scalar
parity_single bitwise_ops
parity_single compare_let
parity_single short_circuit
parity_single repr_tracking
parity_single register_basics
parity_single method_basics
parity_single two_fields
parity_single barrett_test
# adc_pipeline needs stdin input
TOTAL=$((TOTAL + 1))
run_with_timeout "$TIMEOUT_SINGLE" "$TVC_SELF" "$EXAMPLES/adc_pipeline.tv" -o "$TMPDIR/adc_pipeline_dual.ll" 2>/dev/null && \
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/adc_pipeline_dual.ll" -o "$TMPDIR/adc_pipeline_dual.o" 2>/dev/null && \
clang $LINK_PIE "$TMPDIR/adc_pipeline_dual.o" -o "$TMPDIR/adc_pipeline_dual" 2>/dev/null || {
    printf "  [%2d] %-35s FAIL (tvc_self compile)\n" "$TOTAL" "adc_pipeline"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES adc_pipeline"
}
if [ -x "$TMPDIR/adc_pipeline_dual" ]; then
    actual=$(printf '\x0A\x0E\x14\x1C\x26\x32' | run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/adc_pipeline_dual" 2>/dev/null)
    expected=$(cat "$EXPECTED/adc_pipeline.txt")
    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "adc_pipeline"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (parity mismatch)\n" "$TOTAL" "adc_pipeline"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES adc_pipeline"
    fi
fi

# Section 17 types (previously hung, fixed by purity analysis NULL_NODE fix)
parity_single regime_basics
parity_single segment_test

# Tier 2 parity (single-file only — multi-file through tvc_self is complex)
parity_single aes_sbox
parity_single str_compare
parity_single poly_classify

# Function-field zeta / RH-for-curves (boundary map L1.4, the pure exhibit):
# genus 0 (exact PNT / irreducible counting), genus 1 (a_p^2<=4p), genus 2.
parity_single weil_zeta_line
parity_single weil_zeta_ec
parity_single weil_zeta_g2

# Sato-Tate: population statistics of the observable a_p (vertical: fix p / all
# curves; horizontal: one curve / many primes). In-field exact + leaves-field limit.
parity_single satotate_vertical
parity_single satotate_horizontal
# Sato-Tate via negativa: CM curves (semicircle law fails -> CM measure). Gauss
# (Z[i], p=3 mod 4) and Eisenstein (Z[w], p=2 mod 3): exact congruence certificate
# (mismatch=0), supersingular density ~1/2, nm4/nm6 off Catalan, Hasse survives.
parity_single satotate_cm_gauss
parity_single satotate_cm_eisenstein

# Mobius linear-complexity (@internal-note: plan-mobius-inclusion, W-B P5):
# the extremal-horizon witnesses F(L). The
# lone STANDALONE mobius example (the rest are import-based, tvc_self-only) — a
# pure squarefree-arithmetic fact, so it runs on both compilers. 1_{n==a mod d}
# dies at the first non-squarefree term: F(1)=3, F(2)=8, F(4)=26, F(6)=124,
# F(12)=274. Byte-identical output across the seed and the self-host.
parity_single mobius_extremal

# Dynamic fields (Phases 2-5a: instantiate <dyn> + Register<dyn,d> + nested calls)
parity_single dyn_kernel_test
parity_single dyn_register_test
parity_single dyn_nested_test
parity_single global_const_init
# #55: exact-2^63 literal + INT64_MIN print (tvc_self writer fix; seed was clean)
parity_single i64min_print
# #72: cast-wrapped alloc/realloc element sizing (seed was always correct here;
# tvc_self now agrees — parity pins the agreement)
parity_single alloc_cast_sizes

# Crypto stack (Phase 6: NTT infrastructure parity).  Multi-file link
# chains compiled entirely through tvc_self.  The link step verifies
# mangled-name parity; the run verifies codegen parity.
parity_multi ntt_test           ntt ntt_test
parity_multi ntt_link_test      ntt ntt_link_test
parity_multi poseidon2_test     poseidon2 poseidon2_test
parity_multi merkle_test        poseidon2 merkle merkle_test
parity_multi fri_test           ntt poseidon2 merkle fri fri_test
parity_multi fri_highdeg_test   ntt poseidon2 merkle fri fri_highdeg_test
parity_multi plonk_test         ntt poseidon2 merkle fri plonk plonk_test

# --- Step 4: Stage 2 self-compilation ---
echo ""
echo "=== Step 4: Stage 2 self-compilation ==="
echo ""
START_S2=$(python3 -c "import time; print(int(time.time()*1000))")
TOTAL=$((TOTAL + 1))

run_with_timeout "$TIMEOUT_STAGE2" "$TVC_SELF" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/tvc_self2.ll" 2>/dev/null && \
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvc_self2.ll" -o "$TMPDIR/tvc_self2.o" 2>/dev/null && \
clang $LINK_PIE "$TMPDIR/tvc_self2.o" -o "$TMPDIR/tvc_self2" 2>/dev/null || {
    printf "  [%2d] %-35s FAIL (Stage 2 build)\n" "$TOTAL" "stage2_build"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES stage2_build"
    END_S2=$(python3 -c "import time; print(int(time.time()*1000))")
    echo "Stage 2 attempted in $((END_S2 - START_S2))ms"
    # Skip smoke test
    echo ""
    echo "============================================"
    printf "  PARITY: %d PASS, %d FAIL, %d SKIP\n" "$PASS" "$FAIL" "$SKIP"
    if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
    echo "============================================"
    exit "$FAIL"
}

END_S2=$(python3 -c "import time; print(int(time.time()*1000))")
printf "  [%2d] %-35s PASS (%dms)\n" "$TOTAL" "stage2_build" "$((END_S2 - START_S2))"
PASS=$((PASS + 1))

# Stage 2 smoke test: field_basics
TOTAL=$((TOTAL + 1))
run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/tvc_self2" "$EXAMPLES/field_basics.tv" -o "$TMPDIR/fb_s2.ll" 2>/dev/null && \
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/fb_s2.ll" -o "$TMPDIR/fb_s2.o" 2>/dev/null && \
clang $LINK_PIE "$TMPDIR/fb_s2.o" -o "$TMPDIR/fb_s2" 2>/dev/null || {
    printf "  [%2d] %-35s FAIL (Stage 2 compile)\n" "$TOTAL" "stage2_smoke"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES stage2_smoke"
    echo ""
    echo "============================================"
    printf "  PARITY: %d PASS, %d FAIL, %d SKIP\n" "$PASS" "$FAIL" "$SKIP"
    if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
    echo "============================================"
    exit "$FAIL"
}

actual=$(run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/fb_s2" 2>/dev/null)
expected=$(cat "$EXPECTED/field_basics.txt")

if [ "$actual" = "$expected" ]; then
    printf "  [%2d] %-35s PASS\n" "$TOTAL" "stage2_smoke"
    PASS=$((PASS + 1))
else
    printf "  [%2d] %-35s FAIL (Stage 2 output mismatch)\n" "$TOTAL" "stage2_smoke"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES stage2_smoke"
fi

# --- Summary ---
echo ""
echo "============================================"
printf "  PARITY: %d PASS, %d FAIL, %d SKIP\n" "$PASS" "$FAIL" "$SKIP"
if [ -n "$FAILURES" ]; then
    echo "  FAILED:$FAILURES"
fi
echo "============================================"
echo ""

exit "$FAIL"
