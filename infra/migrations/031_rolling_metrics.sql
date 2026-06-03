-- 031: rolling-metric tables used by the validator, anomaly detector and
-- hypothesis generators.
--
-- Physical tables (not materialised views). Refreshed incrementally by
-- the optimizer's metrics.refresh job. Staleness is gated via
-- metrics_refresh_state: consumers block until as_of_ts >= required_ts.
--
-- Every metric row carries:
--   - n_samples  so MIN_N=50 gate (in code, not config) can filter low-n rows
--   - defn_version so metric-formula bumps don't silently compare across eras
--   - config_version_id so A/B comparisons segment cleanly
--
-- Window-days convention: 7, 30, 90. Change requires defn_version bump.

BEGIN;

-- Per-slot rolling metrics.
CREATE TABLE IF NOT EXISTS metrics_slot_rolling (
  slot_id             INTEGER NOT NULL,
  window_days         INTEGER NOT NULL,
  as_of_date          DATE NOT NULL,
  config_version_id   INTEGER NOT NULL DEFAULT 0,  -- 0 = "all/unspecified"; >0 = specific config_versions.id
  defn_version        INTEGER NOT NULL DEFAULT 1,
  n_samples           INTEGER NOT NULL,
  win_rate            NUMERIC,
  profit_factor       NUMERIC,
  expectancy_bps      NUMERIC,
  avg_hold_sec        NUMERIC,
  sharpe_like         NUMERIC,
  max_dd_pct          NUMERIC,
  fees_eur            NUMERIC,
  gross_pnl_eur       NUMERIC,
  net_pnl_eur         NUMERIC,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (slot_id, window_days, as_of_date, config_version_id, defn_version)
);
CREATE INDEX IF NOT EXISTS idx_metrics_slot_rolling_lookup
  ON metrics_slot_rolling (slot_id, window_days, as_of_date DESC);

CREATE TABLE IF NOT EXISTS metrics_regime_rolling (
  slot_id             INTEGER NOT NULL,
  regime              TEXT NOT NULL,
  window_days         INTEGER NOT NULL,
  as_of_date          DATE NOT NULL,
  config_version_id   INTEGER NOT NULL DEFAULT 0,
  defn_version        INTEGER NOT NULL DEFAULT 1,
  n_samples           INTEGER NOT NULL,
  win_rate            NUMERIC,
  profit_factor       NUMERIC,
  expectancy_bps      NUMERIC,
  net_pnl_eur         NUMERIC,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (slot_id, regime, window_days, as_of_date, config_version_id, defn_version)
);

CREATE TABLE IF NOT EXISTS metrics_tod_rolling (
  slot_id             INTEGER NOT NULL,
  hour_utc            INTEGER NOT NULL,
  window_days         INTEGER NOT NULL,
  as_of_date          DATE NOT NULL,
  defn_version        INTEGER NOT NULL DEFAULT 1,
  n_samples           INTEGER NOT NULL,
  win_rate            NUMERIC,
  expectancy_bps      NUMERIC,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (slot_id, hour_utc, window_days, as_of_date, defn_version)
);

CREATE TABLE IF NOT EXISTS metrics_symbol_rolling (
  symbol              TEXT NOT NULL,
  window_days         INTEGER NOT NULL,
  as_of_date          DATE NOT NULL,
  defn_version        INTEGER NOT NULL DEFAULT 1,
  n_samples           INTEGER NOT NULL,
  win_rate            NUMERIC,
  expectancy_bps      NUMERIC,
  net_pnl_eur         NUMERIC,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (symbol, window_days, as_of_date, defn_version)
);

-- Per-LLM-touchpoint×verdict rolling metrics.
-- accuracy compares llm_verdict to hypothetical_outcome_pct:
--   - "allow" is accurate when outcome_pct >= 0
--   - "veto"  is accurate when outcome_pct <  0
--   - "abstain" counts neither (recorded but not scored)
CREATE TABLE IF NOT EXISTS metrics_llm_rolling (
  touchpoint          TEXT NOT NULL,
  verdict             TEXT NOT NULL,
  window_days         INTEGER NOT NULL,
  as_of_date          DATE NOT NULL,
  defn_version        INTEGER NOT NULL DEFAULT 1,
  n_samples           INTEGER NOT NULL,
  accuracy            NUMERIC,
  brier_score         NUMERIC,
  call_count          INTEGER NOT NULL,
  cost_eur            NUMERIC,
  written_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (touchpoint, verdict, window_days, as_of_date, defn_version)
);

-- Per-table refresh bookkeeping. Consumers gate: WHERE as_of_ts >= required.
CREATE TABLE IF NOT EXISTS metrics_refresh_state (
  table_name          TEXT PRIMARY KEY,
  as_of_ts            TIMESTAMPTZ NOT NULL,
  duration_ms         INTEGER,
  rows_written        INTEGER,
  last_error          TEXT,
  last_error_ts       TIMESTAMPTZ
);

COMMIT;
