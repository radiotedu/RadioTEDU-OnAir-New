import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.engine.broadcast_plan_policy import (
    materialize_song_cadence_ads,
    song_ad_progress,
)


class SongCadenceAdRetryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE queue_items (
                id INTEGER PRIMARY KEY,
                station_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                track_type TEXT
            );
            CREATE TABLE ad_break_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_test_plan_ad_dedupe
                ON ad_break_items(station_id, dedupe_key)
                WHERE dedupe_key LIKE 'broadcast-plan:%'
                  AND status IN ('pending','playing','done');
            INSERT INTO tracks (id, track_type) VALUES (1, 'music');
            """
        )
        self.plan = {
            "plan_id": 7,
            "track_id": 99,
            "priority": 10,
            "interval": 5,
            "created_at": "2020-01-01T00:00:00+00:00",
        }
        for idx in range(5):
            self.conn.execute(
                "INSERT INTO queue_items "
                "(station_id,status,track_id,finished_at) VALUES (4,'done',1,?)",
                (f"2026-09-29 10:0{idx}:00",),
            )
        self.key = "broadcast-plan:7:song:4:1"

    def tearDown(self):
        self.conn.close()

    def _insert_ad(
        self,
        status,
        cycle=1,
        plan_id=7,
        station_id=4,
        due_at="2026-09-29T00:00:00+00:00",
    ):
        key = f"broadcast-plan:{plan_id}:song:{station_id}:{cycle}"
        cur = self.conn.execute(
            "INSERT INTO ad_break_items "
            "(station_id,track_id,due_at,status,priority,dedupe_key) "
            "VALUES (?,99,?,?,10,?)",
            (station_id, due_at, status, key),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def test_failed_cycle_remains_due_and_is_requeued_once_with_backoff(self):
        self._insert_ad("failed")
        progress = song_ad_progress(self.conn, self.plan, 4)
        self.assertEqual(progress["cycle"], 1)
        self.assertTrue(progress["due"])
        self.assertEqual(progress["item_status"], "failed")

        with patch(
            "app.engine.broadcast_plan_policy.resolve_song_ad_plans",
            return_value=[self.plan],
        ):
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 1)
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 0)

        rows = self.conn.execute(
            "SELECT status,due_at FROM ad_break_items WHERE dedupe_key=? ORDER BY id",
            (self.key,),
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["failed", "pending"])
        retry_at = datetime.fromisoformat(rows[-1]["due_at"])
        self.assertGreaterEqual(
            retry_at,
            datetime.now(timezone.utc).replace(microsecond=0),
        )

    def test_failed_older_cycle_blocks_later_cycles_until_delivered(self):
        self._insert_ad("failed")
        for idx in range(5, 10):
            self.conn.execute(
                "INSERT INTO queue_items "
                "(station_id,status,track_id,finished_at) VALUES (4,'done',1,?)",
                (f"2026-09-29 10:{idx}:00",),
            )
        self.conn.commit()

        progress = song_ad_progress(self.conn, self.plan, 4)

        self.assertTrue(progress["due"])
        self.assertEqual(progress["cycle"], 1)
        self.assertEqual(progress["item_status"], "failed")

    def test_completed_cycle_advances_only_at_next_song_threshold(self):
        self._insert_ad("done")

        progress = song_ad_progress(self.conn, self.plan, 4)

        self.assertFalse(progress["due"])
        self.assertEqual(progress["cycle"], 2)
        self.assertEqual(progress["remaining_songs"], 5)

    def test_materialize_cancels_only_later_pending_cycles_and_is_idempotent(self):
        for idx in range(5, 15):
            self.conn.execute(
                "INSERT INTO queue_items "
                "(station_id,status,track_id,finished_at) VALUES (4,'done',1,?)",
                (f"2026-09-29 10:{idx:02d}:00",),
            )
        self._insert_ad("pending", cycle=1)
        self._insert_ad("failed", cycle=2)
        self._insert_ad("pending", cycle=2)
        self._insert_ad("pending", cycle=3)
        self._insert_ad("done", cycle=4)
        self._insert_ad("playing", cycle=5)
        self._insert_ad("cancelled", cycle=6)
        self._insert_ad("failed", cycle=7)
        self._insert_ad("pending", cycle=1, plan_id=8)
        self._insert_ad("pending", cycle=1, station_id=5)
        self.conn.execute(
            "INSERT INTO ad_break_items "
            "(station_id,track_id,due_at,status,priority,dedupe_key) "
            "VALUES (4,99,'2026-09-29T00:00:00+00:00','pending',10,'operator-ad')"
        )
        self.conn.commit()

        with patch(
            "app.engine.broadcast_plan_policy.resolve_song_ad_plans",
            return_value=[self.plan],
        ):
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 0)
            rows_after_first = self.conn.execute(
                "SELECT dedupe_key,status FROM ad_break_items ORDER BY id"
            ).fetchall()
            changes_after_first = self.conn.total_changes
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 0)

        self.assertEqual(self.conn.total_changes, changes_after_first)
        states = {str(row["dedupe_key"]): str(row["status"]) for row in rows_after_first}
        self.assertEqual(states[self.key], "pending")
        self.assertEqual(states["broadcast-plan:7:song:4:2"], "cancelled")
        self.assertEqual(states["broadcast-plan:7:song:4:3"], "cancelled")
        self.assertEqual(states["broadcast-plan:7:song:4:4"], "done")
        self.assertEqual(states["broadcast-plan:7:song:4:5"], "playing")
        self.assertEqual(states["broadcast-plan:7:song:4:6"], "cancelled")
        self.assertEqual(states["broadcast-plan:7:song:4:7"], "failed")
        self.assertEqual(states["broadcast-plan:8:song:4:1"], "pending")
        self.assertEqual(states["broadcast-plan:7:song:5:1"], "pending")
        self.assertEqual(states["operator-ad"], "pending")

    def test_oldest_failed_cycle_keeps_retry_history_and_cancels_later_pending(self):
        for idx in range(5, 15):
            self.conn.execute(
                "INSERT INTO queue_items "
                "(station_id,status,track_id,finished_at) VALUES (4,'done',1,?)",
                (f"2026-09-29 11:{idx:02d}:00",),
            )
        self._insert_ad("failed", cycle=1)
        self._insert_ad("pending", cycle=2)
        self._insert_ad("pending", cycle=3)
        self.conn.commit()

        with patch(
            "app.engine.broadcast_plan_policy.resolve_song_ad_plans",
            return_value=[self.plan],
        ):
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 1)

        rows = self.conn.execute(
            "SELECT dedupe_key,status FROM ad_break_items ORDER BY id"
        ).fetchall()
        states_by_key: dict[str, list[str]] = {}
        for row in rows:
            states_by_key.setdefault(str(row["dedupe_key"]), []).append(str(row["status"]))
        self.assertEqual(states_by_key[self.key], ["failed", "pending"])
        self.assertEqual(states_by_key["broadcast-plan:7:song:4:2"], ["cancelled"])
        self.assertEqual(states_by_key["broadcast-plan:7:song:4:3"], ["cancelled"])

    def test_cancelled_cycle_is_terminal_and_next_due_cycle_is_materialized(self):
        for idx in range(5, 10):
            self.conn.execute(
                "INSERT INTO queue_items "
                "(station_id,status,track_id,finished_at) VALUES (4,'done',1,?)",
                (f"2026-09-29 12:{idx:02d}:00",),
            )
        self._insert_ad("cancelled", cycle=1)
        self.conn.commit()

        progress = song_ad_progress(self.conn, self.plan, 4)
        self.assertTrue(progress["due"])
        self.assertEqual(progress["cycle"], 2)
        self.assertEqual(progress["item_status"], "")

        with patch(
            "app.engine.broadcast_plan_policy.resolve_song_ad_plans",
            return_value=[self.plan],
        ):
            self.assertEqual(materialize_song_cadence_ads(self.conn, 4), 1)

        rows = self.conn.execute(
            "SELECT dedupe_key,status FROM ad_break_items ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(str(row["dedupe_key"]), str(row["status"])) for row in rows],
            [
                ("broadcast-plan:7:song:4:1", "cancelled"),
                ("broadcast-plan:7:song:4:2", "pending"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
