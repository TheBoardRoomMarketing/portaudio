# 07 — Automation and reminders

## 1. The constraint

Nothing may depend on an assistant, an agent, or a chat session being alive. The
09:10 prompt has to fire on a Tuesday in March when nobody has opened a terminal
for a week. So scheduling is done by the operating system's own scheduler, and
every job is a plain script that exits.

## 2. Schedule

| When | Job | Does |
|---|---|---|
| 09:10 ET, weekdays | `prompt --pre` | creates the session if absent, sends a notification linking to the check-in form |
| 09:25 ET, weekdays | `prompt --pre-nudge` | one reminder only if no check-in was submitted; then silence |
| 16:05 ET, weekdays | `ingest && enrich && derive && prompt --post` | imports fills, computes context and metrics, then prompts for check-out with the session's real numbers in the notification |
| 16:20 ET, weekdays | `capture-charts` | headless chart snapshots for the day's trades |
| 22:00 daily | `backup` | online backup, integrity checks, retention pruning |
| Friday 16:30 ET | `report --weekly` | generates the weekly report and notifies |
| 1st of month, 09:00 | `report --monthly` | generates the monthly report and notifies |

On macOS these are `launchd` plists in `~/Library/LaunchAgents/`; on Linux,
systemd timers; on Windows, Task Scheduler. `launchd` is the right choice on a
laptop because it runs missed jobs after a wake, which `cron` does not — a
machine asleep at 09:10 still gets its prompt on waking.

**One nudge, then nothing.** A journal that nags gets its notifications turned
off, and a trader who has turned off notifications has no journal. If a check-in
is skipped, the session is created anyway and marked incomplete. Missing data is
recorded as missing.

## 3. Notification → form, in one tap

```
notification tap
   → opens http://localhost:7717/#/checkin/2026-08-07/pre
   → form is already scoped to that session, prefilled from yesterday
   → Save → done
```

No login, no date picker, no navigation. The deep link carries the session and
the form; the app resolves everything else. Target: four taps and one drag from
notification to saved, which is what the prototype's capture screens are laid out
against.

Delivery mechanism: `terminal-notifier` (or a small menu-bar helper) on macOS,
`notify-send` on Linux. Both support an action that opens a URL.

## 4. Reaching it from the phone

The app runs on the laptop. The phone opens `http://<laptop>.local:7717` on the
home network and saves it to the home screen, where it behaves like an app.

Three consequences, all accepted deliberately:

- **The phone holds no copy of the data.** One writer, always the laptop. No
  sync conflicts, no forked database.
- **Off the home network, capture is unavailable.** Mitigation: an offline
  fallback form that stores the answers in `localStorage` and posts them when the
  laptop is next reachable, stamped with the *original* submission time so the
  behavioural timestamp stays honest. This is the one piece of client-side state
  in the system.
- **The laptop must be awake at 09:10.** It is, on a trading day. If it is not,
  the prompt fires on wake and the check-in is recorded with its real submission
  time, visibly late rather than silently backdated.

An alternative — a small always-on machine on the LAN hosting the journal — is
[10](10-open-decisions.md) D6.

## 5. Report generation

`report --weekly` computes the week's metrics, writes a `report` row with the
metrics JSON, generates a narrative into `ai_narrative`, and links the two. The
report is a derived artifact: regenerating it is free and changes nothing that
matters.

What the weekly report contains is fixed in advance and does not vary with what
looks interesting: performance, process, strategies, missed opportunities,
mistakes, manual value add, behaviour, next-week focus. **No correlation
analysis, no subgroup slicing, no "interesting finding" section.** That
constraint is the point — see [05 §4](05-research-and-blinding.md).

The narrative is generated from a fixed fact set (the metrics JSON, the confirmed
tags, the trader's own reflections), never from raw prose, and is stored with its
model, version, prompt hash and inputs hash so it can be regenerated identically
or superseded with a record.

## 6. Failure handling

Every job writes a `system_event` row on start and finish. Failures are `level =
'error'` and appear in the session timeline, so a missing chart capture or a
failed import is visible in the interface rather than only in a log file.

Jobs are idempotent and safe to re-run. If `ingest` fails at 16:05 the 16:20 job
still runs, and the next day's import picks up the overlap without duplicating
anything (see [04 §2](04-ingestion-and-enrichment.md)).

The one job with a hard ordering requirement is the 16:05 chain — `derive` must
follow `ingest` — which is why it is a single sequential script rather than three
independently scheduled ones.
