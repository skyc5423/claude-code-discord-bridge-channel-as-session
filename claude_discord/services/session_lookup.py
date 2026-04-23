"""Routing service for session lookups.

Given a Discord channel (or thread) ID, decide which repository holds the
session metadata and return a normalised ``LookupResult`` that callers can
use without knowing which mode they're in.

This is the core of the "relax existing command gates for Channel-as-Session"
strategy — see ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §9-b.

Design notes:
    * Stateless — no internal cache. Every ``resolve()`` re-reads both repos.
    * ID-only — does not take Discord objects; callers pass the raw int.
    * Deterministic priority: Channel-as-Session registration beats thread
      lookup. A channel listed in ``projects.json`` NEVER falls through to
      ``SessionRepository`` even if the row is missing from the channel DB.
    * Channel lookup failures propagate; they do NOT silently fall through
      to thread lookup. A bug in the channel repo should surface loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..config.projects_config import ProjectsConfig
from ..database.channel_session_repo import ChannelSessionRepository
from ..database.repository import SessionRepository

logger = logging.getLogger(__name__)

LookupKind = Literal["channel", "channel_pending", "thread", "none"]


@dataclass(frozen=True)
class LookupResult:
    """Normalised lookup outcome — ``cwd_mode``-agnostic for callers.

    Callers rely on this surface in three ways:
      * ``kind`` → branch gating (e.g. "show a mode-specific error").
      * ``session_id`` → pass to ``claude --resume`` or skip.
      * ``working_dir`` → a SINGLE effective cwd path. Callers do not need
        to know whether this came from ``worktree_path`` (dedicated mode),
        ``repo_root`` (repo_root mode), or ``SessionRecord.working_dir``
        (thread mode). The service computes it.
      * ``repo`` → the matching repo, so callers that only need to update
        state (e.g. ``/clear``, ``/stop``) can do so without re-resolving.
    """

    kind: LookupKind
    session_id: str | None = None
    working_dir: str | None = None
    repo: SessionRepository | ChannelSessionRepository | None = None


class SessionLookupService:
    """Routes a Discord channel/thread ID to the correct session repository.

    Construction:

        lookup = SessionLookupService(
            projects=projects_config_or_none,
            channel_session_repo=channel_repo_or_none,
            session_repo=session_repo,  # always required
        )

    ``projects=None`` disables the Channel-as-Session branch entirely — the
    service always returns ``kind="thread"`` or ``"none"``. This is the
    behaviour when ``PROJECTS_CONFIG`` is unset.
    """

    def __init__(
        self,
        *,
        projects: ProjectsConfig | None,
        channel_session_repo: ChannelSessionRepository | None,
        session_repo: SessionRepository,
    ) -> None:
        self._projects = projects
        self._channel_repo = channel_session_repo
        self._session_repo = session_repo

        # Validate coherent configuration: channel_session_repo only makes
        # sense when projects is non-None, and vice versa. Misconfiguration
        # is logged but not fatal — we degrade gracefully.
        if (projects is None) != (channel_session_repo is None):
            logger.warning(
                "SessionLookupService: projects and channel_session_repo should "
                "be set together (projects=%s, channel_repo=%s). "
                "Channel-as-Session lookups will be disabled.",
                projects is not None,
                channel_session_repo is not None,
            )

    # -- Public API -------------------------------------------------------

    @property
    def channel_as_session_enabled(self) -> bool:
        """True iff both ``projects`` and ``channel_session_repo`` are wired."""
        return self._projects is not None and self._channel_repo is not None

    async def resolve(self, discord_channel_id: int) -> LookupResult:
        """Resolve a channel/thread ID to a ``LookupResult``.

        Priority:

        1. If ``projects.has(id)``: always resolve via ``ChannelSessionRepository``.
           - Row present + session_id set → ``kind="channel"``.
           - Row absent OR session_id NULL → ``kind="channel_pending"``.
           - Channel repo raises → exception propagates (NO thread fallback).
        2. Else: look up ``SessionRepository.get(id)``.
           - Row present → ``kind="thread"``.
           - Row absent → ``kind="none"``.

        DB errors on the thread lookup propagate too — the bot's command
        handlers are expected to turn them into user-visible errors.
        """
        # Step 1 — Channel-as-Session
        if self.channel_as_session_enabled:
            assert self._projects is not None  # narrowed by property
            assert self._channel_repo is not None
            project = self._projects.get(discord_channel_id)
            if project is not None:
                record = await self._channel_repo.get(discord_channel_id)
                if record is None or record.session_id is None:
                    # Registered but no live session yet.
                    return LookupResult(
                        kind="channel_pending",
                        session_id=None,
                        working_dir=None,
                        repo=self._channel_repo,
                    )

                working_dir = _channel_working_dir(record.cwd_mode, record)
                return LookupResult(
                    kind="channel",
                    session_id=record.session_id,
                    working_dir=working_dir,
                    repo=self._channel_repo,
                )

        # Step 2 — Thread fallback
        thread_record = await self._session_repo.get(discord_channel_id)
        if thread_record is None:
            return LookupResult(kind="none", repo=None)

        return LookupResult(
            kind="thread",
            session_id=thread_record.session_id,
            working_dir=thread_record.working_dir,
            repo=self._session_repo,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel_working_dir(cwd_mode: str, record) -> str | None:  # noqa: ANN001
    """Return the effective cwd for a ``ChannelSessionRecord``.

    ``record`` typed loosely because ChannelSessionRecord is already imported
    via the repo module; keeping it Any here avoids a noisy TYPE_CHECKING
    block.

    * ``dedicated_worktree`` → ``worktree_path`` (may be None if not yet
      created; caller decides how to surface that).
    * ``repo_root``          → ``repo_root``.
    * unknown mode (future schema drift) → ``repo_root`` as a safe default,
      with a warning log.
    """
    if cwd_mode == "dedicated_worktree":
        return record.worktree_path
    if cwd_mode == "repo_root":
        return record.repo_root
    logger.warning(
        "Unknown cwd_mode %r for channel_id=%d — falling back to repo_root",
        cwd_mode,
        record.channel_id,
    )
    return record.repo_root
