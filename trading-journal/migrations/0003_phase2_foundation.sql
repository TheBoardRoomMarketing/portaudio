-- =============================================================================
-- Trading Journal — Phase 2 production foundation
-- Migration 0003_phase2_foundation
-- =============================================================================
--
-- Applies four director rulings from the Phase 1 review:
--
--   D9  session grain: a calendar date may hold several sessions
--       (NY_AM / NY_PM / OVERNIGHT / OTHER) without a future migration.
--   D1  the process score is renamed to SELF_REPORTED_PROCESS_INDEX so its
--       name cannot imply objectivity, and a separate nullable column is
--       reserved for the MECHANICAL_CONFORMANCE_INDEX. The two are never
--       merged into one number.
--   D4  import provenance is recorded on the trade itself, so manually keyed
--       data can never masquerade as complete automatic coverage.
--   —   generic CSV mapping profiles, so a real broker export can be mapped
--       later without touching journal code.
--
-- `session` is rebuilt rather than altered because its UNIQUE(session_date,
-- account_id) table constraint owns an implicit index that SQLite cannot drop.
-- The runner wraps this file in the standard rebuild envelope: foreign keys
-- off, transaction, foreign_key_check, commit, foreign keys on.
-- =============================================================================

-- ---------------------------------------------------------------- views down
-- Every view touching `session` or `process_score` is dropped here and
-- recreated at the bottom of this file.
DROP VIEW IF EXISTS v_session_daily;
DROP VIEW IF EXISTS v_trade_full;
DROP VIEW IF EXISTS r_session_features;
DROP VIEW IF EXISTS r_manual_vs_mechanical;
DROP VIEW IF EXISTS r_state_vs_violations;
DROP VIEW IF EXISTS r_feature_sample_sizes;

-- ------------------------------------------------------- D9: session grain
CREATE TABLE session_new (
    id                  INTEGER PRIMARY KEY,
    session_uid         TEXT NOT NULL UNIQUE,   -- '2026-08-07:NY_AM:ACCT1'
    session_date        TEXT NOT NULL,          -- local calendar date of the open
    session_kind        TEXT NOT NULL DEFAULT 'NY_AM'
                          CHECK (session_kind IN ('NY_AM','NY_PM','OVERNIGHT','OTHER')),
    tz                  TEXT NOT NULL DEFAULT 'America/New_York',
    account_id          INTEGER NOT NULL REFERENCES account(id),
    mode                TEXT NOT NULL CHECK (mode IN ('paper','live','sim_eval')),
    status              TEXT NOT NULL CHECK (status IN
                          ('planned','checked_in','active','closed','no_trade','skipped')),
    started_at          TEXT,
    ended_at            TEXT,
    planned_max_risk_r  REAL,
    planned_max_loss    REAL,
    daily_loss_limit    REAL,
    max_trades_planned  INTEGER,
    iso_week            TEXT,
    iso_month           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    -- one session per (date, kind, account); several kinds per date are fine
    UNIQUE (session_date, session_kind, account_id)
);

INSERT INTO session_new (
    id, session_uid, session_date, session_kind, tz, account_id, mode, status,
    started_at, ended_at, planned_max_risk_r, planned_max_loss, daily_loss_limit,
    max_trades_planned, iso_week, iso_month, created_at, updated_at)
SELECT
    id, REPLACE(session_uid, ':MAIN:', ':NY_AM:'), session_date, 'NY_AM', tz,
    account_id, mode, status, started_at, ended_at, planned_max_risk_r,
    planned_max_loss, daily_loss_limit, max_trades_planned, iso_week, iso_month,
    created_at, updated_at
FROM session;

DROP TABLE session;
ALTER TABLE session_new RENAME TO session;

CREATE INDEX idx_session_date  ON session(session_date);
CREATE INDEX idx_session_week  ON session(iso_week);
CREATE INDEX idx_session_month ON session(iso_month);
CREATE INDEX idx_session_kind  ON session(session_date, session_kind);

-- ------------------------------------------- D1: two indices, never merged
ALTER TABLE session_metrics RENAME COLUMN process_score TO self_reported_process_index;
ALTER TABLE session_metrics ADD COLUMN mechanical_conformance_index REAL;

