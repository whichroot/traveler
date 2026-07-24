<!--
Thanks for contributing to Traveler! Please fill in the sections below.
For anything large or compiler-touching, please open an issue to discuss first
(see CONTRIBUTING.md → "the discuss-first posture").
-->

## Summary

<!-- What does this change do, and why? -->

## Related issue

<!-- e.g. Closes #123 / Refs #123. Required for large or compiler-touching changes. -->

## Type of change

- [ ] Bug fix
- [ ] New feature (language, stdlib, tooling)
- [ ] Refactor (no behavior change)
- [ ] Docs
- [ ] Tests / examples
- [ ] Build / CI

## Checklist

<!-- Tick every box. If one doesn't apply, mark it and say why. -->

- [ ] `tests/run_dual.sh` passes locally with **LLVM 21** (the full gate).
- [ ] Commits follow **Conventional Commits** (`type(scope): summary`).
- [ ] Every commit is **signed off** for the DCO (`git commit -s`).
- [ ] I added/updated **tests** (regression for a fix, coverage for a feature)
      and/or **examples**.
- [ ] I updated **docs / spec** if behavior or the language surface changed.

### Compiler / bootstrap (only if you touched `src/tvc_self.tv`)

- [ ] N/A — I did not change the compiler.
- [ ] The change alters emitted IR, so I ran `src/bootstrap/refresh.sh` and
      **committed** the refreshed `src/bootstrap/tvc_self.boot.ll`.
- [ ] The self-hosting **fixed point** (Stage 2 == Stage 3) and **dual-compiler
      parity** are preserved.
- [ ] I did **not** add language features to the frozen `src-legacy/tvc.c`
      (only guards needed to keep Stage 1 building, if any).

### Standard library (only if you touched `src/lib/**`)

- [ ] N/A — I did not change `src/lib`.
- [ ] All changed/added `src/lib` files are **≤ 1500 lines**
      (`tests/run_sizegate.sh`); anything over-cap was split into `import`
      modules, not golfed.
- [ ] The formatter check passes (`tests/run_fmt.sh`).

## Notes for reviewers

<!-- Anything worth calling out: tradeoffs, follow-ups, areas needing extra eyes. -->
