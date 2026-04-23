"""Git worktree lifecycle manager for Channel-as-Session mode.

Separate from the legacy ``claude_discord/worktree.py`` (``WorktreeManager``)
which serves the thread-based bridge with a different path/branch convention
(``../wt-{thread_id}`` + ``session/{thread_id}``). This manager owns
``{repo_root}/.worktrees/ch-{channel_id}`` + ``channel-session/{channel_id}``.

**Sync API**. Async wrapping is the caller's concern —
``ChannelSessionService`` (step 7) wraps each method in
``asyncio.to_thread`` so the event loop is never blocked by subprocess
I/O or ``git status`` on a large repo.

See ``docs/CHANNEL_AS_SESSION_PHASE1_V3.md`` §7 for the command mapping
table. See step-4 spec (§§4-A…4-H) for TTL caching, dry-run, stderr
classification rules.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level git subprocess wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    """Result of a single ``git`` invocation — always returned, never raised
    (unless ``check=True`` was passed to ``_run_git``, in which case
    ``GitCommandError`` is raised instead).
    """

    returncode: int
    stdout: str
    stderr: str
    args: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitCommandError(RuntimeError):
    """Raised when ``_run_git(..., check=True)`` gets a non-zero exit.

    Message intentionally includes the full command + stderr so logs alone
    are enough to reproduce the failure.
    """

    def __init__(self, result: GitResult) -> None:
        cmd = " ".join(shlex.quote(a) for a in ("git", *result.args))
        super().__init__(
            f"git command failed (exit={result.returncode}): {cmd}\nstderr: {result.stderr.strip()}"
        )
        self.result = result


def _run_git(
    *args: str,
    cwd: str | Path,
    check: bool = False,
    timeout: float = 30.0,
) -> GitResult:
    """Execute ``git *args`` with cwd and return a ``GitResult``.

    Uses ``subprocess.run`` (not ``subprocess.Popen`` with shell) — safe
    against argument injection because the command vector is passed as a
    list. Timeout defaults to 30s which is generous for any single-repo git
    operation.
    """
    completed = subprocess.run(  # noqa: S603 — fixed command, args validated
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = GitResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        args=tuple(args),
    )
    if check and not result.ok:
        raise GitCommandError(result)
    return result


# ---------------------------------------------------------------------------
# stderr → reason classification
# ---------------------------------------------------------------------------

# Ordered: first match wins. Values go into EnsureResult.reason /
# RemovalResult.reason. Use substring-in matching (NOT equality) because git's
# wording drifts across versions.
_GIT_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    # Newer git (>=2.28): "is already used by worktree at"
    # Older git:          "already checked out at"
    ("is already used by worktree", "branch_checked_out_elsewhere"),
    ("already checked out", "branch_checked_out_elsewhere"),
    ("not a git repository", "not_a_git_repo"),
    ("not a working tree", "not_a_git_repo"),
    ("already exists", "path_occupied"),
    ("invalid reference", "invalid_branch_ref"),
    ("cannot lock", "git_lock_contention"),
    ("permission denied", "permission_denied"),
    ("no space left", "disk_full"),
)


def _classify_git_error(stderr: str) -> str:
    """Map git stderr output to a short, stable reason string.

    When no pattern matches, returns ``"git_failed: {first 200 chars}"`` so
    the raw message still surfaces to operators.
    """
    lower = stderr.lower()
    for pattern, reason in _GIT_ERROR_PATTERNS:
        if pattern in lower:
            return reason
    stripped = stderr.strip()
    if not stripped:
        return "git_failed: (no stderr output)"
    return f"git_failed: {stripped[:200]}"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreePaths:
    """Resolved absolute paths for a single channel's worktree."""

    repo_root: str
    worktree_path: str
    branch_name: str
    channel_id: int


@dataclass(frozen=True)
class EnsureResult:
    """Outcome of ``ensure()``.

    ``created`` and ``reused`` are mutually exclusive and only meaningful
    when ``ok=True``. ``planned_commands`` is populated exclusively in
    ``dry_run=True`` mode.
    """

    ok: bool
    worktree_path: str
    branch: str
    created: bool
    reused: bool
    reason: str
    planned_commands: list[str] | None = None


