import copy
import unittest

from flowlab.mixer_research import (
    ResearchContractError,
    compile_research,
    research_identity,
    validate_research,
)


BASE = {
    "version": 1,
    "mode": "baseline",
    "seed": 2049,
    "hypothesis": "Measure baseline UTXO graph traceability.",
    "synthetic": {
        "participant_count": 5,
        "utxos_per_participant": 3,
        "initial_value_sats": 100_000_000,
        "transaction_count": 30,
        "network_fee_sats": 200,
    },
    "analysis": {
        "observer": "public_chain",
    },
}


class MixerResearchContractTests(unittest.TestCase):

    def test_valid_baseline(self):
        cfg = validate_research(BASE)

        self.assertEqual(cfg["mode"], "baseline")
        self.assertEqual(cfg["seed"], 2049)
        self.assertEqual(cfg["version"], 1)

    def test_input_not_mutated(self):
        original = copy.deepcopy(BASE)
        validate_research(BASE)
        self.assertEqual(BASE, original)

    def test_reproducible_identity(self):
        first = research_identity(BASE)
        second = research_identity(copy.deepcopy(BASE))

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("mixer_research:v1:"))

    def test_identity_changes_with_seed(self):
        changed = copy.deepcopy(BASE)
        changed["seed"] += 1

        self.assertNotEqual(
            research_identity(BASE),
            research_identity(changed),
        )

    def test_identity_changes_with_hypothesis(self):
        changed = copy.deepcopy(BASE)
        changed["hypothesis"] = "A different research hypothesis."

        self.assertNotEqual(
            research_identity(BASE),
            research_identity(changed),
        )

    def test_all_research_modes_supported(self):
        for mode in ("baseline", "topology", "utxo", "analyst"):
            with self.subTest(mode=mode):
                cfg = copy.deepcopy(BASE)
                cfg["mode"] = mode
                self.assertEqual(validate_research(cfg)["mode"], mode)

    def test_invalid_mode_rejected(self):
        cfg = copy.deepcopy(BASE)
        cfg["mode"] = "live_mixer"

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_extra_fields_rejected(self):
        cfg = copy.deepcopy(BASE)
        cfg["rpc_password"] = "not-a-real-secret"

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_live_execution_fields_rejected(self):
        for field in ("source_wallet", "destination_address", "broadcast"):
            with self.subTest(field=field):
                cfg = copy.deepcopy(BASE)
                cfg[field] = "forbidden"

                with self.assertRaises(ResearchContractError):
                    validate_research(cfg)

    def test_boolean_integer_substitution_rejected(self):
        for section, field in (
            (None, "seed"),
            ("synthetic", "participant_count"),
            ("synthetic", "transaction_count"),
            ("synthetic", "network_fee_sats"),
        ):
            with self.subTest(field=field):
                cfg = copy.deepcopy(BASE)

                target = cfg if section is None else cfg[section]
                target[field] = True

                with self.assertRaises(ResearchContractError):
                    validate_research(cfg)

    def test_nonpositive_counts_rejected(self):
        for field in (
            "participant_count",
            "utxos_per_participant",
            "initial_value_sats",
            "transaction_count",
        ):
            with self.subTest(field=field):
                cfg = copy.deepcopy(BASE)
                cfg["synthetic"][field] = 0

                with self.assertRaises(ResearchContractError):
                    validate_research(cfg)

    def test_resource_limits_enforced(self):
        limits = {
            "participant_count": 1001,
            "utxos_per_participant": 1001,
            "transaction_count": 100001,
        }

        for field, value in limits.items():
            with self.subTest(field=field):
                cfg = copy.deepcopy(BASE)
                cfg["synthetic"][field] = value

                with self.assertRaises(ResearchContractError):
                    validate_research(cfg)

    def test_negative_fee_rejected(self):
        cfg = copy.deepcopy(BASE)
        cfg["synthetic"]["network_fee_sats"] = -1

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_total_value_overflow_rejected(self):
        cfg = copy.deepcopy(BASE)
        cfg["synthetic"]["participant_count"] = 1000
        cfg["synthetic"]["utxos_per_participant"] = 1000
        cfg["synthetic"]["initial_value_sats"] = 21_000_000 * 100_000_000

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_total_network_fee_budget_cannot_exceed_funding(self):
        cfg = copy.deepcopy(BASE)
        cfg["synthetic"]["participant_count"] = 1
        cfg["synthetic"]["utxos_per_participant"] = 1
        cfg["synthetic"]["initial_value_sats"] = 1000
        cfg["synthetic"]["transaction_count"] = 6
        cfg["synthetic"]["network_fee_sats"] = 200

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_total_network_fee_budget_may_equal_funding(self):
        cfg = copy.deepcopy(BASE)
        cfg["synthetic"]["participant_count"] = 1
        cfg["synthetic"]["utxos_per_participant"] = 1
        cfg["synthetic"]["initial_value_sats"] = 1000
        cfg["synthetic"]["transaction_count"] = 5
        cfg["synthetic"]["network_fee_sats"] = 200

        self.assertEqual(validate_research(cfg), cfg)

    def test_unknown_observer_rejected(self):
        cfg = copy.deepcopy(BASE)
        cfg["analysis"]["observer"] = "omniscient"

        with self.assertRaises(ResearchContractError):
            validate_research(cfg)

    def test_compiler_is_simulation_only(self):
        compiled = compile_research(BASE)

        self.assertEqual(compiled["kind"], "mixer_research")
        self.assertEqual(compiled["execution"], "simulation_only")
        self.assertEqual(compiled["config"], validate_research(BASE))
        self.assertEqual(
            compiled["identity"],
            research_identity(BASE),
        )
        self.assertNotIn("flows", compiled)
        self.assertNotIn("jobs", compiled)

    def test_compilation_is_deterministic(self):
        self.assertEqual(
            compile_research(BASE),
            compile_research(copy.deepcopy(BASE)),
        )

    def test_compiled_result_is_independent(self):
        compiled = compile_research(BASE)
        compiled["config"]["seed"] = 999

        self.assertEqual(BASE["seed"], 2049)


if __name__ == "__main__":
    unittest.main()
