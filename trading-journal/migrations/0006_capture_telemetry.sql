-- =============================================================================
-- Trading Journal — capture telemetry on the day model, and one capture field
-- Migration 0006_capture_telemetry
-- =============================================================================
--
-- Phase 3 measured capture friction against the session model. The real
-- workflow runs on trading_day / logical_trade, so the telemetry has to move
-- with it or it measures a model nobody uses.
--
-- Two additions, both nullable, both additive — no table is rebuilt and no
-- existing row changes meaning:
--
--   1. daily_bias.invalidation_level — the numeric level, alongside the prose.
--      "Wrong below 23,140" written as 23140 is the difference between a bias
--      that can be judged on its own stated terms and one that cannot. Nothing
--      scores it yet; journal/bias_outcome.py holds three candidate
--      methodologies and none is approved. Capturing the number now is what
--      makes that decision possible later, and it costs one field.
--
--   2. trading_day.close_seconds — how long the evening reflection took.
--      The morning is already measured (daily_bias.capture_seconds) and each
--      trade review is measured (trade_annotation.review_seconds); the evening
--      was the gap.
--
-- Both are optional. A missing timing is NULL and stays out of the medians —
-- an unmeasured capture is not a fast one.
-- =============================================================================

ALTER TABLE daily_bias ADD COLUMN invalidation_level REAL;
ALTER TABLE trading_day ADD COLUMN close_seconds INTEGER;

-- ----------------------------------------------------------------- the view
-- One row per real trading day, carrying what capture actually cost that day.
-- Demo rows are excluded here rather than at every call site: this view feeds
-- friction reporting, and synthetic days would flatter every median in it.
DROP VIEW IF EXISTS v_day_capture_quality;
CREATE VIEW v_day_capture_quality AS
SELECT
    d.id                                        AS trading_day_id,
    d.day_date,
    d.iso_week,
    d.status,

    -- morning
    CASE WHEN b.id IS NULL THEN 0 ELSE 1 END    AS has_bias,
    b.capture_seconds                           AS bias_seconds,
    CASE WHEN b.invalidation IS NULL OR b.invalidation = '' THEN 0 ELSE 1 END
                                                AS has_invalidation_text,
    CASE WHEN b.invalidation_level IS NULL THEN 0 ELSE 1 END
                                                AS has_invalidation_level,
    (SELECT COUNT(*) FROM bias_amendment ba WHERE ba.daily_bias_id = b.id)
                                                AS bias_amendments,

    -- the day's trades
    (SELECT COUNT(*) FROM logical_trade t
      WHERE t.trading_day_id = d.id AND t.is_demo = 0)                    AS trades,
    (SELECT COUNT(*) FROM logical_trade t
      WHERE t.trading_day_id = d.id AND t.is_demo = 0
        AND t.review_state = 'REVIEWED')                                  AS trades_reviewed,
    (SELECT COUNT(*) FROM logical_trade t
      WHERE t.trading_day_id = d.id AND t.is_demo = 0
        AND t.grouping_confidence = 'NEEDS_GROUPING_REVIEW')              AS trades_needing_grouping,
    (SELECT COUNT(*) FROM logical_trade t
      JOIN execution_event e ON e.logical_trade_id = t.id
      WHERE t.trading_day_id = d.id AND t.is_demo = 0
        AND e.source = 'manual')                                          AS manual_events,
    (SELECT COUNT(*) FROM logical_trade t
      JOIN execution_event e ON e.logical_trade_id = t.id
      WHERE t.trading_day_id = d.id AND t.is_demo = 0
        AND e.source <> 'manual')                                         AS imported_events,

    -- evening
    CASE WHEN d.went_well IS NULL AND d.went_poorly IS NULL AND d.lesson IS NULL
         THEN 0 ELSE 1 END                      AS has_reflection,
    d.close_seconds,

    -- what the evening cost in total, review by review
    (SELECT SUM(a.review_seconds) FROM trade_annotation a
      JOIN logical_trade t ON t.id = a.logical_trade_id
      WHERE t.trading_day_id = d.id AND t.is_demo = 0)                    AS review_seconds_total,

    (SELECT COUNT(*) FROM voice_note v
      JOIN logical_trade t ON t.id = v.logical_trade_id
      WHERE t.trading_day_id = d.id AND t.is_demo = 0)                    AS voice_notes,
    (SELECT COUNT(*) FROM media_asset m
      JOIN logical_trade t ON t.id = m.logical_trade_id
      WHERE t.trading_day_id = d.id AND t.is_demo = 0)                    AS media_assets

FROM trading_day d
LEFT JOIN daily_bias b ON b.trading_day_id = d.id AND b.is_demo = 0
WHERE d.is_demo = 0;
