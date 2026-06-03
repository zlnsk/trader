-- 030: features_dictionary + trade_outcomes.
--
-- features_dictionary documents every feature column the optimizer may
-- consume. CI enforces: adding a column to signal_snapshots requires an
-- entry here. Prevents silent semantic drift where the optimizer "learns"
-- from a column that changed meaning without a version bump.
--
-- trade_outcomes is a physical table (not a view) holding one row per
-- closed position with entry-time snapshot features joined in. Written
-- by optimizer refresh job; never mutated after first write. Keyed by
-- position_id (FK). Immutable once closed_at set.

BEGIN;

CREATE TABLE IF NOT EXISTS features_dictionary (
  feature_name      TEXT PRIMARY KEY,
  dtype             TEXT NOT NULL,
  unit              TEXT,
  valid_range_min   NUMERIC,
  valid_range_max   NUMERIC,
  source            TEXT NOT NULL,
  introduced_in     TEXT NOT NULL,
  description       TEXT,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO features_dictionary
  (feature_name, dtype, unit, valid_range_min, valid_range_max, source, introduced_in, description)
VALUES
  ('score',                'numeric', 'composite', 0, 100,   'signals.score',          '029', 'Quant composite (RSI 0-60 + sigma 0-40)'),
  ('rsi',                  'numeric', 'level',     0, 100,   'signals.rsi',            '029', 'RSI-2 or RSI-14 per strategy'),
  ('sigma_below_sma20',    'numeric', 'sigma',     0, 10,    'signals',                '029', 'Std devs below SMA20'),
  ('ibs',                  'numeric', 'ratio',     0, 1,     'signals.ibs',            '029', 'Internal Bar Strength (close-low)/(high-low)'),
  ('atr14',                'numeric', 'price',     0, NULL,  'signals.atr',            '029', 'Average True Range 14-period'),
  ('vwap_distance_pct',    'numeric', 'pct',       -50, 50,  'signals',                '029', 'Pct distance from VWAP'),
  ('volume_ratio',         'numeric', 'ratio',     0, 100,   'signals',                '029', 'Current volume / 20-day avg'),
  ('sma200_distance_pct',  'numeric', 'pct',       -100, 500, 'signals',                '029', 'Pct distance from SMA200'),
  ('stock_regime',         'text',    NULL,        NULL, NULL, 'regime_det',            '029', 'Stock market regime label'),
  ('crypto_regime',        'text',    NULL,        NULL, NULL, 'regime_det',            '029', 'Crypto market regime label'),
  ('vix_percentile',       'numeric', 'pct',       0, 100,   'external',               '029', 'VIX rolling percentile (populated later)'),
  ('hurst_exponent',       'numeric', 'exponent',  0, 1,     'external',               '029', 'Hurst exponent (populated later)'),
  ('day_of_week',          'integer', 'dow',       0, 6,     'derived',                '029', 'Python weekday (Mon=0)'),
  ('minute_of_day',        'integer', 'minute',    0, 1439,  'derived',                '029', 'Minute of day (UTC)'),
  ('gate_outcome',         'text',    NULL,        NULL, NULL, 'strategy',              '029', 'executed/llm_veto/fees_skip/...'),
  ('llm_verdict',          'text',    NULL,        NULL, NULL, 'llm',                   '029', 'allow/abstain/veto'),
  ('llm_dive_cause',       'text',    NULL,        NULL, NULL, 'llm',                   '029', 'Short free-text rationale')
ON CONFLICT (feature_name) DO NOTHING;

-- trade_outcomes: one row per closed position, features joined from the
-- entry snapshot. Immutable once closed_at is set. config_version_id
-- stamps which config the trade was executed under (for A/B analysis).

CREATE TABLE IF NOT EXISTS trade_outcomes (
  position_id         BIGINT PRIMARY KEY REFERENCES positions(id),
  symbol              TEXT NOT NULL,
  slot_id             INTEGER NOT NULL,
  strategy            TEXT NOT NULL,
  entry_price         NUMERIC NOT NULL,
  exit_price          NUMERIC NOT NULL,
  qty                 NUMERIC NOT NULL,
  opened_at           TIMESTAMPTZ NOT NULL,
  closed_at           TIMESTAMPTZ NOT NULL,
  hold_seconds        INTEGER NOT NULL,
  gross_pnl_eur       NUMERIC NOT NULL,
  fees_eur            NUMERIC NOT NULL,
  net_pnl_eur         NUMERIC NOT NULL,
  net_pnl_pct         NUMERIC NOT NULL,
  exit_reason         TEXT NOT NULL,
  -- Features copied from the entry-time snapshot. Denormalised on purpose:
  -- queries against trade_outcomes shouldn't need to re-join signal_snapshots.
  entry_rsi           NUMERIC,
  entry_ibs           NUMERIC,
  entry_sigma         NUMERIC,
  entry_atr14         NUMERIC,
  entry_score         NUMERIC,
  entry_regime        TEXT,
  entry_day_of_week   INTEGER,
  entry_minute_of_day INTEGER,
  config_version_id   INTEGER,
  defn_version        INTEGER NOT NULL DEFAULT 1,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_closed_at ON trade_outcomes (closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_slot       ON trade_outcomes (slot_id, closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_regime     ON trade_outcomes (entry_regime, closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_cfgver     ON trade_outcomes (config_version_id);

COMMIT;
