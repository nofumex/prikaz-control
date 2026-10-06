from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse


def open_source_database(url: str) -> sqlite3.Connection:
    """Open prikaz-cancel-bot's SQLite database with SQLite read-only mode."""
    parsed = urlparse(url)
    if parsed.scheme not in {"sqlite", "sqlite+aiosqlite"}:
        raise RuntimeError("SOURCE_DATABASE_URL must point to the prikaz-cancel-bot SQLite database")
    raw_path = unquote(parsed.path)
    if raw_path in {"", "/:memory:"}:
        raise RuntimeError("SOURCE_DATABASE_URL must reference a persistent SQLite file")
    if raw_path.startswith("/") and not raw_path.startswith("//"):
        # SQLAlchemy sqlite:///relative.db stores the relative path with one
        # leading slash in urlparse; sqlite:////absolute.db keeps two.
        raw_path = raw_path[1:]
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise FileNotFoundError(f"Source database does not exist: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn
