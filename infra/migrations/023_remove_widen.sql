-- 023: PR5 — remove "widen" as an LLM stop action (safety-critical).
--
-- Code-side change: pydantic_models.StopAdjust no longer accepts "widen";
-- any widen response is coerced to "hold" with a loud log. LLM prompt
-- template updated to list hold/tighten/exit_now only.
--
-- DB-side change: create stop_adjust_decisions to persist every
-- stop_adjust verdict going forward. The legacy_widen_action column
-- exists so that (a) the coercion path can still record "the LLM said
-- widen but we hold-ed it" for audit, and (b) any future historical
-- backfill of widen decisions from other sources (audit_log text scans,
-- log archives) has somewhere to land.

BEGIN;

CREATE TABLE IF NOT EXISTS stop_adjust_decisions (
  id                    BIGSERIAL PRIMARY KEY,
  ts                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  position_id           BIGINT REFERENCES positions(id),
  symbol                TEXT NOT NULL,
  entry_price           NUMERIC,
  current_price         NUMERIC,
  stop_before           NUMERIC,
  stop_after            NUMERIC,
  action                TEXT NOT NULL,  -- coerced value (hold/tighten/exit_now)
  new_stop_pct          NUMERIC,
  confidence            NUMERIC,
  reasoning             TEXT,
  legacy_widen_action   BOOLEAN NOT NULL DEFAULT FALSE,
  raw_response          JSONB
);
CREATE INDEX IF NOT EXISTS stop_adjust_decisions_pos_idx
    ON stop_adjust_decisions (position_id, ts DESC);
CREATE INDEX IF NOT EXISTS stop_adjust_decisions_legacy_idx
    ON stop_adjust_decisions (legacy_widen_action, ts DESC)
    WHERE legacy_widen_action = TRUE;

COMMIT;
