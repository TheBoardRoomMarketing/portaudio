# 04 — Ingestion and enrichment

The rule that governs this whole document: **the trader never types a number a
machine could have imported.** Manual entry is reserved for things only a human
knows.

## 1. Source inventory

Assessed against what the existing hub plausibly provides. Nothing here changes
any existing trading system; every integration is read-only.

| Source | Gives | Status | Mechanism |
|---|---|---|---|
| Broker statement export (CSV) | fills, fees, order ids | **Available** | watch a download folder, parse, hash, import |
| Broker REST API | fills, positions, account state | **Potential** | read-only key only; blocked if the broker issues full-permission keys only |
| TradersPost | order lifecycle, routing outcomes, rejections | **Potential** | webhook receiver or log tail; confirms bot execution and failures |
| TradingView alerts | signal fired, conditions at fire time | **Available** | alert → local webhook endpoint → `raw_record` + `signal` |
| DORB engine logs | signals, qualification, bot decisions | **Available** | tail the log file the bot already writes |
| 10AM model logs | signals, qualification state | **Available** | same |
| Local market data | bars for context, MFE/MAE, mechanical reference | **Available** | existing local store; 1-minute bars minimum |
| Economic calendar | event times and importance | **Potential** | scheduled fetch, cached locally; low value, low cost |
| Chart screenshots | visual review | **Potential** | headless browser capture, see §4 |
| Broker order routing / execution | placing trades | **Blocked, permanently** | out of scope by design |

**Manual fallbacks** where an automated path does not exist: the trade form
accepts a paste of a fill line; an opportunity can be logged by hand with
`detection_source = 'human_logged'` so its incompleteness is visible to research
queries; a screenshot can be dropped onto a trade with `capture_status = 'manual'`.

## 2. Ingest pipeline

```
watch → hash → dedupe → land RAW → normalise → link → derive
```

1. **Watch** — a folder, a log tail, or a scheduled read-only pull.
2. **Hash** — sha256 of the payload. `raw_import_batch` has
   `UNIQUE(source, content_sha256)`, so re-importing an identical file is a
   no-op rather than a duplicate.
3. **Land RAW** — one `raw_record` per line/event, verbatim, with
   `UNIQUE(source, record_type, external_id)`. Immutable by trigger.
4. **Normalise** — build `trade_fill` rows from fill records, linked back to
   their `raw_record`. `exec_id` is UNIQUE: the same execution can never be
   counted twice even across overlapping exports.
5. **Link** — assemble fills into trades (`trade_uid` is a deterministic hash of
   account plus first exec id), attach each trade to its session by local trading
   date and account, and to an opportunity by strategy version, instrument,
   direction and a time window around `qualified_at`.
6. **Derive** — recompute metrics for the touched sessions.

**Duplicate handling is the failure mode to design for**, because broker exports
overlap by nature — you download "last 7 days" every day. Three independent
defences: batch content hash, `raw_record` external id uniqueness, and
`trade_fill.exec_id` uniqueness. The test plan exercises all three
([09](09-test-plan.md) T4).

**Ambiguous links are flagged, not guessed.** If a trade cannot be matched to an
opportunity confidently, it is left unlinked and surfaced in the session review
as "unlinked trade — attach to a setup?". A wrong automatic link would corrupt
the discretion analysis silently, which is worse than an obvious gap.

**Timezones.** Sources report in whatever they like — broker UTC, TradingView
exchange time, bot logs in local. Each importer declares its source timezone;
everything converts to UTC on the way in, and `session_date` is resolved through
`session.tz`. This is tested explicitly around a DST boundary
([09](09-test-plan.md) T5).

## 3. Market context enrichment

Runs after the session closes, per session per instrument, from local bar data.
Objective values only, stamped with `data_source` and `calc_version`.

Computed: prior day high/low/close, overnight high/low, gap in points and in ATR
units, ATR(14), 20-day realised volatility, trend state, opening range (30 min
default) and its high/low, session high/low/range, VWAP close distance,
economic-event flags. Per trade: minutes from open, time-of-day bucket, day of
week, VWAP distance at entry, nearest key level and distance to it, regime, ATR
at entry.

Strategy eligibility and setup state are recomputed here too, from
`reference_impl` for each active strategy version — this is what produces
`opportunity` rows for setups the trader never saw, and it is why the journal can
report a miss the trader did not know about.

Subjective market impressions are never written here. They live in
`checkin_post.unusual_context`. The interface labels this card "measured, not
felt" so the boundary is visible on screen and not only in the schema.

## 4. Chart capture

**What is captured, per trade** — four snapshots:

| Phase | Window | Default timeframe |
|---|---|---|
| `pre_entry` | entry − 15 min → entry | 5m |
| `entry` | entry − 5 min → entry + 5 min | 1m |
| `exit` | exit − 5 min → exit + 2 min | 1m |
| `post_exit` | exit → exit + 20 min | 5m |

**Overlays drawn on each**: entry, exit, initial stop, target, the strategy's
reference level, and any signal marker. The requested overlays are stored as
JSON in `media_asset.overlays` so the image can be regenerated identically.

**Mechanism.** A headless browser drives a chart page (TradingView, or a local
renderer over the bar data) after the session closes, not during it — nothing
runs while the trader is trading. Each capture writes `media_asset` with the
file path, sha256, dimensions, `capture_spec` and `capture_status`. A failed
capture writes `capture_status = 'failed'`, which is visible in the interface as
a gap rather than an absence.

**Fallback.** If the third-party chart is unavailable, the local renderer draws
the same geometry from bar data. The prototype demonstrates exactly this: the
trade charts on the Trade Detail screen are drawn from entry/stop/target/MFE/MAE
rather than captured, and are labelled as standing in for the screenshot set.

**Retention.** Images are the largest thing the journal stores. Default: keep
all four phases for 12 months, then keep `entry` and `exit` only. Media is
referenced by path and hash, never embedded, so pruning does not touch the
database's integrity.

## 5. Ingest ordering and idempotency

The pipeline is safe to re-run at any time, in any order. Concretely:

- importing the same file twice changes nothing;
- importing an overlapping file adds only the new executions;
- `derive --rebuild` after a metric change produces the same trades with new
  metric values and a new `calc_version`;
- deleting every derived row and rebuilding reproduces the same numbers, which
  is asserted by the test plan ([09](09-test-plan.md) T2).

The one operation that is *not* reversible is landing a RAW record, which is the
intended asymmetry.
