"""Channel topic updater + 80% context warning emitter.

Responsibilities (three per-channel concerns fused into one service so
the rate-limit + hysteresis state has a single home):

1. **Compute** the desired channel topic string from a
   ``ChannelSessionRecord`` plus a lazily-computed dirty flag.
2. **Update** ``channel.topic`` under rate-limit/delta gating enforced by
   ``ChannelSessionRepository.should_update_topic``. Discord's own limit
   (2 topic edits / 10 minutes) is respected by ``MIN_INTERVAL_SECONDS``.
3. **Emit / clear** the 80% context-usage warning with hysteresis
   (``WARN_THRESHOLD`` → mark, ``CLEAR_THRESHOLD`` → clear).

Dirty detection is *best-effort*: ``not a git repo`` collapses to
``is_dirty=None`` so the topic can display an explicit marker instead of
silently claiming a worktree is clean.

See ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §§10-c, 11.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import discord

if TYPE_CHECKING:
    from ..database.channel_session_repo import (
        ChannelSessionRecord,
        ChannelSessionRepository,
    )
    from .channel_worktree import ChannelWorktreeManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public tuning knobs (module-level constants so consumers can override if
# they fork the bridge — but defaults match the design doc).
# ---------------------------------------------------------------------------

WARN_THRESHOLD = 0.80
CLEAR_THRESHOLD = 0.65
MIN_INTERVAL_SECONDS = 300  # 5 minutes; Discord allows 2 edits / 10 min
MIN_DELTA_PCT = 5

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


TopicUpdateReason = Literal[
    "updated",
    "rate_limited",
    "no_delta",
    "api_error",
    "missing_record",
]


@dataclass(frozen=True)
class TopicUpdateResult:
    """Outcome of a single ``maybe_update_topic`` call.

    ``topic`` is only populated on ``updated=True``.
    """

    updated: bool
    reason: TopicUpdateReason
    topic: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TopicUpdater:
    """Rate-limited channel topic + 80% warning manager.

    State kept in-process (not DB): the most recent ``is_dirty`` result per
    channel, used to decide whether a dirty-state flip counts as a
    "changed value" and overrides the ``MIN_DELTA_PCT`` gate. The interval
    gate is always respected because Discord itself enforces it.

    Thread-safety: single asyncio event loop is assumed (discord.py).
    """

    def __init__(
        self,
        *,
        repo: ChannelSessionRepository,
        wt_manager: ChannelWorktreeManager,
    ) -> None:
        self._repo = repo
        self._wt = wt_manager
        # channel_id → last-computed dirty flag (bool | None)
        self._last_dirty: dict[int, bool | None] = {}

    # -- Pure helpers -----------------------------------------------------

    def compute_topic_text(
        self,
        record: ChannelSessionRecord,
        is_dirty: bool | None,
    ) -> str:
        """Build the topic string. Pure function — safe for unit tests.

        Format variants (``A | B | C``):
            "Context: {pct}%"         — or "Context: ?" when stats missing
            "Session: {sid[:8]}"      — or "Session: (none)"
        Prefixes:
            * ``is_dirty is None``                    → ``"⚠️ not a git repo | "``
            * ``is_dirty is True`` + repo_root        → ``"⚠️ repo dirty | "``
            * ``is_dirty is True`` + dedicated_worktree → ``"⚠️ worktree dirty | "``
            * clean                                   → (no prefix)
        """
        # Context segment
        if record.context_window and record.context_used:
            pct = round(record.context_used / record.context_window * 100)
            context_str = f"Context: {pct}%"
        else:
            context_str = "Context: ?"

        # Session segment
        if record.session_id:
            session_str = f"Session: {record.session_id[:8]}"
        else:
            session_str = "Session: (none)"

        # Dirty prefix
        prefix = ""
        if is_dirty is None:
            prefix = "⚠️ not a git repo | "
        elif is_dirty:
            prefix = "⚠️ repo dirty | " if record.cwd_mode == "repo_root" else "⚠️ worktree dirty | "

        return f"{prefix}{context_str} | {session_str}"

    # -- Topic update -----------------------------------------------------

    async def maybe_update_topic(
        self,
        channel: discord.TextChannel,
        record: ChannelSessionRecord,
    ) -> TopicUpdateResult:
        """Attempt a rate-limited channel topic edit.

        Returns a ``TopicUpdateResult`` describing the outcome. Never
        raises for expected conditions — Discord API errors are suppressed
        to a ``reason="api_error"`` result.
        """
        cid = record.channel_id
        pct = _compute_pct(record)
        is_dirty = await self._compute_is_dirty(record)

        # Interval + delta gate via DB snapshot.
        gate = await self._repo.should_update_topic(
            cid,
            new_pct=pct,
            min_interval_seconds=MIN_INTERVAL_SECONDS,
            min_delta_pct=MIN_DELTA_PCT,
        )

        # Dirty-state change overrides the delta gate (not the interval gate —
        # Discord enforces a real 2/10min limit we cannot bypass safely).
        dirty_changed = cid in self._last_dirty and self._last_dirty[cid] != is_dirty
        if not gate and dirty_changed:
            interval_only = await self._repo.should_update_topic(
                cid,
                new_pct=pct,
                min_interval_seconds=MIN_INTERVAL_SECONDS,
                min_delta_pct=0,  # allow any delta incl. zero
            )
            if interval_only:
                gate = True

        if not gate:
            return TopicUpdateResult(updated=False, reason="rate_limited")

        new_topic = self.compute_topic_text(record, is_dirty)
        try:
            await channel.edit(topic=new_topic)
        except discord.HTTPException as exc:
            logger.warning(
                "TopicUpdater: channel.edit failed for channel_id=%d: %s",
                cid,
                exc,
            )
            return TopicUpdateResult(updated=False, reason="api_error")

        now_iso = datetime.now().strftime(_TIMESTAMP_FMT)
        await self._repo.update_topic_snapshot(cid, pct=pct, at_iso=now_iso)
        self._last_dirty[cid] = is_dirty
        return TopicUpdateResult(updated=True, reason="updated", topic=new_topic)

    # -- 80% warning ------------------------------------------------------

    async def maybe_emit_warning(
        self,
        channel: discord.TextChannel,
        record: ChannelSessionRecord,
    ) -> bool:
        """Emit the 80% warning iff:
          * context stats available
          * ratio ≥ ``WARN_THRESHOLD``
          * ``warned_80pct_at`` not yet set for this session

        On success, marks the flag so subsequent calls no-op until
        ``maybe_clear_warning`` resets it (or the session is cleared).
        Discord send failures are suppressed with a warning log.
        """
        if not record.context_window or not record.context_used:
            return False
        ratio = record.context_used / record.context_window
        if ratio < WARN_THRESHOLD:
            return False
        if record.warned_80pct_at is not None:
            return False  # already warned for this session

        pct = round(ratio * 100)
        message = (
            f"⚠️ 컨텍스트 사용률이 {pct}%에 도달했습니다.\n"
            "긴 세션을 계속 이어가시려면 `/compact` 로 요약을 권장합니다."
        )
        try:
            await channel.send(message)
        except discord.HTTPException as exc:
            logger.warning(
                "TopicUpdater: warning send failed for channel_id=%d: %s",
                record.channel_id,
                exc,
            )
            return False
        await self._repo.mark_80pct_warned(record.channel_id)
        return True

    async def maybe_clear_warning(
        self,
        record: ChannelSessionRecord,
    ) -> bool:
        """Clear the 80% warning flag when usage drops below
        ``CLEAR_THRESHOLD`` and the flag is currently set. Returns True
        iff the flag was cleared.

        The hysteresis band (WARN − CLEAR = 15%) prevents flapping between
        "warned" and "clear" states on tiny fluctuations.
        """
        if not record.context_window or not record.context_used:
            return False
        ratio = record.context_used / record.context_window
        if ratio >= CLEAR_THRESHOLD:
            return False
        if record.warned_80pct_at is None:
            return False
        await self._repo.clear_80pct_warned(record.channel_id)
        return True

    # -- Internals --------------------------------------------------------

    async def _compute_is_dirty(
        self,
        record: ChannelSessionRecord,
    ) -> bool | None:
        """Return ``True`` (dirty), ``False`` (clean), or ``None`` (no
        working tree to inspect — e.g. ``not a git repo`` or path missing).

        Runs ``ChannelWorktreeManager.is_clean`` off the event loop via
        ``asyncio.to_thread`` since it invokes ``git status``.
        """
        path = record.worktree_path if record.cwd_mode == "dedicated_worktree" else record.repo_root

        if not path:
            return None
        # Quick presence check to distinguish "not a git repo" from "dirty":
        # is_clean() returns False for both, but here we want to surface
        # the non-repo case explicitly.
        if not (Path(path) / ".git").exists():
            return None

        with contextlib.suppress(Exception):
            clean = await asyncio.to_thread(self._wt.is_clean, path)
            return not clean
        # Unexpected exception — treat as unknown rather than crashing the
        # topic updater.
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_pct(record: ChannelSessionRecord) -> int:
    """Context-used percentage (0–100). Returns 0 when stats are missing
    — the caller still proceeds through the rate-limit gate because a pct
    shift from "unknown" to "known" is itself a meaningful update."""
    if not record.context_window or not record.context_used:
        return 0
    return round(record.context_used / record.context_window * 100)
