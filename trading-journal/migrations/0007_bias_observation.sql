-- =============================================================================
-- Trading Journal — bias methodology observation layer
-- Migration 0007_bias_observation
-- =============================================================================
--
-- Three candidate methodologies for judging a morning bias run in dry-run over
-- the first 20 complete real trading days, so the choice between them is made
-- from disagreements rather than from argument. None is approved, none is
-- canonical, and none may touch anything operational.
--
-- The isolation is structural, not a convention:
--
--   * The table is named with the `r_` prefix, which the daily connection's
--     SQLite authorizer already refuses outright. A normal application
--     connection cannot read this table at all, so no view, report or screen
--     can accidentally join to it. Reaching it takes an explicitly
--     unrestricted connection, which is the same gate the blinded research
--     layer sits behind.
--   * daily_bias.outcome stays NULL. Candidate labels live here and only here.
--     A verdict in this table is an observation about a *methodology*, not a
--     fact about the day.
--
-- What must never happen: a candidate label leaking into P&L, process scores,
-- conformance, review prompts or anything shown during a trading day. A label
-- Zack has seen is a label that can change how he reads tomorrow's market,
-- which would contaminate the very evidence this table exists to gather.
-- =============================================================================

CREATE TABLE IF NOT EXISTS r_bias_candidate_run (
    id                INTEGER PRIMARY KEY,
    -- Plain columns, not foreign keys. A hard FK from the isolated research
    -- layer back into an operational table would make every delete on that
    -- table require read access to an `r_` object — which the daily
    -- connection's authorizer refuses, so `journal demo --purge` would fail
    -- with a confusing permissions error. The isolation has to cut both ways.
    daily_bias_id     INTEGER NOT NULL,
    trading_day_id    INTEGER NOT NULL,
    day_date          TEXT    NOT NULL,
    methodology       TEXT    NOT NULL,
    -- NULL outcome is a real result: the methodology declined to judge. It is
    -- stored rather than skipped, because how often a candidate stays silent is
    -- one of the things being compared.
    outcome           TEXT    CHECK (outcome IN
                          ('CORRECT','PARTIALLY_CORRECT','INCORRECT','INDETERMINATE')),
    unavailable_reason TEXT,
    inputs            TEXT,               -- JSON: the numbers that produced it
    detail            TEXT,
    -- The price envelope this verdict was computed from, kept alongside so a
    -- rerun with corrected prices is visibly a different run.
    price_hash        TEXT,
    engine_version    TEXT    NOT NULL,
    computed_at       TEXT    NOT NULL,
    UNIQUE (daily_bias_id, methodology, engine_version)
);

CREATE INDEX IF NOT EXISTS idx_r_bias_candidate_day
    ON r_bias_candidate_run(day_date);

-- The session price used for a day's dry run. Recorded once per day rather than
-- per methodology so all three candidates are provably judged on identical
-- input — a comparison where the candidates saw different prices would compare
-- nothing.
CREATE TABLE IF NOT EXISTS r_session_price (
    trading_day_id  INTEGER PRIMARY KEY,   -- deliberately not a foreign key; see above
    day_date        TEXT NOT NULL,
    session_open    REAL,
    session_high    REAL,
    session_low     REAL,
    session_close   REAL,
    atr             REAL,
    source          TEXT NOT NULL,        -- where the prices came from
    recorded_at     TEXT NOT NULL
);

-- Observation progress. A view rather than a counter so it cannot drift from
-- the rows it describes.
DROP VIEW IF EXISTS r_bias_observation_progress;
CREATE VIEW r_bias_observation_progress AS
SELECT
    (SELECT COUNT(*) FROM trading_day d
      WHERE d.is_demo = 0
        AND EXISTS (SELECT 1 FROM daily_bias b
                     WHERE b.trading_day_id = d.id AND b.is_demo = 0)
        AND EXISTS (SELECT 1 FROM r_session_price p
                     WHERE p.trading_day_id = d.id AND p.session_close IS NOT NULL)
    )                                                   AS complete_days,
    20                                                  AS target_days,
    (SELECT COUNT(DISTINCT daily_bias_id) FROM r_bias_candidate_run) AS days_run,
    (SELECT COUNT(*) FROM r_bias_candidate_run)         AS verdicts_stored;
