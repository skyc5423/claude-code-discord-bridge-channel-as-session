"""Repository for Channel-as-Session DB records.

Exposes both the EventProcessor duck-typing compatible surface
(``save``, ``update_context_stats``) and the Channel-as-Session specific
lifecycle methods (``ensure``, ``clear_session``, topic snapshot helpers,
error counters, etc.).

Compatibility invariant — do NOT change without also updating
``event_processor.py``::

    async def save(self, thread_id: int, session_id: str, *,
                   working_dir: str | None = None,
                   model: str | None = None,
                   origin: str = "channel",
                   summary: str | None = None) -> None

    async def update_context_stats(self, thread_id: int,
                                   context_window: int,
                                   context_used: int) -> None

The parameter name ``thread_id`` is kept for signature compatibility —
internally it maps to ``channel_id``.

See ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §§4–5 for design rationale.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

import aiosqlite

from .channel_session_models import BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)

_LOCAL_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Record dataclass (1:1 with schema columns)
# ---------------------------------------------------------------------------


@dataclass
class ChannelSessionRecord:
    """A single row from the ``channel_sessions`` table.

    Phase-2 adds ``channel_name`` and ``category_id`` for name-pattern
    auto-registration. Pre-phase-2 rows have NULL for both until the first
    message arrives and ``handle_message`` backfills them via ``ensure``.
    """

    channel_id: int
    session_id: str | None
    project_name: str
    repo_root: str
    worktree_path: str | None
    branch_name: str | None
    cwd_mode: str
    model: str | None
    permission_mode: str | None
    context_window: int | None
    context_used: int | None
    turn_count: int
    error_count: int
    warned_80pct_at: str | None
    topic_last_set_at: str | None
    topic_last_pct: int | None
    summary: str | None
    created_at: str
    last_used_at: str
    channel_name: str | None = None
    category_id: int | None = None


# ---------------------------------------------------------------------------
# Retry-on-lock decorator (single attempt, 200ms backoff)
# ---------------------------------------------------------------------------


def _retry_on_lock(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Retry a DB coroutine once if SQLite reports ``database is locked``.

    Any other ``OperationalError`` (disk full, syntax error, etc.) or any
    non-OperationalError exception propagates unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except aiosqlite.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            logger.warning(
                "ChannelSessionRepository.%s hit 'database is locked' — retrying in 200ms",
                fn.__name__,
            )
            await asyncio.sleep(0.2)
            return await fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ChannelSessionRepository:
    """CRUD + lifecycle operations for ``channel_sessions`` rows.

    Every public coroutine opens a fresh connection (no pooling, matching
    the existing ``SessionRepository`` pattern) with ``PRAGMA busy_timeout``
    to reduce lock contention.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # -- Internal: connection helpers ------------------------------------

    @staticmethod
    async def _configure(db: aiosqlite.Connection) -> None:
        """Apply per-connection pragmas and row factory."""
        await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        db.row_factory = aiosqlite.Row

    # -- Read -------------------------------------------------------------

    @_retry_on_lock
    async def get(self, channel_id: int) -> ChannelSessionRecord | None:
        """Fetch a single record by channel ID, or ``None`` if absent."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                "SELECT * FROM channel_sessions WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return ChannelSessionRecord(**dict(row))

    @_retry_on_lock
    async def list_all(self) -> list[ChannelSessionRecord]:
        """Return every row, ordered by ``last_used_at`` desc.

        Used by ``/ch-worktree-list`` and similar introspection commands.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute("SELECT * FROM channel_sessions ORDER BY last_used_at DESC")
            rows = await cursor.fetchall()
            return [ChannelSessionRecord(**dict(row)) for row in rows]

    # -- Sync-or-create (the upsert) --------------------------------------

    @_retry_on_lock
    async def ensure(
        self,
        *,
        channel_id: int,
        project_name: str,
        repo_root: str,
        worktree_path: str | None,
        branch_name: str | None,
        cwd_mode: str,
        model: str | None,
        permission_mode: str | None,
        channel_name: str | None = None,
        category_id: int | None = None,
    ) -> ChannelSessionRecord:
        """Sync-or-create a channel record.

        Semantics (NOT get-or-create):

        - **New channel**: row inserted with projects.json-derived fields
          and runtime state initialised to NULL/0.
        - **Existing channel**: projects.json-derived fields (``project_name``,
          ``repo_root``, ``worktree_path``, ``branch_name``, ``cwd_mode``,
          ``model``, ``permission_mode``) are overwritten with the incoming
          values; **runtime state is preserved**.

        Runtime state columns — ``session_id``, ``context_window``,
        ``context_used``, ``turn_count``, ``error_count``,
        ``warned_80pct_at``, ``topic_last_set_at``, ``topic_last_pct``,
        ``summary``, ``created_at`` — are intentionally omitted from the
        ``UPDATE SET`` clause below. SQLite preserves their existing values
        on conflict, which is the core of sync-or-create semantics:
        projects.json-derived fields are overwritten on every call, but
        live session state is never clobbered.

        Called from ``ChannelSessionService.run()`` on every user message so
        that projects.json edits take effect on the next turn without losing
        the live session or stats.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                """
                INSERT INTO channel_sessions (
                    channel_id, project_name, repo_root,
                    worktree_path, branch_name, cwd_mode,
                    model, permission_mode,
                    channel_name, category_id,
                    session_id, context_window, context_used,
                    turn_count, error_count, warned_80pct_at,
                    topic_last_set_at, topic_last_pct, summary,
                    created_at, last_used_at
                )
                VALUES (
                    :channel_id, :project_name, :repo_root,
                    :worktree_path, :branch_name, :cwd_mode,
                    :model, :permission_mode,
                    :channel_name, :category_id,
                    NULL, NULL, NULL,
                    0, 0, NULL,
                    NULL, NULL, NULL,
                    datetime('now', 'localtime'),
                    datetime('now', 'localtime')
                )
                -- NOTE: runtime columns (session_id, context_*, turn_count,
                -- error_count, warned_80pct_at, topic_*, summary, created_at)
                -- are omitted by design — SQLite preserves their values.
                ON CONFLICT(channel_id) DO UPDATE SET
                    project_name    = excluded.project_name,
                    repo_root       = excluded.repo_root,
                    worktree_path   = excluded.worktree_path,
                    branch_name     = excluded.branch_name,
                    cwd_mode        = excluded.cwd_mode,
                    model           = excluded.model,
                    permission_mode = excluded.permission_mode,
                    channel_name    = COALESCE(
                        excluded.channel_name, channel_sessions.channel_name
                    ),
                    category_id     = COALESCE(
                        excluded.category_id, channel_sessions.category_id
                    ),
                    last_used_at    = datetime('now', 'localtime')
                """,
                {
                    "channel_id": channel_id,
                    "project_name": project_name,
                    "repo_root": repo_root,
                    "worktree_path": worktree_path,
                    "branch_name": branch_name,
                    "cwd_mode": cwd_mode,
                    "model": model,
                    "permission_mode": permission_mode,
                    "channel_name": channel_name,
                    "category_id": category_id,
                },
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM channel_sessions WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()

        if row is None:  # extremely unlikely — upsert guarantees a row
            raise RuntimeError(f"ensure() failed to produce a row for channel_id={channel_id}")
        return ChannelSessionRecord(**dict(row))

    # -- EventProcessor compatibility surface -----------------------------

    @_retry_on_lock
    async def save(
        self,
        thread_id: int,  # actually channel_id — name kept for duck-typing
        session_id: str,
        *,
        working_dir: str | None = None,  # ignored; worktree_path set by ensure()
        model: str | None = None,  # ignored; model set by ensure()
        origin: str = "channel",  # ignored; origin is implicitly "channel"
        summary: str | None = None,
    ) -> None:
        """Persist the session_id (and optional summary) for a channel.

        This is the surface ``EventProcessor`` calls, potentially many times
        per user turn (once per ``system`` event, once on ``result``, etc.).
        It MUST be a pure UPDATE on top of a pre-existing row — callers
        counting "turns" should use ``increment_turn`` instead, which
        ``ChannelSessionService.handle_message`` invokes exactly once per
        incoming user message.

        If no row exists, logs a warning and silently no-ops rather than
        fabricating one — that would indicate a routing bug.
        """
        _ = (working_dir, model, origin)  # explicit acknowledgement of unused args

        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                """
                UPDATE channel_sessions
                   SET session_id   = ?,
                       summary      = COALESCE(?, summary),
                       last_used_at = datetime('now', 'localtime')
                 WHERE channel_id = ?
                """,
                (session_id, summary, thread_id),
            )
            await db.commit()

        if cursor.rowcount == 0:
            logger.warning(
                "ChannelSessionRepository.save: no row for channel_id=%d — "
                "ensure() was not called first?",
                thread_id,
            )

    @_retry_on_lock
    async def update_context_stats(
        self,
        thread_id: int,  # actually channel_id
        context_window: int,
        context_used: int,
    ) -> None:
        """Persist context usage after a session completes.

        Matches the ``SessionRepository.update_context_stats`` signature so
        ``EventProcessor`` can call it without knowing which repo it has.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions SET context_window = ?, context_used = ? "
                "WHERE channel_id = ?",
                (context_window, context_used, thread_id),
            )
            await db.commit()

    # -- Lifecycle --------------------------------------------------------

    @_retry_on_lock
    async def clear_session(self, channel_id: int) -> bool:
        """Reset the runtime state for a channel while preserving metadata.

        Clears ``session_id``, context stats, ``turn_count``, ``error_count``,
        ``warned_80pct_at`` but keeps ``cwd_mode``, ``worktree_path``, etc.
        Used by ``/ch-compact`` / internal error recovery — NOT by
        ``/channel-reset`` (which does a full ``delete()``).

        Returns True if a row was modified.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                """
                UPDATE channel_sessions
                   SET session_id      = NULL,
                       context_window  = NULL,
                       context_used    = NULL,
                       turn_count      = 0,
                       error_count     = 0,
                       warned_80pct_at = NULL,
                       summary         = NULL,
                       last_used_at    = datetime('now', 'localtime')
                 WHERE channel_id = ?
                """,
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    @_retry_on_lock
    async def delete(self, channel_id: int) -> bool:
        """Delete the record entirely. Used by ``/channel-reset`` after
        worktree teardown. Returns True if a row was deleted.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                "DELETE FROM channel_sessions WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    # -- Counters ---------------------------------------------------------

    @_retry_on_lock
    async def increment_turn(self, channel_id: int) -> None:
        """Bump ``turn_count`` by 1. Called by the service layer on message
        arrival (separate from ``save()`` so it can be invoked even when
        the Claude CLI fails before emitting a session_id)."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions SET turn_count = turn_count + 1, "
                "last_used_at = datetime('now', 'localtime') "
                "WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    @_retry_on_lock
    async def increment_error(self, channel_id: int) -> int:
        """Increment ``error_count`` and return the new value.

        Used by the error matrix (§11): 3 consecutive crashes → suggest
        ``/channel-reset``.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions SET error_count = error_count + 1 WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT error_count FROM channel_sessions WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
            return int(row["error_count"]) if row is not None else 0

    @_retry_on_lock
    async def reset_error(self, channel_id: int) -> None:
        """Zero ``error_count``. Called on every successful session finalise."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions SET error_count = 0 WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    # -- 80% hysteresis ---------------------------------------------------

    @_retry_on_lock
    async def mark_80pct_warned(self, channel_id: int) -> None:
        """Record that the 80% warning has been shown for the current session."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions "
                "SET warned_80pct_at = datetime('now', 'localtime') "
                "WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    @_retry_on_lock
    async def clear_80pct_warned(self, channel_id: int) -> None:
        """Clear the 80% warning flag (post ``/compact``, or hysteresis below
        ``CLEAR_THRESHOLD`` in TopicUpdater)."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions SET warned_80pct_at = NULL WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    # -- Topic snapshot helpers -------------------------------------------

    @_retry_on_lock
    async def update_topic_snapshot(
        self,
        channel_id: int,
        pct: int,
        at_iso: str,
    ) -> None:
        """Record the topic value we just wrote so ``should_update_topic``
        can compute delta/interval on the next tick."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            await db.execute(
                "UPDATE channel_sessions "
                "SET topic_last_pct = ?, topic_last_set_at = ? "
                "WHERE channel_id = ?",
                (pct, at_iso, channel_id),
            )
            await db.commit()

    @_retry_on_lock
    async def should_update_topic(
        self,
        channel_id: int,
        new_pct: int,
        min_interval_seconds: int,
        min_delta_pct: int,
    ) -> bool:
        """Read-only check combining rate-limit and delta thresholds.

        Returns True when EITHER the channel has never been updated OR
        both gates are satisfied: enough time elapsed AND the percentage
        changed by at least ``min_delta_pct``.
        """
        record = await self.get(channel_id)
        if record is None:
            return True
        if record.topic_last_set_at is None or record.topic_last_pct is None:
            return True

        try:
            last_dt = datetime.strptime(record.topic_last_set_at, _LOCAL_TIMESTAMP_FMT)
        except ValueError:
            logger.warning(
                "Malformed topic_last_set_at for channel_id=%d: %r — treating as stale",
                channel_id,
                record.topic_last_set_at,
            )
            return True

        now = datetime.now()
        elapsed = (now - last_dt).total_seconds()
        if elapsed < 0:
            # Clock skew / local time anomaly — treat as stale, don't block.
            return True
        if elapsed < min_interval_seconds:
            return False
        return abs(new_pct - record.topic_last_pct) >= min_delta_pct
