import copy
import unittest

from flowlab.mixer_workloads import (
    WorkloadError,
    build_seeded_workload,
)
from flowlab.mixer_analysis import analyze_ancestry
from flowlab.mixer_common_input import analyze_common_inputs
from flowlab.mixer_utxo_evaluation import evaluate_utxo_ownership


class SeededWorkloadTests(unittest.TestCase):

    def setUp(self):
        self.data = build_seeded_workload(
            seed=2049,
            transaction_count=30,
            max_inputs=6,
        )

    def test_deterministic(self):
        self.assertEqual(
            self.data,
            build_seeded_workload(2049, 30, 6),
        )

    def test_seed_changes_dataset(self):
        other = build_seeded_workload(2050, 30, 6)
        self.assertNotEqual(self.data["public"], other["public"])

    def test_transaction_count(self):
        self.assertEqual(
            len(self.data["public"]["transactions"]), 30
        )

    def test_all_three_scenarios_present(self):
        composition = self.data["summary"]["scenario_composition"]
        self.assertEqual(
            composition,
            {
                "single_owner": 10,
                "collaborative": 10,
                "mixed_ownership": 10,
            },
        )

    def test_multiple_input_sizes(self):
        sizes = {
            len(tx["inputs"])
            for tx in self.data["public"]["transactions"]
        }
        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), 6)

    def test_public_ownership_labels_absent(self):
        public = str(self.data["public"]).lower()
        self.assertNotIn("participant", public)
        self.assertNotIn("owner", public)

    def test_opaque_genesis_identifiers(self):
        ids = [
            item["id"]
            for item in self.data["public"]["genesis_utxos"]
        ]
        self.assertEqual(len(ids), len(set(ids)))
        for uid in ids:
            self.assertRegex(uid, r"^genesis:[0-9a-f]{32}$")

    def test_public_analyzer_accepts_dataset(self):
        result = analyze_ancestry(self.data["public"])
        self.assertEqual(
            result["summary"]["transaction_count"], 30
        )

    def test_value_conservation(self):
        summary = analyze_ancestry(
            self.data["public"]
        )["summary"]
        self.assertEqual(
            summary["initial_value_sats"],
            summary["remaining_value_sats"]
            + summary["total_fees_sats"],
        )

    def test_all_utxos_have_ground_truth(self):
        public = self.data["public"]
        ids = {
            item["id"]
            for item in public["genesis_utxos"]
        }
        for tx in public["transactions"]:
            ids.update(item["id"] for item in tx["outputs"])
        self.assertEqual(
            ids,
            set(self.data["ground_truth"]["utxo_owners"]),
        )

    def test_each_transaction_participants_match_inputs(self):
        owners = self.data["ground_truth"]["utxo_owners"]
        participants = self.data["ground_truth"][
            "transaction_participants"
        ]
        for tx in self.data["public"]["transactions"]:
            expected = sorted({
                owners[uid] for uid in tx["inputs"]
            })
            self.assertEqual(participants[tx["id"]], expected)

    def test_common_input_evaluator_integration(self):
        analysis = analyze_common_inputs(self.data["public"])
        result = evaluate_utxo_ownership(
            self.data["ground_truth"],
            analysis["predictions"],
        )
        self.assertGreater(result["counts"]["tp"], 0)
        self.assertGreater(result["counts"]["fp"], 0)
        self.assertEqual(
            result["counts"]["total"],
            len(analysis["predictions"]),
        )

    def test_no_mutable_result_sharing(self):
        original = build_seeded_workload(2049, 30, 6)
        changed = copy.deepcopy(original)
        changed["public"]["transactions"].clear()
        self.assertEqual(
            build_seeded_workload(2049, 30, 6),
            original,
        )

    def test_invalid_parameters_rejected(self):
        cases = [
            {"seed": -1},
            {"seed": True},
            {"seed": 2**64},
            {"transaction_count": 0},
            {"transaction_count": 1001},
            {"transaction_count": True},
            {"max_inputs": 2},
            {"max_inputs": 13},
            {"max_inputs": "5"},
        ]
        for override in cases:
            with self.subTest(override=override):
                args = {
                    "seed": 2049,
                    "transaction_count": 30,
                    "max_inputs": 6,
                }
                args.update(override)
                with self.assertRaises(WorkloadError):
                    build_seeded_workload(**args)

    def test_single_transaction_supported(self):
        result = build_seeded_workload(1, 1, 3)
        self.assertEqual(len(result["public"]["transactions"]), 1)

    def test_ownership_structure_across_seeds(self):
        from collections import Counter

        scenarios = (
            "single_owner",
            "collaborative",
            "mixed_ownership",
        )

        for seed in (0, 1, 42, 2049, 999999):
            with self.subTest(seed=seed):
                dataset = build_seeded_workload(seed, 30, 8)
                owners = dataset["ground_truth"]["utxo_owners"]

                for index, tx in enumerate(
                    dataset["public"]["transactions"]
                ):
                    scenario = scenarios[index % 3]
                    counts = Counter(
                        owners[uid] for uid in tx["inputs"]
                    )

                    if scenario == "single_owner":
                        self.assertEqual(len(counts), 1)

                    elif scenario == "collaborative":
                        self.assertEqual(
                            len(counts), len(tx["inputs"])
                        )
                        self.assertTrue(
                            all(value == 1 for value in counts.values())
                        )

                    else:
                        self.assertEqual(len(counts), 2)
                        self.assertEqual(
                            sorted(counts.values()),
                            [1, len(tx["inputs"]) - 1],
                        )

    def test_per_owner_value_accounting_across_seeds(self):
        from collections import defaultdict

        for seed in (0, 1, 42, 2049, 999999):
            with self.subTest(seed=seed):
                dataset = build_seeded_workload(seed, 30, 8)
                owners = dataset["ground_truth"]["utxo_owners"]

                for tx in dataset["public"]["transactions"]:
                    input_values = defaultdict(int)
                    output_values = defaultdict(int)
                    input_counts = defaultdict(int)

                    for uid in tx["inputs"]:
                        owner = owners[uid]
                        input_values[owner] += 100_000
                        input_counts[owner] += 1

                    for output in tx["outputs"]:
                        owner = owners[output["id"]]
                        output_values[owner] += output["value_sats"]

                    self.assertEqual(
                        set(input_values), set(output_values)
                    )

                    for owner, value in input_values.items():
                        self.assertEqual(
                            output_values[owner],
                            value - input_counts[owner] * 100,
                        )

                    self.assertEqual(
                        tx["fee_sats"],
                        sum(input_counts.values()) * 100,
                    )

    def test_multiple_seeds_preserve_valid_public_graphs(self):
        snapshots = []

        for seed in (0, 1, 42, 2049, 999999):
            dataset = build_seeded_workload(seed, 30, 8)
            public = dataset["public"]

            ancestry = analyze_ancestry(public)
            analysis = analyze_common_inputs(public)
            evaluation = evaluate_utxo_ownership(
                dataset["ground_truth"],
                analysis["predictions"],
            )

            self.assertEqual(
                ancestry["summary"]["transaction_count"], 30
            )
            self.assertEqual(
                evaluation["counts"]["total"],
                len(analysis["predictions"]),
            )
            self.assertEqual(
                evaluation["counts"]["tp"]
                + evaluation["counts"]["fp"],
                evaluation["counts"]["evaluated"],
            )

            snapshots.append(public["transactions"])

        self.assertEqual(
            len({str(snapshot) for snapshot in snapshots}),
            len(snapshots),
        )

    def test_simulation_only(self):
        self.assertEqual(
            self.data["execution"], "simulation_only"
        )


if __name__ == "__main__":
    unittest.main()
