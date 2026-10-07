import unittest

from flowlab.utxo_policy import (
    DEFAULT_POLICY,
    UtxoPolicyError,
    eligible_utxos,
    resolve_policy,
    select_utxos,
    validate_policy,
)


def coin(
        n,
        sats,
        confirmations,
        *,
        spendable=True,
        safe=True,
):
    return {
        "txid": f"{n:064x}",
        "vout": 0,
        "amount_sats": sats,
        "confirmations": confirmations,
        "spendable": spendable,
        "safe": safe,
    }


class PolicyValidationTests(unittest.TestCase):
    def test_default_policy_is_valid_and_explicit(self):
        policy = validate_policy({})

        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["default"]["strategy"], "legacy")
        self.assertEqual(policy["default"]["scope"], "live_wallet")
        self.assertEqual(policy["default"]["min_confirmations"], 1)
        self.assertEqual(policy["default"]["fallback"], "fail")

    def test_unknown_strategy_is_refused(self):
        with self.assertRaises(UtxoPolicyError):
            validate_policy({
                "default": {
                    "strategy": "magic",
                },
            })

    def test_seeded_strategy_requires_seed(self):
        with self.assertRaises(UtxoPolicyError):
            validate_policy({
                "default": {
                    "strategy": "seeded_selection",
                },
            })

    def test_phase_override_inherits_default(self):
        policy = validate_policy({
            "default": {
                "strategy": "oldest_confirmed",
                "min_confirmations": 25,
            },
            "phases": {
                "settlement": {
                    "strategy": "fewest_inputs",
                },
            },
        })

        settlement = resolve_policy(policy, "settlement")

        self.assertEqual(settlement["strategy"], "fewest_inputs")
        self.assertEqual(settlement["min_confirmations"], 25)

    def test_unknown_phase_is_refused(self):
        with self.assertRaises(UtxoPolicyError):
            resolve_policy(DEFAULT_POLICY, "teleport")


