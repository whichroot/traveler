// engine.js — thin wrapper around the Traveler compiler's LSP query modes.
//
// All language intelligence lives in the compiler (tvc_self), which the
// roadmap calls "the engine is the compiler". This module shells the engine
// in its three query modes and parses the JSONL it emits on stdout:
//
//   tvc_self FILE --diagnostics   ->  {severity,line,col,endLine,endCol,message}
//   tvc_self FILE --symbols       ->  {name,kind,line,col,endCol,signature}
//   tvc_self FILE --references    ->  {refLine,refCol,refEndCol,defLine,defCol,kind}
//
// The same engine compiles to wasm32 (Traveler -> LLVM IR -> wasm32), so a
// browser playground or mobile editor can run identical intelligence with this
// exact JSONL contract — the engine is the single source of truth across web,
// iOS, and Android surfaces.

import { spawnSync } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Resolve the engine binary. Order: TVC_SELF env, then a conventional build
// path. The server passes an explicit path from its config; this is the
// fallback for tests / standalone use.
export function resolveEnginePath() {
  if (process.env.TVC_SELF) return process.env.TVC_SELF;
  return "/tmp/tvc_self";
}

// Run one engine query mode over `text`, returning parsed JSONL records.
// `mode` is "--diagnostics" | "--symbols" | "--references".
// Errors (engine missing, crash) return [] — an editor degrades gracefully
// rather than failing; the caller may inspect `.engineError` on the result.
export function runEngine(enginePath, text, mode) {
  const dir = mkdtempSync(join(tmpdir(), "tvlsp-"));
  const file = join(dir, "buffer.tv");
  try {
    writeFileSync(file, text, "utf8");
    const res = spawnSync(enginePath, [file, mode], {
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
      timeout: 15000,
    });
    if (res.error) {
      return { records: [], engineError: String(res.error.message || res.error) };
    }
    const records = parseJsonl(res.stdout || "");
    return { records, engineError: null };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// Parse newline-delimited JSON. Silently skips malformed lines (defensive:
// the engine also prints progress to stderr, never stdout, but be safe).
export function parseJsonl(out) {
  const records = [];
  for (const line of out.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      records.push(JSON.parse(t));
    } catch {
      // skip non-JSON noise
    }
  }
  return records;
}

// --- LSP coordinate conversion ---
//
// The engine reports 1-based line/col (col = character within the line). LSP
// positions are 0-based {line, character}. These helpers convert a record's
// span into an LSP Range.

export function toLspPosition(line1, col1) {
  return { line: Math.max(0, line1 - 1), character: Math.max(0, col1 - 1) };
}

export function diagToLsp(rec) {
  return {
    range: {
      start: toLspPosition(rec.line, rec.col),
      end: toLspPosition(rec.endLine ?? rec.line, rec.endCol ?? rec.col + 1),
    },
    severity: rec.severity === "error" ? 1 : 2, // 1=Error, 2=Warning
    source: "traveler",
    message: rec.message,
  };
}
