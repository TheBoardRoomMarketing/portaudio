# 02 — Data model

The authoritative definition is `schema/0001_init.sql` (canonical) and
`schema/0002_research_views.sql` (research views). Both execute cleanly and are
exercised by `scripts/build_synthetic.py`. This document explains the decisions
behind them.

## 1. Conventions

- Timestamps: ISO-8601 UTC with an explicit `Z`. Local wall-clock is derived
  through `session.tz`, never stored twice.
- Dates: `session_date` is the *local trading date*, which is what a human means
  by "Tuesday", and which does not shift when a session runs past midnight UTC.
- Ratings: 1–5 integers with CHECK constraints. Booleans: `0 | 1` integers.
- Money: REAL in `account.currency`. R is unitless.
- Identity: integer primary keys internally, plus a stable business key
  (`session_uid`, `trade_uid`) so exports and re-imports are idempotent.
- Every enumerated column is a CHECK constraint, not a convention.

## 2. Registry

`account`, `instrument`, `contract`, `strategy`, `strategy_version`.

**`account.mode`** — `paper | live | sim_eval` — propagates onto `session` and
`trade` rather than being joined at query time. Denormalised on purpose: it is
too important to leave to a join someone might forget.

**`instrument` vs `contract`** — `MES` is the instrument (tick size, point
value); `MESU6` is the contract actually held. R and P&L compute from the
instrument; the contract records what was traded so a roll is visible.

### Strategy versioning

This is the mechanism that stops history being rewritten:

```sql
CREATE TABLE strategy_version (
    strategy_id          TEXT,      -- '10AM_MODEL'
    version              TEXT,      -- '1.0'
    rule_hash            TEXT,      -- sha256 of the canonicalised rule spec
    rules                TEXT,      -- JSON rule spec
    automation_level     TEXT,      -- manual | semi_auto | bot | bot_supervised
    reference_impl       TEXT,      -- module implementing the mechanical evaluator
    qualification_status TEXT,      -- research | forward_test | qualified | retired
    active_from, active_to
);
```

`trade`, `opportunity` and `signal` all reference `strategy_version.id`, never
`strategy.id`. A trigger blocks UPDATE of `rules`, `rule_hash` or `version`:

```sql
CREATE TRIGGER strategy_version_immutable_rules BEFORE UPDATE OF rules, rule_hash, version
ON strategy_version BEGIN
    SELECT RAISE(ABORT, 'strategy_version rules are frozen: publish a new version instead');
END;
```

Change a rule and you publish `1.1` with a new `rule_hash` and `active_from`;
the trades taken under `1.0` keep pointing at `1.0` forever. `reference_impl`
names the module that evaluates the mechanical counterfactual, so the reference
for a 2026 trade is still computed by the 2026 evaluator in 2029.

## 3. Session

One `session` row per trading day per account, keyed
`session_uid = '2026-08-07:MAIN:ACCT1'`. It carries status
(`planned | checked_in | active | closed | no_trade | skipped`), the risk plan
(`planned_max_risk_r`, `planned_max_loss`, `daily_loss_limit`,
`max_trades_planned`), start/end times, and denormalised `iso_week` / `iso_month`
for grouping without date arithmetic in every query.

A no-trade day still gets a session row with a check-in. Days when the trader
stayed out are data — the calendar shows "No trade / Stayed out", and the
opportunity table still records what qualified.

Planned versus actual is captured in `session_strategy` and
`session_instrument`, each with both a `planned` and an `actual` flag on the
same row. Planned-but-not-traded and traded-but-not-planned are then both
one-line queries; the latter is the `UNPLANNED_TRADE` flag.

## 4. Human-reported

`checkin_pre` and `checkin_post` are one row per session, keyed by session, with
every column nullable except the timestamp. That nullability is a product
decision: a check-in submitted with three of eleven fields is worth far more
than one abandoned because it demanded eleven.

