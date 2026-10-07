import http.client
import json
import os
import re
import threading
import time
from decimal import Decimal

from flowlab.webdash import make_server
from tests.test_dashboard import DashBase
from tests.test_sweep import cfg_sweep as _sweep


def cfg_loop():
    """A repeat config that loops back to the source: valid for the CLI, refused by the page."""
    c = _sweep()
    c.pop("workload", None)
    c.pop("randomization", None)
    return c


def cfg_experimental():
    c = {
        "flows": [{
            "description": "experimental",
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "flow_wallets": ["flab_stage", "flab_a", "flab_b"],
            "destination_wallet": "flab_dest",
            "allocation_sats": 300_000_000,
            "experimental_topology": {
                "transitions": [
                    {"from": "flab_stage", "to": "flab_a"},
                    {"from": "flab_stage", "to": "flab_b"},
                    {"from": "flab_a", "to": "flab_a"},
                    {"from": "flab_a", "to": "flab_b"},
                    {"from": "flab_b", "to": "flab_a"},
                    {"from": "flab_b", "to": "flab_b"},
                ]
            },
        }],
        "workload": {
            "mode": "count",
            "jobs": 4,
            "amount_sats_min": 50_000_000,
            "amount_sats_max": 100_000_000,
            "delay_seconds_min": 0,
            "delay_seconds_max": 5,
        },
        "confirmations_required": 2,
        "fee_policy": {"type": "minimum"},
        "address_policy": "new",
        "randomization": {
            "enabled": True,
            "model": "uniform",
            "seed": 2262026,
        },
        "finalization": {
            "mode": "sweep_workers_to_destination",
        },
    }
    return c


def cfg_sweep():
    """What the page builds: source -> a -> b -> destination, then everything swept on."""
    c = cfg_loop()
    fl = c["flows"][0]
    fl.pop("repeat", None)
    fl["flow_wallets"], fl["destination_wallet"] = ["flab_a", "flab_b"], "flab_dest"
    fl["allocation_sats"] = 200_000_000
    fl["transfers"] = [{"from": "flab_source", "to": "flab_a", "amount_sats": 200_000_000, "delay_seconds": 0},
                       {"from": "flab_a", "to": "flab_b", "amount_sats": "all", "delay_seconds": 0},
                       {"from": "flab_b", "to": "flab_dest", "amount_sats": "all", "delay_seconds": 0}]
    return c


class Rpc:
    def __init__(self, fail=False):
        self.fail = fail

    def get_balances(self, w):
        if self.fail:
            raise RuntimeError("node down")
        return {"mine": {"trusted": Decimal("1.5")}}


