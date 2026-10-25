"""Persistent Mixer Lab recovery ownership and scheduling primitives.

No RPC, wallet, transaction, or automatic worker side effects. All mutations
are serialized by SQLite BEGIN IMMEDIATE and require matching lease fencing.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import db


def _time(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec="microseconds")


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _identity(lease_name, owner_token):
    if not isinstance(lease_name, str) or not lease_name.strip():
        raise ValueError("Invalid lease name")
    if not isinstance(owner_token, str) or not owner_token.strip():
        raise ValueError("Invalid owner token")


def acquire_lease(conn, *, lease_name, owner_token=None, ttl_seconds=60,
                  now=None):
    """Acquire free/expired lease; return fenced ownership or None if busy.

    Never overwrite another active owner, including the same owner token.
    Expires on a strictly earlier timestamp or at the exact expiry instant.
    """
    if owner_token is None:
        owner_token = uuid4().hex
    _identity(lease_name, owner_token)
    _positive_int(ttl_seconds, "ttl_seconds")
    instant = _time(now)
    timestamp = _stamp(instant)
    expiry = _stamp(instant + timedelta(seconds=ttl_seconds))
    with db.tx(conn):
        row = conn.execute(
            "SELECT owner_token, fencing_token, expires_at "
            "FROM pool_recovery_leases WHERE lease_name=?",
            (lease_name,),
        ).fetchone()
        if row is not None and row["owner_token"] is not None:
            if row["expires_at"] > timestamp:
                return None
        fence = 1 if row is None else row["fencing_token"] + 1
        if row is None:
            conn.execute(
                "INSERT INTO pool_recovery_leases "
                "(lease_name,owner_token,fencing_token,expires_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (lease_name, owner_token, fence, expiry, timestamp),
            )
        else:
            conn.execute(
                "UPDATE pool_recovery_leases SET owner_token=?, "
                "fencing_token=?,expires_at=?,updated_at=? WHERE lease_name=?",
                (owner_token, fence, expiry, timestamp, lease_name),
            )
    return {"lease_name": lease_name, "owner_token": owner_token,
            "fencing_token": fence, "expires_at": expiry}


def _require_lease(conn, lease, now):
    if not isinstance(lease, dict):
        raise ValueError("Lease credentials required")
    name = lease.get("lease_name")
    owner = lease.get("owner_token")
    fence = lease.get("fencing_token")
    _identity(name, owner)
    _positive_int(fence, "fencing_token")
    row = conn.execute(
        "SELECT owner_token,fencing_token,expires_at "
        "FROM pool_recovery_leases WHERE lease_name=?", (name,),
    ).fetchone()
    if (row is None or row["owner_token"] != owner
            or row["fencing_token"] != fence
            or row["expires_at"] is None
            or row["expires_at"] <= _stamp(now)):
        raise PermissionError("Recovery lease lost or expired")


def renew_lease(conn, *, lease, ttl_seconds=60, now=None):
    """Extend only the current nonexpired fenced owner."""
    _positive_int(ttl_seconds, "ttl_seconds")
    instant = _time(now)
    expiry = _stamp(instant + timedelta(seconds=ttl_seconds))
    with db.tx(conn):
        _require_lease(conn, lease, instant)
        conn.execute(
            "UPDATE pool_recovery_leases SET expires_at=?,updated_at=? "
            "WHERE lease_name=?",
            (expiry, _stamp(instant), lease["lease_name"]),
        )
    return {**lease, "expires_at": expiry}


def release_lease(conn, *, lease, now=None):
    """Release only current fenced owner, retaining monotone generation."""
    instant = _time(now)
    with db.tx(conn):
        _require_lease(conn, lease, instant)
        conn.execute(
            "UPDATE pool_recovery_leases "
            "SET owner_token=NULL,expires_at=NULL,updated_at=? "
            "WHERE lease_name=?",
            (_stamp(instant), lease["lease_name"]),
        )


def _session(conn, session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Invalid session ID")
    if conn.execute(
        "SELECT 1 FROM pool_sessions WHERE id=?", (session_id,)
    ).fetchone() is None:
        raise ValueError("Unknown recovery session")


def start_attempt(conn, *, lease, session_id, now=None):
    """Append STARTED evidence, and mark the session as attempted.

    The caller must already have established session eligibility and must
    not interpret the existence of an attempt as permission for any funds.
    """
    instant = _time(now)
    stamp = _stamp(instant)
    with db.tx(conn):
        _require_lease(conn, lease, instant)
        _session(conn, session_id)
        active = conn.execute(
            "SELECT 1 FROM pool_recovery_runs "
            "WHERE session_id=? AND event='STARTED' "
            "AND NOT EXISTS ("
            " SELECT 1 FROM pool_recovery_runs done "
            " WHERE done.attempt_id=pool_recovery_runs.id"
            ") LIMIT 1", (session_id,),
        ).fetchone()
        if active:
            raise RuntimeError("Unresolved recovery attempt requires reconciliation")
        conn.execute(
            "INSERT INTO pool_recovery_schedules "
            "(session_id,state,consecutive_failures,last_attempt_at,"
            "next_attempt_at,updated_at) VALUES (?,'DUE',0,?,NULL,?) "
            "ON CONFLICT(session_id) DO UPDATE SET state='DUE',"
            "last_attempt_at=excluded.last_attempt_at,"
            "next_attempt_at=NULL,updated_at=excluded.updated_at",
            (session_id, stamp, stamp),
        )
        cur = conn.execute(
            "INSERT INTO pool_recovery_runs "
            "(session_id,lease_name,owner_token,fencing_token,event,recorded_at) "
            "VALUES (?,?,?,?, 'STARTED',?)",
            (session_id, lease["lease_name"], lease["owner_token"],
             lease["fencing_token"], stamp),
        )
        return cur.lastrowid


def complete_attempt(conn, *, lease, session_id, started_id,
                     success, error_type=None, retry_base_seconds=60,
                     retry_max_seconds=3600, monitor_seconds=300, now=None):
    """Finish one current attempt, journal outcome, schedule next check.

    No external action occurs; all three changes are in one transaction.
    """
    instant = _time(now)
    _positive_int(started_id, "started_id")
    for name, value in (("retry_base_seconds", retry_base_seconds),
                        ("retry_max_seconds", retry_max_seconds),
                        ("monitor_seconds", monitor_seconds)):
        _positive_int(value, name)
    if type(success) is not bool:
        raise ValueError("success must be bool")
    if not success and (not isinstance(error_type, str) or not error_type.strip()):
        raise ValueError("Failure requires error_type")
    if success and error_type is not None:
        raise ValueError("Success cannot have error_type")
    with db.tx(conn):
        _require_lease(conn, lease, instant)
        row = conn.execute(
            "SELECT * FROM pool_recovery_runs WHERE id=?", (started_id,)
        ).fetchone()
        if (row is None or row["event"] != "STARTED"
                or row["session_id"] != session_id
                or row["lease_name"] != lease["lease_name"]
                or row["owner_token"] != lease["owner_token"]
                or row["fencing_token"] != lease["fencing_token"]):
            raise ValueError("Attempt does not belong to lease")
        already = conn.execute(
            "SELECT 1 FROM pool_recovery_runs "
            "WHERE attempt_id=? LIMIT 1",
            (started_id,),
        ).fetchone()
        if already:
            raise RuntimeError("Attempt already finalized")
        prev = conn.execute(
            "SELECT consecutive_failures FROM pool_recovery_schedules "
            "WHERE session_id=?", (session_id,),
        ).fetchone()
        if prev is None:
            raise RuntimeError("Recovery schedule missing")
        failures = 0 if success else prev["consecutive_failures"] + 1
        delay = (monitor_seconds if success else
                 min(retry_max_seconds, retry_base_seconds *
                     2 ** min(failures - 1, 30)))
        nxt = _stamp(instant + timedelta(seconds=delay))
        state = "IDLE" if success else "BACKOFF"
        stamp = _stamp(instant)
        conn.execute(
            "UPDATE pool_recovery_schedules SET state=?,"
            "consecutive_failures=?,next_attempt_at=?,updated_at=? "
            "WHERE session_id=?",
            (state, failures, nxt, stamp, session_id),
        )
        event = "COMPLETED" if success else "FAILED"
        conn.execute(
            "INSERT INTO pool_recovery_runs "
            "(session_id,lease_name,owner_token,fencing_token,event,"
            "error_type,recorded_at,attempt_id) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, lease["lease_name"], lease["owner_token"],
             lease["fencing_token"], event, error_type, stamp, started_id),
        )
    return {"session_id": session_id, "state": state, "failures": failures,
            "next_attempt_at": nxt, "financial_actions_authorized": False}


def interrupt_abandoned_attempt(
    conn, *, lease, session_id, started_id, now=None,
    retry_base_seconds=60, retry_max_seconds=3600,
):
    """Resolve an abandoned attempt under a newer fenced lease.

    This records an interruption, not a successful recovery.
    No RPC, credits, transfers, or payouts are performed.
    """
    instant = _time(now)
    _positive_int(started_id, "started_id")
    _positive_int(retry_base_seconds, "retry_base_seconds")
    _positive_int(retry_max_seconds, "retry_max_seconds")

    with db.tx(conn):
        _require_lease(conn, lease, instant)

        started = conn.execute(
            "SELECT * FROM pool_recovery_runs "
            "WHERE id=? AND session_id=? AND event='STARTED'",
            (started_id, session_id),
        ).fetchone()

        if started is None:
            raise ValueError("Unknown STARTED recovery attempt")

        if (
            started["lease_name"] != lease["lease_name"]
            or started["fencing_token"] >= lease["fencing_token"]
        ):
            raise PermissionError("A newer fenced lease is required")

        terminal = conn.execute(
            "SELECT 1 FROM pool_recovery_runs "
            "WHERE attempt_id=? LIMIT 1",
            (started_id,),
        ).fetchone()

        if terminal is not None:
            raise RuntimeError("Attempt already finalized")

        schedule = conn.execute(
            "SELECT consecutive_failures "
            "FROM pool_recovery_schedules WHERE session_id=?",
            (session_id,),
        ).fetchone()

        if schedule is None:
            raise RuntimeError("Recovery schedule missing")

        failures = schedule["consecutive_failures"] + 1
        delay = min(
            retry_max_seconds,
            retry_base_seconds * 2 ** min(failures - 1, 30),
        )

        stamp = _stamp(instant)
        next_attempt = _stamp(
            instant + timedelta(seconds=delay)
        )

        conn.execute(
            "INSERT INTO pool_recovery_runs "
            "(session_id,lease_name,owner_token,fencing_token,"
            "event,recorded_at,attempt_id) "
            "VALUES (?,?,?,?, 'INTERRUPTED',?,?)",
            (
                session_id,
                started["lease_name"],
                started["owner_token"],
                started["fencing_token"],
                stamp,
                started_id,
            ),
        )

        conn.execute(
            "UPDATE pool_recovery_schedules "
            "SET state='BACKOFF',consecutive_failures=?,"
            "next_attempt_at=?,updated_at=? "
            "WHERE session_id=?",
            (failures, next_attempt, stamp, session_id),
        )

    return {
        "session_id": session_id,
        "interrupted_attempt_id": started_id,
        "state": "BACKOFF",
        "failures": failures,
        "next_attempt_at": next_attempt,
        "financial_actions_authorized": False,
    }


def require_active_lease(conn, lease):
    """Check fenced ownership inside the caller's SQLite transaction."""
    _require_lease(conn, lease, _time())
