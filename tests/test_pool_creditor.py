"""Tests for simulation-only verified-deposit accounting."""

import unittest

from flowlab import pool_ledger
from flowlab.pool_creditor import credit_live, simulate_credit
from tests.test_pool_verifier import VerifierTests


class CreditorTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

        # Creditor tests use six-confirmation blockchain evidence.
        self.rpc.raw["confirmations"] = 6
        self.rpc.header["confirmations"] = 6

    def tearDown(self):
        VerifierTests.tearDown(self)

        # The synthetic verified transaction has 6 confirmations.
        self.rpc.raw["confirmations"] = 6
        self.rpc.header["confirmations"] = 6

    def simulate(self, **kwargs):
        return simulate_credit(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
            **kwargs,
        )

    def test_six_confirmations_eligible(self):
        result = self.simulate()

        self.assertEqual(result["state"], "ELIGIBLE_SIMULATION")
        self.assertEqual(result["amount_sats"], 125_000_000)
        self.assertEqual(result["fee_sats"], 6_250_000)
        self.assertEqual(
            result["participant_liability_sats"],
            118_750_000,
        )

    def test_insufficient_confirmations(self):
        self.rpc.raw["confirmations"] = 5
        self.rpc.header["confirmations"] = 5

        result = self.simulate()

        self.assertEqual(
            result["state"],
            "INSUFFICIENT_CONFIRMATIONS",
        )

    def test_simulation_does_not_post_journal(self):
        before = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        self.simulate()

        after = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        self.assertEqual(before, after)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

    def test_already_credited_output(self):
        pool_ledger.record_deposit(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
            deposit_sats=125_000_000,
            fee_basis_points=500,
            deposit_outpoint=("a" * 64, 0),
        )

        result = self.simulate()

        self.assertEqual(result["state"], "ALREADY_CREDITED")

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            1,
        )


    def test_cumulative_fee_rounding(self):
        # A prior credited deposit changes the cumulative fee
        # calculation for the next deposit.
        pool_ledger.record_deposit(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
            deposit_sats=11,
            fee_basis_points=500,
            deposit_outpoint=("f" * 64, 0),
        )

        result = self.simulate()

        self.assertEqual(result["state"], "ELIGIBLE_SIMULATION")

        previous_fee = (11 * 500 + 5000) // 10000
        cumulative_fee = (
            (11 + 125000000) * 500 + 5000
        ) // 10000

        self.assertEqual(
            result["fee_sats"],
            cumulative_fee - previous_fee,
        )

    def test_rejects_cumulative_integer_overflow(self):
        max_sats = (1 << 63) - 1

        pool_ledger.record_deposit(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
            deposit_sats=max_sats - 100,
            fee_basis_points=500,
            deposit_outpoint=("f" * 64, 0),
        )

        with self.assertRaises(ValueError):
            self.simulate()


    def test_existing_credit_identity_mismatch(self):
        # The same outpoint cannot represent a valid credit
        # for two different registered participants.
        pool_ledger.create_session(
            self.conn,
            pool_id="test-pool",
            session_id="other-session",
            fee_basis_points=500,
        )

        pool_ledger.record_deposit(
            self.conn,
            pool_id="test-pool",
            session_id="other-session",
            deposit_sats=125_000_000,
            fee_basis_points=500,
            deposit_outpoint=("a" * 64, 0),
        )

        result = self.simulate()

        self.assertEqual(result["state"], "CREDIT_CONFLICT")

    def test_existing_credit_amount_mismatch(self):
        pool_ledger.record_deposit(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
            deposit_sats=100_000_000,
            fee_basis_points=500,
            deposit_outpoint=("a" * 64, 0),
        )

        result = self.simulate()

        self.assertEqual(result["state"], "CREDIT_CONFLICT")

    def test_live_crediting_disabled(self):
        with self.assertRaises(RuntimeError):
            credit_live()
