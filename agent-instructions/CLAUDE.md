# Traveler with Claude Code

Read [AGENTS.md](AGENTS.md) for the Traveler language and build workflow.
For repository changes, also read [the root rules](../AGENTS.md).

- Use bounded Read calls and targeted Grep searches for `src/tvc_self.tv`.
  Avoid loading the compiler or bootstrap IR in full.
- Builds and test gates can exceed a short tool timeout. Use the supported
  background-command facility or a longer timeout, then collect the exit status
  and log. Check whether the original process is still running before retrying.
- Compiler query flags are documented in the shared guide. `--eval` runs code;
  use it only when program execution is intended.
- Verify application changes with the actual program and expected results.
  Compiler and library changes follow the contributor test workflow.
