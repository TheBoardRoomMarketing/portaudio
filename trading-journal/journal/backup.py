"""Backup, restore and export.

The journal is longitudinal data whose value grows with age and cannot be
reconstructed — a lost check-in is lost evidence about a day that will not
happen again. So a backup is not a copy of the database; it is a copy of
everything needed to rebuild the database, plus a manifest that proves what was
in it.

A backup nobody has restored is a hypothesis. `verify_backup` restores into a
scratch location and checks the row counts match the manifest.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db
from .db import sha256_file, utcnow

MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = "journal.db"

# Tables whose counts go in the manifest. A restore that loses rows in any of
# these is a failed restore, not a partial success.
COUNTED_TABLES = (
    "session", "checkin_pre", "checkin_post", "checkin_amendment", "trade",
    "trade_fill", "raw_record", "raw_import_batch", "opportunity", "human_tag",
    "voice_note", "media_asset", "ai_annotation", "blinded_feature",
    "strategy_version", "import_profile",
)


def _counts(conn) -> dict:
    out = {}
    for table in COUNTED_TABLES:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        except sqlite3.DatabaseError:
            out[table] = None
    return out


def create(conn, *, label: str = "", include_media: bool = True,
           dest_dir: Optional[Path] = None) -> Path:
    """Write a self-describing backup archive.

    Contents: a consistent database snapshot, the raw imports, the media, the
    migration state, and a manifest with per-file hashes and row counts.
    """
    dest_dir = Path(dest_dir or config.BACKUP_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    name = f"journal-{stamp}{('-' + label) if label else ''}"

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / name
        staging.mkdir(parents=True)

        snapshot_path = db.snapshot(conn, staging / SNAPSHOT_NAME)

        copied = {}
        if include_media:
            for source_dir in (config.RAW_DIR, config.MEDIA_DIR):
                if source_dir.exists() and any(source_dir.iterdir()):
                    target = staging / source_dir.name
                    shutil.copytree(source_dir, target)
                    copied[source_dir.name] = sum(1 for _ in target.rglob("*") if _.is_file())

        manifest = {
            "created_at": utcnow(),
            "label": label,
            "schema": db.schema_state(conn),
            "calc_version": config.CALC_VERSION,
            "row_counts": _counts(conn),
            "files": {
                SNAPSHOT_NAME: {
                    "sha256": sha256_file(snapshot_path),
                    "bytes": snapshot_path.stat().st_size,
                }
            },
            "directories": copied,
            "restore_hint": "journal restore <archive>  — verifies integrity before replacing",
        }
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))

        archive = dest_dir / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=name)

    return archive


def _extract(archive: Path, into: Path) -> Path:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            target = (into / m.name).resolve()
            if not str(target).startswith(str(into.resolve())):
                raise RuntimeError(f"refusing path traversal in archive: {m.name}")
        tar.extractall(into)
    roots = [p for p in into.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError(f"expected one directory in {archive.name}, found {len(roots)}")
    return roots[0]


def read_manifest(archive: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = _extract(Path(archive), Path(tmp))
        return json.loads((root / MANIFEST_NAME).read_text())


def verify(archive: Path) -> dict:
    """Restore into a scratch directory and check it against its own manifest."""
    archive = Path(archive)
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        root = _extract(archive, Path(tmp))
        manifest = json.loads((root / MANIFEST_NAME).read_text())

        snapshot = root / SNAPSHOT_NAME
        recorded = manifest["files"][SNAPSHOT_NAME]["sha256"]
        actual = sha256_file(snapshot)
        if recorded != actual:
            problems.append(f"snapshot hash mismatch: {recorded[:12]} vs {actual[:12]}")

        conn = sqlite3.connect(str(snapshot))
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                problems.append("restored snapshot fails integrity_check")
            for table, expected in manifest["row_counts"].items():
                if expected is None:
                    continue
                found = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                if found != expected:
                    problems.append(f"{table}: manifest {expected}, restored {found}")
        finally:
            conn.close()

    return {"ok": not problems, "archive": str(archive), "problems": problems,
            "created_at": manifest.get("created_at"),
            "schema": manifest.get("schema", {}).get("current")}


def restore(archive: Path, db_path: Optional[Path] = None, *,
            restore_media: bool = True) -> dict:
    """Replace the working database from an archive, after verifying it."""
    archive = Path(archive)
    check = verify(archive)
    if not check["ok"]:
        raise RuntimeError("refusing to restore a failing archive: "
                           + "; ".join(check["problems"][:3]))

    with tempfile.TemporaryDirectory() as tmp:
        root = _extract(archive, Path(tmp))
        target = db.restore(root / SNAPSHOT_NAME, db_path)

        restored_dirs = []
        if restore_media:
            for name, dest in (("raw", config.RAW_DIR), ("media", config.MEDIA_DIR)):
                source = root / name
                if source.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    for item in source.rglob("*"):
                        if item.is_file():
                            out = dest / item.relative_to(source)
                            out.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, out)
                    restored_dirs.append(name)

    return {"restored_to": str(target), "directories": restored_dirs,
            "schema": check["schema"], "from": archive.name}


def prune(keep: int = 30, dest_dir: Optional[Path] = None) -> list:
    """Keep the most recent N archives. Returns what was removed."""
    dest_dir = Path(dest_dir or config.BACKUP_DIR)
    archives = sorted(dest_dir.glob("journal-*.tar.gz"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    removed = []
    for old in archives[keep:]:
        old.unlink()
        removed.append(old.name)
    return removed


# --------------------------------------------------------------------- export
EXPORT_QUERIES = {
    "sessions.csv": "SELECT * FROM v_session_daily ORDER BY session_date, session_kind",
    "trading_days.csv": "SELECT * FROM v_trading_day ORDER BY session_date",
    "trades.csv": "SELECT * FROM v_trade_full ORDER BY entry_at",
    "fills.csv": "SELECT f.*, t.trade_uid FROM trade_fill f JOIN trade t ON t.id=f.trade_id "
                 "ORDER BY f.filled_at",
    "opportunities.csv": "SELECT o.*, mr.r_multiple AS mechanical_r FROM opportunity o "
                         "LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id "
                         "ORDER BY o.qualified_at",
    "checkins.csv": "SELECT s.session_date, s.session_kind, cp.*, cq.execution_quality, "
                    "cq.rule_adherence, cq.patience, cq.emotional_control, cq.well_traded "
                    "FROM session s LEFT JOIN checkin_pre cp ON cp.session_id=s.id "
                    "LEFT JOIN checkin_post cq ON cq.session_id=s.id ORDER BY s.session_date",
    "amendments.csv": "SELECT * FROM checkin_amendment ORDER BY amended_at",
    "tags.csv": "SELECT * FROM human_tag ORDER BY confirmed_at",
    "voice_notes.csv": "SELECT id, session_id, trade_id, recorded_at, duration_seconds, "
                       "transcript, transcript_engine FROM voice_note ORDER BY recorded_at",
    "media.csv": "SELECT id, trade_id, session_id, kind, phase, timeframe, path, sha256, "
                 "captured_at, capture_status FROM media_asset ORDER BY captured_at",
    "strategy_versions.csv": "SELECT * FROM strategy_version ORDER BY strategy_id, version",
}


def export_all(conn, dest_dir: Optional[Path] = None) -> dict:
    """Everything the journal holds, in formats nothing else needs to read.

    Blinded features are exported separately and only by the research export,
    so a routine data export can never carry them.
    """
    dest_dir = Path(dest_dir or config.EXPORT_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = {}

    for name, sql in EXPORT_QUERIES.items():
        rows = conn.execute(sql).fetchall()
        path = dest_dir / name
        with open(path, "w", newline="", encoding="utf-8") as fh:
            if rows:
                writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
            else:
                fh.write("")
        written[name] = len(rows)

    from . import repo
    payload = repo.journal_payload(conn)
    (dest_dir / "journal.json").write_text(json.dumps(payload, indent=2, default=str))
    written["journal.json"] = len(payload["sessions"])

    return {"directory": str(dest_dir), "files": written}
