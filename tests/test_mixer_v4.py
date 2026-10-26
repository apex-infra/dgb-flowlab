"""Mixer Lab V4 verified-address telemetry tests.

All tests use isolated temporary databases.
No DigiByte Core RPC calls are made.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flowlab import db, pool_ledger, webdash


class MixerV4Tests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(
            Path(self.tmp.name) / "mixerlab-dev.db"
        )

        conn = db.connect(self.path)

        try:
            db.init(conn)

            pool_ledger.create_pool(
                conn,
                pool_id="research_pool",
                reserve_wallet="research_reserve",
                fee_wallet="research_fees",
            )

            pool_ledger.create_session(
                conn,
                pool_id="research_pool",
                session_id="research_session",
                fee_basis_points=100,
            )

            with db.tx(conn):
                conn.execute(
                    """
                    INSERT INTO pool_payout_instructions(
                        session_id,
                        position,
                        payout_address,
                        allocation_bps
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "research_session",
                        1,
                        "mock-payout-address",
                        10000,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO pool_registration_seals(
                        session_id, completed_at
                    )
                    VALUES (?, ?)
                    """,
                    (
                        "research_session",
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                    ),
                )
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def add_address(self, state, address=None):
        now = datetime.now(timezone.utc).isoformat()

        conn = db.connect(self.path)

        try:
            with db.tx(conn):
                conn.execute(
                    """
                    INSERT INTO pool_deposit_addresses(
                        session_id,
                        wallet,
                        request_label,
                        address,
                        state,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "research_session",
                        "research_reserve",
                        "flowlab-v4-test",
                        None,
                        "REQUESTING",
                        now,
                        now,
                    ),
                )

                if state != "REQUESTING":
                    conn.execute(
                        """
                        UPDATE pool_deposit_addresses
                        SET state = ?,
                            address = ?,
                            updated_at = ?
                        WHERE session_id = ?
                          AND state = 'REQUESTING'
                        """,
                        (
                            state,
                            address,
                            now,
                            "research_session",
                        ),
                    )
        finally:
            conn.close()

    def session(self):
        result = webdash.mixer_lab_sessions_snapshot(
            self.path
        )

        self.assertEqual(result["status"], "AVAILABLE")
        self.assertTrue(result["read_only"])
        self.assertEqual(len(result["sessions"]), 1)

        return result["sessions"][0]

    def test_unrequested_address_is_not_exposed(self):
        record = self.session()

        self.assertIsNone(record["address_state"])
        self.assertIsNone(record["deposit_address"])
        self.assertIsNone(record["deposit_wallet"])
        self.assertFalse(
            record["address_recovery_required"]
        )

    def test_requesting_address_is_not_exposed(self):
        self.add_address("REQUESTING")

        record = self.session()

        self.assertEqual(
            record["address_state"], "REQUESTING"
        )
        self.assertIsNone(record["deposit_address"])
        self.assertIsNone(record["deposit_wallet"])
        self.assertFalse(
            record["address_recovery_required"]
        )

    def test_ready_address_is_exposed(self):
        self.add_address(
            "READY",
            "mock-verified-deposit-address",
        )

        record = self.session()

        self.assertEqual(
            record["address_state"], "READY"
        )
        self.assertEqual(
            record["deposit_address"],
            "mock-verified-deposit-address",
        )
        self.assertEqual(
            record["deposit_wallet"],
            "research_reserve",
        )
        self.assertFalse(
            record["address_recovery_required"]
        )

    def test_recovery_required_never_exposes_address(self):
        self.add_address("RECOVERY_REQUIRED")

        record = self.session()

        self.assertEqual(
            record["address_state"],
            "RECOVERY_REQUIRED",
        )
        self.assertIsNone(record["deposit_address"])
        self.assertIsNone(record["deposit_wallet"])
        self.assertTrue(
            record["address_recovery_required"]
        )

    def test_snapshot_does_not_modify_database(self):
        before = Path(self.path).read_bytes()

        self.session()

        after = Path(self.path).read_bytes()

        self.assertEqual(before, after)

    def test_frontend_has_copy_and_recovery_controls(self):
        script = (
            Path("flowlab/web/app.js")
            .read_text()
        )

        self.assertIn(
            'session.address_state === "READY"',
            script,
        )
        self.assertIn(
            "navigator.clipboard.writeText",
            script,
        )
        self.assertIn(
            "session.address_recovery_required",
            script,
        )


if __name__ == "__main__":
    unittest.main()
