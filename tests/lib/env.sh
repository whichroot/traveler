# tests/lib/env.sh — shared environment probe for the Traveler test gates.
#
# Source this file; it sets capability flags describing the local toolchain
# and platform. It never fails and never exits: a missing tool is a 0 flag,
# not an error. Suites decide what their requirements buy them.
#
# Overrides honored: LLC, OPT, LINKER, CC, LLVM14, TRAVELER_LINK_FLAGS,
# TRAVELER_AGX_PROFILE.
#
# Provides:
#   TV_REPO_DIR                  absolute repo root
#   TV_UNAME_S / TV_UNAME_M      uname -s / -m
#   TV_HOST_TRIPLE               canonical host triple ("" if unknown)
#   LINK_PIE                     link flags ("-no-pie" on Linux; TRAVELER_LINK_FLAGS wins)
#   TIMEOUT_CMD                  gtimeout / timeout / ""
#   LLC, OPT                     tool paths ("" when absent)
#   HAVE_LLC                     1/0
#   LLC_HAS_AMDGCN / LLC_HAS_NVPTX   1/0 (parsed from llc --version)
#   LINKERS                      space-separated deduped link drivers found
#   LINKER                       primary link driver ($LINKER override wins)
#   HAVE_LINKER                  1/0
#   HAVE_LLVM14 / LLVM14_BINDIR  LLVM-14-era llvm-as+llc pair (typedptr gate)
#   HAVE_STAGE1                  1/0 (src/bootstrap/out/stage1 executable)
#   HAVE_SEED                    1/0 (src-legacy/tvc built, or a C compiler exists)
#   HAVE_WAYLAND                 1/0 (live compositor socket)
#   HAVE_AGX                     1/0 (Darwin arm64 + readable TRAVELER_AGX_PROFILE)
#   HAVE_GLSLANG / GLSLANG_VALIDATOR  Vulkan GLSL-to-SPIR-V assembler
#   HAVE_VULKAN                  1/0 (loader metadata + render node)
#   HAVE_HIP                     1/0 (HIP runtime tools)
#   tv_env_summary               prints the capability matrix

TV_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --- Platform -------------------------------------------------------------
TV_UNAME_S="$(uname -s)"
TV_UNAME_M="$(uname -m)"
case "${TV_UNAME_S}-${TV_UNAME_M}" in
    Linux-x86_64)   TV_HOST_TRIPLE="x86_64-linux-gnu" ;;
    Linux-aarch64)  TV_HOST_TRIPLE="aarch64-linux-gnu" ;;
    Darwin-arm64)   TV_HOST_TRIPLE="arm64-apple-darwin" ;;
    Darwin-x86_64)  TV_HOST_TRIPLE="x86_64-apple-darwin" ;;
    *)              TV_HOST_TRIPLE="" ;;
esac

# Link flags: Linux drivers default to PIE output; traveler objects are
# non-PIE. TRAVELER_LINK_FLAGS overrides the guess wholesale.
if [ -n "${TRAVELER_LINK_FLAGS:-}" ]; then
    LINK_PIE="$TRAVELER_LINK_FLAGS"
else
    case "$TV_UNAME_S" in
        Linux) LINK_PIE="-no-pie" ;;
        *)     LINK_PIE="" ;;
    esac
fi

# --- timeout --------------------------------------------------------------
if command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="gtimeout"
elif command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="timeout"
else
    TIMEOUT_CMD=""
fi

# --- llc / opt ------------------------------------------------------------
LLC="${LLC:-}"
if [ -z "$LLC" ] || ! command -v "$LLC" >/dev/null 2>&1; then
    LLC=""
    for p in \
        /opt/homebrew/opt/llvm@21/bin/llc \
        /usr/local/opt/llvm@21/bin/llc \
        /usr/lib/llvm-21/bin/llc \
        llc-21 \
        llc; do
        if command -v "$p" >/dev/null 2>&1; then LLC="$p"; break; fi
    done
fi
if [ -n "$LLC" ]; then HAVE_LLC=1; else HAVE_LLC=0; fi

OPT="${OPT:-}"
if [ -z "$OPT" ] || ! command -v "$OPT" >/dev/null 2>&1; then
    OPT=""
    if [ -n "$LLC" ]; then
        _opt_next_to_llc="${LLC%llc}opt"
        if [ "$_opt_next_to_llc" != "$LLC" ] && command -v "$_opt_next_to_llc" >/dev/null 2>&1; then
            OPT="$_opt_next_to_llc"
        fi
    fi
    if [ -z "$OPT" ]; then
        for p in \
            /opt/homebrew/opt/llvm@21/bin/opt \
            /usr/local/opt/llvm@21/bin/opt \
            /usr/lib/llvm-21/bin/opt \
            opt-21 \
            opt; do
            if command -v "$p" >/dev/null 2>&1; then OPT="$p"; break; fi
        done
    fi
fi

# Device targets built into this llc (missing target = SKIP, not failure).
LLC_HAS_AMDGCN=0
LLC_HAS_NVPTX=0
if [ "$HAVE_LLC" = "1" ]; then
    _llc_targets="$("$LLC" --version 2>/dev/null || true)"
    case "$_llc_targets" in *amdgcn*)  LLC_HAS_AMDGCN=1 ;; esac
    case "$_llc_targets" in *nvptx64*) LLC_HAS_NVPTX=1 ;; esac
    unset _llc_targets
fi

