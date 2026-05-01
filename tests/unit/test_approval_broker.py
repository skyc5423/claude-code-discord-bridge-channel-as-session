"""Unit tests for claude_discord.mcp.approval_broker.ApprovalBroker.

Covers:
- Auto-allow via prefix allowlist
- Auto-deny via allowlist (fires before cache check — security invariant)
- Session cache: add_to_session_cache makes next submit auto-allow
- Blocking submit resolved by resolve()
- Timeout → deny (monkeypatched to 0.05 s for speed)
- Concurrent independent submits for the same channel
- register_channel replaces listener with warning logged
- unregister_channel clears listener; submit times out
- Auto-deny BEFORE cache check (explicit invariant test)
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from claude_discord.mcp.approval_broker import ApprovalBroker, ApprovalRequest
from claude_discord.mcp.errors import ApprovalTimeoutError
from claude_discord.mcp.prefix_allowlist import ApprovalPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHANNEL = 111222333

_ALLOW_POLICY = ApprovalPolicy(
    safe_prefixes=("ls",),
    auto_deny_patterns=("rm -rf /",),
)

_DENY_INPUT = {"command": "rm -rf /"}
_ALLOW_INPUT = {"command": "ls /tmp"}
_UNKNOWN_INPUT = {"command": "some_custom_tool_call --flag"}


def _make_broker(timeout: float = 25.0) -> ApprovalBroker:
    return ApprovalBroker(policy=_ALLOW_POLICY, default_timeout=timeout)


# ---------------------------------------------------------------------------
# Auto-allow / auto-deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_auto_allow_via_allowlist() -> None:
    broker = _make_broker()
    result = await broker.submit(CHANNEL, "Bash", _ALLOW_INPUT, "req-1")
    assert result["behavior"] == "allow"


@pytest.mark.asyncio
async def test_submit_auto_deny_via_allowlist() -> None:
    broker = _make_broker()
    result = await broker.submit(CHANNEL, "Bash", _DENY_INPUT, "req-2")
    assert result["behavior"] == "deny"


@pytest.mark.asyncio
async def test_submit_read_tool_always_allow() -> None:
    broker = _make_broker()
    result = await broker.submit(CHANNEL, "Read", {"file_path": "/etc/hosts"}, "req-3")
    assert result["behavior"] == "allow"


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_session_cache_makes_next_allow() -> None:
    broker = _make_broker()
    tool_input = {"command": "some_custom_tool_call --flag"}

    # First call: no listener → would timeout, but we add to cache first
    broker.add_to_session_cache(CHANNEL, "Bash", tool_input)

    # Now submit should hit cache and return allow immediately
    result = await broker.submit(CHANNEL, "Bash", tool_input, "req-cache-1")
    assert result["behavior"] == "allow"


@pytest.mark.asyncio
async def test_session_cache_miss_then_hit() -> None:
    broker = _make_broker()
    tool_input = {"command": "some_tool"}

    # Not in cache yet — would dispatch to listener (none registered → timeout)
    # Instead, explicitly add then check
    broker.add_to_session_cache(CHANNEL, "Bash", tool_input)
    result = await broker.submit(CHANNEL, "Bash", tool_input, "req-cache-2")
    assert result["behavior"] == "allow"


@pytest.mark.asyncio
async def test_session_cache_different_channels_are_independent() -> None:
    broker = _make_broker()
    tool_input = {"command": "some_tool"}
    channel_a, channel_b = 111, 222

    broker.add_to_session_cache(channel_a, "Bash", tool_input)

    # channel_a: cache hit
    result_a = await broker.submit(channel_a, "Bash", tool_input, "req-chA")
    assert result_a["behavior"] == "allow"

    # channel_b: cache miss → would timeout (no listener); use very short timeout
    broker2 = ApprovalBroker(policy=_ALLOW_POLICY, default_timeout=0.05)
    broker2.add_to_session_cache(channel_a, "Bash", tool_input)
    with pytest.raises(ApprovalTimeoutError):
        await broker2.submit(channel_b, "Bash", tool_input, "req-chB")


# ---------------------------------------------------------------------------
# Blocking submit + resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_blocks_until_resolve() -> None:
    broker = _make_broker()
    received: list[ApprovalRequest] = []

    async def listener(req: ApprovalRequest) -> None:
        received.append(req)

    broker.register_channel(CHANNEL, listener)

    async def _resolve_after_delay() -> None:
        await asyncio.sleep(0.05)
        broker.resolve("req-block", {"behavior": "allow", "updatedInput": _UNKNOWN_INPUT})

    task = asyncio.create_task(_resolve_after_delay())
    result = await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-block")
    await task

    assert result["behavior"] == "allow"
    assert len(received) == 1
    assert received[0].request_id == "req-block"


@pytest.mark.asyncio
async def test_submit_resolves_with_deny() -> None:
    broker = _make_broker()

    async def listener(req: ApprovalRequest) -> None:
        pass

    broker.register_channel(CHANNEL, listener)

    async def _deny() -> None:
        await asyncio.sleep(0.02)
        broker.resolve("req-deny", {"behavior": "deny", "message": "No."})

    task = asyncio.create_task(_deny())
    result = await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-deny")
    await task

    assert result["behavior"] == "deny"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_times_out_and_raises() -> None:
    broker = ApprovalBroker(policy=_ALLOW_POLICY, default_timeout=0.05)
    # No listener registered — future is created but never resolved

    with pytest.raises(ApprovalTimeoutError):
        await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-timeout")


@pytest.mark.asyncio
async def test_submit_timeout_message_mentions_seconds() -> None:
    broker = ApprovalBroker(policy=_ALLOW_POLICY, default_timeout=0.05)

    with pytest.raises(ApprovalTimeoutError, match="did not respond"):
        await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-timeout-msg")


# ---------------------------------------------------------------------------
# Concurrent submits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_submits_are_independent() -> None:
    broker = _make_broker()

    async def listener(req: ApprovalRequest) -> None:
        pass

    broker.register_channel(CHANNEL, listener)

    async def resolve_after(request_id: str, behavior: str, delay: float) -> None:
        await asyncio.sleep(delay)
        broker.resolve(request_id, {"behavior": behavior})

    tasks = [
        asyncio.create_task(resolve_after("req-c1", "allow", 0.03)),
        asyncio.create_task(resolve_after("req-c2", "deny", 0.05)),
        asyncio.create_task(resolve_after("req-c3", "allow", 0.01)),
    ]

    results = await asyncio.gather(
        broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-c1"),
        broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-c2"),
        broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-c3"),
    )
    for task in tasks:
        await task

    # All three distinct decisions should come back correctly
    assert results[0]["behavior"] == "allow"
    assert results[1]["behavior"] == "deny"
    assert results[2]["behavior"] == "allow"


# ---------------------------------------------------------------------------
# register_channel / unregister_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_channel_twice_replaces_listener(caplog) -> None:
    broker = _make_broker()
    calls_a: list[int] = []
    calls_b: list[int] = []

    async def listener_a(req: ApprovalRequest) -> None:
        calls_a.append(1)

    async def listener_b(req: ApprovalRequest) -> None:
        calls_b.append(1)

    with caplog.at_level(logging.WARNING, logger="claude_discord.mcp.approval_broker"):
        broker.register_channel(CHANNEL, listener_a)
        broker.register_channel(CHANNEL, listener_b)  # should log warning

    assert any("already registered" in r.message for r in caplog.records)

    # Resolve immediately to avoid timeout
    async def _resolve() -> None:
        await asyncio.sleep(0.02)
        broker.resolve("req-reg", {"behavior": "allow"})

    task = asyncio.create_task(_resolve())
    await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-reg")
    await task

    assert calls_a == []  # listener_a was replaced
    assert calls_b == [1]  # listener_b received the call


@pytest.mark.asyncio
async def test_unregister_channel_then_submit_times_out() -> None:
    broker = ApprovalBroker(policy=_ALLOW_POLICY, default_timeout=0.05)

    async def listener(req: ApprovalRequest) -> None:
        pass

    broker.register_channel(CHANNEL, listener)
    broker.unregister_channel(CHANNEL)

    with pytest.raises(ApprovalTimeoutError):
        await broker.submit(CHANNEL, "Bash", _UNKNOWN_INPUT, "req-unreg")


# ---------------------------------------------------------------------------
# Auto-deny BEFORE session cache (security invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_deny_fires_before_session_cache() -> None:
    """SECURITY: even if a deny pattern is in the session cache, auto-deny wins."""
    broker = _make_broker()

    # Manually insert the deny command into session cache
    broker.add_to_session_cache(CHANNEL, "Bash", _DENY_INPUT)

    # Must still return DENY — cache must NOT override auto-deny
    result = await broker.submit(CHANNEL, "Bash", _DENY_INPUT, "req-sec")
    assert result["behavior"] == "deny", (
        "Auto-deny must fire before session cache check (security invariant)"
    )


# ---------------------------------------------------------------------------
# resolve on unknown / already-resolved request_id
# ---------------------------------------------------------------------------


def test_resolve_unknown_request_id_is_noop() -> None:
    broker = _make_broker()
    # Should not raise
    broker.resolve("nonexistent-id", {"behavior": "allow"})
