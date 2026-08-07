#!/bin/bash
# Auto-parallelization soundness regression suite.
#
# Each test in tests/pfor/ asserts two things:
#   1. DISPATCH: the emitted .ll contains exactly the expected number of
#      __pfor_worker functions.  Race demonstrators must have their racy
#      loop rejected (worker count drops); positive controls must keep
#      their workers (no lost parallelism).
#   2. DETERMINISM: output under TRAVELER_THREADS=32 equals output under
#      TRAVELER_THREADS=1 equals the recorded baseline.
#
# Tests listed in XFAIL are known-broken against the current compiler
# (the via-negativa record).  They report as XFAIL (expected failure,
# exit 0) until the fixing phase lands, then must be removed from the
# XFAIL list so they become hard-required.
#
# Usage: ./run_pfor.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
PFOR_DIR="$SCRIPT_DIR/pfor"
EXPECTED="$PFOR_DIR/expected"

# --- Platform detection (same as run.sh) ---
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

# --- Build bootstrap compiler ---
(cd "$SRC_DIR" && make tvc >/dev/null 2>&1) || {
    (cd "$SRC_DIR" && clang -O2 -Wall -Wextra -std=c99 -o tvc tvc.c) || exit 1
}
TVC="$SRC_DIR/tvc"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# ============================================================
#  Test manifest: name -> expected worker count (post-fix)
#
#  Race demonstrators expect 0 workers from their racy loops.
#  race2.tv expects 1: fill_ones is legitimately parallel and
#  must KEEP its worker; only sum_cell's dispatch is unsound.
# ============================================================
TESTS=(
    "pfor_race_const_idx:0"
    "pfor_race_invariant_idx:0"
    "pfor_race_noninjective:0"
    "pfor_race_raw:0"
    "pfor_race_prefix_recur:0"
    "pfor_race_let_idx:0"
    "pfor_race_mutating_call:0"
    "pfor_race_global_assign:0"
    "pfor_race_key_collision:0"
    "pfor_race_assign_carried:0"
    "pfor_race_alias_letptr:0"
    "pfor_parity_bigliteral:3"
    "pfor_dispatch_edges:4"
    "pfor_boundary_values:9"
    "pfor_alias_eq:1"
    "race2:1"
    "pfor_ok_pure_call:1"
    "pfor_ok_butterfly:1"
    "pfor_ok_offset_literal:1"
    "pfor_ok_dyn:1"
    "pfor_ok_shortcircuit:1"
    "pfor_ok_rns:3"
)

# Tests that additionally sweep TRAVELER_THREADS over garbage/extreme
# values (env parsing + chunk arithmetic edges)
SWEEP_TESTS="pfor_dispatch_edges pfor_boundary_values pfor_ok_rns"
SWEEP_VALUES=("garbage" "0" "-1" "7" "1000")

is_sweep() {
    local t="$1"
    for x in $SWEEP_TESTS; do
        [ "$x" = "$t" ] && return 0
    done
    return 1
}

# ============================================================
#  XFAIL list: known-unsound against the current compiler.
#  Remove entries as the fixing phase lands:
#    Phase 1 (write-index injectivity): const_idx, invariant_idx,
#             noninjective, let_idx
#    Phase 2 (footprint disjointness):  raw
#    Phase 3 (mutates_params):          mutating_call, race2
# ============================================================
XFAIL=""

is_xfail() {
    local t="$1"
    for x in $XFAIL; do
        [ "$x" = "$t" ] && return 0
    done
    return 1
}

PASS=0
FAIL=0
XFAILED=0
XPASS=0
TOTAL=0
FAILURES=""

echo ""
echo "=== Auto-parallelization soundness tests ==="
echo ""

