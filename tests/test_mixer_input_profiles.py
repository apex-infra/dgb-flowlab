import copy
import unittest

from flowlab.mixer_input_profiles import (
    InputProfileError,
    build_input_profile,
)


class InputProfileTests(unittest.TestCase):

    def test_exact_transaction_count(self):
        result = build_input_profile(2049, 100, 6)
        self.assertEqual(len(result["input_counts"]), 100)

    def test_input_bounds(self):
        result = build_input_profile(2049, 100, 8)
        self.assertTrue(
            all(3 <= count <= 8 for count in result["input_counts"])
        )

    def test_deterministic(self):
        self.assertEqual(
            build_input_profile(2049, 100, 6),
            build_input_profile(2049, 100, 6),
        )

    def test_seed_changes_profile(self):
        first = build_input_profile(42, 100, 6)
        second = build_input_profile(2049, 100, 6)
        self.assertNotEqual(
            first["input_counts"],
            second["input_counts"],
        )

    def test_profile_can_be_reused(self):
        profile = build_input_profile(2049, 100, 6)

        campaign_a = copy.deepcopy(profile["input_counts"])
        campaign_c = copy.deepcopy(profile["input_counts"])

        self.assertEqual(campaign_a, campaign_c)
        self.assertEqual(len(campaign_a), 100)

    def test_allows_single_transaction(self):
        result = build_input_profile(2049, 1, 6)
        self.assertEqual(len(result["input_counts"]), 1)

    def test_minimum_input_range(self):
        result = build_input_profile(2049, 30, 3)
        self.assertEqual(result["input_counts"], [3] * 30)

    def test_maximum_supported_range(self):
        result = build_input_profile(2049, 1000, 12)
        self.assertEqual(len(result["input_counts"]), 1000)
        self.assertTrue(
            all(3 <= count <= 12 for count in result["input_counts"])
        )

    def test_histogram_matches_schedule(self):
        result = build_input_profile(2049, 100, 8)

        expected = {
            count: result["input_counts"].count(count)
            for count in range(3, 9)
        }

        self.assertEqual(result["histogram"], expected)

    def test_total_inputs_consistent(self):
        result = build_input_profile(2049, 100, 8)
        self.assertEqual(
            result["total_inputs"],
            sum(result["input_counts"]),
        )

    def test_invalid_seed_rejected(self):
        for seed in (-1, True, "2049", 2**64):
            with self.subTest(seed=seed):
                with self.assertRaises(InputProfileError):
                    build_input_profile(seed, 30, 6)

    def test_invalid_transaction_count_rejected(self):
        for count in (0, -1, True, 1001, "30"):
            with self.subTest(count=count):
                with self.assertRaises(InputProfileError):
                    build_input_profile(2049, count, 6)

    def test_invalid_max_inputs_rejected(self):
        for maximum in (2, 13, True, "6", 6.0):
            with self.subTest(maximum=maximum):
                with self.assertRaises(InputProfileError):
                    build_input_profile(2049, 30, maximum)

    def test_research_metadata(self):
        result = build_input_profile(2049, 30, 6)
        self.assertEqual(result["execution"], "simulation_only")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["seed"], 2049)
        self.assertEqual(result["transaction_count"], 30)
        self.assertEqual(result["max_inputs"], 6)


if __name__ == "__main__":
    unittest.main()
