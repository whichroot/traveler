# AGENTS.md

House rules for agents that work in this repository.

For Traveler syntax and application development, read
[`agent-instructions/AGENTS.md`](agent-instructions/AGENTS.md). The implemented
language reference is [`spec/language-spec.md`](spec/language-spec.md).

## Branches

- Use `staging` as the development base, not `main`.
- If the user requests an isolated task branch, use a separate worktree so
  concurrent work on `staging` keeps its branch and working files.

## Build and verification

- Build the canonical compiler with `src/bootstrap/build.sh`. The frozen C seed
  is not the compiler for current language work.
- `tests/run_all.sh --list` shows local capabilities and the suite plan.
  Run relevant checks for the change; contributor gates are in `CONTRIBUTING.md`.
- For documentation, verify commands, examples, and links against the source
  revision. Keep unimplemented proposals outside the language specification.
- Read `BUILD.md` for toolchain and backend requirements. Record skipped hardware
  checks separately from successful runtime checks.

## Commits

- Write commit messages in ASD-STE100 (Simplified Technical English).
  Short sentences. Active voice. One instruction or statement per sentence.
- Sign every commit with DCO (`git commit -s`).
- Keep commits small. A performance claim carries before and after numbers.

## Prose

- Do not put agent prose in code or commit bodies.
- Put internal design prose in markdown under `devnotes/` (gitignored).

## Comments

- A comment is no longer than two lines.
- Explain the mechanic so the reader can interpret it in that space.

## Pointers

- Point to an internal design note with the conventional pointer form:
  `// (design-notes/<file>.md "<anchor>")`
