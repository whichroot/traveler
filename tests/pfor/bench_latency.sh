#!/usr/bin/env bash
# bench_latency.sh — pfor dispatch-latency report (no pass/fail gate).
#
# Compiles tests/pfor/pfor_bench_dispatch.tv with the canonical compiler
# (stage1) and runs it at the default thread count. Prints ns per dispatch
# for the first dispatch (pool init post-fix) and the steady-state loop.
# Timing is machine-dependent: this is a report harness for commit-message
# numbers and headroom work, not a CI gate.
#
# Usage: tests/pfor/bench_latency.sh [threads]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
STAGE1="${TVC_SELF:-$REPO_DIR/src/bootstrap/out/stage1}"

[ -x "$STAGE1" ] || { echo "FATAL: stage1 not built ($STAGE1)" >&2; exit 1; }

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

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  LLC_TARGET="-mtriple=x86_64-linux-gnu";  LINK_PIE="-no-pie" ;;
    Linux-aarch64) LLC_TARGET="-mtriple=aarch64-linux-gnu"; LINK_PIE="-no-pie" ;;
    *)             LLC_TARGET="";                           LINK_PIE="" ;;
esac

. "$REPO_DIR/tests/lib/env.sh"

TMPDIR_B=$(mktemp -d)
trap "rm -rf $TMPDIR_B" EXIT

"$STAGE1" "$SCRIPT_DIR/pfor_bench_dispatch.tv" -o "$TMPDIR_B/bench.ll" || {
    echo "FATAL: compile failed" >&2; exit 1; }
"$LLC" $LLC_TARGET -filetype=obj "$TMPDIR_B/bench.ll" -o "$TMPDIR_B/bench.o" && \
"$LINKER" $LINK_PIE "$TMPDIR_B/bench.o" -o "$TMPDIR_B/bench" || {
    echo "FATAL: llc/link failed" >&2; exit 1; }

THREADS_ARG="${1:-}"
if [ -n "$THREADS_ARG" ]; then
    export TRAVELER_THREADS="$THREADS_ARG"
fi

out=$("$TMPDIR_B/bench")
checksum=$(echo "$out" | sed -n 1p)
t_first=$(echo "$out" | sed -n 2p)
t_total=$(echo "$out" | sed -n 3p)
t_per=$(echo "$out" | sed -n 4p)

printf "pfor dispatch latency (TRAVELER_THREADS=%s, ncpu=%s)\n" \
    "${TRAVELER_THREADS:-auto}" "$(sysctl -n hw.ncpu 2>/dev/null || echo '?')"
printf "  checksum:            %s\n" "$checksum"
printf "  first dispatch:      %s ns (pool init post-fix)\n" "$t_first"
printf "  steady state:        %s ns total / 20000 dispatches\n" "$t_total"
printf "  per dispatch:        %s ns\n" "$t_per"
