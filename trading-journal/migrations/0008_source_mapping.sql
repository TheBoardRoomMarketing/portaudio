-- =============================================================================
-- Trading Journal — external source mapping, provenance and sync state
-- Migration 0008_source_mapping
-- =============================================================================
--
-- Built during the TradeSyncer ingestion investigation, and deliberately NOT
-- specific to TradeSyncer. Whatever route wins — an API, an export endpoint, a
-- scheduled download or a hand-uploaded CSV — three things are needed and are
-- the same in every case:
--
--   1. **A mapping from a source's account identifier to ours.** Account names
--      are not hardcoded anywhere. "Tradeify 100k" is a label Zack chose and
--      may change; the source's own ID is what survives, and the mapping
--      carries activation dates so an account that was retired stays
--      interpretable in old data.
--   2. **Provenance on every imported row.** Which source, which record in that
--      source, which adapter version, and a hash of the raw payload. When a
--      parser changes — and it will — every record it produced has to be
--      traceable back to the bytes it came from.
--   3. **Sync state, per source and account.** A watermark so ingestion reads
--      only what is new, and a failure record so a source being down is a
--      visible fact rather than a day that quietly has no trades in it.
--
-- Nothing here connects to anything. No credential column exists in this
-- migration and none will be added without director review; sync state holds
-- watermarks and outcomes, never secrets.
-- =============================================================================

-- ------------------------------------------------------------ account mapping
CREATE TABLE IF NOT EXISTS source_account_map (
    id                INTEGER PRIMARY KEY,
    source            TEXT    NOT NULL,      -- 'tradesyncer', 'tradesea', 'csv:<profile>'
    source_account_id TEXT    NOT NULL,      -- the source's own identifier
    source_label      TEXT,                  -- what the source calls it, for humans
    account_id        INTEGER NOT NULL REFERENCES account(id),
    -- Role as the SOURCE reports it, which is evidence rather than truth. The
    -- journal's own account.role stays authoritative; a disagreement between
    -- the two is worth seeing, not worth silently resolving.
    source_role       TEXT    CHECK (source_role IN ('LEAD','FOLLOWER','UNKNOWN')),
    active_from       TEXT    NOT NULL,
    active_to         TEXT,
    created_at        TEXT    NOT NULL,
    UNIQUE (source, source_account_id, active_from)
);

CREATE INDEX IF NOT EXISTS idx_source_map_lookup
    ON source_account_map(source, source_account_id);

-- ---------------------------------------------------------------- provenance
ALTER TABLE execution_event ADD COLUMN source_record_id TEXT;
ALTER TABLE execution_event ADD COLUMN source_payload_sha256 TEXT;
ALTER TABLE execution_event ADD COLUMN adapter_version TEXT;

CREATE INDEX IF NOT EXISTS idx_event_source_record
    ON execution_event(source, source_record_id);

-- ---------------------------------------------------------------- sync state
-- One row per source per account. Holds watermarks and outcomes only — never a
-- token, cookie, password or key. Credential storage is a separate decision
-- that has not been made.
CREATE TABLE IF NOT EXISTS source_sync_state (
    id                    INTEGER PRIMARY KEY,
    source                TEXT NOT NULL,
    source_account_id     TEXT,
    -- The watermark. Whichever of these a source supports is the one used;
    -- having all three costs nothing and avoids a migration per source.
    last_seen_at          TEXT,
    last_cursor           TEXT,
    last_record_id        TEXT,
    last_attempt_at       TEXT,
    last_success_at       TEXT,
    last_status           TEXT CHECK (last_status IN
                              ('OK','PARTIAL','FAILED','NEVER_RUN')) DEFAULT 'NEVER_RUN',
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    records_last_run      INTEGER,
    updated_at            TEXT,
    UNIQUE (source, source_account_id)
);

-- What the interface reads to say "last synced 14 minutes ago" or "TradeSyncer
-- has been failing since 09:12". Silence about a broken sync is the failure
-- mode worth designing against: a day with no trades and a day whose trades
-- never arrived look identical unless something says so.
DROP VIEW IF EXISTS v_sync_health;
CREATE VIEW v_sync_health AS
SELECT
    s.source,
    s.source_account_id,
    m.source_label,
    a.label                AS account_label,
    s.last_success_at,
    s.last_attempt_at,
    s.last_status,
    s.consecutive_failures,
    s.records_last_run,
    CASE
        WHEN s.last_status = 'NEVER_RUN'          THEN 'never run'
        WHEN s.consecutive_failures >= 3          THEN 'failing'
        WHEN s.last_status = 'FAILED'             THEN 'last run failed'
        WHEN s.last_status = 'PARTIAL'            THEN 'incomplete'
        ELSE 'ok'
    END                    AS health
FROM source_sync_state s
LEFT JOIN source_account_map m
       ON m.source = s.source AND m.source_account_id = s.source_account_id
      AND m.active_to IS NULL
LEFT JOIN account a ON a.id = m.account_id;