-- ------------------------------------------- D4: capture provenance on trade
-- 'manual' means a human keyed it; it is never silently upgraded to 'import'.
ALTER TABLE trade ADD COLUMN entry_source TEXT NOT NULL DEFAULT 'manual'
    CHECK (entry_source IN ('manual','file_import','api_import','bot_log','backfilled'));
ALTER TABLE trade ADD COLUMN import_batch_id INTEGER REFERENCES raw_import_batch(id);

-- MFE/MAE have no source until a market-data enricher exists. Until then they
-- are optionally reported by the human, and the derived R values are computed
-- from these prices — so the provenance chain stays intact either way.
ALTER TABLE trade ADD COLUMN reported_mfe_price REAL;
ALTER TABLE trade ADD COLUMN reported_mae_price REAL;

-- ------------------------------------------- generic broker import mapping
CREATE TABLE import_profile (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,        -- 'tradovate_performance_csv'
    kind           TEXT NOT NULL DEFAULT 'csv' CHECK (kind IN ('csv')),
    -- JSON: {"exec_id":"Order ID","filled_at":"Timestamp","price":"Price", ...}
    column_map     TEXT NOT NULL,
    datetime_format TEXT,                        -- strptime pattern, NULL = ISO
    source_tz      TEXT NOT NULL DEFAULT 'America/New_York',
    side_values    TEXT,                         -- JSON: {"buy":["Buy","B"], ...}
    symbol_map     TEXT,                         -- JSON: {"MESU6":"MES"}
    fee_columns    TEXT,                         -- JSON: ["Commission","Fees"]
    notes          TEXT,
    created_at     TEXT NOT NULL,
    verified_against_sample INTEGER NOT NULL DEFAULT 0 CHECK (verified_against_sample IN (0,1))
);

-- ------------------------------------------- scheduling hook bookkeeping
-- The scheduler is the operating system. This table is only the record of what
-- ran, so a missed check-in prompt is visible rather than silently absent.
CREATE TABLE job_run (
    id           INTEGER PRIMARY KEY,
    job          TEXT NOT NULL CHECK (job IN
                   ('pre_session_checkin','post_session_checkout','weekly_review',
                    'monthly_review','backup','recompute','import')),
    scheduled_for TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('ok','failed','skipped')),
    detail       TEXT
);
CREATE INDEX idx_job_run_job ON job_run(job, started_at);

-- ---------------------------------------------------------------- views up
CREATE VIEW v_session_daily AS
SELECT
    s.id                AS session_id,
    s.session_uid,
    s.session_date,
    s.session_kind,
    s.iso_week,
    s.iso_month,
    s.status,
    a.label             AS account_label,
    s.mode,
    sm.trade_count,
    sm.net_pnl,
    sm.total_r,
    sm.self_reported_process_index,
    sm.mechanical_conformance_index,
    sm.rule_adherence,
    sm.opportunities_total,
    sm.opportunities_taken,
    sm.opportunities_missed,
    sm.user_value_added_r,
    sm.mistake_count,
    cp.well_traded,
    cpre.energy, cpre.focus, cpre.stress, cpre.sleep_hours
FROM session s
JOIN account a               ON a.id = s.account_id
LEFT JOIN session_metrics sm ON sm.session_id = s.id
LEFT JOIN checkin_post cp    ON cp.session_id = s.id
LEFT JOIN checkin_pre  cpre  ON cpre.session_id = s.id;

-- Daily review aggregates whatever sessions the date holds (D9).
CREATE VIEW v_trading_day AS
SELECT
    s.session_date,
    s.account_id,
    COUNT(*)                             AS session_count,
    GROUP_CONCAT(s.session_kind, '+')    AS kinds,
    SUM(COALESCE(sm.trade_count, 0))     AS trade_count,
    SUM(COALESCE(sm.net_pnl, 0))         AS net_pnl,
    SUM(COALESCE(sm.total_r, 0))         AS total_r,
    AVG(sm.self_reported_process_index)  AS self_reported_process_index,
    SUM(COALESCE(sm.mistake_count, 0))   AS mistake_count,
    SUM(COALESCE(sm.opportunities_total, 0))  AS opportunities_total,
    SUM(COALESCE(sm.opportunities_missed, 0)) AS opportunities_missed
