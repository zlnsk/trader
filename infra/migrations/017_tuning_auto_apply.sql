-- 017: enable automatic application of weekly tuning proposals.
--
-- Prior flow: llm.propose_tuning writes a tuning_proposals row with status
-- 'pending' every Sunday 09:00 UTC. Dashboard TuningProposals card showed
-- it until the user clicked Approve, which the Next.js API then turned into
-- config upserts.
--
-- New flow: bot runs auto_apply_pending_tuning(pool) on every tick. When
-- TUNING_AUTO_APPLY=true, every pending row is applied via the same key
-- whitelist used by the dashboard route (QUANT_SCORE_MIN, TARGET_PROFIT_PCT,
-- STOP_LOSS_PCT, MIN_NET_MARGIN_EUR, SIGMA_BELOW_SMA20, RSI_BUY_THRESHOLD).
-- Dashboard approve/reject buttons still work; they just become optional.
--
-- To suspend auto-apply: UPDATE config SET value='false' WHERE key='TUNING_AUTO_APPLY'

BEGIN;

INSERT INTO config (key, value, updated_by) VALUES
  ('TUNING_AUTO_APPLY', 'true'::jsonb, 'migration:017')
ON CONFLICT (key) DO NOTHING;

COMMIT;