class EligibilityTests(unittest.TestCase):
    def test_confirmation_and_size_filters(self):
        coins = [
            coin(1, 100, 2),
            coin(2, 200, 20),
            coin(3, 300, 200),
            coin(4, 400, 2000),
        ]

        rule = resolve_policy({
            "default": {
                "min_confirmations": 10,
                "max_confirmations": 500,
                "min_utxo_sats": 150,
                "max_utxo_sats": 350,
            },
        })

        eligible = eligible_utxos(coins, rule)

        self.assertEqual(
            [u["amount_sats"] for u in eligible],
            [200, 300],
        )

    def test_spendable_and_safe_are_enforced(self):
        coins = [
            coin(1, 100, 10),
            coin(2, 200, 10, spendable=False),
            coin(3, 300, 10, safe=False),
        ]

        rule = resolve_policy({})

        eligible = eligible_utxos(coins, rule)

        self.assertEqual(
            [u["amount_sats"] for u in eligible],
            [100],
        )

    def test_snapshot_scope_requires_cohort(self):
        rule = resolve_policy({
            "default": {
                "scope": "snapshot_at_start",
            },
        })

        with self.assertRaises(UtxoPolicyError):
            eligible_utxos(
                [coin(1, 100, 10)],
                rule,
            )

    def test_snapshot_scope_excludes_new_outpoints(self):
        old = coin(1, 100, 10)
        fresh = coin(2, 200, 10)

        rule = resolve_policy({
            "default": {
                "scope": "snapshot_at_start",
            },
        })

        eligible = eligible_utxos(
            [old, fresh],
            rule,
            cohort_outpoints={(old["txid"], old["vout"])},
        )

        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["txid"], old["txid"])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.coins = [
            coin(1, 100, 100),
            coin(2, 200, 20),
            coin(3, 300, 200),
            coin(4, 400, 5),
        ]

    def select(self, strategy, required=250, **extra):
        policy = {
            "default": {
                "strategy": strategy,
                **extra,
            },
        }

        return select_utxos(
            self.coins,
            policy,
            required_sats=required,
            fee_reserve_for_n=lambda _n: 0,
        )

    def test_legacy_preserves_largest_first_behavior(self):
        result = self.select("legacy", 250)

        self.assertEqual(
            [u["amount_sats"] for u in result.selected],
            [400],
        )

    def test_largest_first(self):
        result = self.select("largest_first", 550)

        self.assertEqual(
            [u["amount_sats"] for u in result.selected],
            [400, 300],
        )

    def test_smallest_first(self):
        result = self.select("smallest_first", 250)

        self.assertEqual(
            [u["amount_sats"] for u in result.selected],
            [100, 200],
        )

    def test_oldest_confirmed(self):
        result = self.select("oldest_confirmed", 250)

        self.assertEqual(
            [u["confirmations"] for u in result.selected],
            [200],
        )

    def test_newest_confirmed(self):
        result = self.select("newest_confirmed", 350)

        self.assertEqual(
            [u["confirmations"] for u in result.selected],
            [5],
        )

    def test_fewest_inputs_prefers_large_outputs(self):
        result = self.select("fewest_inputs", 450)

        self.assertEqual(result.selected_count, 2)
        self.assertEqual(
            [u["amount_sats"] for u in result.selected],
            [400, 300],
        )

    def test_exact_match_finds_exact_combination(self):
        result = self.select("exact_match", 500)

        self.assertEqual(result.selected_total_sats, 500)
        self.assertEqual(result.selected_count, 2)

    def test_consolidation_prefers_small_outputs(self):
        result = self.select("consolidation", 550)

        self.assertEqual(
            [u["amount_sats"] for u in result.selected],
            [100, 200, 300],
        )

    def test_balanced_prefers_middle_of_distribution(self):
        result = self.select("balanced", 250)

        self.assertEqual(result.selected_count, 1)
        self.assertIn(
            result.selected[0]["amount_sats"],
            (200, 300),
        )

    def test_seeded_selection_is_reproducible(self):
        policy = {
            "default": {
                "strategy": "seeded_selection",
                "seed": 12345,
            },
        }

        a = select_utxos(
            self.coins,
            policy,
            required_sats=450,
            seed_material="experiment:7:job:4",
        )

        b = select_utxos(
            self.coins,
            policy,
            required_sats=450,
            seed_material="experiment:7:job:4",
        )

        self.assertEqual(
            [(u["txid"], u["vout"]) for u in a.selected],
            [(u["txid"], u["vout"]) for u in b.selected],
        )

    def test_dynamic_fee_reserve_affects_selection(self):
        result = select_utxos(
            [
                coin(1, 300, 10),
                coin(2, 300, 10),
            ],
            {
                "default": {
                    "strategy": "largest_first",
                },
            },
            required_sats=300,
            fee_reserve_for_n=lambda n: 50 * n,
        )

        self.assertEqual(result.selected_count, 2)

    def test_max_inputs_fails_closed(self):
        with self.assertRaises(UtxoPolicyError):
            self.select(
                "smallest_first",
                550,
                max_inputs=2,
            )

    def test_sweep_consumes_every_eligible_coin(self):
        result = select_utxos(
            self.coins,
            {
                "default": {
                    "strategy": "oldest_confirmed",
                    "min_confirmations": 20,
                },
            },
            sweep=True,
        )

        self.assertEqual(result.selected_count, 3)
        self.assertEqual(
            [u["confirmations"] for u in result.selected],
            [200, 100, 20],
        )

    def test_sweep_respects_max_inputs_by_refusing(self):
        with self.assertRaises(UtxoPolicyError):
            select_utxos(
                self.coins,
                {
                    "default": {
                        "max_inputs": 2,
                    },
                },
                sweep=True,
            )

    def test_telemetry_is_explicit(self):
        result = self.select("largest_first", 250)
        telemetry = result.telemetry()

        self.assertEqual(telemetry["policy_version"], 1)
        self.assertEqual(telemetry["strategy"], "largest_first")
        self.assertEqual(telemetry["candidate_count"], 4)
        self.assertEqual(telemetry["eligible_count"], 4)
        self.assertEqual(telemetry["selected_count"], 1)
        self.assertEqual(telemetry["selected_total_sats"], 400)


