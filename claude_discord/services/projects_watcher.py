"""projects.json mtime-polling watcher.

Runs as an asyncio background task. Every ``interval_seconds`` it ``os.stat``
the watched file; when mtime changes, loads the new config and dispatches to
a user-supplied async ``on_change`` callback.

No external dependencies (watchdog/watchfiles). projects.json edits are
low-frequency (operator actions, not machine-generated) so polling at 15s
is plenty responsive without waste.

See ``docs/CHANNEL_AS_SESSION_PHASE2.md`` §7.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable

from ..config.projects_config import ConfigError, ProjectsConfig

logger = logging.getLogger(__name__)


OnChange = Callable[[ProjectsConfig], Awaitable[None]]


class ProjectsWatcher:
    """Poll projects.json for mtime changes and dispatch reloads.

    Guarantees:
      * First poll records mtime but does NOT call ``on_change`` — setup
        already loaded the config on boot, no need to fire immediately.
      * Load failures (``ConfigError``) log a warning and keep the previous
        config in memory. The next mtime change re-attempts.
      * ``on_change`` exceptions log and continue — the loop is durable.
      * Start/stop is idempotent.
    """

    def __init__(
        self,
        path: str,
        on_change: OnChange,
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._interval = interval_seconds
        self._last_mtime: float | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin polling. No-op if already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="projects-watcher")
        logger.info(
            "ProjectsWatcher started (path=%s, interval=%.1fs)",
            self._path,
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel the polling task and await its exit."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        logger.info("ProjectsWatcher stopped")

    # -- Internals --------------------------------------------------------

    async def _current_mtime(self) -> float | None:
        """Return the file's mtime, or None on stat failure."""
        try:
            return await asyncio.to_thread(lambda: os.stat(self._path).st_mtime)
        except OSError as exc:
            logger.warning(
                "ProjectsWatcher: stat failed for %s: %s",
                self._path,
                exc,
            )
            return None

    async def _loop(self) -> None:
        # Record baseline mtime — do NOT dispatch on first observation.
        self._last_mtime = await self._current_mtime()
        while True:
            await asyncio.sleep(self._interval)
            mtime = await self._current_mtime()
            if mtime is None or mtime == self._last_mtime:
                continue
            try:
                new_cfg = await asyncio.to_thread(ProjectsConfig.load, self._path)
            except ConfigError as exc:
                logger.warning(
                    "projects.json change detected but load failed — keeping previous config: %s",
                    exc,
                )
                # Don't update last_mtime so next mtime change (possibly the
                # operator's fix) re-triggers a load attempt.
                continue
            self._last_mtime = mtime
            logger.info("projects.json reloaded; dispatching on_change")
            try:
                await self._on_change(new_cfg)
            except Exception:
                logger.exception("ProjectsWatcher on_change handler raised")
