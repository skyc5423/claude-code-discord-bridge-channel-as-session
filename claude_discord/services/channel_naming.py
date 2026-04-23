"""Channel name → cwd_mode + slug resolver (pure, stateless).

The regex constants here are the single source of truth shared between
code and documentation (``docs/channel_as_session.md``). If you change
them, update both places.

Pattern rules (strict):
    * ``main`` → repo_root (shared cwd with cron jobs, etc.)
    * ``wt-<slug>`` where ``<slug>`` is ``[a-z0-9][a-z0-9_-]*`` → dedicated worktree
    * anything else → None (channel is silently ignored by ccdb)

The slug regex is intentionally restrictive so that invalid git refname
characters never leak into branch names. Capital letters, leading dashes,
empty slugs are all rejected.

See ``docs/CHANNEL_AS_SESSION_PHASE2.md`` §5 and §6 for the authoritative
spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config.projects_config import CwdMode

# ---------------------------------------------------------------------------
# Public regex constants — shared with docs/channel_as_session.md
# ---------------------------------------------------------------------------

MAIN_CHANNEL_PATTERN = re.compile(r"^main$")
"""Matches the single ``main`` channel name. Case-sensitive by design."""

WORKTREE_CHANNEL_PATTERN = re.compile(r"^wt-([a-z0-9][a-z0-9_-]*)$")
"""Matches ``wt-<slug>`` channel names; capture group 1 is the slug.

Slug rules:
    * lowercase letters, digits, underscore, hyphen only
    * must start with [a-z0-9] (no leading ``-`` or ``_``)
    * minimum length 1 after ``wt-``
"""


@dataclass(frozen=True)
class ResolvedChannelName:
    """Outcome of ``resolve_channel_name``.

    ``slug`` is ``None`` iff ``cwd_mode == "repo_root"``.
    """

    cwd_mode: CwdMode
    slug: str | None


def resolve_channel_name(name: str) -> ResolvedChannelName | None:
    """Classify a Discord channel name against the naming rules.

    Returns ``None`` for any non-matching name — ccdb silently ignores
    such channels. Callers MUST treat ``None`` as "not ours" and skip
    registration / processing.

    Examples::

        resolve_channel_name("main")            # repo_root
        resolve_channel_name("wt-feat-auth")    # dedicated_worktree, slug=feat-auth
        resolve_channel_name("wt-Bug123")       # None (capital letter)
        resolve_channel_name("wt-")             # None (empty slug)
        resolve_channel_name("notes")           # None (unknown pattern)
    """
    if MAIN_CHANNEL_PATTERN.match(name):
        return ResolvedChannelName(cwd_mode="repo_root", slug=None)
    m = WORKTREE_CHANNEL_PATTERN.match(name)
    if m:
        return ResolvedChannelName(cwd_mode="dedicated_worktree", slug=m.group(1))
    return None


def branch_name(branch_prefix: str, slug: str) -> str:
    """Combine project's ``branch_prefix`` with a resolved slug.

    Example::

        branch_name("channel-session", "feat-auth")  # → "channel-session/feat-auth"

    Slug sanitation already happens in ``WORKTREE_CHANNEL_PATTERN``. Callers
    must NOT pass raw Discord channel names here — always pass the
    ``slug`` field from ``ResolvedChannelName``.
    """
    return f"{branch_prefix}/{slug}"
