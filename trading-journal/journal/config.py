"""Paths and settings.

Everything the journal owns lives under one data directory so the project stays
portable. The default sits inside the project (director ruling D5: canonical
path inside the standalone journal project), and can be moved with the
JOURNAL_HOME environment variable.

Deliberately NOT defaulted into iCloud/Dropbox/Drive: SQLite in WAL mode inside
a consumer sync folder is a known corruption path. `raw/`, `media/` and
`backups/` are safe to sync; `journal.db` is not. See docs/01-architecture.md.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories that ship with the project
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
WEB_DIR = PROJECT_ROOT / "web"
OPS_DIR = PROJECT_ROOT / "ops"

# Runtime data root
DATA_HOME = Path(os.environ.get("JOURNAL_HOME", PROJECT_ROOT / "data")).expanduser()

DB_PATH = DATA_HOME / "journal.db"
RAW_DIR = DATA_HOME / "raw"          # untouched broker exports and log captures
MEDIA_DIR = DATA_HOME / "media"      # screenshots, audio
BACKUP_DIR = DATA_HOME / "backups"   # snapshots + manifests
EXPORT_DIR = DATA_HOME / "exports"   # generated CSV/JSON

RUNTIME_DIRS = (DATA_HOME, RAW_DIR, MEDIA_DIR, BACKUP_DIR, EXPORT_DIR)

# Defaults for a new install; overridable per account/session in the database.
DEFAULT_TZ = "America/New_York"
DEFAULT_SESSION_KIND = "NY_AM"

# Bumped whenever a derived-metric definition changes. Every derived row stores
# the version that produced it, so a definition change is always traceable.
CALC_VERSION = "metrics@1.0.0"

# The local server binds to loopback only. That bind is the security boundary:
# there is no auth layer, because there is no network surface to authenticate.
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("JOURNAL_PORT", "8765"))


def ensure_dirs() -> None:
    for d in RUNTIME_DIRS:
        d.mkdir(parents=True, exist_ok=True)
