-- 021: PR3 — IBS (Internal Bar Strength) filter scaffolding.
--
-- IBS = (close - low) / (high - low). Canonical filter for short-RSI
-- mean-reversion (Pagonidis NAAIM 2014, Alvarez replication). Rejecting
-- signals with IBS > threshold cuts trades by ~40% while preserving the
-- bulk of winners — tightens the population from "any dip" to "dips that
-- closed near their intraday low".
--
-- Ships flag-gated (IBS_FILTER_ENABLED=false). The IBS value itself is
-- computed on every signal bar regardless of the flag — future analysis
-- (signals.ibs column) can evaluate filter impact without rerunning.
--
-- Per-slot ibs_max defaults:
--     swing slots (1-9)                 0.35
--     intraday_safe (10-12)             0.40
--     intraday_balanced (13-15)         0.40
--     intraday_aggressive (16-18)       0.50
--     crypto_scalp balanced (19-20)     0.50
--     crypto_scalp aggressive (21)      0.55
--
-- Higher threshold on aggressive tiers preserves fill rate at the cost of
-- a cleaner entry — aggressive slots already accept weaker quant scores
-- and higher RSI, so layering an IBS-tight filter too would drop trade
-- count below the sample-size floor new tuning relies on.

BEGIN;

ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS ibs_max NUMERIC;

UPDATE slot_profiles SET ibs_max = 0.35 WHERE slot BETWEEN 1 AND 9;
UPDATE slot_profiles SET ibs_max = 0.40 WHERE slot IN (10, 11, 12);
UPDATE slot_profiles SET ibs_max = 0.40 WHERE slot IN (13, 14, 15);
UPDATE slot_profiles SET ibs_max = 0.50 WHERE slot IN (16, 17, 18);
UPDATE slot_profiles SET ibs_max = 0.50 WHERE slot IN (19, 20);
UPDATE slot_profiles SET ibs_max = 0.55 WHERE slot = 21;

ALTER TABLE signals ADD COLUMN IF NOT EXISTS ibs NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS ibs_gate_passed BOOLEAN;

INSERT INTO config (key, value, updated_by) VALUES
  ('IBS_FILTER_ENABLED', 'false'::jsonb, 'migration:021')
ON CONFLICT (key) DO NOTHING;

COMMIT;
