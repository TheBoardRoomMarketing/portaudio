# 09 — Test plan

Tests are grouped by what breaks the journal, not by module. The ordering is by
severity: the first four categories protect data that cannot be recovered.

`pytest`, run against a temporary database built from the migrations. The
synthetic pipeline (`scripts/build_synthetic.py`) doubles as an integration
fixture — it already exercises the full schema, the derived layer, the views and
the export path on every run.

## T1 — Schema validity and constraints

| Test | Asserts |
|---|---|
| T1.1 | Both migrations execute on an empty database; `PRAGMA foreign_key_check` returns nothing. *(currently passing)* |
| T1.2 | Every CHECK constraint rejects an out-of-range value: rating 0 and 6, unknown `opportunity.status`, unknown `account.mode`, unknown `ai_annotation.status`. |
| T1.3 | `session (session_date, account_id)` uniqueness rejects a second session for the same day and account. |
| T1.4 | `voice_note` rejects a row with neither `session_id` nor `trade_id`. |
| T1.5 | Rebuilding the database from migrations produces an identical `sqlite_master` to the shipped one. |

## T2 — Raw immutability and derived reproducibility

| Test | Asserts |
|---|---|
| T2.1 | `UPDATE raw_record` raises; `DELETE FROM raw_record` raises; `DELETE FROM raw_import_batch` raises. |
| T2.2 | `UPDATE trade_fill` raises. |
| T2.3 | Deleting all of `trade_metrics` and `session_metrics` and re-running `derive` reproduces byte-identical values. |
| T2.4 | Every `trade_metrics` row carries a non-null `calc_version` and a `derived_run_id` that resolves. |
| T2.5 | Changing a metric definition and re-deriving updates values **and** `calc_version`; the old version is no longer present for those rows. |
| T2.6 | `voice_note.transcript` cannot be updated once written. |
| T2.7 | A `checkin_pre` edit writes a `checkin_amendment` row preserving the old value. |

## T3 — Strategy version preservation

| Test | Asserts |
|---|---|
| T3.1 | `UPDATE strategy_version SET rules = …` raises. |
| T3.2 | Publishing v1.1 leaves every existing trade pointing at v1.0. |
| T3.3 | `v_strategy_scorecard` reports v1.0 and v1.1 separately and never merges them. |
| T3.4 | A `mechanical_reference` computed under v1.0 is unchanged after v1.1 is published. |
| T3.5 | Renaming the parent `strategy` display name does not alter any historical aggregate. |

## T4 — Import idempotency and duplicates

| Test | Asserts |
|---|---|
| T4.1 | Importing the same file twice adds zero rows the second time (batch content hash). |
| T4.2 | An overlapping export (7-day window imported daily) adds only genuinely new executions. |
| T4.3 | The same `exec_id` arriving from two different sources is stored once. |
| T4.4 | Re-importing after a partial failure completes the batch without duplicating what landed. |
| T4.5 | A trade assembled from fills has `sum(entry legs) == sum(exit legs)` in quantity; a mismatch is flagged, not silently averaged. |

## T5 — Time and timezone

| Test | Asserts |
|---|---|
| T5.1 | A fill at 21:30 UTC on 2026-08-07 lands in the 2026-08-07 session (17:30 ET), not 08-08. |
| T5.2 | A session spanning a DST boundary computes durations correctly; `holding_seconds` does not gain or lose an hour. |
| T5.3 | Sources declaring different timezones normalise to the same UTC instant. |
| T5.4 | `iso_week` and `iso_month` match the local trading date, not the UTC date. |
| T5.5 | Interface display converts UTC to `session.tz` on every timestamp — no naive rendering. |

## T6 — Metric correctness

Each with hand-computed fixtures, longs and shorts:

| Test | Asserts |
|---|---|
| T6.1 | R: long, entry 100, stop 95, exit 110 → +2.00R. Short, entry 100, stop 105, exit 90 → +2.00R. |
| T6.2 | R is NULL when no initial stop was set — never zero, never guessed. |
| T6.3 | `net_pnl == gross_pnl − sum(commission + exchange_fees)` across all fills. |
| T6.4 | MFE/MAE from a known bar series match hand computation, both directions. |
| T6.5 | `seconds_to_mfe ≤ holding_seconds`. |
| T6.6 | Slippage sign: a worse-than-intended fill is **negative** for both longs and shorts. |
| T6.7 | `r_captured_pct` is NULL for any trade with `r_multiple ≤ 0`. |
| T6.8 | `process_score` contains no P&L term: doubling every P&L value leaves it unchanged. |
| T6.9 | `max_drawdown_r` on a known R sequence matches hand computation. |
| T6.10 | `user_value_added_r == total_r − mechanical_r`, and the mechanical side includes untaken opportunities. |