class SettlementScenarioTests(unittest.TestCase):
    def test_mature_snapshot_cohort_excludes_fresh_deposits(self):
        mature_a = coin(1, 5_000, 12_000)
        mature_b = coin(2, 4_000, 4_000)

        fresh_a = coin(3, 9_000, 2)
        fresh_b = coin(4, 9_000, 3)
        fresh_c = coin(5, 9_000, 5)

        all_coins = [
            mature_a,
            mature_b,
            fresh_a,
            fresh_b,
            fresh_c,
        ]

        policy = {
            "default": {
                "scope": "snapshot_at_start",
                "strategy": "oldest_confirmed",
                "min_confirmations": 100,
            },
        }

        result = select_utxos(
            all_coins,
            policy,
            required_sats=6_000,
            cohort_outpoints={
                (mature_a["txid"], mature_a["vout"]),
                (mature_b["txid"], mature_b["vout"]),
                (fresh_a["txid"], fresh_a["vout"]),
                (fresh_b["txid"], fresh_b["vout"]),
                (fresh_c["txid"], fresh_c["vout"]),
            },
        )

        self.assertEqual(result.eligible_count, 2)
        self.assertEqual(
            [u["confirmations"] for u in result.selected],
            [12_000, 4_000],
        )




class ConfigIntegrationTests(unittest.TestCase):
    @staticmethod
    def config():
        return {
            "flows": [{
                "description": "utxo policy config test",
                "source_wallet": "flab_source",
                "flow_wallets": ["flab_a"],
                "destination_wallet": "flab_dest",
                "allocation_sats": 100_000_000,
                "transfers": [{
                    "from": "flab_source",
                    "to": "flab_a",
                    "amount_sats": 50_000_000,
                    "delay_seconds": 0,
                }, {
                    "from": "flab_a",
                    "to": "flab_dest",
                    "amount_sats": "all",
                    "delay_seconds": 0,
                }],
            }],
            "confirmations_required": 1,
            "fee_policy": {
                "type": "minimum",
            },
            "address_policy": "new",
            "randomization": {
                "enabled": False,
            },
        }

    def test_config_validation_injects_canonical_legacy_policy(self):
        from flowlab.config_schema import validate

        cfg = validate(self.config())

        self.assertEqual(cfg["utxo_policy"]["version"], 1)
        self.assertEqual(
            cfg["utxo_policy"]["default"]["strategy"],
            "legacy",
        )
        self.assertEqual(
            cfg["utxo_policy"]["default"]["scope"],
            "live_wallet",
        )
        self.assertEqual(cfg["utxo_policy"]["phases"], {})

    def test_utxo_policy_changes_config_hash(self):
        from flowlab.config_schema import config_hash, validate

        legacy = validate(self.config())

        other_raw = self.config()
        other_raw["utxo_policy"] = {
            "default": {
                "strategy": "oldest_confirmed",
                "min_confirmations": 25,
            },
        }

        other = validate(other_raw)

        self.assertNotEqual(
            config_hash(legacy),
            config_hash(other),
        )

    def test_phase_policy_is_preserved_in_canonical_config(self):
        from flowlab.config_schema import validate

        raw = self.config()
        raw["utxo_policy"] = {
            "default": {
                "strategy": "largest_first",
                "min_confirmations": 6,
            },
            "phases": {
                "settlement": {
                    "strategy": "oldest_confirmed",
                    "min_confirmations": 100,
                },
                "reserve_return": {
                    "strategy": "consolidation",
                },
            },
        }

        cfg = validate(raw)

        self.assertEqual(
            cfg["utxo_policy"]["phases"]["settlement"]["strategy"],
            "oldest_confirmed",
        )
        self.assertEqual(
            cfg["utxo_policy"]["phases"]["settlement"]["min_confirmations"],
            100,
        )
        self.assertEqual(
            cfg["utxo_policy"]["phases"]["reserve_return"]["strategy"],
            "consolidation",
        )
        self.assertEqual(
            cfg["utxo_policy"]["phases"]["reserve_return"]["min_confirmations"],
            6,
        )

    def test_invalid_policy_becomes_config_error(self):
        from flowlab.config_schema import ConfigError, validate

        raw = self.config()
        raw["utxo_policy"] = {
            "default": {
                "strategy": "not-a-real-policy",
            },
        }

        with self.assertRaisesRegex(
            ConfigError,
            "invalid utxo_policy",
        ):
            validate(raw)




