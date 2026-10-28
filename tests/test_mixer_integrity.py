import copy
import unittest

from flowlab.mixer_integrity import (
    IntegrityError,
    attest_public_workload,
)
from flowlab.mixer_workloads import build_seeded_workload


class ResearchIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.dataset = build_seeded_workload(
            seed=2049,
            transaction_count=30,
            max_inputs=6,
            scenario_weights={
                "single_owner": 40,
                "collaborative": 30,
                "mixed_ownership": 30,
            },
            matched_input_profile=True,
        )

    def test_attestation_identity(self):
        result = attest_public_workload(self.dataset)
        self.assertEqual(
            result["kind"], "mixer_public_workload_attestation"
        )
        self.assertEqual(result["execution"], "simulation_only")

    def test_fingerprint_format(self):
        fingerprint = attest_public_workload(
            self.dataset
        )["public_sha256"]
        self.assertEqual(len(fingerprint), 64)
        self.assertTrue(
            all(character in "0123456789abcdef"
                for character in fingerprint)
        )

    def test_deterministic(self):
        self.assertEqual(
            attest_public_workload(self.dataset),
            attest_public_workload(self.dataset),
        )

    def test_different_seed_changes_fingerprint(self):
        other = build_seeded_workload(42, 30, 6)
        self.assertNotEqual(
            attest_public_workload(self.dataset)["public_sha256"],
            attest_public_workload(other)["public_sha256"],
        )

    def test_ground_truth_not_exported(self):
        result = attest_public_workload(self.dataset)
        self.assertNotIn("ground_truth", str(result))
        self.assertNotIn("participant_", str(result))
        self.assertNotIn("utxo_owners", str(result))

    def test_ground_truth_change_does_not_change_fingerprint(self):
        altered = copy.deepcopy(self.dataset)
        altered["ground_truth"]["utxo_owners"].clear()
        self.assertEqual(
            attest_public_workload(self.dataset),
            attest_public_workload(altered),
        )

    def test_public_tampering_changes_fingerprint(self):
        altered = copy.deepcopy(self.dataset)
        altered["public"]["transactions"][0]["id"] = (
            "simtx:modified"
        )
        with self.assertRaises(IntegrityError):
            attest_public_workload(altered)

    def test_public_ownership_label_rejected(self):
        altered = copy.deepcopy(self.dataset)
        altered["public"]["participant_id"] = "participant_0001"
        with self.assertRaises(IntegrityError):
            attest_public_workload(altered)

    def test_nested_ownership_label_rejected(self):
        altered = copy.deepcopy(self.dataset)
        altered["public"]["transactions"][0]["owner"] = (
            "participant_0001"
        )
        with self.assertRaises(IntegrityError):
            attest_public_workload(altered)

    def test_invalid_dataset_rejected(self):
        for item in (None, [], {}, "dataset"):
            with self.subTest(item=item):
                with self.assertRaises(IntegrityError):
                    attest_public_workload(item)

    def test_source_not_modified(self):
        before = copy.deepcopy(self.dataset)
        attest_public_workload(self.dataset)
        self.assertEqual(self.dataset, before)

    def test_summary_counts(self):
        result = attest_public_workload(self.dataset)
        self.assertEqual(result["transaction_count"], 30)
        self.assertEqual(
            result["genesis_utxo_count"],
            len(self.dataset["public"]["genesis_utxos"]),
        )

    def test_legacy_seeded_workload_supported(self):
        dataset = build_seeded_workload(2049, 30, 6)
        result = attest_public_workload(dataset)
        self.assertEqual(result["transaction_count"], 30)



class ReproducibilityTests(unittest.TestCase):

    CONFIG = {
        "seed": 2049,
        "transaction_count": 30,
        "max_inputs": 6,
        "scenario_weights": {
            "single_owner": 40,
            "collaborative": 30,
            "mixed_ownership": 30,
        },
        "matched_input_profile": True,
    }

    def setUp(self):
        self.config = copy.deepcopy(self.CONFIG)
        self.dataset = build_seeded_workload(**self.config)

    def attest(self, dataset=None, config=None):
        from flowlab.mixer_integrity import (
            attest_reproducible_workload,
        )
        return attest_reproducible_workload(
            self.dataset if dataset is None else dataset,
            self.config if config is None else config,
        )

    def test_reproducible_run(self):
        result = self.attest()
        self.assertTrue(result["reproduced"])
        self.assertEqual(result["execution"], "simulation_only")
        self.assertEqual(len(result["configuration_sha256"]), 64)

    def test_public_hash_matches_original(self):
        result = self.attest()
        self.assertEqual(
            result["public_sha256"],
            attest_public_workload(self.dataset)["public_sha256"],
        )

    def test_configuration_fingerprint_deterministic(self):
        self.assertEqual(self.attest(), self.attest())

    def test_different_configuration_changes_hash(self):
        other = copy.deepcopy(self.config)
        other["seed"] = 42
        other_dataset = build_seeded_workload(**other)
        result = self.attest(other_dataset, other)

        self.assertNotEqual(
            result["configuration_sha256"],
            self.attest()["configuration_sha256"],
        )

    def test_incorrect_seed_rejected(self):
        wrong = copy.deepcopy(self.config)
        wrong["seed"] = 42

        with self.assertRaisesRegex(
            Exception, "reproduction mismatch"
        ):
            self.attest(config=wrong)

    def test_public_content_tampering_rejected(self):
        altered = copy.deepcopy(self.dataset)
        altered["public"]["genesis_utxos"][0]["value_sats"] += 1

        with self.assertRaises(Exception):
            self.attest(dataset=altered)

    def test_private_labels_not_exported(self):
        result = self.attest()
        self.assertNotIn("ground_truth", str(result))
        self.assertNotIn("participant_", str(result))
        self.assertNotIn("utxo_owners", str(result))

    def test_legacy_configuration_supported(self):
        config = {
            "seed": 2049,
            "transaction_count": 30,
            "max_inputs": 6,
            "scenario_weights": None,
            "matched_input_profile": False,
        }
        dataset = build_seeded_workload(2049, 30, 6)

        result = self.attest(dataset, config)
        self.assertTrue(result["reproduced"])

if __name__ == "__main__":
    unittest.main()
