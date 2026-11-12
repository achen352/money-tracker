"""Local dashboard + Plaid Link server (stdlib http.server). Binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from money import config, db, plaid_api, sync, views
from money.plaid_api import NotConfigured, PlaidClient, PlaidError

STATIC = {"app.js", "style.css"}
_last_sync_error: dict = {"message": None}


def _background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _run_sync(item_id: str | None = None) -> None:
    try:
        results = sync.sync_all(only_item=item_id)
        bad = [r.line() for r in results if r.status != "ok"]
        _last_sync_error["message"] = "; ".join(bad) or None
    except Exception as e:  # surfaced in the dashboard header
        _last_sync_error["message"] = str(e)


def version_token() -> str:
    parts = []
    path = config.db_path()
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            parts.append(str(p.stat().st_mtime_ns))
        except OSError:
            parts.append("0")
    if config.VIEWS_DIR.exists():
        for f in sorted(config.VIEWS_DIR.glob("*.json")):
            parts.append(f"{f.name}:{f.stat().st_mtime_ns}")
    parts.append("syncing" if sync.is_syncing() else "idle")
    return str(hash("|".join(parts)))


class Handler(BaseHTTPRequestHandler):
    server_version = "money-tracker"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # --- helpers --------------------------------------------------------------------------

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _file(self, name: str) -> None:
        path = config.WEB_DIR / name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(min(length, 1_000_000)))

    def _guard(self, write: bool = False) -> bool:
        if not self._allowed_host() or (write and not self._same_origin()):
            self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            return False
        return True

    # --- routes ---------------------------------------------------------------------------

    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        if path == "/":
            return self._file("index.html")
        if path == "/link":
            return self._file("link.html")
        if path.startswith("/static/") and path[len("/static/"):] in STATIC:
            return self._file(path[len("/static/"):])
        if path == "/api/version":
            return self._json({"v": version_token(), "syncing": sync.is_syncing()})
        if path == "/api/dashboard":
            raw = (qs.get("range") or ["6"])[0]
            months = None if raw == "all" else max(1, min(int(raw), 120)) if raw.isdigit() else 6
            data = views.dashboard(months)
            data.update(
                demo=config.is_demo(),
                syncing=sync.is_syncing(),
                sync_error=_last_sync_error["message"],
                plaid_configured=bool(config.plaid_settings()["client_id"]),
                plaid_env=config.plaid_settings()["env"],
                version=version_token(),
            )
            return self._json(data)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._guard(write=True):
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError:
            return self._json({"error": "invalid JSON"}, 400)
        try:
            if path == "/api/sync":
                if config.is_demo():
                    return self._json({"error": "Demo mode: nothing to sync."}, 400)
                if sync.is_syncing():
                    return self._json({"started": False, "message": "Already syncing"})
                _background(_run_sync)
                return self._json({"started": True})
            if path == "/api/link/token":
                client = PlaidClient.from_env()
                access_token = None
                if body.get("item_id"):
                    info = plaid_api.load_items().get(body["item_id"])
                    if not info:
                        return self._json({"error": "Unknown item"}, 404)
                    access_token = info["access_token"]
                token = client.create_link_token(body.get("mode", "bank"), access_token)
                return self._json({"link_token": token, "env": client.env})
            if path == "/api/link/exchange":
                client = PlaidClient.from_env()
                access_token, item_id = client.exchange_public_token(body["public_token"])
                inst = body.get("institution") or {}
                products = ["investments"] if body.get("mode") == "investments" else ["transactions"]
                plaid_api.store_item(item_id, access_token, inst.get("name") or "Unknown institution",
                                     inst.get("institution_id"), products)
                _background(_run_sync, item_id)
                return self._json({"ok": True, "item_id": item_id, "institution": inst.get("name")})
            if path == "/api/link/updated":
                item_id = body.get("item_id")
                if item_id not in plaid_api.load_items():
                    return self._json({"error": "Unknown item"}, 404)
                _background(_run_sync, item_id)
                return self._json({"ok": True})
        except NotConfigured as e:
            return self._json({"error": str(e)}, 400)
        except PlaidError as e:
            return self._json({"error": e.display_message or str(e), "code": e.error_code}, 502)
        self._send(404, b"not found", "text/plain")

    def do_DELETE(self):
        if not self._guard(write=True):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/views/"):
            target = views.custom_path(path[len("/api/views/"):])
            if target and target.exists():
                target.unlink()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        self._send(404, b"not found", "text/plain")


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int | None = None, open_path: str | None = None, auto_sync: bool = True) -> int:
    port = port or config.port()
    url = f"http://127.0.0.1:{port}"
    try:
        httpd = make_server(port)
    except OSError:
        print(f"Port {port} is busy - the dashboard is probably already running at {url}")
        if open_path is not None:
            webbrowser.open(url + open_path)
        return 0

    db.connect().close()  # make sure the schema exists
    config.VIEWS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Dashboard: {url}{'  (DEMO DATA)' if config.is_demo() else ''}")
    print(f"Link accounts (open in your normal browser): {url}/link")
    print("VS Code: click the Money icon in the Activity Bar (install it once: python -m money vscode).")

    if auto_sync and not config.is_demo() and plaid_api.load_items() and config.plaid_settings()["client_id"]:
        conn = db.connect()
        last = db.get_meta(conn, "last_sync")
        conn.close()
        from datetime import datetime
        if not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() > 6 * 3600:
            print("Data is more than 6 hours old - syncing in the background...")
            _background(_run_sync)

    if open_path is not None:
        webbrowser.open(url + open_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(int(os.environ.get("MONEY_PORT", 0)) or None))
