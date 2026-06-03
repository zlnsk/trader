-- Fill quality instrumentation.
-- Captures bid/ask at order submit time + computed slippage vs mid.
-- shadow_fill_price is paper-realism-corrected fill (adds half-spread penalty
-- in paper mode so we can track the optimism gap from real execution).

ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS bid_at_submit         numeric,
  ADD COLUMN IF NOT EXISTS ask_at_submit         numeric,
  ADD COLUMN IF NOT EXISTS spread_at_submit_bps  numeric,
  ADD COLUMN IF NOT EXISTS slippage_bps          numeric,
  ADD COLUMN IF NOT EXISTS shadow_fill_price     numeric;

CREATE INDEX IF NOT EXISTS orders_ts_idx ON orders(ts);
