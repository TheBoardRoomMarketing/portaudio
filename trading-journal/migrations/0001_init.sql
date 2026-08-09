-- =============================================================================
-- Trading Journal v1 — canonical schema (SQLite)
-- Migration 0001_init
-- =============================================================================
--
-- LAYERING CONTRACT
--   raw_*        RAW           append-only, byte-faithful, never rewritten
--   *_metrics    DERIVED       reproducible from RAW + a pinned calc_version
--   checkin_*    HUMAN         what the trader actually said, versioned by amendment
--   ai_*         AI            model output, never promoted without human confirm
--   blinded_*    BLINDED       hidden from daily UI; unblinding is logged
--   report       REPORTS       generated artifacts, regenerable, pinned to inputs
--
-- Rules enforced here (not by convention):
--   * RAW tables reject UPDATE and DELETE via triggers.
--   * Human check-ins are amended, never edited in place (checkin_amendment).
--   * Trades point at strategy_version, never strategy — history cannot be
--     rewritten by renaming or editing a strategy.
--   * AI tags land in ai_annotation with status='suggested'; only an explicit
--     human confirmation writes a row into human_tag.
--   * blinded_feature carries no human-readable labels in the daily views.
--
-- All timestamps: ISO-8601 UTC with explicit offset ('2026-08-07T13:35:12Z').
-- All local wall-clock reasoning goes through session.tz. See docs/02.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- -----------------------------------------------------------------------------
-- 0. Migration bookkeeping
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migration (
    version      TEXT PRIMARY KEY,
    applied_at   TEXT NOT NULL,
    code_sha     TEXT,
    notes        TEXT
);

-- =============================================================================
-- 1. RAW LAYER — immutable source of truth
-- =============================================================================

-- One row per ingested file / API pull / log tail.
CREATE TABLE raw_import_batch (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'tradovate_csv' | 'traderspost_api' | 'dorb_log' | ...
    source_kind    TEXT NOT NULL CHECK (source_kind IN
                     ('broker','execution_platform','alert','bot_log','market_data','manual')),
    source_uri     TEXT,                   -- file path or endpoint (never credentials)
    content_sha256 TEXT,                   -- hash of the source payload, for dedupe
    imported_at    TEXT NOT NULL,
    row_count      INTEGER,
    importer       TEXT,                   -- importer name + version, e.g. 'tradovate@1.2.0'
    status         TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','partial','failed')),
    notes          TEXT,
    UNIQUE (source, content_sha256)
);

-- Byte-faithful record as it arrived. Nothing here is ever normalised in place.
CREATE TABLE raw_record (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES raw_import_batch(id),
    source          TEXT NOT NULL,
    record_type     TEXT NOT NULL,         -- 'fill' | 'order' | 'signal' | 'bot_event' | 'bar' | ...
    external_id     TEXT,                  -- broker exec id, alert id, etc.
    occurred_at     TEXT,                  -- event time as reported by the source
    received_at     TEXT NOT NULL,
    payload         TEXT NOT NULL,         -- verbatim JSON / CSV line
    payload_sha256  TEXT NOT NULL,
    UNIQUE (source, record_type, external_id)
);
CREATE INDEX idx_raw_record_occurred ON raw_record(occurred_at);
CREATE INDEX idx_raw_record_batch    ON raw_record(batch_id);

CREATE TRIGGER raw_record_no_update BEFORE UPDATE ON raw_record BEGIN
    SELECT RAISE(ABORT, 'raw_record is append-only: correct by re-importing a new batch');
END;
CREATE TRIGGER raw_record_no_delete BEFORE DELETE ON raw_record BEGIN
    SELECT RAISE(ABORT, 'raw_record is append-only: rows may not be deleted');
END;
CREATE TRIGGER raw_batch_no_delete BEFORE DELETE ON raw_import_batch BEGIN
    SELECT RAISE(ABORT, 'raw_import_batch is append-only');
END;

-- =============================================================================
-- 2. REGISTRY — accounts, instruments, strategies
-- =============================================================================

CREATE TABLE account (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,      -- 'Topstep 50k Eval', 'IBKR Personal'
    broker      TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('paper','live','sim_eval')),
    currency    TEXT NOT NULL DEFAULT 'USD',
    tz          TEXT NOT NULL DEFAULT 'America/New_York',
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE instrument (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL UNIQUE,     -- 'MES', 'ES', 'NQ'
    name         TEXT,
    asset_class  TEXT NOT NULL CHECK (asset_class IN ('future','equity','option','fx','crypto')),
    exchange     TEXT,
    tick_size    REAL NOT NULL,
    tick_value   REAL NOT NULL,            -- currency per tick per contract
    point_value  REAL NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD'
);

