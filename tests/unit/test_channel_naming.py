"""Unit tests for channel_naming resolver + branch namer."""

from __future__ import annotations

import pytest

from claude_discord.services.channel_naming import (
    MAIN_CHANNEL_PATTERN,
    WORKTREE_CHANNEL_PATTERN,
    branch_name,
    resolve_channel_name,
)


@pytest.mark.parametrize(
    "name,expected_mode,expected_slug",
    [
        ("main", "repo_root", None),
        ("wt-feat-auth", "dedicated_worktree", "feat-auth"),
        ("wt-docs_v2", "dedicated_worktree", "docs_v2"),
        ("wt-a", "dedicated_worktree", "a"),
        ("wt-1", "dedicated_worktree", "1"),
        (
            "wt-long-slug-with-many-dashes-123",
            "dedicated_worktree",
            "long-slug-with-many-dashes-123",
        ),
    ],
)
def test_resolve_valid(name, expected_mode, expected_slug):
    r = resolve_channel_name(name)
    assert r is not None
    assert r.cwd_mode == expected_mode
    assert r.slug == expected_slug


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Main",  # capital M
        "MAIN",
        "wt-",  # empty slug
        "wt--double",  # leading dash
        "wt-_underscore",  # leading underscore
        "wt-Bug123",  # capital
        "wt-foo/bar",  # slash
        "wt-foo bar",  # space
        "notes",  # unknown
        "discussion",
        "general",
        "wt",  # no dash
        "w-foo",  # wrong prefix
    ],
)
def test_resolve_invalid(name):
    assert resolve_channel_name(name) is None


def test_branch_name_basic():
    assert branch_name("channel-session", "feat-auth") == "channel-session/feat-auth"
    assert branch_name("ch", "x") == "ch/x"


def test_regex_constants_shared():
    """Sanity: the constants are the source of truth; make sure they're
    importable from the module-level namespace (used by docs)."""
    assert MAIN_CHANNEL_PATTERN.match("main")
    assert WORKTREE_CHANNEL_PATTERN.match("wt-foo")
    assert not MAIN_CHANNEL_PATTERN.match("Main")
    assert not WORKTREE_CHANNEL_PATTERN.match("wt-FOO")
