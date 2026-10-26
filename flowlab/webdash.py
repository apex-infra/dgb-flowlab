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
from .infrastructure import collect_infrastructure
from .plays import list_plays
from .results import analyze_experiment
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



def mixer_lab_snapshot(path):
    """Aggregate Mixer Lab status; never expose financial records."""
    from . import db
    from .pool_recovery_cli import (
        validate_recovery_database,
        recovery_operational_diagnostic,
    )

    authorization = {
        "financial_actions": False,
        "automatic_recovery": False,
        "transaction_broadcast": False,
    }

    unavailable = {
        "status": "UNAVAILABLE",
        "schema": "V7 REQUIRED",
        "read_only": True,
        "pools": {"total": 0, "states": {}},
        "sessions": {"total": 0, "states": {}},
        "recovery": {
            "eligible_sessions": None,
            "lease": "UNKNOWN",
            "node_sync": "NOT CHECKED",
            "operational_readiness": "UNKNOWN",
        },
        "authorization": authorization,
    }

    if not validate_recovery_database(path):
        return unavailable

    def counts(conn, table):
        rows = conn.execute(
            f"SELECT state, COUNT(*) AS count "
            f"FROM {table} GROUP BY state ORDER BY state"
        ).fetchall()

        states = {row["state"]: row["count"] for row in rows}

        return {
            "total": sum(states.values()),
            "states": states,
        }

    try:
        conn = db.connect_readonly(path)

        try:
            pools = counts(conn, "pools")
            sessions = counts(conn, "pool_sessions")
        finally:
            conn.close()

        diagnostic_lines = []

        readiness = recovery_operational_diagnostic(
            path,
            out=diagnostic_lines.append,
        )

        observed = {}

        for line in diagnostic_lines:
            if ": " in line:
                key, _, value = line.partition(": ")
                observed[key] = value

        lease = observed.get(
            "Recovery lease availability",
            "UNKNOWN",
        )

        eligible_text = observed.get(
            "Session eligibility",
            "UNKNOWN",
        )

        eligible = (
            int(eligible_text)
            if eligible_text.isdecimal()
            else None
        )

        return {
            "status": "AVAILABLE",
            "schema": "V7",
            "read_only": True,
            "pools": pools,
            "sessions": sessions,
            "recovery": {
                "eligible_sessions": eligible,
                "lease": lease,
                "node_sync": "NOT CHECKED",
                "operational_readiness": readiness,
            },
            "authorization": authorization,
        }

    except Exception:
        # Never return database exception details to the browser.
        return unavailable



def mixer_lab_sessions_snapshot(path, *, limit=50):
    """Read-only allowlisted participant telemetry."""
    from . import db
    from .pool_recovery_cli import validate_recovery_database
    from .pool_research import assess_research_session
    from .pool_recovery_assessment import assess_session_recovery

    authorization = {
        "financial_actions": False,
        "automatic_recovery": False,
        "transaction_broadcast": False,
    }

    unavailable = {
        "status": "UNAVAILABLE",
        "read_only": True,
        "sessions": [],
        "truncated": False,
        "authorization": authorization,
    }

    if type(limit) is not int or not 1 <= limit <= 100:
        return unavailable

    if not validate_recovery_database(path):
        return unavailable

    try:
        conn = db.connect_readonly(path)
        try:
            rows = conn.execute(
                """
                SELECT s.id AS session_id,
                       s.pool_id,
                       s.state AS session_state,
                       p.state AS pool_state
                FROM pool_sessions s
                JOIN pools p ON p.id = s.pool_id
                ORDER BY s.created_at DESC, s.id
                LIMIT ?
                """,
                (limit + 1,),
            ).fetchall()

            records = []
            for row in rows[:limit]:
                research = assess_research_session(
                    conn, pool_id=row["pool_id"],
                    session_id=row["session_id"],
                )
                recovery = assess_session_recovery(
                    conn, pool_id=row["pool_id"],
                    session_id=row["session_id"],
                )

                records.append({
                    "pool_id": row["pool_id"],
                    "session_id": row["session_id"],
                    "pool_state": row["pool_state"],
                    "session_state": row["session_state"],
                    "lifecycle_state": research["lifecycle_state"],
                    "observation_count": research["observation_count"],
                    "supported": research["supported_observations"],
                    "invalidated": research["invalidated_observations"],
                    "recheck_required": research["recheck_required"],
                    "research_review_ready": bool(
                        research["research_review_ready"]
                    ),
                    "recovery_state": recovery["state"],
                })
        finally:
            conn.close()

        return {
            "status": "AVAILABLE",
            "read_only": True,
            "sessions": records,
            "truncated": len(rows) > limit,
            "authorization": authorization,
        }
    except Exception:
        return unavailable



