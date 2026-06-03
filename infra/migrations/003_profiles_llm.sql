-- 003: per-slot risk profiles + tables for LLM-driven features.

CREATE TABLE IF NOT EXISTS slot_profiles (
  slot               int PRIMARY KEY CHECK (slot BETWEEN 1 AND 3),
  profile            text NOT NULL CHECK (profile IN ('safe','balanced','aggressive')),
  quant_score_min    numeric NOT NULL,
  rsi_max            numeric NOT NULL,
  sigma_min          numeric NOT NULL,
  target_profit_pct  numeric NOT NULL,
  stop_loss_pct      numeric NOT NULL,
  min_net_margin_eur numeric NOT NULL,
  max_hold_days      int NOT NULL,
  sectors_allowed    jsonb,
  llm_strict         boolean NOT NULL DEFAULT false,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

INSERT INTO slot_profiles
  (slot, profile,      quant_score_min, rsi_max, sigma_min, target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_days, sectors_allowed, llm_strict)
VALUES
  (1,    'safe',        80,  25,  2.0,  2.0, -3.0, 1.0,  5,  '["Healthcare","Consumer"]'::jsonb, true),
  (2,    'balanced',    70,  30,  1.5,  3.0, -5.0, 0.5, 10,  NULL,                               false),
  (3,    'aggressive',  60,  40,  1.0,  5.0, -8.0, 0.25, 20, NULL,                               false)
ON CONFLICT (slot) DO NOTHING;

CREATE TABLE IF NOT EXISTS market_regime (
  id         bigserial PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  regime     text NOT NULL,            -- mean_reversion | momentum | risk_off | mixed
  confidence numeric,
  reasoning  text,
  raw        jsonb
);
CREATE INDEX IF NOT EXISTS market_regime_ts_idx ON market_regime (ts DESC);

CREATE TABLE IF NOT EXISTS daily_reports (
  date            date PRIMARY KEY,
  summary         text,
  wins            int,
  losses          int,
  net_pnl         numeric,
  recommendations jsonb,
  raw             jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tuning_proposals (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  status      text NOT NULL CHECK (status IN ('pending','approved','rejected','applied')) DEFAULT 'pending',
  proposal    jsonb NOT NULL,
  rationale   text,
  reviewed_at timestamptz,
  reviewed_by text
);
CREATE INDEX IF NOT EXISTS tuning_proposals_status_idx ON tuning_proposals (status, ts DESC);
