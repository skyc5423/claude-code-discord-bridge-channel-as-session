"""Configuration loaders for claude-code-discord-bridge.

Phase-2 schema: category-keyed ``projects.json`` with per-channel
resolution via ``services.channel_naming``.
"""

from __future__ import annotations

from .projects_config import (
    CategoryProjectConfig,
    ConfigError,
    CwdMode,
    ProjectConfig,  # phase-1 alias → CategoryProjectConfig
    ProjectsConfig,
    ProjectsConfigDiff,
    RegisteredChannel,
)

__all__ = [
    "CategoryProjectConfig",
    "ConfigError",
    "CwdMode",
    "ProjectConfig",
    "ProjectsConfig",
    "ProjectsConfigDiff",
    "RegisteredChannel",
]
