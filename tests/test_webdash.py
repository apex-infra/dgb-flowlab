import http.client
import json
import os
import re
import threading
import time
from decimal import Decimal
from unittest.mock import patch

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

    def test_page_contains_fund_reserve_section(self):
        self.start()

        page = self.get()[2].decode()
        app = self.get("/app.js")[2].decode()
        css = self.get("/app.css")[2].decode()

        for want in (
            'data-tab="fund"',
            "Fund Reserve",
            'id="tab-fund"',
            'id="fund-reserve"',
        ):
            self.assertIn(want, page)

        for want in (
            "renderFundReserve",
            '"fund_reserve_address"',
            '"Generate receiving address"',
            '"New address"',
            '"Copy address"',
            '"Copy URI"',
            '"Update Core label"',
            '"Clear"',
            '"Fund Reserve request cleared."',
            '"Payment URI"',
            '"Payment QR"',
            '"Amount (DGB)"',
            '"Address type"',
            '"Bech32"',
            '"Legacy"',
            '"P2SH-SegWit"',
            '"Bech32m (Taproot-era)"',
            '"fund_reserve_qr"',
            "scheduleFundQR",
            "fundPaymentURI",
            "fundAddresses",
            "fundQRImages",
            'dataset.fundField = "label"',
            'dataset.fundField = "amount"',
            "setSelectionRange",
        ):
            self.assertIn(want, app)

        for want in (
            ".fund-card",
            ".fund-address",
            ".fund-note",
            ".fund-qr-box",
            ".fund-qr",
        ):
            self.assertIn(want, css)


    def test_page_contains_infrastructure_lab(self):
        self.start()

        page = self.get("/")[2].decode()
        app = self.get("/app.js")[2].decode()
        css = self.get("/app.css")[2].decode()

        for want in (
            'data-tab="infrastructure"',
            "Infrastructure",
            'id="tab-infrastructure"',
            'id="infrastructure"',
        ):
            self.assertIn(want, page)

        for want in (
            "renderInfrastructure",
            "renderInfrastructureWallet",
            "showInfrastructureSubtab",
            '"Overall Infra Rank"',
            '"Infrastructure Rankings"',
            '"Wallet Research Ranking"',
            '"Wallet Research Rank"',
            '"Structural Measurements"',
            '"Wallet Laboratory"',
            '"Infrastructure UTXO Inventory"',
            '"Balance rank"',
            '"UTXO-count rank"',
            '"Fragmentation index"',
        ):
            self.assertIn(want, app)

        for want in (
            ".infra-rank",
            ".infra-subtabs",
            ".infra-subpanel",
            ".infra-wallet-rank",
            ".infra-wallet-tabs",
            ".infra-utxo-table",
            ".infra-all-utxo-table",
            ".infra-rank-table",
            ".infra-wallet-head",
        ):
            self.assertIn(want, css)

    def test_page_contains_results_section(self):
        self.start()

        page = self.get("/")[2].decode()
        app = self.get("/app.js")[2].decode()

        self.assertIn(
            'data-tab="results"',
            page,
        )
        self.assertIn(
            'id="tab-results"',
            page,
        )
        self.assertIn(
            'id="results"',
            page,
        )

        self.assertIn(
            'function renderResults(data)',
            app,
        )
        self.assertIn(
            '"Mixing / Cleanliness"',
            app,
        )
        self.assertIn(
            '"Route activity"',
            app,
        )
        self.assertIn(
            '"Wallet activity"',
            app,
        )

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
            "destinationEndpoint()",
            "destinationConfig()",
            "destinationControls(",
            '"destination_address"',
            '"Custom DigiByte address"',
            '"Destination type"',
            "destinationSetControls(",
            "destinationSetConfig()",
            '"f-dst-mode"',
            '"+ Add destination"',
            '"Percentage split"',
            '"Fixed DGB + remainder"',
            '"Remainder"',
            "percent_bps",
            "remainder",
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
        self.assertIn('mode: "consolidate_then_distribute"', text)
        self.assertIn("finalization_wallet: stage", text)

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
        self.assertIn("destinationEndpoint()", form)
        self.assertIn("destinationConfig()", form)
        self.assertIn("destinationControls(", form)
        self.assertIn('"destination_address"', form)
        self.assertIn('"Custom DigiByte address"', form)
        self.assertIn("destinationSetControls(", form)
        self.assertIn("destinationSetConfig()", form)
        self.assertIn('"f-dst-mode"', form)
        self.assertIn('"+ Add destination"', form)
        self.assertIn('"Percentage split"', form)
        self.assertIn('"Fixed DGB + remainder"', form)
        self.assertIn("finalization_wallet: stage", form)
        self.assertIn('mode: "consolidate_then_distribute"', form)
        self.assertIn(
            "on: keep.has(w) ? keep.get(w) : false",
            form,
        )
        self.assertIn('"Select all"', form)
        self.assertIn('"Clear"', form)
        self.assertIn("state.workers.forEach(w => { w.on = true; })", form)
        self.assertIn("state.workers.forEach(w => { w.on = false; })", form)
        self.assertIn('"hub_and_spoke"', form)
        self.assertIn('"fan_out_fan_in"', form)
        self.assertIn("const fanMode =", form)
        self.assertIn("decisionsField.hidden = fanMode", form)
        self.assertIn('"f-hub"', form)
        self.assertIn('"Hub wallet"', form)
        self.assertIn('roleWallets("hubs")', form)
        self.assertIn('roleWallets("workers")', form)
        self.assertIn("params.hub_wallet = hub.value", form)
        self.assertIn("orderedWalletNames", app)
        self.assertIn("walletRoleName", app)
        self.assertIn("staged_accounting", app)
        self.assertIn("Approved principal", app)
        self.assertIn("Principal after fees", app)
        self.assertIn("Destination receipts", app)
        self.assertIn("reconciled ✓", app)

    def test_form_script_contains_settlement_cycle_controls(self):
        self.start()
        form = self.get("/form.js")[2].decode()

        # Settlement Cycle must have a dedicated UI shape rather than
        # inheriting the generic terminal-distribution Play form.
        self.assertIn('"settlement_cycle"', form)
        self.assertIn('"Settlement payouts"', form)
        self.assertIn('"Workers"', form)
        self.assertIn('"Hubs"', form)

        # Outbound and return workloads are independently counted.
        self.assertIn('"Outbound decisions"', form)
        self.assertIn('"Return decisions"', form)

        # Settlement and reserve-return scheduling are first-class.
        self.assertIn('"Settlement minimum delay (seconds)"', form)
        self.assertIn('"Settlement maximum delay (seconds)"', form)
        self.assertIn('"Reserve return minimum delay (seconds)"', form)
        self.assertIn('"Reserve return maximum delay (seconds)"', form)

        # The lifecycle has an explicit safety ceiling.
        self.assertIn('"Maximum total transactions"', form)

        # Settlement is partial: unassigned value stays in the experiment.
        self.assertIn(
            '"Percentage payouts must total less than 100.00%."',
            form,
        )
        self.assertIn(
            '"Unassigned value remains in the experiment after settlement."',
            form,
        )

        # Settlement payout values are operator-entered. The UI must not
        # silently recommend an arbitrary percentage or fixed amount.
        self.assertIn(
            'percent.placeholder = "e.g. 10.00";',
            form,
        )
        self.assertIn(
            'fixed.placeholder = "e.g. 0.10";',
            form,
        )
        self.assertIn(
            'percent.value = initial.percent || "";',
            form,
        )
        self.assertIn(
            'fixed.value = initial.fixed || "";',
            form,
        )

        # Settlement Cycle must not inherit terminal-distribution wording.
        self.assertIn(
            '"Play configuration is compiled by FlowLab on the server. "',
            form,
        )
        self.assertNotIn(
            '"Play topology and terminal distribution are compiled by FlowLab on the server. "',
            form,
        )

        # Dedicated request fields must match the backend compiler contract.
        for key in (
            "outbound_decisions",
            "return_decisions",
            "settlement_delay_seconds_min",
            "settlement_delay_seconds_max",
            "reserve_return_delay_seconds_min",
            "reserve_return_delay_seconds_max",
            "max_total_transactions",
            "settlement",
            "hubs",
        ):
            self.assertIn(key, form)

    def test_snapshot_exposes_live_infrastructure_data(self):
        from tests.fake_chain import FakeChain

        wallets = (
            "flab_source",
            "flab_stage",
            "flab_a",
            "flab_b",
            "flab_dest",
        )

        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        chain = FakeChain(wallets)

        chain.fund("flab_source", 100_000_000)
        chain.fund("flab_source", 50_000_000)
        chain.fund("flab_stage", 25_000_000)

        self.serve(
            rpc=chain,
            wallets=wallets,
            wallet_roles=roles,
        )

        infra = self.snap()["infrastructure"]

        self.assertIsNotNone(infra)
        self.assertEqual(
            infra["model"],
            "infrastructure_snapshot_v1",
        )

        self.assertEqual(
            infra["overview"]["managed_balance_sats"],
            175_000_000,
        )
        self.assertEqual(
            infra["overview"]["utxo_count"],
            3,
        )
        self.assertEqual(
            infra["overview"]["confirmed_utxo_count"],
            3,
        )

        source = infra["wallets"]["flab_source"]

        self.assertEqual(
            source["role"],
            "reserve",
        )
        self.assertEqual(
            source["trusted_balance_sats"],
            150_000_000,
        )
        self.assertEqual(
            source["utxo_count"],
            2,
        )

        self.assertEqual(
            infra["score"]["model"],
            "infrastructure_model_v1",
        )

    def test_snapshot_exposes_infrastructure_field(self):
        self.start()

        data = self.snap()

        self.assertIn("infrastructure", data)

    def test_snapshot_without_rpc_has_no_infrastructure(self):
        self.serve(rpc=None)

        data = self.snap()

        self.assertIsNone(data["infrastructure"])

    def test_snapshot_exposes_derived_results(self):
        self.start()

        data = self.snap()

        self.assertIn("results", data)
        self.assertIsNotNone(data["results"])

        results = data["results"]

        self.assertEqual(
            results["summary"]["id"],
            data["snapshot"]["exp"]["id"],
        )

        self.assertIn("activity", results)
        self.assertIn("topology", results)
        self.assertIn("amounts", results)
        self.assertIn("timing", results)
        self.assertIn("observability", results)
        self.assertIn("mixing", results)

        self.assertEqual(
            results["mixing"]["model"],
            "mixing_model_v1",
        )

    def test_snapshot_results_do_not_expose_raw_config_payloads(self):
        self.start()

        data = self.snap()
        results = data["results"]

        encoded = json.dumps(results)

        self.assertNotIn("config_json", encoded)
        self.assertNotIn("planned_json", encoded)
        self.assertNotIn("result_json", encoded)
        self.assertNotIn("generated_from_json", encoded)

    def test_missing_database_has_no_results(self):
        self.serve(db="/nonexistent/none.db")

        data = self.snap()

        self.assertIsNone(data["snapshot"])
        self.assertIsNone(data["results"])

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
        wallets = kw.pop("wallets", tuple(self.chain.wallets))
        return self.serve(
            self.chain,
            wallets=wallets,
            builder=self.builder,
            **kw,
        )

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

    def test_fund_reserve_generates_address_only_on_explicit_action(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        calls = []
        real = self.chain.get_new_address

        def counted(wallet, label="", address_type=None):
            calls.append((wallet, label, address_type))
            return real(wallet, label, address_type)

        self.chain.get_new_address = counted
        self.up(wallet_roles=roles)

        # Ordinary dashboard reads must never derive addresses.
        self.snap()
        self.snap()
        self.assertEqual(calls, [])

        s, out = self.do(
            "fund_reserve_address",
            {"wallet": "flab_source"},
        )

        self.assertEqual(s, 200, out)
        self.assertEqual(out["wallet"], "flab_source")
        self.assertTrue(out["address"])
        self.assertEqual(
            calls,
            [("flab_source", "", "bech32")],
        )

        info = self.chain.get_address_info(
            "flab_source",
            out["address"],
        )
        self.assertTrue(info["ismine"])
        self.assertEqual(info["label"], "")

    def test_fund_reserve_generation_uses_selected_address_type(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        for address_type in (
            "legacy",
            "p2sh-segwit",
            "bech32",
            "bech32m",
        ):
            s, out = self.do(
                "fund_reserve_address",
                {
                    "wallet": "flab_source",
                    "label": "type test",
                    "address_type": address_type,
                },
            )

            self.assertEqual(s, 200, out)
            self.assertEqual(out["address_type"], address_type)
            self.assertEqual(
                self.chain.address_types[out["address"]],
                address_type,
            )

    def test_fund_reserve_refuses_unknown_address_type(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        s, out = self.do(
            "fund_reserve_address",
            {
                "wallet": "flab_source",
                "address_type": "future-magic",
            },
        )

        self.assertEqual(s, 400)
        self.assertIn(
            "address_type must be one of",
            out["error"],
        )

    def test_fund_reserve_generation_persists_operator_label(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        s, out = self.do(
            "fund_reserve_address",
            {
                "wallet": "flab_source",
                "label": "FlowLab Reserve 001",
            },
        )

        self.assertEqual(s, 200, out)
        self.assertEqual(
            out["label"],
            "FlowLab Reserve 001",
        )
        self.assertEqual(
            self.chain.labels[out["address"]],
            "FlowLab Reserve 001",
        )

    def test_fund_reserve_can_explicitly_update_core_label(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        s, made = self.do(
            "fund_reserve_address",
            {
                "wallet": "flab_source",
                "label": "first",
            },
        )
        self.assertEqual(s, 200, made)

        s, out = self.do(
            "fund_reserve_label",
            {
                "wallet": "flab_source",
                "address": made["address"],
                "label": "second",
            },
        )

        self.assertEqual(s, 200, out)
        self.assertEqual(out["label"], "second")
        self.assertEqual(
            self.chain.labels[made["address"]],
            "second",
        )

    def test_fund_reserve_qr_returns_local_png_data_uri(self):
        png = b"\x89PNG\r\n\x1a\nfake-qr-payload"

        class Result:
            returncode = 0
            stdout = png
            stderr = b""

        with patch(
            "flowlab.webctl.subprocess.run",
            return_value=Result(),
        ) as run:
            self.up()

            uri = (
                "digibyte:dgb1qexample"
                "?amount=250&label=FlowLab%20Reserve"
            )

            s, out = self.do(
                "fund_reserve_qr",
                {"uri": uri},
            )

        self.assertEqual(s, 200, out)
        self.assertEqual(out["uri"], uri)
        self.assertTrue(
            out["image"].startswith(
                "data:image/png;base64,"
            )
        )

        run.assert_called_once()
        kwargs = run.call_args.kwargs

        self.assertEqual(
            kwargs["input"],
            uri.encode("utf-8"),
        )
        self.assertEqual(
            run.call_args.args[0][0],
            "/usr/bin/qrencode",
        )
        self.assertNotIn("shell", kwargs)

    def test_fund_reserve_qr_refuses_non_digibyte_uri(self):
        self.up()

        s, out = self.do(
            "fund_reserve_qr",
            {"uri": "https://example.com/not-a-payment"},
        )

        self.assertEqual(s, 400)
        self.assertIn("digibyte:", out["error"])

    def test_fund_reserve_qr_refuses_bad_encoder_output(self):
        class Result:
            returncode = 0
            stdout = b"not a png"
            stderr = b""

        with patch(
            "flowlab.webctl.subprocess.run",
            return_value=Result(),
        ):
            self.up()

            s, out = self.do(
                "fund_reserve_qr",
                {"uri": "digibyte:dgb1qexample"},
            )

        self.assertEqual(s, 400)
        self.assertIn(
            "invalid PNG",
            out["error"],
        )

    def test_fund_reserve_refuses_non_reserve_wallet(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        s, out = self.do(
            "fund_reserve_address",
            {"wallet": "flab_a"},
        )

        self.assertEqual(s, 400)
        self.assertIn("reserve role", out["error"])


    def test_read_only_switches_every_action_off(self):
        self.up(control=False)
        before = self.digest() if os.path.exists(self.db) else None
        for a in (
            "new",
            "approve",
            "run",
            "stop",
            "clear_stop",
            "resolve",
            "fund_reserve_address",
            "fund_reserve_label",
            "fund_reserve_qr",
        ):
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
            [
                "random_walk",
                "ring",
                "hub_and_spoke",
                "fan_out_fan_in",
                "settlement_cycle",
            ],
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

    def test_compile_fan_out_fan_in_returns_deterministic_config(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(
            wallet_roles=roles,
            wallets=tuple(self.chain.wallets) + ("flab_stage",),
        )

        s, out = self.do("compile_play", {
            "play": "fan_out_fan_in",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 104,
            },
        })

        self.assertEqual(s, 200, out)
        self.assertEqual(
            out["play"]["name"],
            "fan_out_fan_in",
        )
        self.assertEqual(
            out["config"]["randomization"],
            {"enabled": False},
        )
        self.assertNotIn("workload", out["config"])

        flow = out["config"]["flows"][0]
        transfers = flow["transfers"]

        self.assertEqual(transfers[0]["from"], "flab_source")
        self.assertEqual(transfers[0]["to"], "flab_stage")
        self.assertEqual(transfers[-1]["from"], "flab_stage")
        self.assertEqual(transfers[-1]["to"], "flab_dest")

        status, created = self.do("new", {
            "config": out["config"],
            "description": "fan-out fan-in dashboard run",
        })

        self.assertEqual(status, 200, created)
        self.assertIn("exp", created)
        self.assertIn("hash", created)
        self.assertIn("randomization: disabled", created["text"])

    def _settlement_cycle_params(self):
        return {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 10,
            "return_decisions": 6,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 30,
            "settlement_delay_seconds_max": 180,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 60,
            "confirmations_required": 2,
            "seed": 501,
            "max_total_transactions": 100,
            "settlement": {
                "mode": "percentage",
                "items": [
                    {
                        "type": "wallet",
                        "wallet": "flab_dest",
                        "percent_bps": 500,
                    },
                    {
                        "type": "address",
                        "address": "dgb1qexternaldestination",
                        "percent_bps": 2000,
                    },
                ],
            },
        }

    def test_compile_settlement_cycle_accepts_mixed_settlement_targets(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        s, out = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": self._settlement_cycle_params(),
        })

        self.assertEqual(s, 200, out)
        self.assertEqual(
            out["play"]["name"],
            "settlement_cycle",
        )

        cfg = out["config"]
        flow = cfg["flows"][0]

        self.assertNotIn("destination_wallet", flow)
        self.assertNotIn("destination_address", flow)
        self.assertNotIn("destinations", flow)

        self.assertEqual(
            cfg["settlement_cycle"]["settlement"]["items"],
            self._settlement_cycle_params()["settlement"]["items"],
        )

    def test_new_accepts_compiled_settlement_cycle_config(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        status, compiled = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": self._settlement_cycle_params(),
        })

        self.assertEqual(status, 200, compiled)

        cfg = compiled["config"]

        status, created = self.do("new", {
            "config": cfg,
            "description": "settlement cycle dashboard run",
        })

        self.assertEqual(status, 200, created)
        self.assertIn("exp", created)
        self.assertIn("hash", created)

        exp = created["exp"]
        snap = self.snap(exp)["snapshot"]

        self.assertEqual(
            snap["exp"]["description"],
            "settlement cycle dashboard run",
        )
        self.assertEqual(
            snap["exp"]["state"],
            "CONFIGURED",
        )
        self.assertEqual(
            snap["flows"],
            [],
        )

        # Flow rows are created only after operator approval.
        status, approved = self.do("approve", {
            "exp": exp,
            "hash": created["hash"],
            "note": "Settlement Cycle dashboard test",
        })

        self.assertEqual(status, 200, approved)

        snap = self.snap(exp)["snapshot"]

        self.assertEqual(
            snap["exp"]["state"],
            "APPROVED",
        )
        self.assertEqual(
            snap["flows"][0]["dest"],
            cfg["flows"][0]["flow_identity"],
        )

    def test_new_settlement_cycle_rejects_wallet_outside_dashboard_allowlist(self):
        from flowlab.plays import compile_play

        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        # Deliberately omit the internal settlement payout wallet.
        wallets = (
            "flab_source",
            "flab_stage",
            "flab_a",
            "flab_b",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        cfg = compile_play(
            "settlement_cycle",
            self._settlement_cycle_params(),
        )

        status, out = self.do("new", {
            "config": cfg,
            "description": "direct settlement allowlist refusal",
        })

        self.assertEqual(status, 400)
        self.assertIn(
            "outside the dashboard allowlist",
            out["error"],
        )
        self.assertIn(
            "flab_dest",
            out["error"],
        )

    def test_new_settlement_cycle_enforces_worker_and_hub_roles(self):
        from flowlab.plays import compile_play

        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            # flab_b is deliberately missing from worker role.
            "workers": ["flab_a"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        cfg = compile_play(
            "settlement_cycle",
            self._settlement_cycle_params(),
        )

        status, out = self.do("new", {
            "config": cfg,
            "description": "direct settlement worker-role refusal",
        })

        self.assertEqual(status, 400)
        self.assertIn(
            "worker role",
            out["error"],
        )
        self.assertIn(
            "flab_b",
            out["error"],
        )

    def test_new_settlement_cycle_enforces_settlement_destination_role(self):
        from flowlab.plays import compile_play

        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            # Internal payout exists, but has no destination role.
            "destinations": [],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        cfg = compile_play(
            "settlement_cycle",
            self._settlement_cycle_params(),
        )

        status, out = self.do("new", {
            "config": cfg,
            "description": "direct settlement destination-role refusal",
        })

        self.assertEqual(status, 400)
        self.assertIn(
            "destination role",
            out["error"],
        )
        self.assertIn(
            "flab_dest",
            out["error"],
        )


    def test_compile_settlement_cycle_enforces_settlement_wallet_destination_role(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": [],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        s, out = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": self._settlement_cycle_params(),
        })

        self.assertEqual(s, 400)
        self.assertIn(
            "destination role",
            out["error"],
        )

    def test_compile_settlement_cycle_enforces_worker_and_hub_roles(self):
        base_roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        # Hub wallet cannot silently occupy a worker role.
        roles = {
            **base_roles,
            "hubs": [],
        }

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        s, out = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": self._settlement_cycle_params(),
        })

        self.assertEqual(s, 400)
        self.assertIn("hub role", out["error"])

        self.srv.shutdown()
        self.srv.server_close()
        self.srv.controller.close()

        # Worker wallet cannot silently occupy a hub role.
        roles = {
            **base_roles,
            "workers": ["flab_a"],
        }

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        s, out = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": self._settlement_cycle_params(),
        })

        self.assertEqual(s, 400)
        self.assertIn("worker role", out["error"])

    def test_compile_settlement_cycle_validates_external_settlement_address(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "destinations": ["flab_dest"],
        }

        wallets = tuple(self.chain.wallets) + (
            "flab_stage",
            "flab_hub_a",
        )

        self.up(
            wallet_roles=roles,
            wallets=wallets,
        )

        params = self._settlement_cycle_params()
        params["settlement"] = {
            "mode": "fixed",
            "items": [{
                "type": "address",
                "address": "not-valid",
                "amount_sats": 50_000_000,
            }],
        }

        s, out = self.do("compile_play", {
            "play": "settlement_cycle",
            "params": params,
        })

        self.assertEqual(s, 400)
        self.assertIn(
            "valid DigiByte address",
            out["error"],
        )

    def test_compile_play_accepts_valid_external_destination(self):
        self.up()

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destination_address": "dgb1qexternaldestination",
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
        flow = out["config"]["flows"][0]
        self.assertEqual(
            flow["destinations"],
            {
                "mode": "percentage",
                "items": [{
                    "type": "address",
                    "address": "dgb1qexternaldestination",
                    "percent_bps": 10_000,
                }],
            },
        )
        self.assertEqual(
            flow["finalization_wallet"],
            "flab_stage",
        )
        self.assertNotIn("destination_wallet", flow)
        self.assertNotIn("destination_address", flow)

    def test_compile_play_accepts_mixed_multi_destinations(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(wallet_roles=roles)

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destinations": {
                    "mode": "percentage",
                    "items": [
                        {
                            "type": "wallet",
                            "wallet": "flab_dest",
                            "percent_bps": 5000,
                        },
                        {
                            "type": "address",
                            "address": "dgb1qexternaldestination",
                            "percent_bps": 5000,
                        },
                    ],
                },
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

        flow = out["config"]["flows"][0]

        self.assertEqual(
            flow["finalization_wallet"],
            "flab_stage",
        )
        self.assertEqual(
            flow["destinations"]["mode"],
            "percentage",
        )
        self.assertEqual(
            len(flow["destinations"]["items"]),
            2,
        )
        self.assertEqual(
            out["config"]["finalization"]["mode"],
            "consolidate_then_distribute",
        )

    def test_compile_play_multi_destination_enforces_destination_role(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": [],
        }

        self.up(wallet_roles=roles)

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destinations": {
                    "mode": "percentage",
                    "items": [
                        {
                            "type": "wallet",
                            "wallet": "flab_dest",
                            "percent_bps": 5000,
                        },
                        {
                            "type": "address",
                            "address": "dgb1qexternaldestination",
                            "percent_bps": 5000,
                        },
                    ],
                },
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
        self.assertIn(
            "destination role",
            out["error"],
        )

    def test_compile_play_multi_destination_validates_every_external_address(self):
        self.up()

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destinations": {
                    "mode": "percentage",
                    "items": [
                        {
                            "type": "wallet",
                            "wallet": "flab_dest",
                            "percent_bps": 5000,
                        },
                        {
                            "type": "address",
                            "address": "not-valid",
                            "percent_bps": 5000,
                        },
                    ],
                },
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
        self.assertIn(
            "valid DigiByte address",
            out["error"],
        )

    def test_compile_play_refuses_invalid_external_destination(self):
        self.up()

        s, out = self.do("compile_play", {
            "play": "random_walk",
            "params": {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "destination_address": "not-valid",
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
        self.assertIn("valid DigiByte address", out["error"])

    def test_new_accepts_valid_external_deterministic_destination(self):
        self.up()

        cfg = cfg_sweep()
        flow = cfg["flows"][0]
        flow.pop("destination_wallet")
        flow["destination_address"] = "dgb1qexternaldestination"
        flow["transfers"][-1]["to"] = "dgb1qexternaldestination"

        s, out = self.do("new", {"config": cfg})

        self.assertEqual(s, 200, out)
        self.assertIn(
            "dgb1qexternaldestination",
            out["text"],
        )

    def test_new_refuses_invalid_external_destination(self):
        self.up()

        cfg = cfg_sweep()
        flow = cfg["flows"][0]
        flow.pop("destination_wallet")
        flow["destination_address"] = "not-valid"
        flow["transfers"][-1]["to"] = "not-valid"

        s, out = self.do("new", {"config": cfg})

        self.assertEqual(s, 400)
        self.assertIn("valid DigiByte address", out["error"])

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

    def test_compile_hub_and_spoke_enforces_hub_and_spoke_roles(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b", "flab_c"],
            "hubs": ["flab_hub_a", "flab_hub_b"],
            "destinations": ["flab_dest"],
        }
        self.up(
            wallet_roles=roles,
            wallets=tuple(self.chain.wallets) + (
                "flab_c",
                "flab_hub_a",
                "flab_hub_b",
            ),
        )

        def params():
            return {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "hub_wallet": "flab_hub_a",
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
            }

        s, out = self.do("compile_play", {
            "play": "hub_and_spoke",
            "params": params(),
        })
        self.assertEqual(s, 200, out)
        self.assertEqual(
            out["config"]["flows"][0]["experimental_topology"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_hub_a"},
                {"from": "flab_hub_a", "to": "flab_a"},
                {"from": "flab_hub_a", "to": "flab_b"},
                {"from": "flab_a", "to": "flab_hub_a"},
                {"from": "flab_b", "to": "flab_hub_a"},
            ],
        )

        bad = params()
        bad["hub_wallet"] = "flab_c"

        s, out = self.do("compile_play", {
            "play": "hub_and_spoke",
            "params": bad,
        })
        self.assertEqual(s, 400)
        self.assertIn("hub role", out["error"])

        # Wrong spoke: use a second hub-role wallet so it stays distinct
        # from the selected hub and reaches dashboard role enforcement.
        bad = params()
        bad["workers"] = ["flab_hub_b"]

        s, out = self.do("compile_play", {
            "play": "hub_and_spoke",
            "params": bad,
        })
        self.assertEqual(s, 400)
        self.assertIn("worker role", out["error"])

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

    def test_deterministic_allocation_wallet_may_have_stage_role(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "destinations": ["flab_dest"],
        }

        self.up(
            wallet_roles=roles,
            wallets=tuple(self.chain.wallets) + ("flab_stage",),
        )

        cfg = {
            "flows": [{
                "description": "deterministic staged flow",
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "flow_wallets": [
                    "flab_stage",
                    "flab_a",
                    "flab_b",
                ],
                "destination_wallet": "flab_dest",
                "allocation_sats": 500_000_000,
                "transfers": [
                    {
                        "from": "flab_source",
                        "to": "flab_stage",
                        "amount_sats": 500_000_000,
                        "delay_seconds": 0,
                    },
                    {
                        "from": "flab_stage",
                        "to": "flab_a",
                        "amount_sats": 100_000_000,
                        "delay_seconds": 0,
                    },
                    {
                        "from": "flab_a",
                        "to": "flab_b",
                        "amount_sats": "all",
                        "delay_seconds": 0,
                    },
                    {
                        "from": "flab_b",
                        "to": "flab_dest",
                        "amount_sats": "all",
                        "delay_seconds": 0,
                    },
                ],
            }],
            "confirmations_required": 1,
            "fee_policy": {"type": "minimum"},
            "address_policy": "new",
        }

        status, out = self.do("new", {
            "config": cfg,
            "description": "deterministic staged flow",
        })

        self.assertEqual(status, 200, out)
        self.assertIn("exp", out)

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
