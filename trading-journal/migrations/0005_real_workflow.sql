-- =============================================================================
-- Trading Journal — the real workflow
-- Migration 0005_real_workflow
-- =============================================================================
--
-- Futures only. One trading decision, executed by scaling in and out on a lead
-- account, replicated by a copier across roughly ten follower accounts, entered
-- from a phone while the trader is at work.
--
-- The model that existed could not hold that: a trade was one account, one
-- entry and one exit. This migration adds the layer above it.
--
--     trading_day
--       └── daily_bias                 immutable morning thesis
--       └── logical_trade              ONE decision, however many accounts
--             ├── execution_event      the atomic truth, append-only
--             ├── account_execution    per-account rollup + discrepancies
--             ├── trade_annotation     what the machine cannot know
--             └── logical_trade_metrics  derived, recomputable
--
-- Additive by design. `trade` survives as the per-account execution record and
-- gains a link upward, so Phase-2 data, metrics and tests keep working. Drop
-- the new tables and a working single-account journal remains.
--
-- Two rules the schema enforces rather than trusts:
--   * statistics count logical trades, never account executions — a copier
--     must never turn one idea into ten observations;
--   * demo rows are marked, and a single query proves they are not mixed in.
-- =============================================================================

-- ------------------------------------------------------------- account registry
ALTER TABLE account ADD COLUMN role TEXT NOT NULL DEFAULT 'STANDALONE'
    CHECK (role IN ('LEAD', 'FOLLOWER', 'STANDALONE'));
ALTER TABLE account ADD COLUMN platform TEXT;              -- 'TradeSea', 'TradeSyncer'
ALTER TABLE account ADD COLUMN prop_firm TEXT;
ALTER TABLE account ADD COLUMN account_size REAL;
ALTER TABLE account ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE', 'INACTIVE', 'PASSED', 'FAILED', 'RESET', 'REPLACED'));
ALTER TABLE account ADD COLUMN copy_source_account_id INTEGER REFERENCES account(id);
-- Follower size relative to the lead. Used to judge whether a copy was
-- proportional, never to fabricate a fill that did not happen.
ALTER TABLE account ADD COLUMN size_multiplier REAL NOT NULL DEFAULT 1.0;
ALTER TABLE account ADD COLUMN external_id TEXT;
ALTER TABLE account ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1));
ALTER TABLE account ADD COLUMN opened_at TEXT;
ALTER TABLE account ADD COLUMN closed_at TEXT;

-- An account's configuration changes: it passes, fails, resets, gets replaced,
-- changes size. A trade from March must keep March's identity, so configuration
-- is versioned and historical rows point at the snapshot in force at the time.
CREATE TABLE account_snapshot (
    id                     INTEGER PRIMARY KEY,
    account_id             INTEGER NOT NULL REFERENCES account(id),
    effective_from         TEXT NOT NULL,
    effective_to           TEXT,
    role                   TEXT NOT NULL,
    status                 TEXT NOT NULL,
    platform               TEXT,
    prop_firm              TEXT,
    account_size           REAL,
    size_multiplier        REAL NOT NULL DEFAULT 1.0,
    copy_source_account_id INTEGER REFERENCES account(id),
    reason                 TEXT,
    captured_at            TEXT NOT NULL,
    is_demo                INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1))
);
CREATE INDEX idx_account_snapshot ON account_snapshot(account_id, effective_from);

-- ---------------------------------------------------------------- trading day
CREATE TABLE trading_day (
    id           INTEGER PRIMARY KEY,
    day_date     TEXT NOT NULL,
    tz           TEXT NOT NULL DEFAULT 'America/New_York',
    status       TEXT NOT NULL DEFAULT 'PLANNED' CHECK (status IN
                   ('PLANNED', 'BIAS_MISSING', 'SESSION_ACTIVE', 'TRADES_IMPORTED',
                    'REVIEW_PENDING', 'DAY_COMPLETE', 'NO_TRADE')),
    iso_week     TEXT,
    iso_month    TEXT,
    -- Post-session reflection lives on the day, not on any one account.
    went_well    TEXT,
    went_poorly  TEXT,
    lesson       TEXT,
    free_note    TEXT,
    closed_at    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    is_demo      INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1)),
    UNIQUE (day_date, is_demo)
);
CREATE INDEX idx_trading_day_week ON trading_day(iso_week);

ALTER TABLE session ADD COLUMN trading_day_id INTEGER REFERENCES trading_day(id);

