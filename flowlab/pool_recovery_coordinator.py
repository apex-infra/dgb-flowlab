"""Mixer Lab evidence recovery coordinator.

Runs an explicit recovery pass through the existing coordinator.
Does not authorize financial operations.
"""

from .pool_coordinator import coordinate_session
from .pool_recovery_assessment import assess_session_recovery


def recover_session(conn, rpc, *, pool_id, session_id, page_size=100, lease=None):
    """Refresh blockchain evidence and report recovery state."""

    before = assess_session_recovery(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    coordination = coordinate_session(
        conn,
        rpc,
        pool_id=pool_id,
        session_id=session_id,
        page_size=page_size,
        lease=lease,
    )

    after = assess_session_recovery(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    return {
        "pass_state": coordination["pass_state"],
        "pool_id": pool_id,
        "session_id": session_id,
        "before": before,
        "after": after,
        "coordination": coordination,
        "financial_actions_authorized": False,
    }
