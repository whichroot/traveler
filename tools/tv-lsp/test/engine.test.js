// engine.test.js — tests for the tv-lsp engine wrapper + a full JSON-RPC cycle.
//
// Requires a built engine binary at $TVC_SELF (or /tmp/tvc_self). Run via
// `npm test` from tools/tv-lsp, or directly:
//   TVC_SELF=/tmp/tvc_self node test/engine.test.js
//
// Asserts the JSONL contract the server depends on, and drives the server over
// real stdio framing to prove diagnostics / hover / definition work
// end-to-end without an editor.

import assert from "node:assert";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  resolveEnginePath,
  runEngine,
  parseJsonl,
  diagToLsp,
  toLspPosition,
} from "../src/engine.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const enginePath = resolveEnginePath();

let passed = 0;
let failed = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
    passed++;
  } catch (e) {
    console.log(`  FAIL  ${name}: ${e.message}`);
    failed++;
  }
}

async function main() {
  // --- pure-unit tests (no engine needed) ---
  await test("parseJsonl skips blank/garbage lines", () => {
    const recs = parseJsonl('{"a":1}\n\nnot json\n{"b":2}\n');
    assert.deepEqual(recs, [{ a: 1 }, { b: 2 }]);
  });

  await test("toLspPosition is 0-based and clamped", () => {
    assert.deepEqual(toLspPosition(1, 1), { line: 0, character: 0 });
    assert.deepEqual(toLspPosition(5, 9), { line: 4, character: 8 });
    assert.deepEqual(toLspPosition(0, 0), { line: 0, character: 0 });
  });

  await test("diagToLsp maps severity + range", () => {
    const d = diagToLsp({
      severity: "error",
      line: 2,
      col: 11,
      endLine: 2,
      endCol: 14,
      message: "boom",
    });
    assert.equal(d.severity, 1);
    assert.equal(d.source, "traveler");
    assert.equal(d.message, "boom");
    assert.deepEqual(d.range.start, { line: 1, character: 10 });
    assert.deepEqual(d.range.end, { line: 1, character: 13 });
  });

  if (!existsSync(enginePath)) {
    console.log(`\n  (skipping engine-backed tests: ${enginePath} not found)\n`);
    return;
  }

  // --- engine-backed tests ---
  await test("diagnostics: valid program emits no records", () => {
    const src = "field F = Field<251>;\nfn main() { print(1); }\n";
    const { records, engineError } = runEngine(enginePath, src, "--diagnostics");
    assert.equal(engineError, null);
    assert.equal(records.length, 0);
  });

  await test("diagnostics: missing semicolon yields a located record", () => {
    const src = "fn main() {\n    let x: i32 = 5\n    return;\n}\n";
    const { records } = runEngine(enginePath, src, "--diagnostics");
    assert.ok(records.length >= 1, "expected >=1 diagnostic");
    const r = records[0];
    assert.equal(r.severity, "error");
    assert.ok(typeof r.line === "number" && typeof r.col === "number");
    assert.ok(r.endCol >= r.col);
  });

  await test("symbols: signatures reconstructed", () => {
    const src =
      "fn add(a: i32, b: i32) -> i32 { return a + b; }\nfn main() { print(add(1,2)); }\n";
    const { records } = runEngine(enginePath, src, "--symbols");
    const add = records.find((r) => r.name === "add");
    assert.ok(add, "add symbol present");
    assert.equal(add.kind, "fn");
    assert.equal(add.signature, "fn add(a: i32, b: i32) -> i32");
  });

  await test("references: scope-aware shadowing", () => {
    // A while body opens a fresh scope (the language has no bare {} blocks).
    const src =
      [
        "fn main() {",                 // 1
        "    let x: i32 = 1;",         // 2 (outer x)
        "    var i: i32 = 0;",         // 3
        "    while i < 1 {",           // 4
        "        let x: i32 = 2;",     // 5 (inner x)
        "        print(x);",           // 6 use -> inner (line 5)
        "        i = i + 1;",          // 7
        "    }",                       // 8
        "    print(x);",               // 9 use -> outer (line 2)
        "}",                           // 10
      ].join("\n") + "\n";
    const { records } = runEngine(enginePath, src, "--references");
    const innerUse = records.find((r) => r.refLine === 6 && r.kind === "local");
    const outerUse = records.find((r) => r.refLine === 9 && r.kind === "local");
    assert.ok(innerUse, "inner use resolved");
    assert.equal(innerUse.defLine, 5, "inner x -> line 5");
    assert.ok(outerUse, "outer use resolved");
    assert.equal(outerUse.defLine, 2, "outer x -> line 2");
  });

  // --- full JSON-RPC server cycle ---
  await test("server: initialize + didOpen -> publishDiagnostics", async () => {
    await withServer(async (rpc) => {
      const init = await rpc.request("initialize", { capabilities: {} });
      assert.ok(init.capabilities.hoverProvider, "hover capability");
      assert.ok(init.capabilities.definitionProvider, "definition capability");

      const diagPromise = rpc.waitNotify("textDocument/publishDiagnostics");
      rpc.notify("textDocument/didOpen", {
        textDocument: {
          uri: "file:///t.tv",
          languageId: "traveler",
          version: 1,
          text: "fn main() {\n    let x: i32 = 5\n    return;\n}\n",
        },
      });
      const diag = await diagPromise;
      assert.ok(diag.diagnostics.length >= 1, "diagnostics published");
      assert.equal(diag.diagnostics[0].source, "traveler");
    });
  });

  await test("server: hover returns signature", async () => {
    await withServer(async (rpc) => {
      await rpc.request("initialize", { capabilities: {} });
      rpc.notify("textDocument/didOpen", {
        textDocument: {
          uri: "file:///h.tv",
          languageId: "traveler",
          version: 1,
          text: "fn add(a: i32, b: i32) -> i32 { return a + b; }\nfn main() { print(add(1,2)); }\n",
        },
      });
      const hov = await rpc.request("textDocument/hover", {
        textDocument: { uri: "file:///h.tv" },
        position: { line: 1, character: 18 },
      });
      assert.ok(hov && hov.contents, "hover result");
      assert.ok(
        hov.contents.value.includes("fn add(a: i32, b: i32) -> i32"),
        "signature in hover",
      );
    });
  });

  await test("server: definition jumps to decl (scope-aware)", async () => {
    await withServer(async (rpc) => {
      await rpc.request("initialize", { capabilities: {} });
      const src =
        "fn helper(a: i32) -> i32 { return a; }\nfn main() {\n    let z: i32 = helper(1);\n    print(z);\n}\n";
      rpc.notify("textDocument/didOpen", {
        textDocument: {
          uri: "file:///d.tv",
          languageId: "traveler",
          version: 1,
          text: src,
        },
      });
      const def = await rpc.request("textDocument/definition", {
        textDocument: { uri: "file:///d.tv" },
        position: { line: 2, character: 18 },
      });
      assert.ok(def && def.range, "definition result");
      assert.equal(def.range.start.line, 0, "helper decl on line 1 (0-based 0)");
    });
  });
}

