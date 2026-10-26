"""Read-only Mixer Lab recovery operator commands."""

from . import db as database



def validate_recovery_database(path):
    """Validate an existing V7 database without writing or migrating."""
    try:
        conn = database.connect_readonly(path)
    except (OSError, database.sqlite3.DatabaseError):
        return False

    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()

        if version is None or version["value"] != "7":
            return False

        database.validate_pool_recovery_schema(conn)
        return True
    except (RuntimeError, ValueError, database.sqlite3.DatabaseError):
        return False
    finally:
        conn.close()



def offline_recovery_preflight(
    path, out=print, *, report_offline_rpc=True
):
    """Inspect Mixer Lab recovery readiness without database writes."""
    from .pool_recovery_supervisor import eligible_sessions

    if not validate_recovery_database(path):
        out("PREFLIGHT FAIL: Existing schema V7 database required")
        out("Recovery readiness: BLOCKED")
        return 2

    conn = database.connect_readonly(path)

    try:
        leases = conn.execute(
            "SELECT lease_name,owner_token,fencing_token,expires_at "
            "FROM pool_recovery_leases ORDER BY lease_name"
        ).fetchall()

        sessions = eligible_sessions(conn)

        out("Mixer Lab Recovery Preflight")
        out("Database schema: V7 VALID")
        out("Recovery tables: VALID")
        out(f"Eligible sessions: {len(sessions)}")

        if not leases:
            out("Recovery lease: NOT INITIALIZED")
        else:
            for lease in leases:
                owner = (
                    "FREE"
                    if lease["owner_token"] is None
                    else "OWNED"
                )

                out(
                    f"Recovery lease: {lease['lease_name']} "
                    f"| {owner} "
                    f"| fence={lease['fencing_token']}"
                )

        if report_offline_rpc:
            out("RPC connectivity: NOT CHECKED (offline preflight)")
        out("Financial actions authorized: NO")
        out("Automatic recovery: DISABLED")

        if report_offline_rpc:
            out("Recovery readiness: OFFLINE_VALIDATED")

    finally:
        conn.close()

    return 0



def recovery_node_connectivity(rpc, out=print):
    """Perform one read-only DigiByte Core connectivity request."""
    from .rpc import RpcError

    try:
        height = rpc.get_block_count()

        if type(height) is not int or height < 0:
            raise ValueError("Invalid blockchain height")

    except Exception:
        # Preflight must fail closed without exposing RPC error details.
        # Do not suppress KeyboardInterrupt or SystemExit.
        out("RPC connectivity: FAILED")
        return 2

    out(f"RPC connectivity: OK | block height={height}")
    return 0



def recovery_node_sync_diagnostic(rpc, out=print):
    """Classify node-reported synchronization using read-only RPC."""
    import math
    from decimal import Decimal

    try:
        info = rpc.get_blockchain_info()

        if not isinstance(info, dict):
            raise ValueError("Invalid blockchain response")

        chain = info.get("chain")
        blocks = info.get("blocks")
        headers = info.get("headers")
        initial_download = info.get("initialblockdownload")
        progress = info.get("verificationprogress")

        if chain != "main":
            state = "UNKNOWN"

        elif (
            type(blocks) is not int
            or type(headers) is not int
            or blocks < 0
            or headers < 0
            or type(initial_download) is not bool
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float, Decimal))
            or not math.isfinite(float(progress))
            or not 0 <= progress <= 1
        ):
            state = "UNKNOWN"

        elif initial_download or blocks < headers:
            state = "UNSYNCED"

        elif blocks > headers:
            state = "UNKNOWN"

        else:
            state = "SYNCED"

    except Exception:
        # Never expose RPC exceptions or malformed response details.
        # KeyboardInterrupt and SystemExit remain unsuppressed.
        state = "UNKNOWN"

    out(f"Node synchronization: {state}")
    return state



def recovery_operational_diagnostic(
    path, *, node_rpc=None, check_node=False, out=print
):
    """Report operational conditions without acquiring recovery leases."""
    from datetime import datetime, timezone
    from .pool_recovery_supervisor import eligible_sessions
    from .pool_recovery_runner import LEASE_NAME

    state = "UNKNOWN"
    lease_state = "UNKNOWN"
    count = 0

    try:
        conn = database.connect_readonly(path)

        try:
            count = len(eligible_sessions(conn))

            lease = conn.execute(
                "SELECT owner_token,expires_at "
                "FROM pool_recovery_leases WHERE lease_name=?",
                (LEASE_NAME,),
            ).fetchone()

            if lease is None or lease["owner_token"] is None:
                lease_state = "AVAILABLE"
            else:
                expiry = lease["expires_at"]

                if not isinstance(expiry, str):
                    raise ValueError("Invalid lease expiry")

                expiry_time = datetime.fromisoformat(
                    expiry.replace("Z", "+00:00")
                )

                if expiry_time.tzinfo is None:
                    raise ValueError("Naive lease expiry")

                now = datetime.now(timezone.utc)

                lease_state = (
                    "BUSY" if expiry_time > now
                    else "AVAILABLE"
                )

        finally:
            conn.close()

    except Exception:
        # Unknown database state must never imply availability.
        out("Recovery lease availability: UNKNOWN")
        out("Session eligibility: UNKNOWN")
        out("Node synchronization: UNKNOWN")
        out("Operational readiness: UNKNOWN")
        return "UNKNOWN"

    out(f"Recovery lease availability: {lease_state}")
    out(f"Session eligibility: {count}")

    if check_node and node_rpc is not None:
        sync = recovery_node_sync_diagnostic(node_rpc, out)
    else:
        sync = "UNKNOWN"
        out("Node synchronization: UNKNOWN")

    if sync == "UNSYNCED":
        state = "UNSYNCED"
    elif lease_state == "BUSY":
        state = "BUSY"
    elif lease_state != "AVAILABLE" or sync != "SYNCED":
        state = "UNKNOWN"
    elif count == 0:
        state = "IDLE"
    else:
        state = "READY"

    out(f"Operational readiness: {state}")
    return state


def recovery_command(
    path, command, out=print, *, limit=25, authorize=False,
    rpc=None, check_node=False, node_rpc=None,
    diagnostics=False,
):
    if command == "preflight":
        result = offline_recovery_preflight(
            path,
            out,
            report_offline_rpc=not check_node,
        )

        if result != 0:
            return result

        if not check_node:
            if diagnostics:
                recovery_operational_diagnostic(path, out=out)
            return 0

        if node_rpc is None:
            out("RPC connectivity: FAILED")
            out("Recovery readiness: DEGRADED")

            if diagnostics:
                recovery_operational_diagnostic(
                    path, check_node=True, out=out
                )

            return 2

        result = recovery_node_connectivity(node_rpc, out)

        if result == 0:
            out("Recovery readiness: READY")
        else:
            out("Recovery readiness: DEGRADED")

        if diagnostics:
            recovery_operational_diagnostic(
                path,
                node_rpc=node_rpc if result == 0 else None,
                check_node=True,
                out=out,
            )

        return result

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
            # Revalidate on the writable connection before any
            # recovery lease, schedule, or journal modification.
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

            try:
                result = run_persistent_recovery_pass(conn, observation_rpc)
            except KeyboardInterrupt:
                # The runner owns lease cleanup and interruption recovery.
                # Never report an interrupted pass as successful.
                out("Recovery interrupted by operator")
                return 130
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
