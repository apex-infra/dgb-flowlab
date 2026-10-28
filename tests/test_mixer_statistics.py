import copy
import math
import unittest

from flowlab.mixer_statistics import (
    StatisticsError,
    summarize_seed_statistics,
)
from flowlab.mixer_seeded_campaign import run_seeded_campaign


class ResearchStatisticsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.report = run_seeded_campaign(
            seeds=[42, 2049, 9999],
            transaction_count=100,
            max_inputs=6,
            matched_input_profile=True,
            scenario_weights={
                "single_owner": 40,
                "collaborative": 30,
                "mixed_ownership": 30,
            },
        )

    def test_simulation_only(self):
        result = summarize_seed_statistics(self.report)
        self.assertEqual(result["execution"], "simulation_only")

    def test_seed_count(self):
        result = summarize_seed_statistics(self.report)
        self.assertEqual(result["seed_count"], 3)

    def test_prediction_count(self):
        result = summarize_seed_statistics(self.report)
        self.assertEqual(
            result["total_predictions"],
            self.report["aggregate"]["prediction_count"],
        )

    def test_classification_counts(self):
        result = summarize_seed_statistics(self.report)
        self.assertEqual(
            result["counts"],
            self.report["aggregate"]["counts"],
        )

    def test_mean_precision(self):
        result = summarize_seed_statistics(self.report)

        values = [
            seed["metrics"]["precision"]
            for seed in self.report["seeds"]
        ]

        self.assertAlmostEqual(
            result["precision"]["mean"],
            sum(values) / len(values),
        )

    def test_micro_precision(self):
        result = summarize_seed_statistics(self.report)
        self.assertEqual(
            result["precision"]["micro"],
            self.report["aggregate"]["metrics"]["precision"],
        )

    def test_minimum_maximum(self):
        result = summarize_seed_statistics(self.report)

        values = [
            seed["metrics"]["precision"]
            for seed in self.report["seeds"]
        ]

        self.assertEqual(
            result["precision"]["minimum"], min(values)
        )
        self.assertEqual(
            result["precision"]["maximum"], max(values)
        )

    def test_standard_deviation(self):
        result = summarize_seed_statistics(self.report)

        values = [
            seed["metrics"]["precision"]
            for seed in self.report["seeds"]
        ]

        mean = sum(values) / len(values)

        expected = math.sqrt(
            sum((value - mean) ** 2 for value in values)
            / len(values)
        )

        self.assertAlmostEqual(
            result["precision"]["population_stddev"],
            expected,
        )

    def test_deterministic(self):
        self.assertEqual(
            summarize_seed_statistics(self.report),
            summarize_seed_statistics(self.report),
        )

    def test_source_not_modified(self):
        original = copy.deepcopy(self.report)
        summarize_seed_statistics(self.report)
        self.assertEqual(self.report, original)

    def test_invalid_report_rejected(self):
        for value in (None, [], {}, "report"):
            with self.subTest(value=value):
                with self.assertRaises(StatisticsError):
                    summarize_seed_statistics(value)

    def test_invalid_execution_rejected(self):
        report = copy.deepcopy(self.report)
        report["execution"] = "live"

        with self.assertRaises(StatisticsError):
            summarize_seed_statistics(report)

    def test_inconsistent_counts_rejected(self):
        report = copy.deepcopy(self.report)
        report["aggregate"]["prediction_count"] += 1

        with self.assertRaises(StatisticsError):
            summarize_seed_statistics(report)

    def test_undefined_precision_handling(self):
        report = copy.deepcopy(self.report)

        report["seeds"][0]["counts"] = {
            "tp": 0, "fp": 0, "tn": 0, "fn": 0,
            "abstained": 0, "evaluated": 0, "total": 0,
        }

        report["seeds"][0]["prediction_count"] = 0
        report["seeds"][0]["metrics"]["precision"] = None

        # Recalculate aggregate counts from the remaining seeds.
        keys = report["aggregate"]["counts"]

        for key in keys:
            keys[key] = sum(
                item["counts"][key]
                for item in report["seeds"]
            )

        report["aggregate"]["prediction_count"] = keys["total"]

        denominator = keys["tp"] + keys["fp"]

        report["aggregate"]["metrics"]["precision"] = (
            keys["tp"] / denominator if denominator else None
        )

        result = summarize_seed_statistics(report)

        self.assertEqual(result["precision"]["defined_seeds"], 2)
        self.assertEqual(result["precision"]["undefined_seeds"], 1)

    def test_zero_variance(self):
        report = copy.deepcopy(self.report)

        for item in report["seeds"]:
            item["counts"] = {
                "tp": 3, "fp": 1, "tn": 0, "fn": 0,
                "abstained": 0, "evaluated": 4, "total": 4,
            }
            item["prediction_count"] = 4
            item["metrics"]["precision"] = 0.75

        counts = report["aggregate"]["counts"]

        for key in counts:
            counts[key] = sum(
                item["counts"][key]
                for item in report["seeds"]
            )

        report["aggregate"]["prediction_count"] = 12
        report["aggregate"]["metrics"]["precision"] = 0.75

        result = summarize_seed_statistics(report)

        self.assertEqual(result["precision"]["mean"], 0.75)
        self.assertEqual(
            result["precision"]["population_stddev"], 0.0
        )



class PairedCampaignStatisticsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reports = {}

        distributions = {
            "A": (80, 10, 10),
            "B": (40, 30, 30),
            "C": (10, 70, 20),
        }

        for name, values in distributions.items():
            cls.reports[name] = run_seeded_campaign(
                seeds=[42, 2049, 9999],
                transaction_count=100,
                max_inputs=6,
                matched_input_profile=True,
                scenario_weights=dict(zip(
                    (
                        "single_owner",
                        "collaborative",
                        "mixed_ownership",
                    ),
                    values,
                )),
            )

    def compare(self):
        from flowlab.mixer_statistics import (
            compare_seeded_campaigns,
        )

        return compare_seeded_campaigns(self.reports)

    def test_all_campaigns_present(self):
        result = self.compare()

        self.assertEqual(
            list(result["campaigns"]),
            ["A", "B", "C"],
        )

        self.assertEqual(result["seed_count"], 3)

    def test_prediction_counts_match(self):
        result = self.compare()

        self.assertEqual(
            result["predictions_per_campaign"],
            2581,
        )

        self.assertTrue(
            result["per_seed_prediction_counts_equal"]
        )

    def test_paired_precision_direction(self):
        result = self.compare()

        self.assertGreater(
            result["comparisons"]["A_vs_B"]["mean_delta"],
            0,
        )

        self.assertGreater(
            result["comparisons"]["A_vs_C"]["mean_delta"],
            result["comparisons"]["A_vs_B"]["mean_delta"],
        )

    def test_paired_deltas_match_seed_metrics(self):
        result = self.compare()

        a = self.reports["A"]["seeds"]
        c = self.reports["C"]["seeds"]

        expected = [
            left["metrics"]["precision"]
            - right["metrics"]["precision"]
            for left, right in zip(a, c)
        ]

        self.assertEqual(
            result["comparisons"]["A_vs_C"]["deltas"],
            expected,
        )

    def test_mismatched_seed_order_rejected(self):
        from flowlab.mixer_statistics import (
            StatisticsError,
            compare_seeded_campaigns,
        )

        reports = copy.deepcopy(self.reports)

        reports["B"]["seeds"].reverse()

        with self.assertRaises(StatisticsError):
            compare_seeded_campaigns(reports)

    def test_mismatched_predictions_rejected(self):
        from flowlab.mixer_statistics import (
            StatisticsError,
            compare_seeded_campaigns,
        )

        reports = copy.deepcopy(self.reports)

        # Remove one valid seed prediction and adjust its
        # aggregate consistently.
        seed = reports["C"]["seeds"][0]
        seed["counts"]["fp"] -= 1
        seed["counts"]["evaluated"] -= 1
        seed["counts"]["total"] -= 1
        seed["prediction_count"] -= 1

        seed["metrics"]["precision"] = (
            seed["counts"]["tp"]
            / (
                seed["counts"]["tp"]
                + seed["counts"]["fp"]
            )
        )

        agg = reports["C"]["aggregate"]

        agg["counts"]["fp"] -= 1
        agg["counts"]["evaluated"] -= 1
        agg["counts"]["total"] -= 1
        agg["prediction_count"] -= 1

        agg["metrics"]["precision"] = (
            agg["counts"]["tp"]
            / (
                agg["counts"]["tp"]
                + agg["counts"]["fp"]
            )
        )

        with self.assertRaises(StatisticsError):
            compare_seeded_campaigns(reports)

    def test_reports_not_mutated(self):
        original = copy.deepcopy(self.reports)

        self.compare()

        self.assertEqual(self.reports, original)

    def test_invalid_campaign_collection_rejected(self):
        from flowlab.mixer_statistics import (
            StatisticsError,
            compare_seeded_campaigns,
        )

        for value in (
            {},
            [],
            {"A": self.reports["A"]},
            {"A": self.reports["A"], "B": None},
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(StatisticsError):
                    compare_seeded_campaigns(value)

if __name__ == "__main__":
    unittest.main()
