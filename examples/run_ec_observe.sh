#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../tests/lib/env.sh"
SELF="${TVC_SELF:-$TV_REPO_DIR/src/bootstrap/out/stage1}"
if [ ! -x "$SELF" ] || [ "$HAVE_LLC" != 1 ] || [ "$HAVE_LINKER" != 1 ]; then
    printf '%s\n' 'Build stage1 and provide llc plus a link driver before this experiment.' >&2
    exit 1
fi
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for name in hankel_rank_test ec_subgroup_observe ec_structure_probe ec_divisor_probe ec_descent_probe; do
    "$SELF" "$SCRIPT_DIR/$name.tv" --emit exe -llc "$LLC" -cc "$LINKER" -o "$WORK/$name"
    "$WORK/$name"
done

if [ "$HAVE_CUDA" != 1 ] || [ "$LLC_HAS_NVPTX" != 1 ]; then
    printf '%s\n' 'EC CUDA: SKIP (CUDA driver/device or NVPTX backend unavailable)'
    exit 0
fi

# sm_90 PTX is a portable JIT input for Hopper and later, including Blackwell.
"$SELF" --emit-gpu-nvptx "$SCRIPT_DIR/ec_observe_cuda.tv" -o "$WORK/device.ll"
"$LLC" -march=nvptx64 -mcpu="${EC_CUDA_ARCH:-sm_90}" "$WORK/device.ll" -o "$WORK/device.ptx"
"$SELF" "$SCRIPT_DIR/ec_observe_cuda.tv" --emit obj -llc "$LLC" -o "$WORK/host.o"
"$LINKER" $LINK_PIE -pthread "$WORK/host.o" "$CUDA_LIB" \
    -Wl,-rpath,"$(dirname "$CUDA_LIB")" -o "$WORK/ec_observe_cuda"
"$WORK/ec_observe_cuda" "$WORK/device.ptx"