-- The tradable expiry actually held (MESU6), distinct from the instrument (MES).
CREATE TABLE contract (
    id             INTEGER PRIMARY KEY,
    instrument_id  INTEGER NOT NULL REFERENCES instrument(id),
    contract_symbol TEXT NOT NULL UNIQUE,
    expiry         TEXT,
    UNIQUE (instrument_id, contract_symbol)
);

CREATE TABLE strategy (
    id           TEXT PRIMARY KEY,         -- '10AM_MODEL', 'DORB', 'DISCRETIONARY_X'
    name         TEXT NOT NULL,
    family       TEXT,                     -- 'opening_range', 'mean_reversion', 'discretionary'
    description  TEXT,
    created_at   TEXT NOT NULL
);

-- The unit every historical record points at. Immutable once trades reference it.
CREATE TABLE strategy_version (
    id                   INTEGER PRIMARY KEY,
    strategy_id          TEXT NOT NULL REFERENCES strategy(id),
    version              TEXT NOT NULL,               -- '1.0', '1.1'
    rule_hash            TEXT NOT NULL,               -- sha256 of the canonicalised rule spec
    rules                TEXT NOT NULL,               -- JSON: entry/exit/filter/risk rules
    automation_level     TEXT NOT NULL CHECK (automation_level IN
                            ('manual','semi_auto','bot','bot_supervised')),
    reference_impl       TEXT,                        -- path/module of the mechanical evaluator
    qualification_status TEXT NOT NULL CHECK (qualification_status IN
                            ('research','forward_test','qualified','retired')),
    active_from          TEXT NOT NULL,
    active_to            TEXT,                        -- NULL = currently active
    created_at           TEXT NOT NULL,
    UNIQUE (strategy_id, version)
);
CREATE INDEX idx_stratver_active ON strategy_version(strategy_id, active_from, active_to);

CREATE TRIGGER strategy_version_immutable_rules BEFORE UPDATE OF rules, rule_hash, version
ON strategy_version BEGIN
    SELECT RAISE(ABORT,
      'strategy_version rules are frozen: publish a new version instead of editing history');
END;

-- =============================================================================
-- 3. SESSION — one canonical object per trading day
-- =============================================================================

CREATE TABLE session (
    id                  INTEGER PRIMARY KEY,
    session_uid         TEXT NOT NULL UNIQUE,   -- '2026-08-07:MAIN:ACCT1'
    session_date        TEXT NOT NULL,          -- local calendar date (YYYY-MM-DD)
    tz                  TEXT NOT NULL DEFAULT 'America/New_York',
    account_id          INTEGER NOT NULL REFERENCES account(id),
    mode                TEXT NOT NULL CHECK (mode IN ('paper','live','sim_eval')),
    status              TEXT NOT NULL CHECK (status IN
                          ('planned','checked_in','active','closed','no_trade','skipped')),
    started_at          TEXT,
    ended_at            TEXT,
    planned_max_risk_r  REAL,
    planned_max_loss    REAL,                   -- account currency
    daily_loss_limit    REAL,
    max_trades_planned  INTEGER,
    iso_week            TEXT,                   -- '2026-W32', denormalised for grouping
    iso_month           TEXT,                   -- '2026-08'
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (session_date, account_id)
);
CREATE INDEX idx_session_date  ON session(session_date);
CREATE INDEX idx_session_week  ON session(iso_week);
CREATE INDEX idx_session_month ON session(iso_month);

CREATE TABLE session_strategy (
    session_id          INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    strategy_version_id INTEGER NOT NULL REFERENCES strategy_version(id),
    planned             INTEGER NOT NULL DEFAULT 0,
    actual              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, strategy_version_id)
);

CREATE TABLE session_instrument (
    session_id    INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id),
    planned       INTEGER NOT NULL DEFAULT 0,
    actual        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, instrument_id)
);

-- =============================================================================
-- 4. HUMAN_REPORTED — check-in / check-out / notes / confirmed tags
-- =============================================================================