class JobPhaseTests(unittest.TestCase):
    def test_executor_reads_explicit_job_phase(self):
        import json
        from flowlab.executor import Executor

        job = {
            "generated_from_json": json.dumps({
                "source": "test",
                "phase": "settlement",
            }),
        }

        self.assertEqual(
            Executor._utxo_phase(job),
            "settlement",
        )

    def test_old_job_without_phase_defaults_to_workload(self):
        import json
        from flowlab.executor import Executor

        job = {
            "generated_from_json": json.dumps({
                "source": "approved config",
            }),
        }

        self.assertEqual(
            Executor._utxo_phase(job),
            "workload",
        )

    def test_unknown_job_phase_fails_closed(self):
        import json
        from flowlab.executor import Executor
        from flowlab.planner import PlanError

        job = {
            "generated_from_json": json.dumps({
                "source": "test",
                "phase": "unknown-phase",
            }),
        }

        with self.assertRaisesRegex(
            PlanError,
            "unsupported UTXO policy phase",
        ):
            Executor._utxo_phase(job)




class CohortPersistenceTests(unittest.TestCase):
    def _engine_with_running_flow(self):
        import os
        import tempfile

        from flowlab.engine import Engine
        from tests.test_engine import FakeVerifier

        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "cohort.db")

        engine = Engine(
            path,
            verifier=FakeVerifier(),
        )

        self.addCleanup(tmp.cleanup)
        self.addCleanup(engine.conn.close)

        exp_id = engine.create_experiment("cohort persistence test")

        cfg = ConfigIntegrationTests.config()
        config_hash = engine.configure_experiment(exp_id, cfg)

        engine.approve(exp_id, config_hash, "test")
        engine.start(exp_id)

        flow = engine.list_flows(exp_id)[0]

        return engine, flow["id"]

    def test_experiment_start_cohort_round_trips(self):
        engine, flow_id = self._engine_with_running_flow()

        cohort = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "wallets": {
                "flab_source": [
                    ["a" * 64, 0],
                    ["b" * 64, 1],
                ],
            },
        }

        engine.save_utxo_cohort(
            flow_id,
            "snapshot_at_start",
            cohort,
        )

        self.assertEqual(
            engine.get_utxo_cohort(
                flow_id,
                "snapshot_at_start",
            ),
            cohort,
        )

    def test_experiment_start_cohort_cannot_be_replaced(self):
        from flowlab.engine import EngineError

        engine, flow_id = self._engine_with_running_flow()

        cohort = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "wallets": {},
        }

        engine.save_utxo_cohort(
            flow_id,
            "snapshot_at_start",
            cohort,
        )

        with self.assertRaisesRegex(
            EngineError,
            "already exists",
        ):
            engine.save_utxo_cohort(
                flow_id,
                "snapshot_at_start",
                cohort,
            )

    def test_phase_cohorts_are_independent(self):
        engine, flow_id = self._engine_with_running_flow()

        workload = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "wallets": {
                "flab_a": [["c" * 64, 0]],
            },
        }

        settlement = {
            "captured_at": "2026-01-01T01:00:00+00:00",
            "wallets": {
                "flab_a": [["d" * 64, 0]],
            },
        }

        engine.save_utxo_cohort(
            flow_id,
            "snapshot_at_phase_start",
            workload,
            phase="workload",
        )

        engine.save_utxo_cohort(
            flow_id,
            "snapshot_at_phase_start",
            settlement,
            phase="settlement",
        )

        self.assertEqual(
            engine.get_utxo_cohort(
                flow_id,
                "snapshot_at_phase_start",
                phase="workload",
            ),
            workload,
        )

        self.assertEqual(
            engine.get_utxo_cohort(
                flow_id,
                "snapshot_at_phase_start",
                phase="settlement",
            ),
            settlement,
        )

    def test_cohort_survives_new_engine_instance(self):
        from flowlab.engine import Engine

        engine, flow_id = self._engine_with_running_flow()

        cohort = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "wallets": {
                "flab_source": [["e" * 64, 2]],
            },
        }

        engine.save_utxo_cohort(
            flow_id,
            "snapshot_at_start",
            cohort,
        )

        engine.conn.close()

        restarted = Engine(
            engine.path,
            verifier=engine.verifier,
        )
        self.addCleanup(restarted.conn.close)

        self.assertEqual(
            restarted.get_utxo_cohort(
                flow_id,
                "snapshot_at_start",
            ),
            cohort,
        )




