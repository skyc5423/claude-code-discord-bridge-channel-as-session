"""Orchestration for Channel-as-Session message handling.

``ChannelSessionCog`` is intentionally kept thin — every non-trivial
decision lives here so it can be unit-tested without Discord mocks.

The service composes the pieces built earlier in phase 2:

    projects.json  →  ProjectsConfig
    DB layer       →  ChannelSessionRepository  (sync-or-create upsert)
    worktrees      →  ChannelWorktreeManager    (plan/ensure/remove)
    runners        →  RunnerCache               (per-project ClaudeRunner)
    topic/warn     →  TopicUpdater              (rate-limited + hysteresis)
    routing        →  SessionLookupService      (used by slash commands;
                                                 not by handle_message)

Flow (see v3 §6, §5-c, §11):

    1. cwd_mode drift detection vs DB record
    2. Worktree ensure (dedicated_worktree) or repo_root validation
    3. ``repo.ensure()`` sync-or-create
    4. Runner clone (per-message) with correct working_dir
    5. ``extra_system_prompt`` for shared_cwd_warning
    6. ``run_claude_with_config`` via the existing bridge helper
    7. Topic update + 80% warning hysteresis on completion
    8. Error counter: increment on crash, reset on success

The shared ``RunConfig`` duck-typing surface (``repo.save``,
``repo.update_context_stats``) handles DB persistence through
``EventProcessor`` — we don't re-implement it here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import discord

from ..claude.runner import ClaudeRunner
from ..claude.types import ImageData
from ..cogs._run_helper import run_claude_with_config
from ..cogs.prompt_builder import wants_file_attachment
from ..cogs.run_config import RunConfig
from ..config.projects_config import ProjectsConfig, RegisteredChannel
from ..database.channel_session_repo import ChannelSessionRepository
from ..database.repository import SessionRepository
from ..discord_ui.status import StatusManager
from .channel_worktree import ChannelWorktreeManager, EnsureResult, WorktreePaths
from .runner_cache import RunnerCache
from .session_lookup import SessionLookupService
from .topic_updater import TopicUpdater

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SHARED_CWD_WARNING_TEXT = (
    "⚠️ 이 세션의 작업 디렉터리는 다른 프로세스(크론잡, 다른 Discord 세션 등)와 "
    "공유됩니다. 파일을 수정하기 전 `git status` 로 외부 변경 여부를 확인하고, "
    "저장 후 충돌 가능성을 고려하세요."
)

CleanupReason = Literal["channel_delete", "reset_command"]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupResult:
    """Structured outcome of ``cleanup_channel``.

    ``worktree_reason`` values:
        * ``"removed"``      — worktree deleted
        * ``"dirty"``        — preserved because of uncommitted changes
        * ``"not_exists"``   — nothing to delete
        * ``"repo_root_mode"`` — no worktree by design (cwd_mode="repo_root")
        * ``"no_record"``    — DB had no record for this channel
        * any ``_classify_git_error`` output on failure
    """

    worktree_removed: bool
    worktree_reason: str
    db_deleted: bool
    runner_invalidated: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ChannelSessionService:
    """Handles message routing after the Cog has validated it."""

    def __init__(
        self,
        *,
        projects: ProjectsConfig,
        repo: ChannelSessionRepository,
        session_repo: SessionRepository,
        runner_cache: RunnerCache,
        wt_manager: ChannelWorktreeManager,
        topic_updater: TopicUpdater,
        session_lookup: SessionLookupService,
    ) -> None:
        self._projects = projects
        self._repo = repo
        self._session_repo = session_repo
        self._runner_cache = runner_cache
        self._wt = wt_manager
        self._topic = topic_updater
        self._lookup = session_lookup
        # channel_id → (active runner, asyncio.Task). Cleared in handle_message's
        # finally block and in cleanup_channel. Never mutated concurrently
        # because discord.py single-loops the cog.
        self._active: dict[int, tuple[ClaudeRunner, asyncio.Task]] = {}

    # -- Introspection ---------------------------------------------------

    def active_runner_for(self, channel_id: int) -> ClaudeRunner | None:
        """Return the in-flight runner for *channel_id*, or None.

        Used by ``ChannelSessionCog.on_message`` to decide whether to
        SIGINT the previous turn before starting a new one.
        """
        entry = self._active.get(channel_id)
        return entry[0] if entry else None

    async def await_active_task(self, channel_id: int) -> None:
        """Wait for the current turn's task to finish (no-op if none)."""
        entry = self._active.get(channel_id)
        if entry is None:
            return
        _, task = entry
        if not task.done():
            with contextlib.suppress(Exception):
                await task

    # -- Main flow -------------------------------------------------------

    async def handle_message(
        self,
        *,
        channel: discord.TextChannel,
        user_message: discord.Message,
        registered: RegisteredChannel,  # phase-2
        prompt: str,
        images: list[ImageData] | None = None,
    ) -> None:
        """Orchestrate a single user turn in a Channel-as-Session channel.

        Callers (``ChannelSessionCog``) have already:
          * Validated that this channel is registered in ``projects``.
          * Ensured the author is allowed.
          * Built ``prompt`` and ``images`` via ``build_prompt_and_images``.
          * Interrupted any previous active runner and awaited its task.

        Phase-2: the caller now passes the resolved ``RegisteredChannel``
        instead of a raw ``ProjectConfig`` — ``cwd_mode``/``slug`` come from
        the channel name (``main`` / ``wt-<slug>``), not from projects.json.
        """
        cid = channel.id
        project = registered.project
        effective_mode = registered.cwd_mode

        # 1. Drift detection ↔ last-known DB record.
        prev = await self._repo.get(cid)
        if prev is not None and prev.cwd_mode != effective_mode:
            logger.warning(
                "cwd_mode drift for channel_id=%d: db=%s → projects=%s",
                cid,
                prev.cwd_mode,
                effective_mode,
            )

        # 2. Worktree handling + working_dir decision
        worktree_path, branch_name, working_dir = await self._prepare_cwd(
            channel=channel,
            registered=registered,
        )
        if working_dir is None:
            # _prepare_cwd already surfaced the failure to the channel.
            return

        # 3. Sync-or-create DB record — projects.json fields overwrite,
        # runtime state (session_id/stats) preserved.
        record = await self._repo.ensure(
            channel_id=cid,
            project_name=project.name,
            repo_root=project.repo_root,
            worktree_path=worktree_path,
            branch_name=branch_name,
            cwd_mode=effective_mode,
            model=project.model,
            permission_mode=project.permission_mode,
            channel_name=registered.channel_name,
            category_id=registered.category_id,
        )

        # 3b. Bump the user-turn counter exactly once per message.
        # ``ChannelSessionRepository.save()`` (called many times by EventProcessor
        # per turn) does NOT increment this — it's a pure UPDATE. Keeping the
        # bump here ensures turn_count reflects real conversational turns.
        await self._repo.increment_turn(cid)

        # 4. Per-message runner clone
        base_runner = self._runner_cache.get(cid)
        if base_runner is None:
            logger.error("runner cache miss for channel_id=%d", cid)
            with contextlib.suppress(discord.HTTPException):
                await channel.send("⚠️ 내부 오류: 이 채널의 runner가 준비되지 않았습니다.")
            return

        cloned = base_runner.clone(
            thread_id=cid,  # EventProcessor uses this as the DB key
            working_dir=working_dir,
        )

        # 5. Extra system prompt (shared_cwd_warning)
        extra_system_prompt: str | None = None
        if registered.shared_cwd_warning:
            extra_system_prompt = _SHARED_CWD_WARNING_TEXT

        # 6. Emoji status reactions on the user's message.
        status = self._make_status_manager(channel, user_message, cloned.model)
        await status.set_thinking()

        # 7. Execute via the shared bridge helper.
        config = RunConfig(
            thread=channel,
            runner=cloned,
            prompt=prompt,
            session_id=record.session_id,
            repo=self._repo,  # duck-typing compatible with EventProcessor
            status=status,
            registry=None,  # NO concurrency notice for Channel-as-Session
            lounge_repo=None,
            images=images,
            attach_on_request=wants_file_attachment(prompt),
            claude_command=cloned.command,
            extra_system_prompt=extra_system_prompt,
        )

        task = asyncio.create_task(run_claude_with_config(config))
        self._active[cid] = (cloned, task)
        crashed = False
        try:
            await task
        except asyncio.CancelledError:
            raise  # don't swallow cancellation
        except Exception:
            crashed = True
            logger.exception("run_claude_with_config raised for channel_id=%d", cid)
        finally:
            self._active.pop(cid, None)

        # 8. Error counter + post-turn hooks
        if crashed:
            count = await self._repo.increment_error(cid)
            if count >= 3:
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(
                        "⚠️ 최근 연속 3회 세션이 실패했습니다. `/channel-reset` 을 권장합니다."
                    )
        else:
            await self._repo.reset_error(cid)

        record_after = await self._repo.get(cid)
        if record_after is not None:
            with contextlib.suppress(Exception):
                await self._topic.maybe_update_topic(channel, record_after)
            with contextlib.suppress(Exception):
                await self._topic.maybe_emit_warning(channel, record_after)
            with contextlib.suppress(Exception):
                await self._topic.maybe_clear_warning(record_after)

    # -- cwd preparation -------------------------------------------------

    async def _prepare_cwd(
        self,
        *,
        channel: discord.TextChannel,
        registered: RegisteredChannel,
    ) -> tuple[str | None, str | None, str | None]:
        """Return ``(worktree_path, branch_name, working_dir)``.

        Phase-2: paths derived from ``registered.slug`` (channel name), not
        from ``channel.id`` — so renaming a channel yields a fresh worktree.
        """
        project = registered.project
        effective_mode = registered.cwd_mode
        if effective_mode == "dedicated_worktree":
            assert registered.slug is not None, "dedicated_worktree requires a slug"
            paths = self._wt.plan_paths(
                project.repo_root,
                project.worktree_base,
                project.branch_prefix,
                registered.slug,
                channel_id=channel.id,
            )
            ensure_result = await asyncio.to_thread(self._wt.ensure, paths)
            if not ensure_result.ok:
                await self._report_worktree_error(channel, ensure_result)
                return (None, None, None)
            return (paths.worktree_path, paths.branch_name, paths.worktree_path)

        # repo_root mode
        working_dir = project.repo_root
        if not (Path(project.repo_root) / ".git").exists():
            logger.warning(
                "repo_root is not a git repo: %s (channel_id=%d). "
                "Session will proceed; dirty-state tracking disabled.",
                project.repo_root,
                channel.id,
            )
        return (None, None, working_dir)

    async def _report_worktree_error(
        self,
        channel: discord.TextChannel,
        result: EnsureResult,
    ) -> None:
        """Post a clear error embed explaining why the worktree failed.

        Messages are in Korean because the error is primarily operator-facing
        and this fork's users are Korean; reasons are mapped to human text.
        """
        human = _WORKTREE_REASON_TEXT.get(result.reason, f"Worktree 준비 실패: `{result.reason}`")
        embed = discord.Embed(
            title="❌ Worktree 준비 실패",
            description=human,
            color=0xED4245,
        )
        embed.add_field(
            name="Path",
            value=f"`{result.worktree_path}`",
            inline=False,
        )
        if result.planned_commands:
            joined = "\n".join(result.planned_commands)
            embed.add_field(
                name="Planned",
                value=f"```\n{joined[:950]}\n```",
                inline=False,
            )
        with contextlib.suppress(discord.HTTPException):
            await channel.send(embed=embed)

    # -- StatusManager helper (extracted for testability) ---------------

    def _make_status_manager(
        self,
        channel: discord.TextChannel,
        user_message: discord.Message,
        model: str,
    ) -> StatusManager:
        async def _notify_stall() -> None:
            threshold = status._stall_hard  # noqa: SLF001 — internal signal
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"-# ⚠️ No activity for {threshold}s — extended thinking "
                    "or context compression. Will resume automatically."
                )

        status = StatusManager(
            user_message,
            on_hard_stall=_notify_stall,
            model=model,
        )
        return status

    # -- Cleanup ---------------------------------------------------------

    async def cleanup_channel(
        self,
        channel_id: int,
        *,
        reason: CleanupReason,
    ) -> CleanupResult:
        """Tear down a channel's session state.

        Called from ``/channel-reset`` (Cog passes ``reason="reset_command"``)
        and from ``on_guild_channel_delete`` (``reason="channel_delete"``).

        Behaviour is identical in both paths — the distinction is kept in the
        log line so post-hoc analysis can tell them apart.

        Invariants:
          * Dirty worktrees are NEVER auto-removed.
          * Runner cache entry is invalidated so the next ``handle_message``
            gets a fresh template (auto-rebuilt if the channel is still in
            ``ProjectsConfig``).
        """
        # Interrupt any active turn before we touch the filesystem.
        active = self.active_runner_for(channel_id)
        if active is not None:
            with contextlib.suppress(Exception):
                await active.interrupt()
            await self.await_active_task(channel_id)

        record = await self._repo.get(channel_id)

        worktree_removed = False
        worktree_reason = "no_record"
        if record is not None:
            if (
                record.cwd_mode == "dedicated_worktree"
                and record.worktree_path
                and record.branch_name
            ):
                paths = WorktreePaths(
                    repo_root=record.repo_root,
                    worktree_path=record.worktree_path,
                    branch_name=record.branch_name,
                    channel_id=channel_id,
                )
                result = await asyncio.to_thread(self._wt.remove_if_clean, paths)
                worktree_removed = result.removed
                worktree_reason = result.reason
                if not result.removed and result.reason == "dirty":
                    logger.warning(
                        "Dirty worktree preserved (reason=%s, channel_id=%d): %s. "
                        "Commit/stash then `git worktree remove` manually.",
                        reason,
                        channel_id,
                        result.path,
                    )
            else:
                worktree_reason = "repo_root_mode"

        db_deleted = await self._repo.delete(channel_id)

        runner_invalidated = False
        if self._runner_cache.has(channel_id):
            self._runner_cache.invalidate(channel_id)
            runner_invalidated = True

        logger.info(
            "cleanup_channel(reason=%s) channel_id=%d → "
            "worktree_removed=%s (reason=%s), db_deleted=%s, runner_invalidated=%s",
            reason,
            channel_id,
            worktree_removed,
            worktree_reason,
            db_deleted,
            runner_invalidated,
        )

        return CleanupResult(
            worktree_removed=worktree_removed,
            worktree_reason=worktree_reason,
            db_deleted=db_deleted,
            runner_invalidated=runner_invalidated,
        )

    # -- Phase-2 extension point ----------------------------------------

    async def run_skill_in_channel(
        self,
        *,
        channel: discord.TextChannel,
        user_message: discord.Message | None,
        skill_name: str,
        args: str | None,
        registered: RegisteredChannel,
    ) -> None:
        """Phase-2: run ``/<skill>`` in a Channel-as-Session channel.

        Same pipeline as ``handle_message`` (ensure → increment_turn →
        runner.clone → run_claude_with_config) but the prompt is the skill
        invocation instead of free-form user text.

        When ``user_message`` is None (slash-command interaction path), a
        seed channel message is posted to anchor StatusManager reactions.
        """
        cid = channel.id
        project = registered.project
        effective_mode = registered.cwd_mode

        worktree_path, branch_name, working_dir = await self._prepare_cwd(
            channel=channel,
            registered=registered,
        )
        if working_dir is None:
            return

        record = await self._repo.ensure(
            channel_id=cid,
            project_name=project.name,
            repo_root=project.repo_root,
            worktree_path=worktree_path,
            branch_name=branch_name,
            cwd_mode=effective_mode,
            model=project.model,
            permission_mode=project.permission_mode,
            channel_name=registered.channel_name,
            category_id=registered.category_id,
        )
        await self._repo.increment_turn(cid)

        base_runner = self._runner_cache.get(cid)
        if base_runner is None:
            logger.error("runner cache miss for skill in channel_id=%d", cid)
            with contextlib.suppress(discord.HTTPException):
                await channel.send("⚠️ 내부 오류: 이 채널의 runner가 준비되지 않았습니다.")
            return
        cloned = base_runner.clone(thread_id=cid, working_dir=working_dir)

        status_anchor: discord.Message | None = user_message
        if status_anchor is None:
            try:
                status_anchor = await channel.send(f"🛠️ 스킬 `{skill_name}` 실행 중…")
            except discord.HTTPException:
                logger.warning("run_skill_in_channel: seed send failed, aborting")
                return

        skill_prompt = f"/{skill_name}" + (f" {args}" if args else "")

        extra_system_prompt: str | None = None
        if registered.shared_cwd_warning:
            extra_system_prompt = _SHARED_CWD_WARNING_TEXT

        status = self._make_status_manager(channel, status_anchor, cloned.model)
        await status.set_thinking()

        config = RunConfig(
            thread=channel,
            runner=cloned,
            prompt=skill_prompt,
            session_id=record.session_id,
            repo=self._repo,
            status=status,
            registry=None,
            lounge_repo=None,
            attach_on_request=wants_file_attachment(skill_prompt),
            claude_command=cloned.command,
            extra_system_prompt=extra_system_prompt,
        )
        task = asyncio.create_task(run_claude_with_config(config))
        self._active[cid] = (cloned, task)
        crashed = False
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            crashed = True
            logger.exception("run_skill_in_channel raised for channel_id=%d", cid)
        finally:
            self._active.pop(cid, None)

        if crashed:
            await self._repo.increment_error(cid)
        else:
            await self._repo.reset_error(cid)

        record_after = await self._repo.get(cid)
        if record_after is not None:
            with contextlib.suppress(Exception):
                await self._topic.maybe_update_topic(channel, record_after)
            with contextlib.suppress(Exception):
                await self._topic.maybe_emit_warning(channel, record_after)
            with contextlib.suppress(Exception):
                await self._topic.maybe_clear_warning(record_after)