-- Scales are 1-5 unless noted. Everything except the * fields is optional:
-- the UI must be able to submit a valid pre-session check-in in ~30 seconds.
CREATE TABLE checkin_pre (
    session_id        INTEGER PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    submitted_at      TEXT NOT NULL,
    fill_seconds      INTEGER,                    -- friction telemetry, self-measured
    -- core five (*required in UI)
    sleep_hours       REAL,
    energy            INTEGER CHECK (energy       BETWEEN 1 AND 5),
    focus             INTEGER CHECK (focus        BETWEEN 1 AND 5),
    stress            INTEGER CHECK (stress       BETWEEN 1 AND 5),
    desire_to_trade   INTEGER CHECK (desire_to_trade BETWEEN 1 AND 5),
    -- expanded (collapsed by default in UI)
    sleep_quality     INTEGER CHECK (sleep_quality  BETWEEN 1 AND 5),
    irritability      INTEGER CHECK (irritability   BETWEEN 1 AND 5),
    impulsivity       INTEGER CHECK (impulsivity    BETWEEN 1 AND 5),
    confidence        INTEGER CHECK (confidence     BETWEEN 1 AND 5),
    money_pressure    INTEGER CHECK (money_pressure IN (0,1)),
    physical_state    TEXT,                       -- 'rested' | 'sore' | 'ill' | free text
    caffeine_mg       INTEGER,
    life_stress_note  TEXT,
    -- plan
    bias              TEXT CHECK (bias IN ('bullish','bearish','neutral')),
    planned_note      TEXT,
    well_traded_definition TEXT,                  -- "what makes today well-traded if P&L is hidden"
    note              TEXT
);

CREATE TABLE checkin_post (
    session_id        INTEGER PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    submitted_at      TEXT NOT NULL,
    fill_seconds      INTEGER,
    -- core ratings
    execution_quality INTEGER CHECK (execution_quality BETWEEN 1 AND 5),
    rule_adherence    INTEGER CHECK (rule_adherence    BETWEEN 1 AND 5),
    patience          INTEGER CHECK (patience          BETWEEN 1 AND 5),
    emotional_control INTEGER CHECK (emotional_control BETWEEN 1 AND 5),
    -- binary process flags (checkbox row in UI)
    overtraded        INTEGER CHECK (overtraded       IN (0,1)),
    revenge_trade     INTEGER CHECK (revenge_trade    IN (0,1)),
    stop_moved        INTEGER CHECK (stop_moved       IN (0,1)),
    oversized         INTEGER CHECK (oversized        IN (0,1)),
    missed_qualified  INTEGER CHECK (missed_qualified IN (0,1)),
    manual_override   INTEGER CHECK (manual_override  IN (0,1)),
    -- narrative
    best_decision     TEXT,
    biggest_mistake   TEXT,
    unusual_context   TEXT,
    -- the anchor question
    well_traded       TEXT CHECK (well_traded IN ('yes','mixed','no')),
    note              TEXT
);

-- Check-ins are corrected by amendment, never by silent edit.
CREATE TABLE checkin_amendment (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    which         TEXT NOT NULL CHECK (which IN ('pre','post')),
    field         TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    reason        TEXT,
    amended_at    TEXT NOT NULL
);

-- Spoken reflection. Audio + transcript are the human record and are immutable;
-- anything the model derives from them lives in ai_annotation.
CREATE TABLE voice_note (
    id                 INTEGER PRIMARY KEY,
    session_id         INTEGER REFERENCES session(id) ON DELETE CASCADE,
    trade_id           INTEGER REFERENCES trade(id) ON DELETE CASCADE,
    recorded_at        TEXT NOT NULL,
    audio_path         TEXT NOT NULL,
    audio_sha256       TEXT NOT NULL,
    duration_seconds   REAL,
    transcript         TEXT,
    transcript_engine  TEXT,                -- 'whisper-large-v3'
    transcript_version TEXT,
    transcript_at      TEXT,
    CHECK (session_id IS NOT NULL OR trade_id IS NOT NULL)
);
CREATE TRIGGER voice_note_transcript_immutable
BEFORE UPDATE OF transcript, audio_path, audio_sha256 ON voice_note
WHEN old.transcript IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'voice note transcripts are immutable once written');
END;

