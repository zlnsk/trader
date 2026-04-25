-- 027: PR9 — daily / rolling-5d / weekly drawdown auto-kill.
--
-- Second-layer safety net that halts NEW buys when realised+unrealised
-- losses breach any of three thresholds. Does NOT close existing
-- positions — monitor_open_positions still runs their stops so the
-- auto-kill can't amplify a loss by forcing a bad exit.
--
-- Implemented by risk.check_auto_kill (called every tick). On trip:
-- sets BOT_ENABLED=false, records the reason in config.AUTO_KILLED_REASON.
-- Does not self-recover — operator clears both keys manually.

BEGIN;

INSERT INTO config (key, value, updated_by) VALUES
  ('AUTO_KILL_ENABLED',             'true'::jsonb, 'migration:027'),
  ('DAILY_LOSS_LIMIT_PCT',          '2.0'::jsonb,  'migration:027'),
  ('ROLLING_5D_DRAWDOWN_LIMIT_PCT', '5.0'::jsonb,  'migration:027'),
  ('WEEKLY_LOSS_LIMIT_PCT',         '4.0'::jsonb,  'migration:027')
ON CONFLICT (key) DO NOTHING;

COMMIT;
