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

SCHEMA_VERSION = 1

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


def init(conn):
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        # A brand-new database has nothing running, so the first start is "clean".
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('clean_shutdown', '1')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('emergency_stop', '0')")
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema_version {row['value']} != code {SCHEMA_VERSION}; "
            "refusing to run (write a migration, don't guess)"
        )
