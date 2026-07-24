# Security Policy

## Project status and scope of assurance

Traveler is **early alpha** (`v0.1.0`) and is provided **for research purposes
only**. In particular, as stated in the README:

> Traveler is an alpha product; the ZK-proof infrastructure is unverified by a
> third party at this time. Use at your own risk or verification.

Do **not** deploy Traveler and especially its cryptographic (`src/lib/crypto`)
and zero-knowledge (`src/lib/zk`) subsystems — in production or in any setting
where a failure has real-world consequences. No security guarantees are made at
this stage.

That said, we take defects seriously and want to hear about them.

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` (alpha, `main`) | ✅ actively developed; report against latest `main` |
| anything older | ❌ |

Because the project is pre-1.0, security fixes land on `main`. Please reproduce
against the latest `main` before reporting.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security vulnerability.**

Report privately, via **either**:

1. **GitHub private vulnerability reporting** — go to the repository's
   **Security** tab → **Report a vulnerability** (GitHub Security Advisories).
   This keeps the report private to the maintainers until a fix is ready.
2. **Signal** — **hollow.49**. Will receive email and further instructions from there.

Please include:

- A description of the issue and its impact.
- A **minimal `.tv` reproduction** (or the exact inputs/commands), plus your OS
  and LLVM version.
- The affected component (compiler, a specific `src/lib` subsystem, build
  tooling).
- Any suggested remediation, if you have one.

### What to expect

- **Acknowledgement** of your report within a reasonable time.
- An initial assessment and, where accepted, a plan and rough timeline.
- Credit for the discovery when a fix is published, unless you prefer to remain
  anonymous.
- We ask for **coordinated disclosure**: please give us a reasonable window to
  fix the issue before any public disclosure.

## What is in scope

Because Traveler is a compiler and a math/crypto library, "security" here is
broader than a typical app. In-scope classes of issue include:

- **Miscompilation / unsoundness** — the compiler emitting incorrect code for a
  valid program, or accepting a program it should reject in a way that produces
  unsound results.
- **Auto-parallelization soundness violations** — a loop parallelized when the
  iterations are *not* provably independent (the default-deny guarantee is
  load-bearing; see `tests/run_pfor.sh`).
- **Memory-safety defects in generated code** — out-of-bounds access, use of
  uninitialized memory, or similar in code the compiler produces.
- **Cryptographic / ZK defects** — incorrect field arithmetic, NTT, Poseidon2,
  Merkle, FRI, or PLONK behavior; soundness or completeness gaps in `#[zk]`
  circuits. (Reminder: this stack is explicitly unverified — reports are
  welcome, but treat findings as expected for alpha software.)
- **Build / supply-chain integrity** — anything that undermines the self-hosting
  trust root (`src/bootstrap/tvc_self.boot.ll`) or the byte-identical fixed
  point in a way that could hide malicious codegen.

## What is out of scope

- Bugs with no security impact — please file these as normal
  [issues](.github/ISSUE_TEMPLATE) instead.
- Vulnerabilities in third-party tools (LLVM, your C toolchain, your OS).
- Anything requiring an already-compromised host or a non-default, clearly
  unsafe configuration.

Thank you for helping keep Traveler honest.
