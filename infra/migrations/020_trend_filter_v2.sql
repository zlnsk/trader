-- 020: PR2 — trend filter v2 scaffolding (flag-gated, default off).
--
-- Adds the columns and config keys needed to switch the trend filter to a
-- stricter canonical-Connors/Alvarez rule without removing v1. Callers
-- branch on TREND_FILTER_V2_ENABLED in bot/bot/strategy.py; v1 path is
-- byte-identical to the pre-PR2 behaviour.
--
--   TREND_FILTER_V2_ENABLED       = false   — master flag (off by default
--     per PR spec ground rule; flip manually after review)
--   TREND_TOLERANCE_PCT_V2        = -2.0    — v2 tolerance: accept when
--     close ≥ SMA200 × (1 + -2.0/100). Tighter than v1's -5.0.
--
-- Schema:
--   slot_profiles.require_uptrend_50_200 — per-slot stricter rule. When
--     TRUE and TREND_FILTER_V2_ENABLED is on, also require
--     SMA50 > SMA200 AND price > SMA200.
--   signals.trend_filter_reason — dedicated column for the decision
--     log so later filter-impact analysis can query without jsonb digs.

BEGIN;

ALTER TABLE slot_profiles
  ADD COLUMN IF NOT EXISTS require_uptrend_50_200 BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS trend_filter_reason TEXT;

INSERT INTO config (key, value, updated_by) VALUES
  ('TREND_FILTER_V2_ENABLED', 'false'::jsonb, 'migration:020'),
  ('TREND_TOLERANCE_PCT_V2',  '-2.0'::jsonb,  'migration:020')
ON CONFLICT (key) DO NOTHING;

COMMIT;
