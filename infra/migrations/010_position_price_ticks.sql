-- 010: time-series of per-position current_price updates, feeds dashboard charts.

CREATE TABLE IF NOT EXISTS position_price_ticks (
  position_id bigint NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
  ts          timestamptz NOT NULL DEFAULT now(),
  price       numeric NOT NULL
);
CREATE INDEX IF NOT EXISTS position_price_ticks_pid_ts_idx
  ON position_price_ticks (position_id, ts DESC);

-- Retention: keep last 14 days of ticks. Dashboard renders ~60 points.
-- Run as part of nightly cleanup (cron job owns this, not the bot hot path).
