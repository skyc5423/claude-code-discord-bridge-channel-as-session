"""SQLite schema for Channel-as-Session mode.

Separate database file from the thread-based ``sessions.db`` so the two
modes evolve independently and cannot corrupt each other's schema.

See ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §4 for the authoritative
schema definition.
"""

from __future__ import annotations

import contextlib
import logging

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_sessions (
    channel_id        INTEGER PRIMARY KEY,
    session_id        TEXT,
    project_name      TEXT NOT NULL,
    repo_root         TEXT NOT NULL,
    worktree_path     TEXT,
    branch_name       TEXT,
    cwd_mode          TEXT NOT NULL DEFAULT 'dedicated_worktree',
    model             TEXT,
    permission_mode   TEXT,
    context_window    INTEGER,
    context_used      INTEGER,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    warned_80pct_at   TEXT,
    topic_last_set_at TEXT,
    topic_last_pct    INTEGER,
    summary           TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_used_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_channel_sessions_session_id
    ON channel_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_channel_sessions_last_used
    ON channel_sessions(last_used_at);
"""

# Idempotent migrations. New entries go at the end — NEVER reorder.
# On existing DBs that already have the column/table, SQLite raises
# OperationalError ("duplicate column name" or similar) which we suppress.
# Any OTHER exception (disk full, syntax error) propagates — fail-fast.
_MIGRATIONS: list[str] = [
    # No migrations yet — schema is v3 first release.
]

# The DB busy timeout (ms). 5 seconds is generous enough for single-user
# concurrency while preventing indefinite hangs.
BUSY_TIMEOUT_MS = 5000


async def init_db(db_path: str) -> None:
    """Initialise the Channel-as-Session database.

    Applies ``SCHEMA`` (idempotent) then each migration. Only
    ``aiosqlite.OperationalError`` is suppressed during migration
    replays — every other error propagates so real problems (disk
    full, permission denied, SQL syntax errors) are surfaced at
    boot time.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        await db.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute(stmt)
        await db.commit()
    logger.info("Channel-as-Session DB initialised at %s", db_path)
