#!/usr/bin/env bash
# Module size gate.
#
# Discipline (the tinygrad-mirror inversion): src/tvc_self.tv is the ONE file
# allowed to be large — it is the deliberate self-hosting monolith, kept whole
# to protect the byte-identical fixed point. EVERY OTHER library module is
# bounded. The honest framing: the cap is a proxy for "legible per seam", not a
# golf target. A file over the cap is a prompt to split (see the codec, split
# into import modules), not to delete newlines.
#
# Enforces:
#   1. every src/lib/**/*.tv is <= CAP lines.
#   2. src/tvc_self.tv is the SOLE exemption — and no new file may join it.
#
# Exit 0 if clean, 1 on any violation. No deps beyond coreutils.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

CAP=1500

# The allow-list of large files. tvc_self.tv is the only legitimate entry.
# Adding to this list is a deliberate, reviewable act — the gate exists so a
# new monolith cannot land silently.
EXEMPT=(
  "src/tvc_self.tv"
)

is_exempt() {
  local rel="$1"
  for e in "${EXEMPT[@]}"; do
    [ "$rel" = "$e" ] && return 0
  done
  return 1
}

echo "=== Module size gate (cap ${CAP} lines; sole exemption: src/tvc_self.tv) ==="

violations=0
checked=0

# Library modules: the bounded surface.
while IFS= read -r f; do
  rel="${f#"$REPO_DIR"/}"
  lines=$(wc -l < "$f" | tr -d ' ')
  checked=$((checked + 1))
  if [ "$lines" -gt "$CAP" ]; then
    if is_exempt "$rel"; then
      printf "  [exempt] %-45s %s\n" "$rel" "$lines"
    else
      printf "  OVER CAP %-45s %s  (> %s)\n" "$rel" "$lines" "$CAP"
      violations=$((violations + 1))
    fi
  fi
done < <(find "$REPO_DIR/src/lib" -name '*.tv' | sort)

# Guard the exemption list itself: every exempt path must (a) exist and
# (b) actually be over the cap (a stale exemption is dead weight to remove).
for e in "${EXEMPT[@]}"; do
  f="$REPO_DIR/$e"
  if [ ! -f "$f" ]; then
    printf "  STALE EXEMPTION (missing file): %s\n" "$e"
    violations=$((violations + 1))
    continue
  fi
  lines=$(wc -l < "$f" | tr -d ' ')
  if [ "$lines" -le "$CAP" ]; then
    printf "  STALE EXEMPTION (now under cap, drop it): %-30s %s\n" "$e" "$lines"
    violations=$((violations + 1))
  fi
done

echo "============================================"
if [ "$violations" -eq 0 ]; then
  echo "  SIZE GATE: PASS ($checked lib files within cap; 1 exemption)"
  echo "============================================"
  exit 0
else
  echo "  SIZE GATE: FAIL ($violations violation(s))"
  echo "  Fix: split the file into import modules (Model A), or — only if truly"
  echo "  irreducible — add it to EXEMPT in tests/run_sizegate.sh with rationale."
  echo "============================================"
  exit 1
fi
