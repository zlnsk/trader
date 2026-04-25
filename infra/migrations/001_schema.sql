-- Trader DB schema — minimal v1
-- All timestamps UTC. Append-only where possible.

CREATE TABLE IF NOT EXISTS config (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text
);

CREATE TABLE IF NOT EXISTS signals (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  symbol      text NOT NULL,
  quant_score numeric,
  payload     jsonb NOT NULL,
  llm_verdict jsonb,
  decision    text NOT NULL CHECK (decision IN ('buy','skip','sell','hold')),
  reason      text
);
CREATE INDEX IF NOT EXISTS signals_ts_idx ON signals (ts DESC);
CREATE INDEX IF NOT EXISTS signals_symbol_ts_idx ON signals (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS positions (
  id           bigserial PRIMARY KEY,
  symbol       text NOT NULL,
  slot         int NOT NULL CHECK (slot BETWEEN 1 AND 3),
  status       text NOT NULL CHECK (status IN ('opening','open','closing','closed','error')),
  entry_price  numeric,
  exit_price   numeric,
  qty          numeric,
  target_price numeric,
  stop_price   numeric,
  opened_at    timestamptz NOT NULL DEFAULT now(),
  closed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS positions_status_idx ON positions (status);
CREATE UNIQUE INDEX IF NOT EXISTS positions_slot_open_idx ON positions (slot) WHERE status IN ('opening','open','closing');

CREATE TABLE IF NOT EXISTS orders (
  id          bigserial PRIMARY KEY,
  position_id bigint REFERENCES positions(id),
  side        text NOT NULL CHECK (side IN ('BUY','SELL')),
  status      text NOT NULL CHECK (status IN ('submitted','filled','partial','cancelled','rejected')),
  ib_order_id bigint,
  limit_price numeric,
  fill_price  numeric,
  fill_qty    numeric,
  fees        numeric,
  ts          timestamptz NOT NULL DEFAULT now(),
  raw         jsonb
);
CREATE INDEX IF NOT EXISTS orders_position_idx ON orders (position_id);
CREATE INDEX IF NOT EXISTS orders_ts_idx ON orders (ts DESC);

CREATE TABLE IF NOT EXISTS audit_log (
  id       bigserial PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  actor    text NOT NULL,
  action   text NOT NULL,
  details  jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC);

CREATE TABLE IF NOT EXISTS heartbeat (
  component  text PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  info       jsonb
);

-- Seed config (first-boot defaults — bot polls these each loop)
INSERT INTO config (key, value, updated_by) VALUES
  ('BOT_ENABLED',         'false'::jsonb,                                  'bootstrap'),
  ('TRADING_MODE',        '"paper"'::jsonb,                                'bootstrap'),
  ('TOTAL_CAPITAL_EUR',   '300'::jsonb,                                    'bootstrap'),
  ('SLOT_SIZE_EUR',       '100'::jsonb,                                    'bootstrap'),
  ('MAX_SLOTS',           '3'::jsonb,                                      'bootstrap'),
  ('MIN_NET_MARGIN_EUR',  '0.50'::jsonb,                                   'bootstrap'),
  ('RSI_BUY_THRESHOLD',   '30'::jsonb,                                     'bootstrap'),
  ('SIGMA_BELOW_SMA20',   '1.5'::jsonb,                                    'bootstrap'),
  ('TARGET_PROFIT_PCT',   '3.0'::jsonb,                                    'bootstrap'),
  ('STOP_LOSS_PCT',       '-5.0'::jsonb,                                   'bootstrap'),
  ('UNIVERSE',            '["AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AVGO","JPM","V","MA","UNH","HD","PG","JNJ","XOM","CVX","KO","PEP","WMT","COST","MCD","DIS","NFLX","CRM","ORCL","ADBE","INTC","AMD","QCOM"]'::jsonb, 'bootstrap'),
  ('SIGNAL_INTERVAL_SEC', '300'::jsonb,                                    'bootstrap')
ON CONFLICT (key) DO NOTHING;
