// client.js — minimal VS Code extension entry point for the Traveler LSP.
//
// Launches the editor-agnostic server (../src/server.js) over stdio and wires
// it to .tv documents. The server delegates all language intelligence to the
// tvc_self engine (configurable via traveler.enginePath).

const path = require("path");
const { workspace } = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;

function activate(context) {
  const enginePath =
    workspace.getConfiguration("traveler").get("enginePath") || "/tmp/tvc_self";

  const serverModule = context.asAbsolutePath(
    path.join("..", "src", "server.js"),
  );

  const serverOptions = {
    run: {
      module: serverModule,
      transport: TransportKind.stdio,
      options: { env: { ...process.env, TVC_SELF: enginePath } },
    },
    debug: {
      module: serverModule,
      transport: TransportKind.stdio,
      options: { env: { ...process.env, TVC_SELF: enginePath } },
    },
  };

  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "traveler" }],
    synchronize: {
      fileEvents: workspace.createFileSystemWatcher("**/*.tv"),
    },
  };

  client = new LanguageClient(
    "traveler",
    "Traveler Language Server",
    serverOptions,
    clientOptions,
  );
  client.start();
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