-- ----------------------------------------------------------------- daily bias
-- The morning thesis, timestamped and never rewritten. Hindsight is a real and
-- powerful force; a bias that can be quietly edited after the close is worth
-- nothing as evidence.
CREATE TABLE daily_bias (
    id              INTEGER PRIMARY KEY,
    trading_day_id  INTEGER NOT NULL REFERENCES trading_day(id) ON DELETE CASCADE,
    recorded_at     TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN
                      ('BULLISH', 'BEARISH', 'NEUTRAL', 'UNSURE')),
    strength        INTEGER CHECK (strength BETWEEN 1 AND 5),
    thesis          TEXT,
    invalidation    TEXT,
    sources         TEXT,             -- JSON array of BIAS_SOURCE values
    capture_seconds INTEGER,
    -- Hash of (direction, strength, thesis, invalidation, sources) at recording.
    -- Any later disagreement between this and the row is detectable.
    original_hash   TEXT NOT NULL,
    -- Outcome is deliberately NOT set by anything yet. Methodology must be
    -- designed and approved before any process assigns it (§XI).
    outcome         TEXT CHECK (outcome IN
                      ('CORRECT', 'PARTIALLY_CORRECT', 'INCORRECT', 'INDETERMINATE')),
    outcome_method  TEXT,
    outcome_at      TEXT,
    is_demo         INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1)),
    UNIQUE (trading_day_id)
);

CREATE TRIGGER daily_bias_thesis_immutable
BEFORE UPDATE OF direction, strength, thesis, invalidation, sources, recorded_at, original_hash
ON daily_bias BEGIN
    SELECT RAISE(ABORT,
      'the morning bias is immutable: record an amendment instead of editing it');
END;

CREATE TABLE bias_amendment (
    id             INTEGER PRIMARY KEY,
    daily_bias_id  INTEGER NOT NULL REFERENCES daily_bias(id) ON DELETE CASCADE,
    field          TEXT NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    reason         TEXT,
    amended_at     TEXT NOT NULL
);
CREATE INDEX idx_bias_amendment ON bias_amendment(daily_bias_id, amended_at);

-- ------------------------------------------------------------- setup taxonomy
-- Editable, bounded, and versioned by activation rather than by rewriting.
CREATE TABLE setup (
    id           TEXT PRIMARY KEY,          -- 'LIQUIDITY_SWEEP_RECLAIM'
    name         TEXT NOT NULL,
    description  TEXT,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    retired_at   TEXT,
    is_demo      INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1))
);

-- Context is not setup. The Liquidity Map informs many different setups, so it
-- is a tool tag that can accompany any of them rather than a setup of its own.
CREATE TABLE context_tag (
    id           TEXT PRIMARY KEY,          -- 'LIQUIDITY_MAP', 'VWAP_HOLD'
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN
                   ('tool', 'condition', 'level', 'session_time', 'other')),
    description  TEXT,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sort_order   INTEGER NOT NULL DEFAULT 0
);

-- Process tags gain a polarity so what went well is recordable, not only what
-- went wrong.
ALTER TABLE mistake_tag ADD COLUMN polarity TEXT NOT NULL DEFAULT 'MISTAKE'
    CHECK (polarity IN ('GOOD', 'MISTAKE'));
ALTER TABLE mistake_tag ADD COLUMN editable INTEGER NOT NULL DEFAULT 1
    CHECK (editable IN (0,1));

-- --------------------------------------------------------------- logical trade
CREATE TABLE logical_trade (
    id                 INTEGER PRIMARY KEY,
    logical_trade_uid  TEXT NOT NULL UNIQUE,
    trading_day_id     INTEGER NOT NULL REFERENCES trading_day(id) ON DELETE CASCADE,
    instrument_id      INTEGER NOT NULL REFERENCES instrument(id),
    contract_id        INTEGER REFERENCES contract(id),
    direction          TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    lead_account_id    INTEGER REFERENCES account(id),
    opened_at          TEXT NOT NULL,
    closed_at          TEXT,
    status             TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN
                         ('OPEN', 'CLOSED', 'NEEDS_GROUPING_REVIEW')),
    -- Which rule produced this grouping, and how sure it is. Grouping is a
    -- judgement made from evidence; hiding it would make it unauditable.
    grouping_rule      TEXT,
    grouping_confidence TEXT CHECK (grouping_confidence IN ('high', 'medium', 'low')),
    grouping_note      TEXT,
    -- Review lifecycle. A trade arrives knowing only what machines know.
    review_state       TEXT NOT NULL DEFAULT 'NEEDS_REVIEW' CHECK (review_state IN
                         ('NEEDS_REVIEW', 'IN_REVIEW', 'REVIEWED', 'SKIPPED')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    is_demo            INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1))
);
CREATE INDEX idx_logical_trade_day    ON logical_trade(trading_day_id, opened_at);
CREATE INDEX idx_logical_trade_review ON logical_trade(review_state);