-- Canonical, human-confirmed labels. AI never writes here directly.
CREATE TABLE human_tag (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER REFERENCES session(id) ON DELETE CASCADE,
    trade_id        INTEGER REFERENCES trade(id) ON DELETE CASCADE,
    tag_code        TEXT NOT NULL REFERENCES mistake_tag(code),
    note            TEXT,
    confirmed_at    TEXT NOT NULL,
    from_annotation_id INTEGER REFERENCES ai_annotation(id),  -- set when accepted from a suggestion
    CHECK (session_id IS NOT NULL OR trade_id IS NOT NULL)
);
CREATE INDEX idx_human_tag_trade   ON human_tag(trade_id);
CREATE INDEX idx_human_tag_session ON human_tag(session_id);

CREATE TABLE mistake_tag (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN
                  ('entry','exit','risk','discipline','process','system')),
    description TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- =============================================================================
-- 5. SIGNALS, OPPORTUNITIES, MECHANICAL REFERENCE
-- =============================================================================

CREATE TABLE signal (
    id                  INTEGER PRIMARY KEY,
    external_id         TEXT UNIQUE,
    strategy_version_id INTEGER NOT NULL REFERENCES strategy_version(id),
    instrument_id       INTEGER NOT NULL REFERENCES instrument(id),
    session_id          INTEGER REFERENCES session(id),
    fired_at            TEXT NOT NULL,
    direction           TEXT CHECK (direction IN ('long','short')),
    source              TEXT NOT NULL,      -- 'tradingview_alert' | 'dorb_engine' | 'manual_observation'
    payload             TEXT,               -- JSON snapshot of signal conditions
    raw_record_id       INTEGER REFERENCES raw_record(id)
);
CREATE INDEX idx_signal_session ON signal(session_id);

