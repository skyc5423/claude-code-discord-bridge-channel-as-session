"""Phase-1 → Phase-2 one-shot migrator.

Three concerns (§3 of the design doc):

A. **projects.json**: channel_id-keyed (phase-1) → category_id-keyed (phase-2)
   with ``_meta.schema_version=2`` sentinel. Backup to ``.pre-phase2.bak``.

B. **DB schema**: ALTER channel_sessions ADD channel_name/category_id — handled
   by ``channel_session_models._MIGRATIONS`` + ``init_db``. No action here
   beyond triggering init_db.

C. **DB records**: backfill ``channel_name="main"`` + ``category_id=<lookup>``
   for the 5 phase-1 main channels. Uses the hardcoded Discord map (R1) so
   migration is fully offline.

Runs once at boot. Idempotent: re-running on an already-migrated file is a
no-op. Safe against partial migration — the ``_meta`` sentinel is only
written after projects.json conversion succeeds.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from ..config.projects_config import SCHEMA_VERSION_PHASE2

logger = logging.getLogger(__name__)


# R1: hardcoded phase-1 channel_id → category_id lookup. Avoids needing a
# live Discord connection during migration. Values match the 5 main channels
# registered in the bot's production Discord server at the time of phase-1
# shutdown. Channels NOT in this map are still migrated, but their
# category_id is left NULL (the on_message path will fill it in when a new
# message arrives).
_PHASE1_CHANNEL_TO_CATEGORY: dict[int, int] = {
    1496803518508699798: 1496787663263498332,  # Dalpha-main
    1496803536762175489: 1496787750202900581,  # oi-agent-fnco-chatbot-main
    1496803553484734504: 1496803244478042193,  # fnco-databricks-main
    1496803565858066533: 1496803399575015454,  # dalpha-dynamic-edge-main
    1496803585906708562: 1496803457317994607,  # oi-oliveyoung-crawling-main
}


@dataclass
class MigrationResult:
    projects_json_migrated: bool = False
    db_columns_added: bool = False
    records_backfilled: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.projects_json_migrated:
            parts.append("projects.json=converted")
        if self.db_columns_added:
            parts.append("db_columns=added")
        parts.append(f"records_backfilled={self.records_backfilled}")
        if self.warnings:
            parts.append(f"warnings={len(self.warnings)}")
        return ", ".join(parts) or "no-op"


async def run_if_needed(
    *,
    projects_config_path: str | None,
    channel_session_db_path: str,
) -> MigrationResult:
    """Execute the phase-2 migration, idempotent and offline.

    Args:
        projects_config_path: Path to projects.json. ``None`` skips projects
            conversion entirely (no Channel-as-Session configured).
        channel_session_db_path: Path to ``channel_sessions.db``. Must exist
            OR will be created by ``init_db`` in the caller's chain.

    Returns a ``MigrationResult`` summarising what changed. Never raises for
    "expected" conditions — unknown/novel errors propagate.
    """
    result = MigrationResult()

    if projects_config_path:
        await _migrate_projects_json(projects_config_path, result)
    else:
        result.warnings.append("projects_config_path is None — JSON migration skipped")

    # DB schema ALTER happens elsewhere (init_db on startup). Here we only
    # backfill records for the known 5 phase-1 main channels.
    if Path(channel_session_db_path).exists():
        await _backfill_db_records(channel_session_db_path, result)

    logger.info("Phase-2 migration: %s", result.summary())
    return result


# ---------------------------------------------------------------------------
# A: projects.json conversion
# ---------------------------------------------------------------------------


async def _migrate_projects_json(path_str: str, result: MigrationResult) -> None:
    path = Path(path_str)
    if not path.is_file():
        result.warnings.append(f"projects.json not found at {path} — skipping JSON migration")
        return

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.warnings.append(f"projects.json unreadable: {exc}")
        return

    if not isinstance(raw, dict):
        result.warnings.append("projects.json top level is not an object — skipping")
        return

    meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
    if meta.get("schema_version") == SCHEMA_VERSION_PHASE2:
        logger.debug("projects.json already at schema_version=2 — skipping conversion")
        return

    # Detect phase-1 format: keys are phase-1 channel_ids we know about.
    phase1_ids = [
        k for k in raw if k != "_meta" and k.isdigit() and int(k) in _PHASE1_CHANNEL_TO_CATEGORY
    ]
    if not phase1_ids and not any(k != "_meta" for k in raw):
        # empty or only _meta — add sentinel and exit
        raw["_meta"] = {"schema_version": SCHEMA_VERSION_PHASE2}
        _atomic_write(path, raw)
        result.projects_json_migrated = True
        return

    if not phase1_ids:
        # keys don't match phase-1 IDs — assume it's already phase-2 written
        # by hand. Just stamp the sentinel.
        raw["_meta"] = {"schema_version": SCHEMA_VERSION_PHASE2}
        _atomic_write(path, raw)
        result.projects_json_migrated = True
        result.warnings.append(
            "projects.json keys did not match known phase-1 channels — "
            "assumed to be phase-2 already; only stamped schema_version."
        )
        return

    # Real conversion: channel_id keys → category_id keys.
    backup_path = path.with_suffix(path.suffix + ".pre-phase2.bak")
    shutil.copy2(path, backup_path)
    logger.info("projects.json backed up to %s", backup_path)

    new_cats: dict[int, dict] = {}
    for k in phase1_ids:
        channel_id = int(k)
        category_id = _PHASE1_CHANNEL_TO_CATEGORY[channel_id]
        entry = raw[k]
        if not isinstance(entry, dict):
            result.warnings.append(f"projects.json[{k}] is not an object — dropped")
            continue
        # Phase-1 fields → phase-2 fields (per category).
        # shared_cwd_warning: if ANY main channel for this category had it,
        # the category inherits true.
        existing = new_cats.get(category_id, {})
        cat_entry = {
            "name": entry.get("name", "").replace("-main", "") or f"category-{category_id}",
            "repo_root": entry.get("repo_root", ""),
            "shared_cwd_warning": bool(
                entry.get("shared_cwd_warning") or existing.get("shared_cwd_warning")
            ),
        }
        if entry.get("worktree_base"):
            cat_entry["worktree_base"] = entry["worktree_base"]
        if entry.get("branch_prefix"):
            cat_entry["branch_prefix"] = entry["branch_prefix"]
        if entry.get("model"):
            cat_entry["model"] = entry["model"]
        if entry.get("permission_mode"):
            cat_entry["permission_mode"] = entry["permission_mode"]
        new_cats[category_id] = cat_entry

    new_raw: dict = {"_meta": {"schema_version": SCHEMA_VERSION_PHASE2}}
    for cat_id, cfg in new_cats.items():
        new_raw[str(cat_id)] = cfg
    # Preserve any pre-existing category-keyed entries (not in phase-1 map)
    for k, v in raw.items():
        if k == "_meta":
            continue
        if k.isdigit() and int(k) not in _PHASE1_CHANNEL_TO_CATEGORY and k not in new_raw:
            new_raw[k] = v

    _atomic_write(path, new_raw)
    result.projects_json_migrated = True
    logger.info(
        "projects.json migrated: %d phase-1 channel entries → %d category entries",
        len(phase1_ids),
        len(new_cats),
    )


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# C: DB record backfill
# ---------------------------------------------------------------------------


async def _backfill_db_records(db_path: str, result: MigrationResult) -> None:
    """Backfill ``channel_name`` + ``category_id`` for phase-1 main rows."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT channel_id, channel_name, category_id FROM channel_sessions")
        rows = await cur.fetchall()

        updated = 0
        for row in rows:
            ch_id = int(row["channel_id"])
            # Only fill when missing — never overwrite.
            need_name = row["channel_name"] is None
            need_cat = row["category_id"] is None
            if not (need_name or need_cat):
                continue
            # Phase-1 only registered main channels, so use "main" as the
            # backfill value. If a future phase-1 record had a non-main
            # (shouldn't exist), leave name NULL to be backfilled by the
            # on-message path.
            new_name = "main" if need_name else row["channel_name"]
            new_cat = _PHASE1_CHANNEL_TO_CATEGORY.get(ch_id) if need_cat else row["category_id"]
            await db.execute(
                "UPDATE channel_sessions SET channel_name = ?, category_id = ? "
                "WHERE channel_id = ?",
                (new_name, new_cat, ch_id),
            )
            updated += 1
        await db.commit()
        result.records_backfilled = updated
        if updated:
            logger.info("Phase-2 DB backfill: %d record(s) updated", updated)
