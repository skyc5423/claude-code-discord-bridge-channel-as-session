"""Configuration loaders for claude-code-discord-bridge.

Currently exposes the Channel-as-Session project config loader.
"""

from __future__ import annotations

from .projects_config import (
    ConfigError,
    CwdMode,
    ProjectConfig,
    ProjectsConfig,
)

__all__ = [
    "ConfigError",
    "CwdMode",
    "ProjectConfig",
    "ProjectsConfig",
]