-- The heart of the research design: every qualified setup, taken or not.
CREATE TABLE opportunity (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    strategy_version_id INTEGER NOT NULL REFERENCES strategy_version(id),
    instrument_id       INTEGER NOT NULL REFERENCES instrument(id),
    signal_id           INTEGER REFERENCES signal(id),
    qualified_at        TEXT NOT NULL,
    direction           TEXT CHECK (direction IN ('long','short')),
    status              TEXT NOT NULL CHECK (status IN
                          ('TAKEN','MISSED','SKIPPED_BY_RULE','SKIPPED_DISCRETIONARY',
                           'BOT_EXECUTED','BOT_FAILED','INVALIDATED','NO_ACTION')),
    status_reason       TEXT,               -- free text: "stepped away", "news in 4 min"
    decided_by          TEXT CHECK (decided_by IN ('human','bot','system','unknown')),
    trade_id            INTEGER REFERENCES trade(id),
    detection_source    TEXT NOT NULL CHECK (detection_source IN
                          ('engine','alert','human_logged','backfilled')),
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_opportunity_session  ON opportunity(session_id);
CREATE INDEX idx_opportunity_strategy ON opportunity(strategy_version_id, status);

-- DERIVED counterfactual: what the mechanical rules would have produced.
-- This is what makes USER_VALUE_ADDED computable.
CREATE TABLE mechanical_reference (
    id              INTEGER PRIMARY KEY,
    opportunity_id  INTEGER NOT NULL UNIQUE REFERENCES opportunity(id) ON DELETE CASCADE,
    entry_at        TEXT,
    entry_price     REAL,
    stop_price      REAL,
    target_price    REAL,
    exit_at         TEXT,
    exit_price      REAL,
    exit_reason     TEXT CHECK (exit_reason IN ('target','stop','time','session_end','no_fill')),
    r_multiple      REAL,
    mfe_r           REAL,
    mae_r           REAL,
    calc_version    TEXT NOT NULL,
    inputs_hash     TEXT,
    computed_at     TEXT NOT NULL
);

-- =============================================================================
-- 6. TRADES — human/bot execution
-- =============================================================================

CREATE TABLE trade (
    id                  INTEGER PRIMARY KEY,
    trade_uid           TEXT NOT NULL UNIQUE,     -- deterministic: hash(account, first exec id)
    session_id          INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    account_id          INTEGER NOT NULL REFERENCES account(id),
    mode                TEXT NOT NULL CHECK (mode IN ('paper','live','sim_eval')),
    instrument_id       INTEGER NOT NULL REFERENCES instrument(id),
    contract_id         INTEGER REFERENCES contract(id),
    strategy_version_id INTEGER REFERENCES strategy_version(id),
    execution_mode      TEXT NOT NULL CHECK (execution_mode IN ('manual','bot','semi_auto')),
    opportunity_id      INTEGER REFERENCES opportunity(id),
    signal_id           INTEGER REFERENCES signal(id),
    side                TEXT NOT NULL CHECK (side IN ('long','short')),
    quantity            REAL NOT NULL,            -- max absolute position size held
    entry_at            TEXT NOT NULL,
    exit_at             TEXT,
    avg_entry_price     REAL NOT NULL,
    avg_exit_price      REAL,
    initial_stop        REAL,
    initial_target      REAL,
    planned_risk_r      REAL DEFAULT 1.0,
    planned_quantity    REAL,                     -- for position-sizing deviation
    exit_reason         TEXT CHECK (exit_reason IN
                          ('target','stop','manual','time','session_end','partial_only','error')),
    was_manual_override INTEGER NOT NULL DEFAULT 0 CHECK (was_manual_override IN (0,1)),
    override_note       TEXT,
    execution_note      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_trade_session  ON trade(session_id);
CREATE INDEX idx_trade_strategy ON trade(strategy_version_id);
CREATE INDEX idx_trade_entry    ON trade(entry_at);

-- RAW-linked executions. A trade is an interpretation; fills are the facts.
CREATE TABLE trade_fill (
    id             INTEGER PRIMARY KEY,
    trade_id       INTEGER NOT NULL REFERENCES trade(id) ON DELETE CASCADE,
    raw_record_id  INTEGER REFERENCES raw_record(id),
    order_id       TEXT,
    exec_id        TEXT,
    leg            TEXT NOT NULL CHECK (leg IN ('entry','scale_in','partial','exit','stop','target')),
    side           TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity       REAL NOT NULL,
    price          REAL NOT NULL,
    filled_at      TEXT NOT NULL,
    intended_price REAL,                     -- for slippage: limit price or signal price
    commission     REAL NOT NULL DEFAULT 0,
    exchange_fees  REAL NOT NULL DEFAULT 0,
    UNIQUE (exec_id)
);
CREATE INDEX idx_fill_trade ON trade_fill(trade_id);

CREATE TRIGGER trade_fill_no_update BEFORE UPDATE ON trade_fill BEGIN
    SELECT RAISE(ABORT, 'trade_fill mirrors RAW executions and is append-only');
END;

-- =============================================================================
-- 7. DERIVED — every column here is reproducible from RAW + calc_version
-- =============================================================================

CREATE TABLE derived_run (
    id            INTEGER PRIMARY KEY,
    calc_version  TEXT NOT NULL,
    code_sha      TEXT,
    scope         TEXT NOT NULL,          -- 'trade_metrics' | 'session_metrics' | 'market_context'
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    rows_written  INTEGER,
    status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed'))
);

CREATE TABLE trade_metrics (
    trade_id             INTEGER PRIMARY KEY REFERENCES trade(id) ON DELETE CASCADE,
    gross_pnl            REAL,
    fees                 REAL,
    net_pnl              REAL,
    risk_per_unit        REAL,              -- |entry - initial_stop| in price
    r_multiple           REAL,
    mfe_price            REAL,
    mae_price            REAL,
    mfe_r                REAL,
    mae_r                REAL,
    seconds_to_mfe       INTEGER,
    seconds_to_mae       INTEGER,
    holding_seconds      INTEGER,
    entry_slippage_ticks REAL,
    exit_slippage_ticks  REAL,
    size_deviation_pct   REAL,              -- (actual - planned) / planned
    r_captured_pct       REAL,              -- r_multiple / mfe_r
    session_cum_pnl_after REAL,
    session_trade_index  INTEGER,
    calc_version         TEXT NOT NULL,
    inputs_hash          TEXT,
    derived_run_id       INTEGER REFERENCES derived_run(id),
    computed_at          TEXT NOT NULL
);

CREATE TABLE session_metrics (
    session_id            INTEGER PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    trade_count           INTEGER NOT NULL DEFAULT 0,
    win_count             INTEGER NOT NULL DEFAULT 0,
    loss_count            INTEGER NOT NULL DEFAULT 0,
    scratch_count         INTEGER NOT NULL DEFAULT 0,
    gross_pnl             REAL,
    fees                  REAL,
    net_pnl               REAL,
    total_r               REAL,
    max_drawdown_r        REAL,
    expectancy_r          REAL,
    opportunities_total   INTEGER NOT NULL DEFAULT 0,
    opportunities_taken   INTEGER NOT NULL DEFAULT 0,
    opportunities_missed  INTEGER NOT NULL DEFAULT 0,
    mechanical_r          REAL,             -- sum of mechanical_reference.r_multiple
    user_value_added_r    REAL,             -- total_r - mechanical_r
    process_score         REAL,             -- 0-100, see docs/02 §process score
    rule_adherence        INTEGER,          -- copied from checkin_post for query speed
    mistake_count         INTEGER NOT NULL DEFAULT 0,
    calc_version          TEXT NOT NULL,
    derived_run_id        INTEGER REFERENCES derived_run(id),
    computed_at           TEXT NOT NULL
);

-- Objective market state. Subjective impressions live in checkin_* / notes.
CREATE TABLE market_context_session (
    session_id        INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
    prior_high        REAL,
    prior_low         REAL,
    prior_close       REAL,
    overnight_high    REAL,
    overnight_low     REAL,
    gap_points        REAL,
    gap_atr_ratio     REAL,
    atr14             REAL,
    realized_vol_20d  REAL,
    trend_state       TEXT CHECK (trend_state IN ('up','down','range','undefined')),
    opening_range_high REAL,
    opening_range_low  REAL,
    opening_range_minutes INTEGER,
    session_high      REAL,
    session_low       REAL,
    session_range     REAL,
    vwap_close_dist   REAL,
    econ_events       TEXT,                 -- JSON array of {time, name, importance}
    data_source       TEXT NOT NULL,
    calc_version      TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    PRIMARY KEY (session_id, instrument_id)
);

CREATE TABLE market_context_trade (
    trade_id             INTEGER PRIMARY KEY REFERENCES trade(id) ON DELETE CASCADE,
    minutes_from_open    INTEGER,
    time_bucket          TEXT,             -- 'open_30' | 'mid_morning' | 'lunch' | 'pm' | 'close_30'
    day_of_week          INTEGER,          -- 1=Mon
    vwap_distance_ticks  REAL,
    nearest_level        TEXT,             -- 'PDH' | 'ONL' | 'ORH' | ...
    nearest_level_ticks  REAL,
    regime               TEXT,             -- 'trend_up' | 'trend_down' | 'chop' | 'expansion'
    atr_at_entry         REAL,
    calc_version         TEXT NOT NULL,
    computed_at          TEXT NOT NULL
);

-- =============================================================================
-- 8. MEDIA — screenshots and audio, referenced not embedded
-- =============================================================================

CREATE TABLE media_asset (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('screenshot','audio','export')),
    trade_id      INTEGER REFERENCES trade(id) ON DELETE CASCADE,
    session_id    INTEGER REFERENCES session(id) ON DELETE CASCADE,
    phase         TEXT CHECK (phase IN ('pre_entry','entry','manage','exit','post_exit','session')),
    timeframe     TEXT,                    -- '1m' | '5m' | '15m'
    captured_at   TEXT NOT NULL,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    width         INTEGER,
    height        INTEGER,
    capture_spec  TEXT,                    -- JSON: window, symbol, overlays requested
    overlays      TEXT,                    -- JSON: entry/exit/stop/target/level markers
    capture_status TEXT NOT NULL DEFAULT 'ok' CHECK (capture_status IN ('ok','failed','manual')),
    CHECK (trade_id IS NOT NULL OR session_id IS NOT NULL)
);
CREATE INDEX idx_media_trade ON media_asset(trade_id, phase);

-- =============================================================================
-- 9. AI_ANNOTATED — suggestions and narratives, always separable from human record
-- =============================================================================

CREATE TABLE ai_annotation (
    id              INTEGER PRIMARY KEY,
    target_type     TEXT NOT NULL CHECK (target_type IN ('trade','session','voice_note')),
    target_id       INTEGER NOT NULL,
    annotation_type TEXT NOT NULL CHECK (annotation_type IN ('tag','summary','question')),
    tag_code        TEXT REFERENCES mistake_tag(code),   -- set when annotation_type='tag'
    content         TEXT,                                -- summary text / rationale
    evidence        TEXT,                                -- JSON: fields & spans the model used
    confidence      REAL,
    model           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'suggested'
                      CHECK (status IN ('suggested','accepted','rejected','expired')),
    reviewed_at     TEXT
);
CREATE INDEX idx_ai_annotation_target ON ai_annotation(target_type, target_id, status);

CREATE TABLE ai_narrative (
    id            INTEGER PRIMARY KEY,
    scope         TEXT NOT NULL CHECK (scope IN ('session','week','month','trade')),
    scope_key     TEXT NOT NULL,            -- session_uid | '2026-W32' | '2026-08'
    facts         TEXT,                     -- JSON: the numeric facts handed to the model
    text          TEXT NOT NULL,
    model         TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    inputs_hash   TEXT,
    created_at    TEXT NOT NULL,
    superseded_by INTEGER REFERENCES ai_narrative(id),
    UNIQUE (scope, scope_key, created_at)
);

-- =============================================================================
-- 10. BLINDED_RESEARCH — never joined into a daily-UI view
-- =============================================================================
--
-- Features are stored keyed, not labelled, so a stray SELECT * in a daily view
-- cannot leak an interpretable astro state into the trader's expectations.

CREATE TABLE blinded_feature (
    id            INTEGER PRIMARY KEY,
    scope_type    TEXT NOT NULL CHECK (scope_type IN ('session','trade','day')),
    scope_id      TEXT NOT NULL,            -- session_uid / trade_uid / date
    feature_set   TEXT NOT NULL CHECK (feature_set IN
                    ('market_astro','personal_astro','lunar','transit','pof','money_house','other')),
    feature_key   TEXT NOT NULL,            -- opaque key, e.g. 'pa_f17'
    value_num     REAL,
    value_text    TEXT,
    engine        TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    computed_at   TEXT NOT NULL,
    UNIQUE (scope_type, scope_id, feature_set, feature_key, engine_version)
);
CREATE INDEX idx_blinded_scope ON blinded_feature(scope_type, scope_id);

-- Human-readable definitions live apart from the values, and are only joined
-- inside Research Mode after an unblind event is logged.
CREATE TABLE blinded_feature_dict (
    feature_key   TEXT PRIMARY KEY,
    feature_set   TEXT NOT NULL,
    label         TEXT NOT NULL,
    definition    TEXT,
    engine_version TEXT NOT NULL
);

CREATE TABLE preregistration (
    id             INTEGER PRIMARY KEY,
    question       TEXT NOT NULL,
    hypothesis     TEXT NOT NULL,
    feature_keys   TEXT NOT NULL,           -- JSON array
    outcome_metric TEXT NOT NULL,           -- 'r_multiple' | 'process_score' | 'rule_violation'
    min_n          INTEGER NOT NULL,
    analysis_plan  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    locked_hash    TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('draft','locked','analysed','abandoned'))
);