class CohortCaptureTests(unittest.TestCase):
    class Rpc:
        def __init__(self):
            self.utxos = {
                "flab_source": [
                    {
                        "txid": "b" * 64,
                        "vout": 1,
                    },
                    {
                        "txid": "a" * 64,
                        "vout": 2,
                    },
                ],
                "flab_a": [
                    {
                        "txid": "c" * 64,
                        "vout": 0,
                    },
                ],
            }

        def list_unspent(self, wallet, minconf=0):
            return [
                dict(item)
                for item in self.utxos.get(wallet, [])
            ]

    def _running(self, *, scope):
        import os
        import tempfile

        from flowlab.engine import Engine
        from tests.test_engine import FakeVerifier

        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "capture.db")

        engine = Engine(
            path,
            verifier=FakeVerifier(),
        )

        self.addCleanup(tmp.cleanup)
        self.addCleanup(engine.conn.close)

        cfg = ConfigIntegrationTests.config()
        cfg["utxo_policy"] = {
            "default": {
                "scope": scope,
            },
        }

        exp_id = engine.create_experiment("capture test")
        config_hash = engine.configure_experiment(
            exp_id,
            cfg,
        )
        engine.approve(exp_id, config_hash, "test")
        engine.start(exp_id)

        flow = engine.list_flows(exp_id)[0]

        return engine, exp_id, flow

    def test_snapshot_at_start_captures_sender_wallets(self):
        from flowlab.executor import Executor

        engine, exp_id, flow = self._running(
            scope="snapshot_at_start",
        )

        rpc = self.Rpc()
        executor = Executor(
            engine,
            rpc,
            builder=None,
        )

        executor._ensure_experiment_start_cohorts(
            exp_id,
        )

        cohort = engine.get_utxo_cohort(
            flow["id"],
            "snapshot_at_start",
        )

        self.assertEqual(
            sorted(cohort["wallets"]),
            ["flab_a", "flab_source"],
        )

        self.assertEqual(
            cohort["wallets"]["flab_source"],
            [
                ["a" * 64, 2],
                ["b" * 64, 1],
            ],
        )

        self.assertEqual(
            cohort["wallets"]["flab_a"],
            [
                ["c" * 64, 0],
            ],
        )

    def test_existing_snapshot_is_not_recaptured(self):
        from flowlab.executor import Executor

        engine, exp_id, flow = self._running(
            scope="snapshot_at_start",
        )

        rpc = self.Rpc()
        executor = Executor(
            engine,
            rpc,
            builder=None,
        )

        executor._ensure_experiment_start_cohorts(
            exp_id,
        )

        first = engine.get_utxo_cohort(
            flow["id"],
            "snapshot_at_start",
        )

        rpc.utxos["flab_source"].append({
            "txid": "d" * 64,
            "vout": 9,
        })

        executor._ensure_experiment_start_cohorts(
            exp_id,
        )

        second = engine.get_utxo_cohort(
            flow["id"],
            "snapshot_at_start",
        )

        self.assertEqual(first, second)

        self.assertNotIn(
            ["d" * 64, 9],
            second["wallets"]["flab_source"],
        )

    def test_live_wallet_policy_does_not_capture_snapshot(self):
        from flowlab.executor import Executor

        engine, exp_id, flow = self._running(
            scope="live_wallet",
        )

        executor = Executor(
            engine,
            self.Rpc(),
            builder=None,
        )

        executor._ensure_experiment_start_cohorts(
            exp_id,
        )

        self.assertIsNone(
            engine.get_utxo_cohort(
                flow["id"],
                "snapshot_at_start",
            )
        )




