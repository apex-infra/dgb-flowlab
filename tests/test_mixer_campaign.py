import copy
import unittest

from flowlab.mixer_campaign import (
    CampaignError,
    run_campaign,
)
from flowlab.mixer_scenarios import SCENARIOS


class MixerCampaignTests(unittest.TestCase):

    def test_default_campaign_runs_all_scenarios(self):
        result = run_campaign()
        self.assertEqual(
            [item["scenario"] for item in result["scenarios"]],
            list(SCENARIOS),
        )

    def test_campaign_is_simulation_only(self):
        result = run_campaign()
        self.assertEqual(result["execution"], "simulation_only")
        self.assertEqual(result["kind"], "mixer_research_campaign")

    def test_single_owner_results(self):
        result = run_campaign(["single_owner"])
        item = result["scenarios"][0]

        self.assertEqual(item["counts"]["tp"], 3)
        self.assertEqual(item["counts"]["fp"], 0)
        self.assertEqual(item["metrics"]["precision"], 1.0)

    def test_collaborative_results(self):
        result = run_campaign(["collaborative"])
        item = result["scenarios"][0]

        self.assertEqual(item["counts"]["tp"], 0)
        self.assertEqual(item["counts"]["fp"], 1)
        self.assertEqual(item["metrics"]["precision"], 0.0)

    def test_mixed_results(self):
        result = run_campaign(["mixed_ownership"])
        item = result["scenarios"][0]

        self.assertEqual(item["counts"]["tp"], 1)
        self.assertEqual(item["counts"]["fp"], 2)
        self.assertEqual(item["metrics"]["precision"], 1 / 3)

    def test_aggregate_confusion_matrix(self):
        result = run_campaign()
        counts = result["aggregate"]["counts"]

        self.assertEqual(counts["tp"], 4)
        self.assertEqual(counts["fp"], 3)
        self.assertEqual(counts["tn"], 0)
        self.assertEqual(counts["fn"], 0)
        self.assertEqual(counts["abstained"], 0)
        self.assertEqual(counts["evaluated"], 7)
        self.assertEqual(counts["total"], 7)

    def test_aggregate_precision_uses_micro_average(self):
        result = run_campaign()
        metrics = result["aggregate"]["metrics"]

        self.assertEqual(metrics["precision"], 4 / 7)
        self.assertNotEqual(metrics["precision"], (
            1 + 0 + 1 / 3
        ) / 3)

    def test_aggregate_undefined_rates_remain_null(self):
        result = run_campaign()
        metrics = result["aggregate"]["metrics"]

        # Three actual negative pairs were all predicted positive.
        # FPR = FP / (FP + TN) = 3 / (3 + 0) = 1.0.
        self.assertEqual(metrics["false_positive_rate"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertEqual(metrics["coverage"], 1.0)

    def test_scenario_composition_reported(self):
        result = run_campaign()
        aggregate = result["aggregate"]

        self.assertEqual(aggregate["scenario_count"], 3)
        self.assertEqual(
            aggregate["scenario_composition"],
            {
                "single_owner": 1,
                "collaborative": 1,
                "mixed_ownership": 1,
            },
        )

    def test_subset_selection_preserves_order(self):
        requested = ["mixed_ownership", "single_owner"]
        result = run_campaign(requested)

        self.assertEqual(
            [item["scenario"] for item in result["scenarios"]],
            requested,
        )

    def test_duplicate_scenario_rejected(self):
        with self.assertRaises(CampaignError):
            run_campaign(["single_owner", "single_owner"])

    def test_unknown_scenario_rejected(self):
        with self.assertRaises(CampaignError):
            run_campaign(["nonexistent"])

    def test_invalid_requests_rejected(self):
        invalid = [
            [],
            "single_owner",
            None if False else 123,
            [True],
            [None],
            {},
        ]

        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(CampaignError):
                    run_campaign(item)

    def test_request_is_not_modified(self):
        selection = ["single_owner", "mixed_ownership"]
        original = copy.deepcopy(selection)

        run_campaign(selection)

        self.assertEqual(selection, original)

    def test_deterministic(self):
        self.assertEqual(run_campaign(), run_campaign())

    def test_independent_results(self):
        first = run_campaign()
        first["scenarios"].clear()
        first["aggregate"]["counts"]["tp"] = 999

        second = run_campaign()

        self.assertEqual(len(second["scenarios"]), 3)
        self.assertEqual(second["aggregate"]["counts"]["tp"], 4)

    def test_evidence_counts_are_consistent(self):
        result = run_campaign()

        for item in result["scenarios"]:
            counts = item["counts"]

            self.assertEqual(
                item["prediction_count"],
                counts["total"],
            )
            self.assertEqual(
                counts["evaluated"] + counts["abstained"],
                counts["total"],
            )

    def test_aggregate_equals_sum_of_scenarios(self):
        result = run_campaign()

        for key in (
            "tp", "fp", "tn", "fn",
            "abstained", "evaluated", "total",
        ):
            expected = sum(
                item["counts"][key]
                for item in result["scenarios"]
            )

            self.assertEqual(
                result["aggregate"]["counts"][key],
                expected,
            )


if __name__ == "__main__":
    unittest.main()
