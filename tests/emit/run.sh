#!/usr/bin/env bash
# --emit driver gate: raw/optimized IR and one-command native publication.
# @internal-design: CPU_MIDDLE_END_DESIGN
#
# Proves raw IR identity, the closed none/promote/o1 profiles, verified LLVM,
# argv-safe posix_spawn tool execution, atomic publication, failure cleanup, and
# host retargeting. On Linux the driver adds -mtriple=<host> and -no-pie because
# the module preamble pins the canonical Darwin triple and Traveler is non-PIC.
#
# Usage: ./run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_DIR" || exit 1

# --- Locate llc (same discovery as run.sh; honors $LLC) ---
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

# Optimized profiles are initially supported on LLVM 21. The raw/none and
# requested-missing-opt legs still run when an LLVM 21 opt is unavailable.
OPT_OVERRIDE="${OPT:-}"
find_opt() {
    if [ -n "$OPT_OVERRIDE" ]; then
        if command -v "$OPT_OVERRIDE" &>/dev/null; then OPT="$OPT_OVERRIDE"; return; fi
        return 1
    fi
    local sibling="${LLC%llc}opt"
    for p in \
        "$sibling" \
        /opt/homebrew/opt/llvm@21/bin/opt \
        /usr/local/opt/llvm@21/bin/opt \
        /usr/lib/llvm-21/bin/opt \
        opt-21 \
        opt; do
        if command -v "$p" &>/dev/null; then OPT="$p"; return; fi
    done
    return 1
}
OPT_OK=0
if find_opt && "$OPT" --version 2>/dev/null | grep -q 'version 21\.'; then
    OPT_OK=1
fi
# Resolve to an absolute path: the -opt override leg symlinks "$OPT" into a
# spaces-in-path directory, and a bare PATH-discovered name makes a dangling
# relative symlink there.
if [ "$OPT_OK" = "1" ]; then OPT="$(command -v "$OPT")"; fi
if [ -n "$OPT_OVERRIDE" ] && [ "$OPT_OK" = "0" ]; then
    echo "FATAL: OPT must name LLVM 21 opt (got: $OPT_OVERRIDE)" >&2
    exit 1
fi

# The internal --emit driver now host-retargets (#58): on Linux the object is
# host-native and links. Linux links still need -no-pie for the manual link in
# step 3 (the driver adds it itself for --emit exe).
LINK_PIE=""
if [ "100 1 26 57 100 303uname -s)" = "Linux" ]; then LINK_PIE="-no-pie"; fi

# Shared environment probe (tests/lib/env.sh): LINKER (link driver), LINK_PIE
# re-derived honoring TRAVELER_LINK_FLAGS, plus capability flags.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/env.sh"

STAGE1="$REPO_DIR/src/bootstrap/out/stage1"
if [ ! -x "$STAGE1" ]; then
    LLC="$LLC" "$REPO_DIR/src/bootstrap/build.sh" >/dev/null 2>&1 || true
fi
if [ ! -x "$STAGE1" ]; then
    echo "  FATAL: tvc_self not built at $STAGE1 (run src/bootstrap/build.sh)"; exit 1
fi

echo "=== --emit driver gate (tests/emit) ==="

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SRC="$REPO_DIR/examples/field_basics.tv"
WANT="$(cat "$REPO_DIR/tests/expected/field_basics.txt")"
PROMOTE_SRC="$SCRIPT_DIR/promote_local.tv"
fail=0

file_mode() {
    if [ "$(uname -s)" = "Darwin" ]; then stat -f '%Lp' "$1"; else stat -c '%a' "$1"; fi
}

# 1. --emit exe: one call, source -> runnable binary, correct output.
if "$STAGE1" "$SRC" -o "$TMP/fb" --emit exe -llc "$LLC" -cc "$LINKER" 2>"$TMP/exe.err"; then
    got="$("$TMP/fb" 2>/dev/null)"
    got_status=$?
    if [ "$got_status" = "0" ] && [ "$got" = "$WANT" ]; then
        echo "  ok   --emit exe: one-shot binary runs, output matches"
    else
        echo "  FAIL: --emit exe output mismatch"; echo "    want: $WANT"; echo "    got:  $got"; fail=1
    fi
else
    echo "  FAIL: --emit exe did not produce a binary:"; sed 's/^/       /' "$TMP/exe.err"; fail=1
