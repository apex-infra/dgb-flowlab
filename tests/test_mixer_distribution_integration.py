import copy
import unittest

from flowlab.mixer_workloads import (
    WorkloadError,
    build_seeded_workload,
)
from flowlab.mixer_distribution import build_distribution
from flowlab.mixer_analysis import analyze_ancestry
from flowlab.mixer_common_input import analyze_common_inputs
from flowlab.mixer_utxo_evaluation import evaluate_utxo_ownership


WEIGHTS = {
    "single_owner": 80,
    "collaborative": 10,
    "mixed_ownership": 10,
}


class DistributionIntegrationTests(unittest.TestCase):

    def test_legacy_output_unchanged(self):
        baseline = build_seeded_workload(2049, 30, 6)
        self.assertEqual(
            baseline["summary"]["scenario_composition"],
            {
                "single_owner": 10,
                "collaborative": 10,
                "mixed_ownership": 10,
            },
        )
        self.assertNotIn("scenario_schedule", baseline["summary"])

    def test_custom_distribution(self):
        result = build_seeded_workload(
            2049, 100, 6, scenario_weights=WEIGHTS
        )
        self.assertEqual(
            result["summary"]["scenario_composition"],
            {
                "single_owner": 80,
                "collaborative": 10,
                "mixed_ownership": 10,
            },
        )

    def test_schedule_matches_scheduler(self):
        result = build_seeded_workload(
            2049, 100, 6, scenario_weights=WEIGHTS
        )
        expected = build_distribution(
            2049, 100, WEIGHTS
        )["schedule"]

        self.assertEqual(
            result["summary"]["scenario_schedule"],
            expected,
        )

    def test_public_data_does_not_contain_schedule(self):
        result = build_seeded_workload(
            2049, 30, 6, scenario_weights=WEIGHTS
        )
        self.assertNotIn("scenario_schedule", result["public"])
        self.assertNotIn("scenario", str(result["public"]))
        self.assertNotIn("participant", str(result["public"]))

    def test_custom_workload_deterministic(self):
        first = build_seeded_workload(
            2049, 60, 6, scenario_weights=WEIGHTS
        )
        second = build_seeded_workload(
            2049, 60, 6, scenario_weights=WEIGHTS
        )
        self.assertEqual(first, second)

    def test_custom_workload_passes_public_analysis(self):
        result = build_seeded_workload(
            2049, 30, 6, scenario_weights=WEIGHTS
        )
        ancestry = analyze_ancestry(result["public"])
        self.assertEqual(
            ancestry["summary"]["transaction_count"], 30
        )

    def test_custom_workload_evaluator_compatible(self):
        dataset = build_seeded_workload(
            2049, 100, 6, scenario_weights=WEIGHTS
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

    def test_transaction_ownership_matches_schedule(self):
        dataset = build_seeded_workload(
            2049, 100, 6, scenario_weights=WEIGHTS
        )
        schedule = dataset["summary"]["scenario_schedule"]
        owners = dataset["ground_truth"]["utxo_owners"]

        for tx, scenario in zip(
            dataset["public"]["transactions"], schedule
        ):
            input_owners = [
                owners[uid] for uid in tx["inputs"]
            ]
            unique = len(set(input_owners))

            if scenario == "single_owner":
                self.assertEqual(unique, 1)
            elif scenario == "collaborative":
                self.assertEqual(unique, len(input_owners))
            else:
                self.assertEqual(unique, 2)
                self.assertEqual(
                    max(input_owners.count(owner)
                        for owner in set(input_owners)),
                    len(input_owners) - 1,
                )

    def test_invalid_weights_rejected(self):
        with self.assertRaises(WorkloadError):
            build_seeded_workload(
                2049, 30, 6,
                scenario_weights={
                    "single_owner": 90,
                    "collaborative": 20,
                    "mixed_ownership": 0,
                },
            )

    def test_weights_not_mutated(self):
        original = copy.deepcopy(WEIGHTS)
        build_seeded_workload(
            2049, 30, 6, scenario_weights=WEIGHTS
        )
        self.assertEqual(WEIGHTS, original)

    def test_different_weights_change_composition(self):
        first = build_seeded_workload(
            2049, 100, 6, scenario_weights=WEIGHTS
        )
        second = build_seeded_workload(
            2049, 100, 6,
            scenario_weights={
                "single_owner": 10,
                "collaborative": 70,
                "mixed_ownership": 20,
            },
        )
        self.assertNotEqual(
            first["summary"]["scenario_composition"],
            second["summary"]["scenario_composition"],
        )

    def test_custom_workload_simulation_only(self):
        result = build_seeded_workload(
            2049, 30, 6, scenario_weights=WEIGHTS
        )
        self.assertEqual(result["execution"], "simulation_only")


class CampaignDistributionIntegrationTests(unittest.TestCase):

    def test_custom_campaign_composition(self):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        result = run_seeded_campaign(
            seeds=[42, 2049, 9999],
            transaction_count=100,
            max_inputs=6,
            scenario_weights=WEIGHTS,
        )

        expected = {
            "single_owner": 240,
            "collaborative": 30,
            "mixed_ownership": 30,
        }

        self.assertEqual(
            {
                name: item["transaction_count"]
                for name, item in result["by_scenario"].items()
            },
            expected,
        )

        self.assertEqual(
            result["aggregate"]["transaction_count"], 300
        )

    def test_custom_campaign_classifications(self):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        result = run_seeded_campaign(
            [42, 2049, 9999], 100, 6,
            scenario_weights=WEIGHTS,
        )

        single = result["by_scenario"]["single_owner"]["counts"]
        collaborative = result["by_scenario"]["collaborative"]["counts"]
        mixed = result["by_scenario"]["mixed_ownership"]["counts"]

        self.assertGreater(single["tp"], 0)
        self.assertEqual(single["fp"], 0)

        self.assertEqual(collaborative["tp"], 0)
        self.assertGreater(collaborative["fp"], 0)

        self.assertGreater(mixed["tp"], 0)
        self.assertGreater(mixed["fp"], 0)

        aggregate = result["aggregate"]["counts"]

        for key in aggregate:
            self.assertEqual(
                aggregate[key],
                sum(
                    item["counts"][key]
                    for item in result["by_scenario"].values()
                ),
            )

    def test_custom_campaign_changes_measured_precision(self):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        mostly_single = run_seeded_campaign(
            [42], 100, 6,
            scenario_weights={
                "single_owner": 80,
                "collaborative": 10,
                "mixed_ownership": 10,
            },
        )

        mostly_collaborative = run_seeded_campaign(
            [42], 100, 6,
            scenario_weights={
                "single_owner": 10,
                "collaborative": 70,
                "mixed_ownership": 20,
            },
        )

        self.assertGreater(
            mostly_single["aggregate"]["metrics"]["precision"],
            mostly_collaborative["aggregate"]["metrics"]["precision"],
        )

    def test_custom_campaign_deterministic(self):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        first = run_seeded_campaign(
            [42, 2049], 60, 6,
            scenario_weights=WEIGHTS,
        )
        second = run_seeded_campaign(
            [42, 2049], 60, 6,
            scenario_weights=WEIGHTS,
        )

        self.assertEqual(first, second)

    def test_custom_campaign_rejects_invalid_weights(self):
        from flowlab.mixer_seeded_campaign import (
            SeededCampaignError,
            run_seeded_campaign,
        )

        with self.assertRaises(SeededCampaignError):
            run_seeded_campaign(
                [42], 30, 6,
                scenario_weights={
                    "single_owner": 90,
                    "collaborative": 20,
                    "mixed_ownership": 0,
                },
            )

    def test_legacy_campaign_unchanged(self):
        from flowlab.mixer_seeded_campaign import run_seeded_campaign

        legacy = run_seeded_campaign([42, 2049, 9999], 30, 6)

        self.assertEqual(
            legacy["aggregate"]["prediction_count"], 718
        )
        self.assertEqual(
            legacy["aggregate"]["counts"]["tp"], 370
        )
        self.assertEqual(
            legacy["aggregate"]["counts"]["fp"], 348
        )


if __name__ == "__main__":
    unittest.main()
