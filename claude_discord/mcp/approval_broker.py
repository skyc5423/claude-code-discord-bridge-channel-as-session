"""Async broker mediating between the MCP server and Discord approval UI.

Responsibilities:
- Receive approval requests from ``permission_server.py`` (called from MCP).
- Consult the prefix allowlist for auto-allow / auto-deny decisions.
- Dispatch prompts to the registered Discord channel listener.
- Maintain a per-session cache of previously-allowed (tool, input) pairs.
- Resolve futures when the Discord user clicks a button.
- Enforce a per-request timeout (default 25 s).

Design constraints (per §R4):
- No DB persistence — memory-only cache.
- Auto-deny fires BEFORE session cache check (security invariant).
- Timeout resolves to deny, not allow.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .errors import ApprovalTimeoutError
from .prefix_allowlist import ApprovalPolicy, Decision, evaluate_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / listener types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """Describes a single pending approval request sent to the Discord channel."""

    request_id: str
    channel_id: int
    tool_name: str
    tool_input: dict


OnRequestCallback = Callable[[ApprovalRequest], Awaitable[None]]

# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


class ApprovalBroker:
    """Central mediator between MCP tool callbacks and Discord approval UI.

    Usage::

        broker = ApprovalBroker(policy=ApprovalPolicy())

        # In Discord cog (channel setup):
        broker.register_channel(channel_id, on_request=_post_approval_view)

        # In MCP permission server:
        result = await broker.submit(channel_id, "Bash", {"command": "ls"}, "req-123")
        # result → {"behavior": "allow"|"deny", "message": ..., "updatedInput": ...}

        # In Discord button handler:
        broker.resolve("req-123", {"behavior": "allow"})
    """

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        default_timeout: float | None = None,
    ) -> None:
        # Resolve timeout: explicit arg > env var > built-in default (5 min).
        if default_timeout is None:
            env_value = os.environ.get("CCDB_APPROVAL_TIMEOUT", "").strip()
            try:
                default_timeout = float(env_value) if env_value else 300.0
            except ValueError:
                default_timeout = 300.0
        self._policy: ApprovalPolicy = policy or ApprovalPolicy()
        self._default_timeout = default_timeout
        # request_id → asyncio.Future[dict]
        self._pending: dict[str, asyncio.Future[dict]] = {}
        # channel_id → set of tool names whitelisted for this session
        self._session_cache: dict[int, set[str]] = {}
        # channel_id → on_request callback
        self._channel_listeners: dict[int, OnRequestCallback] = {}

    # ------------------------------------------------------------------
    # Channel registration
    # ------------------------------------------------------------------

    def register_channel(
        self,
        channel_id: int,
        on_request: OnRequestCallback,
    ) -> None:
        """Subscribe a Discord channel to receive approval prompts.

        When a second call replaces an existing listener (e.g. on reconnect),
        the old one is dropped and a warning is logged so operators notice
        potential double-registration bugs.

        Args:
            channel_id: The Discord channel ID.
            on_request: Async callable invoked with an :class:`ApprovalRequest`
                        when a new approval is needed.
        """
        if channel_id in self._channel_listeners:
            logger.warning(
                "ApprovalBroker.register_channel: channel_id=%d already registered — replacing",
                channel_id,
            )
        self._channel_listeners[channel_id] = on_request
        logger.debug("ApprovalBroker: registered channel_id=%d", channel_id)

    def unregister_channel(self, channel_id: int) -> None:
        """Remove a channel listener.

        After unregistering, any new ``submit`` calls for this channel will
        block until timeout (no listener to dispatch to). Existing pending
        futures are NOT cancelled — they continue waiting for ``resolve()``.
        """
        removed = self._channel_listeners.pop(channel_id, None)
        if removed is not None:
            logger.debug("ApprovalBroker: unregistered channel_id=%d", channel_id)

    # ------------------------------------------------------------------
    # Session cache
    # ------------------------------------------------------------------

    def add_to_session_cache(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict,  # noqa: ARG002 — kept for API compat; semantics no longer use input
    ) -> None:
        """Whitelist *tool_name* for *channel_id* until the session ends.

        After this call, every subsequent ``submit`` for the same tool on the
        same channel auto-ALLOWs (auto-deny patterns still fire first, so
        ``rm -rf /`` style commands remain blocked even after caching Bash).

        Rationale: an exact input-hash cache almost never matches in practice
        because Claude varies the input every turn. A tool-wide whitelist is
        what the "Allow + cache" button intuitively means.
        """
        self._session_cache.setdefault(channel_id, set()).add(tool_name)
        logger.info(
            "ApprovalBroker: session whitelist add channel=%d tool=%s",
            channel_id,
            tool_name,
        )

    def _in_session_cache(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict,  # noqa: ARG002 — kept for API compat
    ) -> bool:
        cache = self._session_cache.get(channel_id)
        if not cache:
            return False
        return tool_name in cache

    # ------------------------------------------------------------------
    # Core submit / resolve
    # ------------------------------------------------------------------

    async def submit(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict,
        request_id: str,
    ) -> dict:
        """Evaluate a tool call and return an MCP behavior decision.

        Evaluation order (per §R3 / §R4):
        1. prefix_allowlist auto-deny → immediate DENY
        2. prefix_allowlist auto-allow → cache + immediate ALLOW
        3. Session cache hit → immediate ALLOW
        4. Dispatch to Discord listener → await user response (timeout → DENY)

        Args:
            channel_id: Discord channel the session belongs to.
            tool_name: Claude Code tool name (``"Bash"``, ``"Read"``, …).
            tool_input: Raw tool input dict.
            request_id: Unique request identifier (tool_use_id from MCP).

        Returns:
            Dict with at minimum ``{"behavior": "allow"|"deny", "message": str}``.
        """
        # Step 1+2: allowlist evaluation (auto-deny fires BEFORE cache check)
        decision = evaluate_tool(tool_name, tool_input, self._policy)

        if decision == Decision.DENY:
            logger.info("ApprovalBroker: auto-deny tool=%s request_id=%s", tool_name, request_id)
            return {"behavior": "deny", "message": "Command matches auto-deny policy."}

        if decision == Decision.ALLOW:
            logger.info(
                "ApprovalBroker: auto-allow (allowlist) tool=%s request_id=%s",
                tool_name,
                request_id,
            )
            return {"behavior": "allow", "updatedInput": tool_input}

        # Step 3: session cache
        if self._in_session_cache(channel_id, tool_name, tool_input):
            logger.info(
                "ApprovalBroker: session-cache allow tool=%s request_id=%s",
                tool_name,
                request_id,
            )
            return {"behavior": "allow", "updatedInput": tool_input}

        # Step 4: dispatch to Discord
        return await self._dispatch_to_discord(channel_id, tool_name, tool_input, request_id)

    async def _dispatch_to_discord(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict,
        request_id: str,
    ) -> dict:
        """Create a pending future, fire the listener, and await the result."""
        listener = self._channel_listeners.get(channel_id)

        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[request_id] = future

        if listener is not None:
            req = ApprovalRequest(
                request_id=request_id,
                channel_id=channel_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
            logger.info(
                "ApprovalBroker: dispatching to listener channel_id=%d tool=%s request_id=%s",
                channel_id,
                tool_name,
                request_id,
            )
            try:
                await listener(req)
            except Exception:
                logger.exception(
                    "ApprovalBroker: listener raised for channel_id=%d request_id=%s",
                    channel_id,
                    request_id,
                )
        else:
            logger.warning(
                "ApprovalBroker: no listener for channel_id=%d — request will timeout",
                channel_id,
            )

        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=self._default_timeout)
            return result
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            logger.warning("ApprovalBroker: timeout waiting for decision request_id=%s", request_id)
            raise ApprovalTimeoutError(
                f"User did not respond in time ({self._default_timeout:.0f}s)"
            ) from exc

    def resolve(self, request_id: str, decision: dict) -> None:
        """Settle a pending approval future.

        Called by the Discord button click handler.  If ``request_id`` is no
        longer pending (already timed out or resolved), the call is silently
        ignored — this prevents double-clicks from raising errors.

        Args:
            request_id: The ``tool_use_id`` from the original MCP request.
            decision: Dict with at minimum ``{"behavior": "allow"|"deny"}``.
        """
        future = self._pending.pop(request_id, None)
        if future is None:
            logger.debug(
                "ApprovalBroker.resolve: request_id=%s not found (already settled?)",
                request_id,
            )
            return
        if not future.done():
            future.set_result(decision)
            logger.debug("ApprovalBroker.resolve: settled request_id=%s", request_id)
