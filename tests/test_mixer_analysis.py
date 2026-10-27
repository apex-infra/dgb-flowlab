import copy
import unittest

from flowlab.mixer_analysis import (
    AnalysisError,
    analyze_ancestry,
)
from flowlab.mixer_simulator import simulate_baseline


BASE = {
    "version": 1,
    "mode": "baseline",
    "seed": 2049,
    "hypothesis": "Measure public transaction ancestry.",
    "synthetic": {
        "participant_count": 5,
        "utxos_per_participant": 3,
        "initial_value_sats": 100_000_000,
        "transaction_count": 30,
        "network_fee_sats": 200,
    },
    "analysis": {"observer": "public_chain"},
}


class MixerAncestryTests(unittest.TestCase):

    def setUp(self):
        self.dataset = simulate_baseline(BASE)
        self.public = self.dataset["public"]

    def test_deterministic(self):
        self.assertEqual(
            analyze_ancestry(self.public),
            analyze_ancestry(copy.deepcopy(self.public)),
        )

    def test_input_not_mutated(self):
        original = copy.deepcopy(self.public)
        analyze_ancestry(self.public)
        self.assertEqual(self.public, original)

    def test_all_transactions_analyzed(self):
        result = analyze_ancestry(self.public)
        self.assertEqual(len(result["transactions"]), 30)

    def test_direct_parents_match_public_inputs(self):
        result = analyze_ancestry(self.public)
        source_by_output = {}

        for tx in self.public["transactions"]:
            for output in tx["outputs"]:
                source_by_output[output["id"]] = tx["id"]

        for tx in self.public["transactions"]:
            expected = sorted({
                source_by_output[item]
                for item in tx["inputs"]
                if item in source_by_output
            })
            self.assertEqual(
                result["transactions"][tx["id"]]["parents"],
                expected,
            )

    def test_ancestors_are_transitive(self):
        result = analyze_ancestry(self.public)

        for txid, info in result["transactions"].items():
            for parent in info["parents"]:
                self.assertIn(parent, info["ancestors"])
                self.assertTrue(
                    set(
                        result["transactions"][parent]["ancestors"]
                    ).issubset(info["ancestors"])
                )

    def test_no_ground_truth_required(self):
        result = analyze_ancestry(self.public)
        self.assertNotIn("ground_truth", result)
        self.assertNotIn("owner", str(result))

    def test_genesis_inputs_have_no_parent_transaction(self):
        result = analyze_ancestry(self.public)
        genesis = {
            item["id"] for item in self.public["genesis_utxos"]
        }

        for tx in self.public["transactions"]:
            if all(item in genesis for item in tx["inputs"]):
                self.assertEqual(
                    result["transactions"][tx["id"]]["parents"],
                    [],
                )

    def test_unknown_input_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["transactions"][0]["inputs"] = ["not-a-real-utxo"]

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_duplicate_spend_rejected(self):
        bad = copy.deepcopy(self.public)
        first_input = bad["transactions"][0]["inputs"][0]
        bad["transactions"][1]["inputs"] = [first_input]

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_duplicate_output_identifier_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["transactions"][1]["outputs"][0]["id"] = (
            bad["transactions"][0]["outputs"][0]["id"]
        )

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_future_input_rejected(self):
        bad = copy.deepcopy(self.public)
        future_output = bad["transactions"][-1]["outputs"][0]["id"]
        bad["transactions"][0]["inputs"] = [future_output]

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_incorrect_fee_conservation_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["transactions"][0]["fee_sats"] += 1

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_public_unspent_set_verified(self):
        result = analyze_ancestry(self.public)

        self.assertEqual(
            result["summary"]["unspent_utxo_count"],
            len(self.public["unspent_utxos"]),
        )

    def test_tampered_unspent_set_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["unspent_utxos"].pop()

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)

    def test_public_ownership_labels_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["transactions"][0]["owner"] = "participant_0001"

        with self.assertRaises(AnalysisError):
            analyze_ancestry(bad)


if __name__ == "__main__":
    unittest.main()
