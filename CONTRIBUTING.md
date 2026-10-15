# Contributing to Traveler

Thanks for your interest in Traveler — a general-purpose systems language built
on field arithmetic and polynomial regimes, compiled by a **self-hosting**
compiler written in Traveler itself.

Traveler is **early alpha** (`v0.1.0`) and for now, a research project. The
codebase maintains a byte-identical self-hosting fixed
point, a module size cap, and regression contracts that contributions must
respect. This document explains how to work within them so your change lands
smoothly.

Please also read our [Code of Conduct](CODE_OF_CONDUCT.md). By participating you
agree to uphold it.

---

## Table of contents

- [Before you start: the discuss-first posture](#before-you-start-the-discuss-first-posture)
- [Ways to contribute](#ways-to-contribute)
- [Prerequisites](#prerequisites)
- [Getting set up](#getting-set-up)
- [The invariants you must not break](#the-invariants-you-must-not-break)
- [Running the tests](#running-the-tests)
- [Commit conventions](#commit-conventions)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Opening a pull request](#opening-a-pull-request)
- [Coding conventions](#coding-conventions)
- [Reporting bugs and security issues](#reporting-bugs-and-security-issues)
- [License](#license)

---

## Before you start: the discuss-first posture

Traveler is small, alpha, and load-bearing on a few strict invariants. To save
everyone wasted effort:

- **Open an issue first** for anything large, or anything that touches the
  compiler (`src/tvc_self.tv`), the bootstrap trust root, the language surface,
  or an invariant below. Describe the problem and the approach before writing
  the code. Language-surface changes especially benefit from a design discussion.
- **Go straight to a PR** for small, self-contained fixes: documentation,
  typos, tests, new `examples/`, and clearly-scoped bug fixes.

If you are unsure which bucket you are in, open an issue and ask.

---

## Ways to contribute

- **Report bugs** — miscompilations, crashes, unsound parallelization, incorrect
  field arithmetic, spec/implementation mismatches. See
  [reporting bugs](#reporting-bugs-and-security-issues).
- **Improve docs** — `README.md`, `BUILD.md`, the language spec (`spec/`), and
  this guide.
- **Add examples** — self-contained programs under `examples/` that show off (or
  stress) a language feature.
- **Add tests** — regression cases, negative/error cases, parallelization
  soundness cases. The test suite is the project's memory.
- **Extend the standard library** — subsystems under `src/lib/**` (collections,
  crypto, zk, codec, regime, dsp, ecc, nt, …).
- **Work on the compiler** — `src/tvc_self.tv`. High-value, high-care; please
  discuss first.

---

## Prerequisites

You need:

| Tool | Version | Purpose |
|---|---|---|
| **LLVM** | **15+** (required) | `llc` (IR → object), `opt` (optional) |
| C compiler | clang or gcc | used **only as the linker** for the C-free build |
| bash + coreutils | any | build and test scripts |

LLVM 15+ is required — earlier versions emit incompatible IR. Full toolchain
setup (macOS Homebrew, Debian/Ubuntu) is in [`BUILD.md`](BUILD.md).

The test and build scripts auto-detect `llc`/`opt` in common locations. If
detection fails, set `LLC` (and optionally `OPT`) to the full path:

```sh
export LLC=/opt/homebrew/opt/llvm@21/bin/llc
```

---

## Getting set up

```sh
# 1. Fork on GitHub, then clone your fork
git clone git@github.com:<you>/traveler.git
cd traveler

# 2. Build the canonical, C-free compiler from the committed trust root.
#    This boots src/bootstrap/tvc_self.boot.ll, compiles the current source,
#    and asserts the self-hosting fixed point (stage1 == stage2).
src/bootstrap/build.sh
# -> src/bootstrap/out/stage1 is the canonical compiler.

# 3. Compile and run a program.
TVC=src/bootstrap/out/stage1
$TVC examples/field_basics.tv --emit exe -llc "$LLC" -o /tmp/fb
/tmp/fb        # expected: 49 100 171 2 123 1
```

See [`BUILD.md`](BUILD.md) for the full pipeline, cross-compilation, optimized
builds, shared libraries, and the legacy C-seed audit path.

---

## The invariants you must not break

Use these contracts when changing the compiler and library. The local canonical
gate and the checked-in CI workflow have different entry points, described below.

### 1. The self-hosting fixed point (byte-identical)

The compiler must reproduce itself exactly. Stage 2 (the compiler compiling
itself) must emit IR **byte-identical** to Stage 3 (Stage 2 compiling itself).

**If you change `src/tvc_self.tv` in a way that alters emitted IR, you must
refresh the committed bootstrap snapshot and commit it:**

```sh
src/bootstrap/refresh.sh
git add src/bootstrap/tvc_self.boot.ll
```

`src/bootstrap/tvc_self.boot.ll` is the **trust root** — a Traveler-produced
snapshot of the compiler that lets the whole tree build with no C in the trust
chain. Use `src/bootstrap/build.sh --check` to check freshness. A change to
compiler diagnostics can still change the compiler's own emitted IR; do not
assume that unchanged output for one valid program makes a refresh unnecessary.
See [`src/bootstrap/PROVENANCE.md`](src/bootstrap/PROVENANCE.md) for the full
trust model.

### 2. Legacy-seed compatibility

`tests/run_bootstrap.sh` checks the canonical snapshot-based bootstrap.
`--legacy-seed` explicitly adds the historical seed comparison;
`tests/run_dual.sh` is the separate legacy compatibility path. The frozen seed
does not implement all current language features.

### 3. New language features land in `src/tvc_self.tv` only

`src-legacy/tvc.c` is the **frozen** original seed, kept only as an independent
provenance/audit path. Do **not** add language features to it — mirror in only
the guards needed to keep Stage 1 building.

### 4. The module size cap: `src/lib/**` ≤ 1500 lines

Every library module must be ≤ 1500 lines (`tests/run_sizegate.sh`).
`src/tvc_self.tv` is the **sole** exemption — the deliberate self-hosting
monolith, kept whole to protect the fixed point. No new file may join the
exemption list.

If a library module grows past the cap, **split it into `import` modules**
(a byte-identical merged unit — see the codec for the pattern). Do **not** golf
newlines to squeak under the cap; the cap is a proxy for "legible per seam," not
a code-golf target.

### 5. The formatter is idempotent and meaning-preserving

Run the formatter check before you push (`tests/run_fmt.sh`). `tvfmt` is a
conservative, comment-preserving reindenter; its contract is
`fmt(fmt(x)) == fmt(x)` and it must never change program behavior.

---

## Running the tests

The environment-aware local gate is:

```sh
tests/run_all.sh --list
tests/run_all.sh
```

With the required toolchain, the dispatcher runs `tests/run.sh` through the
canonical compiler and repeats applicable checks for additional link drivers.
Record any toolchain or hardware skips. Run focused suites while developing;
compiler/library contributions should pass the applicable full gate before a PR.
For documentation-only changes, verify examples, commands, and links.

The checked-in `.github/workflows/ci.yml` still invokes `tests/run_dual.sh`
and a seed-driven fixed-point step on Linux and macOS, with push/PR triggers
targeting `main`. Those are legacy
compatibility checks in addition to the canonical local workflow; do not mistake
them for a requirement to build normal Traveler programs through the C seed.

The individual suites, if you want to run them in isolation:

| Script | What it covers |
|---|---|
| `tests/run.sh` | regression suite (incl. LSP / doc / bootstrap gates) |
| `tests/run_pfor.sh` | auto-parallelization **soundness** (default-deny) |
| `tests/dynfield/run.sh` | dynamic-field + traits + closures |
| `tests/run_bootstrap.sh` | snapshot bootstrap and self-hosting fixed point |
| `tests/run_dual.sh` | legacy-seed compatibility |
| `tests/run_sizegate.sh` | module size gate (≤ 1500 lines) |
| `tests/run_fmt.sh` | formatter idempotence + meaning preservation |
| `tests/run_lsp.sh` | language-server diagnostics/hover/references |
| `tests/run_doc.sh` | doc generator |
| `tests/run_diag.sh` | compiler diagnostics |

Override the toolchain path if auto-detection fails:

```sh
LLC=/usr/lib/llvm-21/bin/llc tests/run.sh
```

If you fix a bug, add a regression test that fails before your change and passes
after. If you add a feature, add examples and/or tests that exercise it.

---

## Commit conventions

Traveler uses **[Conventional Commits](https://www.conventionalcommits.org/)**
with a subsystem scope. Format:

```
<type>(<scope>): <imperative summary>   (#<issue>)
```

Examples from the history:

```
feat(nt): promote number-theory kernels — forward point-counting + inverse Berlekamp–Massey
fix(codegen): user fn shadows name-based builtin + widen on assignment
refactor(dyn-field): division-free i128 construction path (#15 follow-up)
test(codegen-diff): broaden corpus to full compiling examples tree
docs(observe): header audit verdict — the LPC header is near-incompressible at w1024
```

- **Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- **Scopes** are the subsystem you touched, e.g. `core`, `codegen`, `nt`,
  `dyn-field`, `wide-field`, `binfield`, `observe`, `exact-arith`, `ecc`,
  `mobius`, `codec`, `examples`, `lsp`, `docs`.
- Use an **imperative** summary ("add", "fix", "split"), keep it tight, and
  reference issues as `(#NN)` where relevant.
- Use ASD-STE100 (Simplified Technical English): short sentences, active voice,
  one instruction or statement per sentence. Follow the root `AGENTS.md` rules.

---

## Developer Certificate of Origin (DCO)

Traveler requires a **DCO sign-off** on every commit. This is a lightweight,
per-commit statement that you wrote the patch or otherwise have the right to
contribute it under the project's [Apache-2.0 license](LICENSE). It is **not** a
CLA — you retain copyright, and contributions are licensed inbound exactly as
the project is licensed outbound.

Add the sign-off automatically with `-s`:

```sh
git commit -s -m "feat(codec): add adaptive window selection"
```

This appends a line to your commit message:

```
Signed-off-by: Your Name <you@example.com>
```

The name and email must be real and match your `git config user.name` /
`user.email`. To sign off a commit you forgot to sign, use
`git commit --amend -s`; for a range, `git rebase --signoff`.

By signing off, you certify the Developer Certificate of Origin, version 1.1:

```
Developer Certificate of Origin
Version 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

The full text also lives at <https://developercertificate.org/>.

---

## Opening a pull request

1. Branch from `staging` in your fork (e.g. `fix/codegen-shadowing` or
   `feat/nt-zeta`).
2. Keep the PR focused — one logical change. Unrelated cleanups belong in their
   own PR.
3. Make sure, locally:
   - Applicable canonical gates pass with LLVM 21; record legacy compatibility
     results separately when running the CI/seed path.
   - Commits follow the [commit conventions](#commit-conventions) and are
     [signed off](#developer-certificate-of-origin-dco).
   - If you changed `src/tvc_self.tv` and it altered emitted IR, you ran
     `src/bootstrap/refresh.sh` and committed the refreshed
     `src/bootstrap/tvc_self.boot.ll`.
   - New/changed `src/lib` files are ≤ 1500 lines.
   - The formatter check passes (`tests/run_fmt.sh`).
4. Push and open the PR. Fill in the PR template checklist.

CI runs on Linux and macOS and gates, in order: module size, bootstrap
(C-free self-build), C-seed provenance, the full suites, and the Stage 2 ==
Stage 3 fixed point. All must pass.

Because the project is alpha and the invariants are strict, expect review to
focus on: parity/fixed-point preservation, parallelization soundness, test
coverage, and whether the change belongs in `tvc_self.tv` vs the library.

---

## Coding conventions

- **Source comments explain the mechanic in at most two lines.** Keep internal
  design prose in `devnotes/` (gitignored). The language specification describes
  implemented behavior; proposals live separately from that reference.
- **Prefer legible seams over cleverness.** The size cap exists to keep modules
  splittable and readable; structure code so a reader can follow one seam at a
  time.
- **Parallelization is default-deny.** A loop parallelizes only when the
  compiler can *prove* the iterations are independent. Never weaken a soundness
  check to make a loop go parallel — an unchecked callsite is unsound.
- **Match the surrounding style** of the file and subsystem you are editing.

---

## Reporting bugs and security issues

- **Ordinary bugs / feature ideas** — open a GitHub issue using the templates.
  For bugs, include a **minimal `.tv` reproduction**, your OS, your LLVM
  version, and which gate/command fails.
- **Security vulnerabilities** — do **not** open a public issue. Follow the
  private process in [`SECURITY.md`](SECURITY.md). Note that Traveler's ZK/crypto
  stack is unverified by a third party and is provided for research use.

---

## License

Traveler is licensed under the [Apache License 2.0](LICENSE). Contributions are
accepted under the same license (inbound = outbound), and the DCO sign-off is
how you attest to that. You retain copyright to your contributions.