def mixer_lab_configuration_actions(path, rpc=None):
    """Development-only pool/session setup; no wallet RPC."""
    import re
    from . import db
    from .pool_ledger import create_pool, create_session
    from .pool_recovery_cli import validate_recovery_database

    def identifier(value):
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
        ):
            raise ValueError(
                "Identifiers must be 1-64 letters, numbers, _ or -"
            )
        return value

    def existing_database():
        if not validate_recovery_database(path):
            raise ValueError("Existing Mixer Lab V7 database required")

    def new_pool(body):
        pool_id = identifier(body.get("pool_id"))
        reserve = identifier(body.get("reserve_wallet"))
        fees = identifier(body.get("fee_wallet"))

        if reserve == fees:
            raise ValueError("Reserve and fee wallets must differ")

        existing_database()
        conn = db.connect(path)
        try:
            create_pool(
                conn,
                pool_id=pool_id,
                reserve_wallet=reserve,
                fee_wallet=fees,
            )
        finally:
            conn.close()

        return {
            "created": True,
            "pool_id": pool_id,
            "state": "PAUSED",
            "financial_actions_authorized": False,
        }

    def new_session(body):
        pool_id = identifier(body.get("pool_id"))
        session_id = identifier(body.get("session_id"))
        fee = body.get("fee_basis_points")

        if type(fee) is not int or not 0 <= fee <= 10000:
            raise ValueError(
                "Fee must be an integer between 0 and 10000 basis points"
            )

        existing_database()
        conn = db.connect(path)
        try:
            # Operational V1 intentionally creates sessions
            # only under paused research pools.
            row = conn.execute(
                "SELECT state FROM pools WHERE id=?",
                (pool_id,),
            ).fetchone()

            if row is None or row["state"] != "PAUSED":
                raise ValueError(
                    "An existing PAUSED research pool is required"
                )

            create_session(
                conn,
                pool_id=pool_id,
                session_id=session_id,
                fee_basis_points=fee,
            )
        finally:
            conn.close()

        return {
            "created": True,
            "pool_id": pool_id,
            "session_id": session_id,
            "financial_actions_authorized": False,
        }


    def complete_registration(body):
        from .pool_registration import register_participant

        pool_id = identifier(body.get("pool_id"))
        session_id = identifier(body.get("session_id"))
        payouts = body.get("payouts")

        if rpc is None or not callable(
            getattr(rpc, "validate_address", None)
        ):
            raise ValueError(
                "DigiByte address-validation RPC unavailable"
            )

        if not isinstance(payouts, list):
            raise ValueError("Payout instructions must be a list")

        # Never echo payout addresses in responses or errors.
        if not 1 <= len(payouts) <= 10:
            raise ValueError("Expected 1-10 payout instructions")

        if any(
            not isinstance(item, dict)
            or set(item) != {"address", "allocation_bps"}
            or not isinstance(item["address"], str)
            or not 1 <= len(item["address"]) <= 128
            for item in payouts
        ):
            raise ValueError("Invalid payout instruction format")

        existing_database()
        conn = db.connect(path)

        try:
            row = conn.execute(
                """
                SELECT fee_basis_points
                FROM pool_sessions
                WHERE id = ? AND pool_id = ?
                """,
                (session_id, pool_id),
            ).fetchone()

            if row is None:
                raise ValueError("Existing session not found")

            result = register_participant(
                conn,
                rpc,
                pool_id=pool_id,
                session_id=session_id,
                payouts=payouts,
                fee_basis_points=row["fee_basis_points"],
                existing_session=True,
            )
        finally:
            conn.close()

        return {
            "registered": True,
            "pool_id": result["pool_id"],
            "session_id": result["session_id"],
            "payout_count": result["payout_count"],
            "registration_sealed": True,
            "financial_actions_authorized": False,
        }

    return {
        "mixer_create_pool": new_pool,
        "mixer_create_session": new_session,
        "mixer_complete_registration": complete_registration,
    }


