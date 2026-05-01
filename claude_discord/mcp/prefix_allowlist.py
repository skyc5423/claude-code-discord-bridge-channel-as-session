"""Bash command safety evaluation for the MCP approval broker.

Implements the three-tier evaluation defined in design doc §R3:

    1. auto_deny_patterns  — immediate DENY
    2. shell metachar check — PROMPT (bypass allowlist for complex commands)
    3. safe_prefixes match  — ALLOW for simple, well-known safe commands
    4. fallback             — PROMPT

The entry point for tool-agnostic dispatch is ``evaluate_tool()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Public enum
# ---------------------------------------------------------------------------


class Decision(Enum):
    """The verdict returned by evaluate_bash() / evaluate_tool()."""

    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SAFE_PREFIXES: tuple[str, ...] = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "wc",
    "git status",
    "git diff",
    "git log",
    "git branch",
    "uv run pytest",
    "uv run ruff",
    "python --version",
    "node --version",
)

DEFAULT_AUTO_DENY_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "chmod 777",
    "curl | sh",
    "curl | bash",
    "wget | sh",
)

# ---------------------------------------------------------------------------
# Shell metachar regex
# ---------------------------------------------------------------------------

# Matches any of the shell metachar sequences that indicate a complex command:
#   &&  ||  ;  |  >  <  >>  `  $(  $((  & (standalone trailing)
#
# Notes:
# - Ordered so longer sequences are tried first (>> before >, $( before $).
# - The standalone & is matched separately with a negative-lookahead to avoid
#   false-positives on $(...) which is already matched via $(. We also avoid
#   double-matching && by anchoring & only when not preceded by another &.
_METACHAR_RE = re.compile(r"&&|\|\||>>|>|<|\||\`|\$\(\(|\$\(|(?<![&])&(?![&])|;")

# ---------------------------------------------------------------------------
# Policy dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalPolicy:
    """Configures which commands are auto-allowed, auto-denied, or prompted.

    Fields default to the project-wide safe lists so callers only need to
    override when they have project-specific requirements.
    """

    safe_prefixes: tuple[str, ...] = field(default=DEFAULT_SAFE_PREFIXES)
    auto_deny_patterns: tuple[str, ...] = field(default=DEFAULT_AUTO_DENY_PATTERNS)


# ---------------------------------------------------------------------------
# Public evaluation functions
# ---------------------------------------------------------------------------


def evaluate_bash(command: str, policy: ApprovalPolicy) -> Decision:
    """Evaluate a Bash command string against *policy*.

    Evaluation order (per §R3):
    1. auto_deny_patterns substring match → DENY  (highest priority)
    2. metachar present → PROMPT           (skip allowlist for complex cmds)
    3. safe_prefix match → ALLOW
    4. fallback → PROMPT

    Args:
        command: The raw Bash command string from the MCP tool input.
        policy: The :class:`ApprovalPolicy` to evaluate against.

    Returns:
        :class:`Decision` enum value.
    """
    if not command:
        return Decision.PROMPT

    # Step 1: auto-deny patterns (substring match, case-sensitive)
    for pattern in policy.auto_deny_patterns:
        if pattern in command:
            return Decision.DENY

    # Step 2: shell metachar — bypass allowlist, go straight to PROMPT
    if _METACHAR_RE.search(command):
        return Decision.PROMPT

    # Step 3: prefix allowlist — command must start with prefix, followed by
    # end-of-string or whitespace (prevents "lsfoo" matching "ls")
    stripped = command.lstrip()
    for prefix in policy.safe_prefixes:
        if stripped == prefix or stripped.startswith(prefix + " "):
            return Decision.ALLOW

    # Step 4: fallback
    return Decision.PROMPT


# Read-only tool names that are always safe regardless of input.
_READ_ONLY_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep"})


def evaluate_tool(
    tool_name: str,
    tool_input: dict,
    policy: ApprovalPolicy,
) -> Decision:
    """Top-level tool evaluation dispatching to per-tool logic.

    Args:
        tool_name: The Claude Code tool name (e.g. ``"Bash"``, ``"Read"``).
        tool_input: The raw tool input dict from the MCP request.
        policy: Approval policy to use for the evaluation.

    Returns:
        :class:`Decision` — ALLOW, DENY, or PROMPT.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        return evaluate_bash(command, policy)

    if tool_name in _READ_ONLY_TOOLS:
        # Read-only built-ins are unconditionally safe.
        return Decision.ALLOW

    return Decision.PROMPT
