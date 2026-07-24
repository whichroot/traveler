#!/bin/bash
# --pfor-report gate (exact-arithmetic arc, Stage C).
#
# Two things in one runner:
#   1. FUNCTIONAL: the read-only --pfor-report query mode emits valid JSONL, and
#      the curated pfor fixtures (the via-negativa race catalogue + positive
#      controls + a not-field consumer) each report their KNOWN verdict/reason.
#      This turns the race catalogue into a reason-labeled oracle.
#   2. BASELINE: the whole compiling corpus (reused from codegen_diff) is run
#      through the mode and projected to a line/col-stripped, sorted golden of
#      per-loop verdicts. Stage D (U1) LANDED against this instrument: the
#      audited diff flipped exactly the 65 `not-field` records (18 files) to
#      dispatched, nothing else — the baseline now tracks POST-U1 dispatch
#      decisions, with `cap-elem` as the residual type refusal.
#      The golden flips only when a DISPATCH DECISION changes (cosmetic edits
#      that move line numbers do NOT churn it — line/col are projected out),
#      extending the fixed-point discipline from bytes to dispatch decisions.
#
# Usage:
#   run.sh            functional asserts + baseline check (CI/gate mode)
#   run.sh --update   re-bless tests/pfor_report/baseline.jsonl from current
#                     output (after an intended dispatch-decision change)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

SELF="$REPO_DIR/src/bootstrap/out/stage1"
CORPUS="$REPO_DIR/tests/codegen_diff/corpus.txt"
GOLDEN="$SCRIPT_DIR/baseline.jsonl"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [ ! -x "$SELF" ]; then
    echo "FATAL: tvc_self not built at $SELF (run src/bootstrap/build.sh)"; exit 1
fi

MODE="check"
if [ "${1:-}" = "--update" ]; then MODE="update"; fi

echo "=== --pfor-report gate (tests/pfor_report) ==="

# ------------------------------------------------------------------
# Part 1: functional assertions on curated fixtures.
# ------------------------------------------------------------------
fail=0

# JSONL validity: every emitted line must parse as JSON.
lines_valid_json() {
    local out="$1"
    [ -z "$out" ] && return 0
    while IFS= read -r ln; do
        [ -z "$ln" ] && continue
        printf '%s' "$ln" | python3 -m json.tool >/dev/null 2>&1 || return 1
    done <<< "$out"
    return 0
}

# assert_reason FILE REASON : at least one record on FILE has "reason":"REASON".
assert_reason() {
    local file="$1"; local reason="$2"
    local out; out="$("$SELF" "$file" --pfor-report 2>/dev/null)"
    if ! lines_valid_json "$out"; then
        echo "  FAIL invalid JSONL: $file"; fail=1; return
    fi
    if printf '%s\n' "$out" | grep -q "\"reason\":\"$reason\""; then
        echo "  ok   $(basename "$file"): reason=$reason"
    else
        echo "  FAIL $(basename "$file"): expected reason=$reason, got:"; printf '%s\n' "$out" | sed 's/^/       /'
        fail=1
    fi
}

# assert_dispatch FILE : at least one record on FILE is dispatched (a positive control).
assert_dispatch() {
    local file="$1"
    local out; out="$("$SELF" "$file" --pfor-report 2>/dev/null)"
    if ! lines_valid_json "$out"; then
        echo "  FAIL invalid JSONL: $file"; fail=1; return
    fi
    if printf '%s\n' "$out" | grep -q '"dispatched":1'; then
        echo "  ok   $(basename "$file"): dispatched"
    else
        echo "  FAIL $(basename "$file"): expected a dispatched loop, got:"; printf '%s\n' "$out" | sed 's/^/       /'
        fail=1
    fi
}