# ---------------------------------------------------------------------------
# Static mapping of worktree-failure reasons to Korean text
# ---------------------------------------------------------------------------

_WORKTREE_REASON_TEXT: dict[str, str] = {
    "not_a_git_repo": (
        "프로젝트의 `repo_root` 가 git 레포지토리가 아닙니다. "
        "`projects.json` 의 해당 채널 설정을 확인해 주세요."
    ),
    "repo_root_does_not_exist": (
        "`repo_root` 경로가 존재하지 않습니다. 경로 오타 또는 삭제를 확인해 주세요."
    ),
    "path_occupied_not_worktree": (
        "워크트리를 생성할 경로에 다른 디렉터리가 이미 존재합니다. "
        "수동으로 정리하거나 다른 경로를 지정해 주세요."
    ),
    "branch_checked_out_elsewhere": (
        "브랜치가 다른 worktree 에서 이미 체크아웃되어 있습니다. "
        "`git worktree list` 로 확인 후 정리해 주세요."
    ),
    "invalid_branch_ref": "브랜치 이름이 유효하지 않습니다.",
    "permission_denied": "파일 시스템 권한 부족으로 worktree 를 생성할 수 없습니다.",
    "disk_full": "디스크 공간 부족으로 worktree 를 생성할 수 없습니다.",
    "git_lock_contention": (
        "git 락이 잡혀 있어 worktree 를 생성할 수 없습니다. "
        "다른 git 프로세스가 끝난 뒤 다시 시도해 주세요."
    ),
}
