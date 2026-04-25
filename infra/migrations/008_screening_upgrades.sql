-- 008: screening algorithm upgrades — ATR stops, trend filter, sector cap,
-- bar cache TTLs, broker + LLM concurrency controls, volume confirm.

-- Per-slot ATR stop multiplier (nullable → fall back to stop_loss_pct).
ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS stop_atr_mult numeric;
-- Per-slot toggle for the 200-bar trend filter (oversold-in-uptrend bias).
-- Default true for swing safe/balanced (where false-positive pain is worst);
-- intraday disabled (not enough history on 5-min bars).
ALTER TABLE slot_profiles ADD COLUMN IF NOT EXISTS trend_filter_enabled bool NOT NULL DEFAULT false;

-- Sensible defaults: swing safe/balanced enable trend filter + ATR 2.5x;
-- swing aggressive keeps pct stops (wider net); intraday untouched.
UPDATE slot_profiles SET trend_filter_enabled=true, stop_atr_mult=2.5
 WHERE strategy='swing' AND profile IN ('safe','balanced');
UPDATE slot_profiles SET stop_atr_mult=3.0
 WHERE strategy='swing' AND profile='aggressive';

-- Config keys driving the new knobs (all hot-reloadable — bot reads each tick).
INSERT INTO config (key, value, updated_by) VALUES
  ('MAX_POSITIONS_PER_SECTOR',   '3'::jsonb,    'bootstrap:008'),
  ('BAR_CACHE_TTL_SWING_SEC',    '240'::jsonb,  'bootstrap:008'),
  ('BAR_CACHE_TTL_INTRADAY_SEC', '45'::jsonb,   'bootstrap:008'),
  ('BROKER_CONCURRENCY',         '8'::jsonb,    'bootstrap:008'),
  ('LLM_CHECK_CONCURRENCY',      '4'::jsonb,    'bootstrap:008'),
  ('TREND_SMA_PERIOD',           '200'::jsonb,  'bootstrap:008'),
  ('TREND_TOLERANCE_PCT',        '-5.0'::jsonb, 'bootstrap:008'),
  ('VOLUME_CONFIRM_MULT',        '1.2'::jsonb,  'bootstrap:008'),
  ('ATR_PERIOD',                 '14'::jsonb,   'bootstrap:008')
ON CONFLICT (key) DO NOTHING;