CREATE TABLE research_unblind_log (
    id                 INTEGER PRIMARY KEY,
    preregistration_id INTEGER REFERENCES preregistration(id),
    scope              TEXT NOT NULL,
    feature_sets       TEXT NOT NULL,
    reason             TEXT NOT NULL,
    unblinded_at       TEXT NOT NULL,
    actor              TEXT NOT NULL
);

-- =============================================================================
-- 11. SYSTEM EVENTS + REPORTS
-- =============================================================================

CREATE TABLE system_event (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER REFERENCES session(id) ON DELETE CASCADE,
    occurred_at   TEXT NOT NULL,
    source        TEXT NOT NULL,           -- 'dorb_bot' | 'traderspost' | 'journal_importer'
    level         TEXT NOT NULL CHECK (level IN ('info','warn','error')),
    event_type    TEXT NOT NULL,           -- 'order_rejected' | 'bot_started' | 'feed_gap'
    message       TEXT,
    payload       TEXT,
    raw_record_id INTEGER REFERENCES raw_record(id)
);
CREATE INDEX idx_system_event_session ON system_event(session_id, occurred_at);

CREATE TABLE report (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('weekly','monthly','quarterly')),
    period_key    TEXT NOT NULL,           -- '2026-W32' | '2026-08'
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    metrics       TEXT NOT NULL,           -- JSON: the computed report payload
    narrative_id  INTEGER REFERENCES ai_narrative(id),
    generated_at  TEXT NOT NULL,
    calc_version  TEXT NOT NULL,
    UNIQUE (kind, period_key, generated_at)
);

