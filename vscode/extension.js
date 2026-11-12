// Money Dashboard: shows the local money-tracker dashboard in a VS Code webview
// (Activity Bar sidebar, or an editor tab), starting the Python server when needed.
// Plain CommonJS, no build step, no dependencies.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

let out;              // OutputChannel "Money"
let server = null;    // the server process this window started, if any
let starting = null;  // promise while a start is in flight
let panel;            // editor-tab webview, if open
let watchdog;
const webviews = new Set();

// --- configuration ---------------------------------------------------------------------

function readPortFromEnv(root) {
  try {
    const m = fs.readFileSync(path.join(root, ".env"), "utf8").match(/^\s*MONEY_PORT\s*=\s*(\d+)/m);
    return m ? Number(m[1]) : 0;
  } catch (_) {
    return 0;
  }
}

function settings(context) {
  let baked = {};
  try {
    baked = JSON.parse(fs.readFileSync(path.join(context.extensionPath, "config.json"), "utf8"));
  } catch (_) { /* not installed via `python -m money vscode` */ }
  const s = vscode.workspace.getConfiguration("money");
  const isRepo = (p) => p && fs.existsSync(path.join(p, "money", "__main__.py"));
  const wsRoot = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath).find(isRepo);
  const root = s.get("projectPath") || wsRoot || baked.root;
  return {
    root,
    python: s.get("pythonPath") || baked.python || (process.platform === "win32" ? "python" : "python3"),
    port: s.get("port") || (root && readPortFromEnv(root)) || baked.port || 8765,
    isRepo: isRepo(root),
  };
}

// --- server ----------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ping(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api/version", timeout: 1000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function ensureServer(cfg) {
  if (starting) return starting;
  starting = (async () => {
    if (await ping(cfg.port)) return;
    if (!cfg.isRepo) {
      throw new Error("Can't find the money-tracker folder. Set \"money.projectPath\" in Settings, " +
        "or re-run `python -m money vscode` from the repo.");
    }
    out.appendLine(`Starting: ${cfg.python} -m money serve --port ${cfg.port}   (in ${cfg.root})`);
    let failure = null;
    server = cp.spawn(cfg.python, ["-m", "money", "serve", "--port", String(cfg.port)], {
      cwd: cfg.root,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    });
    server.stdout.on("data", (d) => out.append(d.toString()));
    server.stderr.on("data", (d) => out.append(d.toString()));
    server.on("error", (e) => { failure = e; out.appendLine(String(e)); });
    server.on("exit", (code) => {
      out.appendLine(`Server exited (${code}).`);
      failure = failure || new Error(`server exited with code ${code}`);
      server = null;
    });
    for (let i = 0; i < 80; i++) {
      await sleep(250);
      if (await ping(cfg.port)) return;
      if (failure) break;
    }
    throw new Error(`The dashboard server didn't start${failure ? ": " + failure.message : ""}. ` +
      "Run “Money: Show Server Log” for details.");
  })().finally(() => { starting = null; });
  return starting;
}

function post(port, urlPath) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: urlPath, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": 2 } },
      (res) => {
        let body = "";
        res.on("data", (d) => { body += d; });
        res.on("end", () => { try { resolve(JSON.parse(body)); } catch (_) { resolve({}); } });
      });
    req.on("error", reject);
    req.end("{}");
  });
}

// --- webviews --------------------------------------------------------------------------

