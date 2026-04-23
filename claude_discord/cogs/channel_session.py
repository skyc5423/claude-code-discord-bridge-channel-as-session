"""Discord cog for Channel-as-Session mode.

Thin event adapter — all real logic lives in
``ChannelSessionService``. This cog:

1. Routes ``on_message`` to the service when the channel is a registered
   Channel-as-Session target.
2. Handles ``on_guild_channel_delete`` → ``cleanup_channel(reason="channel_delete")``.
3. Registers three slash commands:
   * ``/channel-reset``      — user-confirmed reset (see v3 §8)
   * ``/ch-worktree-list``   — dedicated_worktree channels + dirty state
   * ``/ch-worktree-cleanup``— bulk clean-up of orphan dedicated worktrees

Commands use ``_is_session_channel`` to gate access — only registered
channels are allowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..config.projects_config import ProjectsConfig
from ..services.channel_session_service import ChannelSessionService

if TYPE_CHECKING:
    from ..bot import ClaudeDiscordBot

logger = logging.getLogger(__name__)

_CONFIRM_TIMEOUT_SECONDS = 60
_CONFIRM_YES = "✅"
_CONFIRM_NO = "❌"

_COLOR_INFO = 0x5865F2
_COLOR_SUCCESS = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_ERROR = 0xED4245


class ChannelSessionCog(commands.Cog):
    """Event adapter + slash commands for Channel-as-Session mode."""

    def __init__(
        self,
        bot: ClaudeDiscordBot,
        *,
        service: ChannelSessionService,
        projects: ProjectsConfig,
        allowed_user_ids: set[int] | None = None,
    ) -> None:
        self.bot = bot
        self._service = service
        self._projects = projects
        self._allowed_user_ids = allowed_user_ids

    # -- Event listeners --------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Route Channel-as-Session-eligible messages to the service."""
        # Ignore bot authors and system-type messages.
        if message.author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return
        # Only TextChannels are candidates — threads/DMs go to ClaudeChatCog.
        if not isinstance(message.channel, discord.TextChannel):
            return

        # Authorization — if allowed_user_ids is set, enforce; otherwise
        # channel-level Discord perms are the gate.
        if self._allowed_user_ids and message.author.id not in self._allowed_user_ids:
            return

        # Scope: only projects.json-registered channels are ours.
        registered = self._projects.get(message.channel.id)
        if registered is None:
            return

        # Prompt & images
        from .prompt_builder import build_prompt_and_images

        save_dir: str | None = None
        if message.attachments:
            save_dir = os.path.join(tempfile.gettempdir(), "ccdb-uploads", str(message.id))
            os.makedirs(save_dir, exist_ok=True)
        prompt, images = await build_prompt_and_images(message, save_dir=save_dir)
        if not prompt and not images:
            return

        # SIGINT-replace: if the channel already has an in-flight turn,
        # interrupt it and wait for cleanup before starting a new turn.
        active = self._service.active_runner_for(message.channel.id)
        if active is not None:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("🔁")
            await active.interrupt()
            await self._service.await_active_task(message.channel.id)

        await self._service.handle_message(
            channel=message.channel,
            user_message=message,
            registered=registered,
            prompt=prompt,
            images=images,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self,
        channel: discord.abc.GuildChannel,
    ) -> None:
        """Tear down state when a registered channel is deleted."""
        if not isinstance(channel, discord.TextChannel):
            return
        if not self._projects.has(channel.id):
            return

        logger.info("Registered channel deleted (id=%d) — running cleanup", channel.id)
        result = await self._service.cleanup_channel(channel.id, reason="channel_delete")
        logger.info("on_guild_channel_delete cleanup: %s", result)

    # -- Helpers ----------------------------------------------------------

    def _is_session_channel(self, channel_id: int) -> bool:
        return self._projects.has(channel_id)

    async def _reject_non_session_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "❌ This command can only be used in a Channel-as-Session channel "
            "(channels registered in `projects.json`).",
            ephemeral=True,
        )

    # -- Slash command: /channel-reset -----------------------------------

    @app_commands.command(
        name="channel-reset",
        description="Reset the Channel-as-Session state for this channel (with dirty check)",
    )
    async def channel_reset(self, interaction: discord.Interaction) -> None:
        """Interactive reset flow — confirm, then delegate to service.

        See v3 §8 for the full decision tree (cwd_mode branching, dirty
        preservation, etc.).
        """
        if not isinstance(interaction.channel, discord.TextChannel):
            await self._reject_non_session_channel(interaction)
            return
        cid = interaction.channel.id
        if not self._is_session_channel(cid):
            await self._reject_non_session_channel(interaction)
            return

        record = await self._service._repo.get(cid)  # noqa: SLF001 — intentional

        # Build the confirmation message: differs per cwd_mode.
        if record is None:
            body = (
                "이 채널에는 아직 세션 레코드가 없습니다.\n"
                "리셋할 상태가 없으니 리액션 없이 그냥 닫으셔도 됩니다.\n"
                f"{_CONFIRM_YES} 로 확정 / {_CONFIRM_NO} 로 취소 (60초)"
            )
        elif record.cwd_mode == "repo_root":
            body = (
                "⚠️ 세션 상태만 리셋합니다. 파일은 건드리지 않습니다.\n"
                f"- session: `{record.session_id or '(none)'}`\n"
                f"- turns: `{record.turn_count}`\n"
                f"{_CONFIRM_YES} within 60s to confirm, {_CONFIRM_NO} to cancel."
            )
        else:  # dedicated_worktree
            # Compute dirty state with cache bypass — cannot risk a stale
            # read during a destructive operation.
            is_dirty = False
            if record.worktree_path and os.path.isdir(record.worktree_path):
                is_dirty = not await asyncio.to_thread(
                    self._service._wt.is_clean,  # noqa: SLF001
                    record.worktree_path,
                    bypass_cache=True,
                )
            dirty_tag = "DIRTY — will be KEPT" if is_dirty else "clean — will be removed"
            body = (
                "⚠️ Reset the session for this channel?\n"
                f"- worktree: `{record.worktree_path}` ({dirty_tag})\n"
                f"- session:  `{record.session_id or '(none)'}`\n"
                f"- turns:    `{record.turn_count}`\n"
                f"React {_CONFIRM_YES} within 60s to confirm, {_CONFIRM_NO} to cancel."
            )

        await interaction.response.send_message(body)
        prompt_msg = await interaction.original_response()

        with contextlib.suppress(discord.HTTPException):
            await prompt_msg.add_reaction(_CONFIRM_YES)
            await prompt_msg.add_reaction(_CONFIRM_NO)

        def _check(reaction: discord.Reaction, user: discord.User) -> bool:
            if user.id != interaction.user.id:
                return False
            if reaction.message.id != prompt_msg.id:
                return False
            return str(reaction.emoji) in (_CONFIRM_YES, _CONFIRM_NO)

        try:
            reaction, _user = await self.bot.wait_for(
                "reaction_add",
                timeout=_CONFIRM_TIMEOUT_SECONDS,
                check=_check,
            )
        except TimeoutError:
            await interaction.followup.send("⏱️ Timed out — cancelled.", ephemeral=True)
            return

        if str(reaction.emoji) == _CONFIRM_NO:
            await interaction.followup.send("❌ Cancelled.", ephemeral=True)
            return

        # ✅ — execute
        result = await self._service.cleanup_channel(cid, reason="reset_command")

        embed = discord.Embed(
            title="🔄 Channel reset complete",
            color=_COLOR_SUCCESS if result.db_deleted else _COLOR_INFO,
        )
        embed.add_field(
            name="Worktree",
            value=(
                f"removed (`{result.worktree_reason}`)"
                if result.worktree_removed
                else f"preserved (`{result.worktree_reason}`)"
            ),
            inline=False,
        )
        embed.add_field(
            name="DB record",
            value="deleted" if result.db_deleted else "no record",
            inline=True,
        )
        embed.add_field(
            name="Runner",
            value="rebuilt" if result.runner_invalidated else "not affected",
            inline=True,
        )
        if result.worktree_reason == "dirty":
            embed.add_field(
                name="⚠️ Dirty worktree preserved",
                value=(
                    "Commit/stash changes, then run:\n"
                    f"```\ngit worktree remove {record.worktree_path if record else ''}\n```"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # -- Slash command: /ch-worktree-list --------------------------------

    @app_commands.command(
        name="ch-worktree-list",
        description="List Channel-as-Session worktrees (dedicated_worktree channels)",
    )
    async def ch_worktree_list(self, interaction: discord.Interaction) -> None:
        """Show each dedicated_worktree channel's worktree path + dirty state."""
        await interaction.response.defer(ephemeral=True)

        rows: list[tuple[str, str, bool | None]] = []
        for project in self._projects:
            if not project.uses_dedicated_worktree:
                continue
            record = await self._service._repo.get(project.channel_id)  # noqa: SLF001
            if record is None or not record.worktree_path:
                rows.append((project.name, "(not created)", None))
                continue
            exists = os.path.isdir(record.worktree_path)
            if not exists:
                rows.append((project.name, record.worktree_path, None))
                continue
            is_dirty = not await asyncio.to_thread(
                self._service._wt.is_clean,  # noqa: SLF001
                record.worktree_path,
                bypass_cache=False,
            )
            rows.append((project.name, record.worktree_path, is_dirty))

        if not rows:
            embed = discord.Embed(
                title="🌲 Channel-as-Session Worktrees",
                description="No dedicated_worktree channels configured.",
                color=_COLOR_INFO,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🌲 Channel-as-Session Worktrees ({len(rows)})",
            color=_COLOR_INFO,
        )
        for name, path, dirty in rows:
            if dirty is None:
                state = "(not created)"
            elif dirty:
                state = "⚠️ dirty"
            else:
                state = "✅ clean"
            embed.add_field(
                name=name,
                value=f"`{path}`\n{state}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- Slash command: /ch-worktree-cleanup -----------------------------

    @app_commands.command(
        name="ch-worktree-cleanup",
        description="Remove clean orphaned Channel-as-Session worktrees (dirty ones preserved)",
    )
    @app_commands.describe(
        dry_run="Preview without actually removing anything",
    )
    async def ch_worktree_cleanup(
        self,
        interaction: discord.Interaction,
        dry_run: bool = False,
    ) -> None:
        """Bulk cleanup — only clean worktrees, dirty preserved unconditionally."""
        await interaction.response.defer()

        removed: list[str] = []
        preserved_dirty: list[str] = []
        skipped_repo_root: list[str] = []
        other_issues: list[tuple[str, str]] = []
        planned_cmds: list[str] = []

        for project in self._projects:
            if not project.uses_dedicated_worktree:
                skipped_repo_root.append(project.name)
                continue
            record = await self._service._repo.get(project.channel_id)  # noqa: SLF001
            if (
                record is None
                or not record.worktree_path
                or not record.branch_name
                or not os.path.isdir(record.worktree_path)
            ):
                continue
            from ..services.channel_worktree import WorktreePaths

            paths = WorktreePaths(
                repo_root=record.repo_root,
                worktree_path=record.worktree_path,
                branch_name=record.branch_name,
                channel_id=project.channel_id,
            )
            result = await asyncio.to_thread(
                self._service._wt.remove_if_clean,  # noqa: SLF001
                paths,
                dry_run=dry_run,
            )
            if result.removed:
                removed.append(result.path)
            elif result.reason == "dirty":
                preserved_dirty.append(result.path)
            elif result.reason == "would_remove":
                # dry-run clean candidate
                removed.append(result.path)
                if result.planned_commands:
                    planned_cmds.extend(result.planned_commands)
            elif result.reason == "not_exists":
                pass  # silently skip
            else:
                other_issues.append((result.path, result.reason))

        title_suffix = " — Dry Run" if dry_run else ""
        color = _COLOR_INFO if dry_run else (_COLOR_SUCCESS if removed else _COLOR_INFO)
        if preserved_dirty:
            color = _COLOR_WARN

        embed = discord.Embed(
            title=f"🌲 Worktree Cleanup{title_suffix}",
            color=color,
        )
        label = "Would remove" if dry_run else "Removed"
        embed.add_field(
            name=f"✅ {label} ({len(removed)})",
            value="\n".join(f"`{p}`" for p in removed) or "—",
            inline=False,
        )
        if preserved_dirty:
            embed.add_field(
                name=f"⚠️ Dirty — preserved ({len(preserved_dirty)})",
                value="\n".join(f"`{p}`" for p in preserved_dirty) or "—",
                inline=False,
            )
        if other_issues:
            embed.add_field(
                name=f"ℹ️ Issues ({len(other_issues)})",
                value="\n".join(f"`{p}` — {r}" for p, r in other_issues) or "—",
                inline=False,
            )
        if skipped_repo_root:
            embed.add_field(
                name=f"⏭️ repo_root mode skipped ({len(skipped_repo_root)})",
                value=", ".join(f"`{n}`" for n in skipped_repo_root),
                inline=False,
            )
        if dry_run:
            embed.set_footer(text="Re-run without dry_run=True to actually remove.")

        await interaction.followup.send(embed=embed)
