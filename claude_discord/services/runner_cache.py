"""Eager per-project ``ClaudeRunner`` cache.

One ``ClaudeRunner`` instance per channel, pre-created from
``ProjectsConfig`` at bot startup. ``ChannelSessionService`` (step 7)
consults this cache on every incoming message::

    runner = cache.get(channel_id)
    if runner is None: ...  # channel not registered
    per_msg_runner = runner.clone(working_dir=..., thread_id=channel.id)

The cache holds the *template* runner (no active subprocess) — per-message
execution is ``runner.clone(working_dir=...)`` so the cached instance is
reusable forever.

See ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §2-a.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from ..claude.runner import ClaudeRunner
from ..config.projects_config import ProjectConfig, ProjectsConfig

logger = logging.getLogger(__name__)


RunnerFactory = Callable[[ProjectConfig], ClaudeRunner]


class RunnerCacheError(RuntimeError):
    """Raised when a runner cannot be constructed for a project.

    The message identifies the offending ``channel_id`` so bootstrap
    failures point straight at the projects.json line to fix.
    """


def _default_runner_factory(project: ProjectConfig) -> ClaudeRunner:
    """Build a ``ClaudeRunner`` template from a ``ProjectConfig``.

    - ``command``  — ``$CLAUDE_COMMAND`` or ``"claude"``
    - ``model``    — project override or runner default ``"sonnet"``
    - ``permission_mode`` — project override or runner default ``"acceptEdits"``
    - ``working_dir`` is intentionally NOT set here; ``ChannelSessionService``
      injects it via ``runner.clone(working_dir=...)`` on every message.
    """
    kwargs: dict[str, object] = {
        "command": os.getenv("CLAUDE_COMMAND", "claude"),
    }
    if project.model:
        kwargs["model"] = project.model
    if project.permission_mode:
        kwargs["permission_mode"] = project.permission_mode
    return ClaudeRunner(**kwargs)  # type: ignore[arg-type]


class RunnerCache:
    """Per-channel ``ClaudeRunner`` template cache.

    Eagerly constructs one runner per project at ``__init__`` time so that
    projects.json errors (bad model name, unexpected field) surface at
    startup, never during message handling.

    Concurrency: the cache itself is not thread-safe, but Discord cogs run
    on a single asyncio event loop, so no locking is needed.

    ``invalidate()`` semantics (see step-5 spec): when a channel is reset
    we assume *no active subprocess* (caller must have awaited
    ``runner.kill()`` beforehand), then pop and rebuild from the stored
    ``ProjectConfig``. ``get()`` after ``invalidate()`` returns the fresh
    template — callers never have to handle a transient ``None``.
    """

    def __init__(
        self,
        *,
        projects: ProjectsConfig,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self._projects = projects
        self._factory: RunnerFactory = runner_factory or _default_runner_factory
        self._runners: dict[int, ClaudeRunner] = {}

        # Eager construction — fail fast on misconfiguration.
        for project in projects:
            self._runners[project.channel_id] = self._build(project)
        logger.info(
            "RunnerCache initialised: %d project(s) pre-loaded",
            len(self._runners),
        )

    # -- Internal --------------------------------------------------------

    def _build(self, project: ProjectConfig) -> ClaudeRunner:
        try:
            return self._factory(project)
        except Exception as exc:
            raise RunnerCacheError(
                f"Failed to build ClaudeRunner for channel_id={project.channel_id} "
                f"(project={project.name!r}): {type(exc).__name__}: {exc}"
            ) from exc

    # -- Public API ------------------------------------------------------

    def get(self, channel_id: int) -> ClaudeRunner | None:
        """Return the cached template runner, or ``None`` when unregistered."""
        return self._runners.get(channel_id)

    def has(self, channel_id: int) -> bool:
        return channel_id in self._runners

    def invalidate(self, channel_id: int) -> None:
        """Drop and rebuild the runner for *channel_id*.

        Preconditions:
          * The caller has ensured no active subprocess on the previous
            runner (``await runner.kill()`` if one was running). The cache
            cannot do this itself because it is synchronous.

        Post-condition:
          * If *channel_id* is still registered in ``ProjectsConfig``, a
            fresh runner replaces the old one (``get()`` returns it).
          * If *channel_id* is no longer registered, the entry is removed
            and ``get()`` returns ``None`` afterwards.
        """
        self._runners.pop(channel_id, None)
        project = self._projects.get(channel_id)
        if project is None:
            logger.info(
                "RunnerCache.invalidate: channel_id=%d no longer in projects config — "
                "entry removed, not rebuilt.",
                channel_id,
            )
            return
        self._runners[channel_id] = self._build(project)

    def reload(self, projects: ProjectsConfig) -> None:
        """Replace the entire cache from a new ``ProjectsConfig``.

        Designed for a future hot-reload path. Currently the bridge reloads
        by restarting, so ``reload`` is tested but not invoked in the hot
        path.
        """
        self._projects = projects
        self._runners.clear()
        for project in projects:
            self._runners[project.channel_id] = self._build(project)
        logger.info(
            "RunnerCache reloaded: %d project(s) pre-loaded",
            len(self._runners),
        )

    def __len__(self) -> int:
        return len(self._runners)

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self._runners
