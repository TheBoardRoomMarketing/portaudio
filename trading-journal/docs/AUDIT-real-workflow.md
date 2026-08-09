# Audit: existing implementation vs Zack's real workflow

Written before changing code. The journal was built against a generic
single-account, one-entry-one-exit, desktop model. Zack trades futures from his
phone, scales in and out, and replicates one decision across ~10 accounts.

Most of the foundation survives. The trade model does not.

---

## Verdict on the specific assumptions named in the brief

| Assumption | Present? | Verdict |
|---|---|---|
| TradingView execution | **No** | Never assumed. TradingView appears nowhere in the execution path. |
| One entry / one exit | **Partly** | `trade` centres `avg_entry_price` / `avg_exit_price` / `side` / `quantity`. `trade_fill` does carry legs (`entry`, `scale_in`, `partial`, `exit`, `stop`, `target`) and the metrics layer already computes size-weighted average entry and exit across partial fills — that part is right. But the *shape* of a trade is still one open and one close, there is no position-over-time, and no `ADD`/`REDUCE` event semantics. **Wrong for the workflow.** |
| One account = one trade | **Yes, wrong** | `trade.account_id` is a single FK. Nothing represents one decision across many accounts. **The largest defect.** |
| Desktop capture | **Partly** | The interface is genuinely mobile-responsive and the capture forms work at 390 px. But the *model* assumes you fill a form about a session, not that trades arrive from a machine and wait for you. No inbox, no deferred review. **Wrong emphasis.** |
| P&L = setup quality | **No** | Handled well and worth preserving: process is scored with no P&L term, and two process views are kept deliberately apart. |
| Trade outcome = bias accuracy | **N/A** | No bias outcome exists at all — bias is a single enum on the morning check-in. **Missing.** |
| Copied accounts as independent observations | **Not yet possible** | Nothing multiplies accounts today, but nothing prevents it either. Needs to be structurally impossible. |
| Every trade documented while happening | **Yes, wrong** | Capture is a form you complete. There is no notion of a partially-known trade awaiting human context. **Wrong for a phone trader at work.** |

---

## ALREADY IMPLEMENTED — keep

- **Six-layer separation** (RAW / DERIVED / HUMAN / AI / BLINDED / REPORTS) with
  database-enforced immutability: `raw_record`, `raw_import_batch` and
  `trade_fill` reject UPDATE and DELETE via triggers. Exactly the provenance
  model §XXXII asks for; it already exists and is tested.
- **Migration runner** with hash-pinned immutability of applied migrations.
- **Amendment-not-overwrite** for check-ins (`checkin_amendment`). This is
  precisely the mechanism §X needs for bias immutability — it generalises.
- **Version-frozen taxonomy** (`strategy_version` with `rule_hash`, active
  dates, and a trigger blocking rule edits). The setup taxonomy of §XIII can
  reuse this pattern rather than inventing one.
- **Import infrastructure**: `import_profile` CSV mapping profiles, content-hash
  duplicate detection at file level, `exec_id` uniqueness at execution level,
  parse-without-writing dry runs. §XXXIII is largely satisfied already.
- **Backup / restore / export** with manifest verification.
- **Process separation**: self-reported index and mechanical conformance index,
  never merged. §XXVIII's principle already has two of its six dimensions built.
- **95 passing tests** and the harness to add more.

## PARTIALLY IMPLEMENTED — extend

- **Fills** exist but are legs of a single trade, not events with position
  context. Need `OPEN / ADD / REDUCE / CLOSE / STOP_CHANGE / TARGET_CHANGE /
  CANCEL / REJECT` and running position.
- **Accounts** exist as a thin table (`label, broker, mode, currency, tz,
  active`). Need role, platform, prop firm, size, status, copy relationship,
  normalisation metadata, and historical configuration identity.
- **Bias** exists as `checkin_pre.bias` — a three-value enum. Need direction
  (including `UNSURE`), strength, thesis, invalidation, sources, and an
  immutable timestamped record.
- **Mistake taxonomy** exists with 15 codes but is mistake-only and seeded in
  code. Need editable, bounded, and a `GOOD` polarity.
- **Media** links to trade or session; needs to link to a logical trade.
- **R-multiple** already returns NULL without a stop, which satisfies §XXXVI's
  "never invent R". Keep exactly as is.

## MISSING — build

- `trading_day` as the canonical top-level object. Today's `session` is
  account-scoped, which cannot hold a decision spanning ten accounts.
- `logical_trade` — the trading decision.
- `execution_event` — the atomic, append-only truth.
- `account_execution` — per-account rollup with discrepancy detection.
- Grouping engine turning events into logical trades, with an explicit
  `NEEDS_GROUPING_REVIEW` state rather than silent merging.
- Trade inbox and review state machine.
- Setup taxonomy and context tags (Liquidity Map as context, not setup).
- Execution source adapter boundary.
- Dark-primary visual language.

## WRONG FOR REAL WORKFLOW — correct

1. `trade` as the unit of analysis → becomes `logical_trade`, with `trade`
   retained as the per-account execution record.
2. Session as account-scoped container → `trading_day` becomes canonical.
3. Capture-as-form → capture-as-inbox.
4. Light-primary interface → dark-primary.
5. Statistics counting account executions → statistics count logical trades.

## UNKNOWN / NEEDS INVESTIGATION

- Whether TradeSea exposes any export from its Compass journal, and in what
  format. Requires a logged-in look, which only Zack can do.
- Whether TradeSyncer exposes copy-event history (not just an upload-your-own
  journal), and whether follower fills are retrievable per account.
- Whether Rithmic-level access is obtainable, at what cost, and whether it
  would even be permitted by the prop firms involved.
- Instrument specifications (tick size, point value) for the contracts actually
  traded — currently only MES is seeded.

---

## Design decisions taken (conservative, reversible, documented)

1. **`trade` is not deleted.** It becomes the per-account execution record and
   gains `logical_trade_id`. Existing rows, metrics and tests keep working; the
   new layer sits above them. Reversible: dropping the new tables leaves a
   working Phase-2 journal.
2. **`trading_day` is additive.** `session` gains `trading_day_id` and stays.
3. **Grouping is explicit and recorded.** Every logical trade stores the rule
   that produced it and a confidence; ambiguous cases are flagged rather than
   guessed. Reversible: regrouping is a recompute, since events are the truth.
4. **Demo data is marked at the row level** (`is_demo`) rather than kept in a
   separate database, so a single query can prove isolation and the UI can
   refuse to mix them.
5. **The dark theme replaces light as primary** but the light tokens are kept,
   because the existing palette is validated and eye comfort at 3pm in an office
   is a real case.
