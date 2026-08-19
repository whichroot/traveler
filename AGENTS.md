# AGENTS.md

House rules for agents that work in this repository.

## Branches

- Work on `staging`, not `main`.

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
