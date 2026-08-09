"""Connection handling, migrations and integrity.

Two connection modes exist and the difference is a safety feature:

    connect()                    daily use. Research views are not reachable.
    connect(restricted=False)    maintenance and analysis. Used by migrations,
                                 the blinded-feature writer, and (later)
                                 Research Mode, which additionally requires an
                                 unblind log entry.

The daily connection installs an authorizer that refuses to read any object
whose name starts with `r_` or `blinded_`. That is defence in depth behind the
opaque-key design, not the primary mechanism — but it means a careless
`SELECT *` in a daily-view query cannot surface a research feature.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import config

BLINDED_PREFIXES = ("r_", "blinded_")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class BlindedAccessError(sqlite3.DatabaseError):
    """Raised when a daily connection tries to touch the research layer."""


def _daily_authorizer(action, arg1, arg2, db_name, trigger):
    """Refuse reads of research/blinded objects on a daily connection."""
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT):
        name = (arg1 or "").lower()
        if name.startswith(BLINDED_PREFIXES):
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect(path: Optional[Path] = None, restricted: bool = True) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    if restricted:
        conn.set_authorizer(_daily_authorizer)
    return conn


# --------------------------------------------------------------------- migrations
def migration_files() -> list:
    return sorted(config.MIGRATIONS_DIR.glob("*.sql"))


def applied_migrations(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migration'"
    ).fetchone()
    if not row:
        return {}
    return {
        r["version"]: r["code_sha"]
        for r in conn.execute("SELECT version, code_sha FROM schema_migration")
    }


def migrate(conn: sqlite3.Connection, verbose: bool = False) -> list:
    """Apply pending migrations in filename order. Returns the versions applied.

    Must be given an unrestricted connection — connect(restricted=False) —
    because creating the research views requires reading the tables they wrap.

    Each file runs inside the standard SQLite rebuild envelope — foreign keys
    off, one transaction, foreign_key_check before commit — because migrations
    that rebuild a table cannot run with foreign keys enforced.
    """
    applied = applied_migrations(conn)
    done = []

    for path in migration_files():
        version = path.name
        digest = sha256_file(path)

        if version in applied:
            if applied[version] and applied[version] != digest:
                raise RuntimeError(
                    f"migration {version} changed after being applied "
                    f"(recorded {applied[version][:12]}, on disk {digest[:12]}). "
                    "Applied migrations are immutable; add a new one instead."
                )
            continue

        sql = path.read_text()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.executescript(sql)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"{version} left foreign key violations: {violations[:5]}")
            conn.execute(
                "INSERT INTO schema_migration(version, applied_at, code_sha, notes) "
                "VALUES (?,?,?,?)",
                (version, utcnow(), digest, "applied by journal.db.migrate"),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        done.append(version)
        if verbose:
            print(f"applied {version}")

    return done


def schema_state(conn: sqlite3.Connection) -> dict:
    applied = applied_migrations(conn)
    on_disk = [p.name for p in migration_files()]
    return {
        "applied": sorted(applied),
        "available": on_disk,
        "pending": [v for v in on_disk if v not in applied],
        "current": sorted(applied)[-1] if applied else None,
    }


# --------------------------------------------------------------------- integrity
def integrity_check(conn: sqlite3.Connection) -> dict:
    """Physical and logical integrity. Every failure is reported, not raised."""
    problems = []

    physical = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if physical != "ok":
        problems.append(f"integrity_check: {physical}")

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign_key_check: {len(fk)} violations, first {tuple(fk[0])}")

    # Logical invariants that the schema cannot express as constraints.
    checks = (
        ("trade without session",
         "SELECT COUNT(*) FROM trade t LEFT JOIN session s ON s.id=t.session_id "
         "WHERE s.id IS NULL"),
        ("closed trade without fills",
         "SELECT COUNT(*) FROM trade t WHERE t.exit_at IS NOT NULL AND NOT EXISTS "
         "(SELECT 1 FROM trade_fill f WHERE f.trade_id=t.id)"),
        ("taken opportunity without trade",
         "SELECT COUNT(*) FROM opportunity WHERE status IN ('TAKEN','BOT_EXECUTED') "
         "AND trade_id IS NULL"),
        ("trade linked to another session's opportunity",
         "SELECT COUNT(*) FROM trade t JOIN opportunity o ON o.id=t.opportunity_id "
         "WHERE o.session_id <> t.session_id"),
        ("derived metrics without calc_version",
         "SELECT COUNT(*) FROM trade_metrics WHERE calc_version IS NULL "
         "OR calc_version=''"),
        ("ai annotation promoted without human confirmation",
         "SELECT COUNT(*) FROM ai_annotation a WHERE a.status='accepted' AND NOT EXISTS "
         "(SELECT 1 FROM human_tag h WHERE h.from_annotation_id=a.id)"),
    )
    for label, sql in checks:
        n = conn.execute(sql).fetchone()[0]
        if n:
            problems.append(f"{label}: {n}")

    state = schema_state(conn)
    if state["pending"]:
        problems.append(f"pending migrations: {', '.join(state['pending'])}")

    return {"ok": not problems, "problems": problems, "schema": state}


# --------------------------------------------------------------------- snapshots
def snapshot(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent copy via the SQLite backup API — safe while WAL is active.

    An open write transaction on the source is committed first. The backup API
    blocks indefinitely against a connection holding uncommitted writes, and a
    snapshot that silently excluded work already done would be worse than the
    surprise of an implicit commit: taking a backup is a natural commit point.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if conn.in_transaction:
        conn.commit()
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def restore(snapshot_path: Path, db_path: Optional[Path] = None) -> Path:
    """Replace the working database with a snapshot.

    The existing database is moved aside rather than deleted — a restore should
    never be the step that loses data.
    """
    snapshot_path = Path(snapshot_path)
    db_path = Path(db_path or config.DB_PATH)
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)

    probe = sqlite3.connect(str(snapshot_path))
    try:
        if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"snapshot {snapshot_path.name} fails integrity_check")
    finally:
        probe.close()

    if db_path.exists():
        aside = db_path.with_suffix(f".superseded-{datetime.now():%Y%m%d%H%M%S}.db")
        shutil.move(str(db_path), str(aside))
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()

    shutil.copy2(str(snapshot_path), str(db_path))
    return db_path
