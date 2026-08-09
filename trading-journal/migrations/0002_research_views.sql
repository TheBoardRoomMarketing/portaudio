-- =============================================================================
-- Trading Journal v1 — Research Mode views
-- Migration 0002_research_views
-- =============================================================================
--
-- Nothing in this file may be read by the daily UI. The application layer
-- enforces that by opening the daily connection with a view allow-list that
-- excludes every `r_` prefixed view; this file is the second line of defence,
-- not the first.
--
-- Every view here is deliberately *keyed, not labelled*: feature_key stays
-- opaque ('pa_f17'). Joining blinded_feature_dict to get a human label is a
-- separate, logged step performed only under a locked preregistration.
-- =============================================================================

-- Analysis-ready session grain. Blinded features arrive as opaque keys.
CREATE VIEW r_session_features AS
SELECT
    s.session_uid,
    s.session_date,
    s.iso_week,
    s.iso_month,
    sm.trade_count,
    sm.total_r,
    sm.process_score,
    sm.mistake_count,
    sm.user_value_added_r,
    sm.opportunities_total,
    sm.opportunities_missed,
    cpre.sleep_hours, cpre.sleep_quality, cpre.energy, cpre.focus,
    cpre.stress, cpre.irritability, cpre.impulsivity, cpre.confidence,
    cpre.desire_to_trade, cpre.money_pressure,
    cp.execution_quality, cp.rule_adherence, cp.patience, cp.emotional_control,
    cp.overtraded, cp.revenge_trade, cp.stop_moved, cp.oversized,
    cp.well_traded,
    bf.feature_set,
    bf.feature_key,
    bf.value_num,
    bf.value_text
FROM session s
LEFT JOIN session_metrics sm ON sm.session_id = s.id
LEFT JOIN checkin_pre  cpre  ON cpre.session_id = s.id
LEFT JOIN checkin_post cp    ON cp.session_id = s.id
LEFT JOIN blinded_feature bf ON bf.scope_type = 'session' AND bf.scope_id = s.session_uid;

-- Sample-size guard. Research Mode must render the warning band whenever
-- n < min_n_for_signal; the UI reads this view, it does not recount.
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

-- Discretion audit: mechanical counterfactual vs what the human actually did.
CREATE VIEW r_manual_vs_mechanical AS
SELECT
    s.session_date,
    sv.strategy_id,
    sv.version,
    o.status,
    o.decided_by,
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

-- Behaviour grain: does a pre-session state precede rule violations?
CREATE VIEW r_state_vs_violations AS
SELECT
    s.session_uid,
    s.session_date,
    cpre.sleep_hours, cpre.energy, cpre.focus, cpre.stress, cpre.impulsivity,
    COALESCE(sm.mistake_count, 0) AS mistake_count,
    CASE WHEN COALESCE(sm.mistake_count, 0) > 0 THEN 1 ELSE 0 END AS any_violation,
    sm.process_score,
    sm.total_r
FROM session s
LEFT JOIN checkin_pre cpre   ON cpre.session_id = s.id
LEFT JOIN session_metrics sm ON sm.session_id = s.id;