Both tables carry `fill_seconds` — friction telemetry. If the morning median
drifts above 60 seconds, the form is wrong and gets cut, and there will be data
to prove it.

**Amendment, not editing.** `checkin_amendment` records
`(session_id, which, field, old_value, new_value, reason, amended_at)`. Changing
"stress 2" to "stress 4" three days later writes an amendment row. The original
answer survives, because for behavioural research the answer given *at the time*
is the datum; the later correction is a second, differently-interesting datum.

`human_tag` holds confirmed mistake labels and is the only table any statistic
counts. It carries an optional `from_annotation_id` so an accepted model
suggestion remains traceable to its origin without being confused for one.

## 5. Trades and fills

`trade` is an *interpretation* — a position from open to flat. `trade_fill` is
the *fact*: one row per execution, linked to its `raw_record`, immutable by
trigger. Partials, scale-ins and stop fills are all fills with a `leg`
discriminator (`entry | scale_in | partial | exit | stop | target`). Rebuilding
trades from fills is therefore always possible.

`trade_uid` is deterministic — a hash of account plus first execution id — which
is what makes re-importing the same broker file a no-op rather than a duplicate.
`trade_fill.exec_id` carries a UNIQUE constraint as the second line of defence.

`intended_price` on the entry fill (the limit price, or the signal price for a
bot) is what makes slippage computable. Without it, slippage is guesswork.

## 6. Qualified opportunities and the mechanical reference

The most important table in the schema, because it holds what did *not* happen.

```sql
CREATE TABLE opportunity (
    session_id, strategy_version_id, instrument_id, signal_id,
    qualified_at, direction,
    status TEXT CHECK (status IN ('TAKEN','MISSED','SKIPPED_BY_RULE',
        'SKIPPED_DISCRETIONARY','BOT_EXECUTED','BOT_FAILED','INVALIDATED','NO_ACTION')),
    status_reason, decided_by, trade_id, detection_source
);
```

Status meanings, kept deliberately distinct:

| Status | Meaning | Counts as a mistake? |
|---|---|---|
| `TAKEN` | traded by the human | no |
| `BOT_EXECUTED` | traded by automation | no |
| `MISSED` | qualified, no decision was made — nobody was watching | **yes** — `MISSED_SETUP` |
| `SKIPPED_BY_RULE` | a written rule excluded it (news window, risk cap) | no — this is the system working |
| `SKIPPED_DISCRETIONARY` | seen, considered, declined | no, but it is the discretion question |
| `INVALIDATED` | conditions reversed before entry was possible | no |
| `BOT_FAILED` | automation should have traded and did not | **yes** — `BOT_ROUTING_ERROR` |
| `NO_ACTION` | qualified outside the trading window | no |

The distinction between `MISSED` and `SKIPPED_DISCRETIONARY` carries most of the
behavioural weight. "I wasn't there" and "I looked at it and said no" are
different failures, and only the second one is a judgement worth evaluating.

`detection_source` (`engine | alert | human_logged | backfilled`) is honesty
about coverage: opportunities found by an engine are complete, ones logged by a
human are not, and a research query can exclude the incomplete kind.

**`mechanical_reference`** is one row per opportunity: what the strategy version
in force would have done — entry, stop, target, exit, `r_multiple`, MFE, MAE —
computed by `reference_impl`, stamped with `calc_version` and `inputs_hash`.

That yields the journal's headline research metric:

```
USER_VALUE_ADDED (period) = Σ actual R over trades linked to opportunities
                          − Σ mechanical R over ALL qualified opportunities
```

Missed setups therefore count against the trader, which is the correct
accounting: a setup you did not take because you were not at the desk is a real
cost, and the surrounding organisation of the day is part of trading.

Caveats to record with any use of this number: the mechanical reference assumes
fills that may not have been available; it ignores the reality that some skips
were correct for reasons the rules cannot see; and it needs a large sample
before the difference means anything. It belongs in [05](05-research-and-blinding.md),
not in a weekly conclusion.

## 7. Derived metrics — definitions

