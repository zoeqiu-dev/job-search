"""SQLite memory of every posting seen, so nothing is screened or paid for twice."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint   TEXT PRIMARY KEY,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    url           TEXT,
    source        TEXT,
    first_seen    TEXT,
    title_score   INTEGER,
    screen_score  INTEGER,
    screen_reason TEXT,
    blockers      TEXT,
    status        TEXT,     -- screened | matched | packet | failed
    packet_dir    TEXT
)"""


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)
        self.db.commit()

    def seen(self, fingerprint: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM jobs WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None

    def record(self, job, status: str, screen: dict | None = None, packet_dir: str = "") -> None:
        screen = screen or {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                 screen_score = COALESCE(excluded.screen_score, screen_score),
                 screen_reason = COALESCE(excluded.screen_reason, screen_reason),
                 blockers = COALESCE(excluded.blockers, blockers),
                 status = excluded.status,
                 packet_dir = CASE WHEN excluded.packet_dir != '' THEN excluded.packet_dir
                                   ELSE packet_dir END""",
            (job.fingerprint, job.title, job.company, job.location, job.url, job.source,
             now, job.extra.get("title_score"), screen.get("score"), screen.get("reason"),
             json.dumps(screen["blockers"]) if "blockers" in screen else None,
             status, packet_dir))
        self.db.commit()

    def prune(self, days: int) -> int:
        """Forget postings first seen more than `days` ago. Packets are kept:
        those are applications you may still be tracking. A pruned posting
        that's still open will come back once and be re-screened."""
        if not days:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM jobs WHERE first_seen < ? AND status != 'packet'", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.close()
