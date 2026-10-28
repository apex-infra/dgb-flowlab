import copy
import unittest
from collections import Counter

from flowlab.mixer_distribution import (
    DistributionError,
    build_distribution,
)

WEIGHTS = {
    "single_owner": 50,
    "collaborative": 30,
    "mixed_ownership": 20,
}


class DistributionTests(unittest.TestCase):

    def test_exact_transaction_count(self):
        result = build_distribution(2049, 100, WEIGHTS)
        self.assertEqual(len(result["schedule"]), 100)

    def test_exact_weight_allocation(self):
        result = build_distribution(2049, 100, WEIGHTS)
        self.assertEqual(
            result["scenario_composition"],
            {
                "single_owner": 50,
                "collaborative": 30,
                "mixed_ownership": 20,
            },
        )

    def test_schedule_matches_composition(self):
        result = build_distribution(2049, 100, WEIGHTS)
        self.assertEqual(
            dict(Counter(result["schedule"])),
            {
                name: count
                for name, count in
                result["scenario_composition"].items()
                if count
            },
        )

    def test_deterministic(self):
        self.assertEqual(
            build_distribution(2049, 100, WEIGHTS),
            build_distribution(2049, 100, WEIGHTS),
        )

    def test_different_seeds_change_order(self):
        first = build_distribution(42, 100, WEIGHTS)
        second = build_distribution(2049, 100, WEIGHTS)

        self.assertNotEqual(
            first["schedule"],
            second["schedule"],
        )
        self.assertEqual(
            first["scenario_composition"],
            second["scenario_composition"],
        )

    def test_small_count_rounding(self):
        result = build_distribution(2049, 7, WEIGHTS)
        self.assertEqual(
            result["scenario_composition"],
            {
                "single_owner": 4,
                "collaborative": 2,
                "mixed_ownership": 1,
            },
        )

    def test_zero_weight_excludes_scenario(self):
        result = build_distribution(
            2049,
            20,
            {
                "single_owner": 100,
                "collaborative": 0,
                "mixed_ownership": 0,
            },
        )
        self.assertEqual(
            set(result["schedule"]),
            {"single_owner"},
        )

    def test_one_transaction_supported(self):
        result = build_distribution(2049, 1, WEIGHTS)
        self.assertEqual(len(result["schedule"]), 1)
        self.assertEqual(
            sum(result["scenario_composition"].values()),
            1,
        )

    def test_invalid_weights_rejected(self):
        invalid = [
            {},
            {"single_owner": 100},
            {
                "single_owner": 50,
                "collaborative": 30,
                "mixed_ownership": 10,
            },
            {
                "single_owner": 50,
                "collaborative": 30,
                "mixed_ownership": 20,
                "unknown": 0,
            },
            {
                "single_owner": True,
                "collaborative": 0,
                "mixed_ownership": 0,
            },
            {
                "single_owner": -10,
                "collaborative": 80,
                "mixed_ownership": 30,
            },
            {
                "single_owner": 50.0,
                "collaborative": 30,
                "mixed_ownership": 20,
            },
        ]

        for weights in invalid:
            with self.subTest(weights=weights):
                with self.assertRaises(DistributionError):
                    build_distribution(2049, 30, weights)

    def test_invalid_seed_rejected(self):
        for seed in (-1, True, "2049", 2**64):
            with self.subTest(seed=seed):
                with self.assertRaises(DistributionError):
                    build_distribution(seed, 30, WEIGHTS)

    def test_invalid_transaction_count_rejected(self):
        for count in (0, -1, 1001, True, "30"):
            with self.subTest(count=count):
                with self.assertRaises(DistributionError):
                    build_distribution(2049, count, WEIGHTS)

    def test_input_not_modified(self):
        weights = copy.deepcopy(WEIGHTS)
        build_distribution(2049, 30, weights)
        self.assertEqual(weights, WEIGHTS)

    def test_results_are_independent(self):
        first = build_distribution(2049, 30, WEIGHTS)
        first["schedule"].clear()
        first["scenario_composition"]["single_owner"] = 999

        second = build_distribution(2049, 30, WEIGHTS)
        self.assertEqual(len(second["schedule"]), 30)
        self.assertNotEqual(
            second["scenario_composition"]["single_owner"],
            999,
        )

    def test_simulation_only(self):
        result = build_distribution(2049, 30, WEIGHTS)
        self.assertEqual(result["execution"], "simulation_only")
        self.assertEqual(result["version"], 1)

    def test_no_ownership_labels(self):
        result = build_distribution(2049, 30, WEIGHTS)
        self.assertNotIn("participant_", str(result))
        self.assertNotIn("utxo_owners", str(result))


if __name__ == "__main__":
    unittest.main()