class PhaseCohortCaptureTests(unittest.TestCase):
    class Rpc:
        def __init__(self):
            self.utxos = {
                "flab_source": [
                    {
                        "txid": "1" * 64,
                        "vout": 0,
                    },
                ],
                "flab_a": [
                    {
                        "txid": "2" * 64,
                        "vout": 1,
                    },
                ],
            }

        def list_unspent(self, wallet, minconf=0):
            return [
                dict(item)
                for item in self.utxos.get(wallet, [])
            ]

    def _running(self, policy):
        import os
        import tempfile

        from flowlab.engine import Engine
        from tests.test_engine import FakeVerifier

        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "phase-capture.db")

        engine = Engine(
            path,
            verifier=FakeVerifier(),
        )

        self.addCleanup(tmp.cleanup)
        self.addCleanup(engine.conn.close)

        cfg = ConfigIntegrationTests.config()
        cfg["utxo_policy"] = policy

        exp_id = engine.create_experiment(
            "phase cohort capture test"
        )

        config_hash = engine.configure_experiment(
            exp_id,
            cfg,
        )

        engine.approve(
            exp_id,
            config_hash,
            "test",
        )

        engine.start(exp_id)

        flow = engine.list_flows(exp_id)[0]

        return engine, flow

    @staticmethod
    def job(phase):
        import json

        return {
            "generated_from_json": json.dumps({
                "source": "test",
                "phase": phase,
            }),
        }

    def test_phase_snapshot_captures_on_matching_phase(self):
        from flowlab.executor import Executor

        engine, flow = self._running({
            "default": {
                "scope": "snapshot_at_phase_start",
            },
        })

        executor = Executor(
            engine,
            self.Rpc(),
            builder=None,
        )

        cohort = executor._ensure_phase_start_cohort(
            flow,
            self.job("workload"),
        )

        self.assertIsNotNone(cohort)

        stored = engine.get_utxo_cohort(
            flow["id"],
            "snapshot_at_phase_start",
            phase="workload",
        )

        self.assertEqual(cohort, stored)

        self.assertEqual(
            stored["wallets"]["flab_source"],
            [["1" * 64, 0]],
        )

    def test_phase_snapshot_is_not_recaptured(self):
        from flowlab.executor import Executor

        engine, flow = self._running({
            "default": {
                "scope": "snapshot_at_phase_start",
            },
        })

        rpc = self.Rpc()

        executor = Executor(
            engine,
            rpc,
            builder=None,
        )

        first = executor._ensure_phase_start_cohort(
            flow,
            self.job("workload"),
        )

        rpc.utxos["flab_source"].append({
            "txid": "3" * 64,
            "vout": 9,
        })

        second = executor._ensure_phase_start_cohort(
            flow,
            self.job("workload"),
        )

        self.assertEqual(first, second)

        self.assertNotIn(
            ["3" * 64, 9],
            second["wallets"]["flab_source"],
        )

    def test_different_phases_get_independent_snapshots(self):
        from flowlab.executor import Executor

        engine, flow = self._running({
            "default": {
                "scope": "snapshot_at_phase_start",
            },
        })

        rpc = self.Rpc()

        executor = Executor(
            engine,
            rpc,
            builder=None,
        )

        workload = executor._ensure_phase_start_cohort(
            flow,
            self.job("workload"),
        )

        rpc.utxos["flab_source"].append({
            "txid": "4" * 64,
            "vout": 4,
        })

        settlement = executor._ensure_phase_start_cohort(
            flow,
            self.job("settlement"),
        )

        self.assertNotEqual(
            workload,
            settlement,
        )

        self.assertNotIn(
            ["4" * 64, 4],
            workload["wallets"]["flab_source"],
        )

        self.assertIn(
            ["4" * 64, 4],
            settlement["wallets"]["flab_source"],
        )

    def test_phase_override_can_enable_snapshot_scope(self):
        from flowlab.executor import Executor

        engine, flow = self._running({
            "default": {
                "scope": "live_wallet",
            },
            "phases": {
                "settlement": {
                    "scope": "snapshot_at_phase_start",
                },
            },
        })

        executor = Executor(
            engine,
            self.Rpc(),
            builder=None,
        )

        workload = executor._ensure_phase_start_cohort(
            flow,
            self.job("workload"),
        )

        settlement = executor._ensure_phase_start_cohort(
            flow,
            self.job("settlement"),
        )

        self.assertIsNone(workload)
        self.assertIsNotNone(settlement)




