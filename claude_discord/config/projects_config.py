"""projects.json loader for Channel-as-Session mode.

Reads a JSON file mapping Discord channel IDs to project configurations.
The loader is fail-fast: any schema violation raises ``ConfigError`` with a
precise location (``channel_id`` and field name) so misconfigurations are
caught at bot startup rather than at message-handling time.

Schema::

    {
      "<channel_id_string>": {
        "name":                "project name",
        "repo_root":           "/absolute/path",
        "cwd_mode":            "repo_root" | "dedicated_worktree",
        "shared_cwd_warning":  true | false,
        "worktree_base":       ".worktrees",
        "branch_prefix":       "channel-session",
        "model":               "sonnet" | "opus" | ...,
        "permission_mode":     "acceptEdits" | "default" | ...
      },
      ...
    }

Only ``name`` and ``repo_root`` are required. All other fields have
sensible defaults. See ``CHANNEL_AS_SESSION_PHASE1_V3.md`` §3 for full
validation rules.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public type aliases and constants
# ---------------------------------------------------------------------------

CwdMode = Literal["repo_root", "dedicated_worktree"]
_VALID_CWD_MODES: frozenset[str] = frozenset({"repo_root", "dedicated_worktree"})

_DEFAULT_CWD_MODE: CwdMode = "dedicated_worktree"
_DEFAULT_WORKTREE_BASE = ".worktrees"
_DEFAULT_BRANCH_PREFIX = "channel-session"


class ConfigError(ValueError):
    """Raised when projects.json has a schema or value error.

    The message always includes enough context (channel_id, field name) to
    locate the problem in the source file.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectConfig:
    """Configuration for a single Channel-as-Session project.

    Fields are normalised at load time:
      - ``cwd_mode`` defaults to ``"dedicated_worktree"``.
      - When ``cwd_mode == "repo_root"``, ``worktree_base`` / ``branch_prefix``
        are forced back to defaults (user-supplied values are ignored with a
        warning log).
      - When ``cwd_mode == "dedicated_worktree"``, ``shared_cwd_warning`` is
        forced to ``False`` (warning log).
    """

    channel_id: int
    name: str
    repo_root: str
    cwd_mode: CwdMode = _DEFAULT_CWD_MODE
    shared_cwd_warning: bool = False
    worktree_base: str = _DEFAULT_WORKTREE_BASE
    branch_prefix: str = _DEFAULT_BRANCH_PREFIX
    model: str | None = None
    permission_mode: str | None = None

    @property
    def uses_dedicated_worktree(self) -> bool:
        """True when this project creates a per-channel git worktree."""
        return self.cwd_mode == "dedicated_worktree"


