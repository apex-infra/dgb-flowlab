"""Read-only assessment of recorded Mixer Lab blockchain evidence.

This module evaluates historical audit records only. It performs no RPC
calls, credits, payouts, or transaction broadcasts.

CURRENTLY_SUPPORTED does not establish present blockchain finality.
"""



from datetime import datetime


def _parse_audit_time(value):
    """Parse an offset-aware ISO timestamp; reject ambiguous values."""

    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    return parsed


def _valid_audit_order(verified_at, reconciled_at):
    """Reconciliation must not predate its referenced verification."""

    verified = _parse_audit_time(verified_at)
    reconciled = _parse_audit_time(reconciled_at)

    return (
        verified is not None
        and reconciled is not None
        and reconciled >= verified
    )



def _result(state, reason, *, verification_id=None, reconciliation_id=None):
    return {
        "state": state,
        "reason": reason,
        "verification_event_id": verification_id,
        "reconciliation_event_id": reconciliation_id,
    }


def assess_evidence(conn, *, observation_id):
    """Interpret the latest recorded verification and reconciliation."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    observation = conn.execute(
        """
        SELECT state, confirmations, blockhash
        FROM pool_deposit_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()

    if observation is None:
        raise ValueError("Unknown deposit observation")

    verification = conn.execute(
        """
        SELECT id, state, blockhash, confirmations, verified_at
        FROM pool_verification_events
        WHERE observation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    if verification is None:
        return _result(
            "RECHECK_REQUIRED",
            "No recorded blockchain verification",
        )

    verification_id = verification["id"]

    if verification["state"] != "VERIFIED":
        return _result(
            "RECHECK_REQUIRED",
            "Latest verification does not establish valid evidence",
            verification_id=verification_id,
        )

    reconciliation = conn.execute(
        """
        SELECT id, verification_event_id, state, blockhash, reconciled_at
        FROM pool_reconciliation_events
        WHERE observation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    if reconciliation is None:
        return _result(
            "RECHECK_REQUIRED",
            "No reconciliation recorded after verification",
            verification_id=verification_id,
        )

    reconciliation_id = reconciliation["id"]

    if reconciliation["verification_event_id"] != verification_id:
        return _result(
            "RECHECK_REQUIRED",
            "Reconciliation does not reference latest verification",
            verification_id=verification_id,
            reconciliation_id=reconciliation_id,
        )

    if not _valid_audit_order(
        verification["verified_at"],
        reconciliation["reconciled_at"],
    ):
        return _result(
            "RECHECK_REQUIRED",
            "Verification and reconciliation timestamps are inconsistent",
            verification_id=verification_id,
            reconciliation_id=reconciliation_id,
        )

    if reconciliation["state"] == "INVALIDATED":
        return _result(
            "INVALIDATED",
            "Latest reconciliation invalidated historical block evidence",
            verification_id=verification_id,
            reconciliation_id=reconciliation_id,
        )

    if reconciliation["state"] != "CURRENT":
        return _result(
            "RECHECK_REQUIRED",
            "Latest reconciliation does not establish current evidence",
            verification_id=verification_id,
            reconciliation_id=reconciliation_id,
        )

    if (
        verification["confirmations"] <= 0
        or not isinstance(verification["blockhash"], str)
        or reconciliation["blockhash"] != verification["blockhash"]
        or observation["state"] != "CONFIRMED"
        or type(observation["confirmations"]) is not int
        or observation["confirmations"] <= 0
        or observation["blockhash"] != verification["blockhash"]
    ):
        return _result(
            "RECHECK_REQUIRED",
            "Stored observation and audit evidence disagree",
            verification_id=verification_id,
            reconciliation_id=reconciliation_id,
        )

    return _result(
        "CURRENTLY_SUPPORTED",
        "Latest recorded verification and reconciliation agree",
        verification_id=verification_id,
        reconciliation_id=reconciliation_id,
    )
