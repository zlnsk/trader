-- 022: PR4 — earnings blackout scaffolding.
--
-- A swing slot that holds 1-5 days into an earnings release converts a -1.5%
-- stop into a potential -10% gap. That's regime change, not mean reversion;
-- the whole statistical thesis of the strategy breaks when the underlying
-- price process does. This migration adds the data model to fence earnings
-- days off per-slot.
--
-- earnings_calendar is populated nightly by jobs.maybe_sync_earnings
-- (03:00 UTC). Missing rows are treated FAIL-SAFE by the scan gate: unknown
-- earnings date → reject the trade and log loudly. Silent-allow would let
-- a stale calendar ghost through an earnings window unnoticed.
--
-- Per-slot earnings_blackout_days defaults (0 = disabled):
--     swing (1-9)             3
--     intraday (10-18)        1
--     crypto_scalp (19-21)    0  (no earnings concept)

BEGIN;

CREATE TABLE IF NOT EXISTS earnings_calendar (
  symbol          TEXT NOT NULL,
  earnings_date   DATE NOT NULL,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source          TEXT,
  PRIMARY KEY (symbol, earnings_date)
);
CREATE INDEX IF NOT EXISTS earnings_calendar_symbol_date_idx
    ON earnings_calendar (symbol, earnings_date);

ALTER TABLE slot_profiles
  ADD COLUMN IF NOT EXISTS earnings_blackout_days INTEGER NOT NULL DEFAULT 0;

UPDATE slot_profiles SET earnings_blackout_days = 3 WHERE slot BETWEEN 1 AND 9;
UPDATE slot_profiles SET earnings_blackout_days = 1 WHERE slot BETWEEN 10 AND 18;
UPDATE slot_profiles SET earnings_blackout_days = 0 WHERE slot BETWEEN 19 AND 21;

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS earnings_blackout_reason TEXT;

INSERT INTO config (key, value, updated_by) VALUES
  ('EARNINGS_BLACKOUT_ENABLED', 'false'::jsonb, 'migration:022')
ON CONFLICT (key) DO NOTHING;

COMMIT;