def make_server(db_path, exp_id=None, rpc=None, wallets=(), port=8787, builder=None,
                control=True, sleep=None, wallet_roles=None,
                mixer_db_path=None):
    mixer_path = mixer_db_path if mixer_db_path is not None else db_path
    wallet_roles = {
        k: list(v)
        for k, v in (wallet_roles or {}).items()
    }
    ctl = Controller(
        db_path, rpc, builder, wallets, sleep, control,
        wallet_roles=wallet_roles,
    )

    # An explicit, separate development database is mandatory.
    # A general-purpose production database is not enabled here.
    mixer_configuration_enabled = (
        control
        and mixer_db_path is not None
        and Path(mixer_db_path).name == "mixerlab-dev.db"
        and Path(mixer_db_path).expanduser().resolve()
            != Path(db_path).expanduser().resolve()
    )

    if mixer_configuration_enabled:
        ctl.actions.update(
            mixer_lab_configuration_actions(mixer_path, rpc)
        )

    token = secrets.token_urlsafe(24)
    cache = {"at": 0.0, "value": None}
    infrastructure_cache = {"at": 0.0, "value": None}

    def current_balances():
        if not (rpc and wallets):
            return None
        if time.monotonic() - cache["at"] > BALANCE_TTL:
            cache["value"] = dashboard.balances(rpc, wallets)
            cache["at"] = time.monotonic()
        return cache["value"]

    def current_infrastructure():
        if not (rpc and wallets):
            return None
        if time.monotonic() - infrastructure_cache["at"] > BALANCE_TTL:
            infrastructure_cache["value"] = collect_infrastructure(
                rpc,
                wallets,
                wallet_roles=wallet_roles,
            )
            infrastructure_cache["at"] = time.monotonic()
        return infrastructure_cache["value"]

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
                results = (
                    analyze_experiment(
                        snap,
                        wallet_roles=wallet_roles,
                    )
                    if snap
                    else None
                )
                if snap:  # send only what the page draws, never the stored config
                    keep = ("id", "description", "state", "state_reason", "started_at", "completed_at")
                    snap["exp"] = {k: snap["exp"][k] for k in keep}
                return self._json(200, {"snapshot": snap, "results": results,
                                        "infrastructure": current_infrastructure(),
                                        "extras": extras, "balances": current_balances(),
                                        "wallets": list(wallets), "wallet_roles": wallet_roles,
                                        "plays": list_plays(), "control": control,
                                        "runner": {"active": ctl.active(), "exp": ctl.exp},
                                        "log": list(ctl.log), "server_time": now.isoformat()})
            if path == "/api/mixer-lab":
                report = mixer_lab_snapshot(mixer_path)
                report["configuration_enabled"] = (
                    mixer_configuration_enabled
                    and report["status"] == "AVAILABLE"
                )
                return self._json(200, report)
            if path == "/api/mixer-lab/sessions":
                return self._json(
                    200, mixer_lab_sessions_snapshot(mixer_path)
                )
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


def serve(db_path, exp_id=None, rpc=None, wallets=(), port=8787, builder=None, control=True,
          out=print, wallet_roles=None, mixer_db_path=None):
    srv = make_server(
        db_path, exp_id, rpc, wallets, port, builder, control,
        wallet_roles=wallet_roles,
        mixer_db_path=mixer_db_path,
    )
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