-- =============================================================================
-- 12. VIEWS — the only surface the daily UI is allowed to read
-- =============================================================================
-- No view in this section touches blinded_feature. Research Mode uses its own
-- views defined in 0002_research_views.sql and requires an unblind log entry.

CREATE VIEW v_session_daily AS
SELECT
    s.id                AS session_id,
    s.session_uid,
    s.session_date,
    s.iso_week,
    s.iso_month,
    s.status,
    a.label             AS account_label,
    s.mode,
    sm.trade_count,
    sm.net_pnl,
    sm.total_r,
    sm.process_score,
    sm.rule_adherence,
    sm.opportunities_total,
    sm.opportunities_taken,
    sm.opportunities_missed,
    sm.user_value_added_r,
    sm.mistake_count,
    cp.well_traded,
    cpre.energy, cpre.focus, cpre.stress, cpre.sleep_hours
FROM session s
JOIN account a          ON a.id = s.account_id
LEFT JOIN session_metrics sm ON sm.session_id = s.id
LEFT JOIN checkin_post cp    ON cp.session_id = s.id
LEFT JOIN checkin_pre  cpre  ON cpre.session_id = s.id;

CREATE VIEW v_trade_full AS
SELECT
    t.id AS trade_id,
    t.trade_uid,
    t.session_id,
    s.session_date,
    i.symbol,
    st.id       AS strategy_id,
    sv.version  AS strategy_version,
    sv.id       AS strategy_version_id,
    t.execution_mode,
    t.side,
    t.quantity,
    t.entry_at,
    t.exit_at,
    t.avg_entry_price,
    t.avg_exit_price,
    t.initial_stop,
    t.initial_target,
    t.exit_reason,
    t.was_manual_override,
    tm.net_pnl,
    tm.r_multiple,
    tm.mfe_r,
    tm.mae_r,
    tm.holding_seconds,
    tm.entry_slippage_ticks,
    tm.size_deviation_pct,
    tm.r_captured_pct,
    mr.r_multiple AS mechanical_r,
    o.status      AS opportunity_status
