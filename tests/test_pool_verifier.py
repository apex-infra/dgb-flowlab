"""Independent deposit-verification tests using synthetic RPC evidence."""

import unittest

from flowlab import db, pool_ledger, pool_observer
from flowlab.pool_verifier import verify_observation


TXID = "a" * 64
BLOCK = "b" * 64
OTHER_BLOCK = "c" * 64


class FakeRPC:
    def __init__(self):
        self.raw = {
            "txid": TXID,
            "blockhash": BLOCK,
            "confirmations": 3,
            "vout": [
                {
                    "n": 0,
                    "value": "1.25000000",
                    "scriptPubKey": {"address": "deposit-address"},
                },
            ],
        }

        self.header = {
            "hash": BLOCK,
            "height": 100,
            "confirmations": 3,
        }

        self.active_hash = BLOCK
        self.anchored_active = True

    def get_raw_transaction(self, txid, verbose=True, blockhash=None):
        result = dict(self.raw)

        if blockhash is not None:
            result["in_active_chain"] = self.anchored_active

        return result

    def get_block_header(self, blockhash):
        return dict(self.header)

    def get_block_hash(self, height):
        return self.active_hash


class VerifierTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)

        pool_ledger.create_pool(
            self.conn,
            pool_id="test-pool",
            reserve_wallet="test-reserve",
            fee_wallet="test-fees",
        )

        # The verifier must operate on existing registered sessions.
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            ) VALUES ('test-session', 'test-pool', 500, 't')
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_payout_instructions(
                session_id, position, payout_address, allocation_bps
            ) VALUES ('test-session', 1, 'payout-address', 10000)
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_registration_seals(
                session_id, completed_at
            ) VALUES ('test-session', 't')
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label, address,
                state, created_at, updated_at
            ) VALUES (
                'test-session', 'test-reserve', 'test-label',
                NULL, 'REQUESTING', 't', 't'
            )
            """
        )

        self.conn.execute(
            """
            UPDATE pool_deposit_addresses
            SET address = 'deposit-address', state = 'READY'
            WHERE session_id = 'test-session'
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_deposit_observations(
                pool_id, session_id, txid, vout, amount_sats,
                address, confirmations, blockhash, state,
                first_seen_at, last_seen_at
            ) VALUES (
                'test-pool', 'test-session', ?, 0, 125000000,
                'deposit-address', 3, ?, 'CONFIRMED', 't', 't'
            )
            """,
            (TXID, BLOCK),
        )

        self.observation_id = self.conn.execute(
            "SELECT id FROM pool_deposit_observations"
        ).fetchone()["id"]

        self.rpc = FakeRPC()

    def tearDown(self):
        self.conn.close()

    def verify(self):
        return verify_observation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

    def test_valid_active_chain_output(self):
        result = self.verify()

        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(result["confirmations"], 3)
        self.assertEqual(result["blockhash"], BLOCK)

    def test_wrong_transaction_id(self):
        self.rpc.raw["txid"] = "d" * 64

        self.assertEqual(self.verify()["state"], "REJECTED")

    def test_missing_output(self):
        self.rpc.raw["vout"][0]["n"] = 1

        self.assertEqual(self.verify()["state"], "REJECTED")

    def test_wrong_destination(self):
        self.rpc.raw["vout"][0]["scriptPubKey"]["address"] = "other-address"

        self.assertEqual(self.verify()["state"], "REJECTED")

    def test_wrong_amount(self):
        self.rpc.raw["vout"][0]["value"] = "1.25000001"

        self.assertEqual(self.verify()["state"], "REJECTED")

    def test_orphaned_block(self):
        self.rpc.header["confirmations"] = -1

        self.assertEqual(self.verify()["state"], "PENDING")

    def test_active_chain_hash_mismatch(self):
        self.rpc.active_hash = OTHER_BLOCK

        self.assertEqual(self.verify()["state"], "PENDING")

    def test_explicit_membership_failure(self):
        self.rpc.anchored_active = False

        self.assertEqual(self.verify()["state"], "PENDING")

    def test_confirmation_disagreement(self):
        self.rpc.raw["confirmations"] = 2

        self.assertEqual(self.verify()["state"], "PENDING")

    def test_unconfirmed_matching_output(self):
        self.rpc.raw.pop("blockhash")
        self.rpc.raw.pop("confirmations")

        self.assertEqual(self.verify()["state"], "PENDING")

    def test_verification_does_not_credit_funds(self):
        self.assertEqual(self.verify()["state"], "VERIFIED")

        count = self.conn.execute(
            "SELECT COUNT(*) FROM pool_deposits"
        ).fetchone()[0]

        self.assertEqual(count, 0)


    def test_block_specific_output_disagreement(self):
        class ConflictingRPC(FakeRPC):
            def get_raw_transaction(
                self, txid, verbose=True, blockhash=None
            ):
                result = super().get_raw_transaction(
                    txid, verbose=verbose, blockhash=blockhash
                )

                if blockhash is not None:
                    result["vout"] = [
                        {
                            "n": 0,
                            "value": "2.00000000",
                            "scriptPubKey": {
                                "address": "deposit-address"
                            },
                        }
                    ]

                return result

        self.rpc = ConflictingRPC()

        self.assertNotEqual(
            self.verify()["state"],
            "VERIFIED",
        )


    def test_stale_unconfirmed_observation(self):
        # The blockchain reports confirmation, but the observer
        # has not yet refreshed the stored observation.
        self.conn.execute(
            """
            UPDATE pool_deposit_observations
            SET confirmations = 0, blockhash = NULL,
                state = 'UNCONFIRMED'
            WHERE id = ?
            """,
            (self.observation_id,),
        )

        result = self.verify()

        self.assertEqual(result["state"], "PENDING")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )


    def test_inconsistent_observation_state(self):
        # Even if the stored blockhash matches Core, an observation
        # marked UNCONFIRMED must not be treated as verified.
        self.conn.execute(
            """
            UPDATE pool_deposit_observations
            SET confirmations = 0, state = 'UNCONFIRMED'
            WHERE id = ?
            """,
            (self.observation_id,),
        )

        self.assertEqual(
            self.verify()["state"],
            "PENDING",
        )


if __name__ == "__main__":
    unittest.main()