class TxBuilderPolicyIntegrationTests(unittest.TestCase):
    def test_smallest_first_policy_controls_real_builder_inputs(self):
        from flowlab.tx_builder import TxBuilder
        from tests.fake_chain import FakeChain

        wallets = [
            "flab_source",
            "flab_dest",
        ]

        chain = FakeChain(wallets)

        chain.fund(
            "flab_source",
            900_000_000,
        )
        chain.fund(
            "flab_source",
            300_000_000,
        )
        chain.fund(
            "flab_source",
            200_000_000,
        )

        chain.mine(3)

        before = chain.list_unspent(
            "flab_source",
            1,
        )

        by_amount = {
            int(u["amount"] * 100_000_000):
                (u["txid"], u["vout"])
            for u in before
        }

        builder = TxBuilder(
            chain,
            wallets,
            max_fee_sats=10_000_000,
        )

        destination = chain.get_new_address(
            "flab_dest",
            "policy-test",
        )

        prepared = builder.build(
            "flab_source",
            destination,
            350_000_000,
            utxo_policy={
                "default": {
                    "strategy": "smallest_first",
                },
            },
            phase="workload",
            seed_material="test-smallest",
        )

        self.assertEqual(
            set(prepared.inputs),
            {
                by_amount[200_000_000],
                by_amount[300_000_000],
            },
        )

        self.assertEqual(
            prepared.utxo_selection["strategy"],
            "smallest_first",
        )
        self.assertEqual(
            prepared.utxo_selection["phase"],
            "workload",
        )
        self.assertEqual(
            prepared.utxo_selection["selected_count"],
            2,
        )
        self.assertEqual(
            {
                tuple(row)
                for row in prepared.utxo_selection[
                    "selected_outpoints"
                ]
            },
            set(prepared.inputs),
        )

    def test_snapshot_policy_excludes_outpoint_not_in_cohort(self):
        from flowlab.tx_builder import TxBuilder
        from tests.fake_chain import FakeChain

        wallets = [
            "flab_source",
            "flab_dest",
        ]

        chain = FakeChain(wallets)

        chain.fund(
            "flab_source",
            600_000_000,
        )
        chain.fund(
            "flab_source",
            500_000_000,
        )

        chain.mine(3)

        before = chain.list_unspent(
            "flab_source",
            1,
        )

        allowed = min(
            before,
            key=lambda u: u["amount"],
        )

        cohort = {
            (
                allowed["txid"],
                allowed["vout"],
            ),
        }

        builder = TxBuilder(
            chain,
            wallets,
            max_fee_sats=10_000_000,
        )

        destination = chain.get_new_address(
            "flab_dest",
            "snapshot-test",
        )

        prepared = builder.build(
            "flab_source",
            destination,
            100_000_000,
            utxo_policy={
                "default": {
                    "scope": "snapshot_at_start",
                    "strategy": "largest_first",
                },
            },
            phase="workload",
            cohort_outpoints=cohort,
            seed_material="snapshot-test",
        )

        self.assertEqual(
            set(prepared.inputs),
            cohort,
        )




class DistributionPolicyIntegrationTests(unittest.TestCase):
    def _builder(self):
        from flowlab.tx_builder import TxBuilder
        from tests.fake_chain import FakeChain

        wallets = [
            "flab_source",
            "flab_dest",
        ]

        chain = FakeChain(wallets)

        chain.fund("flab_source", 900_000_000)
        chain.fund("flab_source", 300_000_000)
        chain.fund("flab_source", 200_000_000)
        chain.mine(3)

        builder = TxBuilder(
            chain,
            wallets,
            max_fee_sats=10_000_000,
        )

        destination = chain.get_new_address(
            "flab_dest",
            "distribution-policy",
        )

        return chain, builder, destination

    def test_distribution_smallest_first_controls_inputs(self):
        chain, builder, destination = self._builder()

        before = chain.list_unspent(
            "flab_source",
            1,
        )

        by_amount = {
            int(u["amount"] * 100_000_000):
                (u["txid"], u["vout"])
            for u in before
        }

        prepared = builder.build_distribution(
            "flab_source",
            450_000_000,
            "percentage",
            [{
                "address": destination,
                "percent_bps": 10_000,
            }],
            utxo_policy={
                "default": {
                    "strategy": "smallest_first",
                },
            },
            phase="terminal_distribution",
            seed_material="distribution-smallest",
        )

        self.assertEqual(
            set(prepared.inputs),
            {
                by_amount[200_000_000],
                by_amount[300_000_000],
            },
        )

        self.assertEqual(
            prepared.utxo_selection["strategy"],
            "smallest_first",
        )
        self.assertEqual(
            prepared.utxo_selection["phase"],
            "terminal_distribution",
        )
        self.assertEqual(
            {
                tuple(row)
                for row in prepared.utxo_selection[
                    "selected_outpoints"
                ]
            },
            set(prepared.inputs),
        )

    def test_distribution_snapshot_sweep_uses_all_eligible_cohort_inputs(self):
        chain, builder, destination = self._builder()

        before = chain.list_unspent(
            "flab_source",
            1,
        )

        selected = sorted(
            before,
            key=lambda u: u["amount"],
        )[:2]

        cohort = {
            (u["txid"], u["vout"])
            for u in selected
        }

        prepared = builder.build_distribution(
            "flab_source",
            "all",
            "percentage",
            [{
                "address": destination,
                "percent_bps": 10_000,
            }],
            utxo_policy={
                "default": {
                    "scope": "snapshot_at_start",
                    "strategy": "largest_first",
                },
            },
            phase="terminal_distribution",
            cohort_outpoints=cohort,
            seed_material="distribution-snapshot",
        )

        self.assertEqual(
            set(prepared.inputs),
            cohort,
        )




