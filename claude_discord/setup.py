"""One-call setup for all ccdb bridge Cogs.

Consumers call this instead of manually wiring each Cog.
New Cogs added to ccdb are automatically included — no consumer code changes needed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .claude.runner import ClaudeRunner
    from .config.projects_config import ProjectsConfig
    from .database.channel_session_repo import ChannelSessionRepository
    from .database.lounge_repo import LoungeRepository
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.task_repo import TaskRepository
    from .ext.api_server import ApiServer

logger = logging.getLogger(__name__)


@dataclass
class BridgeComponents:
    """References to initialized bridge components.

    After calling setup_bridge(), pass this to apply_to_api_server() so the
    ApiServer gains access to all repos without manual wiring::

        components = await setup_bridge(bot, runner, api_server=api_server)

    Or manually if you need more control::

        components = await setup_bridge(bot, runner)
        components.apply_to_api_server(api_server)
    """

    session_repo: SessionRepository
    task_repo: TaskRepository | None = None
    lounge_repo: LoungeRepository | None = None
    resume_repo: PendingResumeRepository | None = None
    # Channel-as-Session (phase-2) — populated only when PROJECTS_CONFIG is set.
    channel_session_repo: ChannelSessionRepository | None = None
    projects_config: ProjectsConfig | None = None

    def apply_to_api_server(self, api_server: ApiServer) -> None:
        """Wire all optional repos to an ApiServer instance.

        Idempotent — safe to call multiple times.  Only non-None repos are
        applied, so repos that are disabled (e.g. scheduler off) are left as-is.

        When a new repo is added to BridgeComponents in the future, add it here
        and consumers automatically pick it up without changing their own code.
        """
        if self.task_repo is not None:
            api_server.task_repo = self.task_repo
        if self.lounge_repo is not None:
            api_server.lounge_repo = self.lounge_repo
        if self.resume_repo is not None:
            api_server.resume_repo = self.resume_repo
        api_server.session_repo = self.session_repo


async def setup_bridge(
    bot: Bot,
    runner: ClaudeRunner,
    *,
    api_server: ApiServer | None = None,
    session_db_path: str = "data/sessions.db",
    allowed_user_ids: set[int] | None = None,
    claude_channel_id: int | None = None,
    claude_channel_ids: set[int] | None = None,
    mention_only_channel_ids: set[int] | None = None,
    inline_reply_channel_ids: set[int] | None = None,
    chat_only_channel_ids: set[int] | None = None,
    cli_sessions_path: str | None = None,
    enable_scheduler: bool = True,
    task_db_path: str = "data/tasks.db",
    lounge_channel_id: int | None = None,
    max_concurrent: int | None = None,
    worktree_base_dir: str | None = None,
    enable_thread_inbox: bool = False,
    auto_rename_threads: bool | None = None,
    monitor_all_channels: bool | None = None,
    projects_config_path: str | None = None,
    channel_session_db_path: str | None = None,
) -> BridgeComponents:
    """Initialize and register all ccdb Cogs in one call.

    This is the recommended way for consumers to set up ccdb.
    New Cogs added to ccdb will be automatically included.

    Pass ``api_server`` to automatically wire all repos and set the runner's
    ``api_port`` — consumers then need zero manual wiring::

        components = await setup_bridge(bot, runner, api_server=api_server, ...)
        # Done — no manual repo wiring needed.

    Args:
        bot: Discord bot instance.
        runner: ClaudeRunner for Claude CLI invocation.
        api_server: Optional ApiServer to auto-wire repos into.  Also sets
                    runner.api_port so CCDB_API_URL is available to Claude.
        session_db_path: Path for session SQLite DB.
        allowed_user_ids: Set of Discord user IDs allowed to use Claude.
        claude_channel_id: Primary channel ID for Claude chat.  Kept for
                           backward compatibility.  Also used as the fallback
                           thread-creation target in SkillCommandCog.
        claude_channel_ids: Additional channel IDs to listen on.  Combined with
                            ``claude_channel_id`` to form the full set.  Use this
                            to deploy the bot in multiple channels.
        mention_only_channel_ids: Channel IDs where the bot only responds when
                                  explicitly @mentioned.  Thread replies are not
                                  affected (they are already within an active session).
                                  Defaults to MENTION_ONLY_CHANNEL_IDS env var
                                  (comma-separated).
        chat_only_channel_ids: Channel IDs where only text responses are shown.
                               Tool embeds, thinking blocks, and session chrome are
                               hidden.  Useful for public channels where non-technical
                               users are watching.  Defaults to CHAT_ONLY_CHANNEL_IDS
                               env var (comma-separated).
        cli_sessions_path: Path to ~/.claude/projects for session sync.
        enable_scheduler: Whether to enable SchedulerCog.
        task_db_path: Path for scheduled tasks SQLite DB.
        lounge_channel_id: Discord channel ID for AI Lounge messages.
                           Defaults to COORDINATION_CHANNEL_ID env var.
        worktree_base_dir: Base directory to scan for session worktrees
                           (e.g. ``/home/user``). When set, a WorktreeManager
                           is created and attached to the bot, enabling automatic
                           cleanup of session worktrees at session end and startup.
                           Defaults to WORKTREE_BASE_DIR env var, or None (disabled).
        auto_rename_threads: When True, rename each new thread with a Claude-generated
                             title derived from the first user message.  Runs as a
                             background task so it never delays the session start.
                             Defaults to THREAD_AUTO_RENAME env var (off by default).

    Returns:
        BridgeComponents with references to initialized repositories.
    """
    from .cogs.channel_session import ChannelSessionCog
    from .cogs.claude_chat import ClaudeChatCog
    from .cogs.scheduler import SchedulerCog
    from .cogs.session_manage import SessionManageCog
    from .cogs.skill_command import SkillCommandCog
    from .config.projects_config import ProjectsConfig
    from .database.ask_repo import PendingAskRepository
    from .database.channel_session_models import init_db as init_channel_db
    from .database.channel_session_repo import ChannelSessionRepository
    from .database.inbox_repo import ThreadInboxRepository
    from .database.lounge_repo import LoungeRepository
    from .database.models import init_db
    from .database.repository import SessionRepository, UsageStatsRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.settings_repo import SettingsRepository
    from .database.task_repo import TaskRepository
    from .services.channel_session_service import ChannelSessionService
    from .services.channel_worktree import ChannelWorktreeManager
    from .services.runner_cache import RunnerCache
    from .services.session_lookup import SessionLookupService
    from .services.topic_updater import TopicUpdater
    from .worktree import WorktreeManager

    # Build the full set of claude channel IDs from both parameters
    _all_channel_ids: set[int] = set()
    if claude_channel_id is not None:
        _all_channel_ids.add(claude_channel_id)
    if claude_channel_ids is not None:
        _all_channel_ids.update(claude_channel_ids)

    # -------- Channel-as-Session config load (phase-2) ----------
    # Load projects.json early so channel IDs can be excluded from the thread-
    # mode cog before it registers its listener. fail-fast — any schema error
    # aborts bot startup with a precise message.
    if projects_config_path is None:
        projects_config_path = os.getenv("PROJECTS_CONFIG")
    projects_config: ProjectsConfig | None = None
    _pj_channel_ids: set[int] = set()
    if projects_config_path:
        projects_config = ProjectsConfig.load(projects_config_path)
        _pj_channel_ids = projects_config.channel_ids()
        logger.info(
            "Channel-as-Session enabled: %d project(s) from %s",
            len(projects_config),
            projects_config_path,
        )
        # Hybrid-A: remove PJ channels from the thread-mode cog's channel set.
        # Any overlap is intentional on the operator's part; we log it.
        overlap = _all_channel_ids & _pj_channel_ids
        if overlap:
            logger.info(
                "Channel overlap removed from thread-mode cog (PJ wins): %s",
                sorted(overlap),
            )
            _all_channel_ids -= _pj_channel_ids

    # Mention-only channels — fall back to MENTION_ONLY_CHANNEL_IDS env var
    if mention_only_channel_ids is None:
        _env_mention = os.getenv("MENTION_ONLY_CHANNEL_IDS", "")
        mention_only_channel_ids = {
            int(x.strip()) for x in _env_mention.split(",") if x.strip().isdigit()
        } or None

    # Inline-reply channels — fall back to INLINE_REPLY_CHANNEL_IDS env var
    if inline_reply_channel_ids is None:
        _env_inline = os.getenv("INLINE_REPLY_CHANNEL_IDS", "")
        inline_reply_channel_ids = {
            int(x.strip()) for x in _env_inline.split(",") if x.strip().isdigit()
        } or None

    # Chat-only channels — fall back to CHAT_ONLY_CHANNEL_IDS env var
    if chat_only_channel_ids is None:
        _env_chat_only = os.getenv("CHAT_ONLY_CHANNEL_IDS", "")
        chat_only_channel_ids = {
            int(x.strip()) for x in _env_chat_only.split(",") if x.strip().isdigit()
        } or None

    # Lounge channel — fall back to COORDINATION_CHANNEL_ID env var for backward compat
    if lounge_channel_id is None:
        ch_str = os.getenv("COORDINATION_CHANNEL_ID", "")
        lounge_channel_id = int(ch_str) if ch_str.isdigit() else None

    # Thread auto-rename — fall back to THREAD_AUTO_RENAME env var (off by default)
    if auto_rename_threads is None:
        auto_rename_threads = os.getenv("THREAD_AUTO_RENAME", "").lower() in (
            "true",
            "1",
            "yes",
        )
    if auto_rename_threads:
        logger.info("Thread auto-rename enabled (THREAD_AUTO_RENAME)")

    # Monitor-all-channels — fall back to CLAUDE_MONITOR_ALL_CHANNELS env var
    if monitor_all_channels is None:
        monitor_all_channels = os.getenv("CLAUDE_MONITOR_ALL_CHANNELS", "").lower() in (
            "true",
            "1",
            "yes",
        )
    if monitor_all_channels:
        logger.info("Monitor-all-channels enabled — bot will respond in ANY guild channel")

    # Max concurrent sessions — fall back to MAX_CONCURRENT_SESSIONS env var, then 3
    if max_concurrent is None:
        _env_max = os.getenv("MAX_CONCURRENT_SESSIONS", "")
        max_concurrent = int(_env_max) if _env_max.isdigit() else 3
    if max_concurrent != 3:
        logger.info("Max concurrent sessions: %d", max_concurrent)

    from .cogs._run_helper import configure_session_limit

    configure_session_limit(max_concurrent)

    # WorktreeManager — attach to bot so cogs can access it via bot.worktree_manager
    if worktree_base_dir is None:
        worktree_base_dir = os.getenv("WORKTREE_BASE_DIR")
    if worktree_base_dir is not None:
        if not hasattr(bot, "worktree_manager"):
            bot.worktree_manager = WorktreeManager(base_dir=worktree_base_dir)  # type: ignore[attr-defined]
        logger.info("WorktreeManager enabled (base_dir=%s)", worktree_base_dir)

    # --- Session DB (also hosts lounge_messages and pending_resumes tables) ---
    os.makedirs(os.path.dirname(session_db_path) or ".", exist_ok=True)
    await init_db(session_db_path)
    session_repo = SessionRepository(session_db_path)
    settings_repo = SettingsRepository(session_db_path)
    ask_repo = PendingAskRepository(session_db_path)
    lounge_repo = LoungeRepository(session_db_path)
    resume_repo = PendingResumeRepository(session_db_path)
    usage_repo = UsageStatsRepository(session_db_path)
    logger.info("Session DB initialized: %s", session_db_path)

    # Attach repos to bot so generic cogs (e.g. AutoUpgradeCog) can discover them
    # without a hard import dependency on ccdb internals.
    bot.session_repo = session_repo  # type: ignore[attr-defined]
    bot.resume_repo = resume_repo  # type: ignore[attr-defined]

    # --- Thread inbox (optional — THREAD_INBOX_ENABLED=true) ---
    if enable_thread_inbox:
        inbox_repo = ThreadInboxRepository(session_db_path)
        bot.inbox_repo = inbox_repo  # type: ignore[attr-defined]
        logger.info("Thread inbox enabled")

    # --- ClaudeChatCog ---
    chat_cog = ClaudeChatCog(
        bot,  # type: ignore[arg-type]  # consumers pass their own Bot subclass
        repo=session_repo,
        runner=runner,
        max_concurrent=max_concurrent,
        allowed_user_ids=allowed_user_ids,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
        channel_ids=_all_channel_ids or None,
        mention_only_channel_ids=mention_only_channel_ids or None,
        inline_reply_channel_ids=inline_reply_channel_ids or None,
        chat_only_channel_ids=chat_only_channel_ids or None,
        auto_rename_threads=auto_rename_threads,
        monitor_all_channels=monitor_all_channels,
        excluded_channel_ids=_pj_channel_ids or None,
    )
    await bot.add_cog(chat_cog)
    logger.info("Registered ClaudeChatCog")

    # --- SessionManageCog ---
    session_manage_cog = SessionManageCog(
        bot,  # type: ignore[arg-type]  # consumers pass their own Bot subclass
        repo=session_repo,
        cli_sessions_path=cli_sessions_path,
        settings_repo=settings_repo,
        usage_repo=usage_repo,
    )
    await bot.add_cog(session_manage_cog)
    logger.info("Registered SessionManageCog")

    # --- SkillCommandCog (requires at least one channel ID) ---
    if _all_channel_ids:
        # Primary channel: prefer the explicit claude_channel_id, else pick from set
        _primary_channel_id = claude_channel_id or next(iter(_all_channel_ids))
        skill_cog = SkillCommandCog(
            bot,
            repo=session_repo,
            runner=runner,
            claude_channel_id=_primary_channel_id,
            claude_channel_ids=_all_channel_ids,
            allowed_user_ids=allowed_user_ids,
        )
        await bot.add_cog(skill_cog)
        logger.info("Registered SkillCommandCog")

    # --- SchedulerCog (optional) ---
    task_repo: TaskRepository | None = None
    if enable_scheduler:
        os.makedirs(os.path.dirname(task_db_path) or ".", exist_ok=True)
        task_repo = TaskRepository(task_db_path)
        await task_repo.init_db()
        scheduler_cog = SchedulerCog(bot, runner, repo=task_repo, session_repo=session_repo)
        await bot.add_cog(scheduler_cog)
        logger.info("Registered SchedulerCog")

    # -------- ChannelSessionCog (phase-2, only when projects.json is set) --------
    channel_session_repo: ChannelSessionRepository | None = None
    if projects_config is not None:
        if channel_session_db_path is None:
            channel_session_db_path = os.getenv("CHANNEL_SESSION_DB", "data/channel_sessions.db")
        os.makedirs(os.path.dirname(channel_session_db_path) or ".", exist_ok=True)
        await init_channel_db(channel_session_db_path)
        channel_session_repo = ChannelSessionRepository(channel_session_db_path)

        channel_wt_manager = ChannelWorktreeManager()
        runner_cache = RunnerCache(projects=projects_config)
        topic_updater = TopicUpdater(repo=channel_session_repo, wt_manager=channel_wt_manager)
        session_lookup = SessionLookupService(
            projects=projects_config,
            channel_session_repo=channel_session_repo,
            session_repo=session_repo,
        )
        channel_service = ChannelSessionService(
            projects=projects_config,
            repo=channel_session_repo,
            session_repo=session_repo,
            runner_cache=runner_cache,
            wt_manager=channel_wt_manager,
            topic_updater=topic_updater,
            session_lookup=session_lookup,
        )
        channel_cog = ChannelSessionCog(
            bot,  # type: ignore[arg-type]
            service=channel_service,
            projects=projects_config,
            allowed_user_ids=allowed_user_ids,
        )
        await bot.add_cog(channel_cog)
        logger.info("Registered ChannelSessionCog (%d project(s))", len(projects_config))

        # Expose on bot for diagnostics / external access.
        bot.channel_session_repo = channel_session_repo  # type: ignore[attr-defined]
        bot.channel_session_service = channel_service  # type: ignore[attr-defined]
        bot.channel_wt_manager = channel_wt_manager  # type: ignore[attr-defined]
        bot.session_lookup = session_lookup  # type: ignore[attr-defined]

        # Step-9 rewire: inject Channel-as-Session awareness into ClaudeChatCog
        # so relaxed slash commands (/stop, /compact, /clear, /rewind, /fork)
        # can detect and dispatch to the channel-mode service. This is done
        # post-construction because the service is created after the cog.
        chat_cog._projects = projects_config
        chat_cog._channel_session_service = channel_service
        chat_cog._session_lookup = session_lookup

        # Step-10 rewire: SessionManageCog needs session_lookup + channel repos
        # for /context (dirty flag, worktree path) and /resume-info (channel
        # session_id). Same post-inject pattern as ClaudeChatCog above.
        session_manage_cog._session_lookup = session_lookup
        session_manage_cog._channel_session_repo = channel_session_repo
        session_manage_cog._channel_wt_manager = channel_wt_manager

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        channel_session_repo=channel_session_repo,
        projects_config=projects_config,
    )

    # Auto-wire repos to ApiServer and set runner.api_port if provided
    if api_server is not None:
        components.apply_to_api_server(api_server)
        if runner.api_port is None:
            runner.api_port = api_server.port
        logger.info("Auto-wired repos to ApiServer (port=%d)", api_server.port)

    return components
