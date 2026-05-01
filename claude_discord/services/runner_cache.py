"""Per-category ``ClaudeRunner`` cache (phase-2).

Phase-1 held one runner per *channel* (keyed by channel_id).
Phase-2 holds one runner per *category* (keyed by category_id). Every
channel inside a category shares the same template runner because they
share the same repo_root / model / permission_mode. Per-message
``runner.clone(working_dir=...)`` still isolates subprocesses.

``ChannelSessionService`` consults this cache on every incoming message::

    runner = cache.get(channel_id)  # maps channel → category → runner
    if runner is None: ...

See ``docs/CHANNEL_AS_SESSION_PHASE2.md`` §1 for the category-keyed design.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..claude.runner import ClaudeRunner
from ..config.projects_config import CategoryProjectConfig, ProjectsConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP config helpers (Phase A-3)
# ---------------------------------------------------------------------------

_MCP_CONFIG_DIR = Path("/tmp/ccdb-mcp")


def build_mcp_config_for_channel(channel_id: int, api_port: int) -> Path:
    """Write a per-channel MCP config JSON and return its path.

    The file is written to ``/tmp/ccdb-mcp/<channel_id>.json`` and contains
    the SSE transport URL pointing at the bot's API server with the channel
    ID embedded as a query parameter so the broker can route the approval
    request to the right Discord channel.

    The file is NOT deleted by this function — it must remain on disk for the
    entire lifetime of the CLI subprocess that consumes it.  Callers are
    responsible for cleanup after the session ends.

    Args:
        channel_id: Discord channel ID (used as filename and URL parameter).
        api_port: The local port the API server is listening on.

    Returns:
        :class:`pathlib.Path` pointing to the written config file.
    """
    _MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _MCP_CONFIG_DIR / f"{channel_id}.json"
    # Schema: Claude CLI v2.x expects ``type`` (not ``transport``) per
    # canonical config produced by ``claude mcp add --transport sse``.
    config = {
        "mcpServers": {
            "ccdb": {
                "type": "sse",
                "url": f"http://127.0.0.1:{api_port}/mcp/sse?channel_id={channel_id}",
            }
        }
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    logger.debug("build_mcp_config_for_channel: wrote %s (port=%d)", config_path, api_port)
    return config_path


RunnerFactory = Callable[[CategoryProjectConfig], ClaudeRunner]


class RunnerCacheError(RuntimeError):
    """Raised when a runner cannot be constructed for a category.

    The message identifies the offending ``category_id`` so bootstrap
    failures point straight at the projects.json line to fix.
    """


def _default_runner_factory(project: CategoryProjectConfig) -> ClaudeRunner:
    """Build a ``ClaudeRunner`` template from a ``CategoryProjectConfig``.

    ``working_dir`` is intentionally NOT set here — ``ChannelSessionService``
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
    """Category-keyed ``ClaudeRunner`` template cache.

    Eager construction at ``__init__`` so projects.json errors surface at
    startup, never during message handling. Keys are category_id (from
    ``CategoryProjectConfig``), not channel_id.

    Lookups accept channel_id — the cache consults ``ProjectsConfig`` to
    map the channel back to its category. This keeps the caller API
    channel-scoped while the storage is category-scoped.
    """

    def __init__(
        self,
        *,
        projects: ProjectsConfig,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self._projects = projects
        self._factory: RunnerFactory = runner_factory or _default_runner_factory
        self._runners: dict[int, ClaudeRunner] = {}  # category_id → runner

        for cat_cfg in projects.categories():
            self._runners[cat_cfg.category_id] = self._build(cat_cfg)
        logger.info(
            "RunnerCache initialised: %d category-project(s) pre-loaded",
            len(self._runners),
        )

    # -- Internal --------------------------------------------------------

    def _build(self, project: CategoryProjectConfig) -> ClaudeRunner:
        try:
            return self._factory(project)
        except Exception as exc:
            raise RunnerCacheError(
                f"Failed to build ClaudeRunner for category_id={project.category_id} "
                f"(project={project.name!r}): {type(exc).__name__}: {exc}"
            ) from exc

    # -- Public API (channel-scoped) -------------------------------------

    def get(self, channel_id: int) -> ClaudeRunner | None:
        """Return the template runner for *channel_id*'s category.

        Returns ``None`` when the channel is unregistered OR when the
        category is missing from ``_runners`` (should never happen if
        eager construction succeeded).
        """
        registered = self._projects.get(channel_id)
        if registered is None:
            return None
        return self._runners.get(registered.category_id)

    def has(self, channel_id: int) -> bool:
        return self.get(channel_id) is not None

    def invalidate(self, channel_id: int) -> None:
        """Drop and rebuild the runner for *channel_id*'s category.

        Preconditions: caller must await any active subprocess kill first.
        ``/channel-reset`` invokes this so the next message gets a fresh
        template (same category → same config, but a new Python object).
        """
        registered = self._projects.get(channel_id)
        if registered is None:
            logger.info(
                "RunnerCache.invalidate: channel_id=%d is unregistered — nothing to do",
                channel_id,
            )
            return
        cat_id = registered.category_id
        self._runners.pop(cat_id, None)
        cat_cfg = self._projects.get_category(cat_id)
        if cat_cfg is None:
            logger.info(
                "RunnerCache.invalidate: category_id=%d not in projects — entry removed",
                cat_id,
            )
            return
        self._runners[cat_id] = self._build(cat_cfg)

    def reload(self, projects: ProjectsConfig) -> None:
        """Swap in a new ProjectsConfig and rebuild all runners.

        Called from the hot-reload ``on_change`` handler. All categories are
        rebuilt so any config field change takes effect immediately. Active
        subprocesses (already cloned) are unaffected — they run to completion
        on the now-orphaned pre-reload template.
        """
        self._projects = projects
        self._runners.clear()
        for cat_cfg in projects.categories():
            self._runners[cat_cfg.category_id] = self._build(cat_cfg)
        logger.info(
            "RunnerCache reloaded: %d category-project(s) pre-loaded",
            len(self._runners),
        )

    def __len__(self) -> int:
        return len(self._runners)
