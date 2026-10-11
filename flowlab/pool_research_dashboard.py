"""Read-only Mixer Lab research dashboard data.

Transforms existing research reports into structured dashboard telemetry.
Does not perform RPC calls, modify the database, approve experiments,
or authorize financial operations.
"""

from .pool_experiment_research import experiment_research_report


def research_dashboard_snapshot(
    conn,
    *,
    experiment_id,
    pool_id,
    session_id,
):
    """Produce an informational research dashboard snapshot."""

    report = experiment_research_report(
        conn,
        experiment_id=experiment_id,
        pool_id=pool_id,
        session_id=session_id,
    )

    review_ready = report["research_review_ready"]

    return {
        "experiment": {
            "id": report["experiment_id"],
            "state": report["experiment_state"],
            "config_hash": report["config_hash"],
            "config_version": report["config_version"],
            "approved": report["experiment_approved"],
        },
        "participant": {
            "pool_id": report["pool_id"],
            "session_id": report["session_id"],
            "lifecycle_state": report["lifecycle_state"],
        },
        "research": {
            "review_ready": review_ready,
            "observation_count": report["observation_count"],
            "supported_observations": report["supported_observations"],
            "invalidated_observations": report["invalidated_observations"],
            "recheck_required": report["recheck_required"],
            "observations": report["observations"],
        },
        "operator_attention": {
            "required": not review_ready,
            "reason": (
                "Recorded evidence requires research review or rechecking"
                if not review_ready
                else None
            ),
        },
        "authorization": {
            "financial_actions": False,
            "automatic_experiment_approval": False,
            "transaction_broadcast": False,
        },
    }
