ALTER TABLE orders
  DROP COLUMN IF EXISTS bid_at_submit,
  DROP COLUMN IF EXISTS ask_at_submit,
  DROP COLUMN IF EXISTS spread_at_submit_bps,
  DROP COLUMN IF EXISTS slippage_bps,
  DROP COLUMN IF EXISTS shadow_fill_price;

DROP INDEX IF EXISTS orders_ts_idx;
