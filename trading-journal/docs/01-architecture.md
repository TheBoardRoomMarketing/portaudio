# 01 — Architecture

## 1. Shape of the system

Local-first, single-machine, no server that has to be running for the data to be
safe. Five processes, all independent, all restartable:

```
  ingest          reads broker exports, alert logs, bot logs → RAW
  enrich          computes market context and mechanical references → DERIVED
  derive          recomputes trade/session metrics from RAW → DERIVED
  report          generates daily/weekly/monthly artifacts → REPORTS
  app             local web app: capture, review, export
```

Nothing calls out to a broker. Nothing holds a credential that could place an
order. `ingest` reads files and read-only endpoints; everything downstream reads
the database.

```
~/AI-Projects/projects/trading-journal/
├── journal.db                 the canonical database
├── raw/                       immutable source files, hashed and dated
│   └── 2026-08-07/            fills.csv, alerts.jsonl, dorb.log
├── media/                     screenshots and audio, referenced by path + sha256
│   └── 2026-08-07/
├── backups/                   nightly snapshots + weekly off-machine copy
├── exports/                   generated CSV/JSON, safe to delete
├── schema/                    numbered migrations
├── engines/                   strategy reference implementations, versioned
├── src/                       ingest / enrich / derive / report / app
└── prototype/                 this phase's interface prototype
```

The whole project is a directory you can copy, zip, or put in a git repository.
That is the portability guarantee: there is no service to migrate off.

## 2. Why local-first

The data is small — six weeks of dense synthetic sessions produced a 628 KB
database. Ten years will not trouble SQLite. Against that, the costs of a hosted
setup are real: an account to keep, a bill to pay, an outage that blocks the
morning check-in, and personal psychological data leaving the machine.

The one thing local-first costs is cross-device sync. The mitigation is in §7:
the database file lives in a synced folder with a documented single-writer rule,
and the phone talks to the laptop over the local network rather than holding its
own copy.

## 3. Database choice: SQLite

**Chosen.** Single file, zero administration, transactional, ubiquitous
tooling, readable by Python/pandas/DuckDB/Datasette without an adapter, and
portable by copy. Foreign keys, CHECK constraints, triggers and views are all
available and all used. WAL mode gives concurrent readers alongside the writer.

**Rejected — Postgres.** Correct for multi-user and concurrent writes; neither
applies. It adds a daemon that must be running before the 09:10 check-in can be
submitted, which is exactly the failure mode to avoid.

**Rejected — DuckDB as primary.** Excellent analytics engine, weaker fit as an
OLTP store for small frequent writes with constraints. The right use is
alongside: point DuckDB at the SQLite file for research queries. No migration
needed.

**Rejected — Markdown, Notion, a spreadsheet.** None can express the
opportunity/mechanical-reference relationship, none can enforce raw
immutability, and all three make "recompute every R with a new definition" a
manual rewrite.

