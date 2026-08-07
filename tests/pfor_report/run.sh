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
#      per-loop verdicts. U1, the primitive-capture dispatch lift
#      (@internal-note: plan-exact-arithmetic-arc, Stage D), LANDED against
#      this instrument: the
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

SELF="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"
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
assert_reason "$REPO_DIR/tests/pfor/pfor_self_member_call.tv"   "mutating-call"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_hidden_read.tv"   "call-read"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_aggregate_alias_call.tv" "mutating-call"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_affine_wrap.tv"  "index-wrap"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_affine_circle.tv" "noninjective"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_affine_slack.tv" "noninjective"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_affine_point_wrap.tv" "raw"
assert_reason "$REPO_DIR/tests/pfor/pfor_self_shadowed_read.tv" "call-read"
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

# PROOF0 is a separate query schema: scope-aware effects and declaration-aware
# affine hazards beside the unchanged legacy verdict. It has no admission path.
PROOF0_FIXTURE="$SCRIPT_DIR/fixtures/proof0_effects.tv"
if ! "$SELF" "$PROOF0_FIXTURE" --pfor-proof0-report \
        >"$TMP/proof0.focused.jsonl" 2>/dev/null \
   || ! python3 "$SCRIPT_DIR/proof0_probe.py" \
        "$TMP/proof0.focused.jsonl"; then
    echo "  FAIL proof0 focused recursive-effect summary"; fail=1
fi
PROOF0_ADVERSARIAL="$SCRIPT_DIR/fixtures/proof0_adversarial.tv"
if ! "$SELF" "$PROOF0_ADVERSARIAL" --pfor-proof0-report \
        >"$TMP/proof0.adversarial.jsonl" 2>/dev/null \
   || ! python3 "$SCRIPT_DIR/proof0_probe.py" --adversarial \
        "$TMP/proof0.adversarial.jsonl"; then
    echo "  FAIL proof0 adversarial false-safe summary"; fail=1
fi
PROOF0_AFFINE="$SCRIPT_DIR/fixtures/proof0_affine.tv"
if ! "$SELF" "$PROOF0_AFFINE" --pfor-proof0-report \
        >"$TMP/proof0.affine.jsonl" 2>/dev/null \
   || ! python3 "$SCRIPT_DIR/proof0_probe.py" --affine \
        "$TMP/proof0.affine.jsonl"; then
    echo "  FAIL proof0 focused affine summary"; fail=1
fi
legacy_focus="$("$SELF" "$PROOF0_FIXTURE" --pfor-report 2>/dev/null)"
if ! lines_valid_json "$legacy_focus" \
   || printf '%s\n' "$legacy_focus" | grep -q 'cpu_effects'; then
    echo "  FAIL proof0 changed the legacy --pfor-report schema"; fail=1
else
    echo "  ok   PROOF0 remains separate from the legacy report schema"
fi

assert_proof0_standalone() {
    local label="$1"; shift
    if "$SELF" "$PROOF0_FIXTURE" --pfor-proof0-report "$@" \
            >"$TMP/proof0.$label.out" 2>"$TMP/proof0.$label.err"; then
        echo "  FAIL proof0 accepted mixed mode: $label"; fail=1; return
    fi
    if ! grep -qx 'error: --pfor-proof0-report is a standalone query mode' \
            "$TMP/proof0.$label.err"; then
        echo "  FAIL proof0 mixed-mode diagnostic: $label"; fail=1; return
    fi
    echo "  ok   PROOF0 rejects mixed mode: $label"
}

assert_proof0_standalone legacy --pfor-report
assert_proof0_standalone eval --eval
assert_proof0_standalone diagnostics --diagnostics
assert_proof0_standalone output -o "$TMP/proof0.ll"
assert_proof0_standalone emit-ir --emit ir
assert_proof0_standalone gpu --emit-gpu-agx -o "$TMP/proof0.agx"
assert_proof0_standalone target -target x86_64-unknown-linux-gnu
assert_proof0_standalone arguments program-argument

# Parse diagnostics gate malformed trees before the recursive walker.
if ! "$SELF" "$REPO_DIR/tests/diag/expected_expression.tv" \
        --pfor-proof0-report >"$TMP/proof0.malformed.out" \
        2>"$TMP/proof0.malformed.err" \
   || [ -s "$TMP/proof0.malformed.out" ] \
   || ! grep -q 'expected an expression' "$TMP/proof0.malformed.err"; then
    echo "  FAIL proof0 malformed-AST guard"; fail=1
else
    echo "  ok   PROOF0 keeps malformed ASTs out of the walker"
fi
if ! "$SELF" "$REPO_DIR/tests/diag/defer_nested.tv" \
        --pfor-proof0-report >"$TMP/proof0.defer.out" \
        2>"$TMP/proof0.defer.err" \
   || [ -s "$TMP/proof0.defer.out" ] \
   || ! grep -q 'defer must be a top-level statement' "$TMP/proof0.defer.err"; then
    echo "  FAIL proof0 nested-defer parser boundary"; fail=1
else
    echo "  ok   PROOF0 keeps nested defer outside the walker"
fi

# ------------------------------------------------------------------
# Part 2: whole-tree baseline (the audit-rule instrument).
# Project out line/col so the golden tracks DISPATCH DECISIONS, not
# source positions; prefix each record with its source path.
# ------------------------------------------------------------------
: > "$TMP/current.jsonl"
: > "$TMP/proof0.jsonl"
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
    if ! proof_out="$("$SELF" "$REPO_DIR/$line" \
            --pfor-proof0-report 2>/dev/null)"; then
        echo "  PROOF0 COMPILE FAILED: $line"; corpus_fail=1
    elif [ -n "$proof_out" ]; then
        printf '%s\n' "$proof_out" \
            | sed "s|^{|{\"source\":\"$line\",|" >> "$TMP/proof0.jsonl"
    fi
done < "$CORPUS"
sort "$TMP/current.jsonl" > "$TMP/current.sorted"

if ! python3 "$SCRIPT_DIR/proof0_probe.py" --corpus "$TMP/proof0.jsonl"; then
    echo "  FAIL: PROOF0 whole-corpus summary drifted"; fail=1
fi

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