fi

# 2. Intermediates are cleaned up (no derived .tvctmp.* left behind).
if compgen -G "$TMP/fb.tvctmp.*" >/dev/null; then
    echo "  FAIL: --emit exe left intermediates behind"; fail=1
else
    echo "  ok   intermediates cleaned"
fi

# 3. --emit obj: stops at a nonempty object that links + runs.
if "$STAGE1" "$SRC" -o "$TMP/fb.o" --emit obj -llc "$LLC" 2>"$TMP/obj.err"; then
    if [ -s "$TMP/fb.o" ] && "$LINKER" $LINK_PIE "$TMP/fb.o" -o "$TMP/fb_obj" 2>/dev/null && \
       "$TMP/fb_obj" >"$TMP/fb_obj.out" 2>/dev/null && \
       [ "$(cat "$TMP/fb_obj.out")" = "$WANT" ]; then
        echo "  ok   --emit obj: object links and runs"
    else
        echo "  FAIL: --emit obj object did not link/run"; fail=1
    fi
else
    echo "  FAIL: --emit obj did not produce an object:"; sed 's/^/       /' "$TMP/obj.err"; fail=1
fi

# 4. Default IR and explicit none are byte-identical. none never invokes opt,
# even when -opt names a missing tool.
if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/raw.ll" 2>/dev/null && \
   "$STAGE1" "$PROMOTE_SRC" -o "$TMP/none.ll" --opt-level none \
       -opt "$TMP/does-not-exist" 2>/dev/null && \
   cmp -s "$TMP/raw.ll" "$TMP/none.ll" && \
   grep -q "Traveler compiler output" "$TMP/raw.ll"; then
    echo "  ok   omitted/none: raw IR byte-identical, no opt dependency"
else
    echo "  FAIL: default and --opt-level none IR differ"; fail=1
fi
printf '' > "$TMP/mode-control"
if [ "$(file_mode "$TMP/raw.ll")" = "$(file_mode "$TMP/mode-control")" ] && \
   [ "$(file_mode "$TMP/fb.o")" = "$(file_mode "$TMP/mode-control")" ]; then
    echo "  ok   published IR/object modes respect the caller's umask"
else
    echo "  FAIL: staged permissions leaked into published artifacts"; fail=1
fi

# 5. Invalid profiles fail before publishing an output.
if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/invalid.ll" --opt-level fast \
    2>"$TMP/invalid.err"; then
    echo "  FAIL: invalid optimization profile was accepted"; fail=1
elif grep -q '^error: --opt-level expects none|promote|o1$' "$TMP/invalid.err" && \
     [ ! -e "$TMP/invalid.ll" ]; then
    echo "  ok   invalid profile: stable refusal, no output"
else
    echo "  FAIL: invalid profile diagnostic/artifact mismatch"; fail=1
fi

# 6. A requested missing optimizer is loud, preserves the old destination, and
# removes every staged artifact.
printf 'keep-opt\n' > "$TMP/protected.ll"
ln -s "$TMP/protected.ll" "$TMP/protected.ll.tvctmp.raw.ll"
missing_opt_ok=1
if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/protected.ll" --opt-level promote \
    -opt "$TMP/missing-opt" 2>"$TMP/missing-opt.err" || \
   [ "$(cat "$TMP/protected.ll")" != "keep-opt" ] || \
   [ ! -L "$TMP/protected.ll.tvctmp.raw.ll" ] || \
   ! grep -q '^error: opt failed$' "$TMP/missing-opt.err"; then
    missing_opt_ok=0
fi
rm -f "$TMP/protected.ll.tvctmp.raw.ll"
if [ "$missing_opt_ok" = "1" ] && \
   ! compgen -G "$TMP/protected.ll.tvctmp.*" >/dev/null; then
    echo "  ok   missing opt: destination preserved, temporaries removed"
else
    echo "  FAIL: missing opt publication/cleanup mismatch"; fail=1
fi

# Direct codegen exits also run the registered stage cleanup and preserve an
# existing destination.
printf 'keep-codegen\n' > "$TMP/reject.ll"
if "$STAGE1" "$REPO_DIR/tests/dynfield/closures/reject_return.tv" \
    -o "$TMP/reject.ll" 2>"$TMP/reject.err" || \
   [ "$(cat "$TMP/reject.ll")" != "keep-codegen" ] || \
   compgen -G "$TMP/reject.ll.tvctmp.*" >/dev/null; then
    echo "  FAIL: direct codegen refusal publication/cleanup mismatch"; fail=1
