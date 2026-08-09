#!/usr/bin/env bash
#
# Move the journal into its permanent home and make it a standalone repository.
#
# Default destination: ~/AI-Projects/projects/trading-journal
#
# This script refuses to overwrite anything. If the destination exists and holds
# content that did not come from this project, it stops and reports rather than
# merging, renaming or clobbering. Verifying the destination is the whole point
# of running an installer instead of `mv`.
#
# Usage:  scripts/install_local.sh [destination] [--force-empty] [--dry-run]

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${HOME}/AI-Projects/projects/trading-journal"
DRY_RUN=0
FORCE_EMPTY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN=1 ;;
    --force-empty) FORCE_EMPTY=1 ;;
    -*)            echo "unknown option: $arg" >&2; exit 64 ;;
    *)             DEST="$arg" ;;
  esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

say "source      ${SOURCE_DIR}"
say "destination ${DEST}"
say ""

# ---------------------------------------------------------------- verify source
for required in journal migrations web tests bin/journal; do
  [[ -e "${SOURCE_DIR}/${required}" ]] || fail "source is missing ${required}; this does not look like the journal project"
done

# ------------------------------------------------------------ verify destination
if [[ -e "$DEST" ]]; then
  [[ -d "$DEST" ]] || fail "${DEST} exists and is not a directory"

  # Ours, or someone else's? A destination carrying our migrations is a previous
  # install and may be updated. Anything else is another project.
  if [[ -f "${DEST}/migrations/0001_init.sql" && -d "${DEST}/journal" ]]; then
    say "An existing Trading Journal install is already there."
    if [[ -e "${DEST}/data/journal.db" ]]; then
      say ""
      say "It has a database at ${DEST}/data/journal.db."
      say "That database is real longitudinal data and this script will not touch it."
      say ""
      say "To update the code without touching the data, use git in the destination:"
      say "    cd ${DEST} && git pull"
      say ""
      fail "refusing to overwrite an install that holds a database"
    fi
    say "No database present, so the install can be replaced safely."
  else
    entries=$(find "$DEST" -mindepth 1 -maxdepth 1 ! -name '.DS_Store' | wc -l | tr -d ' ')
    if [[ "$entries" -gt 0 ]]; then
      say "${DEST} already exists and holds ${entries} item(s) that are not this project:"
      find "$DEST" -mindepth 1 -maxdepth 1 ! -name '.DS_Store' -exec basename {} \; | head -20 | sed 's/^/    /'
      say ""
      fail "another project appears to live there. Pick a different destination, or move that project first."
    fi
    [[ $FORCE_EMPTY -eq 1 ]] || say "destination exists but is empty — continuing"
  fi
fi

# ---------------------------------------------------------------- preflight
command -v python3 >/dev/null || fail "python3 is required and was not found on PATH"
PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "python3     ${PY_VERSION} ($(command -v python3))"
python3 -c 'import sqlite3, zoneinfo; assert sqlite3.sqlite_version_info >= (3, 25)' \
  || fail "python3 is missing sqlite3/zoneinfo, or SQLite is older than 3.25"
say "checks      ok — stdlib only, no packages to install"
say ""

if [[ $DRY_RUN -eq 1 ]]; then
  say "dry run: nothing was copied."
  say "Would create ${DEST} and copy the project into it."
  exit 0
fi

# ---------------------------------------------------------------- install
mkdir -p "$(dirname "$DEST")"
mkdir -p "$DEST"

# Copy the project, excluding this repository's git metadata and any build output.
( cd "$SOURCE_DIR" && \
  find . -mindepth 1 -maxdepth 1 \
    ! -name '.git' ! -name 'data' ! -name '__pycache__' \
    -exec cp -R {} "$DEST"/ \; )

find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "${DEST}/bin/journal" "${DEST}/scripts/"*.sh "${DEST}/ops/"*.sh 2>/dev/null || true

say "copied the project to ${DEST}"

# ---------------------------------------------------------------- standalone repo
cd "$DEST"
if [[ ! -d .git ]]; then
  git init -q
  git add -A
  git -c user.name="${GIT_AUTHOR_NAME:-$(git config --global user.name || echo 'Trading Journal')}" \
      -c user.email="${GIT_AUTHOR_EMAIL:-$(git config --global user.email || echo 'journal@localhost')}" \
      commit -q -m "Trading Journal — Phase 2 production foundation"
  say "initialised a standalone git repository with one commit"
else
  say "a git repository already exists here; left alone"
fi

# ---------------------------------------------------------------- initialise
python3 bin/journal init --account "Futures Eval 50k" --mode paper
say ""
say "Done."
say ""
say "  Start the journal:      cd ${DEST} && python3 bin/journal serve --open"
say "  Load synthetic data:    python3 bin/journal seed"
say "  Run the tests:          python3 -m unittest discover -s tests"
say "  Schedule the prompts:   ops/install_schedule.sh"
say ""
say "  The database lives at ${DEST}/data/journal.db and is deliberately not"
say "  inside a cloud-sync folder. Backups under data/backups are safe to sync."
