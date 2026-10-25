FlowLab M7.2 - recovery persistence development package

Contains ONLY TWO NEW FILES:
  flowlab/pool_recovery_persistence.py
  tests/test_pool_recovery_persistence.py

Source snapshot basis: flowlab-architecture-review.zip uploaded October 25, 2026.
The developer's CURRENT local flowlab/db.py has schema V7 edits newer than that snapshot.
This archive DOES NOT include or overwrite db.py, the existing supervisor,
configuration, wallets, or databases.

Features:
- SQLite atomic lease acquisition / renewal / release
- Monotonic fencing tokens
- Append-only attempt STARTED and COMPLETED/FAILED events
- Durable backoff scheduling and monitoring intervals
- Refusal to silently retry unresolved attempts
- 13 focused isolated temporary-database tests

Status: This is a development checkpoint, NOT an autonomous supervisor.
It does NOT start threads, contact RPC, migrate production databases, or handle
external payments. Lifecycle integration, complete fencing of observational
writes, expiry/crash handling, and migration regression remain to be done.

After extracting into ~/dgb-flowlab, run:
  python3 -m unittest tests.test_pool_recovery_persistence -q

The tests require local schema V7 recovery tables. The module and tests have
been checked on an isolated V7-compatible reconstruction, not on your live DB.
