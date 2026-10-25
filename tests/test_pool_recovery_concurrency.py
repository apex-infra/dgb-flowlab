"""Persistent recovery ownership across SQLite connections."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowlab import db
from flowlab import pool_recovery_persistence as recovery


class RecoveryConcurrencyTests(unittest.TestCase):

    def test_competing_connections_and_fencing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "recovery.db")

            a = db.connect(path)
            b = db.connect(path)

            try:
                db.init(a)
                db.init(b)

                start = datetime.now(timezone.utc)

                first = recovery.acquire_lease(
                    a,
                    lease_name="mixer-lab-recovery",
                    owner_token="worker-a",
                    ttl_seconds=30,
                    now=start,
                )

                self.assertIsNotNone(first)

                competing = recovery.acquire_lease(
                    b,
                    lease_name="mixer-lab-recovery",
                    owner_token="worker-b",
                    ttl_seconds=30,
                    now=start,
                )

                self.assertIsNone(competing)

                later = start + timedelta(seconds=31)

                replacement = recovery.acquire_lease(
                    b,
                    lease_name="mixer-lab-recovery",
                    owner_token="worker-b",
                    ttl_seconds=30,
                    now=later,
                )

                self.assertIsNotNone(replacement)
                self.assertGreater(
                    replacement["fencing_token"],
                    first["fencing_token"],
                )

                with self.assertRaises(PermissionError):
                    recovery.renew_lease(
                        a,
                        lease=first,
                        now=later,
                    )

                renewed = recovery.renew_lease(
                    b,
                    lease=replacement,
                    now=later,
                )

                self.assertEqual(
                    renewed["fencing_token"],
                    replacement["fencing_token"],
                )

                with self.assertRaises(PermissionError):
                    recovery.release_lease(
                        a,
                        lease=first,
                        now=later,
                    )

            finally:
                a.close()
                b.close()


    def test_schedule_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "recovery-restart.db")

            first = db.connect(path)
            try:
                db.init(first)

                first.execute(
                    "INSERT INTO pools "
                    "(id,state,reserve_wallet,fee_wallet,created_at,updated_at) "
                    "VALUES ('pool','ACTIVE','reserve','fees','t','t')"
                )
                first.execute(
                    "INSERT INTO pool_sessions "
                    "(id,pool_id,fee_basis_points,created_at) "
                    "VALUES ('session','pool',500,'t')"
                )

                retry_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=15)
                ).isoformat()

                first.execute(
                    "INSERT INTO pool_recovery_schedules "
                    "(session_id,state,consecutive_failures,"
                    "next_attempt_at,updated_at) "
                    "VALUES ('session','BACKOFF',3,?,'t')",
                    (retry_at,),
                )
                first.commit()
            finally:
                first.close()

            second = db.connect(path)
            try:
                db.init(second)

                row = second.execute(
                    "SELECT state,consecutive_failures,next_attempt_at "
                    "FROM pool_recovery_schedules "
                    "WHERE session_id='session'"
                ).fetchone()

                self.assertIsNotNone(row)
                self.assertEqual(row["state"], "BACKOFF")
                self.assertEqual(row["consecutive_failures"], 3)
                self.assertEqual(row["next_attempt_at"], retry_at)
            finally:
                second.close()


    def test_simultaneous_process_lease_contention(self):
        import multiprocessing as mp
        import queue

        def compete(path, start, results, worker_id):
            conn = None

            try:
                conn = db.connect(path)

                if not start.wait(timeout=15):
                    results.put("TIMEOUT")
                    return

                lease = recovery.acquire_lease(
                    conn,
                    lease_name="multiprocess-test",
                    owner_token=f"worker-{worker_id}",
                    ttl_seconds=60,
                )

                results.put(
                    "ACQUIRED" if lease is not None else "BLOCKED"
                )

            except Exception as error:
                results.put(f"ERROR:{type(error).__name__}")

            finally:
                if conn is not None:
                    conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "multiprocess.db")

            conn = db.connect(path)
            try:
                db.init(conn)
            finally:
                conn.close()

            context = mp.get_context("fork")
            start = context.Event()
            results = context.Queue()

            processes = [
                context.Process(
                    target=compete,
                    args=(path, start, results, i),
                )
                for i in range(2)
            ]

            try:
                for process in processes:
                    process.start()

                start.set()

                outcomes = [
                    results.get(timeout=20)
                    for _ in processes
                ]

                self.assertEqual(
                    sorted(outcomes),
                    ["ACQUIRED", "BLOCKED"],
                )

            finally:
                start.set()

                for process in processes:
                    process.join(timeout=5)

                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertTrue(
                all(process.exitcode == 0 for process in processes)
            )


if __name__ == "__main__":
    unittest.main()