FROM trade t
JOIN session s        ON s.id = t.session_id
JOIN instrument i     ON i.id = t.instrument_id
LEFT JOIN strategy_version sv ON sv.id = t.strategy_version_id
LEFT JOIN strategy st         ON st.id = sv.strategy_id
LEFT JOIN trade_metrics tm    ON tm.trade_id = t.id
LEFT JOIN opportunity o       ON o.id = t.opportunity_id
LEFT JOIN mechanical_reference mr ON mr.opportunity_id = o.id;

-- Take rate / miss rate / discretionary value per strategy version.
CREATE VIEW v_strategy_scorecard AS
SELECT
    sv.id                AS strategy_version_id,
    sv.strategy_id,
    sv.version,
    sv.qualification_status,
    COUNT(o.id)                                                   AS opportunities,
    SUM(CASE WHEN o.status IN ('TAKEN','BOT_EXECUTED') THEN 1 ELSE 0 END) AS taken,
    SUM(CASE WHEN o.status = 'MISSED' THEN 1 ELSE 0 END)          AS missed,
    SUM(CASE WHEN o.status = 'SKIPPED_DISCRETIONARY' THEN 1 ELSE 0 END) AS skipped_discretionary,
    SUM(CASE WHEN o.status = 'SKIPPED_BY_RULE' THEN 1 ELSE 0 END) AS skipped_by_rule,
    ROUND(AVG(mr.r_multiple), 3)                                  AS mechanical_avg_r,
    ROUND(AVG(tm.r_multiple), 3)                                  AS actual_avg_r,
    ROUND(SUM(COALESCE(tm.r_multiple,0)) - SUM(COALESCE(mr.r_multiple,0)), 3) AS user_value_added_r
FROM strategy_version sv
LEFT JOIN opportunity o           ON o.strategy_version_id = sv.id
LEFT JOIN mechanical_reference mr ON mr.opportunity_id = o.id
LEFT JOIN trade t                 ON t.opportunity_id = o.id
LEFT JOIN trade_metrics tm        ON tm.trade_id = t.id
GROUP BY sv.id;

CREATE VIEW v_mistake_frequency AS
SELECT
    ht.tag_code,
    mt.label,
    mt.category,
    COUNT(*)                        AS occurrences,
    COUNT(DISTINCT COALESCE(ht.session_id,
        (SELECT session_id FROM trade WHERE trade.id = ht.trade_id))) AS sessions_affected
FROM human_tag ht
JOIN mistake_tag mt ON mt.code = ht.tag_code
GROUP BY ht.tag_code;