ALTER TABLE trade ADD COLUMN logical_trade_id INTEGER REFERENCES logical_trade(id);
CREATE INDEX idx_trade_logical ON trade(logical_trade_id);

-- ------------------------------------------------------------ execution events
-- The atomic truth. Append-only: a fill that happened cannot un-happen, and a
-- correction is a new import, not an edit.
CREATE TABLE execution_event (
    id                INTEGER PRIMARY KEY,
    logical_trade_id  INTEGER REFERENCES logical_trade(id) ON DELETE SET NULL,
    account_id        INTEGER NOT NULL REFERENCES account(id),
    account_snapshot_id INTEGER REFERENCES account_snapshot(id),
    instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
    event_type        TEXT NOT NULL CHECK (event_type IN
                        ('OPEN', 'ADD', 'REDUCE', 'CLOSE',
                         'STOP_CHANGE', 'TARGET_CHANGE', 'CANCEL', 'REJECT')),
    occurred_at       TEXT NOT NULL,
    side              TEXT CHECK (side IN ('BUY', 'SELL')),
    quantity          REAL,
    price             REAL,
    stop_price        REAL,
    target_price      REAL,
    -- Signed position held on this account immediately after this event.
    position_after    REAL,
    order_external_id TEXT,
    fill_external_id  TEXT,
    source            TEXT NOT NULL CHECK (source IN
                        ('tradesea_export', 'tradesyncer_export', 'csv_import',
                         'manual', 'backfilled', 'demo')),
    raw_record_id     INTEGER REFERENCES raw_record(id),
    -- Stable identity for idempotent import. Prefer the venue's own fill id;
    -- fall back to a deterministic fingerprint of the event's content.
    fingerprint       TEXT NOT NULL UNIQUE,
    imported_at       TEXT NOT NULL,
    is_demo           INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1))
);
CREATE INDEX idx_event_trade   ON execution_event(logical_trade_id, occurred_at);
CREATE INDEX idx_event_account ON execution_event(account_id, occurred_at);
CREATE INDEX idx_event_time    ON execution_event(occurred_at);

CREATE TRIGGER execution_event_no_update
BEFORE UPDATE OF event_type, occurred_at, quantity, price, account_id, fingerprint
ON execution_event BEGIN
    SELECT RAISE(ABORT,
      'execution events are append-only: re-import to correct, never edit');
END;
CREATE TRIGGER execution_event_no_delete BEFORE DELETE ON execution_event BEGIN
    SELECT RAISE(ABORT, 'execution events may not be deleted');
END;

-- --------------------------------------------------------- per-account rollup
-- Derived. One row per account that participated in a logical trade, plus the
-- discrepancies between a follower and the lead it was supposed to copy.
CREATE TABLE account_execution (
    id                INTEGER PRIMARY KEY,
    logical_trade_id  INTEGER NOT NULL REFERENCES logical_trade(id) ON DELETE CASCADE,
    account_id        INTEGER NOT NULL REFERENCES account(id),
    role_at_time      TEXT NOT NULL,
    first_event_at    TEXT,
    last_event_at     TEXT,
    event_count       INTEGER NOT NULL DEFAULT 0,
    adds              INTEGER NOT NULL DEFAULT 0,
    reductions        INTEGER NOT NULL DEFAULT 0,
    max_position      REAL,
    contracts_traded  REAL,
    avg_entry_price   REAL,
    avg_exit_price    REAL,
    realized_pnl      REAL,
    fees              REAL,
    -- Comparison against the lead. NULL on the lead itself.
    entry_slippage_points REAL,
    quantity_ratio        REAL,   -- actual vs expected from size_multiplier
    missed_events         INTEGER NOT NULL DEFAULT 0,
    discrepancies         TEXT,   -- JSON array of {kind, detail}
    calc_version      TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    is_demo           INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1)),
    UNIQUE (logical_trade_id, account_id)
);

