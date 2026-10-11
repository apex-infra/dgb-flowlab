"""Read-only Mixer Lab participant lifecycle telemetry.

Derives operational status from existing records without financial
crediting, payout authorization, RPC calls, or database mutations.
"""

from .pool_evidence_assessment import assess_evidence


def session_snapshot(conn, *, pool_id, session_id):
    """Return the derived lifecycle status for one pool participant."""

    if not isinstance(pool_id, str) or not pool_id.strip():
        raise ValueError("Invalid pool ID")

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Invalid session ID")

    session = conn.execute(
        """
        SELECT s.id, s.pool_id, s.state, s.fee_basis_points,
               s.created_at
        FROM pool_sessions s
        WHERE s.id = ? AND s.pool_id = ?
        """,
        (session_id, pool_id),
    ).fetchone()

    if session is None:
        raise ValueError("Pool participant session not found")

    seal = conn.execute(
        """
        SELECT completed_at
        FROM pool_registration_seals
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()

    payouts = conn.execute(
        """
        SELECT position, payout_address, allocation_bps
        FROM pool_payout_instructions
        WHERE session_id = ?
        ORDER BY position ASC
        """,
        (session_id,),
    ).fetchall()

    address = conn.execute(
        """
        SELECT wallet, address, state
        FROM pool_deposit_addresses
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()

    observations = conn.execute(
        """
        SELECT id, txid, vout, amount_sats,
               confirmations, blockhash, state
        FROM pool_deposit_observations
        WHERE pool_id = ? AND session_id = ?
        ORDER BY id ASC
        """,
        (pool_id, session_id),
    ).fetchall()

    deposits = []

    for observation in observations:
        evidence = assess_evidence(
            conn,
            observation_id=observation["id"],
        )

        deposits.append({
            "observation_id": observation["id"],
            "txid": observation["txid"],
            "vout": observation["vout"],
            "amount_sats": observation["amount_sats"],
            "confirmations": observation["confirmations"],
            "blockhash": observation["blockhash"],
            "observation_state": observation["state"],
            "evidence_state": evidence["state"],
            "verification_event_id": evidence["verification_event_id"],
            "reconciliation_event_id": evidence["reconciliation_event_id"],
        })

    counts = {
        "CURRENTLY_SUPPORTED": sum(
            item["evidence_state"] == "CURRENTLY_SUPPORTED"
            for item in deposits
        ),
        "INVALIDATED": sum(
            item["evidence_state"] == "INVALIDATED"
            for item in deposits
        ),
        "RECHECK_REQUIRED": sum(
            item["evidence_state"] == "RECHECK_REQUIRED"
            for item in deposits
        ),
    }

    registration_complete = (
        seal is not None
        and 1 <= len(payouts) <= 10
        and sum(row["allocation_bps"] for row in payouts) == 10_000
    )

    address_state = address["state"] if address else None

    if not registration_complete:
        state = "REGISTRATION_REQUIRED"
    elif address_state in ("REQUESTING", "RECOVERY_REQUIRED"):
        state = "ADDRESS_RECOVERY_REQUIRED"
    elif address_state is None:
        state = "ADDRESS_REQUIRED"
    elif address_state != "READY":
        state = "ADDRESS_RECOVERY_REQUIRED"
    elif not deposits:
        state = "AWAITING_DEPOSIT"
    elif (
        counts["INVALIDATED"]
        or any(
            deposit["observation_state"] == "CONFLICTED"
            for deposit in deposits
        )
    ):
        state = "EVIDENCE_REVIEW_REQUIRED"
    elif counts["CURRENTLY_SUPPORTED"] == len(deposits):
        state = "READY_FOR_REVIEW"
    else:
        state = "OBSERVING"

    return {
        "pool_id": pool_id,
        "session_id": session_id,
        "state": state,
        "registration_complete": registration_complete,
        "fee_basis_points": session["fee_basis_points"],
        "payout_count": len(payouts),
        "address_state": address_state,
        "deposit_address": (
            address["address"]
            if address_state == "READY"
            else None
        ),
        "observation_count": len(deposits),
        "evidence_counts": counts,
        "observations": deposits,
        "financial_actions_authorized": False,
    }
