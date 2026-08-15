#!/usr/bin/env bash
# LSP engine gate (roadmap B3b) — machine-readable diagnostics mode.
#
# `tvc_self --diagnostics FILE` emits one JSON-Lines record per diagnostic to
# STDOUT ({"severity","line","col","endLine","endCol","message"}) instead of
# the human caret form, so an editor/LSP front-end can consume A2 spans
# directly. This suite pins that contract:
#   - valid programs emit ZERO diagnostic records (clean stdout) + exit 0
#   - broken programs emit well-formed JSONL with correct line/col/endCol
#   - stdout is parseable as JSON (validated with python3 -m json.tool per line)
#
# Note on exit code: parse-stage diagnostics let the run complete and exit 0;
# a few deep codegen-stage error sites still exit(1) after emitting their JSON
# record (single-error abort). The LSP front-end consumes stdout records
# regardless of exit code, so this gate asserts records + JSON validity, not a
# uniform exit code for broken inputs. Valid inputs must still exit 0.
#
# This is the engine half of B3b; the TypeScript LSP server (tools/tv-lsp)
# consumes exactly this stream.
#
# Usage: ./run_lsp.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src-legacy"
EXAMPLES="$REPO_DIR/examples"
DIAG_DIR="$SCRIPT_DIR/diag"

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

# Shared environment probe (tests/lib/env.sh): LINKER (link driver), LINK_PIE
# re-derived honoring TRAVELER_LINK_FLAGS, plus capability flags.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/env.sh"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# --- Build the canonical compiler (Stage 1: bootstrap -> tvc_self) ---
(cd "$SRC_DIR" && make tvc >/dev/null 2>&1) || {
    (cd "$SRC_DIR" && "$LINKER" -O2 -Wall -Wextra -std=c99 -o tvc tvc.c) || exit 1
}
"$SRC_DIR/tvc" "$REPO_DIR/src/tvc_self.tv" -o "$TMPDIR/tvc_self.ll" 2>/dev/null || {
    echo "FATAL: Stage 1 compile failed" >&2; exit 1
}
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR/tvc_self.ll" -o "$TMPDIR/tvc_self.o" 2>/dev/null || {
    echo "FATAL: Stage 1 llc failed" >&2; exit 1
}
"$LINKER" $LINK_PIE "$TMPDIR/tvc_self.o" -o "$TMPDIR/tvc_self" 2>/dev/null || {
    echo "FATAL: Stage 1 link failed" >&2; exit 1
}
TVC_SELF="$TMPDIR/tvc_self"

PASS=0
FAIL=0
TOTAL=0
FAILURES=""

check() {
    local name="$1"
    local result="$2"   # 1 = pass, 0 = fail
    TOTAL=$((TOTAL + 1))
    if [ "$result" -eq 1 ]; then
        printf "  [%2d] %-40s PASS\n" "$TOTAL" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [%2d] %-40s FAIL\n" "$TOTAL" "$name"
        FAIL=$((FAIL + 1)); FAILURES="$FAILURES $name"
    fi
}

# Validate that every non-empty line of $1 is parseable JSON.
all_lines_json() {
    local file="$1"
    [ -s "$file" ] || return 0   # empty is fine (no diagnostics)
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        printf '%s' "$line" | python3 -m json.tool >/dev/null 2>&1 || return 1
    done < "$file"
    return 0
}

echo ""
echo "=== LSP engine gate (--diagnostics, vs tvc_self) ==="
echo ""

# --- 1. Valid programs emit zero records + exit 0 ---
for tv in field_basics edge_cases poly_classify; do
    out="$TMPDIR/${tv}.json"
    "$TVC_SELF" "$EXAMPLES/${tv}.tv" --diagnostics >"$out" 2>/dev/null
    rc=$?
    if [ "$rc" -eq 0 ] && [ ! -s "$out" ]; then
        check "valid:$tv (no records, exit 0)" 1
    else
        check "valid:$tv (no records, exit 0)" 0
        echo "        rc=$rc, out:" >&2; cat "$out" >&2
    fi
done