else
    echo "  ok   direct codegen refusal preserves destination and removes stage"
fi

# A short/failed write to the anonymous codegen stream is a compilation error,
# never a successfully published truncated module.
printf 'keep-write-failure\n' > "$TMP/write-limit.ll"
if (trap '' XFSZ; ulimit -f 1; "$STAGE1" "$PROMOTE_SRC" \
        -o "$TMP/write-limit.ll") 2>"$TMP/write-limit.err" || \
   [ "$(cat "$TMP/write-limit.ll")" != "keep-write-failure" ] || \
   ! grep -q '^error: cannot write output file$' "$TMP/write-limit.err" || \
   compgen -G "$TMP/write-limit.ll.tvctmp.*" >/dev/null; then
    echo "  FAIL: short IR write was published or left a stage"; fail=1
else
    echo "  ok   short IR write fails without publishing a truncated module"
fi

# 7. CPU profiles never silently apply to device modes or the typed-pointer
# compatibility target. Explicit none remains byte-identical on both paths.
profile_boundaries_ok=1
gpu_n=0
for gpu_mode in --emit-gpu --emit-gpu-nvptx --emit-gpu-agx; do
    if "$STAGE1" "$gpu_mode" "$SRC" -o "$TMP/gpu-$gpu_n.out" \
        --opt-level promote 2>"$TMP/gpu-$gpu_n.err" || \
       [ -e "$TMP/gpu-$gpu_n.out" ] || \
       ! grep -q '^error: CPU optimization profiles do not apply to GPU-only emission$' \
        "$TMP/gpu-$gpu_n.err"; then
        profile_boundaries_ok=0
    fi
    gpu_n=$((gpu_n + 1))
done
if "$STAGE1" "$SRC" -o "$TMP/tpc.ll" -target tpc --opt-level o1 \
    2>"$TMP/tpc-opt.err" || [ -e "$TMP/tpc.ll" ] || \
   ! grep -q '^error: -target tpc accepts only --opt-level none$' "$TMP/tpc-opt.err"; then
    profile_boundaries_ok=0
fi
if ! "$STAGE1" "$PROMOTE_SRC" -o "$TMP/tpc-omitted.ll" -target tpc 2>/dev/null || \
   ! "$STAGE1" "$PROMOTE_SRC" -o "$TMP/tpc-none.ll" -target tpc \
        --opt-level none 2>/dev/null || \
   ! cmp -s "$TMP/tpc-omitted.ll" "$TMP/tpc-none.ll" || \
   ! "$STAGE1" --emit-gpu "$SRC" -o "$TMP/gpu-omitted.ll" 2>/dev/null || \
   ! "$STAGE1" --emit-gpu "$SRC" -o "$TMP/gpu-none.ll" \
        --opt-level none 2>/dev/null || \
   ! cmp -s "$TMP/gpu-omitted.ll" "$TMP/gpu-none.ll"; then
    profile_boundaries_ok=0
fi
if [ "$profile_boundaries_ok" = "1" ]; then
    echo "  ok   GPU/tpc profile boundaries: optimized refuse, none byte-identical"
else
    echo "  FAIL: GPU/tpc profile boundary mismatch"; fail=1
fi

