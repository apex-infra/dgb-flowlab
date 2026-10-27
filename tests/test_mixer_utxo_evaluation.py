import copy
import unittest

from flowlab.mixer_utxo_evaluation import (
    UtxoEvaluationError,
    evaluate_utxo_ownership,
)
from flowlab.mixer_fixtures import multi_owner_fixture
from flowlab.mixer_simulator import simulate_baseline


def prediction(left, right, same_owner):
    return {
        "left": left,
        "right": right,
        "same_owner": same_owner,
    }


class UtxoEvaluationTests(unittest.TestCase):

    def setUp(self):
        self.dataset = multi_owner_fixture()
        self.truth = self.dataset["ground_truth"]
        self.a = "genesis:a:0"
        self.b = "genesis:b:0"
        self.oa = "simtx:0001:0"
        self.ob = "simtx:0001:1"

    def test_multi_owner_false_positive(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.b, True)],
        )
        self.assertEqual(result["counts"]["fp"], 1)
        self.assertEqual(result["counts"]["tp"], 0)

    def test_multi_owner_true_negative(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.b, False)],
        )
        self.assertEqual(result["counts"]["tn"], 1)

    def test_same_owner_true_positive(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.oa, True)],
        )
        self.assertEqual(result["counts"]["tp"], 1)

    def test_same_owner_false_negative(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.oa, False)],
        )
        self.assertEqual(result["counts"]["fn"], 1)

    def test_complete_confusion_matrix(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [
                prediction(self.a, self.oa, True),
                prediction(self.a, self.ob, True),
                prediction(self.b, self.ob, False),
                prediction(self.a, self.b, False),
            ],
        )
        self.assertEqual(
            {
                k: result["counts"][k]
                for k in ("tp", "fp", "fn", "tn")
            },
            {"tp": 1, "fp": 1, "fn": 1, "tn": 1},
        )
        self.assertEqual(result["metrics"]["precision"], 0.5)
        self.assertEqual(result["metrics"]["recall"], 0.5)

    def test_abstention_supported(self):
        result = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.b, None)],
        )
        self.assertEqual(result["counts"]["abstained"], 1)
        self.assertEqual(result["metrics"]["coverage"], 0.0)

    def test_reversed_pair_is_identical(self):
        forward = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.a, self.b, True)],
        )
        reverse = evaluate_utxo_ownership(
            self.truth,
            [prediction(self.b, self.a, True)],
        )
        self.assertEqual(forward, reverse)

    def test_duplicate_reversed_pair_rejected(self):
        with self.assertRaises(UtxoEvaluationError):
            evaluate_utxo_ownership(
                self.truth,
                [
                    prediction(self.a, self.b, True),
                    prediction(self.b, self.a, False),
                ],
            )

    def test_unknown_utxo_rejected(self):
        with self.assertRaises(UtxoEvaluationError):
            evaluate_utxo_ownership(
                self.truth,
                [prediction(self.a, "missing-utxo", True)],
            )

    def test_transaction_id_is_not_utxo_id(self):
        with self.assertRaises(UtxoEvaluationError):
            evaluate_utxo_ownership(
                self.truth,
                [prediction(self.a, "simtx:0001", True)],
            )

    def test_bad_prediction_value_rejected(self):
        with self.assertRaises(UtxoEvaluationError):
            evaluate_utxo_ownership(
                self.truth,
                [prediction(self.a, self.b, 1)],
            )

    def test_unexpected_truth_field_rejected(self):
        bad = copy.deepcopy(self.truth)
        bad["unexpected"] = "value"
        with self.assertRaises(UtxoEvaluationError):
            evaluate_utxo_ownership(bad, [])

    def test_transaction_metadata_is_not_used_for_scoring(self):
        pairs = [prediction(self.a, self.b, True)]

        minimal = {
            "utxo_owners": copy.deepcopy(
                self.truth["utxo_owners"]
            ),
        }

        extra = copy.deepcopy(minimal)
        extra["transaction_participants"] = {
            "simtx:0001": ["incorrect_metadata"]
        }
        extra["transaction_owners"] = {
            "simtx:0001": "incorrect_metadata"
        }

        first = evaluate_utxo_ownership(minimal, pairs)
        second = evaluate_utxo_ownership(extra, pairs)

        self.assertEqual(first, second)
        self.assertEqual(first["counts"]["fp"], 1)

    def test_input_immutability(self):
        truth = copy.deepcopy(self.truth)
        predictions = [prediction(self.a, self.b, True)]
        original = copy.deepcopy(predictions)

        evaluate_utxo_ownership(truth, predictions)

        self.assertEqual(truth, self.truth)
        self.assertEqual(predictions, original)

    def test_single_owner_baseline_compatible(self):
        config = {
            "version": 1,
            "mode": "baseline",
            "seed": 2049,
            "hypothesis": "UTXO ownership evaluation.",
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
        truth = dataset["ground_truth"]

        first = dataset["public"]["genesis_utxos"][0]["id"]
        second = dataset["public"]["genesis_utxos"][1]["id"]

        result = evaluate_utxo_ownership(
            truth,
            [prediction(first, second, True)],
        )

        self.assertEqual(result["counts"]["tp"], 1)

    def test_no_predictions_is_valid(self):
        result = evaluate_utxo_ownership(self.truth, [])
        self.assertEqual(result["counts"]["total"], 0)
        self.assertIsNone(result["metrics"]["coverage"])

    def test_deterministic(self):
        pairs = [prediction(self.a, self.b, True)]
        self.assertEqual(
            evaluate_utxo_ownership(self.truth, pairs),
            evaluate_utxo_ownership(
                copy.deepcopy(self.truth),
                copy.deepcopy(pairs),
            ),
        )


if __name__ == "__main__":
    unittest.main()
