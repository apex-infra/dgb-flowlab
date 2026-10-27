import copy
import unittest

from flowlab.mixer_scenarios import (
    ScenarioError,
    build_scenario,
    SCENARIOS,
)
from flowlab.mixer_analysis import analyze_ancestry
from flowlab.mixer_common_input import analyze_common_inputs
from flowlab.mixer_utxo_evaluation import evaluate_utxo_ownership


class OwnershipScenarioTests(unittest.TestCase):

    def evaluate(self, name):
        dataset = build_scenario(name)
        analysis = analyze_common_inputs(dataset["public"])
        evaluation = evaluate_utxo_ownership(
            dataset["ground_truth"],
            analysis["predictions"],
        )
        return dataset, analysis, evaluation

    def test_three_scenarios_available(self):
        self.assertEqual(
            set(SCENARIOS),
            {
                "single_owner",
                "collaborative",
                "mixed_ownership",
            },
        )

    def test_single_owner_true_positives(self):
        _, analysis, evaluation = self.evaluate("single_owner")

        self.assertEqual(len(analysis["predictions"]), 3)
        self.assertEqual(evaluation["counts"]["tp"], 3)
        self.assertEqual(evaluation["counts"]["fp"], 0)

    def test_collaborative_false_positive(self):
        _, analysis, evaluation = self.evaluate("collaborative")

        self.assertEqual(len(analysis["predictions"]), 1)
        self.assertEqual(evaluation["counts"]["tp"], 0)
        self.assertEqual(evaluation["counts"]["fp"], 1)

    def test_mixed_has_true_and_false_positives(self):
        _, analysis, evaluation = self.evaluate("mixed_ownership")

        self.assertEqual(len(analysis["predictions"]), 3)
        self.assertEqual(evaluation["counts"]["tp"], 1)
        self.assertEqual(evaluation["counts"]["fp"], 2)
        self.assertEqual(evaluation["metrics"]["precision"], 1 / 3)

    def test_all_scenarios_pass_public_validation(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                dataset = build_scenario(name)
                result = analyze_ancestry(dataset["public"])
                self.assertEqual(
                    result["summary"]["transaction_count"], 1
                )

    def test_all_scenarios_conserve_value(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                dataset = build_scenario(name)
                summary = analyze_ancestry(
                    dataset["public"]
                )["summary"]

                self.assertEqual(
                    summary["initial_value_sats"],
                    summary["remaining_value_sats"]
                    + summary["total_fees_sats"],
                )

    def test_public_data_contains_no_owner_labels(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                public = build_scenario(name)["public"]
                text = str(public).lower()

                self.assertNotIn("participant", text)
                self.assertNotIn("owner", text)
                self.assertNotIn("alice", text)
                self.assertNotIn("bob", text)

    def test_genesis_ids_are_opaque(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                dataset = build_scenario(name)
                ids = [
                    item["id"]
                    for item in dataset["public"]["genesis_utxos"]
                ]

                self.assertEqual(len(ids), len(set(ids)))

                for uid in ids:
                    self.assertRegex(
                        uid, r"^genesis:[0-9a-f]{32}$"
                    )

    def test_every_utxo_has_ground_truth(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                dataset = build_scenario(name)
                public = dataset["public"]
                owners = dataset["ground_truth"]["utxo_owners"]

                ids = {
                    item["id"]
                    for item in public["genesis_utxos"]
                }

                for tx in public["transactions"]:
                    ids.update(
                        output["id"] for output in tx["outputs"]
                    )

                self.assertEqual(ids, set(owners))

    def test_deterministic(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                self.assertEqual(
                    build_scenario(name),
                    build_scenario(name),
                )

    def test_result_independence(self):
        first = build_scenario("mixed_ownership")
        original = copy.deepcopy(first)

        first["public"]["transactions"].clear()
        first["ground_truth"]["utxo_owners"].clear()

        self.assertEqual(
            build_scenario("mixed_ownership"),
            original,
        )

    def test_invalid_scenario_rejected(self):
        for name in (
            "unknown",
            "",
            None,
            0,
            True,
        ):
            with self.subTest(name=name):
                with self.assertRaises(ScenarioError):
                    build_scenario(name)

    def test_transactions_use_valid_multi_input_structure(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                dataset = build_scenario(name)
                tx = dataset["public"]["transactions"][0]

                self.assertGreaterEqual(len(tx["inputs"]), 2)
                self.assertGreaterEqual(len(tx["outputs"]), 1)
                self.assertEqual(tx["sequence"], 0)

    def test_scenarios_are_simulation_only(self):
        for name in SCENARIOS:
            dataset = build_scenario(name)
            self.assertEqual(
                dataset["execution"],
                "simulation_only",
            )


if __name__ == "__main__":
    unittest.main()
