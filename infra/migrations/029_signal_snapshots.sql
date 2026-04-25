-- 029: PR12 — signal_snapshots table for filter-impact + future
-- self-optimization hypothetical-outcome analysis.
--
-- Every candidate that passes the pre-filter (regime/hours/sector)
-- gets a row regardless of what the later pipeline does with it.
-- That's the ground truth the Bayesian optimizer (future PR17) and
-- failure clustering (future PR16) need: "what did the population
-- of passing candidates actually look like, and what would have
-- happened if we'd taken the ones we skipped?"
--
-- No feature flag — instrumentation is always on. Single row per
-- candidate per scan, ~50-200 rows per intraday scan tick; acceptable
-- write load on a 17-table schema with daily partition candidates for
-- later growth.
--
-- hypothetical_outcome_pct is NULL at insert time; a nightly job
-- (jobs.backfill_hypothetical_outcomes) fills it for rows older than
-- 24h by replaying the slot's target/stop against subsequent bars.

BEGIN;

CREATE TABLE IF NOT EXISTS signal_snapshots (
  id                         BIGSERIAL PRIMARY KEY,
  symbol                     TEXT NOT NULL,
  strategy                   TEXT NOT NULL,
  slot_id                    INTEGER,
  snapshot_ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  score                      NUMERIC,
  rsi                        NUMERIC,
  sigma_below_sma20          NUMERIC,
  ibs                        NUMERIC,
  atr14                      NUMERIC,
  vwap_distance_pct          NUMERIC,
  volume_ratio               NUMERIC,
  sma200_distance_pct        NUMERIC,
  stock_regime               TEXT,
  crypto_regime              TEXT,
  vix_percentile             NUMERIC,
  hurst_exponent             NUMERIC,
  day_of_week                INTEGER,
  minute_of_day              INTEGER,
  gate_outcome               TEXT NOT NULL,
  llm_verdict                TEXT,
  llm_dive_cause             TEXT,
  trade_id                   BIGINT,
  config_version_id          INTEGER,
  hypothetical_outcome_pct   NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_signal_snapshots_ts
    ON signal_snapshots (snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_signal_snapshots_symbol_ts
    ON signal_snapshots (symbol, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_signal_snapshots_slot
    ON signal_snapshots (slot_id, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_signal_snapshots_pending_backfill
    ON signal_snapshots (snapshot_ts)
    WHERE hypothetical_outcome_pct IS NULL;

COMMIT;