if [ "$OPT_OK" = "1" ]; then
    # 8. Both optimized profiles produce independently verifiable LLVM;
    # promote removes the fixture main's source-local alloca.
    opt_ir_ok=1
    for profile in promote o1; do
        if ! "$STAGE1" "$PROMOTE_SRC" -o "$TMP/$profile.ll" \
            --opt-level "$profile" -opt "$OPT" 2>"$TMP/$profile.err" || \
           ! "$OPT" -passes=verify -disable-output "$TMP/$profile.ll" \
            2>"$TMP/$profile.verify.err"; then
            opt_ir_ok=0
        fi
    done
    if ! "$STAGE1" "$PROMOTE_SRC" -o "$TMP/promote-repeat.ll" \
        --opt-level promote -opt "$OPT" 2>"$TMP/promote-repeat.err" || \
       ! cmp -s "$TMP/promote.ll" "$TMP/promote-repeat.ll"; then
        opt_ir_ok=0
    fi
    if [ "$opt_ir_ok" = "1" ] && \
       grep -A12 '^define i32 @main' "$TMP/raw.ll" | grep -q 'alloca i32' && \
       ! grep -A12 '^define i32 @main' "$TMP/promote.ll" | grep -q 'alloca i32'; then
        echo "  ok   promote/o1: verified/reproducible; mem2reg removes stack traffic"
    else
        echo "  FAIL: optimized IR verification/promotion mismatch"; fail=1
    fi

    # 9. Object and executable modes work under both requested profiles.
    native_profiles_ok=1
    for profile in promote o1; do
        if ! "$STAGE1" "$SRC" -o "$TMP/$profile.o" --emit obj \
            --opt-level "$profile" -opt "$OPT" -llc "$LLC" \
            2>"$TMP/$profile-obj.err" || \
           ! "$LINKER" $LINK_PIE "$TMP/$profile.o" -o "$TMP/$profile-obj" 2>/dev/null || \
           ! "$TMP/$profile-obj" >"$TMP/$profile-obj.out" 2>/dev/null || \
           [ "$(cat "$TMP/$profile-obj.out")" != "$WANT" ] || \
           ! "$STAGE1" "$SRC" -o "$TMP/$profile-exe" --emit exe \
            --opt-level "$profile" -opt "$OPT" -llc "$LLC" -cc "$LINKER" \
            2>"$TMP/$profile-exe.err" || \
           ! "$TMP/$profile-exe" >"$TMP/$profile-exe.out" 2>/dev/null || \
           [ "$(cat "$TMP/$profile-exe.out")" != "$WANT" ]; then
            native_profiles_ok=0
        fi
    done
    if [ "$native_profiles_ok" = "1" ]; then
        echo "  ok   promote/o1: object and executable outputs are exact"
    else
        echo "  FAIL: optimized native artifact/output mismatch"; fail=1
    fi

    # 10. Address-taken storage, aggregates, defer/early-return cleanup, and
    # native wide integers retain their existing outputs under both profiles.
    semantic_ok=1
    for profile in promote o1; do
        for case in address_of struct_basics defer_cleanup wide_i256_add; do
            if ! "$STAGE1" "$REPO_DIR/examples/$case.tv" -o "$TMP/$case-$profile" \
                --emit exe --opt-level "$profile" -opt "$OPT" -llc "$LLC" -cc "$LINKER" \
                2>"$TMP/$case-$profile.err" || \
               ! "$TMP/$case-$profile" >"$TMP/$case-$profile.out" 2>/dev/null || \
               [ "$(cat "$TMP/$case-$profile.out")" != \
                 "$(cat "$REPO_DIR/tests/expected/$case.txt")" ]; then
                semantic_ok=0
            fi
        done
    done
    if [ "$semantic_ok" = "1" ]; then
        echo "  ok   optimized semantic corpus: address/aggregate/defer/wide exact"
    else
        echo "  FAIL: optimized semantic corpus mismatch"; fail=1
    fi

    # 11. Serial/parallel stdout and exit status agree within every profile,
    # including the platform-split integer divide-by-zero outcome.
    pfor_ok=1
    base_status=-1
    for profile in none promote o1; do
        if ! "$STAGE1" "$REPO_DIR/tests/pfor/pfor_u1_trap_parity.tv" \
            -o "$TMP/pfor-$profile" --emit exe --opt-level "$profile" \
            -opt "$OPT" -llc "$LLC" -cc "$LINKER" 2>"$TMP/pfor-$profile.err"; then
            pfor_ok=0
            continue
        fi
        TRAVELER_THREADS=1 "$TMP/pfor-$profile" >"$TMP/pfor-$profile-t1.out" 2>&1
        s1=$?
        TRAVELER_THREADS=4 "$TMP/pfor-$profile" >"$TMP/pfor-$profile-t4.out" 2>&1
        s4=$?
        if [ "$s1" != "$s4" ] || \
           ! cmp -s "$TMP/pfor-$profile-t1.out" "$TMP/pfor-$profile-t4.out"; then
            pfor_ok=0
        fi
        if [ "$profile" = "none" ]; then
            base_status=$s1
            cp "$TMP/pfor-$profile-t1.out" "$TMP/pfor-base.out"
        elif [ "$s1" != "$base_status" ] || \
             ! cmp -s "$TMP/pfor-$profile-t1.out" "$TMP/pfor-base.out"; then
            pfor_ok=0
        fi
    done
    if [ "$pfor_ok" = "1" ]; then
        echo "  ok   none/promote/o1: pfor stdout and trap status parity"
    else
        echo "  FAIL: optimized pfor output/trap parity mismatch"; fail=1
    fi

    # 12. Tool and output paths are argv elements, not shell text. The wrapper
    # also proves the spawned child inherits the compiler's environment and that
    # llc receives the explicit backend level.
    TOOL_DIR="$TMP/tool paths"
    OUT_DIR="$TMP/output paths"
    mkdir -p "$TOOL_DIR" "$OUT_DIR"
    LLC_WRAP="$TOOL_DIR/llc wrapper"
    cat > "$LLC_WRAP" <<'EOF'
