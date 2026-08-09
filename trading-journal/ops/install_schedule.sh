#!/usr/bin/env bash
#
# Install the journal's scheduled prompts as launchd agents (macOS).
#
# The operating system is the scheduler. Nothing here depends on a chat session,
# a terminal being open, or any assistant process staying alive — the agents run
# whether or not anything else is.
#
# Four jobs:
#   09:10 ET weekdays   pre-session check-in prompt
#   16:05 ET weekdays   post-session check-out prompt
#   18:00 daily         backup, verified against its manifest
#   Friday 16:30        weekly review reminder
#
# The prompt jobs do not fill anything in. They make sure the session row exists
# and post a notification that opens the right form. A skipped check-in stays
# skipped — missing data is recorded as missing, never back-filled from memory.
#
# Usage:   ops/install_schedule.sh [--uninstall] [--dry-run]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
PREFIX="com.tradingjournal"
PYTHON="$(command -v python3)"
DRY_RUN=0
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --dry-run)   DRY_RUN=1 ;;
    *) echo "unknown option: $arg" >&2; exit 64 ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  cat <<'EOF'
This installer targets macOS launchd. On Linux, the equivalent crontab is:

  10 9  * * 1-5  cd /path/to/trading-journal && python3 bin/journal job pre_session_checkin
  5  16 * * 1-5  cd /path/to/trading-journal && python3 bin/journal job post_session_checkout
  0  18 * * *    cd /path/to/trading-journal && python3 bin/journal job backup
  30 16 * * 5    cd /path/to/trading-journal && python3 bin/journal job weekly_review

Times are in the machine's local timezone; adjust if that is not America/New_York.
EOF
  exit 0
fi

write_agent() {
  local name="$1" hour="$2" minute="$3" weekday="$4" job="$5" message="$6"
  local label="${PREFIX}.${name}"
  local plist="${AGENT_DIR}/${label}.plist"
  local weekday_block=""
  if [[ -n "$weekday" ]]; then
    weekday_block="      <key>Weekday</key><integer>${weekday}</integer>"
  fi

  local body
  body=$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>WorkingDirectory</key><string>${PROJECT_ROOT}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>${PYTHON} bin/journal job ${job} &amp;&amp; /usr/bin/osascript -e 'display notification "${message}" with title "Trading Journal"'</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
      <key>Hour</key><integer>${hour}</integer>
      <key>Minute</key><integer>${minute}</integer>
${weekday_block}
  </dict>
  <key>StandardOutPath</key><string>${PROJECT_ROOT}/data/logs/${name}.log</string>
  <key>StandardErrorPath</key><string>${PROJECT_ROOT}/data/logs/${name}.err</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
)

  if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- would write ${plist}"
    echo "$body"
    return
  fi

  mkdir -p "$AGENT_DIR" "${PROJECT_ROOT}/data/logs"
  printf '%s\n' "$body" > "$plist"
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "installed ${label} (${hour}:$(printf '%02d' "$minute"))"
}

remove_agent() {
  local plist="${AGENT_DIR}/${PREFIX}.$1.plist"
  [[ -f "$plist" ]] || return 0
  launchctl unload "$plist" 2>/dev/null || true
  rm -f "$plist"
  echo "removed ${PREFIX}.$1"
}

JOBS=(
  "precheckin 9 10 '' pre_session_checkin 'Twenty minutes to the open. Thirty seconds — how are you arriving today?'"
  "postcheckout 16 5 '' post_session_checkout 'Session closed. Two minutes to close the day.'"
  "backup 18 0 '' backup 'Journal backed up and verified.'"
  "weekly 16 30 6 weekly_review 'Week complete. The weekly review is ready.'"
)

if [[ $UNINSTALL -eq 1 ]]; then
  for name in precheckin postcheckout backup weekly; do remove_agent "$name"; done
  echo "scheduled prompts removed. The journal itself is untouched."
  exit 0
fi

# Weekday-restricted agents need one entry per day; launchd's Weekday key takes a
# single integer, so the two check-in prompts are installed five times each.
for day in 1 2 3 4 5; do
  write_agent "precheckin-${day}" 9 10 "$day" pre_session_checkin \
    "Twenty minutes to the open. Thirty seconds — how are you arriving today?"
  write_agent "postcheckout-${day}" 16 5 "$day" post_session_checkout \
    "Session closed. Two minutes to close the day."
done
write_agent "backup" 18 0 "" backup "Journal backed up and verified."
write_agent "weekly" 16 30 5 weekly_review "Week complete. The weekly review is ready."

cat <<EOF

Scheduled prompts installed.

  Times follow this machine's local clock. If it is not set to America/New_York,
  adjust the Hour values in ${AGENT_DIR}/${PREFIX}.*.plist.

  The journal must be running for the notification's link to open a form:
      cd ${PROJECT_ROOT} && python3 bin/journal serve

  Job outcomes are recorded in the database (job_run) whether they succeed or
  fail, so a prompt that never fired is visible rather than merely absent.

  Remove everything with:  ops/install_schedule.sh --uninstall
EOF
