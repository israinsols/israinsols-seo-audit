"""SQLite persistence and run logging."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    cluster TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'queued', 'used', 'rejected')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER REFERENCES topics(id),
    wp_post_id INTEGER,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'reviewed', 'published')),
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    published_at TEXT,
    last_refreshed_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    query TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    ctr REAL NOT NULL DEFAULT 0,
    position REAL NOT NULL DEFAULT 0,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY,
    article_id INTEGER REFERENCES articles(id),
    platform TEXT NOT NULL,
    external_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    UNIQUE (article_id, platform)
);
CREATE TABLE IF NOT EXISTS refresh_queue (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    topic_id INTEGER REFERENCES topics(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
"""


def connect(database_path: Path | str = "data/engine.sqlite3") -> sqlite3.Connection:
    """Open a database connection and ensure the schema exists."""
    path = Path(database_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    refresh_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(refresh_queue)")
    }
    if "topic_id" not in refresh_columns:
        connection.execute("ALTER TABLE refresh_queue ADD COLUMN topic_id INTEGER REFERENCES topics(id)")
    duplicates = connection.execute(
        """SELECT date, url, query, MIN(id) AS keep_id
           FROM metrics GROUP BY date, url, query HAVING COUNT(*) > 1"""
    ).fetchall()
    for duplicate in duplicates:
        connection.execute(
            """DELETE FROM metrics
               WHERE date = ? AND url = ? AND query = ? AND id != ?""",
            (duplicate["date"], duplicate["url"], duplicate["query"], duplicate["keep_id"]),
        )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS metrics_date_url_query_uq
           ON metrics (date, url, query)"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS refresh_queue_topic_active_uq
           ON refresh_queue (topic_id)
           WHERE topic_id IS NOT NULL AND status IN ('queued', 'brief_created')"""
    )
    connection.commit()
    return connection


def record_run(
    connection: sqlite3.Connection,
    step: str,
    status: str,
    message: str = "",
) -> int:
    """Record a step result with a UTC timestamp."""
    cursor = connection.execute(
        "INSERT INTO runs (started_at, step, status, message) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), step, status, message),
    )
    connection.commit()
    return int(cursor.lastrowid)
