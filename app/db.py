"""SQLite access helpers.

Every request gets its own connection.  Write operations run inside
``BEGIN IMMEDIATE`` so that a playability check and the mutation it guards
(enable firmware / withdraw template) are one atomic unit, even when several
requests arrive concurrently.  WAL mode lets readers continue while a writer
holds the lock, which keeps playback available during maintenance.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/prompts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_no     TEXT    NOT NULL,
    lang          TEXT    NOT NULL CHECK (lang IN ('zh', 'en')),
    content       TEXT    NOT NULL,
    variables_json TEXT   NOT NULL DEFAULT '[]',
    status        TEXT    NOT NULL CHECK (status IN ('draft', 'published', 'withdrawn')),
    publish_seq   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    published_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_templates_pick
    ON templates (prompt_no, lang, status);

CREATE TABLE IF NOT EXISTS firmwares (
    firmware_id   TEXT    PRIMARY KEY,
    name          TEXT,
    enabled       INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    enabled_at    TEXT
);

CREATE TABLE IF NOT EXISTS firmware_prompts (
    firmware_id    TEXT NOT NULL
        REFERENCES firmwares (firmware_id) ON DELETE CASCADE,
    prompt_no      TEXT NOT NULL,
    lang           TEXT NOT NULL CHECK (lang IN ('zh', 'en')),
    variables_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (firmware_id, prompt_no)
);

CREATE INDEX IF NOT EXISTS idx_firmware_prompts_prompt
    ON firmware_prompts (prompt_no);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas we rely on."""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_schema(db_path: str) -> None:
    """Create parent directory and tables.  Idempotent."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """Serialized read-modify-write transaction."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def read_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """Consistent snapshot for the whole request."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