FROM session s
LEFT JOIN session_metrics sm ON sm.session_id = s.id
GROUP BY s.session_date, s.account_id;

CREATE VIEW v_trade_full AS
SELECT
    t.id AS trade_id,
    t.trade_uid,
    t.session_id,
    s.session_date,
    s.session_kind,
    i.symbol,
    st.id       AS strategy_id,
    sv.version  AS strategy_version,
    sv.id       AS strategy_version_id,
    t.execution_mode,
    t.entry_source,
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

-- ---- research views (r_ prefix; excluded from the daily connection) --------
CREATE VIEW r_session_features AS
SELECT
    s.session_uid, s.session_date, s.session_kind, s.iso_week, s.iso_month,
    sm.trade_count, sm.total_r, sm.self_reported_process_index,
    sm.mechanical_conformance_index, sm.mistake_count, sm.user_value_added_r,
    sm.opportunities_total, sm.opportunities_missed,
    cpre.sleep_hours, cpre.sleep_quality, cpre.energy, cpre.focus,
    cpre.stress, cpre.irritability, cpre.impulsivity, cpre.confidence,
    cpre.desire_to_trade, cpre.money_pressure,
    cp.execution_quality, cp.rule_adherence, cp.patience, cp.emotional_control,
    cp.overtraded, cp.revenge_trade, cp.stop_moved, cp.oversized, cp.well_traded,
    bf.feature_set, bf.feature_key, bf.value_num, bf.value_text
FROM session s
LEFT JOIN session_metrics sm ON sm.session_id = s.id
LEFT JOIN checkin_pre  cpre  ON cpre.session_id = s.id
LEFT JOIN checkin_post cp    ON cp.session_id = s.id
LEFT JOIN blinded_feature bf ON bf.scope_type = 'session' AND bf.scope_id = s.session_uid;

CREATE VIEW r_feature_sample_sizes AS
SELECT
    bf.feature_set,
    bf.feature_key,
    COUNT(DISTINCT bf.scope_id) AS n_observations,
    MIN(s.session_date)         AS first_observed,
    MAX(s.session_date)         AS last_observed,
    CASE
        WHEN COUNT(DISTINCT bf.scope_id) < 60  THEN 'insufficient'
        WHEN COUNT(DISTINCT bf.scope_id) < 150 THEN 'provisional'
        ELSE 'reportable'
    END AS sample_status
FROM blinded_feature bf
LEFT JOIN session s ON s.session_uid = bf.scope_id
GROUP BY bf.feature_set, bf.feature_key;

CREATE VIEW r_manual_vs_mechanical AS
SELECT
    s.session_date, sv.strategy_id, sv.version, o.status, o.decided_by,
    mr.r_multiple  AS mechanical_r,
    tm.r_multiple  AS actual_r,
    COALESCE(tm.r_multiple, 0) - COALESCE(mr.r_multiple, 0) AS delta_r,
    t.was_manual_override
FROM opportunity o
JOIN session s                    ON s.id = o.session_id
JOIN strategy_version sv          ON sv.id = o.strategy_version_id
LEFT JOIN mechanical_reference mr ON mr.opportunity_id = o.id
LEFT JOIN trade t                 ON t.opportunity_id = o.id
LEFT JOIN trade_metrics tm        ON tm.trade_id = t.id;

CREATE VIEW r_state_vs_violations AS
SELECT
    s.session_uid, s.session_date,
    cpre.sleep_hours, cpre.energy, cpre.focus, cpre.stress, cpre.impulsivity,
    COALESCE(sm.mistake_count, 0) AS mistake_count,
    CASE WHEN COALESCE(sm.mistake_count, 0) > 0 THEN 1 ELSE 0 END AS any_violation,
    sm.self_reported_process_index,
    sm.total_r
FROM session s
LEFT JOIN checkin_pre cpre   ON cpre.session_id = s.id
LEFT JOIN session_metrics sm ON sm.session_id = s.id;
