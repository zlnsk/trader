-- 002: position live price + metadata, config keys for baseline + LLM toggle

ALTER TABLE positions ADD COLUMN IF NOT EXISTS current_price numeric;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_price_update timestamptz;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS sector text;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS company_name text;

INSERT INTO config (key, value, updated_by) VALUES
  ('LLM_VETO_ENABLED', 'false'::jsonb, 'bootstrap'),
  ('MARKET_HOURS_ONLY', 'false'::jsonb, 'bootstrap'),
  ('QUANT_SCORE_MIN', '70'::jsonb, 'bootstrap')
ON CONFLICT (key) DO NOTHING;

-- INITIAL_NET_LIQ_EUR is written by the bot on first heartbeat (not seeded).
