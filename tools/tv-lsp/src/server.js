#!/usr/bin/env node
// server.js — Traveler Language Server (LSP over JSON-RPC / stdio).
//
// A thin transport layer. Every language decision is delegated to the engine
// (the tvc_self compiler) via engine.js. This server only:
//   - frames JSON-RPC over stdio (Content-Length headers),
//   - tracks open document text,
//   - on open/change (debounced): runs --diagnostics, publishes them,
//   - on hover: runs --symbols, shows the matching signature,
//   - on definition: runs --references, jumps to the resolved decl span.
//
// No language logic here — the engine is the source of truth, so navigation
// and diagnostics are exactly what the compiler sees.

import {
  resolveEnginePath,
  runEngine,
  diagToLsp,
  toLspPosition,
} from "./engine.js";

const enginePath = process.env.TVC_SELF || resolveEnginePath();

// --- document store ---
const docs = new Map(); // uri -> { text, version }
const debounceTimers = new Map(); // uri -> timeout
const DEBOUNCE_MS = 250;

// --- JSON-RPC framing over stdio ---
let buffer = Buffer.alloc(0);

process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  for (;;) {
    const headerEnd = buffer.indexOf("\r\n\r\n");
    if (headerEnd < 0) return;
    const header = buffer.slice(0, headerEnd).toString("ascii");
    const m = /Content-Length:\s*(\d+)/i.exec(header);
    if (!m) {
      buffer = buffer.slice(headerEnd + 4);
      continue;
    }
    const len = parseInt(m[1], 10);
    const bodyStart = headerEnd + 4;
    if (buffer.length < bodyStart + len) return; // wait for full body
    const body = buffer.slice(bodyStart, bodyStart + len).toString("utf8");
    buffer = buffer.slice(bodyStart + len);
    try {
      handleMessage(JSON.parse(body));
    } catch (e) {
      logError("parse error: " + e);
    }
  }
});

function send(msg) {
  const json = JSON.stringify(msg);
  const payload = Buffer.from(json, "utf8");
  process.stdout.write(`Content-Length: ${payload.length}\r\n\r\n`);
  process.stdout.write(payload);
}