function html(cfg, kind) {
  const nonce = crypto.randomBytes(16).toString("base64");
  const origin = `http://127.0.0.1:${cfg.port}`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${origin}; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  html, body { height: 100%; margin: 0; padding: 0; overflow: hidden; }
  iframe { border: 0; width: 100%; height: 100%; display: block; }
  #msg { padding: 12px 14px; font: 13px var(--vscode-font-family); color: var(--vscode-descriptionForeground); }
  #msg.err { color: var(--vscode-errorForeground); white-space: pre-wrap; }
</style>
</head>
<body>
<div id="msg">Starting the money dashboard…</div>
<iframe id="f" title="Money dashboard" hidden></iframe>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const ORIGIN = ${JSON.stringify(origin)};
  const KIND = ${JSON.stringify(kind)};
  const f = document.getElementById("f");
  const msg = document.getElementById("msg");
  function look() {
    const cls = document.body.classList;
    const theme = cls.contains("vscode-light") || cls.contains("vscode-high-contrast-light") ? "light" : "dark";
    const css = getComputedStyle(document.documentElement);
    const bg = css.getPropertyValue(KIND === "view" ? "--vscode-sideBar-background" : "--vscode-editor-background").trim();
    return { theme, bg };
  }
  function load() {
    const { theme, bg } = look();
    f.src = ORIGIN + "/?embed=1&theme=" + theme + (bg ? "&bg=" + encodeURIComponent(bg) : "") + "&t=" + Date.now();
  }
  window.addEventListener("message", (e) => {
    const m = e.data || {};
    if (e.source === f.contentWindow) {
      if (e.origin === ORIGIN && m.type === "money:open-external") vscode.postMessage({ type: "openExternal", url: m.url });
      return;
    }
    if (m.type === "load") { msg.hidden = true; f.hidden = false; load(); }
    else if (m.type === "reload") { if (!f.hidden) load(); }
    else if (m.type === "error") { f.hidden = true; msg.hidden = false; msg.className = "err"; msg.textContent = m.text; }
  });
  new MutationObserver(() => {
    if (f.contentWindow) f.contentWindow.postMessage({ type: "money:theme", ...look() }, ORIGIN);
  }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  vscode.postMessage({ type: "ready" });
</script>
</body>
</html>`;
}

function attach(webview, kind, context) {
  const cfg = settings(context);
  webview.options = { enableScripts: true };
  webview.html = html(cfg, kind);
  webviews.add(webview);
  webview.onDidReceiveMessage(async (m) => {
    if (m.type === "ready") {
      try {
        await ensureServer(cfg);
        webview.postMessage({ type: "load" });
      } catch (e) {
        webview.postMessage({ type: "error", text: e.message });
      }
    } else if (m.type === "openExternal" && typeof m.url === "string"
               && m.url.startsWith(`http://127.0.0.1:${cfg.port}/`)) {
      vscode.env.openExternal(vscode.Uri.parse(m.url));
    }
  });
}

class SidebarProvider {
  constructor(context) { this.context = context; }
  resolveWebviewView(view) {
    attach(view.webview, "view", this.context);
    view.onDidDispose(() => webviews.delete(view.webview));
  }
}

function openPanel(context) {
  if (panel) {
    panel.reveal();
    return;
  }
  panel = vscode.window.createWebviewPanel("money.panel", "Money", vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true });
  panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "money.svg");
  attach(panel.webview, "panel", context);
  const wv = panel.webview;
  panel.onDidDispose(() => { webviews.delete(wv); panel = undefined; });
}

function reloadAll() {
  for (const wv of webviews) wv.postMessage({ type: "reload" });
}

// --- activation ------------------------------------------------------------------------

function activate(context) {
  out = vscode.window.createOutputChannel("Money");
  context.subscriptions.push(out);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("money.dashboard", new SidebarProvider(context),
      { webviewOptions: { retainContextWhenHidden: true } }),

    vscode.commands.registerCommand("money.openPanel", () => openPanel(context)),
    vscode.commands.registerCommand("money.reload", reloadAll),
    vscode.commands.registerCommand("money.showLog", () => out.show()),

    vscode.commands.registerCommand("money.link", async () => {
      const cfg = settings(context);
      try {
        await ensureServer(cfg);
        vscode.env.openExternal(vscode.Uri.parse(`http://127.0.0.1:${cfg.port}/link`));
      } catch (e) {
        vscode.window.showErrorMessage(e.message);
      }
    }),

    vscode.commands.registerCommand("money.sync", async () => {
      const cfg = settings(context);
      try {
        await ensureServer(cfg);
        const r = await post(cfg.port, "/api/sync");
        if (r.error) vscode.window.showWarningMessage(`Money: ${r.error}`);
        else vscode.window.setStatusBarMessage(r.started ? "Money: syncing…" : "Money: already syncing", 4000);
      } catch (e) {
        vscode.window.showErrorMessage(e.message);
      }
    }),
  );

  // If the server dies (crash, another window closed it), bring it back for open views.
  watchdog = setInterval(async () => {
    if (!webviews.size || starting) return;
    const cfg = settings(context);
    if (await ping(cfg.port)) return;
    try {
      await ensureServer(cfg);
      reloadAll();
    } catch (_) { /* shown on next open */ }
  }, 20000);
}

function deactivate() {
  clearInterval(watchdog);
  if (server) server.kill();
}

module.exports = { activate, deactivate };
