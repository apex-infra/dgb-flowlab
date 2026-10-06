"""
webdash.py -- the dashboard as a local web page, with the operator controls.

Safety, in layers:
  * binds to 127.0.0.1 only; there is no option to change that
  * a request must carry a Host header for this machine and port (DNS-rebinding guard)
  * reading is open to the page; every change is a POST to /api/do/<action> that also needs
    the per-run secret token (only the page we serve contains it), a same-origin Origin
    header and a JSON body; anything else is refused
  * the page loads only its own files (no external scripts, fonts or images)
  * the actions are the engine's own, so one-time approval, verification and the
    write-ahead journal apply exactly as on the command line
Start it with --read-only to switch every action off.
"""

import csv
import hmac
import io
import json
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dashboard
from .webctl import Controller

WEB = Path(__file__).with_name("web")
STATIC = {"/app.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8",
          "/form.js": "text/javascript; charset=utf-8"}
BALANCE_TTL = 4.0
MAX_BODY = 65536
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def export_run(snap, fmt):
    """A finished picture of one run: hops, fees, txids and events. Integer satoshis, no credentials."""
    keep = ("id", "description", "state", "state_reason", "started_at", "completed_at")
    hops = [{"hop": j["seq"], "from": j["from"], "to": j["to"], "state": j["state"], "planned_sats": j["planned"],
             "sent_sats": j["amount"], "fee_sats": j["fee"], "txid": j["txid"], "confirmations": j["confs"]}
            for f in snap["flows"] for j in f["jobs"]]
    if fmt == "csv":
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(hops[0]) if hops else ["hop"], lineterminator="\n")
        w.writeheader()
        w.writerows(hops)
        return out.getvalue().encode(), "text/csv; charset=utf-8"
    doc = {"experiment": {k: snap["exp"][k] for k in keep},
           "totals": {"hops": len(hops), "confirmed": sum(h["state"] == "CONFIRMED" for h in hops),
                      "fee_sats": sum(h["fee_sats"] or 0 for h in hops)},
           "hops": hops, "events": snap["events"]}
    return json.dumps(doc, indent=2).encode(), "application/json"


def make_server(db_path, exp_id=None, rpc=None, wallets=(), port=8787, builder=None,
                control=True, sleep=None):
    ctl = Controller(db_path, rpc, builder, wallets, sleep, control)
    token = secrets.token_urlsafe(24)
    cache = {"at": 0.0, "value": None}

    def current_balances():
        if not (rpc and wallets):
            return None
        if time.monotonic() - cache["at"] > BALANCE_TTL:
            cache["value"] = dashboard.balances(rpc, wallets)
            cache["at"] = time.monotonic()
        return cache["value"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "flowlab"
        sys_version = ""

        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _origins(self):
            p = self.server.server_address[1]
            return (f"127.0.0.1:{p}", f"localhost:{p}")

        def _host_ok(self):
            return self.headers.get("Host", "") in self._origins()

        def _handle(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path, _, query = self.path.partition("?")
            if path == "/":
                page = (WEB / "index.html").read_text(encoding="utf-8")
                page = page.replace("{{TOKEN}}", token).replace("{{CONTROL}}", "1" if control else "0")
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path in STATIC:
                return self._send(200, (WEB / path[1:]).read_bytes(), STATIC[path])
            if path == "/api/snapshot":
                want = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("exp") or exp_id
                now = datetime.now(timezone.utc)
                try:
                    snap = dashboard.snapshot(db_path, want, now)
                    extras = dashboard.extras(db_path)
                except Exception:  # noqa: BLE001  (no database yet, locked, ...)
                    snap, extras = None, None
                if snap:  # send only what the page draws, never the stored config
                    keep = ("id", "description", "state", "state_reason", "started_at", "completed_at")
                    snap["exp"] = {k: snap["exp"][k] for k in keep}
                return self._json(200, {"snapshot": snap, "extras": extras, "balances": current_balances(),
                                        "wallets": list(wallets), "control": control,
                                        "runner": {"active": ctl.active(), "exp": ctl.exp},
                                        "log": list(ctl.log), "server_time": now.isoformat()})
            if path == "/api/export":
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                fmt = q.get("fmt", "json")
                try:
                    snap = dashboard.snapshot(db_path, q.get("exp") or exp_id, datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001
                    snap = None
                if fmt not in ("json", "csv") or not snap:
                    return self._send(404, b"not found", "text/plain")
                body, ctype = export_run(snap, fmt)
                name = "".join(c for c in snap["exp"]["id"] if c.isalnum() or c in "-_")
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})
            return self._send(404, b"not found", "text/plain")

        do_GET = do_HEAD = _handle

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.partition("?")[0]
            if not path.startswith("/api/do/"):
                return self._json(404, {"error": "not found"})
            if not control:
                return self._json(403, {"error": "this dashboard is read-only"})
            origin = self.headers.get("Origin")
            if origin is not None and origin not in [f"http://{h}" for h in self._origins()]:
                return self._json(403, {"error": "bad origin"})
            if not hmac.compare_digest(self.headers.get("X-Flowlab-Token", ""), token):
                return self._json(403, {"error": "missing or wrong token; reload the page"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "JSON only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= MAX_BODY:
                    return self._json(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            fn = ctl.actions.get(path[len("/api/do/"):])
            if fn is None:
                return self._json(404, {"error": "unknown action"})
            try:
                return self._json(200, fn(body))
            except Exception as err:  # noqa: BLE001  (messages are free of credentials by design)
                return self._json(400, {"error": f"{type(err).__name__}: {err}"})

        def _refuse(self):
            self._send(405, b"read-only", "text/plain", {"Allow": "GET, HEAD, POST"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.controller, srv.token = ctl, token
    return srv


def serve(db_path, exp_id=None, rpc=None, wallets=(), port=8787, builder=None, control=True, out=print):
    srv = make_server(db_path, exp_id, rpc, wallets, port, builder, control)
    mode = "full control" if control else "read-only"
    out(f"dashboard: http://127.0.0.1:{srv.server_address[1]}   ({mode}; this computer only; Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        out("stopping: finishing the current step first...")
    finally:
        srv.controller.close()
        srv.server_close()
    return 0
