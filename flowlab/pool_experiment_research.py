"""Read-only FlowLab experiment and Mixer Lab research reporting.

Combines existing experiment metadata with recorded participant evidence.
This report does not associate funds with an experiment, grant approval,
or authorize financial activity.
"""

from .pool_research import assess_research_session


def experiment_research_report(
    conn,
    *,
    experiment_id,
    pool_id,
    session_id,
):
    """Return an informational report without modifying either system."""

    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("Invalid experiment ID")

    experiment = conn.execute(
        """
        SELECT id, state, config_hash, config_version,
               approved_at
        FROM experiments
        WHERE id = ?
        """,
        (experiment_id,),
    ).fetchone()

    if experiment is None:
        raise ValueError("Experiment not found")

    research = assess_research_session(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    return {
        "experiment_id": experiment["id"],
        "experiment_state": experiment["state"],
        "config_hash": experiment["config_hash"],
        "config_version": experiment["config_version"],
        "experiment_approved": experiment["approved_at"] is not None,
        "pool_id": research["pool_id"],
        "session_id": research["session_id"],
        "lifecycle_state": research["lifecycle_state"],
        "research_review_ready": research["research_review_ready"],
        "observation_count": research["observation_count"],
        "supported_observations": research["supported_observations"],
        "invalidated_observations": research["invalidated_observations"],
        "recheck_required": research["recheck_required"],
        "observations": research["observations"],
        "financial_actions_authorized": False,
    }
