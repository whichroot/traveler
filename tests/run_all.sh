#!/usr/bin/env bash
# tests/run_all.sh — environment-aware test dispatcher.
#
# Probes the local toolchain once (tests/lib/env.sh), prints the capability
# matrix, then runs the suites this environment can actually exercise:
#
#   - no llc / no link driver:  run.sh degrades (IR-only, link/run SKIP) and
#     the AGX byte goldens run via tests/gpu/run.sh --goldens-only.
#   - llc + one link driver:    the full gate once (run_dual.sh when the C
#     seed is available, else run.sh).
#   - llc + several drivers:    the full gate on the primary driver, then the
#     full regression suite once per ADDITIONAL driver with the tool-neutral
#     sub-gates skipped (they are linker-independent).
#
# Usage:
#   run_all.sh              probe + run everything the environment supports
#   run_all.sh --list       print the capability matrix and suite plan only
#   run_all.sh --suite=NAME run a single suite (run, dual, gpu, goldens,
#                           pfor, dynfield, diag, fmt, lsp, doc, sizegate,
#                           foldbug, pow_assoc, codegen_diff, pfor_report,
#                           typedptr, emit, eval_diff, repl, alloc_debug,
#                           bootstrap)
#
# Overrides: LLC, OPT, LINKER, LINK, CC, LLVM14, TRAVELER_LINK_FLAGS,
# TRAVELER_AGX_PROFILE (see tests/lib/env.sh).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR" || exit 1

. "$SCRIPT_DIR/lib/env.sh"

LIST_ONLY=0
ONLY_SUITE=""
for arg in "$@"; do
    case "$arg" in
        --list)        LIST_ONLY=1 ;;
        --suite=*)     ONLY_SUITE="${arg#--suite=}" ;;
        -h|--help)     sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

echo "=== Traveler test dispatcher ==="
tv_env_summary

# --- Single-suite escape hatch --------------------------------------------
suite_path() {
    case "$1" in
        run)          echo "tests/run.sh" ;;
        dual)         echo "tests/run_dual.sh" ;;
        gpu)          echo "tests/gpu/run.sh" ;;
        goldens)      echo "tests/gpu/run.sh --goldens-only" ;;
        pfor)         echo "tests/run_pfor.sh" ;;
        dynfield)     echo "tests/dynfield/run.sh" ;;
        diag)         echo "tests/run_diag.sh" ;;
        fmt)          echo "tests/run_fmt.sh" ;;
        lsp)          echo "tests/run_lsp.sh" ;;
        doc)          echo "tests/run_doc.sh" ;;
        sizegate)     echo "tests/run_sizegate.sh" ;;
        foldbug)      echo "tests/foldbug/run.sh" ;;
        pow_assoc)    echo "tests/pow_assoc/run.sh" ;;
        codegen_diff) echo "tests/codegen_diff/run.sh" ;;
        pfor_report)  echo "tests/pfor_report/run.sh" ;;
        typedptr)     echo "tests/typedptr/run.sh" ;;
        emit)         echo "tests/emit/run.sh" ;;
        eval_diff)    echo "tests/eval_diff/run.sh" ;;
        repl)         echo "tests/repl/run.sh" ;;
        alloc_debug)  echo "tests/alloc_debug/run.sh" ;;
        bootstrap)    echo "tests/run_bootstrap.sh" ;;
        *)            echo "" ;;
    esac
}

if [ -n "$ONLY_SUITE" ]; then
    _sp="$(suite_path "$ONLY_SUITE")"
    if [ -z "$_sp" ]; then
        echo "unknown suite: $ONLY_SUITE (see --help)" >&2; exit 2
    fi
    echo ""
    echo "=== suite: $ONLY_SUITE ($_sp) ==="
    bash $_sp
    exit $?
fi

# --- Suite plan -------------------------------------------------------------
# stage1 is the floor; build it when the toolchain allows.
if [ "$HAVE_STAGE1" != "1" ]; then
    if [ "$HAVE_LLC" = "1" ] && [ "$HAVE_LINKER" = "1" ]; then
        echo ""
        echo "stage1 missing — building it (src/bootstrap/build.sh)..."
        LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
        [ -x "$REPO_DIR/src/bootstrap/out/stage1" ] && HAVE_STAGE1=1
    fi
fi

echo ""
echo "Plan:"
if [ "$HAVE_STAGE1" != "1" ]; then
    echo "  - sizegate (coreutils only; stage1 unavailable and unbuildable here)"
elif [ "$HAVE_LLC" != "1" ] || [ "$HAVE_LINKER" != "1" ]; then
    echo "  - run.sh        (degraded: IR-only, link/run stages SKIP)"
    echo "  - gpu goldens   (tests/gpu/run.sh --goldens-only)"
else
    if [ "$HAVE_SEED" = "1" ]; then
        echo "  - run_dual.sh   (full gate incl. dual parity; linker: ${LINKERS%% *})"
    else
        echo "  - run.sh        (full; linker: ${LINKERS%% *}; no C seed — parity gate off)"
    fi
    _rest="${LINKERS#* }"
    if [ "$_rest" != "$LINKERS" ]; then
        for _d in $_rest; do
            echo "  - run.sh        (regression pass; linker: $_d; tool-neutral sub-gates off)"
        done
    fi
fi

if [ "$LIST_ONLY" = "1" ]; then exit 0; fi

# --- Execute ----------------------------------------------------------------
RESULTS=""   # space-separated name:status pairs (names carry no spaces)

run_one() { # name command...
    local name="$1"; shift
    echo ""
    echo "=== $name ==="
    "$@"
    local rc=$?
    if [ "$rc" = "0" ]; then
        RESULTS="$RESULTS $name:PASS"
    else
        RESULTS="$RESULTS $name:FAIL($rc)"
    fi
}

if [ "$HAVE_STAGE1" != "1" ]; then
    run_one sizegate bash tests/run_sizegate.sh
elif [ "$HAVE_LLC" != "1" ] || [ "$HAVE_LINKER" != "1" ]; then
    run_one "run.sh(degraded)" bash tests/run.sh
    run_one "gpu-goldens" bash tests/gpu/run.sh --goldens-only
else
    _first=1
    for _d in $LINKERS; do
        if [ "$_first" = "1" ]; then
            if [ "$HAVE_SEED" = "1" ]; then
                LINKER="$_d" run_one "run_dual[$_d]" bash tests/run_dual.sh
            else
                LINKER="$_d" run_one "run[$_d]" bash tests/run.sh
            fi
            _first=0
        else
            LINKER="$_d" TRAVELER_SKIP_TOOL_NEUTRAL=1 \
                run_one "run[$_d]" bash tests/run.sh
        fi
    done
fi

# --- Summary -----------------------------------------------------------------
echo ""
echo "============================================"
echo "  SUITE SUMMARY"
_any_fail=0
for _r in $RESULTS; do
    printf "    %-24s %s\n" "${_r%%:*}" "${_r##*:}"
    case "$_r" in *":FAIL("*) _any_fail=1 ;; esac
done
echo "============================================"
exit "$_any_fail"