Every one of these is computed by `derive` and stamped with `calc_version`. Named
so they can be changed deliberately rather than drifting.

| Metric | Definition |
|---|---|
| `risk_per_unit` | `abs(avg_entry_price − initial_stop)`. NULL if no stop was set. |
| `r_multiple` | `(exit − entry) · direction / risk_per_unit` |
| `gross_pnl` | `(exit − entry) · direction · quantity · point_value` |
| `net_pnl` | `gross_pnl − (commission + exchange_fees)` summed over fills |
| `mfe_r` / `mae_r` | best / worst excursion between entry and exit, in R, from intrabar data |
| `seconds_to_mfe` / `seconds_to_mae` | elapsed from entry to that excursion |
| `holding_seconds` | `exit_at − entry_at` |
| `entry_slippage_ticks` | `(intended_price − fill_price) · direction / tick_size`; **negative is adverse** |
| `size_deviation_pct` | `(quantity − planned_quantity) / planned_quantity × 100` |
| `r_captured_pct` | `r_multiple / mfe_r × 100`, **only defined for winners** |
| `session_cum_pnl_after` | running net P&L within the session, ordered by entry time |
| `max_drawdown_r` | worst peak-to-trough of the running R sum |
| `expectancy_r` | `total_r / trade_count` |
| `process_score` | see [00](00-product-requirements.md); contains no P&L term |
| `user_value_added_r` | `total_r − mechanical_r` over the period |

Two of these are easy to get wrong and worth stating flatly. `r_captured_pct` is
meaningless on losers (a −1R trade with a +0.6R MFE yields −164%), so it is NULL
there and the interface says "reached +0.6R before the stop" instead. And
slippage sign is a convention that must not flip: negative always means the fill
was worse than intended, for both longs and shorts.

## 8. Market context

`market_context_session` (per session per instrument) holds prior high/low/close,
overnight high/low, gap in points and in ATR units, ATR(14), 20-day realised
volatility, trend state, opening range, session range, VWAP distance and
economic events as JSON. `market_context_trade` holds the per-entry slice:
minutes from open, time bucket, day of week, VWAP distance, nearest key level and
its distance, regime, ATR at entry.

All of it is objective and sourced, with `data_source` and `calc_version`
recorded. Subjective impressions — "choppy", "it felt directional" — live in
`checkin_post.unusual_context` and notes. The interface labels the market card
"measured, not felt" to keep the boundary visible.

## 9. AI annotations

```sql
ai_annotation(target_type, target_id, annotation_type, tag_code, content,
              evidence, confidence, model, model_version, prompt_hash,
              status CHECK IN ('suggested','accepted','rejected','expired'))
```

A suggestion is never a label. Promotion is an explicit user action that writes a
`human_tag` row carrying `from_annotation_id`. Every statistic in the journal
counts `human_tag`; `ai_annotation` is counted only when measuring the model.

`evidence` records which fields the model looked at, so a suggestion can be
checked rather than believed. In the prototype's Trade Detail, the accepted and
rejected suggestions are both shown with their confidence — the rejected one is
as informative as the accepted one.

`ai_narrative` stores generated prose with `model`, `model_version`,
`prompt_hash`, `inputs_hash` and a `superseded_by` chain. Narratives are derived
artifacts: regenerating them changes nothing that matters.

## 10. Media

`media_asset` references files by path plus `sha256`; blobs stay on disk. Each
screenshot carries `phase` (`pre_entry | entry | manage | exit | post_exit`),
`timeframe`, the `capture_spec` that produced it and the `overlays` drawn on it,
plus `capture_status` so a failed capture is recorded as failed rather than
silently absent.

## 11. Views the interface may read

`v_session_daily`, `v_trade_full`, `v_strategy_scorecard`, `v_mistake_frequency`.
None of them join `blinded_feature`. The research views in migration 0002 are
prefixed `r_` and are outside the daily connection's allow-list — see
[05](05-research-and-blinding.md).
