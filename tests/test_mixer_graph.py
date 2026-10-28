import copy
import json
import unittest

from flowlab.mixer_graph import (
    GraphError,
    build_public_graph,
)
from flowlab.mixer_workloads import build_seeded_workload


class PublicGraphTests(unittest.TestCase):

    def setUp(self):
        self.dataset = build_seeded_workload(
            seed=2049,
            transaction_count=30,
            max_inputs=6,
            matched_input_profile=True,
        )
        self.public = self.dataset["public"]

    def test_graph_identity(self):
        graph = build_public_graph(self.public)

        self.assertEqual(
            graph["kind"], "mixer_public_utxo_graph"
        )
        self.assertEqual(graph["version"], 1)
        self.assertEqual(
            graph["execution"], "simulation_only"
        )

    def test_transaction_limit(self):
        graph = build_public_graph(self.public)

        self.assertEqual(
            graph["summary"]["total_transactions"], 30
        )
        self.assertEqual(
            graph["summary"]["visible_transactions"], 24
        )
        self.assertTrue(graph["summary"]["truncated"])

    def test_all_transactions_when_under_limit(self):
        smaller = build_seeded_workload(
            seed=2049,
            transaction_count=20,
            max_inputs=6,
            matched_input_profile=True,
        )

        graph = build_public_graph(
            smaller["public"],
            transaction_limit=24,
        )

        self.assertEqual(
            graph["summary"]["total_transactions"], 20
        )
        self.assertEqual(
            graph["summary"]["visible_transactions"], 20
        )
        self.assertFalse(graph["summary"]["truncated"])

    def test_transaction_nodes(self):
        graph = build_public_graph(self.public)

        transactions = [
            node for node in graph["nodes"]
            if node["kind"] == "transaction"
        ]

        self.assertEqual(len(transactions), 24)

        self.assertEqual(
            transactions[0]["id"], "simtx:00000000"
        )

    def test_input_edges(self):
        graph = build_public_graph(
            self.public,
            transaction_limit=1,
        )

        tx = self.public["transactions"][0]

        edges = [
            edge for edge in graph["edges"]
            if edge["kind"] == "spend"
        ]

        self.assertEqual(
            {(edge["source"], edge["target"]) for edge in edges},
            {(uid, tx["id"]) for uid in tx["inputs"]},
        )

    def test_output_edges(self):
        graph = build_public_graph(
            self.public,
            transaction_limit=1,
        )

        tx = self.public["transactions"][0]

        edges = [
            edge for edge in graph["edges"]
            if edge["kind"] == "create"
        ]

        self.assertEqual(
            {(edge["source"], edge["target"]) for edge in edges},
            {
                (tx["id"], output["id"])
                for output in tx["outputs"]
            },
        )

    def test_all_edge_endpoints_exist(self):
        graph = build_public_graph(self.public)

        ids = {node["id"] for node in graph["nodes"]}

        for edge in graph["edges"]:
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)

    def test_unique_node_identifiers(self):
        graph = build_public_graph(self.public)

        ids = [node["id"] for node in graph["nodes"]]

        self.assertEqual(len(ids), len(set(ids)))

    def test_deterministic(self):
        first = build_public_graph(self.public)
        second = build_public_graph(self.public)

        self.assertEqual(first, second)

    def test_no_private_ownership_labels(self):
        graph = build_public_graph(self.public)
        encoded = json.dumps(graph)

        for forbidden in (
            "ground_truth",
            "utxo_owners",
            "transaction_participants",
            "participant_",
            "scenario_schedule",
            "seed",
            "configuration",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_source_not_mutated(self):
        original = copy.deepcopy(self.public)

        build_public_graph(self.public)

        self.assertEqual(original, self.public)

    def test_invalid_limit_rejected(self):
        for limit in (0, -1, True, 1.5, 25, "24"):
            with self.subTest(limit=limit):
                with self.assertRaises(GraphError):
                    build_public_graph(
                        self.public,
                        transaction_limit=limit,
                    )

    def test_malformed_public_graph_rejected(self):
        corrupted = copy.deepcopy(self.public)

        corrupted["transactions"][0]["inputs"][0] = (
            "genesis:00000000000000000000000000000000"
        )

        with self.assertRaises(GraphError):
            build_public_graph(corrupted)

    def test_public_fingerprint_format(self):
        graph = build_public_graph(self.public)

        self.assertEqual(
            len(graph["summary"]["public_sha256"]), 64
        )

    def test_small_graph_has_correct_counts(self):
        graph = build_public_graph(
            self.public,
            transaction_limit=1,
        )

        tx = self.public["transactions"][0]

        self.assertEqual(
            graph["summary"]["visible_transactions"], 1
        )
        self.assertEqual(
            graph["summary"]["visible_spend_edges"],
            len(tx["inputs"]),
        )
        self.assertEqual(
            graph["summary"]["visible_create_edges"],
            len(tx["outputs"]),
        )


if __name__ == "__main__":
    unittest.main()
