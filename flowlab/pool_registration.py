"""Mixer Lab participant registration.

Registration does not generate deposit addresses or move funds.
All percentages use integer basis points.
"""

from datetime import datetime, timezone

from . import db


DEFAULT_FEE_BPS = 500
MAX_PAYOUT_ADDRESSES = 10


def register_participant(
    conn,
    rpc,
    *,
    pool_id,
    session_id,
    payouts,
    fee_basis_points=DEFAULT_FEE_BPS,
    existing_session=False,
):
    """Atomically register a session with immutable payout instructions."""

    if not isinstance(pool_id, str) or not pool_id.strip():
        raise ValueError("Pool ID is required")

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Session ID is required")

    if (
        type(fee_basis_points) is not int
        or not 0 <= fee_basis_points <= 10_000
    ):
        raise ValueError("Invalid service fee")

    if not isinstance(payouts, (list, tuple)):
        raise ValueError("Payout instructions must be a list")

    if not 1 <= len(payouts) <= MAX_PAYOUT_ADDRESSES:
        raise ValueError("Registration requires 1-10 payout addresses")

    normalized = []
    seen = set()
    total_bps = 0

    for item in payouts:
        if not isinstance(item, dict):
            raise ValueError("Invalid payout instruction")

        if set(item) != {"address", "allocation_bps"}:
            raise ValueError("Payout requires address and allocation_bps")

        address = item["address"]
        allocation = item["allocation_bps"]

        if not isinstance(address, str) or not address.strip():
            raise ValueError("Invalid payout address")

        if address != address.strip():
            raise ValueError("Payout address contains surrounding whitespace")

        if type(allocation) is not int or not 1 <= allocation <= 10_000:
            raise ValueError("Invalid payout allocation")

        if address in seen:
            raise ValueError("Duplicate payout address")

        seen.add(address)
        total_bps += allocation
        normalized.append((address, allocation))

    if total_bps != 10_000:
        raise ValueError("Payout allocations must total 100%")

    if type(existing_session) is not bool:
        raise ValueError("Invalid registration mode")

    if rpc is None or not callable(
        getattr(rpc, "validate_address", None)
    ):
        raise ValueError(
            "DigiByte address-validation RPC required"
        )

    # Validate before opening a write transaction.
    # Any RPC failure leaves the database unchanged.
    for address, _ in normalized:
        result = rpc.validate_address(address)

        if not isinstance(result, dict) or result.get("isvalid") is not True:
            raise ValueError("Invalid DigiByte payout address")

    now = datetime.now(timezone.utc).isoformat()

    # Session, payout instructions and completion seal must commit together.
    with db.tx(conn):
        if existing_session:
            row = conn.execute(
                """
                SELECT s.state AS session_state,
                       s.fee_basis_points,
                       p.state AS pool_state
                FROM pool_sessions s
                JOIN pools p ON p.id = s.pool_id
                WHERE s.id = ? AND s.pool_id = ?
                """,
                (session_id, pool_id),
            ).fetchone()

            if row is None:
                raise ValueError("Existing session not found")

            if row["pool_state"] != "PAUSED":
                raise ValueError(
                    "Registration requires a PAUSED pool"
                )

            if row["session_state"] != "REGISTERED":
                raise ValueError("Session state not eligible")

            if row["fee_basis_points"] != fee_basis_points:
                raise ValueError(
                    "Existing session fee terms must match"
                )

            seal = conn.execute(
                """
                SELECT 1 FROM pool_registration_seals
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            instructions = conn.execute(
                """
                SELECT 1 FROM pool_payout_instructions
                WHERE session_id = ?
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()

            if seal is not None or instructions is not None:
                raise ValueError(
                    "Registration already started or finalized"
                )
        else:
            conn.execute(
                """
                INSERT INTO pool_sessions(
                    id, pool_id, fee_basis_points, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (session_id, pool_id, fee_basis_points, now),
            )

        for position, (address, allocation) in enumerate(
            normalized, start=1
        ):
            conn.execute(
                """
                INSERT INTO pool_payout_instructions(
                    session_id, position, payout_address, allocation_bps
                )
                VALUES (?, ?, ?, ?)
                """,
                (session_id, position, address, allocation),
            )

        conn.execute(
            """
            INSERT INTO pool_registration_seals(session_id, completed_at)
            VALUES (?, ?)
            """,
            (session_id, now),
        )

    return {
        "pool_id": pool_id,
        "session_id": session_id,
        "fee_basis_points": fee_basis_points,
        "payout_count": len(normalized),
        "state": "REGISTERED",
    }
