"""Service layer for Channel-as-Session mode.

Pure-logic services live here so they can be unit-tested without Discord
or a live bot. Cogs in ``claude_discord/cogs/`` compose these services
into the user-facing behaviour.
"""

from __future__ import annotations

from .channel_session_service import (
    ChannelSessionService,
)
from .channel_session_service import (
    CleanupResult as ChannelCleanupResult,
)
from .channel_worktree import (
    ChannelWorktreeManager,
    EnsureResult,
    GitCommandError,
    GitResult,
    RemovalResult,
    WorktreeInfo,
    WorktreePaths,
)
from .runner_cache import RunnerCache, RunnerCacheError
from .session_lookup import LookupResult, SessionLookupService
from .topic_updater import TopicUpdater, TopicUpdateResult

__all__ = [
    "ChannelCleanupResult",
    "ChannelSessionService",
    "ChannelWorktreeManager",
    "EnsureResult",
    "GitCommandError",
    "GitResult",
    "LookupResult",
    "RemovalResult",
    "RunnerCache",
    "RunnerCacheError",
    "SessionLookupService",
    "TopicUpdateResult",
    "TopicUpdater",
    "WorktreeInfo",
    "WorktreePaths",
]