// --- minimal JSON-RPC client over the server's stdio ---
function withServer(fn) {
  const serverPath = join(__dirname, "..", "src", "server.js");
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, TVC_SELF: enginePath },
    stdio: ["pipe", "pipe", "inherit"],
  });
  let buf = Buffer.alloc(0);
  let nextId = 1;
  const pending = new Map();
  const notifyWaiters = new Map();

  child.stdout.on("data", (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    for (;;) {
      const he = buf.indexOf("\r\n\r\n");
      if (he < 0) break;
      const m = /Content-Length:\s*(\d+)/i.exec(buf.slice(0, he).toString("ascii"));
      if (!m) {
        buf = buf.slice(he + 4);
        continue;
      }
      const len = parseInt(m[1], 10);
      const start = he + 4;
      if (buf.length < start + len) break;
      const body = buf.slice(start, start + len).toString("utf8");
      buf = buf.slice(start + len);
      const msg = JSON.parse(body);
      if (msg.id !== undefined && pending.has(msg.id)) {
        pending.get(msg.id)(msg.result);
        pending.delete(msg.id);
      } else if (msg.method && notifyWaiters.has(msg.method)) {
        const w = notifyWaiters.get(msg.method);
        notifyWaiters.delete(msg.method);
        w(msg.params);
      }
    }
  });

  function frame(obj) {
    const p = Buffer.from(JSON.stringify(obj), "utf8");
    child.stdin.write(`Content-Length: ${p.length}\r\n\r\n`);
    child.stdin.write(p);
  }

  const rpc = {
    request(method, params) {
      const id = nextId++;
      return new Promise((resolve) => {
        pending.set(id, resolve);
        frame({ jsonrpc: "2.0", id, method, params });
      });
    },
    notify(method, params) {
      frame({ jsonrpc: "2.0", method, params });
    },
    waitNotify(method) {
      return new Promise((resolve) => notifyWaiters.set(method, resolve));
    },
  };

  return Promise.resolve()
    .then(() => fn(rpc))
    .finally(() => child.kill());
}

main().then(() => {
  console.log(`\n  tv-lsp: ${passed} passed, ${failed} failed\n`);
  if (failed > 0) process.exitCode = 1;
});