for entry in "${TESTS[@]}"; do
    name="${entry%%:*}"
    want_workers="${entry##*:}"
    TOTAL=$((TOTAL + 1))

    status="PASS"
    detail=""

    # Compile
    if ! "$TVC" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile"
    fi

    # Dispatch assertion: exact worker count in IR
    if [ "$status" = "PASS" ]; then
        got_workers=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
        if [ "$got_workers" != "$want_workers" ]; then
            status="FAIL"; detail="workers: want $want_workers, got $got_workers"
        fi
    fi

    # Build native
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || {
            status="FAIL"; detail="link"
        }
    fi

    # Determinism assertion: threads=1 == threads=32 == baseline
    if [ "$status" = "PASS" ]; then
        baseline=$(cat "$EXPECTED/${name}.txt")
        s1=$(TRAVELER_THREADS=1 "$TMPDIR/${name}" 2>/dev/null)
        s32=$(TRAVELER_THREADS=32 "$TMPDIR/${name}" 2>/dev/null)
        if [ "$s1" != "$baseline" ]; then
            status="FAIL"; detail="serial output != baseline"
        elif [ "$s32" != "$baseline" ]; then
            status="FAIL"; detail="parallel output != baseline (race manifested)"
        fi
    fi

    # Thread-count sweep: garbage and extreme TRAVELER_THREADS values
    # must all produce the baseline (env parsing + chunk math edges)
    if [ "$status" = "PASS" ] && is_sweep "$name"; then
        for tv in "${SWEEP_VALUES[@]}"; do
            sv=$(TRAVELER_THREADS="$tv" "$TMPDIR/${name}" 2>/dev/null)
            if [ "$sv" != "$baseline" ]; then
                status="FAIL"; detail="sweep THREADS=$tv != baseline"
                break
            fi
        done
    fi

    # XFAIL bookkeeping
    if is_xfail "$name"; then
        if [ "$status" = "FAIL" ]; then
            printf "  [%2d] %-32s XFAIL (%s)\n" "$TOTAL" "$name" "$detail"
            XFAILED=$((XFAILED + 1))
        else
            printf "  [%2d] %-32s XPASS (remove from XFAIL list)\n" "$TOTAL" "$name"
            XPASS=$((XPASS + 1))
            FAILURES="$FAILURES $name(xpass)"
        fi
    else
        if [ "$status" = "PASS" ]; then
            printf "  [%2d] %-32s PASS\n" "$TOTAL" "$name"
            PASS=$((PASS + 1))
        else
            printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
            FAIL=$((FAIL + 1))
            FAILURES="$FAILURES $name"
        fi
    fi
done

# ------------------------------------------------------------
#  A-4 (tvc_self only): the purity FIXPOINT. A 4-deep caller-before-callee
#  impure chain must keep its calling loop SERIAL (0 workers). The C seed used
#  above still has the old two fixed passes (would wrongly emit 1 worker), so
#  this is asserted against stage1 (tvc_self) — like the wide-int / intern-stress
#  self-compiler gates. Build stage1 if absent (mirrors run.sh).
# ------------------------------------------------------------
STAGE1="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ -x "$STAGE1" ]; then
    TOTAL=$((TOTAL + 1))
    name="pfor_self_deep_impure_chain"
    status="PASS"; detail=""
    if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile (stage1)"
    fi
    if [ "$status" = "PASS" ]; then
        gw=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
        [ "$gw" = "0" ] || { status="FAIL"; detail="workers: want 0, got $gw (purity fixpoint regressed)"; }
    fi
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
    fi
    if [ "$status" = "PASS" ]; then
        out=$("$TMPDIR/${name}" 2>/dev/null)
        [ "$out" = "50000" ] || { status="FAIL"; detail="output '$out' != 50000"; }
    fi
    if [ "$status" = "PASS" ]; then
        printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
fi

# Self-only effect/index hardening: hidden effects, caller-relative reads,
# lexical parameter identity, and runtime-width affine constants fail closed.
if [ -x "$STAGE1" ]; then
    EFFECT_TESTS=(
        "pfor_self_member_call:0"
        "pfor_self_hidden_read:1"
        "pfor_self_member_read:0"
        "pfor_self_aggregate_alias_call:0"
        "pfor_self_affine_wrap:0"
        "pfor_self_affine_circle:0"
        "pfor_self_affine_slack:0"
        "pfor_self_affine_point_wrap:0"
        "pfor_self_shadowed_read:0"
        "pfor_parity_bigliteral:2"
    )
    for entry in "${EFFECT_TESTS[@]}"; do
        name="${entry%%:*}"; want_workers="${entry##*:}"
        TOTAL=$((TOTAL + 1)); status="PASS"; detail=""
        if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
            status="FAIL"; detail="compile (stage1)"
        fi
        if [ "$status" = "PASS" ]; then
            gw=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
            [ "$gw" = "$want_workers" ] || { status="FAIL"; detail="workers: want $want_workers, got $gw"; }
        fi
        if [ "$status" = "PASS" ]; then
            "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
            clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
        fi
        if [ "$status" = "PASS" ]; then
            baseline=$(cat "$EXPECTED/${name}.txt")
            s1=$(TRAVELER_THREADS=1 "$TMPDIR/${name}" 2>/dev/null)
            s32=$(TRAVELER_THREADS=32 "$TMPDIR/${name}" 2>/dev/null)
            if [ "$s1" != "$baseline" ]; then
                status="FAIL"; detail="serial output != baseline"
            elif [ "$s32" != "$baseline" ]; then
                status="FAIL"; detail="parallel output != baseline"
            fi
        fi
        if [ "$status" = "PASS" ]; then
            printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
        else
            printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
            FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        fi
    done