function respond(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function notify(method, params) {
  send({ jsonrpc: "2.0", method, params });
}

function logError(text) {
  // window/logMessage type 1 = Error
  notify("window/logMessage", { type: 1, message: "[tv-lsp] " + text });
}

// --- message dispatch ---
function handleMessage(msg) {
  if (msg.method === "initialize") return onInitialize(msg);
  if (msg.method === "initialized") return;
  if (msg.method === "shutdown") return respond(msg.id, null);
  if (msg.method === "exit") return process.exit(0);

  if (msg.method === "textDocument/didOpen") return onDidOpen(msg);
  if (msg.method === "textDocument/didChange") return onDidChange(msg);
  if (msg.method === "textDocument/didClose") return onDidClose(msg);

  if (msg.method === "textDocument/hover") return onHover(msg);
  if (msg.method === "textDocument/definition") return onDefinition(msg);
  if (msg.method === "textDocument/documentSymbol") return onDocumentSymbol(msg);

  // Unknown request: respond null so the client isn't left hanging.
  if (msg.id !== undefined) respond(msg.id, null);
}

function onInitialize(msg) {
  respond(msg.id, {
    capabilities: {
      textDocumentSync: 1, // full document sync
      hoverProvider: true,
      definitionProvider: true,
      documentSymbolProvider: true,
    },
    serverInfo: { name: "tv-lsp", version: "0.1.0" },
  });
}

// --- document lifecycle ---
function onDidOpen(msg) {
  const { uri, text, version } = msg.params.textDocument;
  docs.set(uri, { text, version });
  scheduleDiagnostics(uri);
}

function onDidChange(msg) {
  const { uri, version } = msg.params.textDocument;
  const changes = msg.params.contentChanges;
  if (changes && changes.length) {
    // Full sync (textDocumentSync=1): last change holds the whole document.
    const text = changes[changes.length - 1].text;
    docs.set(uri, { text, version });
  }
  scheduleDiagnostics(uri);
}

function onDidClose(msg) {
  const { uri } = msg.params.textDocument;
  docs.delete(uri);
  notify("textDocument/publishDiagnostics", { uri, diagnostics: [] });
}

// --- diagnostics (debounced) ---
function scheduleDiagnostics(uri) {
  clearTimeout(debounceTimers.get(uri));
  debounceTimers.set(
    uri,
    setTimeout(() => publishDiagnostics(uri), DEBOUNCE_MS),
  );
}

function publishDiagnostics(uri) {
  const doc = docs.get(uri);
  if (!doc) return;
  const { records, engineError } = runEngine(enginePath, doc.text, "--diagnostics");
  if (engineError) {
    logError("engine: " + engineError);
    return;
  }
  notify("textDocument/publishDiagnostics", {
    uri,
    diagnostics: records.map(diagToLsp),
  });
}

// --- hover ---
function onHover(msg) {
  const { textDocument, position } = msg.params;
  const doc = docs.get(textDocument.uri);
  if (!doc) return respond(msg.id, null);
  const word = wordAt(doc.text, position);
  if (!word) return respond(msg.id, null);
  const { records } = runEngine(enginePath, doc.text, "--symbols");
  const sym = records.find((r) => r.name === word);
  if (!sym) return respond(msg.id, null);
  respond(msg.id, {
    contents: { kind: "markdown", value: "```traveler\n" + sym.signature + "\n```" },
  });
}

// --- go-to-definition (scope-aware via --references) ---
function onDefinition(msg) {
  const { textDocument, position } = msg.params;
  const doc = docs.get(textDocument.uri);
  if (!doc) return respond(msg.id, null);
  const { records } = runEngine(enginePath, doc.text, "--references");
  // LSP position is 0-based; engine refs are 1-based. Find the reference
  // whose span covers the cursor.
  const line1 = position.line + 1;
  const char1 = position.character + 1;
  const ref = records.find(
    (r) =>
      r.refLine === line1 &&
      char1 >= r.refCol &&
      char1 <= r.refEndCol,
  );
  if (ref) {
    const len = ref.refEndCol - ref.refCol;
    return respond(msg.id, {
      uri: textDocument.uri,
      range: {
        start: toLspPosition(ref.defLine, ref.defCol),
        end: toLspPosition(ref.defLine, ref.defCol + Math.max(1, len)),
      },
    });
  }
  // Fall back to a top-level symbol by the word under the cursor.
  const word = wordAt(doc.text, position);
  if (word) {
    const { records: syms } = runEngine(enginePath, doc.text, "--symbols");
    const sym = syms.find((s) => s.name === word);
    if (sym) {
      return respond(msg.id, {
        uri: textDocument.uri,
        range: {
          start: toLspPosition(sym.line, sym.col),
          end: toLspPosition(sym.line, sym.endCol),
        },
      });
    }
  }
  respond(msg.id, null);
}

// --- document symbols (outline) ---
function onDocumentSymbol(msg) {
  const { textDocument } = msg.params;
  const doc = docs.get(textDocument.uri);
  if (!doc) return respond(msg.id, []);
  const { records } = runEngine(enginePath, doc.text, "--symbols");
  const kindMap = { fn: 12, struct: 23, enum: 10, trait: 11, field: 8 };
  respond(
    msg.id,
    records.map((s) => {
      const range = {
        start: toLspPosition(s.line, s.col),
        end: toLspPosition(s.line, s.endCol),
      };
      return {
        name: s.name,
        detail: s.signature,
        kind: kindMap[s.kind] || 13,
        range,
        selectionRange: range,
      };
    }),
  );
}

// --- helpers ---
// Extract the identifier word at an LSP position (0-based).
function wordAt(text, position) {
  const lines = text.split("\n");
  const line = lines[position.line];
  if (line === undefined) return null;
  const isIdent = (c) => /[A-Za-z0-9_]/.test(c);
  let s = position.character;
  let e = position.character;
  while (s > 0 && isIdent(line[s - 1])) s--;
  while (e < line.length && isIdent(line[e])) e++;
  if (s === e) return null;
  return line.slice(s, e);
}

logError("started (engine: " + enginePath + ")");