**Rejected — a JSON document store.** The queries that matter are relational
("every opportunity for strategy version X where status = MISSED, with its
mechanical result and the session's pre-check-in state"). That is a join.

Practical settings: `PRAGMA journal_mode = WAL`, `foreign_keys = ON`,
`synchronous = FULL` for the app connection. Timestamps are ISO-8601 UTC with an
explicit `Z`; local reasoning goes through `session.tz`.

## 4. The six data layers

| Layer | Tables | Mutability | Rebuildable |
|---|---|---|---|
| RAW | `raw_import_batch`, `raw_record`, `trade_fill` | append-only, triggers reject UPDATE/DELETE | no — this *is* the source |
| DERIVED | `trade_metrics`, `session_metrics`, `market_context_*`, `mechanical_reference` | recomputed wholesale | yes, from RAW + `calc_version` |
| HUMAN_REPORTED | `checkin_pre`, `checkin_post`, `voice_note`, `human_tag`, notes | amended via `checkin_amendment`, never silently edited | no |
| AI_ANNOTATED | `ai_annotation`, `ai_narrative` | append; status transitions only | yes, but regeneration is recorded |
| BLINDED_RESEARCH | `blinded_feature`, `blinded_feature_dict`, `preregistration`, `research_unblind_log` | append | features yes, log never |
| REPORTS | `report` | regenerable artifacts | yes |

Two rules make the separation real rather than decorative:

- **RAW immutability is enforced in the database.** `raw_record` and
  `trade_fill` carry `BEFORE UPDATE`/`BEFORE DELETE` triggers that `RAISE(ABORT)`.
  A bug in the importer cannot quietly rewrite a fill.
- **The daily UI reads only from views.** `v_session_daily`, `v_trade_full`,
  `v_strategy_scorecard`, `v_mistake_frequency`. None of them touch
  `blinded_feature`. The research views live in a separate migration and are
  prefixed `r_`; the application opens the daily connection with a view
  allow-list that excludes that prefix.

Worked example of the layering, for one moment on 7 August:

| Fact | Layer | Where |
|---|---|---|
| broker filled 2 MESU6 short at 5716.75, exec id `a1b2…` | RAW | `raw_record`, `trade_fill` |
| that trade was −1.00R, held 18m, entry slippage −6 ticks | DERIVED | `trade_metrics` |
| "I hesitated on the trigger and paid for it" | HUMAN | `checkin_post.biggest_mistake` |
| `LATE_ENTRY` | HUMAN (confirmed) | `human_tag` |
| model proposed `LATE_ENTRY` at 0.86, and `DISTRACTED` at 0.41 | AI | `ai_annotation` (accepted / rejected) |
| personal transit state that day | BLINDED | `blinded_feature`, opaque key |
| the week's narrative | REPORTS | `report` + `ai_narrative` |

## 5. Reproducibility of derived data

Every derived row carries `calc_version`, and every batch run writes a
`derived_run` row with the code SHA and row count. `trade_metrics` also stores
an `inputs_hash` so a stale row is detectable.

`derive --rebuild` truncates the derived tables and recomputes from RAW. This is
expected to be run whenever a metric definition changes; the point of the
layering is that it is a routine operation rather than a data-loss event.

Metric definitions live in [02 §7](02-data-model.md) and are versioned with the
code that implements them.

## 6. Safety and privacy

**No trading actions, structurally.** The journal has no order-routing code path
and no write credential. Broker access, where it exists at all, is a read-only
API key or a downloaded statement file. If a broker only offers full-permission
keys, use file exports instead — see [10](10-open-decisions.md) D4.

**Credentials never enter the repository or the database.** They live in a
`.env` outside version control, or in the OS keychain. `raw_record.payload`
stores the response body; the importer strips any `Authorization` header before
writing, and the payload is hashed so tampering is detectable.

**Paper and live never mix.** `account.mode` is one of `paper | live |
sim_eval`, carried onto every session and every trade, and every aggregate is
filtered by it. A paper fill can never be counted into live expectancy.

**Personal data stays local.** Sleep, stress, irritability, life stress and
voice recordings are the most sensitive content here. They stay on the machine,
in an encrypted-at-rest filesystem (FileVault on macOS), and are excluded from
any export shared with anyone.

**Model calls.** Tagging and narrative generation send session facts to a model.
Two mitigations: a `--local-model` mode for anyone uncomfortable with that, and a
hard exclusion list — voice audio is never sent, only the transcript, and only
when generating a narrative the user asked for.

**Nothing writes to the trading lanes.** The journal reads DORB and 10AM model
logs. It has no path that writes into those systems, and no scheduled job with
permission to.

## 7. Backup, recovery, and sync

The journal becomes valuable precisely because it is long. Losing it in year
three is the worst realistic failure, so the backup story is deliberately dull.

**Nightly, 22:00 local** — `sqlite3 journal.db ".backup backups/journal-YYYY-MM-DD.db"`
(a proper online backup, not a file copy of a WAL-mode database), then gzip.
Retention: 14 daily, 12 monthly, all yearly.

**Weekly** — the whole project directory, including `raw/` and `media/`, synced
to a second physical location (external disk or an encrypted cloud folder). RAW
and media are the irreplaceable parts; the database can be rebuilt from them,
the reverse is not true.

**Integrity, on every backup run** — `PRAGMA integrity_check`,
`PRAGMA foreign_key_check`, a count of `raw_record` rows against the previous
run (it must be monotonically non-decreasing), and a sample re-hash of media
files against `media_asset.sha256`. Any failure is loud.

**Restore drill, quarterly** — restore last night's backup into a scratch
directory, run `derive --rebuild`, and diff derived metrics against production.
A backup that has never been restored is not a backup.

**Sync.** The database file is not a multi-writer store. The rule is one writer:
the laptop. The phone reaches the app over the local network (see
[07](07-automation-and-reminders.md) §4) rather than holding a copy. If the
project directory sits in iCloud/Dropbox, the risk is a sync conflict producing
a forked database; the mitigation is that `raw/` and `media/` are append-only
and safe to sync, while `journal.db` is either excluded from sync and backed up
separately, or accepted with a documented single-machine rule. This is
[10](10-open-decisions.md) D5.
