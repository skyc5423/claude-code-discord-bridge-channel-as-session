"""MCP server that handles Claude Code permission-prompt-tool callbacks.

Registers a single MCP tool ``approval_request`` which Claude Code calls
whenever it needs user approval for a tool use.  The server runs in-process
inside the bot via the HTTP/SSE transport mounted on the existing aiohttp app.

Phase A-2: full broker integration.  The ``channel_id`` is injected via a
module-level ``contextvars.ContextVar`` set by the SSE handler in
``ext/api_server.py`` before delegating to the SDK transport.
"""

from __future__ import annotations

import contextvars
import json
import logging

logger = logging.getLogger(__name__)

# ContextVar injected by the SSE handler for every connection so the tool
# handler knows which Discord channel owns this MCP session.
_current_channel_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "ccdb_mcp_channel_id", default=None
)


def build_mcp_server(broker: object | None = None) -> object:
    """Build and return an MCP Server with the ``approval_request`` tool registered.

    Args:
        broker: An :class:`~claude_discord.mcp.approval_broker.ApprovalBroker`
                instance.  When ``None`` (Phase A-1 stub mode), the tool always
                returns an immediate deny response so existing callers are not
                broken.

    Returns:
        mcp.server.Server: configured server.

    Raises:
        ImportError: propagated if the ``mcp`` package is not installed.
            Install with ``pip install claude-code-discord-bridge[approval]``.
    """
    # Import here so callers get a clear ImportError if mcp is absent.
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server: Server = Server("ccdb-approval")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="approval_request",
                description=(
                    "Request user approval for a Claude Code tool invocation. "
                    "Returns a behavior decision: 'allow' or 'deny'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "tool_use_id": {
                            "type": "string",
                            "description": "Unique identifier for the tool-use request.",
                        },
                        "tool_name": {
                            "type": "string",
                            "description": "Name of the Claude Code tool requesting permission.",
                        },
                        "input": {
                            "type": "object",
                            "description": "Tool input parameters as provided by Claude.",
                        },
                    },
                    "required": ["tool_use_id", "tool_name", "input"],
                },
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Dispatch the approval request to the broker, or stub-deny if no broker."""
        if name != "approval_request":
            logger.warning("approval_request tool: unknown tool name %r", name)
            result = {"behavior": "deny", "message": f"Unknown tool: {name}"}
            return [TextContent(type="text", text=json.dumps(result))]

        tool_name = arguments.get("tool_name", "<unknown>")
        tool_use_id = arguments.get("tool_use_id", "<unknown>")
        tool_input = arguments.get("input", {})

        if broker is None:
            # Phase A-1 stub — broker not yet wired.
            logger.debug(
                "approval_request stub: denying tool_use_id=%s tool_name=%s",
                tool_use_id,
                tool_name,
            )
            result = {
                "behavior": "deny",
                "message": "approval not yet wired (no broker)",
            }
            return [TextContent(type="text", text=json.dumps(result))]

        channel_id = _current_channel_id.get()
        if channel_id is None:
            logger.error(
                "approval_request: no channel_id in context for request_id=%s; denying",
                tool_use_id,
            )
            result = {
                "behavior": "deny",
                "message": "Internal error: channel_id not resolved.",
            }
            return [TextContent(type="text", text=json.dumps(result))]

        try:
            from .errors import ApprovalTimeoutError

            result = await broker.submit(  # type: ignore[attr-defined]
                channel_id, tool_name, tool_input, tool_use_id
            )
        except ApprovalTimeoutError as exc:
            logger.warning("approval_request: timeout for request_id=%s: %s", tool_use_id, exc)
            result = {"behavior": "deny", "message": str(exc)}
        except Exception:
            logger.exception("approval_request: unexpected error for request_id=%s", tool_use_id)
            result = {
                "behavior": "deny",
                "message": "Internal error during approval; session denied for safety.",
            }

        return [TextContent(type="text", text=json.dumps(result))]

    logger.debug("MCP approval server built (broker=%s)", "wired" if broker else "stub")
    return server
