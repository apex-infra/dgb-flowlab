
import copy
import unittest

from flowlab.mixer_fixtures import multi_owner_fixture
from flowlab.mixer_analysis import analyze_ancestry


class MultiOwnerFixtureTests(unittest.TestCase):

    def test_deterministic(self):
        self.assertEqual(
            multi_owner_fixture(),
            multi_owner_fixture(),
        )

    def test_independent_results(self):
        first = multi_owner_fixture()
        first["public"]["transactions"].clear()

        second = multi_owner_fixture()
        self.assertEqual(len(second["public"]["transactions"]), 1)

    def test_two_inputs_and_outputs(self):
        dataset = multi_owner_fixture()
        tx = dataset["public"]["transactions"][0]

        self.assertEqual(len(tx["inputs"]), 2)
        self.assertEqual(len(tx["outputs"]), 2)

    def test_inputs_have_different_owners(self):
        dataset = multi_owner_fixture()
        tx = dataset["public"]["transactions"][0]
        owners = dataset["ground_truth"]["utxo_owners"]

        self.assertNotEqual(
            owners[tx["inputs"][0]],
            owners[tx["inputs"][1]],
        )

    def test_ground_truth_identifies_both_participants(self):
        dataset = multi_owner_fixture()
        participants = dataset["ground_truth"]["transaction_participants"]

        self.assertEqual(
            participants["simtx:0001"],
            ["participant_a", "participant_b"],
        )

    def test_public_data_contains_no_owner_labels(self):
        dataset = multi_owner_fixture()
        self.assertNotIn("owner", str(dataset["public"]))
        self.assertNotIn("participant_", str(dataset["public"]))

    def test_public_analyzer_accepts_fixture(self):
        dataset = multi_owner_fixture()
        result = analyze_ancestry(dataset["public"])

        self.assertEqual(result["summary"]["transaction_count"], 1)
        self.assertEqual(result["summary"]["unspent_utxo_count"], 2)

    def test_value_conservation(self):
        dataset = multi_owner_fixture()
        result = analyze_ancestry(dataset["public"])
        summary = result["summary"]

        self.assertEqual(
            summary["initial_value_sats"],
            summary["remaining_value_sats"]
            + summary["total_fees_sats"],
        )

    def test_existing_single_owner_field_is_not_invented(self):
        dataset = multi_owner_fixture()

        self.assertNotIn(
            "transaction_owners",
            dataset["ground_truth"],
        )

    def test_ground_truth_has_all_utxo_owners(self):
        dataset = multi_owner_fixture()
        truth = dataset["ground_truth"]["utxo_owners"]

        public = dataset["public"]
        ids = {
            item["id"] for item in public["genesis_utxos"]
        }

        for tx in public["transactions"]:
            ids.update(item["id"] for item in tx["outputs"])

        self.assertEqual(ids, set(truth))


if __name__ == "__main__":
    unittest.main()
