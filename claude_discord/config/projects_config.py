"""projects.json loader for Channel-as-Session mode (phase-2 schema).

Schema (category-keyed)::

    {
      "<category_id_string>": {
        "name":               "display name (not validated)",
        "repo_root":          "/absolute/path",
        "shared_cwd_warning": true/false,     // default false
        "worktree_base":      ".worktrees",   // default
        "branch_prefix":      "channel-session",  // default
        "model":              "sonnet",       // optional
        "permission_mode":    "acceptEdits"   // optional
      },
      ...
    }

- Key: Discord category ID (永久 불변, rename 가능한 name 과 분리)
- ``cwd_mode`` 는 저장되지 않음 — 매 채널마다 이름 패턴으로 동적 결정
  (``services.channel_naming.resolve_channel_name``).

In-memory model
---------------

``ProjectsConfig`` 는 두 개의 인덱스를 유지:

* ``_categories`` — ``category_id → CategoryProjectConfig`` (projects.json 의 fact)
* ``_channel_index`` — ``channel_id → RegisteredChannel`` (Discord 이벤트 +
  startup scan 으로 채워짐. 런타임에 변경)

페이즈 1 호환
-------------

페이즈 1 의 ``projects.get(channel_id)`` / ``projects.has(channel_id)`` /
``projects.channel_ids()`` 시그니처는 그대로 유지 — 단 ``get`` 의 반환 타입이
``ProjectConfig`` → ``RegisteredChannel`` 로 바뀌었으므로 호출부는 필드 접근
경로를 업데이트해야 함 (예: ``project.cwd_mode`` → ``registered.cwd_mode``,
``project.shared_cwd_warning`` → ``registered.shared_cwd_warning``).

See ``docs/CHANNEL_AS_SESSION_PHASE2.md`` §2 for the full design.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public type aliases and constants
# ---------------------------------------------------------------------------

CwdMode = Literal["repo_root", "dedicated_worktree"]
_VALID_CWD_MODES: frozenset[str] = frozenset({"repo_root", "dedicated_worktree"})

_DEFAULT_WORKTREE_BASE = ".worktrees"
_DEFAULT_BRANCH_PREFIX = "channel-session"

_META_KEY = "_meta"
SCHEMA_VERSION_PHASE2 = 2


class ConfigError(ValueError):
    """Raised when projects.json has a schema or value error.

    Message always includes enough context (category_id or field name) to
    locate the problem in the source file.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryProjectConfig:
    """One projects.json entry — one category = one repo = one project.

    cwd_mode is NOT stored; it's resolved per-channel at runtime based on
    the channel's name (``main`` vs ``wt-<slug>``).
    """

    category_id: int
    name: str
    repo_root: str
    shared_cwd_warning: bool = False
    worktree_base: str = _DEFAULT_WORKTREE_BASE
    branch_prefix: str = _DEFAULT_BRANCH_PREFIX
    model: str | None = None
    permission_mode: str | None = None


@dataclass(frozen=True)
class RegisteredChannel:
    """A resolved Discord channel — what callers care about at runtime.

    Replaces phase-1's ``ProjectConfig`` for per-channel consumers.
    """

    channel_id: int
    channel_name: str  # e.g. "main" or "wt-feat-auth"
    category_id: int
    cwd_mode: CwdMode
    slug: str | None  # None iff cwd_mode == "repo_root"
    worktree_path: str | None  # computed lazily; None for repo_root
    branch_name: str | None  # None for repo_root
    project: CategoryProjectConfig

    @property
    def shared_cwd_warning(self) -> bool:
        """True iff this channel should get the shared-cwd system-prompt warning.

        Only meaningful for repo_root mode (dedicated worktrees are isolated
        so no shared-cwd concern).
        """
        return self.cwd_mode == "repo_root" and self.project.shared_cwd_warning

    @property
    def repo_root(self) -> str:
        """Convenience accessor for ``project.repo_root``."""
        return self.project.repo_root

    @property
    def uses_dedicated_worktree(self) -> bool:
        """Phase-1 compatibility property — for /ch-worktree-list iteration."""
        return self.cwd_mode == "dedicated_worktree"


@dataclass(frozen=True)
class ProjectsConfigDiff:
    """Delta between two ProjectsConfig instances (used by hot-reload)."""

    added: set[int]  # category_ids newly added
    removed: set[int]  # category_ids dropped
    changed: set[int]  # category_ids with config field changes

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


# ---------------------------------------------------------------------------
# ProjectsConfig
# ---------------------------------------------------------------------------


