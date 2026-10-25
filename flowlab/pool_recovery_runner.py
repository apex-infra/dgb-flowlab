"""Manually invoked persistent Mixer Lab evidence recovery runner.

Development-only orchestration. No background scheduling, financial
crediting, transaction construction, or broadcasting.
"""

from datetime import datetime, timezone

from .pool_recovery_supervisor import eligible_sessions
from .pool_recovery_coordinator import recover_session
from .pool_recovery_persistence import (
    acquire_lease,
    renew_lease,
    release_lease,
    start_attempt,
    complete_attempt,
    interrupt_abandoned_attempt,
)


LEASE_NAME = "mixer-lab-recovery"


def _now():
    return datetime.now(timezone.utc)


def _schedule(conn, session_id):
    return conn.execute(
        "SELECT state, next_attempt_at, consecutive_failures "
        "FROM pool_recovery_schedules WHERE session_id=?",
        (session_id,),
    ).fetchone()


def _unresolved(conn, session_id):
    """Return the oldest unfinished STARTED event, if any."""

    return conn.execute(
        """
        SELECT started.*
        FROM pool_recovery_runs started
        WHERE started.session_id=?
          AND started.event='STARTED'
          AND NOT EXISTS (
              SELECT 1
              FROM pool_recovery_runs finished
              WHERE finished.attempt_id=started.id
          )
        ORDER BY started.id
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def run_persistent_recovery_pass(
    conn,
    rpc,
    *,
    lease_name=LEASE_NAME,
    ttl_seconds=600,
):
    """Run one fenced, schedule-aware evidence recovery pass.

    This function must be invoked explicitly. It does not start a
    background service or authorize financial operations.
    """

    lease = acquire_lease(
        conn,
        lease_name=lease_name,
        ttl_seconds=ttl_seconds,
    )

    if lease is None:
        return {
            "state": "LEASE_BUSY",
            "results": [],
            "financial_actions_authorized": False,
        }

    results = []

    try:
        for session in eligible_sessions(conn):
            # Refresh ownership before processing each session.
            # A stale worker must stop rather than proceed.
            try:
                lease = renew_lease(
                    conn,
                    lease=lease,
                    ttl_seconds=ttl_seconds,
                )
            except PermissionError:
                return {
                    "state": "LEASE_LOST",
                    "results": results,
                    "financial_actions_authorized": False,
                }

            pool_id = session["pool_id"]
            session_id = session["session_id"]

            schedule = _schedule(conn, session_id)

            # An explicit pause always takes precedence.
            if schedule is not None and schedule["state"] == "PAUSED":
                results.append({
                    "session_id": session_id,
                    "state": "SKIPPED_PAUSED",
                })
                continue

            # Resolve abandoned attempts before checking retry schedules.
            # A future retry must not hide an unfinished STARTED event.
            abandoned = _unresolved(conn, session_id)

            if abandoned is not None:
                try:
                    interrupt_abandoned_attempt(
                        conn,
                        lease=lease,
                        session_id=session_id,
                        started_id=abandoned["id"],
                    )
                    results.append({
                        "session_id": session_id,
                        "state": "INTERRUPTED_BACKOFF",
                    })
                except PermissionError:
                    # Ownership loss is fatal for this entire pass.
                    return {
                        "state": "LEASE_LOST",
                        "results": results + [{
                            "session_id": session_id,
                            "state": "INTERRUPTED_PENDING",
                        }],
                        "financial_actions_authorized": False,
                    }
                except (RuntimeError, ValueError) as error:
                    results.append({
                        "session_id": session_id,
                        "state": "INTERRUPTION_REVIEW_REQUIRED",
                        "error_type": type(error).__name__,
                    })
                continue

            # Ordinary scheduling applies only after interruption recovery.
            if schedule is not None:
                next_attempt = schedule["next_attempt_at"]

                if next_attempt is not None:
                    due = datetime.fromisoformat(next_attempt)

                    if due > _now():
                        results.append({
                            "session_id": session_id,
                            "state": "SKIPPED_NOT_DUE",
                        })
                        continue

            started_id = start_attempt(
                conn,
                lease=lease,
                session_id=session_id,
            )

            try:
                recovery = recover_session(
                    conn,
                    rpc,
                    pool_id=pool_id,
                    session_id=session_id,
                    lease=lease,
                )

                if recovery["pass_state"] != "COMPLETED":
                    raise RuntimeError("Incomplete evidence recovery pass")

            except Exception as error:
                # A lost lease cannot authorize a terminal recovery event.
                # Leave STARTED unresolved for the next fenced owner.
                try:
                    outcome = complete_attempt(
                        conn,
                        lease=lease,
                        session_id=session_id,
                        started_id=started_id,
                        success=False,
                        error_type=type(error).__name__,
                    )
                except PermissionError:
                    return {
                        "state": "LEASE_LOST",
                        "results": results + [{
                            "session_id": session_id,
                            "state": "INTERRUPTED_PENDING",
                        }],
                        "financial_actions_authorized": False,
                    }

                results.append({
                    "session_id": session_id,
                    "state": "FAILED_BACKOFF",
                    "next_attempt_at": outcome["next_attempt_at"],
                    "error_type": type(error).__name__,
                })

            else:
                try:
                    outcome = complete_attempt(
                        conn,
                        lease=lease,
                        session_id=session_id,
                        started_id=started_id,
                        success=True,
                    )
                except PermissionError:
                    # Evidence processing may have finished, but the
                    # worker no longer owns the lease. Do not claim
                    # a completed recovery pass.
                    return {
                        "state": "LEASE_LOST",
                        "results": results + [{
                            "session_id": session_id,
                            "state": "INTERRUPTED_PENDING",
                        }],
                        "financial_actions_authorized": False,
                    }

                results.append({
                    "session_id": session_id,
                    "state": "COMPLETED",
                    "next_attempt_at": outcome["next_attempt_at"],
                })

    finally:
        # Expiration or ownership transfer can invalidate release.
        # The newer owner must never be overwritten.
        try:
            release_lease(conn, lease=lease)
        except PermissionError:
            pass

    return {
        "state": "PASS_FINISHED",
        "results": results,
        "financial_actions_authorized": False,
    }
