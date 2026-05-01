"""Unit tests for approval-flow wiring in ChannelSessionService.handle_message.

Tests:
- approval_enabled=False → clone called WITHOUT mcp_config_path /
  permission_prompt_tool / permission_mode overrides
- approval_enabled=True + broker provided → clone called WITH all three
  approval args; broker.register_channel called before run,
  broker.unregister_channel called after run
- broker=None but approval_enabled=True → backward-compat (no approval)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_discord.config.projects_config import (
    CategoryProjectConfig,
    RegisteredChannel,
)
from claude_discord.services.channel_session_service import ChannelSessionService

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_REPO_ROOT = "/tmp/test-repo"
_STATUS_PATH = "claude_discord.services.channel_session_service.StatusManager"
_RUN_PATH = "claude_discord.services.channel_session_service.run_claude_with_config"
_MCP_BUILD_PATH = "claude_discord.services.channel_session_service.build_mcp_config_for_channel"


def _make_project(*, approval_enabled: bool = False) -> CategoryProjectConfig:
    return CategoryProjectConfig(
        category_id=111,
        name="test-project",
        repo_root=_REPO_ROOT,
        model="sonnet",
        permission_mode="acceptEdits",
        approval_enabled=approval_enabled,
    )


def _make_registered(project: CategoryProjectConfig, channel_id: int = 999) -> RegisteredChannel:
    return RegisteredChannel(
        channel_id=channel_id,
        channel_name="main",
        category_id=project.category_id,
        cwd_mode="repo_root",
        slug=None,
        worktree_path=None,
        branch_name=None,
        project=project,
    )


def _make_service(
    project: CategoryProjectConfig,
    *,
    approval_broker: object = None,
    api_port: int | None = None,
) -> ChannelSessionService:
    """Build a ChannelSessionService with all dependencies mocked."""
    projects = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.ensure = AsyncMock(return_value=MagicMock(session_id="sess-1"))
    repo.increment_turn = AsyncMock()
    repo.reset_error = AsyncMock()

    session_repo = MagicMock()
    runner_cache = MagicMock()
    wt_manager = MagicMock()
    topic_updater = MagicMock()
    topic_updater.maybe_update_topic = AsyncMock()
    topic_updater.maybe_emit_warning = AsyncMock()
    topic_updater.maybe_clear_warning = AsyncMock()
    session_lookup = MagicMock()

    return ChannelSessionService(
        projects=projects,
        repo=repo,
        session_repo=session_repo,
        runner_cache=runner_cache,
        wt_manager=wt_manager,
        topic_updater=topic_updater,
        session_lookup=session_lookup,
        approval_broker=approval_broker,  # type: ignore[arg-type]
        api_port=api_port,
    )


def _make_mock_runner() -> tuple[MagicMock, MagicMock]:
    runner = MagicMock()
    cloned = MagicMock()
    cloned.model = "claude-3-sonnet"
    cloned.command = "claude"
    runner.clone.return_value = cloned
    return runner, cloned


def _make_channel(channel_id: int = 999) -> MagicMock:
    channel = MagicMock()
    channel.id = channel_id
    channel.send = AsyncMock(return_value=MagicMock())
    return channel


def _make_user_message() -> MagicMock:
    msg = MagicMock()
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()
    msg.edit = AsyncMock()
    return msg


def _mock_status() -> MagicMock:
    s = MagicMock()
    s.set_thinking = AsyncMock()
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_disabled_clone_has_no_approval_args() -> None:
    """When approval_enabled=False, runner.clone must NOT receive approval args."""
    project = _make_project(approval_enabled=False)
    registered = _make_registered(project)
    svc = _make_service(project, approval_broker=None, api_port=None)

    base_runner, _cloned = _make_mock_runner()
    svc._runner_cache.get.return_value = base_runner

    channel = _make_channel(registered.channel_id)
    user_msg = _make_user_message()

    with (
        patch.object(svc, "_prepare_cwd", AsyncMock(return_value=(None, None, _REPO_ROOT))),
        patch(_RUN_PATH, AsyncMock()),
        patch(_STATUS_PATH, return_value=_mock_status()),
    ):
        svc._repo.get = AsyncMock(return_value=None)
        await svc.handle_message(
            channel=channel,
            user_message=user_msg,
            registered=registered,
            prompt="hello",
        )

    base_runner.clone.assert_called_once()
    call_kwargs = base_runner.clone.call_args[1]
    assert "mcp_config_path" not in call_kwargs
    assert "permission_prompt_tool" not in call_kwargs


@pytest.mark.asyncio
async def test_approval_enabled_clone_has_approval_args() -> None:
    """When approval_enabled=True + broker set, clone must include all three args."""
    project = _make_project(approval_enabled=True)
    registered = _make_registered(project)

    broker = MagicMock()
    broker.register_channel = MagicMock()
    broker.unregister_channel = MagicMock()

    svc = _make_service(project, approval_broker=broker, api_port=8765)

    base_runner, _cloned = _make_mock_runner()
    svc._runner_cache.get.return_value = base_runner

    channel = _make_channel(registered.channel_id)
    user_msg = _make_user_message()

    mcp_path = Path(f"/tmp/ccdb-mcp/{registered.channel_id}.json")

    with (
        patch.object(svc, "_prepare_cwd", AsyncMock(return_value=(None, None, _REPO_ROOT))),
        patch(_RUN_PATH, AsyncMock()),
        patch(_STATUS_PATH, return_value=_mock_status()),
        patch(_MCP_BUILD_PATH, return_value=mcp_path) as mock_build,
    ):
        svc._repo.get = AsyncMock(return_value=None)
        await svc.handle_message(
            channel=channel,
            user_message=user_msg,
            registered=registered,
            prompt="hello",
        )

    mock_build.assert_called_once_with(registered.channel_id, 8765)

    base_runner.clone.assert_called_once()
    call_kwargs = base_runner.clone.call_args[1]
    assert call_kwargs["mcp_config_path"] == mcp_path
    assert call_kwargs["permission_prompt_tool"] == "mcp__ccdb__approval_request"
    # Project's permission_mode is preserved (acceptEdits is compatible with
    # --permission-prompt-tool). Only incompatible modes get coerced to default.
    assert call_kwargs["permission_mode"] == "acceptEdits"


@pytest.mark.asyncio
async def test_broker_register_before_run_unregister_after_run() -> None:
    """broker.register_channel is called before run; unregister after."""
    project = _make_project(approval_enabled=True)
    registered = _make_registered(project)

    call_order: list[str] = []

    broker = MagicMock()
    broker.register_channel = MagicMock(
        side_effect=lambda *a, **kw: call_order.append("register")
    )
    broker.unregister_channel = MagicMock(
        side_effect=lambda *a, **kw: call_order.append("unregister")
    )

    svc = _make_service(project, approval_broker=broker, api_port=8765)

    base_runner, _cloned = _make_mock_runner()
    svc._runner_cache.get.return_value = base_runner

    channel = _make_channel(registered.channel_id)
    user_msg = _make_user_message()

    async def _mock_run(config: object) -> None:  # noqa: ANN401
        call_order.append("run")

    mcp_path = Path(f"/tmp/ccdb-mcp/{registered.channel_id}.json")

    with (
        patch.object(svc, "_prepare_cwd", AsyncMock(return_value=(None, None, _REPO_ROOT))),
        patch(_RUN_PATH, side_effect=_mock_run),
        patch(_STATUS_PATH, return_value=_mock_status()),
        patch(_MCP_BUILD_PATH, return_value=mcp_path),
    ):
        svc._repo.get = AsyncMock(return_value=None)
        await svc.handle_message(
            channel=channel,
            user_message=user_msg,
            registered=registered,
            prompt="hello",
        )

    assert call_order == ["register", "run", "unregister"], (
        f"Expected register→run→unregister, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_broker_none_with_approval_enabled_no_approval_args() -> None:
    """If approval_enabled=True but broker is None, no approval args are passed."""
    project = _make_project(approval_enabled=True)
    registered = _make_registered(project)

    svc = _make_service(project, approval_broker=None, api_port=8765)

    base_runner, _cloned = _make_mock_runner()
    svc._runner_cache.get.return_value = base_runner

    channel = _make_channel(registered.channel_id)
    user_msg = _make_user_message()

    with (
        patch.object(svc, "_prepare_cwd", AsyncMock(return_value=(None, None, _REPO_ROOT))),
        patch(_RUN_PATH, AsyncMock()),
        patch(_STATUS_PATH, return_value=_mock_status()),
    ):
        svc._repo.get = AsyncMock(return_value=None)
        await svc.handle_message(
            channel=channel,
            user_message=user_msg,
            registered=registered,
            prompt="hello",
        )

    call_kwargs = base_runner.clone.call_args[1]
    assert "mcp_config_path" not in call_kwargs
    assert "permission_prompt_tool" not in call_kwargs
