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

SCHEMA_VERSION = 6

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


def validate_pool_registration_schema(conn):
    """Reject incomplete schema-v3 participant registration objects."""

    required = {
        ("table", "pool_payout_instructions"),
        ("table", "pool_registration_seals"),
        ("table", "pool_deposit_addresses"),
        ("trigger", "pool_deposit_address_ready_immutable"),
        ("trigger", "pool_deposit_address_no_delete"),
        ("trigger", "pool_deposit_address_no_insert_ready"),
        ("trigger", "pool_registration_seal_validate"),
        ("trigger", "pool_payout_no_update"),
        ("trigger", "pool_payout_no_delete"),
        ("trigger", "pool_payout_no_insert_after_seal"),
        ("trigger", "pool_registration_seal_no_update"),
        ("trigger", "pool_registration_seal_no_delete"),
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

    missing = sorted(required - objects)

    if missing:
        raise RuntimeError(
            f"incomplete Mixer Lab registration schema v3: {missing}"
        )


def validate_pool_observation_schema(conn):
    required = {
        ('table', 'pool_deposit_observations'),
        ('table', 'pool_observation_history'),
        ('trigger', 'pool_observation_history_no_update'),
        ('trigger', 'pool_observation_history_no_delete'),
        ('trigger', 'pool_observation_address_guard'),
        ('trigger', 'pool_observation_identity_immutable'),
    }
    existing = {
        (row['type'], row['name'])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table','trigger')"
        )
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(f'incomplete Mixer Lab schema v4: {missing}')



def validate_pool_verification_schema(conn):
    """Reject incomplete M4.1 verification history infrastructure."""

    required = {
        ("table", "pool_verification_events"),
        ("index", "pool_verification_events_observation_idx"),
        ("trigger", "pool_verification_events_no_update"),
        ("trigger", "pool_verification_events_no_delete"),
    }

    existing = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger')"
        )
    }

    missing = sorted(required - existing)

    if missing:
        raise RuntimeError(
            f"incomplete Mixer Lab verification schema v5: {missing}"
        )



