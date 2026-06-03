-- 024: PR6 — ATR-native stops + tiered time-stop scaffolding.
--
-- stop_mode is per-slot; 'pct' (current behaviour) is the default, and
-- 'atr_native' switches a slot's stop to entry × (1 − atr_mult × atr14 /
-- entry). Where a slot opts in without setting its own stop_atr_mult, the
-- per-strategy defaults from strategy._ATR_MULT_DEFAULT are used
-- (swing 1.5, intraday 1.0, crypto 1.25).
--
-- MIN_STOP_WIDTH_PCT is bumped from 0.5 to 0.75 — a 0.5% stop on a 5-min
-- bar can't survive normal tick jitter on volatile names (SAP 2026-04-20).
-- Old value preserved in-line for rollback context.
--
-- Tiered time-stop adds two behavioural tiers before max_hold_seconds
-- triggers a forced exit: 50% warn, 75% force-exit when underwater. Gated
-- by TIERED_TIME_STOP_ENABLED (default false).

BEGIN;

ALTER TABLE slot_profiles
  ADD COLUMN IF NOT EXISTS stop_mode TEXT NOT NULL DEFAULT 'pct'
    CHECK (stop_mode IN ('pct', 'atr_native'));

-- Bump MIN_STOP_WIDTH_PCT if present; insert with the new value if not.
INSERT INTO config (key, value, updated_by) VALUES
  ('MIN_STOP_WIDTH_PCT', '0.75'::jsonb, 'migration:024')
ON CONFLICT (key) DO UPDATE
    SET value = '0.75'::jsonb,
        updated_by = 'migration:024 (was 0.5)',
        updated_at = NOW();

INSERT INTO config (key, value, updated_by) VALUES
  ('TIERED_TIME_STOP_ENABLED', 'false'::jsonb, 'migration:024')
ON CONFLICT (key) DO NOTHING;

COMMIT;