-- ---------------------------------------------------------- derived trade view
CREATE TABLE logical_trade_metrics (
    logical_trade_id     INTEGER PRIMARY KEY REFERENCES logical_trade(id) ON DELETE CASCADE,
    initial_entry_price  REAL,
    avg_entry_price      REAL,
    avg_exit_price       REAL,
    max_position         REAL,
    contracts_traded     REAL,
    adds                 INTEGER NOT NULL DEFAULT 0,
    reductions           INTEGER NOT NULL DEFAULT 0,
    duration_seconds     INTEGER,
    seconds_to_max_size  INTEGER,
    initial_stop         REAL,
    -- R is only computed when a legitimate initial risk exists. No stop, no R.
    initial_risk_points  REAL,
    r_multiple           REAL,
    r_status             TEXT NOT NULL DEFAULT 'UNKNOWN'
                           CHECK (r_status IN ('COMPUTED', 'UNKNOWN')),
    lead_pnl             REAL,
    total_pnl            REAL,
    normalized_pnl       REAL,   -- lead-equivalent, so ten copies are not ten trades
    fees                 REAL,
    accounts_participating INTEGER NOT NULL DEFAULT 0,
    accounts_expected      INTEGER,
    copy_complete        INTEGER,
    calc_version         TEXT NOT NULL,
    computed_at          TEXT NOT NULL
);

-- ------------------------------------------------------------- human annotation
CREATE TABLE trade_annotation (
    logical_trade_id  INTEGER PRIMARY KEY REFERENCES logical_trade(id) ON DELETE CASCADE,
    setup_id          TEXT REFERENCES setup(id),
    why               TEXT,
    planning_mode     TEXT CHECK (planning_mode IN ('PLANNED', 'REACTIVE', 'IMPULSIVE')),
    conviction        INTEGER CHECK (conviction BETWEEN 1 AND 5),
    -- Kept separate on purpose (§XXVIII). A profitable trade can have poor
    -- process; a losing trade can be executed well.
    execution_grade   INTEGER CHECK (execution_grade BETWEEN 1 AND 5),
    process_grade     INTEGER CHECK (process_grade BETWEEN 1 AND 5),
    setup_quality     INTEGER CHECK (setup_quality BETWEEN 1 AND 5),
    note              TEXT,
    reviewed_at       TEXT,
    review_seconds    INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE trade_context_tag (
    logical_trade_id  INTEGER NOT NULL REFERENCES logical_trade(id) ON DELETE CASCADE,
    context_tag_id    TEXT NOT NULL REFERENCES context_tag(id),
    PRIMARY KEY (logical_trade_id, context_tag_id)
);

CREATE TABLE trade_process_tag (
    logical_trade_id  INTEGER NOT NULL REFERENCES logical_trade(id) ON DELETE CASCADE,
    tag_code          TEXT NOT NULL REFERENCES mistake_tag(code),
    confirmed_at      TEXT NOT NULL,
    note              TEXT,
    PRIMARY KEY (logical_trade_id, tag_code)
);

-- Media and voice attach to the decision, not only to an account's execution.
-- Both tables are rebuilt rather than altered: their original CHECK required a
-- session or a per-account trade, which a note about a logical trade satisfies
-- neither of. A note recorded at 10:06 about an NQ long is attached to the
-- decision, and now the constraint says so.
CREATE TABLE media_asset_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('screenshot','audio','export')),
    trade_id       INTEGER REFERENCES trade(id) ON DELETE CASCADE,
    session_id     INTEGER REFERENCES session(id) ON DELETE CASCADE,
    logical_trade_id INTEGER REFERENCES logical_trade(id) ON DELETE CASCADE,
    trading_day_id INTEGER REFERENCES trading_day(id) ON DELETE CASCADE,
    phase          TEXT CHECK (phase IN
                     ('pre_entry','entry','manage','exit','post_exit','session')),
    timeframe      TEXT,
    captured_at    TEXT NOT NULL,
    path           TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    width          INTEGER,
    height         INTEGER,
    capture_spec   TEXT,
    overlays       TEXT,
    capture_status TEXT NOT NULL DEFAULT 'ok' CHECK (capture_status IN ('ok','failed','manual')),
    CHECK (trade_id IS NOT NULL OR session_id IS NOT NULL
           OR logical_trade_id IS NOT NULL OR trading_day_id IS NOT NULL)
);

