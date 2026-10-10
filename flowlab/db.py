"""
db.py -- SQLite schema and transaction helper.

Design rules enforced here, in the database itself, not just in Python:
  * audit_log is append-only (UPDATE and DELETE are aborted by trigger)
  * an experiment's config cannot change once it has been approved
  * at most one live (intent / unknown / done) action per (job, kind),
    which is what makes a duplicate broadcast structurally impossible
  * amounts are INTEGER satoshis everywhere, never floats

Every state change happens inside one BEGIN IMMEDIATE transaction
together with its audit row, so state and audit can never disagree.
"""

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id                TEXT PRIMARY KEY,
    description       TEXT NOT NULL DEFAULT '',
    tags_json         TEXT NOT NULL DEFAULT '[]',
    operator_notes    TEXT NOT NULL DEFAULT '',
    state             TEXT NOT NULL,
    state_reason      TEXT NOT NULL DEFAULT '',
    paused_from       TEXT,
    config_version    INTEGER NOT NULL DEFAULT 0,
    config_json       TEXT,
    config_hash       TEXT,
    random_seed       INTEGER,
    created_at        TEXT NOT NULL,
    configured_at     TEXT,
    approved_at       TEXT,
    approval_note     TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    final_state_json  TEXT,
    stats_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS flows (
    id                     TEXT PRIMARY KEY,
    experiment_id          TEXT NOT NULL REFERENCES experiments(id),
    description            TEXT NOT NULL DEFAULT '',
    source_wallet          TEXT NOT NULL,
    flow_wallets_json      TEXT NOT NULL,
    destination_wallet     TEXT NOT NULL,
    initial_alloc_sats     INTEGER NOT NULL CHECK(initial_alloc_sats > 0),
    state                  TEXT NOT NULL,
    error_state            TEXT NOT NULL DEFAULT 'NONE'
                             CHECK(error_state IN ('NONE','ERROR','RECOVERY')),
    error_detail           TEXT NOT NULL DEFAULT '',
    recovery_detail        TEXT NOT NULL DEFAULT '',
    confirmations_required INTEGER NOT NULL CHECK(confirmations_required >= 1),
    randomization_json     TEXT NOT NULL DEFAULT '{}',
    random_seed            INTEGER,
    final_state_json       TEXT,
    stats_json             TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    completed_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_flows_experiment ON flows(experiment_id);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    flow_id             TEXT NOT NULL REFERENCES flows(id),
    seq                 INTEGER NOT NULL,
    state               TEXT NOT NULL DEFAULT 'PLANNED'
                          CHECK(state IN ('PLANNED','BROADCAST','CONFIRMED','FAILED','CANCELLED')),
    planned_json        TEXT NOT NULL,
    generated_from_json TEXT NOT NULL DEFAULT '{}',   -- state that caused this job
    result_json         TEXT,                          -- what actually happened
    planned_delay_s     INTEGER,
    actual_executed_at  TEXT,
    txid                TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(flow_id, seq)
);

CREATE TABLE IF NOT EXISTS job_dependencies (
    job_id            TEXT NOT NULL REFERENCES jobs(id),
    depends_on_job_id TEXT NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (job_id, depends_on_job_id)
);

CREATE TABLE IF NOT EXISTS flow_txids (
    txid          TEXT PRIMARY KEY,
    flow_id       TEXT NOT NULL REFERENCES flows(id),
    job_id        TEXT REFERENCES jobs(id),
    confirmations INTEGER NOT NULL DEFAULT 0,
    block_height  INTEGER,
    recorded_at   TEXT NOT NULL,
    confirmed_at  TEXT
);

-- Write-ahead journal for anything that touches the outside world.
-- 'intent' is written BEFORE the action; a crash leaves it behind, and
-- recovery turns it into 'unknown' (never auto-retried).
CREATE TABLE IF NOT EXISTS action_journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id      TEXT NOT NULL REFERENCES flows(id),
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(status IN ('intent','done','failed','unknown')),
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_action
    ON action_journal(job_id, kind) WHERE status IN ('intent','unknown','done');

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    event           TEXT NOT NULL,
    experiment_id   TEXT,
    flow_id         TEXT,
    job_id          TEXT,
    state           TEXT,
    txid            TEXT,
    wallet          TEXT,
    address         TEXT,
    amount_sats     INTEGER,
    resulting_state TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_experiment ON audit_log(experiment_id);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS experiments_config_immutable
