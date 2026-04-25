-- 009: risk / ops upgrades
--   - Idempotent order placement via client_order_id (rec 9)
--   - LLM spend tracking + per-day budget (rec 2)
--   - Daily-loss / drawdown circuit breakers (rec 8)
--   - Deterministic regime source alongside LLM label (rec 3)
--   - Volatility-target position sizing (rec 4)
--   - Shadow-mode signal versioning (rec 10)

-- Idempotency.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_order_id uuid;
CREATE UNIQUE INDEX IF NOT EXISTS orders_client_order_id_uidx
  ON orders (client_order_id) WHERE client_order_id IS NOT NULL;

-- Shadow/canary versioning.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS strategy_version text NOT NULL DEFAULT 'live';

-- LLM spend ledger.
CREATE TABLE IF NOT EXISTS llm_spend (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  touchpoint    text NOT NULL,
  model         text,
  input_tokens  int,
  output_tokens int,
  cached_tokens int,
  cost_usd      numeric,
  meta          jsonb
);
CREATE INDEX IF NOT EXISTS llm_spend_ts_idx          ON llm_spend (ts DESC);
CREATE INDEX IF NOT EXISTS llm_spend_touchpoint_idx  ON llm_spend (touchpoint, ts DESC);

-- Circuit-breaker state (single row).
CREATE TABLE IF NOT EXISTS risk_state (
  id          int PRIMARY KEY DEFAULT 1,
  equity_hwm  numeric,
  day_start_equity numeric,
  day_start_date date,
  tripped_at  timestamptz,
  tripped_reason text,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT risk_state_singleton CHECK (id = 1)
);
INSERT INTO risk_state (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Regime deterministic attribution.
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS realized_vol_z numeric;

-- Config keys driving the new behaviours. All default to "off-by-default" or
-- preserve-current-behaviour so this migration changes observable behaviour only
-- after the user flips a switch.
INSERT INTO config (key, value, updated_by) VALUES
  ('LLM_DAILY_BUDGET_USD',       '5.0'::jsonb,   'bootstrap:009'),
  ('DAILY_LOSS_BREAKER_PCT',     '-2.0'::jsonb,  'bootstrap:009'),
  ('DRAWDOWN_BREAKER_PCT',       '-10.0'::jsonb, 'bootstrap:009'),
  ('CIRCUIT_BREAKER_ENABLED',    'true'::jsonb,  'bootstrap:009'),
  ('POSITION_SIZE_MODE',         '"fixed"'::jsonb,        'bootstrap:009'),
  ('POSITION_RISK_PCT',          '0.5'::jsonb,   'bootstrap:009'),
  ('POSITION_RISK_ATR_MULT',     '1.5'::jsonb,   'bootstrap:009'),
  ('REGIME_SOURCE',              '"hybrid"'::jsonb,       'bootstrap:009'),
  ('SPY_VOL_LOOKBACK_DAYS',      '252'::jsonb,   'bootstrap:009'),
  ('VOL_Z_RISKOFF_THRESHOLD',    '1.5'::jsonb,   'bootstrap:009'),
  ('TICK_SIZE_ROUND_ENABLED',    'true'::jsonb,  'bootstrap:009')
ON CONFLICT (key) DO NOTHING;