@dataclass(frozen=True)
class RemovalResult:
    """Outcome of ``remove_if_clean()``.

    ``reason`` values:
      * ``"removed"``  — deleted successfully
      * ``"dirty"``    — uncommitted changes; skipped by design
      * ``"not_exists"`` — worktree directory absent
      * ``"would_remove"`` — dry-run
      * or any ``_classify_git_error()`` output on failure
    """

    removed: bool
    reason: str
    path: str
    planned_commands: list[str] | None = None


@dataclass(frozen=True)
class WorktreeInfo:
    """One entry from ``git worktree list --porcelain``."""

    path: str
    head: str | None
    branch: str | None
    is_detached: bool
    is_bare: bool


# ---------------------------------------------------------------------------
# Worktree list parser (pure; isolated for unit testing)
# ---------------------------------------------------------------------------


def _parse_worktree_list(stdout: str) -> list[WorktreeInfo]:
    """Parse ``git worktree list --porcelain`` output into ``WorktreeInfo``.

    Format (blank-line separated blocks, one attr per line)::

        worktree /path/to/wt1
        HEAD abc123...
        branch refs/heads/main

        worktree /path/to/wt2
        HEAD aaa111...
        detached

        worktree /path/to/bare
        bare

    Unknown keys are ignored. Missing blocks (empty input) → empty list.
    Blocks without a ``worktree`` line are dropped defensively.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        current[key] = value
    if current:
        blocks.append(current)

    results: list[WorktreeInfo] = []
    for block in blocks:
        path = block.get("worktree", "")
        if not path:
            continue
        results.append(
            WorktreeInfo(
                path=path,
                head=block.get("HEAD") or None,
                branch=block.get("branch") or None,
                is_detached="detached" in block,
                is_bare="bare" in block,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ChannelWorktreeManager:
    """Create / inspect / remove Channel-as-Session worktrees.

    Construction (no IO)::

        wt = ChannelWorktreeManager(clean_cache_ttl_seconds=5.0)

    All IO methods raise nothing for *expected* git failures — they return
    a result object with ``ok=False`` and a ``reason``. Truly unexpected
    errors (subprocess timeout, OSError on the filesystem) propagate.
    """

    def __init__(self, *, clean_cache_ttl_seconds: float = 5.0) -> None:
        self._cache_ttl = clean_cache_ttl_seconds
        # key: resolved worktree path (str) → (expires_at_monotonic, is_clean)
        self._clean_cache: dict[str, tuple[float, bool]] = {}

    # -- Pure calculation -------------------------------------------------

    @staticmethod
    def plan_paths(
        repo_root: str | Path,
        worktree_base: str,
        branch_prefix: str,
        channel_id: int,
    ) -> WorktreePaths:
        """Compute absolute worktree path and branch name.

        No IO. ``worktree_base`` is interpreted relative to ``repo_root``
        when not absolute (matches projects.json convention like
        ``.worktrees``).
        """
        repo = Path(repo_root).resolve()
        base = Path(worktree_base)
        if not base.is_absolute():
            base = repo / base
        wt_path = (base / f"ch-{channel_id}").resolve()
        branch = f"{branch_prefix}/{channel_id}"
        return WorktreePaths(
            repo_root=str(repo),
            worktree_path=str(wt_path),
            branch_name=branch,
            channel_id=channel_id,
        )

    # -- Existence & cleanliness -----------------------------------------

    def exists(self, paths: WorktreePaths) -> bool:
        """True iff the worktree directory exists *and* looks like a worktree
        (has a ``.git`` file or directory)."""
        wt = Path(paths.worktree_path)
        return wt.is_dir() and (wt / ".git").exists()

    def is_clean(
        self,
        worktree_path: str | Path,
        *,
        bypass_cache: bool = False,
    ) -> bool:
        """Run ``git status --porcelain`` and cache the result for
        ``clean_cache_ttl_seconds``.

        Always call with ``bypass_cache=True`` before destructive ops
        (``/channel-reset``, ``/ch-worktree-cleanup``) to eliminate the
        stale-judgment window.
        """
        key = str(Path(worktree_path).resolve())
        now = time.monotonic()
        if not bypass_cache:
            cached = self._clean_cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        result = self._compute_is_clean(key)
        self._clean_cache[key] = (now + self._cache_ttl, result)
        return result

    @staticmethod
    def _compute_is_clean(worktree_path: str) -> bool:
        result = _run_git("status", "--porcelain", cwd=worktree_path)
        if not result.ok:
            # Cannot determine — treat as dirty to avoid destructive loss.
            logger.warning(
                "is_clean: git status --porcelain failed at %s: %s",
                worktree_path,
                result.stderr.strip(),
            )
            return False
        return not result.stdout.strip()

    def invalidate_cache(self, worktree_path: str | Path) -> None:
        """Drop a cached clean/dirty result. Called automatically after
        ``remove_if_clean`` success; external callers should invoke this
        after bulk file writes they performed themselves."""
        key = str(Path(worktree_path).resolve())
        self._clean_cache.pop(key, None)

    # -- Create / reuse ---------------------------------------------------

    def ensure(
        self,
        paths: WorktreePaths,
        *,
        dry_run: bool = False,
    ) -> EnsureResult:
        """Guarantee the worktree exists at ``paths.worktree_path``.

        Decision tree (matches v3 §7):
            0. Verify ``repo_root`` is a git work tree.
            1. If ``worktree_path`` already exists:
               * with ``.git`` marker → reuse.
               * without            → ``path_occupied_not_worktree``.
            2. Check whether ``branch_name`` already exists:
               * exists → ``git worktree add {path} {branch}`` (no ``-b``).
               * absent → ``git worktree add {path} -b {branch}``.

        Failures are reported via ``ok=False`` with a classified ``reason``;
        callers surface this to the Discord channel.
        """
        planned: list[str] = []

        # 0. repo validation
        repo_root = paths.repo_root
        # Pre-check: repo_root directory must exist. Without this, subprocess
        # raises FileNotFoundError on cwd= which surfaces as an unhandled
        # exception — we want a structured result instead.
        if not Path(repo_root).is_dir():
            if dry_run:
                # In dry-run we still show what would run, but flag the issue.
                planned.append(
                    _format_cmd("git", "-C", repo_root, "rev-parse", "--is-inside-work-tree")
                )
                return EnsureResult(
                    ok=False,
                    worktree_path=paths.worktree_path,
                    branch=paths.branch_name,
                    created=False,
                    reused=False,
                    reason="repo_root_does_not_exist",
                    planned_commands=planned,
                )
            return EnsureResult(
                ok=False,
                worktree_path=paths.worktree_path,
                branch=paths.branch_name,
                created=False,
                reused=False,
                reason="repo_root_does_not_exist",
            )

        if dry_run:
            planned.append(
                _format_cmd("git", "-C", repo_root, "rev-parse", "--is-inside-work-tree")
            )
        else:
            check = _run_git("rev-parse", "--is-inside-work-tree", cwd=repo_root)
            if not check.ok or check.stdout.strip() != "true":
                return EnsureResult(
                    ok=False,
                    worktree_path=paths.worktree_path,
                    branch=paths.branch_name,
                    created=False,
                    reused=False,
                    reason="not_a_git_repo",
                )

        # 1. directory preemption
        wt = Path(paths.worktree_path)
        if wt.exists():
            if (wt / ".git").exists():
                if dry_run:
                    return EnsureResult(
                        ok=True,
                        worktree_path=paths.worktree_path,
                        branch=paths.branch_name,
                        created=False,
                        reused=True,
                        reason="would_reuse",
                        planned_commands=planned,
                    )
                return EnsureResult(
                    ok=True,
                    worktree_path=paths.worktree_path,
                    branch=paths.branch_name,
                    created=False,
                    reused=True,
                    reason="reused",
                )
            return EnsureResult(
                ok=False,
                worktree_path=paths.worktree_path,
                branch=paths.branch_name,
                created=False,
                reused=False,
                reason="path_occupied_not_worktree",
            )

        # 2. branch preemption + 3. worktree add
        if dry_run:
            planned.append(
                _format_cmd(
                    "git",
                    "-C",
                    repo_root,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{paths.branch_name}",
                )
            )
            # In dry-run, we can't tell which branch path will be taken —
            # surface BOTH commands so operators see the real intent.
            planned.append(
                _format_cmd(
                    "git",
                    "-C",
                    repo_root,
                    "worktree",
                    "add",
                    paths.worktree_path,
                    "-b",
                    paths.branch_name,
                    "# OR (if branch already exists):",
                )
            )
            planned.append(
                _format_cmd(
                    "git",
                    "-C",
                    repo_root,
                    "worktree",
                    "add",
                    paths.worktree_path,
                    paths.branch_name,
                )
            )
            return EnsureResult(
                ok=True,
                worktree_path=paths.worktree_path,
                branch=paths.branch_name,
                created=True,
                reused=False,
                reason="would_create",
                planned_commands=planned,
            )

        branch_check = _run_git(
            "rev-parse",
            "--verify",
            f"refs/heads/{paths.branch_name}",
            cwd=repo_root,
        )
        if branch_check.ok:
            add = _run_git(
                "worktree",
                "add",
                paths.worktree_path,
                paths.branch_name,
                cwd=repo_root,
            )
        else:
            add = _run_git(
                "worktree",
                "add",
                paths.worktree_path,
                "-b",
                paths.branch_name,
                cwd=repo_root,
            )

        if add.ok:
            self.invalidate_cache(paths.worktree_path)
            return EnsureResult(
                ok=True,
                worktree_path=paths.worktree_path,
                branch=paths.branch_name,
                created=True,
                reused=False,
                reason="created",
            )
        return EnsureResult(
            ok=False,
            worktree_path=paths.worktree_path,
            branch=paths.branch_name,
            created=False,
            reused=False,
            reason=_classify_git_error(add.stderr),
        )

    # -- Remove (clean-only, dirty-preserving) ----------------------------

    def remove_if_clean(
        self,
        paths: WorktreePaths,
        *,
        dry_run: bool = False,
    ) -> RemovalResult:
        """Remove the worktree iff it has no uncommitted changes.

        Dirty worktrees are *never* auto-removed (design invariant). The
        dirty-check uses ``bypass_cache=True`` so a 5-second-old "clean"
        verdict cannot cause data loss.
        """
        wt = Path(paths.worktree_path)
        if not wt.exists():
            return RemovalResult(
                removed=False,
                reason="not_exists",
                path=paths.worktree_path,
            )

        if not self.is_clean(paths.worktree_path, bypass_cache=True):
            return RemovalResult(
                removed=False,
                reason="dirty",
                path=paths.worktree_path,
            )

        cmd = ("git", "-C", paths.repo_root, "worktree", "remove", paths.worktree_path)
        if dry_run:
            return RemovalResult(
                removed=False,
                reason="would_remove",
                path=paths.worktree_path,
                planned_commands=[_format_cmd(*cmd)],
            )

        result = _run_git("worktree", "remove", paths.worktree_path, cwd=paths.repo_root)
        if result.ok:
            self.invalidate_cache(paths.worktree_path)
            return RemovalResult(
                removed=True,
                reason="removed",
                path=paths.worktree_path,
            )
        return RemovalResult(
            removed=False,
            reason=_classify_git_error(result.stderr),
            path=paths.worktree_path,
        )

    # -- Introspection ---------------------------------------------------

    def list_all(self, repo_root: str | Path) -> list[WorktreeInfo]:
        """Return every worktree associated with ``repo_root`` (includes the
        main worktree itself). Empty list if the command fails."""
        result = _run_git("worktree", "list", "--porcelain", cwd=str(repo_root))
        if not result.ok:
            logger.warning(
                "worktree list failed at %s: %s",
                repo_root,
                result.stderr.strip(),
            )
            return []
        return _parse_worktree_list(result.stdout)


# ---------------------------------------------------------------------------
# Internal: command formatting for dry-run display
# ---------------------------------------------------------------------------


def _format_cmd(*args: str) -> str:
    """Shell-quote a command for operator-friendly display in dry-run output."""
    return " ".join(shlex.quote(a) for a in args)
