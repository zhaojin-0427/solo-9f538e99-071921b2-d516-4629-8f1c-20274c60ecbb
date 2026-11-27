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

-- -------------------------------------------------------------------------
-- Recording studio: 制单 (orders) and 交件 (delivery / booking)
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS recording_batches (
    batch_no     TEXT    PRIMARY KEY,
    note         TEXT,
    revision     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

-- Every 制单 POST is one immutable order revision under a batch number.
CREATE TABLE IF NOT EXISTS recording_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no    TEXT    NOT NULL
        REFERENCES recording_batches (batch_no) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT    NOT NULL,
    UNIQUE (batch_no, revision)
);

-- A merged line ("条目") of one issued order: original language/sentence are
-- frozen here and are what a later delivery is reviewed against.
CREATE TABLE IF NOT EXISTS order_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL
        REFERENCES recording_orders (id) ON DELETE CASCADE,
    entry_no     INTEGER NOT NULL,
    lang         TEXT    NOT NULL CHECK (lang IN ('zh', 'en')),
    text         TEXT    NOT NULL,
    template_id  INTEGER,
    fallback     INTEGER NOT NULL DEFAULT 0,
    recording_no TEXT,
    created_at   TEXT    NOT NULL,
    UNIQUE (order_id, entry_no),
    UNIQUE (order_id, lang, text)
);

-- Numbered samples ("样例") of an order, with the resolution snapshot taken
-- at 制单 time.  entry_id is NULL for samples listed as unresolvable.
CREATE TABLE IF NOT EXISTS order_samples (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL
        REFERENCES recording_orders (id) ON DELETE CASCADE,
    entry_id         INTEGER
        REFERENCES order_entries (id) ON DELETE CASCADE,
    ordinal          INTEGER NOT NULL,
    sample_no        TEXT    NOT NULL,
    firmware_id      TEXT    NOT NULL,
    prompt_no        TEXT    NOT NULL,
    variables_json   TEXT    NOT NULL DEFAULT '{}',
    status           TEXT    NOT NULL CHECK (status IN ('resolved', 'unresolved')),
    requested_lang   TEXT,
    used_lang        TEXT,
    fallback         INTEGER NOT NULL DEFAULT 0,
    template_id      INTEGER,
    text             TEXT,
    firmware_enabled INTEGER,
    reason_code      TEXT,
    reason_message   TEXT,
    missing_json     TEXT,
    UNIQUE (order_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_order_samples_entry
    ON order_samples (entry_id);

-- Batch-scoped recording registry: first recording number for a
-- (batch_no, lang, sentence) wins and is reused by later matching samples.
CREATE TABLE IF NOT EXISTS batch_recordings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no     TEXT    NOT NULL
        REFERENCES recording_batches (batch_no) ON DELETE CASCADE,
    lang         TEXT    NOT NULL CHECK (lang IN ('zh', 'en')),
    text         TEXT    NOT NULL,
    recording_no TEXT    NOT NULL,
    entry_id     INTEGER,
    delivery_id  INTEGER,
    created_at   TEXT    NOT NULL,
    UNIQUE (batch_no, lang, text)
);

-- Studio-wide reusable recording library: same language + same composed
-- sentence always points at the first recording number.
CREATE TABLE IF NOT EXISTS recordings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lang             TEXT    NOT NULL CHECK (lang IN ('zh', 'en')),
    text             TEXT    NOT NULL,
    recording_no     TEXT    NOT NULL,
    source_batch_no  TEXT,
    created_at       TEXT    NOT NULL,
    UNIQUE (lang, text)
);

-- Accepted (booked/reused) delivery attempts; rejected reviews return 409
-- and write nothing, matching the catalogue's refused-mutation convention.
CREATE TABLE IF NOT EXISTS deliveries (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no               TEXT    NOT NULL,
    order_id               INTEGER NOT NULL,
    entry_id               INTEGER NOT NULL,
    submitted_recording_no TEXT    NOT NULL,
    status                 TEXT    NOT NULL CHECK (status IN ('booked', 'reused')),
    effective_recording_no TEXT    NOT NULL,
    reviewed_count         INTEGER NOT NULL DEFAULT 0,
    matched_count          INTEGER NOT NULL DEFAULT 0,
    review_json            TEXT    NOT NULL DEFAULT '{}',
    created_at             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deliveries_batch
    ON deliveries (batch_no, id);
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
