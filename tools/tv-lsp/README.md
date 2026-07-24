# tv-lsp — Traveler Language Server

A Language Server Protocol implementation for Traveler (`.tv`), providing
**inline diagnostics**, **hover** (signatures), **go-to-definition**
(scope-aware), and a **document outline**.

## Architecture: the engine is the compiler

There is **no second parser or type-checker here.** All language intelligence
lives in the Traveler compiler (`tvc_self`), exposed through three
machine-readable query modes that emit JSON-Lines on stdout:

| Mode | Records | Powers |
|---|---|---|
| `tvc_self FILE --diagnostics` | `{severity,line,col,endLine,endCol,message}` | inline diagnostics |
| `tvc_self FILE --symbols` | `{name,kind,line,col,endCol,signature}` | hover, outline |
| `tvc_self FILE --references` | `{refLine,refCol,refEndCol,defLine,defCol,kind}` | go-to-definition (scope-aware) |

This server (`src/server.js`) is a **thin JSON-RPC/stdio transport**: it tracks
open documents, debounces changes, shells the engine, and translates the JSONL
into LSP messages. Navigation and diagnostics are therefore *exactly* what the
compiler sees — scope-correct, shadowing-aware, generic-aware.

### One engine, every surface

Because the engine is the compiler, and Traveler already compiles to LLVM IR
(and thus `wasm32`), the **same** intelligence runs:

- **Desktop editors** (VS Code, Neovim, …) — this server over a native
  `tvc_self` binary.
- **Web** — the engine built to `wasm32`, driven by the identical JSONL
  contract from a browser playground.
- **iOS / Android** — a thin native shim around the same wasm engine, or this
  TS server in an embedded Node/JS runtime.

The JSONL contract is the single integration point across all three.

## Running

```sh
# Build the engine:
#   src-legacy/tvc src/tvc_self.tv -o /tmp/tvc_self.ll
#   llc -filetype=obj /tmp/tvc_self.ll -o /tmp/tvc_self.o
#   clang /tmp/tvc_self.o -o /tmp/tvc_self

# Point the server at the engine and run it (stdio LSP):
TVC_SELF=/tmp/tvc_self node tools/tv-lsp/src/server.js
```

Any LSP client can connect over stdio. A minimal VS Code extension manifest is
in `vscode/` (associates `.tv`, launches this server).

## Tests

```sh
cd tools/tv-lsp
TVC_SELF=/tmp/tvc_self npm test
```

The suite covers the JSONL contract (unit) and a full JSON-RPC cycle
(initialize → didOpen → publishDiagnostics, hover, scope-aware definition)
driven over real stdio framing — no editor required. It is wired into the repo
gate via `tests/run_lsp.sh` (engine side) and run standalone here (transport
side).

## Scope & deferrals

- **In:** diagnostics, hover, go-to-definition (scope-aware, receiver-typed
  member/method resolution), document symbols.
- **Deferred:** find-all-references / rename (the reference table is the
  substrate — a clean follow-up), DWARF/debugger, type-aware completion,
  multi-file project navigation (single-buffer today; Model-A `import`
  resolution at the engine level is a follow-up).
