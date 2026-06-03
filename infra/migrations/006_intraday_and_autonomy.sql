-- 006: intraday slots, manual approval mode, briefings, news checks,
-- multi-region market hours, news watcher config.

ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS strategy           text NOT NULL DEFAULT 'swing';
ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS max_hold_seconds   int;
ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS scan_interval_sec  int NOT NULL DEFAULT 300;

UPDATE slot_profiles SET max_hold_seconds = max_hold_days * 86400 WHERE strategy='swing' AND max_hold_seconds IS NULL;

ALTER TABLE slot_profiles DROP CONSTRAINT IF EXISTS slot_profiles_slot_check;
ALTER TABLE slot_profiles ADD CONSTRAINT slot_profiles_slot_check CHECK (slot BETWEEN 1 AND 18);

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_slot_check;
ALTER TABLE positions ADD CONSTRAINT positions_slot_check CHECK (slot BETWEEN 1 AND 18);

-- Intraday profiles (slots 10-18): 3 each of safe/balanced/aggressive.
-- Parameters: tight targets/stops (0.5-1.5% / -0.7 to -2.0%), hold 1h-4h, scan every 60s.
INSERT INTO slot_profiles
  (slot, profile,     strategy,   quant_score_min, rsi_max, sigma_min, target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_days, max_hold_seconds, scan_interval_sec, sectors_allowed, llm_strict)
VALUES
  (10, 'safe',       'intraday',  80, 20, 2.0, 0.5, -0.7, 0.25, 1,  3600,  60, '["Healthcare","Consumer"]'::jsonb, true),
  (11, 'safe',       'intraday',  80, 20, 2.0, 0.5, -0.7, 0.25, 1,  3600,  60, '["Healthcare","Consumer"]'::jsonb, true),
  (12, 'safe',       'intraday',  80, 20, 2.0, 0.5, -0.7, 0.25, 1,  3600,  60, '["Healthcare","Consumer"]'::jsonb, true),
  (13, 'balanced',   'intraday',  70, 25, 1.5, 1.0, -1.2, 0.25, 1,  7200,  60, NULL, false),
  (14, 'balanced',   'intraday',  70, 25, 1.5, 1.0, -1.2, 0.25, 1,  7200,  60, NULL, false),
  (15, 'balanced',   'intraday',  70, 25, 1.5, 1.0, -1.2, 0.25, 1,  7200,  60, NULL, false),
  (16, 'aggressive', 'intraday',  60, 35, 1.0, 1.5, -2.0, 0.25, 1, 14400,  60, NULL, false),
  (17, 'aggressive', 'intraday',  60, 35, 1.0, 1.5, -2.0, 0.25, 1, 14400,  60, NULL, false),
  (18, 'aggressive', 'intraday',  60, 35, 1.0, 1.5, -2.0, 0.25, 1, 14400,  60, NULL, false)
ON CONFLICT (slot) DO NOTHING;

-- New config keys
INSERT INTO config (key, value, updated_by) VALUES
  ('MANUAL_APPROVAL_MODE',      'false'::jsonb,   'bootstrap'),
  ('NEWS_WATCHER_ENABLED',      'true'::jsonb,    'bootstrap'),
  ('NEWS_WATCHER_INTERVAL_SEC', '900'::jsonb,     'bootstrap'),
  ('APPROVAL_EXPIRY_SEC',       '1800'::jsonb,    'bootstrap'),
  ('ANNUAL_TARGET_PCT',         '12.0'::jsonb,    'bootstrap')
ON CONFLICT (key) DO NOTHING;

-- Pre-open / end-of-day briefings (Claude-authored)
CREATE TABLE IF NOT EXISTS briefings (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  kind        text NOT NULL CHECK (kind IN ('pre_open_eu','pre_open_us','end_of_day')),
  region      text,
  summary     text,
  candidates  jsonb,
  raw         jsonb
);
CREATE INDEX IF NOT EXISTS briefings_ts_idx ON briefings (ts DESC);

-- Manual-approval queue (when MANUAL_APPROVAL_MODE = true)
CREATE TABLE IF NOT EXISTS pending_approvals (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  symbol       text NOT NULL,
  slot         int  NOT NULL,
  strategy     text NOT NULL,
  profile      text NOT NULL,
  quant_score  numeric,
  payload      jsonb,
  llm_verdict  jsonb,
  price        numeric,
  qty          numeric,
  target_price numeric,
  stop_price   numeric,
  currency     text,
  status       text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','approved','skipped','expired','executed')),
  reviewed_at  timestamptz,
  reviewed_by  text,
  executed_order_id bigint
);
CREATE INDEX IF NOT EXISTS pending_approvals_status_idx ON pending_approvals (status, ts DESC);

-- News-watcher audit log: every proactive Claude check on held positions
CREATE TABLE IF NOT EXISTS news_checks (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  position_id  bigint REFERENCES positions(id),
  symbol       text NOT NULL,
  verdict      jsonb NOT NULL,
  triggered    text
);
CREATE INDEX IF NOT EXISTS news_checks_position_idx ON news_checks (position_id, ts DESC);