INSERT INTO media_asset_new (id, kind, trade_id, session_id, phase, timeframe, captured_at,
                             path, sha256, width, height, capture_spec, overlays, capture_status)
SELECT id, kind, trade_id, session_id, phase, timeframe, captured_at, path, sha256,
       width, height, capture_spec, overlays, capture_status
FROM media_asset;

DROP TABLE media_asset;
ALTER TABLE media_asset_new RENAME TO media_asset;
CREATE INDEX idx_media_trade ON media_asset(trade_id, phase);

-- v_capture_quality (0004) reads voice_note; it must go before the rebuild and
-- come back after, or the schema is left holding a view over a missing table.
DROP VIEW IF EXISTS v_capture_quality;
DROP TRIGGER IF EXISTS voice_note_transcript_immutable;

CREATE TABLE voice_note_new (
    id                 INTEGER PRIMARY KEY,
    session_id         INTEGER REFERENCES session(id) ON DELETE CASCADE,
    trade_id           INTEGER REFERENCES trade(id) ON DELETE CASCADE,
    logical_trade_id   INTEGER REFERENCES logical_trade(id) ON DELETE CASCADE,
    trading_day_id     INTEGER REFERENCES trading_day(id) ON DELETE CASCADE,
    recorded_at        TEXT NOT NULL,
    audio_path         TEXT NOT NULL,
    audio_sha256       TEXT NOT NULL,
    duration_seconds   REAL,
    transcript         TEXT,
    transcript_engine  TEXT,
    transcript_version TEXT,
    transcript_at      TEXT,
    CHECK (session_id IS NOT NULL OR trade_id IS NOT NULL
           OR logical_trade_id IS NOT NULL OR trading_day_id IS NOT NULL)
);

INSERT INTO voice_note_new (id, session_id, trade_id, recorded_at, audio_path, audio_sha256,
                            duration_seconds, transcript, transcript_engine,
                            transcript_version, transcript_at)
SELECT id, session_id, trade_id, recorded_at, audio_path, audio_sha256, duration_seconds,
       transcript, transcript_engine, transcript_version, transcript_at
FROM voice_note;

DROP TABLE voice_note;
ALTER TABLE voice_note_new RENAME TO voice_note;

-- The audio and its transcript remain the human record, frozen once written.
CREATE TRIGGER voice_note_transcript_immutable
BEFORE UPDATE OF transcript, audio_path, audio_sha256 ON voice_note
WHEN old.transcript IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'voice note transcripts are immutable once written');
END;

CREATE INDEX idx_voice_logical ON voice_note(logical_trade_id);
CREATE INDEX idx_media_logical ON media_asset(logical_trade_id);

CREATE VIEW v_capture_quality AS
SELECT
    s.id                AS session_id,
    s.session_date,
    s.session_kind,
    CASE WHEN cpre.session_id IS NULL THEN 0 ELSE 1 END  AS has_pre,
    CASE WHEN cp.session_id   IS NULL THEN 0 ELSE 1 END  AS has_post,
    cpre.fill_seconds   AS pre_fill_seconds,
    cp.fill_seconds     AS post_fill_seconds,
    cpre.optional_opened AS pre_optional_opened,
    cp.optional_opened   AS post_optional_opened,
    (SELECT COUNT(*) FROM voice_note v WHERE v.session_id = s.id)      AS voice_notes,
    (SELECT COUNT(*) FROM trade t WHERE t.session_id = s.id
        AND t.entry_source = 'manual')                                 AS manual_trades,
    (SELECT COUNT(*) FROM trade t WHERE t.session_id = s.id)           AS trades,
    (SELECT COUNT(*) FROM opportunity o WHERE o.session_id = s.id
        AND o.detection_source = 'human_logged')                       AS human_logged_setups,
    (SELECT COUNT(*) FROM human_tag h LEFT JOIN trade t2 ON t2.id = h.trade_id
        WHERE (h.session_id = s.id OR t2.session_id = s.id)
          AND h.tag_code = 'OTHER')                                    AS other_tag_uses,
    sm.self_reported_process_index,
    sm.mechanical_conformance_index
FROM session s
LEFT JOIN checkin_pre  cpre ON cpre.session_id = s.id
LEFT JOIN checkin_post cp   ON cp.session_id = s.id
LEFT JOIN session_metrics sm ON sm.session_id = s.id;

