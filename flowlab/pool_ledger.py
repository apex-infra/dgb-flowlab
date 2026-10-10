"""
Persistent Mixer Lab accounting foundation.

All monetary values are integer DigiByte satoshis.
No RPC operations or blockchain transactions occur here.
"""

import sqlite3
from datetime import datetime, timezone

from . import db


MAX_SQLITE_INTEGER = (1 << 63) - 1


class DuplicateDepositError(ValueError):
    """A deposit outpoint has already been credited."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_pool(conn, *, pool_id, reserve_wallet, fee_wallet):
    if not pool_id or not reserve_wallet or not fee_wallet:
        raise ValueError("Pool ID and wallet names are required")

    if reserve_wallet == fee_wallet:
        raise ValueError("Reserve and fee wallets must be different")

    now = _now()

    with db.tx(conn):
        conn.execute(
            """
            INSERT INTO pools(
                id, state, reserve_wallet, fee_wallet,
                created_at, updated_at
            )
            VALUES (?, 'PAUSED', ?, ?, ?, ?)
            """,
            (pool_id, reserve_wallet, fee_wallet, now, now),
        )


def create_session(conn, *, pool_id, session_id, fee_basis_points):
    if (
        type(fee_basis_points) is not int
        or not 0 <= fee_basis_points <= 10_000
    ):
        raise ValueError("Invalid fee basis points")

    if not session_id:
        raise ValueError("Session ID is required")

    with db.tx(conn):
        conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (session_id, pool_id, fee_basis_points, _now()),
        )


def record_deposit(
    conn, *, pool_id, session_id, deposit_sats,
    fee_basis_points, deposit_outpoint
):
    if (
        type(deposit_sats) is not int
        or not 0 < deposit_sats <= MAX_SQLITE_INTEGER
    ):
        raise ValueError("Deposit must fit positive signed 64-bit satoshis")

    if type(fee_basis_points) is not int:
        raise ValueError("Fee must be integer basis points")

    if (
        not isinstance(deposit_outpoint, tuple)
        or len(deposit_outpoint) != 2
    ):
        raise ValueError("Expected (txid, vout)")

    txid, vout = deposit_outpoint

    if (
        not isinstance(txid, str)
        or len(txid) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in txid)
        or type(vout) is not int
        or vout < 0
    ):
        raise ValueError("Invalid deposit outpoint")

    with db.tx(conn):
        session = conn.execute(
            """
            SELECT fee_basis_points
            FROM pool_sessions
            WHERE id = ? AND pool_id = ?
            """,
            (session_id, pool_id),
        ).fetchone()

        if session is None:
            raise ValueError("Unknown pool session")

        if session["fee_basis_points"] != fee_basis_points:
            raise ValueError("Fee differs from registered session terms")

        # Calculate the fee increment from the cumulative session
        # total. This avoids charging separate rounding increments
        # for multiple deposits into the same session.
        previous = conn.execute(
            """
            SELECT COALESCE(SUM(amount_sats), 0) AS total_sats
            FROM pool_deposits
            WHERE pool_id = ? AND session_id = ?
            """,
            (pool_id, session_id),
        ).fetchone()

        previous_sats = previous["total_sats"]

        if deposit_sats > MAX_SQLITE_INTEGER - previous_sats:
            raise ValueError("Cumulative session deposits exceed integer limit")

        cumulative_sats = previous_sats + deposit_sats

        previous_fee_sats = (
            previous_sats * fee_basis_points + 5_000
        ) // 10_000

        cumulative_fee_sats = (
            cumulative_sats * fee_basis_points + 5_000
        ) // 10_000

        fee_sats = cumulative_fee_sats - previous_fee_sats
        entitlement_sats = deposit_sats - fee_sats
        now = _now()

        try:
            cursor = conn.execute(
                """
                INSERT INTO pool_deposits(
                    pool_id, session_id, txid, vout,
                    amount_sats, fee_sats, credited_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pool_id, session_id, txid.lower(), vout,
                    deposit_sats, fee_sats, now
                ),
            )
        except sqlite3.IntegrityError as exc:
            existing = conn.execute(
                """
                SELECT 1 FROM pool_deposits
                WHERE txid = ? AND vout = ?
                """,
                (txid.lower(), vout),
            ).fetchone()

            if existing:
                raise DuplicateDepositError(
                    "Deposit outpoint already credited"
                ) from exc

            raise

        deposit_id = cursor.lastrowid

        journal = conn.execute(
            """
            INSERT INTO pool_journal_transactions(
                pool_id, deposit_id, created_at
            )
            VALUES (?, ?, ?)
            """,
            (pool_id, deposit_id, now),
        )

        transaction_id = journal.lastrowid

        entries = [
            ("pool_assets", deposit_sats, 0),
            (
                f"participant_liability:{session_id}",
                0,
                entitlement_sats,
            ),
        ]

        if fee_sats:
            entries.append(("service_fee_revenue", 0, fee_sats))

        for account, debit, credit in entries:
            conn.execute(
                """
                INSERT INTO pool_journal_entries(
                    transaction_id, account,
                    debit_sats, credit_sats
                )
                VALUES (?, ?, ?, ?)
                """,
                (transaction_id, account, debit, credit),
            )

        conn.execute(
            """
            UPDATE pool_journal_transactions
            SET state = 'POSTED'
            WHERE id = ?
            """,
            (transaction_id,),
        )

    return deposit_id


def session_balances(conn, *, pool_id, session_id):
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(amount_sats), 0) AS deposit_sats,
            COALESCE(SUM(fee_sats), 0) AS fee_sats
        FROM pool_deposits
        WHERE pool_id = ? AND session_id = ?
        """,
        (pool_id, session_id),
    ).fetchone()

    deposit = row["deposit_sats"]
    fees = row["fee_sats"]

    return {
        "deposit_sats": deposit,
        "fee_sats": fees,
        "participant_liability_sats": deposit - fees,
    }


def journal_totals(conn, *, pool_id):
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(e.debit_sats), 0) AS debits,
            COALESCE(SUM(e.credit_sats), 0) AS credits
        FROM pool_journal_entries e
        JOIN pool_journal_transactions t
            ON t.id = e.transaction_id
        WHERE t.pool_id = ? AND t.state = 'POSTED'
        """,
        (pool_id,),
    ).fetchone()

    return {
        "total_debits_sats": row["debits"],
        "total_credits_sats": row["credits"],
    }
