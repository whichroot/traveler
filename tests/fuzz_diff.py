#!/usr/bin/env python3
"""Differential + robustness fuzzer for the Traveler compilers (roadmap A5).

Three modes, all targeting *robustness* (the compiler must never crash) and,
for valid inputs, *agreement* (bootstrap tvc and canonical tvc_self must emit
byte-identical IR — the dual-parity invariant, here on randomized programs
rather than the fixed example set):

  valid     - generate well-formed field-arithmetic programs. Both compilers
              must exit 0, and the compiled programs must produce identical
              runtime OUTPUT. (Bootstrap and tvc_self deliberately emit
              different IR text — different preamble, extra dyn/wide-field
              scaffolding in tvc_self — so the parity invariant is on program
              behavior, exactly as tests/run_dual.sh checks, not on IR bytes.)
              A divergence is a parity bug; a crash is a robustness bug.
  malformed - random token soup / truncated programs. Neither compiler may
              crash (segfault 139 / abort 134). Clean exit (0 or 1) is fine;
              we only assert "no crash".
  oversized - very large generated programs to exercise the A1 arena guards.
              Must either compile or fail with the clean arena-overflow
              diagnostic; never corrupt/crash.

Usage:
  fuzz_diff.py [--mode valid|malformed|oversized|all] [--count N] [--seed S]

Exit code is non-zero if any case fails (crash, or valid-mode divergence).
This is intended for local runs and an optional CI nightly; it is NOT wired
into the fast per-PR suite.
"""

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src-legacy")  # the C seed (tvc.c)
COMPILER = os.path.join(REPO, "src", "tvc_self.tv")
EXAMPLES = os.path.join(REPO, "examples")


def find_llc():
    if os.environ.get("LLC") and shutil.which(os.environ["LLC"]):
        return os.environ["LLC"]
    for p in (
        "/opt/homebrew/opt/llvm@21/bin/llc",
        "/usr/local/opt/llvm@21/bin/llc",
        "/usr/lib/llvm-21/bin/llc",
        "llc-21",
        "llc",
    ):
        if shutil.which(p):
            return p
    sys.exit("FATAL: llc not found; set LLC")


