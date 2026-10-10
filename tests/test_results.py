import unittest

from flowlab.results import analyze_experiment


class ResultsTests(unittest.TestCase):
    def snapshot(self):
        return {
            "exp": {
                "id": "exp-1",
                "description": "results fixture",
                "state": "COMPLETE",
                "started_at": "2026-10-07T12:00:00+00:00",
                "completed_at": "2026-10-07T12:01:00+00:00",
            },
            "mode": "experimental",
            "staged_accounting": {
                "approved_principal_sats": 1000,
                "committed_principal_sats": 1000,
                "experiment_fees_sats": 10,
                "destination_receipts_sats": 990,
                "accounting_delta_sats": 0,
                "accounting_reconciled": True,
            },
            "flows": [
                {
                    "jobs": [
                        {
                            "state": "CONFIRMED",
                            "from": "reserve",
                            "to": "stage",
                            "amount": 1000,
                            "delay_s": 0,
                            "actual_executed_at":
                                "2026-10-07T12:00:01+00:00",
                            "confirmed_at":
                                "2026-10-07T12:00:03+00:00",
                            "generated": {
                                "source":
                                    "experimental allocation commit",
                                "phase": "allocation",
                            },
                        },
                        {
                            "state": "CONFIRMED",
                            "from": "stage",
                            "to": "a",
                            "amount": 100,
                            "delay_s": 2,
                            "actual_executed_at":
                                "2026-10-07T12:00:05+00:00",
                            "confirmed_at":
                                "2026-10-07T12:00:07+00:00",
                            "generated": {
                                "source":
                                    "seeded experimental generator",
                                "decision_index": 0,
                            },
                        },
                        {
                            "state": "CONFIRMED",
                            "from": "stage",
                            "to": "a",
                            "amount": 200,
                            "delay_s": 4,
                            "actual_executed_at":
                                "2026-10-07T12:00:10+00:00",
                            "confirmed_at":
                                "2026-10-07T12:00:12+00:00",
                            "generated": {
                                "source":
                                    "seeded experimental generator",
                                "decision_index": 1,
                            },
                        },
                        {
                            "state": "CONFIRMED",
                            "from": "a",
                            "to": "stage",
                            "amount": 150,
                            "delay_s": 6,
                            "actual_executed_at":
                                "2026-10-07T12:00:20+00:00",
                            "confirmed_at":
                                "2026-10-07T12:00:23+00:00",
                            "generated": {
                                "source":
                                    "seeded experimental generator",
                                "decision_index": 2,
                            },
                        },
                    ],
                },
            ],
        }

    def test_summary_and_activity(self):
        result = analyze_experiment(self.snapshot())

        self.assertEqual(result["summary"]["id"], "exp-1")
        self.assertEqual(result["summary"]["runtime_s"], 60.0)

        self.assertEqual(result["activity"]["total_jobs"], 4)
        self.assertEqual(result["activity"]["confirmed"], 4)
        self.assertEqual(result["activity"]["allocation_jobs"], 1)
        self.assertEqual(result["activity"]["workload_jobs"], 3)

    def test_settlement_cycle_phases_have_explicit_activity_counts(self):
        snap = self.snapshot()

        snap["flows"][0]["jobs"].extend([
            {
                "state": "CONFIRMED",
                "from": "stage",
                "to": None,
                "amount": None,
                "distributed": 4000,
                "fee": 5,
                "delay_s": 0,
                "actual_executed_at":
                    "2026-10-07T12:00:30+00:00",
                "confirmed_at":
                    "2026-10-07T12:00:32+00:00",
                "generated": {
                    "source": "settlement cycle settlement",
                    "phase": "settlement",
                },
            },
            {
                "state": "CONFIRMED",
                "from": "stage",
                "to": "reserve",
                "amount": 950,
                "distributed": None,
                "fee": 5,
                "delay_s": 0,
                "actual_executed_at":
                    "2026-10-07T12:00:35+00:00",
                "confirmed_at":
                    "2026-10-07T12:00:37+00:00",
                "generated": {
                    "source": "settlement cycle reserve return",
                    "phase": "reserve_return",
                },
            },
        ])

        result = analyze_experiment(snap)
        activity = result["activity"]

        self.assertEqual(activity["settlement_jobs"], 1)
        self.assertEqual(activity["reserve_return_jobs"], 1)
        self.assertEqual(activity["other_jobs"], 0)

    def test_topology_metrics_are_workload_only(self):
        result = analyze_experiment(self.snapshot())
        topology = result["topology"]

        self.assertEqual(topology["route_executions"], 3)
        self.assertEqual(topology["unique_edges"], 2)
        self.assertEqual(topology["repeated_edge_executions"], 1)
        self.assertAlmostEqual(
            topology["repeated_edge_ratio"],
            1 / 3,
        )

        self.assertEqual(topology["self_transfers"], 0)
        self.assertEqual(topology["wallets_exercised"], 2)

        self.assertAlmostEqual(
            topology["route_entropy_bits"],
            0.9182958340544896,
        )

        self.assertAlmostEqual(
            topology["route_entropy_normalized"],
            0.9182958340544896,
        )

    def test_amount_metrics_are_exact(self):
        result = analyze_experiment(self.snapshot())
        amounts = result["amounts"]

        self.assertEqual(amounts["count"], 3)
        self.assertEqual(amounts["total_sats"], 450)
        self.assertEqual(amounts["min"], 100)
        self.assertEqual(amounts["max"], 200)
        self.assertEqual(amounts["mean"], 150)
        self.assertEqual(amounts["median"], 150)

    def test_timing_metrics_use_real_execution_times(self):
        result = analyze_experiment(self.snapshot())
        timing = result["timing"]

        self.assertEqual(
            timing["planned_delay_s"]["mean"],
            4,
        )

        self.assertEqual(
            timing["execution_gap_s"]["count"],
            2,
        )

        self.assertEqual(
            timing["execution_gap_s"]["min"],
            5,
        )

        self.assertEqual(
            timing["execution_gap_s"]["max"],
            10,
        )

        self.assertEqual(
            timing["confirmation_s"]["count"],
            4,
        )

        self.assertEqual(
            timing["confirmation_s"]["max"],
            3,
        )

    def test_empty_workload_is_safe(self):
        snap = self.snapshot()
        snap["flows"][0]["jobs"] = []

        result = analyze_experiment(snap)

        self.assertEqual(result["activity"]["total_jobs"], 0)
        self.assertEqual(result["topology"]["route_executions"], 0)
        self.assertEqual(result["topology"]["route_entropy_bits"], 0.0)
        self.assertEqual(result["amounts"]["count"], 0)
        self.assertEqual(result["timing"]["execution_gap_s"]["count"], 0)

    def test_observability_metrics_are_bounded(self):
        result = analyze_experiment(self.snapshot())
        obs = result["observability"]

        for name in (
            "route_diversity",
            "wallet_activity_diversity",
            "edge_distribution_diversity",
            "amount_diversity",
            "timing_diversity",
            "repeated_edge_ratio",
            "self_transfer_ratio",
        ):
            self.assertGreaterEqual(obs[name], 0.0)
            self.assertLessEqual(obs[name], 1.0)

    def test_mixing_model_is_versioned_and_transparent(self):
        result = analyze_experiment(self.snapshot())
        mixing = result["mixing"]

        self.assertEqual(
            mixing["model"],
            "mixing_model_v1",
        )

        self.assertGreaterEqual(mixing["score"], 0)
        self.assertLessEqual(mixing["score"], 100)

        self.assertIn(
            mixing["label"],
            {
                "LOW",
                "LIMITED",
                "MODERATE",
                "HIGH",
                "VERY HIGH",
            },
        )

        self.assertEqual(
            sum(mixing["weights"].values()),
            1.0,
        )

        self.assertEqual(
            set(mixing["components"]),
            set(mixing["weights"]),
        )

        self.assertIn(
            "does not imply",
            mixing["interpretation"],
        )

    def test_uniform_routes_score_more_diverse_than_one_route(self):
        diverse = self.snapshot()
        concentrated = self.snapshot()

        concentrated["flows"][0]["jobs"][2]["from"] = "stage"
        concentrated["flows"][0]["jobs"][2]["to"] = "a"
        concentrated["flows"][0]["jobs"][3]["from"] = "stage"
        concentrated["flows"][0]["jobs"][3]["to"] = "a"

        diverse_result = analyze_experiment(diverse)
        concentrated_result = analyze_experiment(concentrated)

        self.assertGreater(
            diverse_result["observability"]["route_diversity"],
            concentrated_result["observability"]["route_diversity"],
        )

        self.assertGreater(
            diverse_result["observability"][
                "edge_distribution_diversity"
            ],
            concentrated_result["observability"][
                "edge_distribution_diversity"
            ],
        )

    def test_wallet_roles_are_copied(self):
        roles = {
            "workers": ["a", "b"],
            "hubs": ["hub"],
        }

        result = analyze_experiment(
            self.snapshot(),
            wallet_roles=roles,
        )

        self.assertEqual(
            result["wallet_roles"],
            roles,
        )


if __name__ == "__main__":
    unittest.main()
