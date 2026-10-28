import unittest

from flowlab.mixer_workloads import (
    WorkloadError,
    build_seeded_workload,
)
from flowlab.mixer_input_profiles import build_input_profile
from flowlab.mixer_analysis import analyze_ancestry
from flowlab.mixer_common_input import analyze_common_inputs
from flowlab.mixer_utxo_evaluation import evaluate_utxo_ownership


A = {
    "single_owner": 80,
    "collaborative": 10,
    "mixed_ownership": 10,
}

C = {
    "single_owner": 10,
    "collaborative": 70,
    "mixed_ownership": 20,
}


class MatchedWorkloadIntegrationTests(unittest.TestCase):

    def build(self, weights):
        return build_seeded_workload(
            seed=2049,
            transaction_count=100,
            max_inputs=6,
            scenario_weights=weights,
            matched_input_profile=True,
        )

    def test_exact_profile_match(self):
        dataset = self.build(A)
        profile = build_input_profile(2049, 100, 6)

        actual = [
            len(tx["inputs"])
            for tx in dataset["public"]["transactions"]
        ]

        self.assertEqual(actual, profile["input_counts"])

    def test_distributions_share_input_counts(self):
        first = self.build(A)
        second = self.build(C)

        first_counts = [
            len(tx["inputs"])
            for tx in first["public"]["transactions"]
        ]

        second_counts = [
            len(tx["inputs"])
            for tx in second["public"]["transactions"]
        ]

        self.assertEqual(first_counts, second_counts)

    def test_distributions_retain_distinct_composition(self):
        first = self.build(A)
        second = self.build(C)

        self.assertEqual(
            first["summary"]["scenario_composition"],
            A,
        )
        self.assertEqual(
            second["summary"]["scenario_composition"],
            C,
        )

    def test_matched_workload_deterministic(self):
        self.assertEqual(self.build(A), self.build(A))

    def test_legacy_workload_unchanged(self):
        original = build_seeded_workload(2049, 30, 6)

        explicit = build_seeded_workload(
            2049, 30, 6,
            matched_input_profile=False,
        )

        self.assertEqual(original, explicit)
        self.assertNotIn("matched_input_profile", original["summary"])

    def test_matched_metadata_not_public(self):
        dataset = self.build(A)

        self.assertTrue(
            dataset["summary"]["matched_input_profile"]
        )

        self.assertNotIn(
            "matched_input_profile",
            str(dataset["public"]),
        )

        self.assertNotIn(
            "scenario_schedule",
            str(dataset["public"]),
        )

    def test_public_analysis_and_evaluation(self):
        dataset = self.build(A)

        ancestry = analyze_ancestry(dataset["public"])
        self.assertEqual(
            ancestry["summary"]["transaction_count"],
            100,
        )

        analysis = analyze_common_inputs(dataset["public"])

        evaluation = evaluate_utxo_ownership(
            dataset["ground_truth"],
            analysis["predictions"],
        )

        self.assertEqual(
            evaluation["counts"]["total"],
            len(analysis["predictions"]),
        )

    def test_invalid_matched_flag(self):
        for value in (1, "yes", None, []):
            with self.subTest(value=value):
                with self.assertRaises(WorkloadError):
                    build_seeded_workload(
                        2049, 30, 6,
                        matched_input_profile=value,
                    )


class MatchedCampaignIntegrationTests(unittest.TestCase):

    A = {
        "single_owner": 80,
        "collaborative": 10,
        "mixed_ownership": 10,
    }

    B = {
        "single_owner": 40,
        "collaborative": 30,
        "mixed_ownership": 30,
    }

    C = {
        "single_owner": 10,
        "collaborative": 70,
        "mixed_ownership": 20,
    }

    def campaign(self, weights, seeds=(42, 2049, 9999)):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        return run_seeded_campaign(
            seeds=seeds,
            transaction_count=100,
            max_inputs=6,
            scenario_weights=weights,
            matched_input_profile=True,
        )

    def test_matched_campaign_accepted(self):
        result = self.campaign(self.A)

        self.assertEqual(
            result["aggregate"]["transaction_count"], 300
        )
        self.assertEqual(
            result["execution"], "simulation_only"
        )

    def test_matched_prediction_counts_equal(self):
        first = self.campaign(self.A)
        second = self.campaign(self.C)

        self.assertEqual(
            first["aggregate"]["prediction_count"],
            second["aggregate"]["prediction_count"],
        )

    def test_per_seed_prediction_counts_equal(self):
        first = self.campaign(self.A)
        second = self.campaign(self.C)

        self.assertEqual(
            [
                item["prediction_count"]
                for item in first["seeds"]
            ],
            [
                item["prediction_count"]
                for item in second["seeds"]
            ],
        )

    def test_matched_scenario_composition(self):
        for weights in (self.A, self.B, self.C):
            with self.subTest(weights=weights):
                result = self.campaign(weights, seeds=(42,))

                self.assertEqual(
                    {
                        name: item["transaction_count"]
                        for name, item in
                        result["by_scenario"].items()
                    },
                    weights,
                )

    def test_precision_changes_with_composition(self):
        first = self.campaign(self.A)
        third = self.campaign(self.C)

        self.assertGreater(
            first["aggregate"]["metrics"]["precision"],
            third["aggregate"]["metrics"]["precision"],
        )

    def test_matched_campaign_deterministic(self):
        self.assertEqual(
            self.campaign(self.B),
            self.campaign(self.B),
        )

    def test_invalid_matched_flag_rejected(self):
        from flowlab.mixer_seeded_campaign import (
            SeededCampaignError,
            run_seeded_campaign,
        )

        for flag in (1, None, "yes", []):
            with self.subTest(flag=flag):
                with self.assertRaises(SeededCampaignError):
                    run_seeded_campaign(
                        seeds=[42],
                        transaction_count=30,
                        max_inputs=6,
                        matched_input_profile=flag,
                    )

if __name__ == "__main__":
    unittest.main()
