"""Read-only Mixer Lab recovery operator commands."""

from . import db as database


def recovery_command(
    path, command, out=print, *, limit=25, authorize=False, rpc=None
):
    if command == "run-once":
        if not authorize:
            out("REFUSED: recovery requires --authorize")
            return 2

        if rpc is None:
            out("REFUSED: recovery RPC must be explicitly supplied")
            return 2

        conn = database.connect_readonly(path)
        try:
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()

            if version is None or version["value"] != "7":
                out("REFUSED: recovery requires schema V7")
                return 2

            try:
                database.validate_pool_recovery_schema(conn)
            except (RuntimeError, ValueError, database.sqlite3.DatabaseError):
                out("REFUSED: recovery schema validation failed")
                return 2
        finally:
            conn.close()

        from .pool_recovery_runner import run_persistent_recovery_pass
        from .pool_recovery_rpc import RecoveryRpc

        # The recovery coordinator receives observation methods only.
        # Never expose transaction signing or broadcasting interfaces.
        observation_rpc = RecoveryRpc(rpc)

        # Authorization and read-only schema preflight must succeed
        # before acquiring a writable recovery connection.
        conn = database.connect(path)
        try:
            result = run_persistent_recovery_pass(conn, observation_rpc)
        finally:
            conn.close()

        if result["financial_actions_authorized"] is not False:
            raise RuntimeError("Recovery violated financial safety boundary")

        out(f"Recovery result: {result['state']}")

        for item in result["results"]:
            out(
                f"{item['session_id']} | {item['state']}"
            )

        # A completed pass is not necessarily a successful pass.
        # Surface session failures to operators and monitoring scripts.
        safe_states = {
            "COMPLETED",
            "SKIPPED_PAUSED",
            "SKIPPED_NOT_DUE",
        }

        failed_sessions = [
            item for item in result["results"]
            if item["state"] not in safe_states
        ]

        if result["state"] != "PASS_FINISHED" or failed_sessions:
            return 2

        return 0

    if command not in ("status", "schedules", "history"):
        raise ValueError("Unsupported recovery command")

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 500
    ):
        raise ValueError("History limit must be between 1 and 500")

    conn = database.connect_readonly(path)

    try:
        if command == "status":
            rows = conn.execute(
                "SELECT lease_name,owner_token,fencing_token,"
                "expires_at,updated_at FROM pool_recovery_leases "
                "ORDER BY lease_name"
            ).fetchall()

            out("Mixer Lab recovery: MANUAL / NOT AUTOMATICALLY ENABLED")

            if not rows:
                out("No recovery leases recorded")

            for row in rows:
                owner = "FREE" if row["owner_token"] is None else "OWNED"
                out(
                    f"{row['lease_name']} | {owner} | "
                    f"fence={row['fencing_token']} | "
                    f"expires={row['expires_at'] or '-'}"
                )

        elif command == "schedules":
            rows = conn.execute(
                "SELECT session_id,state,consecutive_failures,"
                "last_attempt_at,next_attempt_at "
                "FROM pool_recovery_schedules "
                "ORDER BY session_id"
            ).fetchall()

            if not rows:
                out("No recovery schedules")

            for row in rows:
                out(
                    f"{row['session_id']} | {row['state']} | "
                    f"failures={row['consecutive_failures']} | "
                    f"next={row['next_attempt_at'] or '-'}"
                )

        elif command == "history":
            rows = conn.execute(
                "SELECT id,session_id,event,attempt_id,error_type,recorded_at "
                "FROM pool_recovery_runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

            if not rows:
                out("No recovery history")

            for row in rows:
                out(
                    f"#{row['id']} | {row['session_id']} | "
                    f"{row['event']} | "
                    f"attempt={row['attempt_id'] or '-'} | "
                    f"error={row['error_type'] or '-'}"
                )

    finally:
        conn.close()

    return 0