class ExecutorUtxoContextTests(unittest.TestCase):
    def test_live_wallet_context_has_no_cohort(self):
        from flowlab.executor import Executor

        engine, _exp_id, flow = CohortCaptureTests._running(
            self,
            scope="live_wallet",
        )

        executor = Executor(
            engine,
            CohortCaptureTests.Rpc(),
            builder=None,
        )

        job = {
            "seq": 7,
            "generated_from_json":
                '{"source":"test","phase":"workload"}',
        }

        ctx = executor._utxo_build_context(
            flow,
            job,
            "flab_source",
        )

        self.assertEqual(ctx["phase"], "workload")
        self.assertIsNone(ctx["cohort_outpoints"])
        self.assertIn(
            "flab_source",
            ctx["seed_material"],
        )

    def test_experiment_start_context_uses_persisted_outpoints(self):
        from flowlab.executor import Executor

        engine, exp_id, flow = CohortCaptureTests._running(
            self,
            scope="snapshot_at_start",
        )

        executor = Executor(
            engine,
            CohortCaptureTests.Rpc(),
            builder=None,
        )

        executor._ensure_experiment_start_cohorts(
            exp_id,
        )

        job = {
            "seq": 1,
            "generated_from_json":
                '{"source":"test","phase":"workload"}',
        }

        ctx = executor._utxo_build_context(
            flow,
            job,
            "flab_source",
        )

        self.assertEqual(
            ctx["cohort_outpoints"],
            {
                ("a" * 64, 2),
                ("b" * 64, 1),
            },
        )

    def test_phase_context_uses_persisted_phase_snapshot(self):
        from flowlab.executor import Executor

        engine, flow = PhaseCohortCaptureTests._running(
            self,
            {
                "default": {
                    "scope": "snapshot_at_phase_start",
                },
            },
        )

        executor = Executor(
            engine,
            PhaseCohortCaptureTests.Rpc(),
            builder=None,
        )

        job = PhaseCohortCaptureTests.job(
            "workload",
        )
        job["seq"] = 3

        executor._ensure_phase_start_cohort(
            flow,
            job,
        )

        ctx = executor._utxo_build_context(
            flow,
            job,
            "flab_source",
        )

        self.assertEqual(
            ctx["cohort_outpoints"],
            {
                ("1" * 64, 0),
            },
        )

    def test_missing_required_snapshot_fails_closed(self):
        from flowlab.executor import Executor
        from flowlab.planner import PlanError

        engine, _exp_id, flow = CohortCaptureTests._running(
            self,
            scope="snapshot_at_start",
        )

        executor = Executor(
            engine,
            CohortCaptureTests.Rpc(),
            builder=None,
        )

        job = {
            "seq": 1,
            "generated_from_json":
                '{"source":"test","phase":"workload"}',
        }

        with self.assertRaisesRegex(
            PlanError,
            "cohort is missing",
        ):
            executor._utxo_build_context(
                flow,
                job,
                "flab_source",
            )


if __name__ == "__main__":
    unittest.main()