@dataclass(frozen=True)
class ProjectsConfig:
    """Loaded projects.json — a keyed collection of ``ProjectConfig``."""

    projects: Mapping[int, ProjectConfig] = field(default_factory=dict)
    source_path: str | None = None

    # -- accessors --------------------------------------------------------

    def get(self, channel_id: int) -> ProjectConfig | None:
        """Return the project for *channel_id*, or ``None`` if unregistered."""
        return self.projects.get(channel_id)

    def has(self, channel_id: int) -> bool:
        """True if *channel_id* is registered."""
        return channel_id in self.projects

    def channel_ids(self) -> set[int]:
        """Return the set of registered channel IDs."""
        return set(self.projects.keys())

    def values(self) -> Iterable[ProjectConfig]:
        """Iterate over all project configs."""
        return self.projects.values()

    def __iter__(self) -> Iterator[ProjectConfig]:
        return iter(self.projects.values())

    def __len__(self) -> int:
        return len(self.projects)

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self.projects

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> ProjectsConfig:
        """Load and validate a projects.json file.

        Raises ``ConfigError`` on any schema violation. The exception message
        identifies the offending channel_id and field when possible.
        """
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
        """Validate an already-parsed mapping and build a ``ProjectsConfig``.

        Useful for tests that want to skip disk I/O.
        """
        if not isinstance(raw, dict):
            raise ConfigError(
                f"projects.json: top-level value must be an object (got {type(raw).__name__})"
            )

        projects: dict[int, ProjectConfig] = {}
        repo_roots: dict[str, list[int]] = {}

        for key, value in raw.items():
            channel_id = _parse_channel_id(key)
            project = _parse_project(channel_id, value)
            projects[channel_id] = project
            repo_roots.setdefault(project.repo_root, []).append(channel_id)

        # Same-repo_root sharing is allowed (explicit operator choice), but log.
        for repo_root, cids in repo_roots.items():
            if len(cids) > 1:
                logger.info(
                    "projects.json: repo_root %s is shared by %d channels: %s",
                    repo_root,
                    len(cids),
                    cids,
                )

        return cls(projects=projects, source_path=source_path)


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _parse_channel_id(key: Any) -> int:
    """Convert a dict key to an int channel_id.

    JSON object keys are always strings, but we accept ints too (e.g. when
    callers build the dict programmatically).
    """
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        s = key.strip()
        if not s:
            raise ConfigError("projects.json: channel_id key is an empty string")
        if not (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            raise ConfigError(f"projects.json: channel_id key must be numeric, got {key!r}")
        try:
            return int(s)
        except ValueError as exc:
            raise ConfigError(
                f"projects.json: channel_id key {key!r} is not a valid integer"
            ) from exc
    raise ConfigError(
        f"projects.json: channel_id key must be a string or int, got {type(key).__name__}"
    )


def _require_str(
    channel_id: int,
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"projects.json[channel_id={channel_id}, field={field_name!r}]: "
            f"must be a string (got {type(value).__name__})"
        )
    if not allow_empty and not value.strip():
        raise ConfigError(
            f"projects.json[channel_id={channel_id}, field={field_name!r}]: "
            "must be a non-empty string"
        )
    return value


def _optional_str(
    channel_id: int,
    value: Any,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _require_str(channel_id, value, field_name)


def _optional_bool(
    channel_id: int,
    value: Any,
    field_name: str,
) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(
            f"projects.json[channel_id={channel_id}, field={field_name!r}]: "
            f"must be a boolean (got {type(value).__name__})"
        )
    return value


def _parse_cwd_mode(channel_id: int, value: Any) -> CwdMode:
    if value is None:
        return _DEFAULT_CWD_MODE
    if not isinstance(value, str):
        raise ConfigError(
            f"projects.json[channel_id={channel_id}, field='cwd_mode']: "
            f"must be a string (got {type(value).__name__})"
        )
    if value not in _VALID_CWD_MODES:
        raise ConfigError(
            f"projects.json[channel_id={channel_id}, field='cwd_mode']: "
            f"must be one of {sorted(_VALID_CWD_MODES)}, got {value!r}"
        )
    return value  # type: ignore[return-value]


def _parse_project(channel_id: int, value: Any) -> ProjectConfig:
    """Validate and normalise a single project entry."""
    if not isinstance(value, dict):
        raise ConfigError(
            f"projects.json[channel_id={channel_id}]: "
            f"value must be an object (got {type(value).__name__})"
        )

    # Required fields.
    name = _require_str(channel_id, value.get("name"), "name")
    repo_root = _require_str(channel_id, value.get("repo_root"), "repo_root")

    # cwd_mode — optional, defaults to dedicated_worktree.
    cwd_mode = _parse_cwd_mode(channel_id, value.get("cwd_mode"))

    # shared_cwd_warning — optional, defaults to False.
    raw_shared = _optional_bool(channel_id, value.get("shared_cwd_warning"), "shared_cwd_warning")
    shared_cwd_warning = bool(raw_shared)  # None → False

    # worktree_base / branch_prefix — optional strings with defaults.
    raw_worktree_base = _optional_str(channel_id, value.get("worktree_base"), "worktree_base")
    raw_branch_prefix = _optional_str(channel_id, value.get("branch_prefix"), "branch_prefix")

    # Optional passthroughs.
    model = _optional_str(channel_id, value.get("model"), "model")
    permission_mode = _optional_str(channel_id, value.get("permission_mode"), "permission_mode")

    # Mode-specific normalisation: warn + override when fields don't match mode.
    if cwd_mode == "repo_root":
        if raw_worktree_base is not None:
            logger.warning(
                "projects.json[channel_id=%d]: cwd_mode='repo_root' ignores "
                "'worktree_base' — value %r dropped.",
                channel_id,
                raw_worktree_base,
            )
        if raw_branch_prefix is not None:
            logger.warning(
                "projects.json[channel_id=%d]: cwd_mode='repo_root' ignores "
                "'branch_prefix' — value %r dropped.",
                channel_id,
                raw_branch_prefix,
            )
        effective_worktree_base = _DEFAULT_WORKTREE_BASE
        effective_branch_prefix = _DEFAULT_BRANCH_PREFIX
    else:  # "dedicated_worktree"
        if shared_cwd_warning:
            logger.warning(
                "projects.json[channel_id=%d]: cwd_mode='dedicated_worktree' "
                "ignores 'shared_cwd_warning' — forced to False.",
                channel_id,
            )
            shared_cwd_warning = False
        effective_worktree_base = raw_worktree_base or _DEFAULT_WORKTREE_BASE
        effective_branch_prefix = raw_branch_prefix or _DEFAULT_BRANCH_PREFIX

    # Surface unknown fields so typos don't fail silently.
    known = {
        "name",
        "repo_root",
        "cwd_mode",
        "shared_cwd_warning",
        "worktree_base",
        "branch_prefix",
        "model",
        "permission_mode",
    }
    unknown = set(value.keys()) - known
    if unknown:
        logger.warning(
            "projects.json[channel_id=%d]: unknown field(s) %s — ignored.",
            channel_id,
            sorted(unknown),
        )

    return ProjectConfig(
        channel_id=channel_id,
        name=name,
        repo_root=repo_root,
        cwd_mode=cwd_mode,
        shared_cwd_warning=shared_cwd_warning,
        worktree_base=effective_worktree_base,
        branch_prefix=effective_branch_prefix,
        model=model,
        permission_mode=permission_mode,
    )