class WebBase(DashBase):
    control = True
    use_chain = False

    def serve(self, rpc=None, wallets=("flab_a", "flab_b"), sleep=None, control=True,
              db=None, builder=None, wallet_roles=None):
        self.srv = make_server(
            db or self.db, None, rpc, wallets, port=0,
            builder=builder, control=control, sleep=sleep,
            wallet_roles=wallet_roles,
        )
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.controller.close)
        return self.srv

    def request(self, method, path, body=None, headers=None, host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, dict(r.getheaders()), out

    def get(self, path="/", **kw):
        return self.request("GET", path, **kw)

    def do(self, action, body=None, token=True, **kw):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Flowlab-Token"] = self.srv.token
        h.update(kw.pop("headers", {}))
        s, hd, out = self.request("POST", "/api/do/" + action, body or {}, headers=h, **kw)
        try:
            return s, json.loads(out)
        except ValueError:  # plain-text refusals (wrong host)
            return s, {"text": out}

    def snap(self, exp=None):
        return json.loads(self.get("/api/snapshot" + (f"?exp={exp}" if exp else ""))[2])

    def until(self, test, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            v = test()
            if v:
                return v
            time.sleep(0.05)
        self.fail("timed out waiting")


class WebReadTests(WebBase):
    def start(self, rpc=None, db=None):
        self.exp = self.make()
        self.call("run", self.exp)
        self.serve(rpc, db=db)

    def test_binds_to_loopback_only(self):
        self.start()
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_page_loads_only_its_own_files_and_carries_the_token(self):
        self.start()
        s, h, b = self.get()
        page = b.decode()
        self.assertEqual(s, 200)
        self.assertEqual(sorted(re.findall(r'(?:src|href)="([^"]+)"', page)), ["/app.css", "/app.js", "/form.js"])
        self.assertNotIn("<script>", page)
        self.assertIn(self.srv.token, page)
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        for p in ("/app.css", "/app.js", "/form.js"):
            self.assertEqual(self.get(p)[0], 200, p)

    def test_page_contains_experimental_monitor_anchors(self):
        self.start()
        page = self.get()[2].decode()

        for want in (
            'id="m-generated"',
            'id="m-mode"',
            'id="m-hops-l"',
            'id="panel-experimental"',
            "transaction jobs",
            "total network fees",
        ):
            self.assertIn(want, page)

        app = self.get("/app.js")[2].decode()
        css = self.get("/app.css")[2].decode()

        for want in (
            "walletRoleGroup",
            "wallet-group",
            "wallet-role",
            "wallet-balance",
        ):
            self.assertIn(want, app)

        for want in (
            ".wallet-group",
            ".wallet-role.role-reserve",
            ".wallet-role.role-stage",
            ".wallet-role.role-worker",
            ".wallet-role.role-hub",
            ".wallet-role.role-destination",
        ):
            self.assertIn(want, css)

    def test_form_script_contains_both_experiment_modes(self):
        self.start()
        script = self.get("/form.js")[2].decode()

        for want in (
            "Deterministic",
            "Experimental",
            "experimental_topology",
            "amount_sats_min",
            "amount_sats_max",
            "delay_seconds_min",
            "delay_seconds_max",
            "seeded_deterministic",
            'roleWallets("reserve")',
            'roleWallets("destinations")',
            "workloadWallets()",
            "Reserve / funding wallet",
            "Passes through workers / hubs",
            "Stage wallets are reserved for Experimental and Play allocation",
        ):
            self.assertIn(want, script)


    def test_experimental_form_includes_finalization_contract(self):
        self.start()
        status, _, body = self.get("/form.js")
        self.assertEqual(status, 200)

        text = body.decode()
        self.assertIn("finalization:", text)
        self.assertIn('mode: "sweep_workers_to_destination"', text)

    def test_experimental_form_generates_fresh_seed(self):
        self.start()
        status, _, body = self.get("/form.js")
        self.assertEqual(status, 200)

        text = body.decode()
        self.assertIn("crypto.getRandomValues", text)
        self.assertIn("new Uint32Array(1)", text)
        self.assertIn("New seed", text)
        self.assertNotIn('num("f-seed", "2262026"', text)


    def test_each_server_has_its_own_secret(self):
        self.start()
        first = self.srv.token
        self.serve()
        self.assertNotEqual(first, self.srv.token)
        self.assertGreaterEqual(len(first), 24)

    def test_snapshot_is_json_without_the_stored_config(self):
        self.start(Rpc())
        data = self.snap()
        self.assertEqual(set(data["snapshot"]["exp"]), {"id", "description", "state", "state_reason",
                                                         "started_at", "completed_at"})
        self.assertNotIn("config_json", json.dumps(data))
        self.assertEqual(data["balances"], {"flab_a": 150_000_000, "flab_b": 150_000_000})
        self.assertTrue(data["control"])
        self.assertEqual(data["runner"]["active"], False)

    def test_unreachable_node_gives_dashes_not_an_error(self):
        self.start(Rpc(fail=True))
        self.assertEqual(self.snap()["balances"], {"flab_a": None, "flab_b": None})


    def test_wallet_balance_refreshes_after_cache_expires(self):
        from unittest.mock import patch

        class MovingRpc:
            def __init__(self):
                self.amounts = {
                    "flab_a": Decimal("0"),
                    "flab_b": Decimal("0"),
                }

            def get_balances(self, wallet):
                return {"mine": {"trusted": self.amounts[wallet]}}

        rpc = MovingRpc()

        # Keep the first result cached so we can prove the dashboard really
        # holds it until the TTL says it is stale.
        with patch("flowlab.webdash.BALANCE_TTL", 3600):
            self.start(rpc)

            first = self.snap()["balances"]
            self.assertEqual(first["flab_a"], 0)

            rpc.amounts["flab_a"] = Decimal("0.22339458")

            cached = self.snap()["balances"]
            self.assertEqual(cached["flab_a"], 0)

            # Force that same cache entry to be considered expired.
            with patch("flowlab.webdash.BALANCE_TTL", -1):
                fresh = self.snap()["balances"]

            self.assertEqual(fresh["flab_a"], 22_339_458)
            self.assertEqual(fresh["flab_b"], 0)

    def test_wrong_host_header_is_refused_everywhere(self):
        self.start()
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1"):
            self.assertEqual(self.get("/api/snapshot", host=host)[0], 403, host)
            self.assertEqual(self.get("/", host=host)[0], 403, host)
            self.assertEqual(self.do("stop", host=host)[0], 403, host)

    def test_put_delete_patch_options_are_refused(self):
        self.start()
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request(m, "/api/snapshot")[0], 405, m)

    def test_unknown_paths_are_404_and_reads_write_nothing(self):
        self.start()
        before = self.digest()
        for p in ("/etc/passwd", "/../flowlab.db", "/web/index.html", "/app.js/../x"):
            self.assertEqual(self.get(p)[0], 404, p)
        self.snap()
        self.assertEqual(self.digest(), before)

    def test_form_script_contains_play_mode_and_server_compiler(self):
        self.start()
        form = self.get("/form.js")[2].decode()
        app = self.get("/app.js")[2].decode()

        self.assertIn('"Play"', form)
        self.assertIn('"compile_play"', form)
        self.assertIn('"f-stage"', form)
        self.assertIn("Allocation wallet", form)
        self.assertIn("window.flowPlays", app)
        self.assertIn("window.flowPlays", form)
        self.assertIn("window.flowWalletRoles", app)
        self.assertIn("walletRoles()", form)
        self.assertIn('roleWallets("reserve")', form)
        self.assertIn('roleWallets("stage")', form)
        self.assertIn('roleWallets("destinations")', form)
        self.assertIn("orderedWalletNames", app)
        self.assertIn("walletRoleName", app)
        self.assertIn("staged_accounting", app)
        self.assertIn("Approved principal", app)
        self.assertIn("Principal after fees", app)
        self.assertIn("Destination receipts", app)
        self.assertIn("reconciled ✓", app)

    def test_snapshot_exposes_wallet_roles(self):
        roles = {
            "reserve": ["flab_a"],
            "stage": ["flab_b"],
            "workers": [],
            "hubs": [],
            "destinations": [],
        }
        self.serve(wallet_roles=roles)

        self.assertEqual(self.snap()["wallet_roles"], roles)

    def test_missing_database_serves_an_empty_snapshot(self):
        self.serve(db="/nonexistent/none.db")
        data = self.snap()
        self.assertIsNone(data["snapshot"])


class WebControlTests(WebBase):
    def setUp(self):
        super().setUp()
        from flowlab.tx_builder import TxBuilder
        self.builder = TxBuilder(self.chain, self.chain.wallets, max_fee_sats=10_000_000)

    def up(self, **kw):
        kw.setdefault("sleep", lambda s: self.chain.mine(2))
        return self.serve(self.chain, wallets=tuple(self.chain.wallets), builder=self.builder, **kw)

    def wait_idle(self):
        self.until(lambda: not self.srv.controller.active())

    def state(self, exp):
        return self.snap(exp)["snapshot"]["exp"]["state"]

    def test_refusals_token_origin_type_size(self):
        self.up()
        self.assertEqual(self.do("stop", token=False)[0], 403)
        self.assertEqual(self.do("stop", headers={"X-Flowlab-Token": "nope"})[0], 403)
        self.assertEqual(self.do("stop", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.do("stop", headers={"Origin": f"http://localhost:{self.port}"})[0], 200)
        s, h, b = self.request("POST", "/api/do/stop", {}, headers={"X-Flowlab-Token": self.srv.token,
                                                                      "Content-Type": "text/plain"})
        self.assertEqual(s, 415)
        s, h, b = self.request("POST", "/api/do/new", raw=b"x" * 70000, headers={
            "X-Flowlab-Token": self.srv.token, "Content-Type": "application/json"})
        self.assertEqual(s, 413)
        self.assertEqual(self.do("nonsense")[0], 404)
        self.assertEqual(self.do("stop", headers={"Content-Type": "application/json"})[0], 200)

    def test_read_only_switches_every_action_off(self):
        self.up(control=False)
        before = self.digest() if os.path.exists(self.db) else None
        for a in ("new", "approve", "run", "stop", "clear_stop", "resolve"):
            self.assertEqual(self.do(a)[0], 403, a)
        self.assertEqual(self.snap()["control"], False)
        self.assertEqual(self.digest() if os.path.exists(self.db) else None, before)

    def test_new_approve_run_to_completion_through_the_page(self):
        self.up()
        s, out = self.do("new", {"config": cfg_sweep(), "description": "from page"})
        self.assertEqual(s, 200, out)
        exp, h = out["exp"], out["hash"]
        self.assertEqual(self.do("run", {"exp": exp})[0], 400)          # not approved yet
        self.assertEqual(self.do("approve", {"exp": exp, "hash": "0" * 64})[0], 400)
        self.assertEqual(self.state(exp), "CONFIGURED")
        self.assertEqual(self.do("approve", {"exp": exp, "hash": h})[0], 200)
        self.assertEqual(self.do("approve", {"exp": exp, "hash": h})[0], 400)   # one time only
        self.assertEqual(self.do("run", {"exp": exp})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(exp), "COMPLETE")
        self.assertEqual(self.snap(exp)["snapshot"]["flows"][0]["jobs"][-1]["state"], "CONFIRMED")
        self.assertEqual(self.do("run", {"exp": exp})[0], 400)          # finished

    def test_new_enforces_deterministic_wallet_roles(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a"],
            "hubs": ["flab_b"],
            "destinations": ["flab_dest"],
        }

        # The normal deterministic route is valid under the role contract.
        self.up(wallet_roles=roles)
        s, out = self.do("new", {"config": cfg_sweep()})
        self.assertEqual(s, 200, out)

    def test_new_refuses_wrong_deterministic_wallet_roles(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a"],
            "hubs": ["flab_b"],
            "destinations": ["flab_dest"],
        }
        self.up(wallet_roles=roles)

        # Stage cannot act as deterministic reserve/source.
        bad = cfg_sweep()
        fl = bad["flows"][0]
        fl["source_wallet"] = "flab_stage"
        fl["transfers"][0]["from"] = "flab_stage"

        s, out = self.do("new", {"config": bad})
        self.assertEqual(s, 400)
        self.assertIn("reserve role", out["error"])

        # Stage cannot act as an intermediate deterministic wallet.
        bad = cfg_sweep()
        fl = bad["flows"][0]
        fl["flow_wallets"] = ["flab_a", "flab_stage"]
        fl["transfers"][1]["to"] = "flab_stage"
        fl["transfers"][2]["from"] = "flab_stage"

        s, out = self.do("new", {"config": bad})
        self.assertEqual(s, 400)
        self.assertIn("worker or hub role", out["error"])

        # A hub/worker cannot act as the configured final destination.
        bad = cfg_sweep()
        fl = bad["flows"][0]
        fl["destination_wallet"] = "flab_b"
        fl["flow_wallets"] = ["flab_a"]
        fl["transfers"] = [
            {
                "from": "flab_source",
                "to": "flab_a",
                "amount_sats": 200_000_000,
                "delay_seconds": 0,
            },
            {
                "from": "flab_a",
                "to": "flab_b",
                "amount_sats": "all",
                "delay_seconds": 0,
            },
        ]

        s, out = self.do("new", {"config": bad})
        self.assertEqual(s, 400)
        self.assertIn("destination role", out["error"])

    def test_new_refuses_bad_and_randomising_configs(self):
        self.up()
        self.assertEqual(self.do("new", {"config": "x"})[0], 400)
        bad = cfg_loop()
        bad["flows"][0]["repeat"]["randomize"] = True
        s, out = self.do("new", {"config": bad})
        self.assertEqual(s, 400)
        bad = cfg_sweep()
        bad["flows"][0]["jitter_seconds"] = 5
        self.assertEqual(self.do("new", {"config": bad})[0], 400)

    def test_experimental_config_requires_finalization(self):
        self.up()

        cfg = cfg_experimental()
        del cfg["finalization"]

        s, out = self.do("new", {"config": cfg})

        self.assertEqual(s, 400)
        self.assertIn("finalization required", out["error"])

    def test_experimental_config_rejects_unknown_finalization_mode(self):
        self.up()

        cfg = cfg_experimental()
        cfg["finalization"]["mode"] = "something_else"

        s, out = self.do("new", {"config": cfg})

        self.assertEqual(s, 400)
        self.assertIn("finalization.mode", out["error"])

    def test_new_accepts_experimental_config(self):
        self.up()

        s, out = self.do("new", {
            "config": cfg_experimental(),
            "description": "experimental from page",
        })

        self.assertEqual(s, 200, out)
        self.assertIn("exp", out)
        self.assertIn("hash", out)
        self.assertIn("randomization: ENABLED", out["text"])

    def test_snapshot_exposes_play_catalog(self):
        self.up()

        data = self.snap()

        self.assertEqual(
            [p["name"] for p in data["plays"]],
            ["random_walk", "ring"],
        )

    def test_compile_play_returns_config_without_creating_experiment(self):
        self.up()

        # Establish a real database first. Compiling a play must not create
        # another experiment or otherwise mutate persistent state.
        s0, baseline = self.do("new", {
            "config": cfg_experimental(),
            "description": "baseline",
        })
        self.assertEqual(s0, 200, baseline)

        before = len(self.snap()["extras"]["experiments"])

        s, out = self.do("compile_play", {
            "play": "ring",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "decisions": 20,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            },
        })

        self.assertEqual(s, 200, out)
        self.assertEqual(out["play"]["name"], "ring")
        self.assertNotIn("play", out["config"])
        self.assertEqual(
            out["config"]["flows"][0]["experimental_topology"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_a", "to": "flab_b"},
                {"from": "flab_b", "to": "flab_a"},
            ],
        )

        after = len(self.snap()["extras"]["experiments"])
        self.assertEqual(after, before)

    def test_compile_play_refuses_wallet_outside_dashboard_allowlist(self):
        self.up()

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "not_a_flowlab_wallet"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "decisions": 20,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            },
        })

        self.assertEqual(s, 400)
        self.assertIn("wallet", out["error"].lower())

    def test_compile_play_enforces_configured_wallet_roles(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a"],
            "hubs": ["flab_b"],
            "destinations": ["flab_dest"],
        }
        self.up(wallet_roles=roles)

        def params():
            return {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_b"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "decisions": 20,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            }

        # A hub is a valid workload wallet.
        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": params(),
        })
        self.assertEqual(s, 200, out)

        bad = params()
        bad["source_wallet"] = "flab_a"
        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": bad,
        })
        self.assertEqual(s, 400)
        self.assertIn("reserve role", out["error"])

        bad = params()
        bad["allocation_wallet"] = "flab_a"
        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": bad,
        })
        self.assertEqual(s, 400)
        self.assertIn("stage role", out["error"])

        bad = params()
        bad["workers"] = ["flab_a"]
        bad["destination_wallet"] = "flab_b"
        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": bad,
        })
        self.assertEqual(s, 400)
        self.assertIn("destination role", out["error"])

    def test_compile_play_refuses_allowlisted_wallet_without_workload_role(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }
        self.up(wallet_roles=roles)

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_b"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "decisions": 20,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            },
        })

        self.assertEqual(s, 400)
        self.assertIn("worker or hub role", out["error"])

    def test_compile_play_refuses_unknown_play(self):
        self.up()

        s, out = self.do("compile_play", {
            "play": "not_real",
            "params": {},
        })

        self.assertEqual(s, 400)
        self.assertIn("unknown play", out["error"])

    def test_compiled_play_config_can_enter_normal_new_review_path(self):
        self.up()

        s, compiled = self.do("compile_play", {
            "play": "ring",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "decisions": 20,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            },
        })

        self.assertEqual(s, 200, compiled)

        s, created = self.do("new", {
            "config": compiled["config"],
            "description": "ring play from dashboard",
        })

        self.assertEqual(s, 200, created)
        self.assertIn("exp", created)
        self.assertIn("hash", created)
        self.assertIn("experimental topology:", created["text"])
        self.assertIn("flab_stage -> flab_a", created["text"])
        self.assertIn("flab_a -> flab_b", created["text"])
        self.assertIn("flab_b -> flab_a", created["text"])

    def test_experimental_config_does_not_require_destination_as_last_workload_hop(self):
        self.up()

        cfg = cfg_experimental()
        transitions = cfg["flows"][0]["experimental_topology"]["transitions"]

        self.assertTrue(all(t["to"] != "flab_dest" for t in transitions))
        self.assertEqual(self.do("new", {"config": cfg})[0], 200)


    def test_experimental_review_shows_finalization_contract(self):
        self.up()

        s, out = self.do("new", {"config": cfg_experimental()})

        self.assertEqual(s, 200, out)
        text = out["text"]

        self.assertIn("finalization: sweep_workers_to_destination", text)
        self.assertIn(
            "flab_a -> flab_dest   ENTIRE BALANCE minus fee",
            text,
        )
        self.assertIn(
            "flab_b -> flab_dest   ENTIRE BALANCE minus fee",
            text,
        )
        self.assertIn(
            "source flab_source is not swept",
            text,
        )

    def test_experimental_review_shows_approved_topology(self):
        self.up()

        s, out = self.do("new", {"config": cfg_experimental()})

        self.assertEqual(s, 200, out)
        text = out["text"]

        self.assertIn("experimental topology:", text)
        self.assertIn("flab_stage -> flab_a", text)
        self.assertIn("flab_stage -> flab_b", text)
        self.assertIn("flab_a -> flab_a", text)
        self.assertIn("flab_a -> flab_b", text)
        self.assertIn("flab_b -> flab_a", text)
        self.assertIn("flab_b -> flab_b", text)

    def test_second_run_refused_while_active_and_halt_then_continue(self):
        gate = threading.Event()

        def slow(s):
            self.chain.mine(2)
            gate.wait(5)
        self.up(sleep=slow)
        exp = self.do("new", {"config": cfg_sweep()})[1]["exp"]
        self.do("approve", {"exp": exp, "hash": self.do("review", {"exp": exp})[1]["hash"]})
        self.assertEqual(self.do("run", {"exp": exp})[0], 200)
        self.until(lambda: self.srv.controller.active())
        self.assertEqual(self.do("run", {"exp": exp})[0], 400)
        self.assertEqual(self.do("resume", {"exp": exp})[0], 400)
        self.do("halt")
        gate.set()
        self.wait_idle()
        self.assertNotEqual(self.state(exp), "COMPLETE")
        self.assertIn("stopped", " ".join(l["line"] for l in self.snap()["log"]))
        self.assertEqual(self.do("run", {"exp": exp})[0], 200)          # carries on
        self.wait_idle()
        self.assertEqual(self.state(exp), "COMPLETE")

    def test_emergency_stop_aborts_and_clear_needs_a_note(self):
        self.up()
        exp = self.do("new", {"config": cfg_sweep()})[1]["exp"]
        s, out = self.do("stop")
        self.assertEqual(s, 200)
        self.assertEqual(self.state(exp), "ABORTED")
        self.assertTrue(self.snap()["extras"]["emergency"])
        self.assertEqual(self.do("clear_stop", {"note": " "})[0], 400)
        self.assertEqual(self.do("clear_stop", {"note": "checked, all good"})[0], 200)
        self.assertFalse(self.snap()["extras"]["emergency"])

    def test_resolve_validates_its_input(self):
        self.up()
        for body in ({}, {"action": "1", "outcome": "broadcast"}, {"action": True, "outcome": "broadcast"},
                     {"action": 1, "outcome": "maybe"}, {"action": 999, "outcome": "not_broadcast"}):
            self.assertEqual(self.do("resolve", body)[0], 400, body)

    def test_without_a_node_runs_are_refused(self):
        self.serve()
        exp = self.do("new", {"config": cfg_sweep()})[1]["exp"]
        self.do("approve", {"exp": exp, "hash": self.do("review", {"exp": exp})[1]["hash"]})
        s, out = self.do("run", {"exp": exp})
        self.assertEqual(s, 400)
        self.assertIn("node is not connected", out["error"])

    def test_closing_the_server_halts_and_joins_the_runner(self):
        gate = threading.Event()
        self.up(sleep=lambda s: gate.wait(5))
        exp = self.do("new", {"config": cfg_sweep()})[1]["exp"]
        self.do("approve", {"exp": exp, "hash": self.do("review", {"exp": exp})[1]["hash"]})
        self.do("run", {"exp": exp})
        self.until(lambda: self.srv.controller.active())
        gate.set()
        self.srv.controller.close()
        self.assertFalse(self.srv.controller.active())

    def test_funds_must_end_in_the_destination(self):
        self.up()
        s, out = self.do("new", {"config": cfg_loop()})          # ends back at the source
        self.assertEqual(s, 400)
        self.assertIn("destination", out["error"])
        bad = cfg_sweep()
        bad["flows"][0]["transfers"].pop()                       # stops at flab_b
        self.assertEqual(self.do("new", {"config": bad})[0], 400)
        self.assertEqual(self.do("new", {"config": cfg_sweep()})[0], 200)


