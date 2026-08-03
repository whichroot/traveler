#!/bin/bash
# Traveler regression test suite
# Usage: ./run.sh [--tier1] [--negative-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"
EXPECTED="$SCRIPT_DIR/expected"
NEGATIVE="$SCRIPT_DIR/negative"
EXPECTED_ERRORS="$SCRIPT_DIR/expected_errors"

# Timeouts (seconds)
TIMEOUT_SINGLE=5
TIMEOUT_MULTI=10

# Portable timeout: use gtimeout (macOS brew), timeout (Linux), or no timeout
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

# Parse flags
TIER1_ONLY=0
NEGATIVE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --tier1) TIER1_ONLY=1 ;;
        --negative-only) NEGATIVE_ONLY=1 ;;
    esac
done

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

find_opt() {
    OPT="${LLC%llc}opt"
    if ! command -v "$OPT" &>/dev/null; then
        # Try common alternatives
        for p in \
            /opt/homebrew/opt/llvm@21/bin/opt \
            /usr/local/opt/llvm@21/bin/opt \
            /usr/lib/llvm-21/bin/opt \
            opt-21 \
            opt; do
            if command -v "$p" &>/dev/null; then OPT="$p"; return; fi
        done
        echo "WARNING: opt not found, skipping IR validation" >&2
        OPT=""
    fi
}

find_llc
find_opt

# --- host target: retarget IR objects + non-PIE link off-macOS ---
# Traveler-emitted IR text carries the canonical triple on every host (the
# byte-identity gates depend on that); execution overrides it at llc
# (-mtriple) and links with -no-pie where Linux defaults to PIE.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac

# --- Build bootstrap compiler ---
echo "Building bootstrap compiler..."
(cd "$SRC_DIR" && make tvc 2>&1) || {
    # Force rebuild if make thinks it's up to date but binary missing
    (cd "$SRC_DIR" && clang -O2 -Wall -Wextra -std=c99 -o tvc tvc.c 2>&1)
}
TVC="$SRC_DIR/tvc"
if [ ! -x "$TVC" ]; then
    echo "FATAL: bootstrap compiler not found at $TVC" >&2; exit 1
fi

# --- Build the self-hosting compiler (for import-based modules the C seed
#     cannot parse, e.g. the split piecewise codec). C-free canonical build. ---
TVC_SELF="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$TVC_SELF" ]; then
    echo "Building self-hosting compiler (C-free)..."
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || {
        echo "FATAL: could not build self-hosting compiler" >&2; exit 1
    }
fi

# --- Temp directory ---
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# --- Test helpers ---
# Resolve a .tv by basename across the demo + library + tool trees. Demos live
# in examples/, reusable kernels in src/lib/<subsys>/, tools in src/tools/.
resolve_tv() {
    local name="$1"
    if [ -f "$EXAMPLES/${name}.tv" ]; then echo "$EXAMPLES/${name}.tv"; return 0; fi
    local f
    f=$(find "$REPO_DIR/src/lib" "$REPO_DIR/src/tools" -name "${name}.tv" 2>/dev/null | head -1)
    if [ -n "$f" ]; then echo "$f"; return 0; fi
    echo "$EXAMPLES/${name}.tv"   # fall back (will fail visibly)
    return 0
}

compile_single() {
    local name="$1"
    local timeout="${2:-$TIMEOUT_SINGLE}"
    "$TVC" "$(resolve_tv "$name")" -o "$TMPDIR/${name}.ll" 2>/dev/null || return 1
    # IR validation
    if [ -n "$OPT" ]; then
        "$OPT" -passes=verify -S "$TMPDIR/${name}.ll" -o /dev/null 2>/dev/null || {
            echo "  IR INVALID: ${name}" >&2; return 1
        }
    fi
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null || return 1
    clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || return 1
    return 0
}

compile_obj() {
    local name="$1"
    "$TVC" "$(resolve_tv "$name")" -o "$TMPDIR/${name}.ll" 2>/dev/null || return 1
    if [ -n "$OPT" ]; then
        "$OPT" -passes=verify -S "$TMPDIR/${name}.ll" -o /dev/null 2>/dev/null || {
            echo "  IR INVALID: ${name}" >&2; return 1
        }
    fi
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null || return 1
    return 0
}

# Like compile_obj, but uses the self-hosting compiler. Required for modules
# that use `import` (Model A), which the frozen C seed cannot parse — e.g. the
# split piecewise codec. The resolved root .tv pulls in its imports relative to
# its own directory.
compile_obj_self() {
    local name="$1"
    "$TVC_SELF" "$(resolve_tv "$name")" -o "$TMPDIR/${name}.ll" 2>/dev/null || return 1
    if [ -n "$OPT" ]; then
        "$OPT" -passes=verify -S "$TMPDIR/${name}.ll" -o /dev/null 2>/dev/null || {
            echo "  IR INVALID: ${name}" >&2; return 1
        }
    fi
    "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null || return 1
    return 0
}

link_objs() {
    local outname="$1"; shift
    local objs=""
    for o in "$@"; do objs="$objs $TMPDIR/${o}.o"; done
    clang $LINK_PIE $objs -o "$TMPDIR/${outname}" 2>/dev/null || return 1
    return 0
}

