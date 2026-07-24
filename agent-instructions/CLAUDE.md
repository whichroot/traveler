# CLAUDE.md — Traveler (Claude Code)

Read [`AGENTS.md`](AGENTS.md) first. It is the canonical, agent-agnostic guide to
building programs with Traveler — build/run, language reference, standard library,
and gotchas. This file adds only what is specific to working as **Claude Code** in
this repo.

## Working effectively here

- **Builds and tests are long-running.** `src/bootstrap/build.sh` and the
  `tests/*.sh` gates routinely exceed the default 2-minute Bash timeout. Start
  them with the Bash tool's `run_in_background`, or raise `timeout` (up to
  600000 ms). If a background run isn't honored by your harness, fall back to
  shell-native backgrounding — `nohup <cmd> > /tmp/build.log 2>&1 &` — then poll
  `/tmp/build.log` with Read. Do **not** retry a timed-out build in a loop; run it
  once, detached.

- **The compiler source is huge.** `src/tvc_self.tv` is ~1 MB / ~24k lines, and
  the boot IR `src/bootstrap/tvc_self.boot.ll` is larger. Never Read either whole
  — it will exhaust your context. Use Grep (with `-n` and context) or Read with
  `offset`/`limit`.

- **Explore with the Task tool.** For open-ended "where is X / how does Y work"
  questions across the compiler, dispatch an Agent rather than scanning by hand —
  it keeps the large files out of your context window. Reserve direct Read/Grep
  for known targets.

- **Prefer the dedicated tools** (Read, Grep, Glob, Edit) over shell `cat`/`grep`/
  `sed`/`find`. The files here are large; the dedicated tools page and stream.

- **Use the compiler as a code-intelligence engine.** Before guessing where a
  symbol lives, run the read-only query modes — `--diagnostics`, `--symbols`,
  `--references` (AGENTS.md §1). The intelligence lives in the compiler and these
  answers are exact.

- **Verify your change by compiling one example**, not by running the full gate.
  `examples/field_basics.tv` compiling and printing `49 100 171 2 123 1` confirms
  the toolchain. The self-host gates are a contributor concern
  ([`../CONTRIBUTING.md`](../CONTRIBUTING.md)), not part of building a program.

## Field-arithmetic gotchas that bite generated code

Repeats of AGENTS.md §5 worth pre-loading, because they are the errors a model
tends to produce:

- There are **no floats** — this is an integer/field language.
- `let` is immutable; use `var` to mutate.
- `i128/i256` have no `/` or `%`.
- `match` needs every arm (`_` for catch-all); closures don't escape; there is no
  `dyn Trait`.
- Off macOS, pass the platform triple to `stage1` **and** `llc`, and link with
  `-no-pie` on Linux (AGENTS.md §1).
