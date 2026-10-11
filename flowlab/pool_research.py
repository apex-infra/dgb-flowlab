"""Read-only Mixer Lab research eligibility assessment.

Summarizes stored participant lifecycle and blockchain evidence
for research review.

No RPC calls, experiment approval, financial crediting, or broadcasting.
"""

from .pool_lifecycle import session_snapshot


def assess_research_session(conn, *, pool_id, session_id):
    """Assess recorded evidence for research review only."""

    lifecycle = session_snapshot(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    counts = lifecycle["evidence_counts"]

    supported = counts["CURRENTLY_SUPPORTED"]
    invalidated = counts["INVALIDATED"]
    recheck = counts["RECHECK_REQUIRED"]

    ready = (
        lifecycle["state"] == "READY_FOR_REVIEW"
        and lifecycle["registration_complete"]
        and lifecycle["address_state"] == "READY"
        and lifecycle["observation_count"] > 0
        and supported == lifecycle["observation_count"]
        and invalidated == 0
        and recheck == 0
    )

    return {
        "pool_id": pool_id,
        "session_id": session_id,
        "lifecycle_state": lifecycle["state"],
        "research_review_ready": ready,
        "observation_count": lifecycle["observation_count"],
        "supported_observations": supported,
        "invalidated_observations": invalidated,
        "recheck_required": recheck,
        "observations": lifecycle["observations"],
        "financial_actions_authorized": False,
    }