#!/bin/sh
: "${EMIT_REAL_LLC:?}"
: "${EMIT_LLC_LOG:?}"
printf '%s\n' "$@" > "$EMIT_LLC_LOG"
exec "$EMIT_REAL_LLC" "$@"
EOF
    chmod +x "$LLC_WRAP"
    EMIT_REAL_LLC="$LLC" EMIT_LLC_LOG="$TMP/llc.argv" \
        "$STAGE1" "$SRC" -o "$OUT_DIR/field object.o" --emit obj \
        --opt-level promote -opt "$OPT" -llc "$LLC_WRAP" \
        2>"$TMP/argv.err"
    argv_status=$?
    if [ "$argv_status" = "0" ] && [ -s "$OUT_DIR/field object.o" ] && \
       grep -Fxq -- '-O2' "$TMP/llc.argv" && \
       grep -Fq -- "$OUT_DIR/field object.o.tvctmp.o." "$TMP/llc.argv"; then
        echo "  ok   posix_spawn: spaces safe, environment inherited, llc -O2 explicit"
    else
        echo "  FAIL: argv/environment/backend-level contract mismatch"; fail=1
    fi

    # -opt overrides PATH discovery and itself supports spaces.
    OPT_LINK="$TOOL_DIR/opt override"
    ln -s "$OPT" "$OPT_LINK"
    if PATH=/nonexistent "$STAGE1" "$PROMOTE_SRC" -o "$OUT_DIR/optimized ir.ll" \
        --opt-level o1 -opt "$OPT_LINK" 2>"$TMP/opt-override.err" && \
       "$OPT" -passes=verify -disable-output "$OUT_DIR/optimized ir.ll" 2>/dev/null; then
        echo "  ok   -opt override: PATH-independent and space-safe"
    else
        echo "  FAIL: -opt override did not control tool discovery"; fail=1
    fi

    # Explicit Darwin, Linux, and non-Linux targets retain their module target
    # through the middle end and lower to objects. Implicit host behavior is
    # exercised by every native test above (including Linux CI retargeting).
    target_matrix_ok=1
    target_n=0
    for target in arm64-apple-darwin x86_64-linux-gnu x86_64-unknown-freebsd; do
        if ! "$STAGE1" "$PROMOTE_SRC" -o "$TMP/target-$target_n.o" --emit obj \
            --opt-level promote -opt "$OPT" -llc "$LLC" -target "$target" \
            2>"$TMP/target-$target_n.err" || \
           [ ! -s "$TMP/target-$target_n.o" ]; then
            target_matrix_ok=0
        fi
        target_n=$((target_n + 1))
    done
    if [ "$target_matrix_ok" = "1" ]; then
        echo "  ok   optimized target matrix: Darwin, Linux, and non-Linux objects"
    else
        echo "  FAIL: optimized explicit-target matrix mismatch"; fail=1
    fi

    # 13. Every tool failure may leave a partial staging file, but the driver
    # removes it and preserves an existing destination.
    FAIL_TOOL="$TOOL_DIR/failing tool"
    cat > "$FAIL_TOOL" <<'EOF'
#!/bin/sh
out=
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ] && [ "$#" -gt 1 ]; then
        shift
        out=$1
    fi
    shift
