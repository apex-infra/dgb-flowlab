import copy
import unittest

from flowlab.mixer_evaluation import (
    EvaluationError,
    evaluate_ownership,
)
from flowlab.mixer_simulator import simulate_baseline


BASE = {
    "version": 1,
    "mode": "baseline",
    "seed": 2049,
    "hypothesis": "Evaluate synthetic ownership predictions.",
    "synthetic": {
        "participant_count": 5,
        "utxos_per_participant": 3,
        "initial_value_sats": 100_000_000,
        "transaction_count": 30,
        "network_fee_sats": 200,
    },
    "analysis": {"observer": "public_chain"},
}

TRUTH = {
    "transaction_owners": {
        "a": "alice",
        "b": "alice",
        "c": "bob",
        "d": "bob",
    }
}


def prediction(left, right, same_owner):
    return {
        "left": left,
        "right": right,
        "same_owner": same_owner,
    }


class MixerEvaluationTests(unittest.TestCase):

    def test_confusion_matrix(self):
        predictions = [
            prediction("a", "b", True),   # TP
            prediction("a", "c", True),   # FP
            prediction("c", "d", False),  # FN
            prediction("b", "d", False),  # TN
        ]
        result = evaluate_ownership(TRUTH, predictions)
        self.assertEqual(
            {key: result["counts"][key] for key in (
                "tp", "fp", "fn", "tn",
            )},
            {"tp": 1, "fp": 1, "fn": 1, "tn": 1},
        )
        self.assertEqual(result["metrics"]["precision"], 0.5)
        self.assertEqual(result["metrics"]["recall"], 0.5)
        self.assertEqual(result["metrics"]["false_positive_rate"], 0.5)

    def test_abstentions_and_coverage(self):
        predictions = [
            prediction("a", "b", True),
            prediction("a", "c", None),
        ]
        result = evaluate_ownership(TRUTH, predictions)
        self.assertEqual(result["counts"]["abstained"], 1)
        self.assertEqual(result["counts"]["evaluated"], 1)
        self.assertEqual(result["counts"]["total"], 2)
        self.assertEqual(result["metrics"]["coverage"], 0.5)

    def test_all_abstained_has_undefined_classification_metrics(self):
        result = evaluate_ownership(
            TRUTH, [prediction("a", "b", None)]
        )
        self.assertIsNone(result["metrics"]["precision"])
        self.assertIsNone(result["metrics"]["recall"])
        self.assertIsNone(result["metrics"]["false_positive_rate"])
        self.assertEqual(result["metrics"]["coverage"], 0.0)

    def test_zero_denominator_is_not_reported_as_zero(self):
        result = evaluate_ownership(
            TRUTH, [prediction("a", "c", False)]
        )
        self.assertIsNone(result["metrics"]["precision"])
        self.assertIsNone(result["metrics"]["recall"])
        self.assertEqual(result["metrics"]["false_positive_rate"], 0.0)

    def test_pair_order_does_not_change_identity(self):
        first = evaluate_ownership(
            TRUTH, [prediction("a", "b", True)]
        )
        second = evaluate_ownership(
            TRUTH, [prediction("b", "a", True)]
        )
        self.assertEqual(first, second)

    def test_duplicate_pair_rejected_even_if_reversed(self):
        with self.assertRaises(EvaluationError):
            evaluate_ownership(TRUTH, [
                prediction("a", "b", True),
                prediction("b", "a", False),
            ])

    def test_self_pair_rejected(self):
        with self.assertRaises(EvaluationError):
            evaluate_ownership(
                TRUTH, [prediction("a", "a", True)]
            )

    def test_unknown_transaction_rejected(self):
        with self.assertRaises(EvaluationError):
            evaluate_ownership(
                TRUTH, [prediction("a", "missing", True)]
            )

    def test_truth_is_required(self):
        with self.assertRaises(EvaluationError):
            evaluate_ownership({}, [])

    def test_boolean_or_none_prediction_only(self):
        for value in (0, 1, "true", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(EvaluationError):
                    evaluate_ownership(
                        TRUTH, [prediction("a", "b", value)]
                    )

    def test_extra_fields_rejected(self):
        item = prediction("a", "b", True)
        item["ground_truth_owner"] = "alice"
        with self.assertRaises(EvaluationError):
            evaluate_ownership(TRUTH, [item])

    def test_inputs_not_modified(self):
        truth = copy.deepcopy(TRUTH)
        predictions = [prediction("a", "b", True)]
        original = copy.deepcopy(predictions)
        evaluate_ownership(truth, predictions)
        self.assertEqual(truth, TRUTH)
        self.assertEqual(predictions, original)

    def test_deterministic(self):
        predictions = [
            prediction("a", "b", True),
            prediction("a", "c", False),
        ]
        self.assertEqual(
            evaluate_ownership(TRUTH, predictions),
            evaluate_ownership(
                copy.deepcopy(TRUTH),
                copy.deepcopy(predictions),
            ),
        )

    def test_real_simulator_ground_truth_compatible(self):
        dataset = simulate_baseline(BASE)
        owners = dataset["ground_truth"]["transaction_owners"]
        by_owner = {}
        for txid, owner in owners.items():
            by_owner.setdefault(owner, []).append(txid)
        same_pair = next(
            (ids[0], ids[1])
            for ids in by_owner.values() if len(ids) >= 2
        )
        result = evaluate_ownership(
            {"transaction_owners": owners},
            [prediction(*same_pair, True)],
        )
        self.assertEqual(result["counts"]["tp"], 1)

    def test_no_predictions_is_valid(self):
        result = evaluate_ownership(TRUTH, [])
        self.assertEqual(result["counts"]["total"], 0)
        self.assertIsNone(result["metrics"]["coverage"])


if __name__ == "__main__":
    unittest.main()