## T7 — Sessions, signals, opportunities

| Test | Asserts |
|---|---|
| T7.1 | Every trade resolves to exactly one session. |
| T7.2 | A trade whose opportunity link is ambiguous is left unlinked and flagged, not guessed. |
| T7.3 | An opportunity with `status = 'MISSED'` produces a `MISSED_SETUP` tag and appears in weekly missed opportunities. |
| T7.4 | `SKIPPED_BY_RULE` does **not** count as a mistake and does not depress the process score. |
| T7.5 | Every opportunity has a `mechanical_reference`, including untaken ones. |
| T7.6 | A no-trade session still produces a session row, a check-in, and any opportunities that qualified. |
| T7.7 | A manual override sets `was_manual_override` and surfaces on the trade detail screen. |

## T8 — AI separation

| Test | Asserts |
|---|---|
| T8.1 | Writing an `ai_annotation` creates no `human_tag`. |
| T8.2 | `v_mistake_frequency` counts only `human_tag`. |
| T8.3 | Accepting a suggestion writes a `human_tag` with `from_annotation_id` set. |
| T8.4 | Rejecting sets `status = 'rejected'` and writes no tag. |
| T8.5 | Every `ai_narrative` carries model, version, prompt hash and inputs hash. |
| T8.6 | No code path updates a human free-text field from model output. |

## T9 — Blinding isolation

The category where a passing test matters most, because a leak is invisible.

| Test | Asserts |
|---|---|
| T9.1 | No daily view (`v_*`) references `blinded_feature` — asserted by parsing `sqlite_master`, not by reading the source. |
| T9.2 | The daily connection's allow-list rejects every `r_*` view. |
| T9.3 | No API response served to a daily screen contains a blinded feature key or value — asserted by scanning serialised payloads. |
| T9.4 | Entering research mode writes a `research_unblind_log` row. |
| T9.5 | `blinded_feature_dict` is not joined outside research code paths. |
| T9.6 | An analysis whose feature n is below `preregistration.min_n` returns a refusal, not a result. |
| T9.7 | A `preregistration` cannot change `analysis_plan` after `status = 'locked'` without failing its `locked_hash` check. |

## T10 — Export and portability

| Test | Asserts |
|---|---|
| T10.1 | CSV export of sessions, trades, opportunities, check-ins round-trips: re-importing reproduces identical values. |
| T10.2 | JSON export contains every layer, and every media reference resolves to a file on disk. |
| T10.3 | Export excludes blinded features unless explicitly requested with a logged reason. |
| T10.4 | Numeric precision survives export and re-import — no float drift in R or P&L. |
| T10.5 | The exported dataset opens in pandas and DuckDB without transformation. |

## T11 — Backup and restore

| Test | Asserts |
|---|---|
| T11.1 | `.backup` on a WAL database produces a restorable file while the app is running. |
| T11.2 | Restoring last night's backup and running `derive --rebuild` reproduces production metrics. |
| T11.3 | `PRAGMA integrity_check` and `foreign_key_check` pass on every backup. |
| T11.4 | `raw_record` count is monotonically non-decreasing across consecutive backups. |
| T11.5 | Media sha256 sampling matches `media_asset.sha256`; a corrupted file is detected. |
| T11.6 | A database rebuilt from `raw/` alone reproduces all derived data (raw is genuinely sufficient). |

## T12 — Interface

| Test | Asserts |
|---|---|
| T12.1 | Every route renders with no console error. *(currently passing, headless)* |
| T12.2 | No horizontal overflow at 390px on any screen. *(currently passing)* |
| T12.3 | Light and dark both render every token; no colour defined only inside a media query. |
| T12.4 | Every signed value renders with an explicit sign and an outcome word — colour is never the sole carrier. |
| T12.5 | Charts expose `role="img"` with a text label stating the value. |
| T12.6 | The timeline disclosure toggles `aria-expanded` and the panel's `hidden`. |
| T12.7 | Research Mode is locked on load and requires an explicit action to open. |
| T12.8 | A session with no trades, no check-out, or no opportunities renders without error — empty states are designed, not accidental. |

## Continuous checks

Run on every commit: T1, T2, T3, T6, T8, T9, T12.1–T12.2.
Run nightly with the backup job: T11.
Run on every import: T4, T5.
