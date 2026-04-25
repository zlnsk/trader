-- 037: Apply recommendations 1-4 from trader-recommendations.md
--
-- Rec 1: Quant signal improvements (RSI divergence, multi-timeframe, RV hard gate)
-- Rec 2: Risk management (ATR-native default, bracket orders, partial TP)
-- Rec 3: LLM sentiment scoring + earnings intelligence
-- Rec 4: Matrix notifications, shadow canaries, 90d backtest minimum

BEGIN;

-- New feature flags (all default OFF for safe rollout; flip via dashboard)
INSERT INTO config (key, value, updated_by) VALUES
  ('VOLUME_HARD_GATE_ENABLED',   'false'::jsonb, 'migration:037'),
  ('MULTI_TF_CONFIRM_ENABLED',   'false'::jsonb, 'migration:037'),
  ('RSI_DIVERGENCE_ENABLED',     'true'::jsonb,  'migration:037'),
  ('BRACKET_ORDER_ENABLED',      'false'::jsonb, 'migration:037'),
  ('PARTIAL_TP_ENABLED',         'false'::jsonb, 'migration:037'),
  ('LLM_SENTIMENT_SIZING_ENABLED','false'::jsonb, 'migration:037'),
  ('MIN_STOP_WIDTH_PCT',         '0.75'::jsonb,  'migration:037'),
  ('MANUAL_APPROVAL_MODE',       'true'::jsonb,  'migration:037')
ON CONFLICT (key) DO NOTHING;

-- Rec 4: bump default volume multiplier to 1.5 (was 1.2)
UPDATE config SET value = '1.5'::jsonb, updated_by = 'migration:037'
  WHERE key = 'VOLUME_CONFIRM_MULT' AND (value::text = '1.2' OR value::text = '"1.2"');

-- Rec 2: default stop_mode for existing slots without explicit mode → atr_native
UPDATE slot_profiles
  SET stop_mode = 'atr_native'
  WHERE stop_mode IS NULL OR stop_mode = 'pct';

-- Rec 4: shadow_trades table for database-only canary / paper-shadow mode.
-- The optimizer can "trade" a new configuration here without risking capital.
-- config_version_id is kept as plain integer (no FK) because the trader DB
-- user may not have REFERENCES permission on config_versions.
CREATE TABLE IF NOT EXISTS shadow_trades (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  symbol        TEXT NOT NULL,
  slot          INT NOT NULL,
  strategy      TEXT NOT NULL,
  config_version_id INTEGER,
  entry_price   NUMERIC NOT NULL,
  qty           NUMERIC NOT NULL,
  target_price  NUMERIC NOT NULL,
  stop_price    NUMERIC NOT NULL,
  exit_price    NUMERIC,
  exit_ts       TIMESTAMPTZ,
  exit_reason   TEXT,
  pnl_eur       NUMERIC,
  raw           JSONB
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_symbol ON shadow_trades (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_version ON shadow_trades (config_version_id, ts DESC);

COMMIT;
