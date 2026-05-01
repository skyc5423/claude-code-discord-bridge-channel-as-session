"""Discord UI for MCP-based tool approval requests (Phase A-2).

Presents a three-button embed when Claude Code needs explicit user approval
for a tool invocation.  The buttons map to:

    - Allow (single)              → broker.resolve(allow)
    - Allow + cache prefix        → broker.resolve(allow) + session cache
    - Deny                        → broker.resolve(deny)

The view timeout (23 s) fires slightly before the broker timeout (25 s) so
that buttons are disabled before the broker's asyncio timeout is raised.
This prevents double-clicks after the deadline.
"""

from __future__ import annotations

import json
import logging
import os

import discord

logger = logging.getLogger(__name__)


def _resolve_view_timeout() -> float:
    """View timeout = broker timeout - 2s (buttons disable before deadline).

    Honors ``CCDB_APPROVAL_TIMEOUT`` so users can extend the prompt window.
    Default 5 minutes (broker default 300s).
    """
    env_value = os.environ.get("CCDB_APPROVAL_TIMEOUT", "").strip()
    try:
        broker_timeout = float(env_value) if env_value else 300.0
    except ValueError:
        broker_timeout = 300.0
    return max(broker_timeout - 2.0, 5.0)


_VIEW_TIMEOUT = _resolve_view_timeout()

# Discord embed limits: description ≤ 4096 chars, field value ≤ 1024.
# We render tool input as the embed description (not a field) so larger
# inputs (e.g. Write tool with a full file) survive without truncation issues.
# 3800 leaves headroom for the surrounding ```json fence (~12 chars) and
# the "(truncated)" suffix.
_MAX_INPUT_DISPLAY = 3800


class ApprovalView(discord.ui.View):
    """Three-button approval view for MCP permission requests.

    Constructor Args:
        broker: The :class:`~claude_discord.mcp.approval_broker.ApprovalBroker`
                instance that owns the pending future.
        channel_id: Discord channel the session belongs to.
        request_id: Unique request identifier (``tool_use_id``).
        tool_name: Claude Code tool name (``"Bash"``, ``"Write"``, …).
        tool_input: Raw tool input dict; displayed truncated in the embed.
    """

    def __init__(
        self,
        broker: object,
        channel_id: int,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT)
        # Avoid a direct import cycle — broker is typed as object here;
        # callers know it is an ApprovalBroker.
        self._broker = broker
        self._channel_id = channel_id
        self._request_id = request_id
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._settled = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self._settled = True

    async def _edit_view(self, interaction: discord.Interaction) -> None:
        """Replace the view with disabled buttons after a decision."""
        self._disable_all()
        if interaction.message:
            import contextlib

            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(view=self)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="✅ 허용", style=discord.ButtonStyle.success)
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Allow this single tool invocation."""
        await interaction.response.defer()
        if self._settled:
            return
        self._broker.resolve(  # type: ignore[attr-defined]
            self._request_id,
            {"behavior": "allow", "updatedInput": self._tool_input},
        )
        await self._edit_view(interaction)
        logger.info(
            "ApprovalView: allowed request_id=%s tool=%s", self._request_id, self._tool_name
        )

    @discord.ui.button(
        label="✅ 허용 + 이 도구 세션 내 자동 허용",
        style=discord.ButtonStyle.primary,
    )
    async def allow_and_cache(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Allow and add to session cache so identical calls auto-allow."""
        await interaction.response.defer()
        if self._settled:
            return
        self._broker.add_to_session_cache(  # type: ignore[attr-defined]
            self._channel_id, self._tool_name, self._tool_input
        )
        self._broker.resolve(  # type: ignore[attr-defined]
            self._request_id,
            {"behavior": "allow", "updatedInput": self._tool_input},
        )
        await self._edit_view(interaction)
        logger.info(
            "ApprovalView: allowed+cached request_id=%s tool=%s channel=%d",
            self._request_id,
            self._tool_name,
            self._channel_id,
        )

    @discord.ui.button(label="❌ 거부", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Deny this tool invocation."""
        await interaction.response.defer()
        if self._settled:
            return
        self._broker.resolve(  # type: ignore[attr-defined]
            self._request_id,
            {"behavior": "deny", "message": "User denied the request."},
        )
        await self._edit_view(interaction)
        logger.info("ApprovalView: denied request_id=%s tool=%s", self._request_id, self._tool_name)

    async def on_timeout(self) -> None:
        """Disable buttons when the view times out.

        The broker's asyncio timeout fires ~2 s later, producing the deny
        response for the MCP tool call.  We only need to clean up the UI.
        """
        if self._settled:
            return
        self._disable_all()
        logger.debug("ApprovalView: timed out request_id=%s", self._request_id)


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------


def build_approval_embed(tool_name: str, tool_input: dict) -> discord.Embed:
    """Build a Discord embed summarising the approval request.

    The tool input is JSON-formatted and truncated to ``_MAX_INPUT_DISPLAY``
    characters to stay within Discord embed limits.
    """
    try:
        raw = json.dumps(tool_input, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(tool_input)

    if len(raw) > _MAX_INPUT_DISPLAY:
        raw = raw[:_MAX_INPUT_DISPLAY] + "\n… (truncated)"

    embed = discord.Embed(
        title=f"Claude tool permission request: {tool_name}",
        description=f"```json\n{raw}\n```",
        color=0xFFA500,  # orange — pending decision
    )
    timeout_label = max(int(_resolve_view_timeout()) + 2, 5)
    embed.set_footer(text=f"{timeout_label}s 내 응답 없으면 자동 거부됩니다.")
    return embed
