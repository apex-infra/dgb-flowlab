import copy
import unittest

from flowlab.mixer_simulator import (
    SimulationError,
    simulate_baseline,
)


BASE = {
    "version": 1,
    "mode": "baseline",
    "seed": 2049,
    "hypothesis": "Measure baseline transaction traceability.",
    "synthetic": {
        "participant_count": 5,
        "utxos_per_participant": 3,
        "initial_value_sats": 100_000_000,
        "transaction_count": 30,
        "network_fee_sats": 200,
    },
    "analysis": {"observer": "public_chain"},
}


class MixerSimulatorTests(unittest.TestCase):

    def test_deterministic(self):
        self.assertEqual(
            simulate_baseline(BASE),
            simulate_baseline(copy.deepcopy(BASE)),
        )

    def test_input_not_mutated(self):
        original = copy.deepcopy(BASE)
        simulate_baseline(BASE)
        self.assertEqual(BASE, original)

    def test_transaction_count(self):
        result = simulate_baseline(BASE)
        self.assertEqual(len(result["public"]["transactions"]), 30)

    def test_genesis_utxo_count(self):
        result = simulate_baseline(BASE)
        self.assertEqual(len(result["public"]["genesis_utxos"]), 15)

    def test_value_conservation(self):
        result = simulate_baseline(BASE)
        initial = 15 * 100_000_000
        fees = 30 * 200
        self.assertEqual(
            result["summary"]["remaining_value_sats"] + fees,
            initial,
        )

    def test_no_double_spending(self):
        result = simulate_baseline(BASE)
        inputs = [
            tx["inputs"][0]
            for tx in result["public"]["transactions"]
        ]
        self.assertEqual(len(inputs), len(set(inputs)))

    def test_each_transaction_balances(self):
        result = simulate_baseline(BASE)
        values = {
            item["id"]: item["value_sats"]
            for item in result["public"]["genesis_utxos"]
        }

        for tx in result["public"]["transactions"]:
            input_total = sum(values[item] for item in tx["inputs"])
            output_total = sum(
                item["value_sats"] for item in tx["outputs"]
            )
            self.assertEqual(input_total, output_total + tx["fee_sats"])
            for output in tx["outputs"]:
                values[output["id"]] = output["value_sats"]

    def test_public_dataset_excludes_ownership(self):
        result = simulate_baseline(BASE)
        public = str(result["public"])
        self.assertNotIn("owner", public)
        self.assertNotIn("participant_", public)

    def test_ground_truth_present(self):
        result = simulate_baseline(BASE)
        self.assertEqual(
            len(result["ground_truth"]["transaction_owners"]),
            30,
        )
        self.assertEqual(
            len(result["ground_truth"]["utxo_owners"]),
            45,
        )

    def test_ground_truth_matches_inputs(self):
        result = simulate_baseline(BASE)
        truth = result["ground_truth"]
        for tx in result["public"]["transactions"]:
            owner = truth["transaction_owners"][tx["id"]]
            for utxo in tx["inputs"]:
                self.assertEqual(truth["utxo_owners"][utxo], owner)
            for output in tx["outputs"]:
                self.assertEqual(
                    truth["utxo_owners"][output["id"]],
                    owner,
                )

    def test_reject_nonbaseline_mode(self):
        config = copy.deepcopy(BASE)
        config["mode"] = "topology"
        with self.assertRaises(SimulationError):
            simulate_baseline(config)

    def test_insufficient_spendable_value_fails(self):
        config = copy.deepcopy(BASE)
        config["synthetic"].update({
            "participant_count": 1,
            "utxos_per_participant": 1,
            "initial_value_sats": 100,
            "transaction_count": 2,
            "network_fee_sats": 50,
        })
        with self.assertRaises(SimulationError):
            simulate_baseline(config)

    def test_unspent_utxos_are_exactly_unconsumed_outputs(self):
        result = simulate_baseline(BASE)

        created = {
            item["id"]
            for item in result["public"]["genesis_utxos"]
        }

        spent = set()

        for tx in result["public"]["transactions"]:
            spent.update(tx["inputs"])
            created.update(
                item["id"] for item in tx["outputs"]
            )

        actual_unspent = {
            item["id"]
            for item in result["public"]["unspent_utxos"]
        }

        self.assertEqual(actual_unspent, created - spent)
        self.assertEqual(
            len(actual_unspent),
            len(result["public"]["unspent_utxos"]),
        )

    def test_unspent_value_matches_summary(self):
        result = simulate_baseline(BASE)

        total_unspent = sum(
            item["value_sats"]
            for item in result["public"]["unspent_utxos"]
        )

        self.assertEqual(
            total_unspent,
            result["summary"]["remaining_value_sats"],
        )

        self.assertEqual(
            total_unspent + result["summary"]["total_fees_sats"],
            result["summary"]["initial_value_sats"],
        )

    def test_unspent_dataset_does_not_expose_owners(self):
        result = simulate_baseline(BASE)

        for item in result["public"]["unspent_utxos"]:
            self.assertEqual(
                set(item),
                {"id", "value_sats"},
            )
            self.assertIn(
                item["id"],
                result["ground_truth"]["utxo_owners"],
            )

    def test_result_independence(self):
        first = simulate_baseline(BASE)
        first["public"]["transactions"].clear()
        second = simulate_baseline(BASE)
        self.assertEqual(len(second["public"]["transactions"]), 30)


if __name__ == "__main__":
    unittest.main()
