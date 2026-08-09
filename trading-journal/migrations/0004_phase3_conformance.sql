-- =============================================================================
-- Trading Journal — Phase 3: conformance, telemetry, blinded provenance
-- Migration 0004_phase3_conformance
-- =============================================================================
--
-- Four additions, all in service of real daily use:
--
--   MECHANICAL_CONFORMANCE_INDEX_V1 — a second process view built only from
--     objectively observable fields. Stored alongside the self-reported index
--     and never merged with it. The components that went into a score are
--     stored with it, because a score whose composition varies by session is
--     uninterpretable without knowing what was in it.
--
--   Planned trading window — needed by the "traded outside the planned window"
--     component. Defaults per session kind; NULL means the component does not
--     apply rather than that it passed.
--
--   Friction telemetry — whether the optional section was opened, and how long
--     manual trade entry took. Evidence before form redesign.
--
--   Blinded provenance — every research row carries its session, engine hash
--     and source hypothesis, so a feature computed under one version of a
--     hypothesis is never silently pooled with another.
-- =============================================================================

-- ------------------------------------------------- conformance on the session
ALTER TABLE session_metrics ADD COLUMN conformance_index_version TEXT;
-- JSON arrays: which components contributed, and which could not be measured.
-- A missing component is NEVER scored as perfect; it is excluded from the
-- normalisation and named here.
ALTER TABLE session_metrics ADD COLUMN conformance_components_available TEXT;
ALTER TABLE session_metrics ADD COLUMN conformance_components_missing TEXT;
-- Per-component sub-scores, so the interface can explain the number.
ALTER TABLE session_metrics ADD COLUMN conformance_detail TEXT;

-- ------------------------------------------------------ planned session window
-- Local wall-clock times in the session's own timezone. NULL = no declared
-- window, which makes the window component not applicable for that session.
ALTER TABLE session ADD COLUMN planned_window_start TEXT;
ALTER TABLE session ADD COLUMN planned_window_end TEXT;

UPDATE session SET planned_window_start = '09:30', planned_window_end = '12:00'
    WHERE session_kind = 'NY_AM' AND planned_window_start IS NULL;
UPDATE session SET planned_window_start = '12:00', planned_window_end = '16:00'
    WHERE session_kind = 'NY_PM' AND planned_window_start IS NULL;
UPDATE session SET planned_window_start = '18:00', planned_window_end = '09:30'
    WHERE session_kind = 'OVERNIGHT' AND planned_window_start IS NULL;

-- ------------------------------------------------------------ friction telemetry
ALTER TABLE checkin_pre  ADD COLUMN optional_opened INTEGER CHECK (optional_opened IN (0,1));
ALTER TABLE checkin_post ADD COLUMN optional_opened INTEGER CHECK (optional_opened IN (0,1));
ALTER TABLE trade ADD COLUMN capture_seconds INTEGER;

-- ------------------------------------------------------------ blinded provenance
-- session_id joins the research layer to the session directly. scope_type /
-- scope_id stay for non-session grains; this is the common case made explicit.
ALTER TABLE blinded_feature ADD COLUMN session_id INTEGER REFERENCES session(id);
ALTER TABLE blinded_feature ADD COLUMN engine_hash TEXT;
-- Which hypothesis or feature-set revision produced this value. Two rows with
-- the same key but different source hypotheses are different variables.
ALTER TABLE blinded_feature ADD COLUMN source_hypothesis TEXT;

UPDATE blinded_feature
   SET session_id = (SELECT id FROM session WHERE session.session_uid = blinded_feature.scope_id)
 WHERE scope_type = 'session' AND session_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_blinded_session ON blinded_feature(session_id);

-- --------------------------------------------------------------- report kinds
-- Weekly reports are generated descriptively. The generator has an allowlist of
-- sections; `sections` records what was actually included so a later change to
-- that allowlist is visible in the record rather than silent.
ALTER TABLE report ADD COLUMN sections TEXT;
ALTER TABLE report ADD COLUMN generator_version TEXT;

-- ----------------------------------------------------------------- views up
DROP VIEW IF EXISTS v_session_daily;
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
    sm.conformance_index_version,
    sm.rule_adherence,
    sm.opportunities_total,
    sm.opportunities_taken,
    sm.opportunities_missed,
    sm.user_value_added_r,
    sm.mistake_count,
    cp.well_traded,
    cp.fill_seconds     AS post_fill_seconds,
    cpre.fill_seconds   AS pre_fill_seconds,
    cpre.energy, cpre.focus, cpre.stress, cpre.sleep_hours
FROM session s
JOIN account a               ON a.id = s.account_id
LEFT JOIN session_metrics sm ON sm.session_id = s.id
LEFT JOIN checkin_post cp    ON cp.session_id = s.id
LEFT JOIN checkin_pre  cpre  ON cpre.session_id = s.id;

-- Capture completeness and friction, for the real-use review. Descriptive only.
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
