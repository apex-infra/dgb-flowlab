import copy
import unittest

from flowlab.mixer_common_input import (
    analyze_common_inputs,
)
from flowlab.mixer_fixtures import multi_owner_fixture
from flowlab.mixer_simulator import simulate_baseline
from flowlab.mixer_utxo_evaluation import evaluate_utxo_ownership


class CommonInputTests(unittest.TestCase):

    def setUp(self):
        self.dataset = multi_owner_fixture()
        self.public = self.dataset["public"]

    def test_detects_shared_transaction_inputs(self):
        result = analyze_common_inputs(self.public)

        self.assertEqual(len(result["predictions"]), 1)
        self.assertEqual(
            result["predictions"][0],
            {
                "left": "genesis:a:0",
                "right": "genesis:b:0",
                "same_owner": True,
            },
        )

    def test_identifies_false_positive(self):
        analysis = analyze_common_inputs(self.public)

        evaluation = evaluate_utxo_ownership(
            self.dataset["ground_truth"],
            analysis["predictions"],
        )

        self.assertEqual(evaluation["counts"]["fp"], 1)
        self.assertEqual(evaluation["counts"]["tp"], 0)
        self.assertEqual(evaluation["counts"]["evaluated"], 1)

    def test_no_ground_truth_required(self):
        result = analyze_common_inputs(self.public)

        self.assertNotIn("ground_truth", result)
        self.assertNotIn("actual_same_owner", str(result))

    def test_input_not_mutated(self):
        before = copy.deepcopy(self.public)

        analyze_common_inputs(self.public)

        self.assertEqual(self.public, before)

    def test_deterministic(self):
        self.assertEqual(
            analyze_common_inputs(self.public),
            analyze_common_inputs(copy.deepcopy(self.public)),
        )

    def test_single_input_transactions_produce_no_predictions(self):
        config = {
            "version": 1,
            "mode": "baseline",
            "seed": 2049,
            "hypothesis": "Test common-input heuristic.",
            "synthetic": {
                "participant_count": 3,
                "utxos_per_participant": 2,
                "initial_value_sats": 100000,
                "transaction_count": 5,
                "network_fee_sats": 100,
            },
            "analysis": {"observer": "public_chain"},
        }

        dataset = simulate_baseline(config)
        result = analyze_common_inputs(dataset["public"])

        self.assertEqual(result["predictions"], [])
        self.assertEqual(result["summary"]["prediction_count"], 0)

    def test_invalid_public_transaction_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["transactions"][0]["inputs"].append("missing")

        with self.assertRaises(ValueError):
            analyze_common_inputs(bad)

    def test_three_inputs_generate_three_pairs(self):
        public = {
            "genesis_utxos": [
                {"id": "u1", "value_sats": 1000},
                {"id": "u2", "value_sats": 1000},
                {"id": "u3", "value_sats": 1000},
            ],
            "transactions": [{
                "id": "tx1",
                "sequence": 0,
                "inputs": ["u1", "u2", "u3"],
                "outputs": [
                    {"id": "out1", "value_sats": 2990}
                ],
                "fee_sats": 10,
            }],
            "unspent_utxos": [
                {"id": "out1", "value_sats": 2990}
            ],
        }

        result = analyze_common_inputs(public)

        self.assertEqual(
            result["predictions"],
            [
                {"left": "u1", "right": "u2", "same_owner": True},
                {"left": "u1", "right": "u3", "same_owner": True},
                {"left": "u2", "right": "u3", "same_owner": True},
            ],
        )

    def test_no_owner_inference_from_outputs(self):
        result = analyze_common_inputs(self.public)

        predicted_ids = {
            uid
            for item in result["predictions"]
            for uid in (item["left"], item["right"])
        }

        self.assertNotIn("simtx:0001:0", predicted_ids)
        self.assertNotIn("simtx:0001:1", predicted_ids)

    def test_excessive_prediction_count_rejected(self):
        count = 449

        genesis = [
            {"id": f"input:{i:04d}", "value_sats": 1000}
            for i in range(count)
        ]

        public = {
            "genesis_utxos": genesis,
            "transactions": [{
                "id": "large-tx",
                "sequence": 0,
                "inputs": [item["id"] for item in genesis],
                "outputs": [{
                    "id": "large-tx:0",
                    "value_sats": count * 1000 - 100,
                }],
                "fee_sats": 100,
            }],
            "unspent_utxos": [{
                "id": "large-tx:0",
                "value_sats": count * 1000 - 100,
            }],
        }

        # 449 * 448 / 2 = 100576 candidate pairs.
        with self.assertRaisesRegex(
            ValueError, "prediction limit"
        ):
            analyze_common_inputs(public)

    def test_policy_is_explicit(self):
        result = analyze_common_inputs(self.public)

        self.assertEqual(
            result["policy"],
            "common_input_same_owner_hypothesis",
        )


if __name__ == "__main__":
    unittest.main()