# --- Link drivers ---------------------------------------------------------
# A link driver only links traveler-emitted objects (cc/clang/gcc all work).
# LINKERS lists every driver found; LINKER is the primary (override wins).
LINKERS=""
_seen=" "
for _c in ${LINKER:-} ${LINK:-} ${CC:-} cc clang gcc; do
    [ -n "$_c" ] || continue
    command -v "$_c" >/dev/null 2>&1 || continue
    case "$_seen" in *" $_c "*) continue ;; esac
    _seen="$_seen$_c "
    LINKERS="${LINKERS:+$LINKERS }$_c"
done
unset _seen _c
if [ -n "$LINKERS" ]; then
    HAVE_LINKER=1
    LINKER="${LINKER:-${LINKERS%% *}}"
else
    HAVE_LINKER=0
    LINKER=""
fi

# --- LLVM-14-era pair (typed-pointer gate) --------------------------------
LLVM14_BINDIR=""
for p in \
    "${LLVM14:-}/bin" \
    /opt/homebrew/opt/llvm@14/bin \
    /usr/local/opt/llvm@14/bin \
    /usr/lib/llvm-14/bin; do
    if [ -x "$p/llvm-as" ] && [ -x "$p/llc" ]; then LLVM14_BINDIR="$p"; break; fi
done
if [ -n "$LLVM14_BINDIR" ]; then HAVE_LLVM14=1; else HAVE_LLVM14=0; fi

# --- Compilers under test --------------------------------------------------
if [ -x "$TV_REPO_DIR/src/bootstrap/out/stage1" ]; then
    HAVE_STAGE1=1
else
    HAVE_STAGE1=0
fi

# The frozen C seed: already built, or buildable with any link driver
# (a driver is a C compiler front-end for these purposes).
if [ -x "$TV_REPO_DIR/src-legacy/tvc" ] || [ "$HAVE_LINKER" = "1" ]; then
    HAVE_SEED=1
else
    HAVE_SEED=0
fi

# --- Display / hardware ----------------------------------------------------
if command -v python3 >/dev/null 2>&1; then HAVE_PYTHON3=1; else HAVE_PYTHON3=0; fi

HAVE_WAYLAND=0
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    _xdg="${XDG_RUNTIME_DIR:-/run/user/$(id -u 2>/dev/null || echo 0)}"
    if [ -S "$_xdg/$WAYLAND_DISPLAY" ]; then HAVE_WAYLAND=1; fi
    unset _xdg
fi

HAVE_AGX=0
if [ "$TV_UNAME_S" = "Darwin" ] && [ "$TV_UNAME_M" = "arm64" ] \
   && [ -n "${TRAVELER_AGX_PROFILE:-}" ] && [ -r "${TRAVELER_AGX_PROFILE:-}" ]; then
    HAVE_AGX=1
fi

GLSLANG_VALIDATOR="${GLSLANG_VALIDATOR:-}"
if [ -z "$GLSLANG_VALIDATOR" ] || ! command -v "$GLSLANG_VALIDATOR" >/dev/null 2>&1; then
    GLSLANG_VALIDATOR=""
    if command -v glslangValidator >/dev/null 2>&1; then
        GLSLANG_VALIDATOR="glslangValidator"
    fi
fi
if [ -n "$GLSLANG_VALIDATOR" ]; then HAVE_GLSLANG=1; else HAVE_GLSLANG=0; fi

HAVE_VULKAN=0
if [ "$TV_UNAME_S" = "Linux" ] && command -v pkg-config >/dev/null 2>&1 \
   && pkg-config --exists vulkan 2>/dev/null; then
    for _render in /dev/dri/renderD*; do
        if [ -r "$_render" ]; then HAVE_VULKAN=1; break; fi
    done
    unset _render
fi

if command -v hipconfig >/dev/null 2>&1; then HAVE_HIP=1; else HAVE_HIP=0; fi

# --- Summary ---------------------------------------------------------------
tv_env_summary() {
    cat <<EOF
  platform:    ${TV_UNAME_S}-${TV_UNAME_M} (triple: ${TV_HOST_TRIPLE:-unknown})
  stage1:      $( [ "$HAVE_STAGE1" = "1" ] && echo yes || echo no )
  llc:         ${LLC:-none}$( [ "$LLC_HAS_AMDGCN" = "1" ] && echo " [+amdgcn]" )$( [ "$LLC_HAS_NVPTX" = "1" ] && echo " [+nvptx]" )
  opt:         ${OPT:-none}
  linkers:     ${LINKERS:-none}
  link flags:  ${LINK_PIE:-none}
  llvm14:      ${LLVM14_BINDIR:-none}
  C seed:      $( [ "$HAVE_SEED" = "1" ] && echo yes || echo no )
  wayland:     $( [ "$HAVE_WAYLAND" = "1" ] && echo "live ($WAYLAND_DISPLAY)" || echo no )
  agx profile: $( [ "$HAVE_AGX" = "1" ] && echo yes || echo no )
  glslang:     ${GLSLANG_VALIDATOR:-none}
  vulkan:      $( [ "$HAVE_VULKAN" = "1" ] && echo yes || echo no )
  hip runtime: $( [ "$HAVE_HIP" = "1" ] && echo yes || echo no )
  python3:     $( [ "$HAVE_PYTHON3" = "1" ] && echo yes || echo no )
  timeout:     ${TIMEOUT_CMD:-none}
EOF
}