# --- 2. Broken programs emit well-formed JSONL ---
for tv in "$DIAG_DIR"/*.tv; do
    [ -f "$tv" ] || continue
    name="$(basename "$tv" .tv)"
    out="$TMPDIR/diag_${name}.json"
    "$TVC_SELF" "$tv" --diagnostics >"$out" 2>/dev/null
    rc=$?
    ok=1
    # Must NOT crash (segfault/abort).
    if [ "$rc" -eq 139 ] || [ "$rc" -eq 134 ] || [ "$rc" -eq 138 ]; then ok=0; fi
    # Must emit at least one record.
    [ -s "$out" ] || ok=0
    # Every line must be valid JSON.
    all_lines_json "$out" || ok=0
    check "broken:$name (valid JSONL)" "$ok"
    if [ "$ok" -eq 0 ]; then echo "        rc=$rc, out:" >&2; cat "$out" >&2; fi
done

# --- 3. Field-content spot checks (line/col/endCol correctness) ---
# missing_semicolon: error on line 2 (the `let x: i32 = 5` with no semicolon,
# reported at the next token `return` on line 3).
out="$TMPDIR/spot_missing.json"
"$TVC_SELF" "$DIAG_DIR/missing_semicolon.tv" --diagnostics >"$out" 2>/dev/null
if grep -q '"line":3' "$out" && grep -q '"severity":"error"' "$out" \
   && grep -q '"endCol":' "$out"; then
    check "spot:missing_semicolon fields" 1
else
    check "spot:missing_semicolon fields" 0
    cat "$out" >&2
fi

# undefined_var: endCol must exceed col (multi-char identifier span).
out="$TMPDIR/spot_undef.json"
"$TVC_SELF" "$DIAG_DIR/undefined_var.tv" --diagnostics >"$out" 2>/dev/null
col=$(grep -o '"col":[0-9]*' "$out" | head -1 | grep -o '[0-9]*')
endcol=$(grep -o '"endCol":[0-9]*' "$out" | head -1 | grep -o '[0-9]*')
if [ -n "$col" ] && [ -n "$endcol" ] && [ "$endcol" -gt "$col" ]; then
    check "spot:undefined_var endCol>col ($col<$endcol)" 1
else
    check "spot:undefined_var endCol>col" 0
    cat "$out" >&2
fi

# multi_error: must emit multiple records (many-per-run preserved in JSON mode).
out="$TMPDIR/spot_multi.json"
"$TVC_SELF" "$DIAG_DIR/multi_error.tv" --diagnostics >"$out" 2>/dev/null
nrec=$(grep -c '"severity"' "$out")
if [ "$nrec" -ge 2 ]; then
    check "spot:multi_error multiple records ($nrec)" 1
else
    check "spot:multi_error multiple records" 0
    cat "$out" >&2
fi

# Escaping: message with embedded quotes (identifier lexemes) stays valid JSON,
# already covered by all_lines_json on undefined_var/multi_error.

echo ""
echo "--- symbol index (--symbols) ---"

# --- 4. Symbol index: valid JSONL with the expected names + signatures ---
out="$TMPDIR/sym_polycore.json"
"$TVC_SELF" "$REPO_DIR/src/lib/core/poly_core.tv" --symbols >"$out" 2>/dev/null
rc=$?
ok=1
[ "$rc" -eq 0 ] || ok=0
all_lines_json "$out" || ok=0
# Expect the kernel's four exported fns to appear.
grep -q '"name":"forward_sum"' "$out" || ok=0
grep -q '"name":"forward_diff"' "$out" || ok=0
grep -q '"kind":"fn"' "$out" || ok=0
# Signature reconstruction must include params + return type.
grep -q '"signature":"fn mod_add(a: i32, b: i32, p: i32) -> i32"' "$out" || ok=0
check "symbols:poly_core (names + signatures)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# struct + field + fn kinds all present.
out="$TMPDIR/sym_struct.json"
"$TVC_SELF" "$EXAMPLES/struct_basics.tv" --symbols >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
grep -q '"kind":"struct"' "$out" || ok=0
grep -q '"kind":"field"' "$out" || ok=0
grep -q '"kind":"fn"' "$out" || ok=0
check "symbols:struct_basics (struct/field/fn kinds)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# enum + trait kinds.
out="$TMPDIR/sym_enum.json"
"$TVC_SELF" "$EXAMPLES/enum_basics.tv" --symbols >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
grep -q '"kind":"enum"' "$out" || ok=0
check "symbols:enum_basics (enum kind)" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# Symbol position points at the decl name (endCol > col).
out="$TMPDIR/sym_pos.json"
"$TVC_SELF" "$EXAMPLES/field_basics.tv" --symbols >"$out" 2>/dev/null
scol=$(grep '"name":"main"' "$out" | grep -o '"col":[0-9]*' | head -1 | grep -o '[0-9]*')
secol=$(grep '"name":"main"' "$out" | grep -o '"endCol":[0-9]*' | head -1 | grep -o '[0-9]*')
if [ -n "$scol" ] && [ -n "$secol" ] && [ "$secol" -gt "$scol" ]; then
    check "symbols:field_basics main span ($scol<$secol)" 1
else
    check "symbols:field_basics main span" 0
    cat "$out" >&2
fi

echo ""
echo "--- reference table (--references, scope-aware) ---"

# --- 5. Scope-aware shadowing (the load-bearing property) ---
# nav_scope.tv: inner x (line 19 col 9) shadows outer x (line 12 col 5).
out="$TMPDIR/ref_scope.json"
"$TVC_SELF" "$SCRIPT_DIR/lsp/nav_scope.tv" --references >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
# line 20 `acc = acc + x`: x at col 21 -> inner def line 19 (NOT line 12).
grep -q '"refLine":20,"refCol":21,[^}]*"defLine":19,"defCol":9,"kind":"local"' "$out" || ok=0
# line 25 helper(x,y): x at col 25 -> OUTER def line 12 (inner out of scope).
grep -q '"refLine":25,"refCol":25,[^}]*"defLine":12,"defCol":5,"kind":"local"' "$out" || ok=0
# helper call -> fn decl line 7.
grep -q '"refLine":25,[^}]*"defLine":7,"defCol":1,"kind":"fn"' "$out" || ok=0
check "references:nav_scope shadowing + fn + scope-exit" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 6. Receiver-typed member access ---
# struct_basics.tv: p.x (line 23) -> field def line 14; q.x (line 36) -> line 14.
# (line numbers are +2 vs the raw decls: the file carries a 2-line SPDX/Author header.)
out="$TMPDIR/ref_member.json"
"$TVC_SELF" "$EXAMPLES/struct_basics.tv" --references >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
grep -q '"refLine":23,[^}]*"defLine":14,"defCol":5,"kind":"field"' "$out" || ok=0
grep -q '"kind":"struct"' "$out" || ok=0
check "references:struct_basics member + struct type" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 7. Receiver-typed trait dispatch (collision-free) ---
# trait_dispatch.tv: r.area() -> Rect impl (line 35), c.area() -> Circle (line 40).
# (line numbers are +2 vs the raw decls: the file carries a 2-line SPDX/Author header.)
out="$TMPDIR/ref_trait.json"
"$TVC_SELF" "$EXAMPLES/trait_dispatch.tv" --references >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
grep -q '"refLine":55,[^}]*"defLine":35,"defCol":5,"kind":"method"' "$out" || ok=0
grep -q '"refLine":56,[^}]*"defLine":40,"defCol":5,"kind":"method"' "$out" || ok=0
check "references:trait_dispatch type-directed methods" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 8. Enum type-name resolution ---
out="$TMPDIR/ref_enum.json"
"$TVC_SELF" "$EXAMPLES/enum_basics.tv" --references >"$out" 2>/dev/null
ok=1
all_lines_json "$out" || ok=0
grep -q '"kind":"enum"' "$out" || ok=0
check "references:enum_basics enum type name" "$ok"
[ "$ok" -eq 0 ] && cat "$out" >&2

# --- 9. TypeScript LSP server (transport) end-to-end ---
# The server (tools/tv-lsp) is the thin JSON-RPC/stdio layer over this engine.
# Its test suite drives a full initialize/didOpen/hover/definition cycle. Run
# it here when node is available (skipped cleanly otherwise — the engine half
# above is the parity-critical part; the TS half is host-side).
LSP_DIR="$REPO_DIR/tools/tv-lsp"
if command -v node &>/dev/null && [ -f "$LSP_DIR/test/engine.test.js" ]; then
    if TVC_SELF="$TVC_SELF" node "$LSP_DIR/test/engine.test.js" >"$TMPDIR/tslsp.out" 2>&1; then
        nts=$(grep -oE '[0-9]+ passed' "$TMPDIR/tslsp.out" | grep -oE '[0-9]+' | head -1)
        check "tv-lsp server: JSON-RPC cycle (${nts:-?} node tests)" 1
    else
        check "tv-lsp server: JSON-RPC cycle" 0
        cat "$TMPDIR/tslsp.out" >&2
    fi
else
    echo "  [--] tv-lsp server                          SKIP (node unavailable)"
fi

echo ""
echo "============================================"
printf "  LSP: %d PASS, %d FAIL  (of %d)\n" "$PASS" "$FAIL" "$TOTAL"
if [ -n "$FAILURES" ]; then echo "  FAILED:$FAILURES"; fi
echo "============================================"
echo ""

[ "$FAIL" -eq 0 ]
