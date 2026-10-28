import copy
import json
import unittest

from flowlab.mixer_exports import (
    ExportError,
    build_public_export,
    verify_public_export,
)
from flowlab.mixer_workloads import build_seeded_workload


class PublicExportTests(unittest.TestCase):

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

    def test_export_identity(self):
        result = build_public_export(self.dataset)
        self.assertEqual(
            result["kind"], "mixer_public_research_export"
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["execution"], "simulation_only")

    def test_public_graph_preserved(self):
        result = build_public_export(self.dataset)
        self.assertEqual(result["public"], self.dataset["public"])

    def test_no_ground_truth(self):
        result = build_public_export(self.dataset)
        text = json.dumps(result)

        self.assertNotIn("ground_truth", text)
        self.assertNotIn("utxo_owners", text)
        self.assertNotIn("transaction_participants", text)
        self.assertNotIn("participant_", text)

    def test_no_seed_or_private_schedule(self):
        result = build_public_export(self.dataset)

        self.assertNotIn("seed", result)
        self.assertNotIn("configuration", result)
        self.assertNotIn("scenario_schedule", str(result))
        self.assertNotIn("scenario_weights", str(result))
        self.assertNotIn("matched_input_profile", str(result))
        self.assertNotIn("ground_truth", result)

    def test_attestation_present(self):
        result = build_public_export(self.dataset)

        self.assertEqual(
            result["attestation"]["hash_algorithm"],
            "sha256",
        )
        self.assertEqual(
            len(result["attestation"]["public_sha256"]),
            64,
        )

    def test_deterministic_serialization(self):
        first = build_public_export(self.dataset)
        second = build_public_export(self.dataset)

        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_round_trip(self):
        result = build_public_export(self.dataset)
        restored = json.loads(json.dumps(result))

        verified = verify_public_export(restored)

        self.assertTrue(verified["valid"])
        self.assertEqual(
            verified["public_sha256"],
            result["attestation"]["public_sha256"],
        )

    def test_transaction_tampering_rejected(self):
        result = build_public_export(self.dataset)
        result["public"]["transactions"][0]["fee_sats"] += 1

        with self.assertRaises(ExportError):
            verify_public_export(result)

    def test_genesis_tampering_rejected(self):
        result = build_public_export(self.dataset)
        result["public"]["genesis_utxos"][0]["value_sats"] += 1

        with self.assertRaises(ExportError):
            verify_public_export(result)

    def test_fingerprint_tampering_rejected(self):
        result = build_public_export(self.dataset)
        result["attestation"]["public_sha256"] = "0" * 64

        with self.assertRaises(ExportError):
            verify_public_export(result)

    def test_unexpected_fields_rejected(self):
        result = build_public_export(self.dataset)
        result["seed"] = 2049

        with self.assertRaises(ExportError):
            verify_public_export(result)

    def test_source_not_mutated(self):
        original = copy.deepcopy(self.dataset)
        build_public_export(self.dataset)
        self.assertEqual(self.dataset, original)

    def test_export_is_independent_copy(self):
        result = build_public_export(self.dataset)
        result["public"]["transactions"][0]["fee_sats"] = 999

        self.assertNotEqual(
            self.dataset["public"]["transactions"][0]["fee_sats"],
            999,
        )

    def test_invalid_input_rejected(self):
        for value in (None, [], {}, "dataset"):
            with self.subTest(value=value):
                with self.assertRaises(ExportError):
                    build_public_export(value)

    def test_verification_does_not_mutate(self):
        result = build_public_export(self.dataset)
        before = copy.deepcopy(result)

        verify_public_export(result)

        self.assertEqual(result, before)



class RestrictedExportTests(unittest.TestCase):

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

    def build(self):
        from flowlab.mixer_exports import build_restricted_export
        return build_restricted_export(self.dataset, self.config)

    def verify(self, record):
        from flowlab.mixer_exports import verify_restricted_export
        return verify_restricted_export(record)

    def test_restricted_identity(self):
        record = self.build()
        self.assertEqual(
            record["kind"], "mixer_restricted_research_export"
        )
        self.assertEqual(record["execution"], "simulation_only")
        self.assertEqual(record["access"], "restricted")

    def test_ground_truth_retained(self):
        record = self.build()
        self.assertEqual(
            record["ground_truth"],
            self.dataset["ground_truth"],
        )
        self.assertEqual(record["configuration"], self.config)

    def test_reproducible_verification(self):
        result = self.verify(self.build())
        self.assertTrue(result["valid"])
        self.assertTrue(result["reproduced"])

    def test_public_export_separate(self):
        record = self.build()
        public = record["public_export"]

        self.assertTrue(verify_public_export(public)["valid"])
        self.assertNotIn("ground_truth", json.dumps(public))
        self.assertNotIn("configuration", json.dumps(public))
        self.assertNotIn("seed", public)

    def test_ground_truth_tampering_rejected(self):
        record = self.build()
        owners = record["ground_truth"]["utxo_owners"]
        first = next(iter(owners))
        owners[first] = "participant_tampered"

        with self.assertRaises(ExportError):
            self.verify(record)

    def test_configuration_tampering_rejected(self):
        record = self.build()
        record["configuration"]["seed"] = 42

        with self.assertRaises(ExportError):
            self.verify(record)

    def test_restricted_record_independent_copy(self):
        record = self.build()
        before = copy.deepcopy(self.dataset)

        record["ground_truth"]["utxo_owners"].clear()

        self.assertEqual(self.dataset, before)

    def test_invalid_record_rejected(self):
        for record in (None, {}, [], "record"):
            with self.subTest(value=type(record).__name__):
                with self.assertRaises(ExportError):
                    self.verify(record)

if __name__ == "__main__":
    unittest.main()
