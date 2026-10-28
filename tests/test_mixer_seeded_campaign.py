import copy
import unittest

from flowlab.mixer_seeded_campaign import (
    SeededCampaignError,
    run_seeded_campaign,
)
from flowlab.mixer_workloads import SCENARIOS


class SeededCampaignTests(unittest.TestCase):

    def setUp(self):
        self.result = run_seeded_campaign(
            seeds=[42, 2049, 9999],
            transaction_count=30,
            max_inputs=6,
        )

    def test_simulation_only(self):
        self.assertEqual(
            self.result["execution"], "simulation_only"
        )

    def test_three_seeds_reported(self):
        self.assertEqual(
            [item["seed"] for item in self.result["seeds"]],
            [42, 2049, 9999],
        )

    def test_scenario_reports_present(self):
        self.assertEqual(
            set(self.result["by_scenario"]),
            set(SCENARIOS),
        )

    def test_scenario_composition(self):
        for name in SCENARIOS:
            self.assertEqual(
                self.result["by_scenario"][name]["transaction_count"],
                30,
            )

    def test_total_transaction_count(self):
        self.assertEqual(
            self.result["aggregate"]["transaction_count"], 90
        )

    def test_aggregate_counts_match_seeds(self):
        aggregate = self.result["aggregate"]["counts"]

        for key in (
            "tp", "fp", "tn", "fn",
            "abstained", "evaluated", "total",
        ):
            self.assertEqual(
                aggregate[key],
                sum(
                    item["counts"][key]
                    for item in self.result["seeds"]
                ),
            )

    def test_scenario_counts_match_aggregate(self):
        aggregate = self.result["aggregate"]["counts"]

        for key in (
            "tp", "fp", "tn", "fn",
            "abstained", "evaluated", "total",
        ):
            self.assertEqual(
                aggregate[key],
                sum(
                    item["counts"][key]
                    for item in self.result[
                        "by_scenario"
                    ].values()
                ),
            )

    def test_precision_uses_pooled_counts(self):
        counts = self.result["aggregate"]["counts"]
        expected = counts["tp"] / (
            counts["tp"] + counts["fp"]
        )
        self.assertEqual(
            self.result["aggregate"]["metrics"]["precision"],
            expected,
        )

    def test_single_owner_has_no_false_positives(self):
        counts = self.result[
            "by_scenario"
        ]["single_owner"]["counts"]
        self.assertGreater(counts["tp"], 0)
        self.assertEqual(counts["fp"], 0)

    def test_collaborative_has_no_true_positives(self):
        counts = self.result[
            "by_scenario"
        ]["collaborative"]["counts"]
        self.assertEqual(counts["tp"], 0)
        self.assertGreater(counts["fp"], 0)

    def test_mixed_has_both_outcomes(self):
        counts = self.result[
            "by_scenario"
        ]["mixed_ownership"]["counts"]
        self.assertGreater(counts["tp"], 0)
        self.assertGreater(counts["fp"], 0)

    def test_deterministic(self):
        self.assertEqual(
            self.result,
            run_seeded_campaign([42, 2049, 9999], 30, 6),
        )

    def test_seed_order_preserved(self):
        result = run_seeded_campaign([9999, 42], 12, 5)
        self.assertEqual(
            [item["seed"] for item in result["seeds"]],
            [9999, 42],
        )

    def test_duplicate_seed_rejected(self):
        with self.assertRaises(SeededCampaignError):
            run_seeded_campaign([42, 42], 30, 6)

    def test_invalid_seed_rejected(self):
        for seeds in ([], "42", [True], [-1], [2**64]):
            with self.subTest(seeds=seeds):
                with self.assertRaises(SeededCampaignError):
                    run_seeded_campaign(seeds, 30, 6)

    def test_excessive_prediction_budget_rejected(self):
        with self.assertRaisesRegex(
            SeededCampaignError, "prediction budget"
        ):
            run_seeded_campaign(
                list(range(8)), 300, 12
            )

    def test_invalid_workload_settings_rejected(self):
        for count, maximum in (
            (0, 6),
            (1001, 6),
            (True, 6),
            (30, 2),
            (30, 13),
            (30, True),
        ):
            with self.subTest(count=count, maximum=maximum):
                with self.assertRaises(SeededCampaignError):
                    run_seeded_campaign([42], count, maximum)

    def test_request_not_mutated(self):
        seeds = [42, 2049]
        original = copy.deepcopy(seeds)
        run_seeded_campaign(seeds, 12, 5)
        self.assertEqual(seeds, original)

    def test_no_ground_truth_in_report(self):
        self.assertNotIn("ground_truth", str(self.result))
        self.assertNotIn("utxo_owners", str(self.result))

    def test_prediction_counts_are_consistent(self):
        aggregate = self.result["aggregate"]
        counts = aggregate["counts"]
        self.assertEqual(
            counts["total"],
            aggregate["prediction_count"],
        )
        self.assertEqual(
            counts["total"],
            counts["evaluated"] + counts["abstained"],
        )


if __name__ == "__main__":
    unittest.main()
