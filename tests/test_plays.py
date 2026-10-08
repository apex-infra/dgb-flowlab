import unittest

from flowlab.config_schema import destination_identity, validate
from flowlab.plays import PlayError, compile_play, get_play_spec, list_plays


BASE = {
    "source_wallet": "flab_source",
    "allocation_wallet": "flab_stage",
    "workers": ["flab_a", "flab_b", "flab_c"],
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


class PlayCatalogTests(unittest.TestCase):
    def test_first_catalog_contains_random_walk_ring_and_hub_and_spoke(self):
        names = [p["name"] for p in list_plays()]
        self.assertEqual(
            names,
            [
                "random_walk",
                "ring",
                "hub_and_spoke",
                "fan_out_fan_in",
                "settlement_cycle",
            ],
        )

    def test_get_play_spec_returns_public_metadata(self):
        p = get_play_spec("ring")
        self.assertEqual(p["name"], "ring")
        self.assertTrue(p["title"])
        self.assertTrue(p["description"])

    def test_unknown_play_is_refused(self):
        with self.assertRaises(PlayError):
            get_play_spec("does_not_exist")


class PlayCompilerTests(unittest.TestCase):
    def test_random_walk_compiles_to_normal_valid_experimental_config(self):
        cfg = compile_play("random_walk", BASE)

        # The compiler produces the existing execution schema, not a second
        # play-specific execution format.
        self.assertNotIn("play", cfg)
        self.assertEqual(validate(cfg), cfg)

        flow = cfg["flows"][0]
        self.assertEqual(flow["source_wallet"], "flab_source")
        self.assertEqual(flow["allocation_wallet"], "flab_stage")
        self.assertEqual(
            flow["flow_wallets"],
            ["flab_stage", "flab_a", "flab_b", "flab_c"],
        )
        self.assertEqual(
            flow["destinations"],
            {
                "mode": "percentage",
                "items": [{
                    "type": "wallet",
                    "wallet": "flab_dest",
                    "percent_bps": 10_000,
                }],
            },
        )
        self.assertEqual(
            flow["finalization_wallet"],
            "flab_stage",
        )
        self.assertNotIn("destination_wallet", flow)

        self.assertEqual(
            flow["experimental_topology"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_stage", "to": "flab_b"},
                {"from": "flab_stage", "to": "flab_c"},
                {"from": "flab_a", "to": "flab_b"},
                {"from": "flab_a", "to": "flab_c"},
                {"from": "flab_b", "to": "flab_a"},
                {"from": "flab_b", "to": "flab_c"},
                {"from": "flab_c", "to": "flab_a"},
                {"from": "flab_c", "to": "flab_b"},
            ],
        )

        self.assertEqual(cfg["workload"]["jobs"], 20)
        self.assertEqual(cfg["randomization"], {
            "enabled": True,
            "model": "uniform",
            "seed": 104,
        })
        self.assertEqual(
            cfg["finalization"],
            {"mode": "consolidate_then_distribute"},
        )

    def test_play_can_compile_to_external_destination(self):
        params = dict(BASE)
        params.pop("destination_wallet")
        params["destination_address"] = "dgb1qexternaldestination"

        cfg = compile_play("random_walk", params)

        self.assertEqual(validate(cfg), cfg)
        flow = cfg["flows"][0]
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
        self.assertNotIn("destination_wallet", flow)
        self.assertNotIn("destination_address", flow)

    def test_play_destination_wallet_and_address_are_mutually_exclusive(self):
        bad = dict(BASE)
        bad["destination_address"] = "dgb1qexternaldestination"

        with self.assertRaisesRegex(
            PlayError,
            "exactly one destination definition",
        ):
            compile_play("random_walk", bad)

    def test_play_accepts_multi_destination_set(self):
        params = dict(BASE)
        params.pop("destination_wallet")
        params["destinations"] = {
            "mode": "fixed",
            "items": [
                {
                    "type": "address",
                    "address": "dgb1qexternaldestination",
                    "amount_sats": 100_000_000,
                },
                {
                    "type": "wallet",
                    "wallet": "flab_dest",
                    "remainder": True,
                },
            ],
        }

        cfg = compile_play("random_walk", params)
        self.assertEqual(validate(cfg), cfg)

        flow = cfg["flows"][0]

        self.assertEqual(
            flow["destinations"],
            params["destinations"],
        )
        self.assertEqual(
            flow["finalization_wallet"],
            "flab_stage",
        )
        self.assertEqual(
            cfg["finalization"]["mode"],
            "consolidate_then_distribute",
        )

    def test_ring_compiles_to_entry_plus_worker_cycle(self):
        cfg = compile_play("ring", BASE)
        self.assertEqual(validate(cfg), cfg)

        transitions = cfg["flows"][0]["experimental_topology"]["transitions"]
        self.assertEqual(
            transitions,
            [
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_a", "to": "flab_b"},
                {"from": "flab_b", "to": "flab_c"},
                {"from": "flab_c", "to": "flab_a"},
            ],
        )

    def test_hub_and_spoke_compiles_to_dedicated_hub_topology(self):
        params = {
            **BASE,
            "hub_wallet": "flab_hub_a",
        }

        cfg = compile_play("hub_and_spoke", params)
        self.assertEqual(validate(cfg), cfg)

        flow = cfg["flows"][0]

        self.assertEqual(
            flow["flow_wallets"],
            [
                "flab_stage",
                "flab_hub_a",
                "flab_a",
                "flab_b",
                "flab_c",
            ],
        )

        self.assertEqual(
            flow["experimental_topology"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_hub_a"},
                {"from": "flab_hub_a", "to": "flab_a"},
                {"from": "flab_hub_a", "to": "flab_b"},
                {"from": "flab_hub_a", "to": "flab_c"},
                {"from": "flab_a", "to": "flab_hub_a"},
                {"from": "flab_b", "to": "flab_hub_a"},
                {"from": "flab_c", "to": "flab_hub_a"},
            ],
        )

    def test_hub_and_spoke_requires_a_distinct_hub(self):
        missing = dict(BASE)

        with self.assertRaisesRegex(
            PlayError,
            "hub_wallet is required",
        ):
            compile_play("hub_and_spoke", missing)

        duplicate = {
            **BASE,
            "hub_wallet": "flab_a",
        }

        with self.assertRaisesRegex(
            PlayError,
            "must all be distinct",
        ):
            compile_play("hub_and_spoke", duplicate)

    def test_fan_out_fan_in_compiles_to_ordered_deterministic_lifecycle(self):
        cfg = compile_play("fan_out_fan_in", BASE)
        self.assertEqual(validate(cfg), cfg)

        self.assertEqual(
            cfg["randomization"],
            {"enabled": False},
        )
        self.assertNotIn("workload", cfg)
        self.assertNotIn("finalization", cfg)

        flow = cfg["flows"][0]

        self.assertEqual(
            flow["flow_wallets"],
            ["flab_stage", "flab_a", "flab_b", "flab_c"],
        )

        transfers = flow["transfers"]

        # Exact reserve -> Stage commitment.
        self.assertEqual(
            transfers[0],
            {
                "from": "flab_source",
                "to": "flab_stage",
                "amount_sats": BASE["allocation_sats"],
                "delay_seconds": 0,
            },
        )

        # Three fan-out jobs, then exactly three fan-in jobs.
        fanout = transfers[1:4]
        fanin = transfers[4:7]
        terminal = transfers[7]

        self.assertEqual(
            {t["from"] for t in fanout},
            {"flab_stage"},
        )
        self.assertEqual(
            {t["to"] for t in fanout},
            {"flab_a", "flab_b", "flab_c"},
        )

        for t in fanout:
            self.assertGreaterEqual(
                t["amount_sats"],
                BASE["amount_sats_min"],
            )
            self.assertLessEqual(
                t["amount_sats"],
                BASE["amount_sats_max"],
            )
            self.assertGreaterEqual(
                t["delay_seconds"],
                BASE["delay_seconds_min"],
            )
            self.assertLessEqual(
                t["delay_seconds"],
                BASE["delay_seconds_max"],
            )

        self.assertLess(
            sum(t["amount_sats"] for t in fanout),
            BASE["allocation_sats"],
        )

        self.assertEqual(
            {t["from"] for t in fanin},
            {"flab_a", "flab_b", "flab_c"},
        )
        self.assertEqual(
            {t["to"] for t in fanin},
            {"flab_stage"},
        )
        self.assertTrue(
            all(t["amount_sats"] == "all" for t in fanin)
        )

        self.assertEqual(
            terminal["from"],
            "flab_stage",
        )
        self.assertEqual(
            terminal["to"],
            "flab_dest",
        )
        self.assertEqual(
            terminal["amount_sats"],
            "all",
        )

    def test_fan_out_fan_in_is_seed_reproducible(self):
        a = compile_play("fan_out_fan_in", BASE)
        b = compile_play(
            "fan_out_fan_in",
            dict(BASE),
        )

        self.assertEqual(a, b)

        changed = dict(BASE)
        changed["seed"] = BASE["seed"] + 1

        c = compile_play("fan_out_fan_in", changed)

        self.assertNotEqual(
            a["flows"][0]["transfers"],
            c["flows"][0]["transfers"],
        )

    def test_fan_out_fan_in_does_not_require_decision_count(self):
        params = dict(BASE)
        params.pop("decisions")

        cfg = compile_play("fan_out_fan_in", params)

        self.assertEqual(validate(cfg), cfg)
        self.assertNotIn("workload", cfg)

    def test_fan_out_fan_in_requires_two_workers(self):
        bad = dict(BASE)
        bad["workers"] = ["flab_a"]

        with self.assertRaisesRegex(
            PlayError,
            "requires at least 2 workers",
        ):
            compile_play("fan_out_fan_in", bad)

    def test_fan_out_fan_in_v1_refuses_multi_destination(self):
        params = dict(BASE)
        params.pop("destination_wallet")
        params["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": "flab_dest",
                    "percent_bps": 10_000,
                },
            ],
        }

        with self.assertRaisesRegex(
            PlayError,
            "does not support multi-destination",
        ):
            compile_play("fan_out_fan_in", params)

    def test_settlement_cycle_compiles_v1_contract(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b", "flab_c"],
            "hubs": ["flab_hub_a"],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 12,
            "return_decisions": 8,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 30,
            "settlement_delay_seconds_max": 180,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 60,
            "confirmations_required": 2,
            "seed": 104,
            "max_total_transactions": 100,
            "settlement": {
                "mode": "fixed",
                "items": [
                    {
                        "type": "address",
                        "address": "DExternalSettlementAddress",
                        "amount_sats": 100_000_000,
                    },
                    {
                        "type": "wallet",
                        "wallet": "flab_dest",
                        "amount_sats": 10_000_000,
                    },
                ],
            },
        }

        cfg = compile_play("settlement_cycle", params)

        self.assertEqual(
            cfg["play"],
            {
                "name": "settlement_cycle",
                "version": 1,
            },
        )

        self.assertEqual(validate(cfg), cfg)

        flow = cfg["flows"][0]

        self.assertEqual(flow["source_wallet"], "flab_source")
        self.assertEqual(flow["allocation_wallet"], "flab_stage")
        self.assertEqual(flow["allocation_sats"], 500_000_000)

        # Settlement Cycle returns to its Reserve and therefore does not
        # masquerade as an ordinary terminal-destination flow.
        self.assertNotIn("destination_wallet", flow)
        self.assertNotIn("destination_address", flow)
        self.assertNotIn("destinations", flow)

        cycle = cfg["settlement_cycle"]

        self.assertEqual(
            cycle["workers"],
            ["flab_a", "flab_b", "flab_c"],
        )
        self.assertEqual(cycle["hubs"], ["flab_hub_a"])

        self.assertEqual(cycle["outbound"]["decisions"], 12)
        self.assertEqual(cycle["return"]["decisions"], 8)

        self.assertFalse(cycle["outbound"]["multi_output"])
        self.assertFalse(cycle["return"]["multi_output"])

        self.assertEqual(
            cycle["settlement"]["source_wallet"],
            "flab_stage",
        )
        self.assertEqual(
            cycle["settlement"]["mode"],
            "fixed",
        )
        self.assertEqual(
            cycle["settlement"]["items"],
            params["settlement"]["items"],
        )
        self.assertTrue(
            cycle["settlement"]["retain_remainder"]
        )

        self.assertEqual(
            cycle["reserve_return"]["from_wallet"],
            "flab_stage",
        )
        self.assertEqual(
            cycle["reserve_return"]["to_wallet"],
            "flab_source",
        )

        self.assertEqual(
            cycle["max_total_transactions"],
            100,
        )

        self.assertEqual(
            cfg["randomization"],
            {
                "enabled": True,
                "model": "seeded_deterministic",
                "seed": 104,
            },
        )

    def test_settlement_cycle_compiles_explicit_outbound_and_return_routes(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": ["flab_hub_a"],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 250,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        cfg = compile_play("settlement_cycle", params)
        cycle = cfg["settlement_cycle"]

        self.assertEqual(
            cycle["outbound"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_hub_a"},
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_stage", "to": "flab_b"},
                {"from": "flab_hub_a", "to": "flab_a"},
                {"from": "flab_hub_a", "to": "flab_b"},
            ],
        )

        self.assertEqual(
            cycle["return"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_hub_a"},
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_stage", "to": "flab_b"},
                {"from": "flab_hub_a", "to": "flab_a"},
                {"from": "flab_hub_a", "to": "flab_b"},
            ],
        )

    def test_settlement_cycle_without_hubs_routes_stage_to_workers_and_back(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 251,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        cfg = compile_play("settlement_cycle", params)
        cycle = cfg["settlement_cycle"]

        self.assertEqual(
            cycle["outbound"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_stage", "to": "flab_b"},
            ],
        )

        self.assertEqual(
            cycle["return"]["transitions"],
            [
                {"from": "flab_stage", "to": "flab_a"},
                {"from": "flab_stage", "to": "flab_b"},
            ],
        )

    def test_settlement_cycle_has_stable_synthetic_flow_identity(self):
        params = {
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
            "seed": 300,
            "max_total_transactions": 100,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        a = compile_play("settlement_cycle", params)
        b = compile_play("settlement_cycle", dict(params))

        flow_a = a["flows"][0]
        flow_b = b["flows"][0]

        self.assertIn("flow_identity", flow_a)
        self.assertTrue(
            flow_a["flow_identity"].startswith("settlement_cycle:")
        )
        self.assertEqual(
            flow_a["flow_identity"],
            flow_b["flow_identity"],
        )
        self.assertEqual(
            destination_identity(flow_a),
            flow_a["flow_identity"],
        )

    def test_settlement_cycle_identity_changes_with_contract(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
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
            "seed": 301,
            "max_total_transactions": 100,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        a = compile_play("settlement_cycle", params)

        changed = dict(params)
        changed["outbound_decisions"] = 11

        b = compile_play("settlement_cycle", changed)

        self.assertNotEqual(
            a["flows"][0]["flow_identity"],
            b["flows"][0]["flow_identity"],
        )

    def test_settlement_cycle_supports_percentage_payout_splits(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 200,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "percentage",
                "items": [
                    {
                        "type": "address",
                        "address": "DExternalA",
                        "percent_bps": 2000,
                    },
                    {
                        "type": "wallet",
                        "wallet": "flab_dest",
                        "percent_bps": 500,
                    },
                ],
            },
        }

        cfg = compile_play("settlement_cycle", params)
        settlement = cfg["settlement_cycle"]["settlement"]

        # Settlement percentages intentionally need not total 100%.
        # The unassigned percentage is retained inside the experiment.
        self.assertEqual(
            sum(
                item["percent_bps"]
                for item in settlement["items"]
            ),
            2500,
        )
        self.assertTrue(settlement["retain_remainder"])

    def test_settlement_cycle_v1_declares_single_output_workload_decisions(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 200,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "percentage",
                "items": [{
                    "type": "wallet",
                    "wallet": "flab_dest",
                    "percent_bps": 500,
                }],
            },
        }

        cfg = compile_play("settlement_cycle", params)
        cycle = cfg["settlement_cycle"]

        self.assertFalse(
            cycle["outbound"]["multi_output"],
            "Settlement Cycle V1 currently generates one recipient per "
            "workload decision and must not advertise true multi-output",
        )
        self.assertFalse(
            cycle["return"]["multi_output"],
            "Settlement Cycle V1 currently generates one recipient per "
            "return-workload decision and must not advertise true multi-output",
        )

    def test_settlement_cycle_decision_counts_are_independent(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 30,
            "return_decisions": 7,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 201,
            "max_total_transactions": 100,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        cfg = compile_play("settlement_cycle", params)

        self.assertEqual(
            cfg["settlement_cycle"]["outbound"]["decisions"],
            30,
        )
        self.assertEqual(
            cfg["settlement_cycle"]["return"]["decisions"],
            7,
        )

    def test_settlement_cycle_requires_positive_decision_counts(self):
        base = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 202,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "fixed",
                "items": [{
                    "type": "address",
                    "address": "DExternalA",
                    "amount_sats": 50_000_000,
                }],
            },
        }

        for key in ("outbound_decisions", "return_decisions"):
            bad = dict(base)
            bad[key] = 0

            with self.subTest(key=key):
                with self.assertRaises(PlayError):
                    compile_play("settlement_cycle", bad)

    def test_settlement_cycle_requires_at_least_one_payout(self):
        params = {
            "source_wallet": "flab_source",
            "allocation_wallet": "flab_stage",
            "workers": ["flab_a", "flab_b"],
            "hubs": [],
            "allocation_sats": 500_000_000,
            "outbound_decisions": 5,
            "return_decisions": 5,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 50_000_000,
            "delay_seconds_min": 5,
            "delay_seconds_max": 60,
            "settlement_delay_seconds_min": 10,
            "settlement_delay_seconds_max": 120,
            "reserve_return_delay_seconds_min": 0,
            "reserve_return_delay_seconds_max": 30,
            "confirmations_required": 2,
            "seed": 203,
            "max_total_transactions": 50,
            "settlement": {
                "mode": "fixed",
                "items": [],
            },
        }

        with self.assertRaises(PlayError):
            compile_play("settlement_cycle", params)

    def test_same_play_parameters_compile_identically(self):
        a = compile_play("random_walk", BASE)
        b = compile_play("random_walk", dict(BASE))
        self.assertEqual(a, b)

    def test_compiler_does_not_mutate_input(self):
        params = dict(BASE)
        params["workers"] = list(BASE["workers"])
        before = {
            **params,
            "workers": list(params["workers"]),
        }

        compile_play("ring", params)
        self.assertEqual(params, before)

    def test_source_destination_and_workers_must_be_distinct(self):
        bad = dict(BASE)
        bad["workers"] = ["flab_a", "flab_source"]

        with self.assertRaises(PlayError):
            compile_play("random_walk", bad)

    def test_random_walk_requires_at_least_one_worker(self):
        bad = dict(BASE)
        bad["workers"] = []

        with self.assertRaises(PlayError):
            compile_play("random_walk", bad)

    def test_ring_requires_at_least_two_workers(self):
        bad = dict(BASE)
        bad["workers"] = ["flab_a"]

        with self.assertRaises(PlayError):
            compile_play("ring", bad)

    def test_amount_bounds_must_fit_allocation(self):
        bad = dict(BASE)
        bad["amount_sats_max"] = bad["allocation_sats"] + 1

        with self.assertRaises(PlayError):
            compile_play("random_walk", bad)

    def test_bad_decision_delay_confirmation_and_seed_values_are_refused(self):
        cases = [
            ("decisions", 0),
            ("delay_seconds_min", -1),
            ("delay_seconds_max", -1),
            ("confirmations_required", 0),
            ("seed", -1),
        ]

        for key, value in cases:
            with self.subTest(key=key):
                bad = dict(BASE)
                bad[key] = value
                with self.assertRaises(PlayError):
                    compile_play("random_walk", bad)


if __name__ == "__main__":
    unittest.main()