class ProjectsConfig:
    """Loaded projects.json + in-memory channel registration index.

    Mutable state (``_channel_index``) is populated by:
      * ``ChannelSessionCog._startup_scan`` at bot boot
      * ``on_guild_channel_create`` / ``on_guild_channel_update`` events
      * Hot-reload via ``replace_categories``

    Thread-safety: discord.py single-loops the cog so no locks needed.
    """

    def __init__(
        self,
        categories: Mapping[int, CategoryProjectConfig] | None = None,
        source_path: str | None = None,
    ) -> None:
        self._categories: dict[int, CategoryProjectConfig] = dict(categories or {})
        self._channel_index: dict[int, RegisteredChannel] = {}
        self.source_path = source_path

    # -- Category API (projects.json fact) -------------------------------

    def has_category(self, category_id: int) -> bool:
        return category_id in self._categories

    def get_category(self, category_id: int) -> CategoryProjectConfig | None:
        return self._categories.get(category_id)

    def category_ids(self) -> set[int]:
        return set(self._categories.keys())

    def categories(self) -> Iterable[CategoryProjectConfig]:
        return self._categories.values()

    # -- Channel API (runtime registration index) ------------------------

    def has(self, channel_id: int) -> bool:
        """True when the channel is currently registered.

        Phase-1 compatibility shim. Backed by the in-memory ``_channel_index``
        (populated by startup scan + Discord events), NOT by projects.json
        directly.
        """
        return channel_id in self._channel_index

    def get(self, channel_id: int) -> RegisteredChannel | None:
        """Return the registered channel info, or None when unregistered.

        Phase-1 compat: signature unchanged, return type now
        ``RegisteredChannel`` instead of ``ProjectConfig``.
        """
        return self._channel_index.get(channel_id)

    def channel_ids(self) -> set[int]:
        return set(self._channel_index.keys())

    def registered_channels(self) -> Iterable[RegisteredChannel]:
        return self._channel_index.values()

    # -- Registration mutators (called from Cog / startup scan) ----------

    def register_channel(
        self,
        *,
        channel_id: int,
        channel_name: str,
        category_id: int,
    ) -> RegisteredChannel | None:
        """Register or replace a channel in the index.

        Returns the resulting ``RegisteredChannel`` or ``None`` if:
          * ``category_id`` is not in ``_categories`` (unregistered category)
          * the channel name fails ``resolve_channel_name`` (doesn't match
            ``main`` / ``wt-<slug>``)

        The path/branch are computed deterministically — no IO here. git
        worktree creation happens lazily in ``handle_message``.
        """
        from ..services.channel_naming import (
            branch_name as _branch,
        )
        from ..services.channel_naming import (
            resolve_channel_name,
        )

        project = self._categories.get(category_id)
        if project is None:
            return None
        resolved = resolve_channel_name(channel_name)
        if resolved is None:
            return None

        if resolved.cwd_mode == "dedicated_worktree":
            assert resolved.slug is not None
            slug = resolved.slug
            base = Path(project.worktree_base)
            if not base.is_absolute():
                base = Path(project.repo_root) / base
            wt_path: str | None = str((base / f"ch-{slug}").resolve())
            br_name: str | None = _branch(project.branch_prefix, slug)
        else:
            slug = None
            wt_path = None
            br_name = None

        reg = RegisteredChannel(
            channel_id=channel_id,
            channel_name=channel_name,
            category_id=category_id,
            cwd_mode=resolved.cwd_mode,
            slug=slug,
            worktree_path=wt_path,
            branch_name=br_name,
            project=project,
        )
        self._channel_index[channel_id] = reg
        return reg

    def unregister_channel(self, channel_id: int) -> RegisteredChannel | None:
        """Remove a channel from the index. NO DB side effect (see R2)."""
        return self._channel_index.pop(channel_id, None)

    def replace_categories(
        self,
        new_categories: Mapping[int, CategoryProjectConfig],
    ) -> ProjectsConfigDiff:
        """Atomically swap the categories dict and compute the diff.

        Used by hot-reload. Channels that now belong to removed categories
        are automatically unregistered. Channels in changed categories are
        re-registered so they pick up the new project config.
        """
        old_ids = set(self._categories.keys())
        new_ids = set(new_categories.keys())
        added = new_ids - old_ids
        removed = old_ids - new_ids

        changed: set[int] = set()
        for cid in old_ids & new_ids:
            if self._categories[cid] != new_categories[cid]:
                changed.add(cid)

        self._categories = dict(new_categories)

        # Re-register every indexed channel against the new categories.
        old_index = dict(self._channel_index)
        self._channel_index.clear()
        for ch_id, old_reg in old_index.items():
            if old_reg.category_id not in new_ids:
                continue  # category removed → channel dropped
            self.register_channel(
                channel_id=ch_id,
                channel_name=old_reg.channel_name,
                category_id=old_reg.category_id,
            )

        return ProjectsConfigDiff(added=added, removed=removed, changed=changed)

    # -- Dict-like convenience -------------------------------------------

    def __iter__(self) -> Iterator[RegisteredChannel]:
        return iter(self._channel_index.values())

    def __len__(self) -> int:
        return len(self._channel_index)

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self._channel_index

    # -- Loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> ProjectsConfig:
        """Load and validate a projects.json file."""
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"projects.json: file not found: {p}") from exc
        except OSError as exc:
            raise ConfigError(f"projects.json: cannot read {p}: {exc}") from exc

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"projects.json: JSON parse error at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}"
            ) from exc

        return cls.from_mapping(raw, source_path=str(p))

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        source_path: str | None = None,
    ) -> ProjectsConfig:
        if not isinstance(raw, dict):
            raise ConfigError(
                f"projects.json: top-level value must be an object (got {type(raw).__name__})"
            )

        categories: dict[int, CategoryProjectConfig] = {}
        repo_roots: dict[str, list[int]] = {}

        for key, value in raw.items():
            if key == _META_KEY:
                # Reserved for migration bookkeeping. Ignored here.
                continue
            category_id = _parse_category_id(key)
            cfg = _parse_category(category_id, value)
            categories[category_id] = cfg
            repo_roots.setdefault(cfg.repo_root, []).append(category_id)

        for repo_root, cids in repo_roots.items():
            if len(cids) > 1:
                logger.info(
                    "projects.json: repo_root %s is shared by %d categories: %s",
                    repo_root,
                    len(cids),
                    cids,
                )

        return cls(categories=categories, source_path=source_path)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_category_id(key: Any) -> int:
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        s = key.strip()
        if not s:
            raise ConfigError("projects.json: category_id key is an empty string")
        if not (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            raise ConfigError(f"projects.json: category_id key must be numeric, got {key!r}")
        try:
            return int(s)
        except ValueError as exc:
            raise ConfigError(
                f"projects.json: category_id key {key!r} is not a valid integer"
            ) from exc
    raise ConfigError(
        f"projects.json: category_id key must be a string or int, got {type(key).__name__}"
    )


def _require_str(
    category_id: int,
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"projects.json[category_id={category_id}, field={field_name!r}]: "
            f"must be a string (got {type(value).__name__})"
        )
    if not allow_empty and not value.strip():
        raise ConfigError(
            f"projects.json[category_id={category_id}, field={field_name!r}]: "
            "must be a non-empty string"
        )
    return value


def _optional_str(category_id: int, value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_str(category_id, value, field_name)


def _optional_bool(category_id: int, value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(
            f"projects.json[category_id={category_id}, field={field_name!r}]: "
            f"must be a boolean (got {type(value).__name__})"
        )
    return value


def _parse_category(category_id: int, value: Any) -> CategoryProjectConfig:
    if not isinstance(value, dict):
        raise ConfigError(
            f"projects.json[category_id={category_id}]: "
            f"value must be an object (got {type(value).__name__})"
        )

    name = _require_str(category_id, value.get("name"), "name")
    repo_root = _require_str(category_id, value.get("repo_root"), "repo_root")

    raw_shared = _optional_bool(category_id, value.get("shared_cwd_warning"), "shared_cwd_warning")
    shared = bool(raw_shared)

    worktree_base = _optional_str(category_id, value.get("worktree_base"), "worktree_base")
    branch_prefix = _optional_str(category_id, value.get("branch_prefix"), "branch_prefix")

    model = _optional_str(category_id, value.get("model"), "model")
    permission_mode = _optional_str(category_id, value.get("permission_mode"), "permission_mode")

    known = {
        "name",
        "repo_root",
        "shared_cwd_warning",
        "worktree_base",
        "branch_prefix",
        "model",
        "permission_mode",
    }
    unknown = set(value.keys()) - known
    if unknown:
        logger.warning(
            "projects.json[category_id=%d]: unknown field(s) %s — ignored.",
            category_id,
            sorted(unknown),
        )

    return CategoryProjectConfig(
        category_id=category_id,
        name=name,
        repo_root=repo_root,
        shared_cwd_warning=shared,
        worktree_base=worktree_base or _DEFAULT_WORKTREE_BASE,
        branch_prefix=branch_prefix or _DEFAULT_BRANCH_PREFIX,
        model=model,
        permission_mode=permission_mode,
    )


# ---------------------------------------------------------------------------
# Phase-1 compatibility shim — ProjectConfig alias
# ---------------------------------------------------------------------------

# Phase-1 had a ``ProjectConfig`` dataclass keyed by channel_id. Phase-2
# replaces it with ``CategoryProjectConfig`` (keyed by category_id) + the
# per-channel ``RegisteredChannel``. For third-party code still importing
# the old name, we re-export ``CategoryProjectConfig`` as ``ProjectConfig``.
# Field set is different; callers MUST migrate.
ProjectConfig = CategoryProjectConfig

__all__ = [
    "CategoryProjectConfig",
    "ConfigError",
    "CwdMode",
    "ProjectConfig",  # phase-1 alias
    "ProjectsConfig",
    "ProjectsConfigDiff",
    "RegisteredChannel",
    "SCHEMA_VERSION_PHASE2",
    "_DEFAULT_BRANCH_PREFIX",
    "_DEFAULT_WORKTREE_BASE",
]
