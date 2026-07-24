# HERMES.md — Traveler (hermes-agent)

hermes-agent loads one project-context file, first match wins:
`.hermes.md` / `HERMES.md` → `AGENTS.md` → `CLAUDE.md`. This file is the top
match, so hermes loads it **instead of** `AGENTS.md`.

**Read [`AGENTS.md`](AGENTS.md) first** — it is the canonical guide to building
programs with Traveler (build/run, language reference, standard library, gotchas).
Everything below is only the hermes-agent-specific delta. (Delete this file and
hermes falls through to `AGENTS.md` directly — that is also fine; hermes reads
`AGENTS.md` natively.)

## Working effectively here

- **Builds and tests are long-running.** `src/bootstrap/build.sh` and the
  `tests/*.sh` gates can outlast a `terminal` timeout. Launch them detached —
  `nohup <cmd> > /tmp/build.log 2>&1 &` — and poll with `read_file`, instead of
  blocking one `terminal` call or retrying on timeout.
- **The compiler source is huge:** `src/tvc_self.tv` is ~1 MB / ~24k lines, and
  the boot IR `src/bootstrap/tvc_self.boot.ll` is larger. Use `search_files` or
  ranged `read_file`; never load either whole.
- **Locate symbols with the compiler, not scans.** Beyond hermes's own LSP
  diagnostics, the Traveler compiler answers `--diagnostics` / `--symbols` /
  `--references` exactly (AGENTS.md §1).
- **Verify a change by compiling one example**
  (`examples/field_basics.tv` → `49 100 171 2 123 1`), not by running the
  self-host gate — that is a contributor workflow
  ([`../CONTRIBUTING.md`](../CONTRIBUTING.md)).
- **Context-file size:** hermes head/tail-truncates a project-context file past
  ~20,000 chars (`context_file_max_chars`). `AGENTS.md` is ~18k today — keep it
  lean or hermes will truncate it.

## SOUL.md is not in this repo

`SOUL.md` is hermes-agent's personality file, loaded only from `HERMES_HOME`,
never from the working directory. It is intentionally absent here — bring your
own; it is not project context.

## Gotchas that bite generated code (from AGENTS.md §5)

No floats (integer/field only); `let` is immutable (use `var`); `i128/i256` have
no `/` or `%`; `match` needs every arm; closures don't escape; no `dyn Trait`;
off macOS, pass the platform triple to `stage1` **and** `llc`, and link with
`-no-pie` on Linux.
