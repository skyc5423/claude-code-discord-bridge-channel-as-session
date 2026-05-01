"""Exception classes for the MCP approval server.

Per design doc §R4 (no-silent-fallback): every failure mode must surface
an explicit exception so callers can notify the user and abort the session
rather than silently degrading to a permissive policy.
"""

from __future__ import annotations


class ApprovalServerUnavailableError(RuntimeError):
    """Raised when the MCP approval server failed to start or mount its routes.

    When ``approval_enabled=True`` and this exception is raised during bot
    startup, the runner must refuse to spawn new sessions and notify the user.
    Automatic fallback to ``acceptEdits`` mode is explicitly forbidden (R4).
    """


class ApprovalTimeoutError(TimeoutError):
    """Raised when the Discord user did not respond within the deadline (25 s).

    The approval broker raises this for the pending asyncio Future when the
    per-request timeout fires.  The MCP tool handler converts it into a
    ``{"behavior": "deny", "message": "User did not respond in time (25s)"}``
    response so Claude Code receives a deterministic deny rather than hanging.
    """


class ApprovalDeniedError(Exception):
    """Raised when the user explicitly clicked Deny on the Discord approval UI.

    Distinct from :class:`ApprovalTimeoutError` so callers can distinguish
    between an active refusal and a passive non-response.
    """