fi

# ------------------------------------------------------------
#  #36 (tvc_self only): the deferred-emitter FIXED POINT. A generic fn called
#  from inside an auto-parallelized `for` loop must be emitted — before the fix
#  the worker's `call @sq_G` had no `define @sq_G` (llc: use of undefined value).
#  The loop IS dispatched (1 worker); the gate is that it then passes llc, links,
#  and runs (prints 5). Found by the Wanderer chess engine. The frozen seed
#  shares the pass shape, so this is asserted against stage1 only.
# ------------------------------------------------------------
if [ -x "$STAGE1" ]; then
    TOTAL=$((TOTAL + 1))
    name="pfor_generic_mono"
    status="PASS"; detail=""
    if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile (stage1)"
    fi
    if [ "$status" = "PASS" ]; then
        gw=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
        [ "$gw" = "1" ] || { status="FAIL"; detail="workers: want 1, got $gw"; }
    fi
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="llc/link (undefined-value regressed)"; }
    fi
    if [ "$status" = "PASS" ]; then
        out=$("$TMPDIR/${name}" 2>/dev/null)
        [ "$out" = "5" ] || { status="FAIL"; detail="output '$out' != 5"; }
    fi
    if [ "$status" = "PASS" ]; then
        printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
fi

# ------------------------------------------------------------
#  U1, the lifted dispatch gate (@internal-note: plan-exact-arithmetic-arc,
#  Stage D) —
#  primitive-element loops. Asserted against stage1 ONLY: the frozen C seed
#  keeps the pre-U1 gate (field-only dispatch) by design, so its worker
#  counts would be vacuously 0 here; dual parity remains output-level.
#    - positive controls MUST dispatch (i64 map / overflow map)
#    - via-negativa i64 demonstrators MUST stay serial (raw, scatter):
#      U1 lifted the TYPE gate, not the independence proof
#    - overflow-determinism: wrapped i64 output bit-identical at every
#      thread count (plain add/mul, no nsw — wrap is defined per-element)
#    - trap parity: (stdout, exit status) at T=1 == T=32 for a div-by-zero
#      lane (platform-split outcome, so the assertion is self-relative)
# ------------------------------------------------------------
if [ -x "$STAGE1" ]; then
    U1_TESTS=(
        "pfor_u1_i64_map:1"
        "pfor_u1_i64_raw:0"
        "pfor_u1_i64_scatter:0"
        "pfor_u1_overflow_det:1"
        "pfor_global_capture:3"
    )
    for entry in "${U1_TESTS[@]}"; do
        name="${entry%%:*}"; want_workers="${entry##*:}"
        TOTAL=$((TOTAL + 1)); status="PASS"; detail=""
        if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
            status="FAIL"; detail="compile (stage1)"
        fi
        if [ "$status" = "PASS" ]; then
            got_workers=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
            if [ "$got_workers" != "$want_workers" ]; then
                status="FAIL"; detail="workers: want $want_workers, got $got_workers"
            fi
        fi
        if [ "$status" = "PASS" ]; then
            "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
            clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
        fi
        if [ "$status" = "PASS" ]; then
            baseline=$(cat "$EXPECTED/${name}.txt")
            s1=$(TRAVELER_THREADS=1 "$TMPDIR/${name}" 2>/dev/null)
            s32=$(TRAVELER_THREADS=32 "$TMPDIR/${name}" 2>/dev/null)
            if [ "$s1" != "$baseline" ]; then
                status="FAIL"; detail="serial output != baseline"
            elif [ "$s32" != "$baseline" ]; then
                status="FAIL"; detail="parallel output != baseline (nondeterminism manifested)"
            fi
        fi
        if [ "$status" = "PASS" ] && [ "$name" = "pfor_u1_overflow_det" ]; then
            for tv in "${SWEEP_VALUES[@]}"; do
                sv=$(TRAVELER_THREADS="$tv" "$TMPDIR/${name}" 2>/dev/null)
                if [ "$sv" != "$baseline" ]; then
                    status="FAIL"; detail="sweep THREADS=$tv != baseline"; break
                fi
            done
        fi
        if [ "$status" = "PASS" ]; then
            printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
        else
            printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
            FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        fi
    done

    # Trap parity: self-relative — (stdout, status) T=1 == T=32. No absolute
    # baseline: AArch64 completes (x/0 == 0), x86-64 SIGFPEs; BOTH must do
    # the same thing serial and parallel. Results, when produced, are
    # bit-identical; "which worker trips first" is unspecified, observables
    # are not.
    TOTAL=$((TOTAL + 1)); name="pfor_u1_trap_parity"; status="PASS"; detail=""
    if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile (stage1)"
    fi
    if [ "$status" = "PASS" ]; then
        gw=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}.ll" || true)
        [ "$gw" = "1" ] || { status="FAIL"; detail="workers: want 1, got $gw (division must be admitted)"; }
    fi
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
    fi
    if [ "$status" = "PASS" ]; then
        t1_out=$(TRAVELER_THREADS=1 "$TMPDIR/${name}" 2>/dev/null); t1_st=$?
        t32_out=$(TRAVELER_THREADS=32 "$TMPDIR/${name}" 2>/dev/null); t32_st=$?
        if [ "$t1_st" != "$t32_st" ]; then
            status="FAIL"; detail="exit status: T1=$t1_st T32=$t32_st"
        elif [ "$t1_out" != "$t32_out" ]; then
            status="FAIL"; detail="stdout diverged between T1 and T32"
        fi
    fi
    if [ "$status" = "PASS" ]; then
        printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi

    # ------------------------------------------------------------
    #  #43/#46 (workitem-private mutable): stage1-only —
    #    - positive control: multi-assign to body-local vars MUST dispatch
    #      (1 worker) and stay bit-deterministic across thread counts
    #    - the alias-hole regression (body-local ptr copy as array base,
    #      write side + read-taint side) MUST stay serial
    #    - the no-escape fence (&body-local) MUST stay serial
    #  The frozen seed refuses every AST_ASSIGN by design (serial = correct),
    #  so the positive is asserted against stage1 only; the alias regression
    #  also runs through the seed in the main manifest above (fence mirrored).
    # ------------------------------------------------------------
    PRIV_TESTS=(
        "pfor_ok_private_var:1"
        "pfor_race_alias_letptr:0"
        "pfor_private_escape:0"
    )
    for entry in "${PRIV_TESTS[@]}"; do
        name="${entry%%:*}"; want_workers="${entry##*:}"
        TOTAL=$((TOTAL + 1)); status="PASS"; detail=""
        if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}_s1.ll" >/dev/null 2>&1; then
            status="FAIL"; detail="compile (stage1)"
        fi
        if [ "$status" = "PASS" ]; then
            got_workers=$(grep -c "define internal void @__pfor_worker" "$TMPDIR/${name}_s1.ll" || true)
            if [ "$got_workers" != "$want_workers" ]; then
                status="FAIL"; detail="workers: want $want_workers, got $got_workers"
            fi
        fi
        if [ "$status" = "PASS" ]; then
            "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}_s1.ll" -o "$TMPDIR/${name}_s1.o" 2>/dev/null && \
            clang $LINK_PIE "$TMPDIR/${name}_s1.o" -o "$TMPDIR/${name}_s1" 2>/dev/null || { status="FAIL"; detail="link"; }
        fi
        if [ "$status" = "PASS" ]; then
            baseline=$(cat "$EXPECTED/${name}.txt")
            s1=$(TRAVELER_THREADS=1 "$TMPDIR/${name}_s1" 2>/dev/null)
            s32=$(TRAVELER_THREADS=32 "$TMPDIR/${name}_s1" 2>/dev/null)
            if [ "$s1" != "$baseline" ]; then
                status="FAIL"; detail="serial output != baseline"
            elif [ "$s32" != "$baseline" ]; then
                status="FAIL"; detail="parallel output != baseline (race manifested)"
            fi
        fi
        if [ "$status" = "PASS" ]; then
            printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
        else
            printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
            FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
        fi
    done

    # ------------------------------------------------------------
    #  #54 (bound width adoption): stage1-only —
    #    - pfor_i64_bounds: an i64-bound field loop MUST dispatch through the
    #      i64 ABI (__parallel_for_i64 + (ptr, i64, i64) worker) and stay
    #      bit-deterministic across thread counts
    #    - pfor_bound_trap_u64: a u64 bound >= 2^63 aborts (134) at loop
    #      entry — same status at every thread count (guard precedes dispatch)
    #  The frozen seed refuses wide bounds outright (i32-only space).
    # ------------------------------------------------------------
    TOTAL=$((TOTAL + 1)); name="pfor_i64_bounds"; status="PASS"; detail=""
    if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile (stage1)"
    fi
    if [ "$status" = "PASS" ]; then
        gw=$(grep -c "define internal void @__pfor_worker_0(ptr %p0, i64 %p1, i64 %p2)" "$TMPDIR/${name}.ll" || true)
        [ "$gw" = "1" ] || { status="FAIL"; detail="i64 worker sig missing"; }
    fi
    if [ "$status" = "PASS" ]; then
        gd=$(grep -c "call void @__parallel_for_i64(" "$TMPDIR/${name}.ll" || true)
        [ "$gd" -ge 1 ] || { status="FAIL"; detail="__parallel_for_i64 dispatch missing"; }
    fi
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
    fi
    if [ "$status" = "PASS" ]; then
        baseline=$(cat "$EXPECTED/${name}.txt")
        t1_out=$(TRAVELER_THREADS=1 "$TMPDIR/${name}" 2>/dev/null)
        t32_out=$(TRAVELER_THREADS=32 "$TMPDIR/${name}" 2>/dev/null)
        if [ "$t1_out" != "$baseline" ]; then
            status="FAIL"; detail="serial output != baseline"
        elif [ "$t32_out" != "$t1_out" ]; then
            status="FAIL"; detail="stdout diverged between T1 and T32"
        fi
    fi
    if [ "$status" = "PASS" ]; then
        printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi

    TOTAL=$((TOTAL + 1)); name="pfor_bound_trap_u64"; status="PASS"; detail=""
    if ! "$STAGE1" "$PFOR_DIR/${name}.tv" -o "$TMPDIR/${name}.ll" >/dev/null 2>&1; then
        status="FAIL"; detail="compile (stage1)"
    fi
    if [ "$status" = "PASS" ]; then
        "$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/${name}.ll" -o "$TMPDIR/${name}.o" 2>/dev/null && \
        clang $LINK_PIE "$TMPDIR/${name}.o" -o "$TMPDIR/${name}" 2>/dev/null || { status="FAIL"; detail="link"; }
    fi
    if [ "$status" = "PASS" ]; then
        TRAVELER_THREADS=1 "$TMPDIR/${name}" >/dev/null 2>&1; t1_st=$?
        TRAVELER_THREADS=32 "$TMPDIR/${name}" >/dev/null 2>&1; t32_st=$?
        if [ "$t1_st" != "134" ]; then
            status="FAIL"; detail="T1 status: want 134 (SIGABRT), got $t1_st"
        elif [ "$t32_st" != "134" ]; then
            status="FAIL"; detail="T32 status: want 134 (SIGABRT), got $t32_st"
        fi
    fi
    if [ "$status" = "PASS" ]; then
        printf "  [%2d] %-32s PASS (stage1)\n" "$TOTAL" "$name"; PASS=$((PASS + 1))
    else
        printf "  [%2d] %-32s FAIL (%s)\n" "$TOTAL" "$name" "$detail"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
else
    echo "  U1 (Stage D) tests SKIPPED (stage1 not built)"
fi

echo ""
echo "============================================"
printf "  PFOR: %d PASS, %d FAIL, %d XFAIL, %d XPASS\n" \
    "$PASS" "$FAIL" "$XFAILED" "$XPASS"
if [ -n "$FAILURES" ]; then
    echo "  FAILED:$FAILURES"
fi
echo "============================================"
echo ""

# XPASS is an error: it means a fix landed but the manifest wasn't updated.
exit $((FAIL + XPASS))