-- ----------------------------------------------------------------- demo marking
-- Demo rows are marked, never separated into another database, so isolation is
-- provable with one query and the interface can refuse to mix them.
CREATE VIEW v_demo_isolation AS
SELECT 'trading_day' AS table_name, COUNT(*) AS demo_rows FROM trading_day WHERE is_demo = 1
UNION ALL SELECT 'logical_trade', COUNT(*) FROM logical_trade WHERE is_demo = 1
UNION ALL SELECT 'execution_event', COUNT(*) FROM execution_event WHERE is_demo = 1
UNION ALL SELECT 'account', COUNT(*) FROM account WHERE is_demo = 1
UNION ALL SELECT 'daily_bias', COUNT(*) FROM daily_bias WHERE is_demo = 1
UNION ALL SELECT 'account_execution', COUNT(*) FROM account_execution WHERE is_demo = 1;

-- ---------------------------------------------------------------------- views
-- The inbox: what the machines captured, waiting on what only Zack knows.
CREATE VIEW v_trade_inbox AS
SELECT
    lt.id                AS logical_trade_id,
    lt.logical_trade_uid,
    td.day_date,
    i.symbol,
    lt.direction,
    lt.opened_at,
    lt.closed_at,
    lt.status,
    lt.review_state,
    lt.grouping_confidence,
    m.max_position,
    m.adds,
    m.reductions,
    m.duration_seconds,
    m.lead_pnl,
    m.total_pnl,
    m.normalized_pnl,
    m.r_status,
    m.r_multiple,
    m.accounts_participating,
    m.accounts_expected,
    m.copy_complete,
    a.setup_id,
    a.why,
    a.process_grade,
    lt.is_demo,
    (SELECT COUNT(*) FROM account_execution ae
      WHERE ae.logical_trade_id = lt.id AND ae.discrepancies IS NOT NULL
        AND ae.discrepancies <> '[]') AS accounts_with_discrepancies
FROM logical_trade lt
JOIN trading_day td   ON td.id = lt.trading_day_id
JOIN instrument i     ON i.id = lt.instrument_id
LEFT JOIN logical_trade_metrics m ON m.logical_trade_id = lt.id
LEFT JOIN trade_annotation a      ON a.logical_trade_id = lt.id;

-- The day at a glance, including days with no trades — which are evidence too.
CREATE VIEW v_day_summary AS
SELECT
    td.id AS trading_day_id,
    td.day_date,
    td.status,
    td.iso_week,
    b.direction   AS bias_direction,
    b.strength    AS bias_strength,
    b.thesis      AS bias_thesis,
    b.outcome     AS bias_outcome,
    COUNT(lt.id)                                                          AS trades,
    SUM(CASE WHEN lt.review_state = 'REVIEWED' THEN 1 ELSE 0 END)         AS reviewed,
    SUM(CASE WHEN lt.review_state = 'NEEDS_REVIEW' THEN 1 ELSE 0 END)     AS needs_review,
    SUM(CASE WHEN lt.status = 'NEEDS_GROUPING_REVIEW' THEN 1 ELSE 0 END)  AS needs_grouping,
    ROUND(SUM(m.lead_pnl), 2)                                             AS lead_pnl,
    ROUND(SUM(m.total_pnl), 2)                                            AS total_pnl,
    ROUND(SUM(m.normalized_pnl), 2)                                       AS normalized_pnl,
    td.is_demo
FROM trading_day td
LEFT JOIN daily_bias b   ON b.trading_day_id = td.id
LEFT JOIN logical_trade lt ON lt.trading_day_id = td.id
LEFT JOIN logical_trade_metrics m ON m.logical_trade_id = lt.id
GROUP BY td.id;

-- Copy quality: did the followers reproduce the lead?
CREATE VIEW v_copy_quality AS
SELECT
    lt.id AS logical_trade_id,
    td.day_date,
    i.symbol,
    ae.account_id,
    acc.label AS account_label,
    ae.role_at_time,
    ae.event_count,
    ae.max_position,
    ae.avg_entry_price,
    ae.entry_slippage_points,
    ae.quantity_ratio,
    ae.missed_events,
    ae.realized_pnl,
    ae.discrepancies
FROM account_execution ae
JOIN logical_trade lt ON lt.id = ae.logical_trade_id
JOIN trading_day td   ON td.id = lt.trading_day_id
JOIN instrument i     ON i.id = lt.instrument_id
JOIN account acc      ON acc.id = ae.account_id;
