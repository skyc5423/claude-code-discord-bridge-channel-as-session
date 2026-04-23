"""Unit tests for the phase-2 category-keyed ProjectsConfig."""

from __future__ import annotations

import pytest

from claude_discord.config.projects_config import (
    CategoryProjectConfig,
    ConfigError,
    ProjectsConfig,
    RegisteredChannel,
)


def _sample_cfg() -> ProjectsConfig:
    return ProjectsConfig.from_mapping(
        {
            "100": {"name": "Dalpha", "repo_root": "/r/dalpha", "shared_cwd_warning": True},
            "200": {"name": "oi-agent", "repo_root": "/r/oi-agent"},
        }
    )


def test_category_api():
    cfg = _sample_cfg()
    assert cfg.has_category(100)
    assert cfg.has_category(200)
    assert not cfg.has_category(999)
    c = cfg.get_category(100)
    assert isinstance(c, CategoryProjectConfig)
    assert c.name == "Dalpha"
    assert c.shared_cwd_warning is True
    assert cfg.category_ids() == {100, 200}


def test_register_main_channel():
    cfg = _sample_cfg()
    reg = cfg.register_channel(channel_id=10, channel_name="main", category_id=100)
    assert reg is not None
    assert reg.cwd_mode == "repo_root"
    assert reg.slug is None
    assert reg.worktree_path is None
    assert reg.branch_name is None
    assert reg.shared_cwd_warning is True  # category says so
    assert reg.repo_root == "/r/dalpha"
    assert reg.uses_dedicated_worktree is False
    assert cfg.has(10) is True
    assert cfg.get(10) is reg


def test_register_worktree_channel():
    cfg = _sample_cfg()
    reg = cfg.register_channel(channel_id=20, channel_name="wt-feat-auth", category_id=100)
    assert reg is not None
    assert reg.cwd_mode == "dedicated_worktree"
    assert reg.slug == "feat-auth"
    assert reg.worktree_path is not None
    assert reg.worktree_path.endswith("/.worktrees/ch-feat-auth")
    assert reg.branch_name == "channel-session/feat-auth"
    assert reg.shared_cwd_warning is False  # worktree doesn't inherit shared flag
    assert reg.uses_dedicated_worktree is True


def test_register_unknown_category():
    cfg = _sample_cfg()
    assert cfg.register_channel(channel_id=1, channel_name="main", category_id=999) is None
    assert not cfg.has(1)


def test_register_invalid_name_rejected():
    cfg = _sample_cfg()
    for bad in ["notes", "wt-Bug", "Main", "wt-", ""]:
        assert cfg.register_channel(channel_id=1, channel_name=bad, category_id=100) is None


def test_unregister_in_memory_only():
    cfg = _sample_cfg()
    reg = cfg.register_channel(channel_id=10, channel_name="main", category_id=100)
    assert reg is not None
    removed = cfg.unregister_channel(10)
    assert removed is reg
    assert cfg.has(10) is False
    # Idempotent
    assert cfg.unregister_channel(10) is None


def test_replace_categories_diff():
    cfg = _sample_cfg()
    cfg.register_channel(channel_id=10, channel_name="main", category_id=100)
    cfg.register_channel(channel_id=20, channel_name="main", category_id=200)

    new_cats = {
        100: CategoryProjectConfig(  # unchanged
            category_id=100,
            name="Dalpha",
            repo_root="/r/dalpha",
            shared_cwd_warning=True,
        ),
        300: CategoryProjectConfig(  # added
            category_id=300,
            name="new",
            repo_root="/r/new",
        ),
        # 200 removed
    }
    diff = cfg.replace_categories(new_cats)
    assert diff.added == {300}
    assert diff.removed == {200}
    assert diff.changed == set()
    # Channel 10 still registered (category 100 still here)
    assert cfg.has(10)
    # Channel 20 dropped (category 200 removed)
    assert not cfg.has(20)


def test_replace_categories_detects_changed_fields():
    cfg = _sample_cfg()
    new_cats = {
        100: CategoryProjectConfig(
            category_id=100,
            name="Dalpha",
            repo_root="/r/dalpha_NEW",  # changed!
            shared_cwd_warning=True,
        ),
        200: CategoryProjectConfig(category_id=200, name="oi-agent", repo_root="/r/oi-agent"),
    }
    diff = cfg.replace_categories(new_cats)
    assert diff.added == set()
    assert diff.removed == set()
    assert diff.changed == {100}


def test_from_mapping_validation():
    with pytest.raises(ConfigError, match="must be numeric"):
        ProjectsConfig.from_mapping({"abc": {"name": "x", "repo_root": "/r"}})
    with pytest.raises(ConfigError, match="field='name'"):
        ProjectsConfig.from_mapping({"1": {"repo_root": "/r"}})
    with pytest.raises(ConfigError, match="field='repo_root'"):
        ProjectsConfig.from_mapping({"1": {"name": "x"}})


def test_meta_key_ignored():
    """``_meta`` is reserved for migration bookkeeping and must be skipped."""
    cfg = ProjectsConfig.from_mapping(
        {
            "_meta": {"schema_version": 2},
            "100": {"name": "x", "repo_root": "/r"},
        }
    )
    assert cfg.category_ids() == {100}


def test_channel_ids_reflects_index_only():
    cfg = _sample_cfg()
    assert cfg.channel_ids() == set()
    cfg.register_channel(channel_id=10, channel_name="main", category_id=100)
    assert cfg.channel_ids() == {10}


def test_registered_channel_iter():
    cfg = _sample_cfg()
    cfg.register_channel(channel_id=10, channel_name="main", category_id=100)
    cfg.register_channel(channel_id=11, channel_name="wt-a", category_id=100)
    items = list(cfg)
    assert len(items) == 2
    assert all(isinstance(r, RegisteredChannel) for r in items)
