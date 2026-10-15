# Traveler with Hermes

Read [AGENTS.md](AGENTS.md) explicitly if Hermes selected this file as its
project context. For repository changes, also read
[the root rules](../AGENTS.md).

- Use `search_files` and ranged `read_file` calls for the compiler. Avoid
  loading `src/tvc_self.tv` or its bootstrap IR in full.
- Builds and test gates can exceed a terminal timeout. Use the installed
  harness's long-running-command support and collect both the log and exit
  status. Check whether a process is still running before retrying.
- Keep project context compact; context-file limits depend on the installed
  Hermes version and configuration.
- Language rules, toolchain commands, and verification guidance live in the
  shared guide so that agent-specific files do not carry competing copies.
