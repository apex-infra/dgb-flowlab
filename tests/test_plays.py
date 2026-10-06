import unittest

from flowlab.config_schema import validate
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
    def test_first_catalog_contains_random_walk_and_ring(self):
        names = [p["name"] for p in list_plays()]
        self.assertEqual(names, ["random_walk", "ring"])

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
        self.assertEqual(flow["destination_wallet"], "flab_dest")

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
            {"mode": "sweep_workers_to_destination"},
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
