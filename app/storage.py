from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    """Private reporting state; never opens or migrates the customer bot DB."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS report_snapshots(
                report_date TEXT PRIMARY KEY, timezone TEXT NOT NULL,
                raw_json TEXT NOT NULL, built_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_deliveries(
                report_date TEXT NOT NULL, chat_id TEXT NOT NULL,
                sent_at INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'sent',
                PRIMARY KEY(report_date, chat_id)
            );
            CREATE TABLE IF NOT EXISTS telegram_state(
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(report_deliveries)")}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE report_deliveries ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'")
        self.conn.commit()

    def save_report(self, report: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO report_snapshots VALUES(?,?,?,?) ON CONFLICT(report_date) DO UPDATE SET timezone=excluded.timezone, raw_json=excluded.raw_json, built_at=excluded.built_at",
            (report["date"], report["timezone"], json.dumps(report, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()

    def report(self, day: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT raw_json FROM report_snapshots WHERE report_date=?", (day,)).fetchone()
        return json.loads(row["raw_json"]) if row else None

    def mark_sent(self, day: str, chat_id: int) -> None:
        self.conn.execute(
            "UPDATE report_deliveries SET sent_at=?, status='sent' WHERE report_date=? AND chat_id=?",
            (int(time.time()), day, str(chat_id)),
        )
        self.conn.commit()

    def claim_delivery(self, day: str, chat_id: int) -> bool:
        # Atomically reserve the unique day/chat pair before contacting Telegram.
        # An uncertain network result stays claimed, avoiding duplicate reports.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO report_deliveries(report_date, chat_id, sent_at, status) VALUES(?,?,0,'claimed')",
                (day, str(chat_id)),
            )
            self.conn.commit()
            return cursor.rowcount == 1
        except Exception:
            self.conn.rollback()
            raise

    def was_sent(self, day: str, chat_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM report_deliveries WHERE report_date=? AND chat_id=? AND status='sent'", (day, str(chat_id))).fetchone() is not None

    def telegram_offset(self) -> int:
        row = self.conn.execute("SELECT value FROM telegram_state WHERE key='update_offset'").fetchone()
        return int(row["value"]) if row else 0

    def set_telegram_offset(self, value: int) -> None:
        self.conn.execute(
            "INSERT INTO telegram_state(key,value) VALUES('update_offset',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(value),),
        )
        self.conn.commit()