BEFORE UPDATE OF config_json, config_hash, config_version, random_seed ON experiments
WHEN OLD.approved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'experiment config is immutable after approval'); END;
"""


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextmanager
def tx(conn):
    """One atomic write transaction. Commits on success, rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def validate_pool_schema(conn):
    """Reject incomplete Mixer Lab schemas without modifying them."""

    required_tables = {
        "pools",
        "pool_sessions",
        "pool_deposits",
        "pool_journal_transactions",
        "pool_journal_entries",
    }

    required_triggers = {
        "pool_session_fee_immutable",
        "pool_deposit_no_update",
        "pool_deposit_no_delete",
        "pool_journal_insert_draft_only",
        "pool_journal_post_balanced",
        "pool_journal_no_update",
        "pool_journal_no_delete",
        "pool_journal_no_insert",
        "pool_journal_transaction_immutable",
        "pool_journal_transaction_no_delete",
    }

    objects = {
        (row["type"], row["name"])
        for row in conn.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE type IN ('table', 'trigger')
            """
        )
    }

    missing_tables = sorted(
        name for name in required_tables
        if ("table", name) not in objects
    )

    missing_triggers = sorted(
        name for name in required_triggers
        if ("trigger", name) not in objects
    )

    if missing_tables or missing_triggers:
        raise RuntimeError(
            "incomplete Mixer Lab schema v2: "
            f"missing tables={missing_tables}; "
            f"missing triggers={missing_triggers}"
        )


def init(conn):
    # Check existing schema versions before changing an existing database.
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()

    if has_meta:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("existing database has no schema_version")
        try:
            version = int(row["value"])
        except ValueError as exc:
            raise RuntimeError("invalid database schema_version") from exc
        if version not in (1, SCHEMA_VERSION):
            raise RuntimeError(
                f"database schema_version {version} != code {SCHEMA_VERSION}; "
                "refusing unsupported migration"
            )
        if version == SCHEMA_VERSION:
            validate_pool_schema(conn)
            return
    else:
        version = None

    # Install or migrate the entire schema atomically.
    # executescript() is deliberately avoided here because it can
    # commit an active transaction before running its statements.
    with tx(conn):
        if version is None:
            statement = ""
            for line in SCHEMA.splitlines():
                statement += line + "\n"
                if sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ""

            if statement.strip():
                raise RuntimeError("Incomplete FlowLab schema statement")

        existing_pool_object = conn.execute(
            """
            SELECT type FROM sqlite_master
            WHERE name = 'pools'
            """
        ).fetchone()

        if existing_pool_object is not None:
            if existing_pool_object["type"] != "table":
                raise sqlite3.OperationalError(
                    "schema migration conflict: pools exists but is not a table"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pools (
                id             TEXT PRIMARY KEY,
                state          TEXT NOT NULL DEFAULT 'PAUSED'
                                 CHECK(state IN ('PAUSED', 'ACTIVE', 'RETIRED')),
                reserve_wallet TEXT NOT NULL,
                fee_wallet     TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                CHECK(reserve_wallet <> fee_wallet)
            )
            """
        )
        # Mixer Lab financial records. All are created in the same
        # transaction as the schema version update.
        conn.execute("""
            CREATE TABLE pool_sessions (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL REFERENCES pools(id),
                fee_basis_points INTEGER NOT NULL
                    CHECK(fee_basis_points BETWEEN 0 AND 10000),
                state TEXT NOT NULL DEFAULT 'REGISTERED',
                created_at TEXT NOT NULL,
                UNIQUE(id, pool_id)
            )
        """)

        conn.execute("""
            CREATE TABLE pool_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id TEXT NOT NULL REFERENCES pools(id),
                session_id TEXT NOT NULL,
                txid TEXT NOT NULL,
                vout INTEGER NOT NULL CHECK(vout >= 0),
                amount_sats INTEGER NOT NULL CHECK(amount_sats > 0),
                fee_sats INTEGER NOT NULL CHECK(fee_sats >= 0),
                credited_at TEXT NOT NULL,
                UNIQUE(txid, vout),
                FOREIGN KEY(session_id, pool_id)
                    REFERENCES pool_sessions(id, pool_id)
            )
        """)

        conn.execute("""
            CREATE TABLE pool_journal_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id TEXT NOT NULL REFERENCES pools(id),
                deposit_id INTEGER UNIQUE REFERENCES pool_deposits(id),
                state TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK(state IN ('DRAFT', 'POSTED')),
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE pool_journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL
                    REFERENCES pool_journal_transactions(id),
                account TEXT NOT NULL,
                debit_sats INTEGER NOT NULL DEFAULT 0
                    CHECK(debit_sats >= 0),
                credit_sats INTEGER NOT NULL DEFAULT 0
                    CHECK(credit_sats >= 0),
                CHECK(
                    (debit_sats > 0 AND credit_sats = 0)
                    OR
                    (credit_sats > 0 AND debit_sats = 0)
                )
            )
        """)

        # Registration terms are immutable once a session exists.
        conn.execute("""
            CREATE TRIGGER pool_session_fee_immutable
            BEFORE UPDATE OF fee_basis_points ON pool_sessions
            WHEN NEW.fee_basis_points != OLD.fee_basis_points
            BEGIN
                SELECT RAISE(ABORT, 'session fee terms are immutable');
            END
        """)

        # Credited deposit records are immutable. Reorganizations,
        # refunds, and corrections require separate auditable events,
        # never rewriting a previously credited deposit.
        conn.execute("""
            CREATE TRIGGER pool_deposit_no_update
            BEFORE UPDATE ON pool_deposits
            BEGIN
                SELECT RAISE(ABORT, 'credited deposit is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_deposit_no_delete
            BEFORE DELETE ON pool_deposits
            BEGIN
                SELECT RAISE(ABORT, 'credited deposit is immutable');
            END
        """)

        # Journal transactions must begin as DRAFT.
        # POSTED is only allowed through the balanced transition.
        conn.execute("""
            CREATE TRIGGER pool_journal_insert_draft_only
            BEFORE INSERT ON pool_journal_transactions
            WHEN NEW.state != 'DRAFT'
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'pool journal must begin as DRAFT'
                );
            END
        """)

        # A journal transaction cannot be posted unless it balances.
        conn.execute("""
            CREATE TRIGGER pool_journal_post_balanced
            BEFORE UPDATE OF state ON pool_journal_transactions
            WHEN NEW.state = 'POSTED'
            BEGIN
                SELECT CASE WHEN
                    COALESCE((
                        SELECT SUM(debit_sats - credit_sats)
                        FROM pool_journal_entries
                        WHERE transaction_id = NEW.id
                    ), 0) != 0
                    OR NOT EXISTS (
                        SELECT 1 FROM pool_journal_entries
                        WHERE transaction_id = NEW.id
                    )
                THEN RAISE(ABORT, 'unbalanced pool journal')
                END;
            END
        """)

        # Posted journals are immutable.
        conn.execute("""
            CREATE TRIGGER pool_journal_no_update
            BEFORE UPDATE ON pool_journal_entries
            WHEN EXISTS (
                SELECT 1 FROM pool_journal_transactions
                WHERE id = OLD.transaction_id AND state = 'POSTED'
            )
            BEGIN
                SELECT RAISE(ABORT, 'posted journal is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_journal_no_delete
            BEFORE DELETE ON pool_journal_entries
            WHEN EXISTS (
                SELECT 1 FROM pool_journal_transactions
                WHERE id = OLD.transaction_id AND state = 'POSTED'
            )
            BEGIN
                SELECT RAISE(ABORT, 'posted journal is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_journal_no_insert
            BEFORE INSERT ON pool_journal_entries
            WHEN EXISTS (
                SELECT 1 FROM pool_journal_transactions
                WHERE id = NEW.transaction_id AND state = 'POSTED'
            )
            BEGIN
                SELECT RAISE(ABORT, 'posted journal is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_journal_transaction_immutable
            BEFORE UPDATE ON pool_journal_transactions
            WHEN OLD.state = 'POSTED'
            BEGIN
                SELECT RAISE(ABORT, 'posted journal is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_journal_transaction_no_delete
            BEFORE DELETE ON pool_journal_transactions
            WHEN OLD.state = 'POSTED'
            BEGIN
                SELECT RAISE(ABORT, 'posted journal is immutable');
            END
        """)

        if version is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) "
                "VALUES('clean_shutdown', '1')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) "
                "VALUES('emergency_stop', '0')"
            )
        else:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
