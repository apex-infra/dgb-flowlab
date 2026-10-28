import copy
import json
import unittest

from flowlab.mixer_research_api import (
    ResearchAPIError,
    run_research_request,
)


CONFIG = {
    "seeds": [42, 2049, 9999],
    "transaction_count": 100,
    "max_inputs": 6,
    "scenario_weights": {
        "single_owner": 40,
        "collaborative": 30,
        "mixed_ownership": 30,
    },
    "matched_input_profile": True,
}


class ResearchAPITests(unittest.TestCase):

    def setUp(self):
        self.config = copy.deepcopy(CONFIG)

    def test_request_runs(self):
        result = run_research_request(self.config)
        self.assertEqual(
            result["kind"],
            "mixer_dashboard_research_result",
        )
        self.assertEqual(
            result["execution"],
            "simulation_only",
        )

    def test_transaction_count(self):
        result = run_research_request(self.config)
        self.assertEqual(
            result["aggregate"]["transaction_count"],
            300,
        )

    def test_prediction_count(self):
        result = run_research_request(self.config)
        self.assertEqual(
            result["aggregate"]["prediction_count"],
            2581,
        )

    def test_scenario_counts(self):
        result = run_research_request(self.config)
        self.assertEqual(
            {
                name: item["transaction_count"]
                for name, item in result["by_scenario"].items()
            },
            {
                "single_owner": 120,
                "collaborative": 90,
                "mixed_ownership": 90,
            },
        )

    def test_statistics_present(self):
        result = run_research_request(self.config)
        self.assertEqual(
            result["statistics"]["seed_count"],
            3,
        )
        self.assertEqual(
            result["statistics"]["total_predictions"],
            2581,
        )

    def test_no_private_records_in_response(self):
        result = run_research_request(self.config)
        encoded = json.dumps(result)

        for forbidden in (
            "ground_truth",
            "utxo_owners",
            "transaction_participants",
            "participant_",
            "scenario_schedule",
            "configuration_sha256",
            "public_export",
            "restricted",
        ):
            self.assertNotIn(forbidden, encoded)

        self.assertNotIn('"seeds":', encoded)
        self.assertNotIn('"seed":', encoded)

    def test_response_deterministic(self):
        self.assertEqual(
            run_research_request(self.config),
            run_research_request(self.config),
        )

    def test_request_not_mutated(self):
        before = copy.deepcopy(self.config)
        run_research_request(self.config)
        self.assertEqual(self.config, before)

    def test_invalid_request_rejected(self):
        for value in (None, [], {}, "request"):
            with self.subTest(value=value):
                with self.assertRaises(ResearchAPIError):
                    run_research_request(value)

    def test_unknown_field_rejected(self):
        self.config["wallet"] = "flab_source"
        with self.assertRaises(ResearchAPIError):
            run_research_request(self.config)

    def test_invalid_seeds_rejected(self):
        for seeds in (
            [],
            [True],
            [-1],
            [42, 42],
            list(range(9)),
        ):
            with self.subTest(seeds=seeds):
                self.config["seeds"] = seeds
                with self.assertRaises(ResearchAPIError):
                    run_research_request(self.config)

    def test_invalid_weights_rejected(self):
        self.config["scenario_weights"] = {
            "single_owner": 80,
            "collaborative": 30,
            "mixed_ownership": 0,
        }
        with self.assertRaises(ResearchAPIError):
            run_research_request(self.config)

    def test_invalid_matched_flag_rejected(self):
        self.config["matched_input_profile"] = 1
        with self.assertRaises(ResearchAPIError):
            run_research_request(self.config)

    def test_prediction_budget_enforced(self):
        self.config["seeds"] = list(range(8))
        self.config["transaction_count"] = 300
        self.config["max_inputs"] = 12

        with self.assertRaises(ResearchAPIError):
            run_research_request(self.config)

    def test_empty_scenario_supported(self):
        self.config["scenario_weights"] = {
            "single_owner": 100,
            "collaborative": 0,
            "mixed_ownership": 0,
        }

        result = run_research_request(self.config)

        self.assertEqual(
            result["by_scenario"]["collaborative"]
            ["transaction_count"],
            0,
        )
        self.assertEqual(
            result["by_scenario"]["mixed_ownership"]
            ["transaction_count"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