run_test() {
    local name="$1"
    local timeout="${2:-$TIMEOUT_SINGLE}"
    TOTAL=$((TOTAL + 1))

    if [ ! -f "$EXPECTED/${name}.txt" ]; then
        printf "  [%2d] %-35s SKIP (no baseline)\n" "$TOTAL" "$name"
        SKIP=$((SKIP + 1))
        return
    fi

    local actual
    actual=$(run_with_timeout "$timeout" "$TMPDIR/${name}" 2>/dev/null) || {
        printf "  [%2d] %-35s FAIL (crash/timeout)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }

    local expected
    expected=$(cat "$EXPECTED/${name}.txt")

    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (output mismatch)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
    fi
}

# Single-file test: compile + run + compare
test_single() {
    local name="$1"
    TOTAL=$((TOTAL + 1))

    if ! compile_single "$name"; then
        printf "  [%2d] %-35s FAIL (compile)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    fi

    if [ ! -f "$EXPECTED/${name}.txt" ]; then
        printf "  [%2d] %-35s SKIP (no baseline)\n" "$TOTAL" "$name"
        SKIP=$((SKIP + 1))
        TOTAL=$((TOTAL - 1))  # don't count skips
        return
    fi

    local actual expected
    actual=$(run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/${name}" 2>/dev/null) || {
        printf "  [%2d] %-35s FAIL (crash/timeout)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
        return
    }
    expected=$(cat "$EXPECTED/${name}.txt")

    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (output mismatch)\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES $name"
    fi
}

# --- Negative test helper ---
test_negative() {
    local name="$1"
    local tv_file="$NEGATIVE/${name}.tv"
    local err_file="$EXPECTED_ERRORS/${name}.txt"
    TOTAL=$((TOTAL + 1))

    if [ ! -f "$tv_file" ]; then
        printf "  [%2d] %-35s SKIP (no test file)\n" "$TOTAL" "neg:$name"
        SKIP=$((SKIP + 1))
        TOTAL=$((TOTAL - 1))
        return
    fi

    local stderr_out
    stderr_out=$("$TVC" "$tv_file" -o "$TMPDIR/${name}_neg.ll" 2>&1) || true

    # For negative tests that should compile but produce correct output (pub_struct, eval_register)
    # check if expected output file exists instead of expected error
    if [ -f "$EXPECTED/${name}.txt" ]; then
        # This is a "should work correctly" test, not an error test
        if [ -f "$TMPDIR/${name}_neg.ll" ]; then
            "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}_neg.ll" -o "$TMPDIR/${name}_neg.o" 2>/dev/null && \
            clang $LINK_PIE "$TMPDIR/${name}_neg.o" -o "$TMPDIR/${name}_neg" 2>/dev/null || {
                printf "  [%2d] %-35s FAIL (link)\n" "$TOTAL" "neg:$name"
                FAIL=$((FAIL + 1))
                FAILURES="$FAILURES neg:$name"
                return
            }
            local actual expected
            actual=$(run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/${name}_neg" 2>/dev/null) || {
                printf "  [%2d] %-35s FAIL (crash)\n" "$TOTAL" "neg:$name"
                FAIL=$((FAIL + 1))
                FAILURES="$FAILURES neg:$name"
                return
            }
            expected=$(cat "$EXPECTED/${name}.txt")
            if [ "$actual" = "$expected" ]; then
                printf "  [%2d] %-35s PASS\n" "$TOTAL" "neg:$name"
                PASS=$((PASS + 1))
            else
                printf "  [%2d] %-35s FAIL (output mismatch)\n" "$TOTAL" "neg:$name"
                FAIL=$((FAIL + 1))
                FAILURES="$FAILURES neg:$name"
            fi
            return
        fi
    fi

    # Error test: check for expected error substring
    if [ -f "$err_file" ]; then
        local pattern
        pattern=$(cat "$err_file")
        if echo "$stderr_out" | grep -qF "$pattern"; then
            printf "  [%2d] %-35s PASS\n" "$TOTAL" "neg:$name"
            PASS=$((PASS + 1))
        else
            printf "  [%2d] %-35s FAIL (wrong error)\n" "$TOTAL" "neg:$name"
            echo "        expected: $pattern" >&2
            echo "        got: $stderr_out" >&2
            FAIL=$((FAIL + 1))
            FAILURES="$FAILURES neg:$name"
        fi
    else
        # No expected error file — just verify it doesn't segfault (exit != 139)
        local rc=0
        "$TVC" "$tv_file" -o "$TMPDIR/${name}_neg.ll" 2>/dev/null || rc=$?
        if [ $rc -eq 139 ] || [ $rc -eq 134 ] || [ $rc -eq 138 ]; then
            printf "  [%2d] %-35s FAIL (crash: exit %d)\n" "$TOTAL" "neg:$name" "$rc"
            FAIL=$((FAIL + 1))
            FAILURES="$FAILURES neg:$name"
        else
            printf "  [%2d] %-35s PASS\n" "$TOTAL" "neg:$name"
            PASS=$((PASS + 1))
        fi
    fi
}

# --- IR zero-float check ---
test_zero_float() {
    local name="$1"
    TOTAL=$((TOTAL + 1))
    if grep -qE '\bfloat\b|\bdouble\b|\bfadd\b|\bfmul\b|\bfdiv\b|\bfsub\b' "$TMPDIR/${name}.ll" 2>/dev/null; then
        printf "  [%2d] %-35s FAIL (float in IR)\n" "$TOTAL" "${name}_zero_float"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES ${name}_zero_float"
    else
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "${name}_zero_float"
        PASS=$((PASS + 1))
    fi
}

# time/'s hot-path contract: the clock-read/sleep path performs NO heap
# allocation (stack timespec only). Consumers poll clocks at loop cadence —
# the first downstream consumer (the Wanderer engine) reads the clock from
# its search loop — so a malloc per read is a latent tax. Asserts no
# @malloc/@free inside the named fn bodies of an already-compiled .ll.
test_no_alloc_fns() {
    local name="$1"; shift
    TOTAL=$((TOTAL + 1))
    local bad=""
    for fn in "$@"; do
        if awk "/^define.*@${fn}\(/,/^}/" "$TMPDIR/${name}.ll" 2>/dev/null \
           | grep -qE 'call.*@(malloc|free)\('; then
            bad="$bad $fn"
        fi
    done
    if [ -n "$bad" ]; then
        printf "  [%2d] %-35s FAIL (heap alloc in:%s)\n" "$TOTAL" "${name}_no_alloc" "$bad"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES ${name}_no_alloc"
    else
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "${name}_no_alloc"
        PASS=$((PASS + 1))
    fi
}

# ============================================================
#  TEST EXECUTION
# ============================================================

START_TIME=$(python3 -c "import time; print(int(time.time()*1000))")

if [ "$NEGATIVE_ONLY" -eq 1 ]; then
    echo ""
    echo "=== Negative Tests ==="
    for nf in "$NEGATIVE"/*.tv; do
        [ -f "$nf" ] || continue
        test_negative "$(basename "$nf" .tv)"
    done
else

echo ""
echo "=== Tier 1: Critical Path ==="
echo ""

# Field arithmetic
test_single field_basics
test_single edge_cases
# #60 gate: signed integer -> Field<p>. A negative source used to emit a plain
# `urem`, reading the two's-complement pattern as unsigned ((-5) as Field<251>
# gave 64, not 246). Silent miscompile; the `signed(x) as F` round trip was
# broken at every field width and in every syntactic context. Covers the baked
# path (emit_implicit_convert) plus unsigned/non-negative controls.
#
# tvc_self-ONLY (compile_obj_self, not test_single): the fix landed in
# tvc_self.tv, and the frozen C seed additionally emits invalid IR for this file
# ("zext i64 %tN to i64" — a no-op cast LLVM rejects; @internal-note: known-issues #61), so
# it is not dual-parity eligible. Deliberately NOT added to run_dual.sh.
compile_obj_self field_signed_cast
link_objs field_signed_cast field_signed_cast
run_test field_signed_cast "$TIMEOUT_SINGLE"
# #62 gate: read_bytes' BUFFER argument may be any pointer expression. The
# builtin used to resolve it with sym_find(node.name0) — meaningless on a
# non-identifier node, so &buf[i] / a call result silently read into the WRONG
# buffer (or indexed g_syms[-1]). tvc_self-only: the fix is in tvc_self.tv.
compile_obj_self read_bytes_expr
link_objs read_bytes_expr read_bytes_expr
run_test read_bytes_expr "$TIMEOUT_SINGLE"
# #72 gate: `alloc(n) as *T` sizes elements, not bytes. The cast used to
# interrupt context propagation (elem=1 floor -> silent 4x/8x heap
# under-allocation); alloc/realloc now receive a pointer cast target as their
# context. Dual-parity eligible: the seed always passed context through casts.
test_single alloc_cast_sizes
# #63(c) gate: a cast-typed argument drives generic inference (arg_value_type
# reads through AST_CAST). tvc_self-only: the seed does not parse unbounded
# `<F>` generics.
compile_obj_self generic_infer_cast
link_objs generic_infer_cast generic_infer_cast
run_test generic_infer_cast "$TIMEOUT_SINGLE"
# #55 gate: the exact-2^63 literal (INT64_MIN bit pattern) + INT64_MIN print.
# Pre-fix tvc_self SEGFAULTED on the literal (wr_int/fmt_i64 negate-overflow
# recursion); the seed never had the bug -> dual-parity eligible.
test_single i64min_print
test_single algebraic_loops
# GPU Stage-0 exhibit — CPU side (device codegen is gated in tests/gpu/run.sh):
# a proven-parallel elementwise field map; dual-path + determinism baseline.
test_single gpu_field_map

# Codegen-correctness regressions (call resolution + integer coercion)
test_single builtin_shadow   # user fn shadows name-based builtin (advance/eval/signed)
test_single assign_widen     # implicit widening on plain assignment (u8->i32, i16->i32)

# Binary fields
test_single binfield_basics
test_single binfield_newton

# Extension fields
test_single extfield_basics
test_single global_const_init
# Bug #27 gate: runtime ExtField construction, ==/!=, bool-literal-as-value.
# tvc_self-only — these codegen paths are realized in tvc_self.tv, not the
# frozen C seed (extfield_basics stays the dual-parity ExtField example).
compile_obj_self extfield_runtime
link_objs extfield_runtime extfield_runtime
run_test extfield_runtime "$TIMEOUT_SINGLE"

# Wanderer customer bug gates: Barrett constant overflow, operand-order literal
# coercion, cast-does-not-reduce (spec §5.9), pow i32-exponent width, and bare
# `return;` in main. tvc_self-only — the frozen C seed still exhibits several of
# these, so this does NOT route through dual parity. See
# @internal-note: plan-wanderer-customer-bugs.
compile_obj_self wanderer_field_gates
link_objs wanderer_field_gates wanderer_field_gates
run_test wanderer_field_gates "$TIMEOUT_SINGLE"

# #34 gate: a STORED bool as an if/while condition + a `-> bool` predicate
# returning a comparison (the return-path sibling). Both were llc-refused invalid
# IR (`br i1 %i8` / `ret i1` into an i8 fn). tvc_self-only — the frozen C seed
# shares the bool i8/i1 model and both bugs, so this does NOT route through dual
# parity. See @internal-note: known-issues #34.
compile_obj_self bool_branch
link_objs bool_branch bool_branch
run_test bool_branch "$TIMEOUT_SINGLE"

# #51 gate: aggregate-var REASSIGNMENT (struct from call / from var, enum from
# construct). AST_ASSIGN stored the aggregate's POINTER as its value
# (`store %S %ptr` — invalid IR); the fix loads through the pointer first.
# tvc_self-only — the frozen C seed shares the struct-var-call-reassign rough
# edge, so this does NOT route through dual parity. See @internal-note: known-issues #51.
compile_obj_self struct_reassign
link_objs struct_reassign struct_reassign
run_test struct_reassign "$TIMEOUT_SINGLE"

# #54 gate: for-loop bound WIDTH ADOPTION — i64/usize/u32/narrow/wide-literal
# bounds (serial + pfor-dispatched via the i64 ABI + the prepeek-miss call
# bound). tvc_self-only — the frozen C seed refuses wide bounds (i32-only
# iteration space; the guard is its mirror). See @internal-note: known-issues #54 and
# @internal-note: plan-for-bound-width.
compile_obj_self for_i64_bounds
link_objs for_i64_bounds for_i64_bounds
run_test for_i64_bounds "$TIMEOUT_SINGLE"

# #56 positive gate: text-wide (>u64) literals through every LEGAL spelling —
# u128/i256 let-annotations + the completed `as`-cast context rule. The <=64
# contexts refuse (tests/diag/lit_wide_*; the seed refuses at lex). tvc_self-
# only (wide integer surface types). See @internal-note: known-issues #56.
compile_obj_self wide_literal_casts
link_objs wide_literal_casts wide_literal_casts
run_test wide_literal_casts "$TIMEOUT_SINGLE"

# The "a chess engine found a compiler bug" exhibits (Wanderer). Both tvc_self-
# only (synkey imports nt/linrec.tv -> bm<F>/fpow<F> monomorphized onto
# BinField<13>; collide uses BinField<7>). Deterministic (fixed-seed xorshift):
# synkey decodes castling/promotion out of a hash difference; collide shows the
# BCH bound is tight. See @internal-note: research-journal.
compile_obj_self synkey
link_objs synkey synkey
run_test synkey 30

# The gpt-2 inference kit ported to Traveler stdlib (src/lib/nn/{fixed,linear}.tv):
# fixed_exp/gelu_one/softmax_fp/linear_fp. Golden values pinned from gpt-2-research's
# field/fixed_point.py oracle -> a cross-implementation parity gate. tvc_self-only
# (import Model A). See @internal-note: plan-gpt2-kit-wanderer-actions.
compile_obj_self nn_kit_gate
link_objs nn_kit_gate nn_kit_gate
run_test nn_kit_gate "$TIMEOUT_SINGLE"
compile_obj_self collide
link_objs collide collide
run_test collide "$TIMEOUT_SINGLE"

# Surface ergonomics (@internal-note: plan-syntax-modernization, Phase 1) — additive aliases that
# lower to existing AST nodes, so both compilers must agree (dual-parity) and the
# IR must match the old spelling (proven by the codegen-diff twin).
test_single syntax_var        # `var` == `let mut`
test_single syntax_type       # `type N = <FieldTy>;` == field/binfield/extfield
test_single syntax_print      # `print` de-reserved: usable as an identifier
test_single syntax_shadow     # strict-`let` shadowing (same/nested scope, let->var)
test_single syntax_compound   # `x op= e` == `x = x op e` (all 10 ops, 3 lvalue forms)

# Generics
test_single generic_test
test_single generic_multi
test_single generic_export

# Structs & enums
test_single enum_basics
test_single struct_basics

# Pointers
test_single address_of
test_single addr_scalar      # &scalar/&global cells + the print-is-stdio interleave law (E2a)
test_single pointer_basics

# Control flow
test_single int_match
test_single match_nested_scalar
test_single bitwise_ops
test_single compare_let
test_single short_circuit

# Repr tracking
test_single repr_tracking

# Section 17 types
test_single register_basics
test_single method_basics

# Multi-field & 64-bit
test_single two_fields
test_single barrett_test

# Zero-float pipeline (requires stdin input)
TOTAL=$((TOTAL + 1))
if compile_single adc_pipeline; then
    actual=$(printf '\x0A\x0E\x14\x1C\x26\x32' | run_with_timeout "$TIMEOUT_SINGLE" "$TMPDIR/adc_pipeline" 2>/dev/null) || actual=""
    expected=$(cat "$EXPECTED/adc_pipeline.txt")
    if [ "$actual" = "$expected" ]; then
        printf "  [%2d] %-35s PASS\n" "$TOTAL" "adc_pipeline"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-35s FAIL (output mismatch)\n" "$TOTAL" "adc_pipeline"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES adc_pipeline"
    fi
else
    printf "  [%2d] %-35s FAIL (compile)\n" "$TOTAL" "adc_pipeline"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES adc_pipeline"
fi
test_zero_float adc_pipeline

if [ "$TIER1_ONLY" -eq 0 ]; then

echo ""
echo "=== Tier 2: Full Coverage ==="
echo ""

# Additional single-file
test_single aes_sbox
test_single str_compare
test_single poly_classify

# Function-field zeta / RH-for-curves (boundary map L1.4, the pure exhibit):
# the exact function-field PNT (genus 0: count irreducibles 3 ways), RH as the
# exact integer check a_p^2 <= 4p (genus 1), the Newton-identity region (genus 2).
# @internal-oracle: weil_zeta_*.
test_single weil_zeta_line
test_single weil_zeta_ec
test_single weil_zeta_g2
# The INVERSE (observe): recover the zeta FROM point-count behaviour via Berlekamp-
# Massey (linear-recurrence recovery), Hankel-rank as the convergence check; genus =
# order/2. Part C builds the Frobenius companion operator (Hilbert-Polya, a theorem
# in the function field): Tr(M^n) = p^n+1-#E(F_{p^n}) counts the points, det M = p,
# RH = spectrum on |z|=sqrt p. The lifting-prime coordinate (recovery needs L>range,
# L!=p; over F_p the constant term p vanishes) is the RNS move in number theory.
# @internal-oracle: weil_recover_ec.
# Import-based: imports src/lib/nt/{curve,linrec}.tv
# (Model A) so it is tvc_self-built, not seed-parity. Golden byte-identical = the
# refactor certificate. (Not in run_dual.sh — import is tvc_self-only.)
compile_obj_self weil_recover_ec
link_objs weil_recover_ec weil_recover_ec
run_test weil_recover_ec "$TIMEOUT_SINGLE"

# Sato-Tate: population statistics of the observable a_p (defining the observable
# for the proposed `observe` morphism). Vertical (fix p, all curves; exact integer
# moments + a_p histogram, odd moments exactly 0) and horizontal (one curve, many
# primes; normalized moments -> Catalan). In-field exact spine + declared-scale
# leaves-field limit. Oracles: @internal-oracle: satotate_*.
test_single satotate_vertical
test_single satotate_horizontal
# Sato-Tate VIA NEGATIVA: CM curves where the semicircle law FAILS (equidistribute
# to the CM measure). y^2=x^3+x (Z[i], a_p=0 <=> p=3 mod 4) and y^2=x^3+1 (Z[w],
# a_p=0 <=> p=2 mod 3): supersingular density ~1/2 (vs ~0 non-CM), an exact
# congruence certificate (mismatch=0), and nm4/nm6 diverging from Catalan while
# the Hasse bound survives. Oracles: @internal-oracle: satotate_cm_*.
test_single satotate_cm_gauss
test_single satotate_cm_eisenstein
# Frobenius operator as a random matrix: Sato-Tate SHARPENED per irrep. The
# symmetric-power traces Sym^k = p^{k/2} U_k(cos theta) (exact integers, same
# recurrence as the point counts) verify SU(2) Haar equidistribution E[chi_k]->0
# and orthonormality E[chi_k^2]->1 irrep-by-irrep (Katz-Sarnak/Montgomery-Odlyzko),
# with the exact in-field tensor certificate std(x)std = Sym^2(+)triv <=> Sym1^2 ==
# Sym2 + p (mismatch=0). Oracle: @internal-oracle: frobenius_rmt.
# Import-based since the nt promotion: imports src/lib/nt/curve.tv (Model A).
compile_obj_self frobenius_rmt
link_objs frobenius_rmt frobenius_rmt
run_test frobenius_rmt "$TIMEOUT_SINGLE"
# THE FINALE — where the coordinate does NOT flip (boundary-map invariant #2). The same
# recovery machinery (Berlekamp-Massey) reports a FINITE order on the function-field zeta
# (elliptic trace seq, order 2g=2, locks) and an UNBOUNDED one on the number-field zeta
# (mu(n) = the 1/zeta Dirichlet coefficients, BM order climbs ~m/2, never locks: mu is no
# LRS, so 1/zeta has no finite spectral realization). Part C: Mobius inversion sum_{d|n}
# mu(d)=[n==1] is exact over Z (the skeleton survives; the finite spectrum does not). The
# via-negativa edge of the arc. Oracle: @internal-oracle: zeta_classical_edge.
# Import-based since the nt promotion: imports src/lib/nt/linrec.tv (Model A).
compile_obj_self zeta_classical_edge
link_objs zeta_classical_edge zeta_classical_edge
run_test zeta_classical_edge "$TIMEOUT_SINGLE"
# W7 — BM-over-RNS: genus-3 zeta recovery where the lifting PRIME becomes an RNS
# BASIS (char-poly coefficients reach p^3 ~ 2^93 at p = M31 — beyond any word).
# Part A: one word prime keeps order + residues, loses the integer (c6 unreadable).
# Part B: bm_rns — ONE BM control flow, per-channel arithmetic, JOINT zero-test,
# Garner mixed-radix digit reconstruction (the big integer never materialized);
# certified by order 6 = 2g, per-channel Hankel rank, re-annihilation == 0, digits
# vs bigint oracle. Part C: the seam — q_c | d makes a channel see a FALSE zero
# (Boundary Invariance's one-sided error): independent runs silently diverge
# (orders 1,2,2; naive CRT reads c2 = 161, truth 18), the joint run REFUSES at the
# exact step (channel 0, step 1), a clean spare basis recovers [1,-11,18].
# @internal-oracle: weil_recover_rns.
# Import-based (src/lib/nt/linrec.tv, Model A) so tvc_self-built, not seed-parity.
compile_obj_self weil_recover_rns
link_objs weil_recover_rns weil_recover_rns
run_test weil_recover_rns "$TIMEOUT_SINGLE"
# Carrier-generic Berlekamp-Massey smoke gate: the bm<F> in src/lib/nt/linrec.tv,
# ONE generic source, monomorphizes + recovers over BOTH Field<65537> (Fibonacci
# order 2, eigen-{1,2,3} order 3) AND BinField<8,0x11B> = GF(2^8) (order-2 LFSR +
# an in-field eq0 re-annihilation certificate). The generic form's FIRST consumer
# (second-consumer discipline); grounds bm<GF> for the queued RS error-decoder.
# Division-free (Fermat inverse via `*`, |F|-2 passed in). Oracle: nt_bm_carriers_oracle.py.
compile_obj_self nt_bm_carriers
link_objs nt_bm_carriers nt_bm_carriers
run_test nt_bm_carriers "$TIMEOUT_SINGLE"

# Squares, both citizens (src/lib/nt/sqrt.tv): exact isqrt/is-square with the
# bracketing certificate (magnitude side) + Tonelli-Shanks sqrt_mod (field side)
# — detection (Euler) finally paired with EXTRACTION; check = one re-mul (the
# minimal compute+certificate pair). Exhibit star 1048576 = 1024^2 = 32^4 =
# 2^20: a ring hom preserves squares, so it is a square in EVERY F_p (five
# primes, all certified) while squareness of 2 (p == +-1 mod 8) and -1
# (p == 1 mod 4) is p-dependent (refusals exercised; p=65521 S=4 and
# sqrt(-1 mod 13) t=-1 drive the deep T-S loop). Cross-gate: #E(F_101) built
# point-by-point via sqrt_mod lifts == count_fp's character sum (105 == 105),
# Hasse checked both ways (a^2 <= 4p AND |a| <= isqrt64(4p)). Import-based
# (nt/{sqrt,curve}), tvc_self-built. Oracle: perfect_square_oracle.py.
compile_obj_self perfect_square
link_objs perfect_square perfect_square
run_test perfect_square "$TIMEOUT_SINGLE"

# Mobius linear-complexity paper (@internal-paper: mobius_linear_complexity),
# Remark 3.4 INCLUSION — the elided cutoff arithmetic, executed. BOTH tiers of
# "c = 10^-15 admissible for every N>=3". FINITE tier [3, 2^62): there f(N) =
# N/(ln N * ln ln N)^2 <= 10^15, which with the trivial L>=1 (mu(1)=1) IS the theorem
# in that range. Dyadic windows (no calculus) + directed atanh ln-bounds
# (src/lib/util/lnbounds.tv, rigorous LOWER bounds) + wide exact compare
# (src/lib/util/wideint.tv, 31-bit limbs); verdict is the exact w_cmp, per-window
# headroom (~45 -> 2 bits, tightest at k=61) corroborates the paper's ~5.7x slack at
# N=2^62. ASYMPTOTIC tier N>=2^62: the seven Remark-3.4 endpoints (E1-E6) at N=2^62,
# each a directed inequality made HARDER than the truth; E6b native i128, E5 native
# i256 (product ~2^182 — the wide surface types, no hand-limbing). E2 is the binding
# check (~0.05%). No floats. Import-based (Model A, util/*), so tvc_self-built, not
# seed-parity. Plan: @internal-note: plan-mobius-inclusion; @internal-design:
# mobius-inclusion.
compile_obj_self mobius_cutoff
link_objs mobius_cutoff mobius_cutoff
run_test mobius_cutoff "$TIMEOUT_SINGLE"
# Two primitive selftests, gated (@internal-note: plan-parallel-windows, W-A
# section-0 loose end). lnbounds_selftest: the directed ln bounds bracket true ln
# (lower <= true <= upper, gap ~1 micro) for ln2/3/10/43/62. wideint_selftest: the
# 31-bit-limb mul/cmp three ways (exact 2^122 identity in the high limbs, native-i64
# reconstruction where products fit, mod-9 casting-out for a 2.5e19 product that
# overflows i64). Both import-based (Model A), tvc_self-built.
compile_obj_self lnbounds_selftest
link_objs lnbounds_selftest lnbounds_selftest
run_test lnbounds_selftest "$TIMEOUT_SINGLE"
compile_obj_self wideint_selftest
link_objs wideint_selftest wideint_selftest
run_test wideint_selftest "$TIMEOUT_SINGLE"
# Mobius linear-complexity PROFILE (paper section 6 / @internal-note: plan-mobius-inclusion B1):
# Berlekamp-Massey over F_L exposes the per-prefix complexity L(n); we report the
# extremal centered deviation 2*L(n)-n. Oracle is a THEOREM — Grunberger-Winterhof:
# the {0,1}-encoding of Liouville over F_2 has floor(N/2)<=L(N)<=floor(N/2)+1, so
# 2L-n in {-1,0,1,2} for every n (reproduced: dmin=-1,dmax=2,in-band=1). Then mu mod
# {2,3,5,7} and lambda mod 3 as observed deviations (lambda mod 3 = [-8,9] doubled =
# [-4,4.5], the paper's stated band, exactly). Adds bm_profile_l + liouville to
# linrec.tv (shared lib — existing bm_l/hankel/mobius consumers byte-untouched).
# Default N=10^4 (O(N^2) BM, ~2s), so TIMEOUT_MULTI. Import-based (Model A).
compile_obj_self mobius_profile
link_objs mobius_profile mobius_profile
run_test mobius_profile "$TIMEOUT_MULTI"

# Mobius-paper infrastructure (@internal-note: plan-mobius-inclusion, W-B P3):
# carrier-generic dense linear algebra (src/lib/nt/linalg.tv)
# + univariate poly algebra (src/lib/nt/polyfield.tv) — consumers #3/#4 of the nt
# carrier contract, grounded over Field<p> AND BinField<8,0x11B> (each new
# consumer must run on both carriers). nt_linalg: rank/nullity/A·v==0 (the admissible-solution kernel op).
# nt_polyfield: mul/divmod/gcd/deriv/eval/x^e-mod-g + charpoly(companion(g))==g
# (eval-interp charpoly, char-agnostic). Both tvc_self-only (import, Model A).
# @internal-note: plan-mobius-inclusion B2 (P3). Design: @internal-design: nt.
compile_obj_self nt_linalg
link_objs nt_linalg nt_linalg
run_test nt_linalg "$TIMEOUT_SINGLE"
compile_obj_self nt_polyfield
link_objs nt_polyfield nt_polyfield
run_test nt_polyfield "$TIMEOUT_SINGLE"
# crtsolve: multi-modular EXACT integer linear solve (src/lib/nt/crtsolve.tv) —
# the elimination-engine seed (@internal-note: plan-elimination-engine, Stage 3);
# first consumer = Wanderer's exact PST
# base. Per-prime cs_elim (31-bit primes: every product < 2^62, pure i64) +
# Cramer det/num residues + Garner CRT reconstruction (imports rns_dyn's wide
# limb kit — the GPT-2 RNS kernel reused) + Q20 by limb long division + FRESH-
# prime certificate. Gate: hand-exact 2x2 (w=(4/5,7/5) -> Q20 838861/1468006),
# n=12 pipeline with la_solve<Field<2013265921>> cross-check 12/12 (Cramer-CRT
# vs carrier-contract rref, two independent paths), singular refusal (status 2).
# tvc_self-only (import, Model A). Design: @internal-design: nt (crtsolve).
compile_obj_self crtsolve_test
link_objs crtsolve_test crtsolve_test
run_test crtsolve_test "$TIMEOUT_SINGLE"
# nt_radical: pf_radical (char-p squarefree part, incl. the g'==0 p-th-root branch)
# + pf_no_simple_root (the Partner-Lemma "no singleton fiber" test). The W2 P4
# partner consumer's infra. Grounded over prime F_2/F_3 (exercises the p-th root)
# and F_97. tvc_self-only (import).
compile_obj_self nt_radical
link_objs nt_radical nt_radical
run_test nt_radical "$TIMEOUT_SINGLE"

# Mobius paper (@internal-note: plan-mobius-inclusion, W-B P4): the Section-6
# exhaustive corroborations over F_2 / F_3. Oracle =
# ZERO violations (a count difference vs the paper is an enumeration-convention
# finding). mobius_coset: Lemma-cosets promotion + coset structure — reproduces
# 1,085 instances / 112 distinct (q=3) and 148 (q=5) EXACTLY, zero violations;
# the short-window control shows the 2L threshold is necessary. Plan B3.
compile_obj_self mobius_coset
link_objs mobius_coset mobius_coset
run_test mobius_coset "$TIMEOUT_MULTI"
# mobius_signed: the q=2 signed case over F_3 (a_{2n}=-a_n, a_{4n}=0 imposed
# directly). Reproduces 182 admissible EXACTLY; all four checks pass with zero
# violations (Z + S propagate, coset structure of the decimation D_n=a_{2n}+a_n,
# D != 0). Plan B3.
compile_obj_self mobius_signed
link_objs mobius_signed mobius_signed
run_test mobius_signed "$TIMEOUT_SINGLE"
# mobius_partner: the Partner-Lemma exhaustive over F_2 (q=3,5). Full chain
# bm -> minpoly -> pf_radical -> x^{q^2} action matrix -> charpoly over BinField<8>
# (F_2 has too few interp points) -> downcast -> pf_no_simple_root. ZERO singleton
# fibers on every (Z)-solution (the oracle); the instance count follows a window
# convention that admits a superset of the note's 31,232/15,439 (a finding — see
# the file header). Plan B3.
compile_obj_self mobius_partner
link_objs mobius_partner mobius_partner
run_test mobius_partner "$TIMEOUT_MULTI"
# mobius_coeff: the F_2^60 coefficient-function closure (B6, the STRONG Lemma 5.1(c)
# over q=3/F_2 — coeff_verify.py's check). Every distinct solution: factor Mrev over
# F_2, build the EXACT splitting field F_{2^m} (m=lcm(2,factor degs) in a per-m
# BinField<m> carrier — the 15-carrier table {2..60}), locate roots by subfield
# enumeration, solve the confluent binom-basis system over F_{2^m}, re-verify on 40
# fresh terms, and check coset membership + multiplicity + COEFFICIENT-TUPLE equality
# across each U_3-coset. 1,085 instances / 112 distinct, ZERO violations (cross-checked
# vs coeff_verify.py to the digit). The m-histogram is a FINDING (only {2,4,6,12} occur
# of the <=60 a-priori set); the m=70 refusal probe (factor pattern {5,7}) shows the
# one-word-model boundary. The BinField<K!=8> unlock's coefficient-closure consumer.
# Plan B6; design @internal-design: nt.
compile_obj_self mobius_coeff
link_objs mobius_coeff mobius_coeff
run_test mobius_coeff "$TIMEOUT_MULTI"

# Mobius paper (@internal-note: plan-mobius-inclusion, W-B P5): the local model
# + extremal horizon (Theorem "Local" / F(L)).
# mobius_local: the principal-class minimum frequency count 2q-delta0-delta1.
# Constructs the A=1 extremal (Step 4 of the proof), verifies (Z)/(S)/(W) on
# Z/q^2, counts its Fourier support by an exact DFT over the carrier field.
# Prime cases VERIFIED (q=3 @ p=19,37 -> 6; q=5 @ p=101 -> 10, delta=0 = min
# 2q). The BINARY delta!=0 cases FLIPPED by the BinField<K!=8> unlock: q=3
# over F_64 (BinField<6>) -> computed 4 = 2q-2, q=5 over F_2^20 (BinField<20>)
# -> computed 8 = 2q-2 — the theorem's formula values, now computed. The odd-
# char k>2 cases (F_7^3, F_3^20, F_7^4, F_11^5) stay NAMED REFUSALS (general
# ExtField<F, m> deferred). Import-based. Plan B4.
compile_obj_self mobius_local
link_objs mobius_local mobius_local
run_test mobius_local "$TIMEOUT_SINGLE"
# mobius_extremal: the F(L) extremal-horizon witnesses. The vanishing-only class
# decodes as indicators of squarefree-rich progressions that "die" at the first
# non-squarefree term; this verifies the named witnesses (a field-independent
# squarefree-arithmetic fact): 1_{n==a mod d} dies at first non-squarefree n in
# the progression. Reproduces F(1)=3, F(2)=8, F(4)=26, F(6)=124, F(12)=274 EXACTLY
# (deaths 4/9/27/125/275). Full max-optimality + non-indicator intermediates
# (F(3)=17, ...) deferred to the absent extremal-search reference. Standalone ->
# dual-parity (also run_dual.sh). Plan B5.
test_single mobius_extremal
# jacobian_counterexample: machine interrogation of the Alpoge map (July 2026),
# the first counterexample to the Jacobian Conjecture. [A] CRT-complete proof
# that det JF = -2 over Z (exhaustive over F_p^3, p in {13,31,61,101,127};
# bound B=19286, prime product 315326141 > 2B — the five zeros ARE the proof);
# [B] the triple collision (0,0,-1/4),(1,-3/2,13/2),(-1,3/2,13/2) -> (-1/4,0,0)
# in all five fields; [C] monodromy tomography: fiber histograms over F_31^3 /
# F_101^3 match full-S_3 Chebotarev (1/3, 1/2, 0, 1/6) with the empty k=2
# class as the etale signature; [D] the aggregation punchline: fiber-count
# streams N_n over F_{3^n} (n<=6) into carrier-generic bm<FB> — the pointwise
# inverse does not exist, the zeta-style aggregate has linear complexity <= 2
# (C = 1-x at the triple point, 1-x^2 generically: Dwork rationality on eight
# bytes); [E] the Jelonek leading coefficient vanishes AT the triple point:
# the collision lives over the non-properness hypersurface. Exact-arithmetic
# oracle: examples/jacobian_oracle.py (symbolic det, Res_y eliminant deg 3,
# F_{3^n} log-table brute force — every printed line cross-checked).
compile_obj_self jacobian_counterexample
link_objs jacobian_counterexample jacobian_counterexample
run_test jacobian_counterexample "$TIMEOUT_MULTI"

# O0 observability import (src/lib/observe/trace.tv) — the in-field COLUMN of the
# `observe` morphism: caller-owned own-cell Trace + in-field reducers (moments,
# histogram) + the regime/onset DECIDER. tvc_self-only (import-based, like the
# codec). observe_satotate retrofits satotate_vertical's in-field readouts through
# the library (bit-identical baseline; the leaves-field nm divided explicitly in
# the driver). observe_onset gates observe_residual (forward_diff inward) + the
# agg_regime decider on an own-cell trace. Oracle for onset:
# @internal-oracle: observe_onset.
compile_obj_self observe_satotate
link_objs observe_satotate observe_satotate
run_test observe_satotate "$TIMEOUT_SINGLE"
compile_obj_self observe_onset
link_objs observe_onset observe_onset
run_test observe_onset "$TIMEOUT_SINGLE"

# observe-learn bit-accounting golden (src/lib/observe/learn.tv). Synthetic,
# WAV-free, register-trace-free: isolates the FITTING/coding logic (Rice cost,
# zigzag centering, momentum-bucket assignment, per-window + per-order selection).
# Anchor 1 all-zero is hand-derived (n bits at best k=0); Anchor 2 mixed signal is
# a quadratic whose Delta^3 vanishes -> multiorder must find the annihilating
# order. Any change to the bit accounting flips a number and this fails. The audio
# result (@internal-design: util) rides on this logic being correct.
compile_obj_self learn
compile_obj_self observe_learn_gate
link_objs observe_learn_gate observe_learn_gate learn
run_test observe_learn_gate "$TIMEOUT_SINGLE"

# Phase 3 round-trip gate: the REAL bitstream (wire.tv imports learn.tv —
# link wire.o only). Asserts decode(encode(x)) == x on all four streams AND
# bit parity (wire == cost model + 32-bit header) AND encoder-cost identity
# with codec_stereo_windowed_bits. Every emitter claim is decoder-payable;
# corpus certificate: examples/observe_wire_audio.tv (3 tracks, 0 mismatches).
compile_obj_self wire
compile_obj_self observe_wire_gate
link_objs observe_wire_gate observe_wire_gate wire
run_test observe_wire_gate "$TIMEOUT_SINGLE"

# Lossy layer v3 (reduced-depth per-block B_w; exact-arithmetic arc Stage A):
# the certificate gate. Per case: parity 0 (wire == cost + n hdr + B_w),
# decode(encode(Q(x))) == Q(x) on all four streams, and per-sample
# |orig - x_hat| <= 2^s (reduced-depth bound). s in {0,2,4,8} + a varying
# per-block profile. Detail: @internal-design: util "The lossy layer".
compile_obj_self observe_lossy_gate
link_objs observe_lossy_gate observe_lossy_gate wire
run_test observe_lossy_gate "$TIMEOUT_SINGLE"

# NTT (single-file tests)
compile_obj ntt
test_single ntt_test
test_single ntt_roots_test

# NTT link test (2-file)
compile_obj ntt_link_test
link_objs ntt_link_test ntt ntt_link_test
run_test ntt_link_test "$TIMEOUT_MULTI"

# Crypto stack
compile_obj poseidon2
compile_obj merkle

# poseidon2_test (2-file: poseidon2 + test, but test has its own field decl)
compile_obj poseidon2_test
link_objs poseidon2_test poseidon2 poseidon2_test
run_test poseidon2_test "$TIMEOUT_MULTI"

# merkle_test (3-file)
compile_obj merkle_test
link_objs merkle_test poseidon2 merkle merkle_test
run_test merkle_test "$TIMEOUT_MULTI"

# FRI (5-file)
compile_obj fri
compile_obj fri_test
link_objs fri_test ntt poseidon2 merkle fri fri_test
run_test fri_test "$TIMEOUT_MULTI"

# FRI high-degree (5-file)
compile_obj fri_highdeg_test
link_objs fri_highdeg_test ntt poseidon2 merkle fri fri_highdeg_test
run_test fri_highdeg_test "$TIMEOUT_MULTI"

# PLONK (6-file)
compile_obj plonk
compile_obj plonk_test
link_objs plonk_test ntt poseidon2 merkle fri plonk plonk_test
run_test plonk_test "$TIMEOUT_MULTI"

# The Tier-0 flagship: the field stack over a socket. PLONK prove+verify
# (the plonk_test circuit) runs server-side and the verdicts travel over
# TCP as an HTTP response — single-process loopback, ephemeral port, so it
# gates like any other test. Mixed-compiler link: net_zk_serve is
# tvc_self-built (imports net/), the crypto objects are seed-built.
compile_obj_self net_zk_serve
link_objs net_zk_serve ntt poseidon2 merkle fri plonk net_zk_serve
run_test net_zk_serve "$TIMEOUT_MULTI"

# ZK backend (7-file chains)
compile_obj zk_cubic
compile_obj zk_cubic_test
link_objs zk_cubic_test ntt poseidon2 merkle fri plonk zk_cubic zk_cubic_test
run_test zk_cubic_test "$TIMEOUT_MULTI"

compile_obj zk_forward_sum
compile_obj zk_forward_sum_test
link_objs zk_forward_sum_test ntt poseidon2 merkle fri plonk zk_forward_sum zk_forward_sum_test
run_test zk_forward_sum_test "$TIMEOUT_MULTI"

compile_obj zk_loop_test
compile_obj zk_loop_test_driver
link_objs zk_loop_test ntt poseidon2 merkle fri plonk zk_loop_test zk_loop_test_driver
run_test zk_loop_test "$TIMEOUT_MULTI"

compile_obj zk_if_test
compile_obj zk_if_test_driver
link_objs zk_if_test ntt poseidon2 merkle fri plonk zk_if_test zk_if_test_driver
run_test zk_if_test "$TIMEOUT_MULTI"

# Float membrane: IEEE-754 decoder (2-file: ieee + test).
# The decoder is pure integer shifts/masks — assert ZERO float ops in its IR
# (the membrane never touches the FPU), then check decode of known patterns.
compile_obj ieee
compile_obj ieee_decode_test
test_zero_float ieee
link_objs ieee_decode_test ieee ieee_decode_test
run_test ieee_decode_test "$TIMEOUT_MULTI"

# Float membrane: exact dyadic embedding into Z/pZ (2-file: embed + test).
# Decode -> embed M*inv(2)^|E| -> reconstruct, must roundtrip bit-exact, and
# match across two primes (dual-field). Embedding is exact at rest.
compile_obj embed
compile_obj embed_roundtrip_test
test_zero_float embed
link_objs embed_roundtrip_test ieee embed embed_roundtrip_test
run_test embed_roundtrip_test "$TIMEOUT_MULTI"

# Float membrane: exact dyadic scaled-int quantization (import: quant + ieee;
# tvc_self-only). clamp(round_half_away(x*2^k)) by integer shifts on (sign,M,E)
# — the int8 NNUE import rule; ternary = the k=0,qmax=1 special case. Pinned
# half/clamp/saturation boundaries + inf/nan refusals + subnormal counting;
# zero float ops in the merged unit (the membrane never touches the FPU).
compile_obj_self quant_membrane
test_zero_float quant_membrane
link_objs quant_membrane quant_membrane
run_test quant_membrane "$TIMEOUT_SINGLE"

# Shared Result<T,E> (src/lib/core/result.tv) consumed via import
# (tvc_self-only). Struct T + enum E payloads through a fn return, matched
# and nested-matched — the wrap-layer (fs/, net/) error-model shapes.
compile_obj_self result_basics
link_objs result_basics result_basics
run_test result_basics "$TIMEOUT_SINGLE"

# `?` operator (tvc_self-only): Result early-return propagation — Ok yields
# the payload in place (chainable, aggregate T by pointer), Err rebuilds the
# enclosing fn's Result and returns. Refusals gated in tests/diag/qmark_*.
compile_obj_self qmark_basics
link_objs qmark_basics qmark_basics
run_test qmark_basics "$TIMEOUT_SINGLE"

# fs/ wrap layer (tvc_self-only): buffered FILE* underneath, Result at the
# membrane, `?` end-to-end (write/exists/size/read-back/append + the Err
# arm on a missing path). Writes /tmp/traveler_fs_basics.txt (truncated
# each run — deterministic).
compile_obj_self fs_basics
link_objs fs_basics fs_basics
run_test fs_basics "$TIMEOUT_SINGLE"

# The lab harness in Traveler (tvc_self-only): fmt + json + fs/atomic compose to
# emit the self-improving lab's tmp-output contract — output.jsonl (append-only
# JSONL events), an ATOMIC heartbeat.json (write-tmp + rename), and final.json.
# The proof that the harness, not just the compute kernel, is writable in
# Traveler. Also the first cross-subdir import diamond (fs/ + json/ + fmt/ share
# collections/string.tv) — exercises the imp_resolve_path canonicalization.
compile_obj_self lab_emit
link_objs lab_emit lab_emit
run_test lab_emit "$TIMEOUT_SINGLE"

# The READ side of the lab harness (tvc_self-only): emit a request.json, then
# read + PARSE it and extract fields (object, NESTED object, array, missing key,
# and a float REFUSED loudly). The json/ parser (flat index arena, integers-only,
# Result at the membrane) — the experiment-container entrypoint's first act.
compile_obj_self lab_request
link_objs lab_request lab_request
run_test lab_request "$TIMEOUT_SINGLE"

# The lab experiment-container ENTRYPOINT, end to end (tvc_self-only): read
# request.json -> bounded self-CERTIFYING work (a Gauss sum vs its closed form),
# emitting an atomic heartbeat + measurement events -> final.json with the
# outcome + certificate. The Phase-2 container payload: request in, contract out,
# all Traveler.
compile_obj_self lab_experiment
link_objs lab_experiment lab_experiment
run_test lab_experiment "$TIMEOUT_SINGLE"

# net/ TCP wrap layer (tvc_self-only): single-process loopback on an
# EPHEMERAL port (CI-safe), sockaddr_in as raw bytes per detected OS
# (uname), bytes both directions through `?`.
compile_obj_self net_loopback
link_objs net_loopback net_loopback
run_test net_loopback "$TIMEOUT_SINGLE"

# time/ wrap layer (tvc_self-only): clock_gettime/nanosleep underneath,
# Result at the membrane, `?` end-to-end. Verdict-only output (monotonicity,
# wall sanity, the ns multiply, the sleep lower-bound, the BadDuration Err
# arm) — never raw timings. The sleep-contract line is CI's live check of the
# per-OS CLOCK_MONOTONIC id table (macOS 6 / Linux 1).
compile_obj_self time_basics
link_objs time_basics time_basics
run_test time_basics "$TIMEOUT_SINGLE"
test_no_alloc_fns time_basics time_mono_ns time_wall_ns time_wall_sec time_sleep_ns

# time/ as an algebraic source (tvc_self-only): T1a — sample CLOCK_MONOTONIC,
# lift the rebased ns stream into F_{2^31-1}, and round-trip it through the
# four-op kernel (forward_diff → forward_sum). Verdicts only: raw-clock
# monotonicity + the bit-exact Σ∘Δ isomorphism on live time.
compile_obj_self time_deltas
link_objs time_deltas time_deltas
run_test time_deltas "$TIMEOUT_SINGLE"

# time/ as an algebraic source (tvc_self-only): T1b — drive a two-phase
# inter-arrival stream (8× 2ms sleeps, then 8× 80ms), binarize the intervals
# OUTSIDE the field (the membrane rule), and let regime_detect land the phase
# boundary at index 8, field-invariant across two primes. Sleeps ~0.66s.
compile_obj_self time_regime
link_objs time_regime time_regime
run_test time_regime "$TIMEOUT_SINGLE"

# Codec (2-file). The codec is import-split (tvc_self-only); its driver is
# plain and still C-seed-compiled. Object ABI is identical, so they link.
compile_obj_self piecewise_codec
compile_obj piecewise_continuation_test
link_objs piecewise_continuation_test piecewise_codec piecewise_continuation_test
run_test piecewise_continuation_test "$TIMEOUT_MULTI"

# Codec byte-identity golden (2-file: pc_golden + piecewise_codec)
# FNV-1a hash + length + segment count + lossless flag per test vector.
# Gates the wire format against accidental drift (and the Fork-A rewrite).
compile_obj pc_golden
link_objs pc_golden piecewise_codec pc_golden
run_test pc_golden "$TIMEOUT_MULTI"

# Reed-Solomon erasure coding over GF(2^8) (2-file: reed_solomon + test)
# Encode k data symbols -> n; recover from any k survivors (Lagrange interp).
# reed_solomon.tv is a carrier header importing the carrier-free rs_core.tv
# (the BinField<K!=8> unlock's split) -> tvc_self-built; the merged unit is
# IR-byte-identical to the old monolith. The test driver stays seed-compiled
# (extern + 2-obj link).
compile_obj_self reed_solomon
compile_obj reed_solomon_test
link_objs reed_solomon_test reed_solomon reed_solomon_test
run_test reed_solomon_test "$TIMEOUT_MULTI"

# Reed-Solomon ERROR decode over GF(2^8) (unknown positions): syndromes -> bm<GF>
# (nt/linrec.tv, carrier-generic Berlekamp-Massey) -> Chien -> Forney, with the
# erasure-reduction cross-gate (two independent value routes must agree). The
# load-bearing consumer that makes bm<GF> real. Imports (Model A) reed_solomon.tv +
# nt/linrec.tv -> tvc_self-only. RS(12,5) 0-3 errors corrected; 4 errors refused
# DETERMINISTICALLY (d=8 > 2t+1). Oracle: rs_error_decode_oracle.py (bit-for-bit).
compile_obj_self rs_error_decode_test
link_objs rs_error_decode_test rs_error_decode_test
run_test rs_error_decode_test "$TIMEOUT_MULTI"

# BinField<K != 8> unlock (the Rabin-irreducibility axiom gate): GF(2^6) /
# GF(2^16) / GF(2^60) through the generic shift-xor runtime — mul oracles,
# Fermat inverse round-trips, char-2 identities, Frobenius x^(2^K) == x,
# cast canonicalization (low-K-bits embed). GF(2^8, 0x11B) stays on the
# legacy log/exp path (byte-stable IR, asserted by the codegen-diff gate).
# tvc_self-only: the frozen C seed refuses K != 8. @internal-design: nt.
compile_obj_self binfield_wide_basics
link_objs binfield_wide_basics binfield_wide_basics
run_test binfield_wide_basics "$TIMEOUT_SINGLE"

# RS error decode over GF(2^16) — the unlock's NAMED ECC consumer (@internal-design: ecc:
# "this is the named next consumer"). The driver IS the carrier header: the
# only carrier lines are `binfield GF = BinField<16, 0x1100B>` + gf_qm2() =
# 65534; rs_core.tv + rs_errdec_core.tv are byte-shared with GF(2^8).
# RS(300, 281): n > 255 (past GF(2^8)'s ceiling), 16-bit error values
# recovered exactly, t = 9 corrected, 10 errors refused deterministically
# (d = 20 > 2t+1). tvc_self-only (import).
compile_obj_self rs16_errdec_test
link_objs rs16_errdec_test rs16_errdec_test
run_test rs16_errdec_test "$TIMEOUT_MULTI"

# Multi-level pointer alloc element-size (known-issue #19). tvc_self-only: the
# frozen C seed cannot codegen `**i64` indexing (`getelementptr void`), so this
# gates the self-hosting compiler's alloc sizing. Pre-fix the 256-slot table was
# malloc'd at 256 bytes (elem=1) and produced non-deterministic garbage; post-fix
# it is sized n*8 and sums deterministically to 5788160.
compile_obj_self alloc_ptr_table
link_objs alloc_ptr_table alloc_ptr_table
run_test alloc_ptr_table "$TIMEOUT_SINGLE"

# Generic-N RNS exact NN matmul (src/lib/rns/rns_dyn.tv). tvc_self-only (import-
# based). Drives the runtime-k kernel on synthetic data: 66-bit accumulators
# (past i64) carried exactly across a 4-prime RNS, auto-parallel MAC + Garner CRT,
# deterministic checksum 2912256. The parallel-determinism sweep is in run_pfor.sh.
compile_obj_self rns_dyn_matmul
link_objs rns_dyn_matmul rns_dyn_matmul
run_test rns_dyn_matmul "$TIMEOUT_MULTI"

# Wide integer surface types (i128/u128/i256/u256), division-free. tvc_self-only:
# the frozen C seed has no i128/i256 surface type. Slices 1-2: add/sub/mul/shift/
# compare/cast across the >64-bit boundary lower to hardware iN IR (no __udivti3);
# values cast to i64 to print. See @internal-note: plan-wide-int-surface-types.
compile_obj_self wide_i128_add
link_objs wide_i128_add wide_i128_add
run_test wide_i128_add "$TIMEOUT_SINGLE"
compile_obj_self wide_i256_add
link_objs wide_i256_add wide_i256_add
run_test wide_i256_add "$TIMEOUT_SINGLE"
# Slice 3: literals > 2^64. Decimal verbatim; hex->decimal converted at compile
# time (i64-only long division, no emitted __udivti3). BN254 modulus hex==dec.
compile_obj_self wide_literal
link_objs wide_literal wide_literal
run_test wide_literal "$TIMEOUT_SINGLE"
# Slice 4: full-width decimal print via double-dabble (binary->BCD, no division
# of any width). 39-digit i128 + 78-digit i256, zero/boundary/maxima cases.
compile_obj_self wide_print
link_objs wide_print wide_print
run_test wide_print "$TIMEOUT_SINGLE"

# Wide-accumulator consumer: a 2x
# nested 4x4 matmul over NARROW (<2^62) i64 inputs whose column sums reach ~2^130
# (PAST i128), requantized by a constant right-shift (round half up — division-
# free, #21's core preserved). Computed TWO ways that must agree: hand-limbed
# 8x31-bit-limb naturals AND a native i256 accumulator; a width certificate then
# drives both past 2^200. Bit-exact vs a Python bignum oracle
# (@internal-oracle: wide_acc). After U1, the primitive-capture dispatch lift
# (Stage D): the i256 output loop is a
# flat pure body (cell() hoisted) and DISPATCHES — the named Stage-D consumer,
# measured ~8.3x at 14 threads on the scaled idiom, bit-identical across thread
# counts; the hand-limb loop stays serial (mutating-call — the Stage-E datapoint).
# tvc_self-only (i256). See @internal-note: plan-exact-arithmetic-arc Stages B/D.
compile_obj_self wide_acc_matmul
link_objs wide_acc_matmul wide_acc_matmul
run_test wide_acc_matmul "$TIMEOUT_SINGLE"

# Intern-table grow/rehash gate (A-1): >9000 distinct identifiers cross both the
# old fixed 8192-slot ceiling and the new 0.7 load factor (two rehashes). Self-
# compiler only — the frozen C seed keeps the un-fixed fixed-cap table, so this
# program is intentionally outside dual parity (like the wide-int surface tests).
compile_obj_self intern_stress
link_objs intern_stress intern_stress
run_test intern_stress "$TIMEOUT_SINGLE"

# Poly core (2-file)
compile_obj poly_core
compile_obj poly_core_test
link_objs poly_core_test poly_core poly_core_test
run_test poly_core_test "$TIMEOUT_MULTI"

# Regime topology (2-file, reuses poly_core.o)
 compile_obj regime_topology_test
 link_objs regime_topology_test poly_core regime_topology_test
 run_test regime_topology_test "$TIMEOUT_MULTI"

 # Regime invariance theorem demonstrators (2-file, reuses poly_core.o)
 compile_obj regime_invariance_test
 link_objs regime_invariance_test poly_core regime_invariance_test
 run_test regime_invariance_test "$TIMEOUT_MULTI"

 # Regime 2D (2-file, reuses poly_core.o)
compile_obj regime_2d_test
link_objs regime_2d_test poly_core regime_2d_test
run_test regime_2d_test "$TIMEOUT_MULTI"

# Regime intersection (2-file, reuses poly_core.o)
compile_obj regime_intersect_test
link_objs regime_intersect_test poly_core regime_intersect_test
run_test regime_intersect_test "$TIMEOUT_MULTI"

fi  # TIER1_ONLY

# --- Negative tests ---
echo ""
echo "=== Negative Tests ==="
echo ""
for nf in "$NEGATIVE"/*.tv; do
    [ -f "$nf" ] || continue
    test_negative "$(basename "$nf" .tv)"
done

fi  # NEGATIVE_ONLY

# --- Auto-parallelization soundness suite ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Parallel Soundness (tests/pfor) ==="
    PFOR_RC=0
    "$SCRIPT_DIR/run_pfor.sh" || PFOR_RC=$?
    if [ "$PFOR_RC" -ne 0 ]; then
        FAIL=$((FAIL + PFOR_RC))
        FAILURES="$FAILURES pfor_suite"
    fi
fi

# --- Dynamic field primitives (Phase 1 runtime IR) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Dynamic Field Primitives (tests/dynfield) ==="
    if "$SCRIPT_DIR/dynfield/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES dynfield"
    fi
fi

# --- Diagnostics catalog (A2 error messages, vs tvc_self) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Diagnostics Catalog (tests/diag) ==="
    if "$SCRIPT_DIR/run_diag.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES diag"
    fi
fi

# --- Formatter idempotence + meaning preservation (B3a) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Formatter (tests/run_fmt.sh) ==="
    if "$SCRIPT_DIR/run_fmt.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES fmt"
    fi
fi

# --- LSP engine gate (B3b machine-readable --diagnostics) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== LSP engine (tests/run_lsp.sh) ==="
    if "$SCRIPT_DIR/run_lsp.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES lsp"
    fi
fi

# --- Doc generator gate (B3b tvdoc) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Doc generator (tests/run_doc.sh) ==="
    if "$SCRIPT_DIR/run_doc.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES doc"
    fi
fi

# --- Module size gate (lib modules <= 1500 lines; tvc_self the sole exemption) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Module size gate (tests/run_sizegate.sh) ==="
    if "$SCRIPT_DIR/run_sizegate.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES sizegate"
    fi
fi

# --- Multi-object field-runtime fold probe (#15: i128 + i512 link hazard) ---
# Exercises the cross-object path the dyn/wide single-object stack never hits.
# Asserts the safe (llvm-link-first) path is correct on this toolchain and warns
# if the -O2 separate-object fold hazard is live. Needs tvc_self (uses dyn/wide).
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Field-runtime fold probe (tests/foldbug/run.sh) ==="
    if LLC="$LLC" "$SCRIPT_DIR/foldbug/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES foldbug"
    fi
fi

# --- ** right-associativity gate (spec 2.10; AST-shape regression) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Pow associativity (tests/pow_assoc/run.sh) ==="
    if "$SCRIPT_DIR/pow_assoc/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES pow_assoc"
    fi
fi

# --- Codegen-diff gate (IR-graph Stage 0: emitted-IR drift on a real corpus) ---
# Hashes tvc_self's emitted IR for a curated corpus vs a golden manifest. Catches
# unintended codegen drift the fixed point can't (fixed point = compiler
# reproduces itself; this = compiler reproduces same output for arbitrary code).
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Codegen-diff (tests/codegen_diff/run.sh) ==="
    if "$SCRIPT_DIR/codegen_diff/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES codegen_diff"
    fi
fi

# --- pfor-report gate (exact-arithmetic arc Stage C: per-loop dispatch verdicts) ---
# Functional: the --pfor-report query mode emits valid JSONL and the race
# catalogue reports its known refusal reasons. Baseline: the whole corpus's
# per-loop verdicts are dispatch-stable vs a golden (the not-field subset is
# Stage D's flip set). Read-only mode -> codegen byte-unchanged (parity-safe).
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== pfor-report (tests/pfor_report/run.sh) ==="
    if "$SCRIPT_DIR/pfor_report/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES pfor_report"
    fi
fi

# --- GPU Stage-0 device codegen gate (@internal-note: plan-gpu-purity-runtime) ---
# Re-emits a proven-parallel pfor worker as an amdgpu_kernel and asserts llc
# lowers it to a valid gfx1100 code object (no GPU hardware). Skips cleanly if
# the local llc lacks the amdgcn target. tvc_self-only (--emit-gpu).
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== gpu Stage-0 (tests/gpu/run.sh) ==="
    if "$SCRIPT_DIR/gpu/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES gpu"
    fi
fi

# --- --emit driver gate (the one-shot compile: source -> native in one call) ---
# `tvc x.tv -o x --emit exe` drives llc (+ cc) itself. The default --emit ir path
# is byte-unchanged (g_emit_mode==0 never touches the driver), so the fixed point
# is untouched; this gate just proves the obj/exe modes produce a running binary.
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== emit driver (tests/emit/run.sh) ==="
    if "$SCRIPT_DIR/emit/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES emit"
    fi
fi

# --- Eval-diff gate (eval-engine E1: the differential semantic oracle) ---
# eval(prog) == run(compile(prog)) byte-exact (stdout + exit) on a curated
# covered corpus. The evaluator refuses uncovered constructs (exit 97), so a
# refusal on a corpus entry is a coverage regression. --eval is a separate
# walk (not a codegen tap) -> normal-mode output byte-unchanged by
# construction (proven by the codegen-diff gate above).
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== eval-diff (tests/eval_diff/run.sh) ==="
    if "$SCRIPT_DIR/eval_diff/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES eval_diff"
    fi
fi

# --- REPL session gate (E3: persistence + error containment) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== REPL session (tests/repl/run.sh) ==="
    if "$SCRIPT_DIR/repl/run.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES repl"
    fi
fi

# --- Bootstrap gate (B4: Traveler builds itself, no C in the chain) ---
if [ "$NEGATIVE_ONLY" -eq 0 ] && [ "$TIER1_ONLY" -eq 0 ]; then
    echo ""
    echo "=== Bootstrap (tests/run_bootstrap.sh) ==="
    if "$SCRIPT_DIR/run_bootstrap.sh"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES bootstrap"
    fi
fi

# --- Summary ---
END_TIME=$(python3 -c "import time; print(int(time.time()*1000))")
ELAPSED=$(( (END_TIME - START_TIME) ))

echo ""
echo "============================================"
printf "  RESULTS: %d PASS, %d FAIL, %d SKIP  (%d.%03ds)\n" \
    "$PASS" "$FAIL" "$SKIP" "$((ELAPSED / 1000))" "$((ELAPSED % 1000))"
if [ -n "$FAILURES" ]; then
    echo "  FAILED:$FAILURES"
fi
echo "============================================"
echo ""

exit "$FAIL"