done
if [ -n "$out" ]; then printf 'partial\n' > "$out"; fi
exit 9
EOF
    chmod +x "$FAIL_TOOL"
    failures_ok=1

    printf 'keep-opt-failure\n' > "$TMP/fail-opt.ll"
    if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/fail-opt.ll" --opt-level promote \
        -opt "$FAIL_TOOL" 2>"$TMP/fail-opt.err" || \
       [ "$(cat "$TMP/fail-opt.ll")" != "keep-opt-failure" ] || \
       ! grep -q '^error: opt failed$' "$TMP/fail-opt.err" || \
       compgen -G "$TMP/fail-opt.ll.tvctmp.*" >/dev/null; then
        failures_ok=0
    fi

    printf 'keep-llc-failure\n' > "$TMP/fail-llc.o"
    if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/fail-llc.o" --emit obj \
        -llc "$FAIL_TOOL" 2>"$TMP/fail-llc.err" || \
       [ "$(cat "$TMP/fail-llc.o")" != "keep-llc-failure" ] || \
       ! grep -q '^error: llc failed$' "$TMP/fail-llc.err" || \
       compgen -G "$TMP/fail-llc.o.tvctmp.*" >/dev/null; then
        failures_ok=0
    fi

    printf 'keep-cc-failure\n' > "$TMP/fail-cc"
    if "$STAGE1" "$PROMOTE_SRC" -o "$TMP/fail-cc" --emit exe \
        -llc "$LLC" -cc "$FAIL_TOOL" 2>"$TMP/fail-cc.err" || \
       [ "$(cat "$TMP/fail-cc")" != "keep-cc-failure" ] || \
       ! grep -q '^error: cc failed$' "$TMP/fail-cc.err" || \
       compgen -G "$TMP/fail-cc.tvctmp.*" >/dev/null; then
        failures_ok=0
    fi

    if [ "$failures_ok" = "1" ]; then
        echo "  ok   opt/llc/cc failures preserve destinations and remove temporaries"
    else
        echo "  FAIL: tool failure publication/cleanup mismatch"; fail=1
    fi

    # 14. AGX runtime selection emits an ordinary host module, so its CPU
    # fallback may use a profile even though standalone device modes may not.
    agx_host_ok=1
    if ! "$STAGE1" "$REPO_DIR/tests/gpu/agx_dispatch_gate.tv" \
        -o "$TMP/agx-host.ll" --agx-dispatch --opt-level promote -opt "$OPT" \
        2>"$TMP/agx-host.err" || \
       ! "$OPT" -passes=verify -disable-output "$TMP/agx-host.ll" 2>/dev/null; then
        agx_host_ok=0
    fi
    if [ "$(uname -s)" = "Darwin" ]; then
        if ! "$STAGE1" "$REPO_DIR/tests/gpu/agx_dispatch_gate.tv" \
            -o "$TMP/agx-host.o" --emit obj --agx-dispatch \
            --opt-level o1 -opt "$OPT" -llc "$LLC" 2>"$TMP/agx-host-obj.err" || \
           ! "$LINKER" "$TMP/agx-host.o" -framework IOKit -o "$TMP/agx-host" 2>/dev/null; then
            agx_host_ok=0
        else
            TRAVELER_THREADS=1 "$TMP/agx-host" >"$TMP/agx-host-t1.out" 2>/dev/null
            agx_s1=$?
            TRAVELER_THREADS=4 "$TMP/agx-host" >"$TMP/agx-host-t4.out" 2>/dev/null
            agx_s4=$?
            if [ "$agx_s1" != "0" ] || [ "$agx_s4" != "0" ] || \
               ! cmp -s "$TMP/agx-host-t1.out" "$TMP/agx-host-t4.out"; then
                agx_host_ok=0
            fi
        fi
    fi
    if [ "$agx_host_ok" = "1" ]; then
        echo "  ok   --agx-dispatch optimized host and CPU fallback remain exact"
    else
        echo "  FAIL: --agx-dispatch optimized host/fallback mismatch"; fail=1
    fi
else
    echo "  SKIP optimized-profile success legs (LLVM 21 opt not found)"
fi

echo ""
if [ "$fail" = "0" ]; then
    echo "  EMIT: PASS"
    exit 0
else
    echo "  EMIT: FAIL"
    exit 1
fi