# The race catalogue, now reason-labeled (via negativa -> named refusals).
assert_reason "$REPO_DIR/tests/pfor/pfor_race_const_idx.tv"     "const-idx"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_invariant_idx.tv" "const-idx"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_noninjective.tv"  "noninjective"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_let_idx.tv"       "let-hidden"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_mutating_call.tv" "mutating-call"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_raw.tv"           "raw"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_global_assign.tv" "mutating-call"
# U1 (Stage D) i64 flavors: the independence proof is type-blind — default-deny
# survived the gate lift with the same named refusals.
assert_reason "$REPO_DIR/tests/pfor/pfor_u1_i64_raw.tv"         "raw"
assert_reason "$REPO_DIR/tests/pfor/pfor_u1_i64_scatter.tv"     "noninjective"
# The post-U1 residual TYPE refusal (the boundary U1 does NOT cross):
# struct-element captures stay refused. `not-field` is retired (unreachable).
assert_reason "$SCRIPT_DIR/fixtures/cap_elem_struct.tv"         "cap-elem"
# Stage D LANDED: the flip-set shape now DISPATCHES (pre-U1: reason=not-field).
assert_dispatch "$SCRIPT_DIR/fixtures/not_field_map.tv"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_u1_i64_map.tv"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_u1_overflow_det.tv"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_u1_trap_parity.tv"
# Positive controls: must still dispatch (no lost parallelism).
assert_dispatch "$REPO_DIR/tests/pfor/pfor_ok_butterfly.tv"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_ok_dyn.tv"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_ok_pure_call.tv"
# #43/#46 (workitem-private mutable): the three new named refusals + the
# positive control (multi-assign to body-local vars dispatches).
assert_reason "$REPO_DIR/tests/pfor/pfor_race_assign_carried.tv" "assign-carried"
assert_reason "$REPO_DIR/tests/pfor/pfor_race_alias_letptr.tv"   "private-base"
assert_reason "$REPO_DIR/tests/pfor/pfor_private_escape.tv"      "private-escape"
assert_dispatch "$REPO_DIR/tests/pfor/pfor_ok_private_var.tv"

# ------------------------------------------------------------------
# Part 2: whole-tree baseline (the audit-rule instrument).
# Project out line/col so the golden tracks DISPATCH DECISIONS, not
# source positions; prefix each record with its source path.
# ------------------------------------------------------------------
: > "$TMP/current.jsonl"
corpus_fail=0
while IFS= read -r line; do
    line="${line%%#*}"; line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    [ -f "$REPO_DIR/$line" ] || { echo "  MISSING SOURCE: $line"; corpus_fail=1; continue; }
    if ! out="$("$SELF" "$REPO_DIR/$line" --pfor-report 2>/dev/null)"; then
        echo "  COMPILE FAILED: $line"; corpus_fail=1; continue
    fi
    [ -z "$out" ] && continue
    printf '%s\n' "$out" \
        | sed -E 's/"line":[0-9]+,"col":[0-9]+,//' \
        | sed "s|^|$line\t|" >> "$TMP/current.jsonl"
done < "$CORPUS"
sort "$TMP/current.jsonl" > "$TMP/current.sorted"

# Headline: the reason tally + the post-U1 residual (cap-elem) count.
echo "  --- verdict tally (whole corpus) ---"
grep -o '"reason":"[^"]*"' "$TMP/current.sorted" | sort | uniq -c | sed 's/^/    /'
nf="$(grep -c '"reason":"cap-elem"' "$TMP/current.sorted" 2>/dev/null || true)"
disp="$(grep -c '"dispatched":1' "$TMP/current.sorted" 2>/dev/null || true)"
echo "    (cap-elem = post-U1 residual type refusals: $nf; dispatched: $disp)"

if [ "$MODE" = "update" ]; then
    cp "$TMP/current.sorted" "$GOLDEN"
    echo "  === baseline re-blessed ($(wc -l < "$GOLDEN" | tr -d ' ') records) -> $GOLDEN"
    echo "  Review the diff and commit tests/pfor_report/baseline.jsonl."
    [ "$fail" = "0" ] && [ "$corpus_fail" = "0" ] && exit 0 || exit 1
fi

if [ ! -f "$GOLDEN" ]; then
    echo "  NO BASELINE: run 'tests/pfor_report/run.sh --update' to create it."; exit 1
fi

if diff -q "$GOLDEN" "$TMP/current.sorted" >/dev/null; then
    echo "  BASELINE: PASS ($(wc -l < "$TMP/current.sorted" | tr -d ' ') records, dispatch-stable)"
else
    echo "  BASELINE: DRIFT — dispatch decisions changed:"
    diff "$GOLDEN" "$TMP/current.sorted" | sed 's/^/    /' | head -60
    echo "  If INTENTIONAL (e.g. a gate change): tests/pfor_report/run.sh --update, then commit baseline.jsonl."
    fail=1
fi

if [ "$corpus_fail" = "1" ]; then
    echo "  FAIL: corpus had missing/uncompilable sources"; fail=1
fi

if [ "$fail" = "0" ]; then
    echo "  PFOR-REPORT: PASS"
    exit 0
else
    echo "  PFOR-REPORT: FAIL"
    exit 1
fi