class ExportTests(WebBase):
    def test_export_csv_and_json_match_the_run_and_hold_no_secrets(self):
        self.exp = self.make()
        self.call("run", self.exp)
        self.serve(self.chain, wallets=tuple(self.chain.wallets))
        s, h, body = self.get(f"/api/export?exp={self.exp}&fmt=csv")
        self.assertEqual(s, 200)
        self.assertIn("text/csv", h["Content-Type"])
        self.assertIn(f'{self.exp}.csv', h["Content-Disposition"])
        rows = body.decode().strip().splitlines()
        self.assertEqual(rows[0], "hop,from,to,state,planned_sats,sent_sats,fee_sats,txid,confirmations")
        self.assertEqual(len(rows), 5)                       # header + 4 hops
        s, h, body = self.get(f"/api/export?exp={self.exp}&fmt=json")
        doc = json.loads(body)
        self.assertEqual(doc["totals"]["hops"], 4)
        self.assertEqual(doc["totals"]["confirmed"], 4)
        self.assertEqual(doc["totals"]["fee_sats"], sum(x["fee_sats"] for x in doc["hops"]))
        self.assertNotIn("config_json", json.dumps(doc))
        self.assertNotIn(self.srv.token, body.decode())

    def test_export_refuses_bad_format_unknown_run_and_wrong_host(self):
        self.exp = self.make()
        self.serve()
        self.assertEqual(self.get(f"/api/export?exp={self.exp}&fmt=xml")[0], 404)
        self.assertEqual(self.get("/api/export?exp=EXP-NOPE&fmt=csv")[0], 404)
        self.assertEqual(self.get(f"/api/export?exp={self.exp}&fmt=csv", host="evil.example")[0], 403)
