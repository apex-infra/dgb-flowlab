import unittest

from flowlab.infrastructure import collect_infrastructure
from tests.fake_chain import FakeChain


ROLES = {
    "reserve": ["flab_source"],
    "stage": ["flab_stage"],
    "workers": ["flab_a", "flab_b"],
    "hubs": [],
    "destinations": ["flab_dest"],
}

WALLETS = (
    "flab_source",
    "flab_stage",
    "flab_a",
    "flab_b",
    "flab_dest",
)


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain(WALLETS)

        self.chain.fund("flab_source", 100_000_000)
        self.chain.fund("flab_source", 50_000_000)
        self.chain.fund("flab_stage", 25_000_000)
        self.chain.fund("flab_a", 10_000_000)

    def snap(self):
        return collect_infrastructure(
            self.chain,
            WALLETS,
            wallet_roles=ROLES,
        )

    def test_overview_counts_wallets_balances_and_utxos(self):
        data = self.snap()
        overview = data["overview"]

        self.assertEqual(
            overview["wallet_count"],
            5,
        )
        self.assertEqual(
            overview["available_wallet_count"],
            5,
        )
        self.assertEqual(
            overview["managed_balance_sats"],
            185_000_000,
        )
        self.assertEqual(
            overview["utxo_count"],
            4,
        )
        self.assertEqual(
            overview["confirmed_utxo_count"],
            4,
        )
        self.assertEqual(
            overview["unconfirmed_utxo_count"],
            0,
        )

    def test_wallet_breakdown_contains_roles_and_utxo_stats(self):
        data = self.snap()

        source = data["wallets"]["flab_source"]
        stage = data["wallets"]["flab_stage"]

        self.assertEqual(source["role"], "reserve")
        self.assertEqual(stage["role"], "stage")

        self.assertTrue(source["available"])
        self.assertEqual(
            source["trusted_balance_sats"],
            150_000_000,
        )
        self.assertEqual(source["utxo_count"], 2)

        self.assertEqual(
            source["utxo_stats"]["min"],
            50_000_000,
        )
        self.assertEqual(
            source["utxo_stats"]["max"],
            100_000_000,
        )
        self.assertEqual(
            source["utxo_stats"]["median"],
            75_000_000,
        )

    def test_utxo_projection_is_safe_and_exact(self):
        source = self.snap()["wallets"]["flab_source"]

        self.assertEqual(len(source["utxos"]), 2)

        for utxo in source["utxos"]:
            self.assertEqual(
                set(utxo),
                {
                    "txid",
                    "vout",
                    "amount_sats",
                    "confirmed",
                    "confirmations",
                    "address",
                    "spendable",
                    "solvable",
                    "safe",
                },
            )

            self.assertTrue(utxo["confirmed"])
            self.assertTrue(utxo["spendable"])
            self.assertTrue(utxo["safe"])

    def test_empty_wallet_is_available_not_missing(self):
        data = self.snap()

        wallet = data["wallets"]["flab_b"]

        self.assertTrue(wallet["available"])
        self.assertEqual(wallet["trusted_balance_sats"], 0)
        self.assertEqual(wallet["utxo_count"], 0)
        self.assertEqual(wallet["utxos"], [])

    def test_unloaded_configured_wallet_is_reported(self):
        configured = WALLETS + ("flab_missing",)

        data = collect_infrastructure(
            self.chain,
            configured,
            wallet_roles=ROLES,
        )

        missing = data["wallets"]["flab_missing"]

        self.assertFalse(missing["loaded"])
        self.assertFalse(missing["available"])
        self.assertIsNone(
            missing["trusted_balance_sats"]
        )

        self.assertIn(
            "flab_missing",
            data["overview"]["unavailable_wallets"],
        )

    def test_research_metrics_are_bounded(self):
        research = self.snap()["research"]

        for value in research.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_score_is_versioned_and_transparent(self):
        score = self.snap()["score"]

        self.assertEqual(
            score["model"],
            "infrastructure_model_v1",
        )

        self.assertGreaterEqual(score["score"], 0)
        self.assertLessEqual(score["score"], 100)

        self.assertEqual(
            set(score["components"]),
            set(score["weights"]),
        )

        self.assertAlmostEqual(
            sum(score["weights"].values()),
            1.0,
        )

        self.assertIn(
            "descriptive",
            score["interpretation"],
        )

    def test_wallet_research_rank_is_transparent(self):
        data = self.snap()

        source = data["wallets"]["flab_source"]["research"]

        self.assertIsInstance(source["score"], int)
        self.assertGreaterEqual(source["score"], 0)
        self.assertLessEqual(source["score"], 100)

        self.assertEqual(
            set(source["components"]),
            set(source["weights"]),
        )

        self.assertAlmostEqual(
            sum(source["weights"].values()),
            1.0,
        )

        self.assertGreater(source["balance_share"], 0)
        self.assertGreater(source["utxo_share"], 0)

    def test_empty_wallet_has_no_utxo_research_score(self):
        data = self.snap()

        empty = data["wallets"]["flab_b"]["research"]

        self.assertIsNone(empty["score"])
        self.assertEqual(
            empty["label"],
            "NO UTXO DATA",
        )
        self.assertIsNone(empty["utxo_size_diversity"])
        self.assertIsNone(empty["fragmentation_index"])

    def test_wallet_balance_and_utxo_ranks_are_exposed(self):
        data = self.snap()

        source = data["wallets"]["flab_source"]["research"]
        stage = data["wallets"]["flab_stage"]["research"]

        self.assertEqual(source["balance_rank"], 1)
        self.assertEqual(source["utxo_count_rank"], 1)

        self.assertGreater(
            stage["balance_rank"],
            source["balance_rank"],
        )

    def test_overall_score_includes_utxo_size_diversity(self):
        score = self.snap()["score"]

        self.assertIn(
            "utxo_size_diversity",
            score["components"],
        )
        self.assertIn(
            "utxo_size_diversity",
            score["weights"],
        )

    def test_node_state_is_exposed(self):
        node = self.snap()["node"]

        self.assertTrue(node["available"])
        self.assertTrue(node["ready"])
        self.assertEqual(node["blocks"], 100)
        self.assertEqual(node["headers"], 100)

    def test_role_coverage_reports_configured_wallet_assignment(self):
        data = self.snap()

        self.assertEqual(
            data["research"]["role_coverage"],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
