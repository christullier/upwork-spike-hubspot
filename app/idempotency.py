"""Idempotency: the same request must never create a second deal.

Key = sha256(email + normalised message + time bucket). The bucket is the
signed webhook timestamp divided by the window (default one hour). To avoid a
duplicate slipping through right at a bucket boundary, lookups check both the
current and the previous bucket.

The ledger is a small SQLite table (stdlib, no extra dependency). Each key
moves through: processing -> deal_created -> completed. If a request fails
before the deal exists, its claim is released so the sender can retry. If it
fails after the deal exists (e.g. the note call), the retry resumes from the
note instead of creating a second deal.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

PROCESSING = "processing"
DEAL_CREATED = "deal_created"
COMPLETED = "completed"


def normalise_message(message: str) -> str:
    return " ".join(message.split()).lower()


def make_key(email: str, message: str, bucket: int) -> str:
    raw = f"{email.strip().lower()}\n{normalise_message(message)}\n{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()


def candidate_keys(email: str, message: str, timestamp: int, window: int) -> list[str]:
    """[current_bucket_key, previous_bucket_key]."""
    bucket = timestamp // window
    return [make_key(email, message, bucket), make_key(email, message, bucket - 1)]


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    status: str
    contact_id: str | None
    deal_id: str | None
    note_id: str | None
    classification_json: str | None
    updated_at: float


class Ledger:
    def __init__(self, path: str, stale_after_seconds: float = 120.0) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        with self._conn() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS intake (
                    key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    contact_id TEXT,
                    deal_id TEXT,
                    note_id TEXT,
                    classification TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per operation keeps this safe across
        # FastAPI's worker threads without a shared-connection lock.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def claim(self, key: str, aliases: list[str]) -> LedgerEntry | None:
        """Atomically claim ``key``. Return the existing entry if any alias is taken.

        A ``processing`` claim older than ``stale_after_seconds`` is treated as
        abandoned (e.g. the process crashed) and re-claimed.
        """
        now = time.time()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = None
                for alias in aliases:
                    row = db.execute(
                        "SELECT key, status, contact_id, deal_id, note_id, classification,"
                        " updated_at FROM intake WHERE key = ?",
                        (alias,),
                    ).fetchone()
                    if row:
                        break
                if row:
                    entry = LedgerEntry(*row)
                    stale = (
                        entry.status == PROCESSING
                        and now - entry.updated_at > self.stale_after_seconds
                    )
                    if not stale:
                        db.execute("COMMIT")
                        return entry
                    db.execute("DELETE FROM intake WHERE key = ?", (entry.key,))
                db.execute(
                    "INSERT INTO intake (key, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (key, PROCESSING, now, now),
                )
                db.execute("COMMIT")
                return None
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def mark_deal_created(
        self, key: str, contact_id: str, deal_id: str, classification_json: str
    ) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE intake SET status=?, contact_id=?, deal_id=?, classification=?,"
                " updated_at=? WHERE key=?",
                (DEAL_CREATED, contact_id, deal_id, classification_json, time.time(), key),
            )

    def complete(
        self,
        key: str,
        *,
        note_id: str | None = None,
        deal_id: str | None = None,
        contact_id: str | None = None,
        classification_json: str | None = None,
    ) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE intake SET status=?, note_id=COALESCE(?, note_id),"
                " deal_id=COALESCE(?, deal_id), contact_id=COALESCE(?, contact_id),"
                " classification=COALESCE(?, classification), updated_at=? WHERE key=?",
                (COMPLETED, note_id, deal_id, contact_id, classification_json, time.time(), key),
            )

    def release(self, key: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM intake WHERE key = ? AND status = ?", (key, PROCESSING))
