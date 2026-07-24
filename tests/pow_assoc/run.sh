#!/bin/bash
# Regression gate: ** is right-associative (spec 2.10).
#
# Dumps the AST of `a ** b ** c` (tvc_self's default no-`-o` mode prints the
# S-expression, before semantic analysis) and asserts the inner `**` is the
# RIGHT child of the outer `**`, i.e. the tree is a ** (b ** c), not
# (a ** b) ** c.
#
# Only tvc_self is gated here: the C seed has no AST-dump mode (it runs semantic
# analysis immediately and rejects the undeclared a/b/c), but its parser has
# always been right-associative by construction (src-legacy/tvc.c parse_power
# recurses for the RHS, marked "/* right-assoc */"). This gate pins tvc_self to
# that same shape, closing the divergence where tvc_self formerly left-folded.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

SRC="tests/pow_assoc/chain.tv"
SELF="src/bootstrap/out/stage1"

# Right-associative AST, whitespace-normalized to a single line, is:
#   (** (ident a) (** (ident b) (ident c)))
# Left-associative (the former bug) would be:
#   (** (** (ident a) (ident b)) (ident c))
# We detect right-assoc by: the FIRST child after `(**` is a leaf `(ident a)`,
# and the SECOND child is itself a `(**`. Normalize, then pattern-match.
normalize() { tr '\n' ' ' | tr -s ' '; }

RIGHT='(** (ident a) (** (ident b) (ident c)))'
LEFT='(** (** (ident a) (ident b)) (ident c))'

rc=0
check() {
    local name="$1" bin="$2"
    if [ ! -x "$bin" ]; then
        echo "  $name: SKIP (compiler $bin not built)"
        return
    fi
    local ast
    ast="$("$bin" "$SRC" 2>/dev/null | normalize)"
    if printf '%s' "$ast" | grep -qF "$RIGHT"; then
        echo "  $name: PASS (right-associative: a ** (b ** c))"
    elif printf '%s' "$ast" | grep -qF "$LEFT"; then
        echo "  $name: FAIL (left-associative: (a ** b) ** c) — regression!"
        rc=1
    else
        echo "  $name: FAIL (unrecognized AST shape)"
        printf '       got: %s\n' "$ast"
        rc=1
    fi
}

echo "=== ** right-associativity gate (spec 2.10) ==="
check "tvc_self" "$SELF"
exit $rc
