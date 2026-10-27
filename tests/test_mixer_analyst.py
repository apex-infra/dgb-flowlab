import copy
import unittest

from flowlab.mixer_analyst import AnalystError, analyze_pairs
from flowlab.mixer_simulator import simulate_baseline
from flowlab.mixer_evaluation import evaluate_ownership

BASE = {
    "version": 1,
    "mode": "baseline",
    "seed": 2049,
    "hypothesis": "Evaluate public transaction evidence.",
    "synthetic": {
        "participant_count": 5,
        "utxos_per_participant": 3,
        "initial_value_sats": 100_000_000,
        "transaction_count": 30,
        "network_fee_sats": 200,
    },
    "analysis": {"observer": "public_chain"},
}


class PublicAnalystTests(unittest.TestCase):

    def setUp(self):
        self.dataset = simulate_baseline(BASE)
        self.public = self.dataset["public"]
        self.ids = [tx["id"] for tx in self.public["transactions"]]

    def test_returns_one_result_per_pair(self):
        result = analyze_pairs(
            self.public, [[self.ids[0], self.ids[1]]]
        )
        self.assertEqual(len(result["predictions"]), 1)
        self.assertIsNone(result["predictions"][0]["same_owner"])

    def test_ancestry_is_not_ownership(self):
        result = analyze_pairs(
            self.public, [[self.ids[0], self.ids[1]]]
        )
        self.assertIsNone(result["predictions"][0]["same_owner"])
        self.assertIn(
            result["evidence"][0]["relationship"],
            {"ancestor", "descendant", "unconnected"},
        )

    def test_ancestor_relationship(self):
        txs = self.public["transactions"]
        pair = None
        produced = {}

        for tx in txs:
            for uid in tx["inputs"]:
                if uid in produced:
                    pair = [produced[uid], tx["id"]]
                    break
            if pair:
                break
            for output in tx["outputs"]:
                produced[output["id"]] = tx["id"]

        self.assertIsNotNone(pair)
        result = analyze_pairs(self.public, [pair])
        self.assertEqual(
            result["evidence"][0]["relationship"], "ancestor"
        )

    def test_pair_order_normalized(self):
        left, right = self.ids[:2]
        self.assertEqual(
            analyze_pairs(self.public, [[left, right]]),
            analyze_pairs(self.public, [[right, left]]),
        )

    def test_reversed_connected_pair_is_identical(self):
        txs = self.public["transactions"]
        produced = {}
        connected = None

        for tx in txs:
            for uid in tx["inputs"]:
                if uid in produced:
                    connected = [produced[uid], tx["id"]]
                    break
            if connected:
                break
            for output in tx["outputs"]:
                produced[output["id"]] = tx["id"]

        self.assertIsNotNone(connected)

        forward = analyze_pairs(self.public, [connected])
        reverse = analyze_pairs(
            self.public, [list(reversed(connected))]
        )

        self.assertEqual(forward, reverse)
        self.assertIn(
            forward["evidence"][0]["relationship"],
            {"ancestor", "descendant"},
        )

    def test_duplicate_pair_rejected(self):
        left, right = self.ids[:2]
        with self.assertRaises(AnalystError):
            analyze_pairs(
                self.public,
                [[left, right], [right, left]],
            )

    def test_self_pair_rejected(self):
        with self.assertRaises(AnalystError):
            analyze_pairs(self.public, [[self.ids[0], self.ids[0]]])

    def test_unknown_transaction_rejected(self):
        with self.assertRaises(AnalystError):
            analyze_pairs(self.public, [[self.ids[0], "missing"]])

    def test_invalid_pair_shape_rejected(self):
        with self.assertRaises(AnalystError):
            analyze_pairs(self.public, [["only_one"]])

    def test_inputs_not_mutated(self):
        public = copy.deepcopy(self.public)
        pairs = [[self.ids[0], self.ids[1]]]
        before = copy.deepcopy(pairs)
        analyze_pairs(public, pairs)
        self.assertEqual(public, self.public)
        self.assertEqual(pairs, before)

    def test_evaluator_integration(self):
        result = analyze_pairs(
            self.public, [[self.ids[0], self.ids[1]]]
        )
        evaluation = evaluate_ownership(
            {
                "transaction_owners":
                    self.dataset["ground_truth"]["transaction_owners"]
            },
            result["predictions"],
        )
        self.assertEqual(evaluation["counts"]["abstained"], 1)
        self.assertEqual(evaluation["metrics"]["coverage"], 0.0)

    def test_no_ground_truth_in_output(self):
        result = analyze_pairs(
            self.public, [[self.ids[0], self.ids[1]]]
        )
        self.assertNotIn("owner", str(result["evidence"]))
        self.assertNotIn("ground_truth", result)

    def test_empty_pairs_supported(self):
        result = analyze_pairs(self.public, [])
        self.assertEqual(result["predictions"], [])
        self.assertEqual(result["evidence"], [])


if __name__ == "__main__":
    unittest.main()