def validate_pool_reconciliation_schema(conn):
    """Reject incomplete M4.2 reconciliation history infrastructure."""

    required = {
        ("table", "pool_reconciliation_events"),
        ("index", "pool_reconciliation_events_observation_idx"),
        ("trigger", "pool_reconciliation_events_no_update"),
        ("trigger", "pool_reconciliation_events_no_delete"),
        ("trigger", "pool_reconciliation_events_reference_guard"),
    }

    existing = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger')"
        )
    }

    missing = sorted(required - existing)

    if missing:
        raise RuntimeError(
            f"incomplete Mixer Lab schema v6: {missing}"
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
        if version not in (1, 2, 3, 4, 5, SCHEMA_VERSION):
            raise RuntimeError(
                f"database schema_version {version} != code {SCHEMA_VERSION}; "
                "refusing unsupported migration"
            )
        if version == SCHEMA_VERSION:
            validate_pool_schema(conn)
            validate_pool_registration_schema(conn)
            validate_pool_observation_schema(conn)
            validate_pool_verification_schema(conn)
            validate_pool_reconciliation_schema(conn)
            return
        if version in (2, 3, 4, 5):
            validate_pool_schema(conn)
        if version in (3, 4, 5):
            validate_pool_registration_schema(conn)
        if version in (4, 5):
            validate_pool_observation_schema(conn)
        if version == 5:
            validate_pool_verification_schema(conn)
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

        if version in (None, 1):
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


        if version in (None, 1, 2):
            # M2.1: immutable participant payout instructions.
            conn.execute("""
                CREATE TABLE pool_payout_instructions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL
                        REFERENCES pool_sessions(id),
                    position INTEGER NOT NULL
                        CHECK(position BETWEEN 1 AND 10),
                    payout_address TEXT NOT NULL,
                    allocation_bps INTEGER NOT NULL
                        CHECK(allocation_bps BETWEEN 1 AND 10000),
                    UNIQUE(session_id, position),
                    UNIQUE(session_id, payout_address)
                )
            """)

            # A registration seal can only be created when payout
            # instructions are complete and total exactly 100%.
            conn.execute("""
                CREATE TABLE pool_registration_seals (
                    session_id TEXT PRIMARY KEY
                        REFERENCES pool_sessions(id),
                    completed_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TRIGGER pool_registration_seal_validate
                BEFORE INSERT ON pool_registration_seals
                BEGIN
                    SELECT CASE WHEN
                        (
                            SELECT COUNT(*)
                            FROM pool_payout_instructions
                            WHERE session_id = NEW.session_id
                        ) NOT BETWEEN 1 AND 10
                        OR
                        (
                            SELECT COALESCE(SUM(allocation_bps), 0)
                            FROM pool_payout_instructions
                            WHERE session_id = NEW.session_id
                        ) != 10000
                    THEN RAISE(
                        ABORT,
                        'incomplete payout registration'
                    )
                    END;
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_payout_no_update
                BEFORE UPDATE ON pool_payout_instructions
                BEGIN
                    SELECT RAISE(ABORT, 'payout instruction is immutable');
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_payout_no_delete
                BEFORE DELETE ON pool_payout_instructions
                BEGIN
                    SELECT RAISE(ABORT, 'payout instruction is immutable');
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_payout_no_insert_after_seal
                BEFORE INSERT ON pool_payout_instructions
                WHEN EXISTS (
                    SELECT 1 FROM pool_registration_seals
                    WHERE session_id = NEW.session_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'registration already finalized');
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_registration_seal_no_update
                BEFORE UPDATE ON pool_registration_seals
                BEGIN
                    SELECT RAISE(ABORT, 'registration seal is immutable');
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_registration_seal_no_delete
                BEFORE DELETE ON pool_registration_seals
                BEGIN
                    SELECT RAISE(ABORT, 'registration seal is immutable');
                END
            """)


            # M2.2: persistent deposit-address request lifecycle.
            conn.execute("""
                CREATE TABLE pool_deposit_addresses (
                    session_id TEXT PRIMARY KEY
                        REFERENCES pool_registration_seals(session_id),
                    wallet TEXT NOT NULL,
                    request_label TEXT NOT NULL UNIQUE,
                    address TEXT UNIQUE,
                    state TEXT NOT NULL
                        CHECK(state IN (
                            'REQUESTING',
                            'READY',
                            'RECOVERY_REQUIRED'
                        )),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(
                        (state = 'READY' AND address IS NOT NULL)
                        OR
                        (state != 'READY' AND address IS NULL)
                    )
                )
            """)

            conn.execute("""
                CREATE TRIGGER pool_deposit_address_ready_immutable
                BEFORE UPDATE ON pool_deposit_addresses
                WHEN OLD.state = 'READY'
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'ready deposit address is immutable'
                    );
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_deposit_address_no_delete
                BEFORE DELETE ON pool_deposit_addresses
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'deposit address request is immutable'
                    );
                END
            """)

            conn.execute("""
                CREATE TRIGGER pool_deposit_address_no_insert_ready
                BEFORE INSERT ON pool_deposit_addresses
                WHEN NEW.state != 'REQUESTING'
                     OR NEW.address IS NOT NULL
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'deposit address request must start REQUESTING'
                    );
                END
            """)


        if version in (None, 1, 2, 3):
            # M3.1: Observations are separate from financial credits.
            conn.execute("CREATE TABLE pool_deposit_observations (\n id INTEGER PRIMARY KEY AUTOINCREMENT,\n pool_id TEXT NOT NULL REFERENCES pools(id),\n session_id TEXT NOT NULL REFERENCES pool_deposit_addresses(session_id),\n txid TEXT NOT NULL,\n vout INTEGER NOT NULL CHECK(vout >= 0),\n amount_sats INTEGER NOT NULL CHECK(amount_sats > 0),\n address TEXT NOT NULL,\n confirmations INTEGER NOT NULL,\n blockhash TEXT,\n state TEXT NOT NULL CHECK(state IN ('UNCONFIRMED','CONFIRMED','CONFLICTED')),\n first_seen_at TEXT NOT NULL,\n last_seen_at TEXT NOT NULL,\n UNIQUE(txid, vout),\n FOREIGN KEY(session_id, pool_id) REFERENCES pool_sessions(id, pool_id)\n)")
            conn.execute("CREATE TABLE pool_observation_history (\n id INTEGER PRIMARY KEY AUTOINCREMENT,\n observation_id INTEGER NOT NULL REFERENCES pool_deposit_observations(id),\n confirmations INTEGER NOT NULL,\n blockhash TEXT,\n state TEXT NOT NULL CHECK(state IN ('UNCONFIRMED','CONFIRMED','CONFLICTED')),\n observed_at TEXT NOT NULL\n)")
            conn.execute("CREATE TRIGGER pool_observation_history_no_update\nBEFORE UPDATE ON pool_observation_history\nBEGIN\n SELECT RAISE(ABORT, 'observation history is immutable');\nEND")
            conn.execute("CREATE TRIGGER pool_observation_history_no_delete\nBEFORE DELETE ON pool_observation_history\nBEGIN\n SELECT RAISE(ABORT, 'observation history is immutable');\nEND")


            # Enforce receiving-address ownership and immutable output identity.
            conn.execute("CREATE TRIGGER pool_observation_address_guard\nBEFORE INSERT ON pool_deposit_observations\nWHEN NOT EXISTS (\n SELECT 1 FROM pool_deposit_addresses a\n JOIN pool_sessions s ON s.id = a.session_id\n WHERE a.session_id = NEW.session_id\n AND s.pool_id = NEW.pool_id\n AND a.state = 'READY'\n AND a.address = NEW.address\n)\nBEGIN\n SELECT RAISE(ABORT, 'observation address does not belong to session');\nEND")
            conn.execute("CREATE TRIGGER pool_observation_identity_immutable\nBEFORE UPDATE ON pool_deposit_observations\nWHEN NEW.pool_id != OLD.pool_id\n OR NEW.session_id != OLD.session_id\n OR NEW.address != OLD.address\n OR NEW.txid != OLD.txid\n OR NEW.vout != OLD.vout\n OR NEW.amount_sats != OLD.amount_sats\nBEGIN\n SELECT RAISE(ABORT, 'observation identity is immutable');\nEND")


        # M4.1: Append-only verification evidence.
        if version in (None, 1, 2, 3, 4):
            conn.execute("CREATE TABLE pool_verification_events (\n id INTEGER PRIMARY KEY AUTOINCREMENT,\n observation_id INTEGER NOT NULL\n   REFERENCES pool_deposit_observations(id),\n state TEXT NOT NULL CHECK (\n   state IN ('VERIFIED', 'PENDING', 'REJECTED')\n ),\n reason TEXT NOT NULL CHECK (length(reason) > 0),\n confirmations INTEGER NOT NULL CHECK (confirmations >= 0),\n blockhash TEXT,\n verified_at TEXT NOT NULL,\n CHECK (\n   state != 'VERIFIED'\n   OR (confirmations > 0 AND blockhash IS NOT NULL)\n )\n)")
            conn.execute('CREATE INDEX pool_verification_events_observation_idx\nON pool_verification_events(observation_id, id)')
            conn.execute("CREATE TRIGGER pool_verification_events_no_update\nBEFORE UPDATE ON pool_verification_events\nBEGIN\n SELECT RAISE(ABORT, 'verification history is immutable');\nEND")
            conn.execute("CREATE TRIGGER pool_verification_events_no_delete\nBEFORE DELETE ON pool_verification_events\nBEGIN\n SELECT RAISE(ABORT, 'verification history is immutable');\nEND")

        # M4.2: Append-only historical reconciliation evidence.
        conn.execute("""
            CREATE TABLE pool_reconciliation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER NOT NULL
                    REFERENCES pool_deposit_observations(id),
                verification_event_id INTEGER
                    REFERENCES pool_verification_events(id),
                state TEXT NOT NULL CHECK (
                    state IN ('CURRENT', 'INVALIDATED', 'INDETERMINATE')
                ),
                reason TEXT NOT NULL CHECK (length(reason) > 0),
                blockhash TEXT,
                reconciled_at TEXT NOT NULL,
                CHECK (
                    state != 'CURRENT'
                    OR (
                        verification_event_id IS NOT NULL
                        AND blockhash IS NOT NULL
                    )
                )
            )
        """)

        conn.execute("""
            CREATE INDEX pool_reconciliation_events_observation_idx
            ON pool_reconciliation_events(observation_id, id)
        """)

        # A reconciliation may reference only verification evidence
        # belonging to the same deposit observation.
        conn.execute("""
            CREATE TRIGGER pool_reconciliation_events_reference_guard
            BEFORE INSERT ON pool_reconciliation_events
            WHEN NEW.verification_event_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM pool_verification_events v
                     WHERE v.id = NEW.verification_event_id
                       AND v.observation_id = NEW.observation_id
                       AND v.state = 'VERIFIED'
                       AND (
                           NEW.state != 'CURRENT'
                           OR (
                               NEW.blockhash IS NOT NULL
                               AND v.blockhash = NEW.blockhash
                           )
                       )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'reconciliation verification reference mismatch'
                );
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_reconciliation_events_no_update
            BEFORE UPDATE ON pool_reconciliation_events
            BEGIN
                SELECT RAISE(ABORT, 'reconciliation history is immutable');
            END
        """)

        conn.execute("""
            CREATE TRIGGER pool_reconciliation_events_no_delete
            BEFORE DELETE ON pool_reconciliation_events
            BEGIN
                SELECT RAISE(ABORT, 'reconciliation history is immutable');
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