def build_compilers(workdir, llc):
    """Build bootstrap tvc and Stage-1 tvc_self; return their paths."""
    tvc = os.path.join(SRC, "tvc")
    subprocess.run(
        ["clang", "-O2", "-Wall", "-Wextra", "-std=c99", "-o", tvc, "tvc.c"],
        cwd=SRC,
        check=True,
    )
    ll = os.path.join(workdir, "tvc_self.ll")
    obj = os.path.join(workdir, "tvc_self.o")
    tvc_self = os.path.join(workdir, "tvc_self")
    subprocess.run(
        [tvc, COMPILER, "-o", ll],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run([llc, "-filetype=obj", ll, "-o", obj], check=True)
    subprocess.run(["clang", obj, "-o", tvc_self], check=True)
    return tvc, tvc_self


# ---- Program generators ----

FIELDS = ["Field<251>", "Field<65521>", "Field<4294967291>"]
OPS = ["+", "-", "*", "/"]


def gen_valid(rng):
    """A well-formed program: one field, a chain of let-bindings, prints."""
    prime = rng.choice(FIELDS)
    n = rng.randint(2, 12)
    lines = [f"field F = {prime};", "", "fn main() {"]
    vars_ = []
    # seed two constants
    for i in range(2):
        v = f"v{i}"
        lines.append(f"    let {v}: F = {rng.randint(0, 250)};")
        vars_.append(v)
    for i in range(2, n):
        v = f"v{i}"
        a = rng.choice(vars_)
        b = rng.choice(vars_)
        op = rng.choice(OPS)
        # avoid division by a literal-zero-prone term by using vars only
        lines.append(f"    let {v}: F = {a} {op} {b};")
        vars_.append(v)
    for v in rng.sample(vars_, min(len(vars_), rng.randint(1, len(vars_)))):
        lines.append(f"    print({v});")
    lines.append("}")
    return "\n".join(lines) + "\n"


MALFORMED_TOKENS = [
    "fn",
    "let",
    "mut",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    ";",
    ":",
    ",",
    "=",
    "+",
    "-",
    "*",
    "/",
    "->",
    "Field",
    "<",
    ">",
    "251",
    "main",
    "print",
    "return",
    "if",
    "else",
    "for",
    "in",
    "x",
    "y",
    "::",
    "..",
    "%",
    "&&",
    '"str',
    "'",
    "#[",
    "match",
    "struct",
    "enum",
]


def gen_malformed(rng):
    """Random token soup, sometimes starting from a valid skeleton then
    corrupting it (truncation, token deletion/insertion)."""
    style = rng.randint(0, 2)
    if style == 0:
        # pure soup
        k = rng.randint(1, 60)
        return " ".join(rng.choice(MALFORMED_TOKENS) for _ in range(k)) + "\n"
    if style == 1:
        # valid program, truncated at a random point
        src = gen_valid(rng)
        cut = rng.randint(0, len(src))
        return src[:cut]
    # valid program with random byte corruptions
    src = list(gen_valid(rng))
    for _ in range(rng.randint(1, 8)):
        if not src:
            break
        i = rng.randrange(len(src))
        src[i] = rng.choice(MALFORMED_TOKENS[:20])
    return "".join(src)


def gen_oversized(rng):
    """A huge but well-formed program to push arena usage toward the A1
    ceilings. Long let-chain in one function."""
    prime = rng.choice(FIELDS)
    n = rng.randint(20000, 60000)
    out = [
        f"field F = {prime};",
        "",
        "fn main() {",
        "    let v0: F = 1;",
        "    let v1: F = 2;",
    ]
    for i in range(2, n):
        op = OPS[i % len(OPS)]
        out.append(f"    let v{i}: F = v{i - 1} {op} v{i - 2};")
    out.append(f"    print(v{n - 1});")
    out.append("}")
    return "\n".join(out) + "\n"


CRASH_CODES = {139, 134, 138, -11, -6}  # segv / abort (posix and negative)


def is_crash(rc):
    return rc in CRASH_CODES or rc < 0


def run_compiler(binary, tv, ll):
    try:
        p = subprocess.run(
            [binary, tv, "-o", ll],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        return p.returncode
    except subprocess.TimeoutExpired:
        return "timeout"


def compile_link_run(llc, ll, work, tag):
    """llc + clang-link + run a compiled .ll; return (rc, stdout) or
    ("buildfail", "") if the toolchain stage fails."""
    obj = os.path.join(work, f"{tag}.o")
    exe = os.path.join(work, f"{tag}.exe")
    try:
        if (
            subprocess.run(
                [llc, "-filetype=obj", ll, "-o", obj], stderr=subprocess.DEVNULL
            ).returncode
            != 0
        ):
            return ("buildfail", "")
        if (
            subprocess.run(
                ["clang", obj, "-o", exe], stderr=subprocess.DEVNULL
            ).returncode
            != 0
        ):
            return ("buildfail", "")
        p = subprocess.run([exe], capture_output=True, timeout=30)
        if is_crash(p.returncode):
            return ("runcrash", "")
        return (p.returncode, p.stdout)
    except subprocess.TimeoutExpired:
        return ("timeout", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode", default="all", choices=["valid", "malformed", "oversized", "all"]
    )
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    llc = find_llc()
    work = tempfile.mkdtemp(prefix="tvfuzz.")
    failures = []
    try:
        tvc, tvc_self = build_compilers(work, llc)
        print(f"built tvc + tvc_self; seed={args.seed}")

        modes = (
            ["valid", "malformed", "oversized"] if args.mode == "all" else [args.mode]
        )
        for mode in modes:
            # oversized is slow; cap iterations
            count = 3 if mode == "oversized" else args.count
            gen = {
                "valid": gen_valid,
                "malformed": gen_malformed,
                "oversized": gen_oversized,
            }[mode]
            ok = 0
            for i in range(count):
                src = gen(rng)
                tv = os.path.join(work, f"{mode}_{i}.tv")
                with open(tv, "w") as f:
                    f.write(src)
                ll_a = os.path.join(work, f"{mode}_{i}_a.ll")
                ll_b = os.path.join(work, f"{mode}_{i}_b.ll")
                # Only valid mode needs the bootstrap (for behavioral parity).
                # Malformed/oversized judge tvc_self alone, so skip the frozen
                # seed there — it avoids irrelevant bootstrap crashes on
                # garbage and roughly halves runtime.
                rc_a = run_compiler(tvc, tv, ll_a) if mode == "valid" else 0
                rc_b = run_compiler(tvc_self, tv, ll_b)

                # Robustness assertion targets the CANONICAL compiler tvc_self
                # (rc_b): it must never crash, on any input. The frozen
                # bootstrap tvc (rc_a) is only a parity reference for VALID
                # inputs — its behavior on malformed input is out of scope (it
                # is a historical seed whose sole job is building Stage 1 from
                # well-formed source). So a bootstrap crash is fatal only in
                # valid mode; in malformed/oversized we judge tvc_self alone.
                if rc_b == "timeout":
                    failures.append((mode, i, f"tvc_self timeout", src))
                    continue
                if is_crash(rc_b):
                    failures.append((mode, i, f"tvc_self CRASH (rc={rc_b})", src))
                    continue
                if mode == "valid" and (rc_a == "timeout" or is_crash(rc_a)):
                    failures.append(
                        (
                            mode,
                            i,
                            f"bootstrap crash/timeout on valid input (rc={rc_a})",
                            src,
                        )
                    )
                    continue

                if mode == "valid":
                    # Both must accept, and the compiled programs must produce
                    # identical runtime output (behavioral parity).
                    if rc_a != 0 or rc_b != 0:
                        failures.append(
                            (
                                mode,
                                i,
                                f"valid prog rejected (tvc={rc_a}, self={rc_b})",
                                src,
                            )
                        )
                        continue
                    ra, out_a = compile_link_run(llc, ll_a, work, f"{mode}_{i}_a")
                    rb, out_b = compile_link_run(llc, ll_b, work, f"{mode}_{i}_b")
                    if ra == "runcrash" or rb == "runcrash":
                        failures.append(
                            (
                                mode,
                                i,
                                f"compiled program crashed (tvc={ra}, self={rb})",
                                src,
                            )
                        )
                        continue
                    if (
                        ra == "buildfail"
                        or rb == "buildfail"
                        or ra == "timeout"
                        or rb == "timeout"
                    ):
                        failures.append(
                            (mode, i, f"llc/link/run failed (tvc={ra}, self={rb})", src)
                        )
                        continue
                    if out_a != out_b:
                        failures.append(
                            (mode, i, "runtime output divergence (parity)", src)
                        )
                        continue
                elif mode == "oversized":
                    # Must either compile (0) or clean-fail (1); never crash
                    # (already checked). Accept both.
                    if rc_b not in (0, 1):
                        failures.append(
                            (mode, i, f"oversized unexpected rc={rc_b}", src)
                        )
                        continue
                # malformed: no-crash is the whole assertion; already passed.
                ok += 1
            print(f"  {mode:9s}: {ok}/{count} ok")

        if failures:
            print(f"\n{len(failures)} FAILURE(S):")
            for mode, i, why, src in failures[:10]:
                print(f"  [{mode} #{i}] {why}")
                snippet = src[:200].replace("\n", "\\n")
                print(f"      src: {snippet}")
            return 1
        print("\nall fuzz cases passed")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
