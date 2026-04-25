-- 034: keep trade_outcomes in sync with positions.
--
-- Trigger fires when a position transitions to status='closed'. Joins the
-- most-recent executed signal_snapshots row (position's entry snapshot) for
-- feature denormalisation. If no snapshot exists, features are NULL — the
-- refresh job backfills during the next pass.
--
-- fees come from orders.fees summed per position (BUY + SELL).

BEGIN;

CREATE OR REPLACE FUNCTION write_trade_outcome() RETURNS trigger AS $$
DECLARE
  v_fees NUMERIC;
  v_hold_sec INTEGER;
  v_gross NUMERIC;
  v_net NUMERIC;
  v_net_pct NUMERIC;
  v_exit_reason TEXT;
  v_snap RECORD;
BEGIN
  IF NEW.status <> 'closed' OR OLD.status = 'closed' THEN
    RETURN NEW;
  END IF;
  IF NEW.exit_price IS NULL OR NEW.entry_price IS NULL OR NEW.qty IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT COALESCE(SUM(fees), 0)
    INTO v_fees
    FROM orders WHERE position_id = NEW.id;

  v_hold_sec := EXTRACT(EPOCH FROM (NEW.closed_at - NEW.opened_at))::INTEGER;
  v_gross := (NEW.exit_price - NEW.entry_price) * NEW.qty;
  v_net := v_gross - v_fees;
  v_net_pct := CASE WHEN NEW.entry_price > 0
                     THEN (NEW.exit_price - NEW.entry_price) / NEW.entry_price * 100.0
                     ELSE NULL END;

  -- exit_reason best-effort inference when not stored explicitly.
  IF NEW.exit_price >= COALESCE(NEW.target_price, NEW.exit_price + 1) THEN
    v_exit_reason := 'target';
  ELSIF NEW.exit_price <= COALESCE(NEW.stop_price, NEW.exit_price - 1) THEN
    v_exit_reason := 'stop';
  ELSE
    v_exit_reason := 'other';
  END IF;

  -- Most-recent executed snapshot for this symbol before opened_at.
  SELECT * INTO v_snap
    FROM signal_snapshots
   WHERE symbol = NEW.symbol
     AND snapshot_ts <= NEW.opened_at + INTERVAL '5 minutes'
     AND gate_outcome = 'executed'
   ORDER BY snapshot_ts DESC
   LIMIT 1;

  INSERT INTO trade_outcomes (
    position_id, symbol, slot_id, strategy, entry_price, exit_price, qty,
    opened_at, closed_at, hold_seconds,
    gross_pnl_eur, fees_eur, net_pnl_eur, net_pnl_pct, exit_reason,
    entry_rsi, entry_ibs, entry_sigma, entry_atr14, entry_score,
    entry_regime, entry_day_of_week, entry_minute_of_day, config_version_id
  ) VALUES (
    NEW.id, NEW.symbol, NEW.slot, COALESCE(v_snap.strategy, 'unknown'),
    NEW.entry_price, NEW.exit_price, NEW.qty,
    NEW.opened_at, NEW.closed_at, v_hold_sec,
    v_gross, v_fees, v_net, v_net_pct, v_exit_reason,
    v_snap.rsi, v_snap.ibs, v_snap.sigma_below_sma20, v_snap.atr14, v_snap.score,
    COALESCE(v_snap.stock_regime, v_snap.crypto_regime),
    v_snap.day_of_week, v_snap.minute_of_day, v_snap.config_version_id
  )
  ON CONFLICT (position_id) DO NOTHING;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_write_trade_outcome ON positions;
CREATE TRIGGER trg_write_trade_outcome
  AFTER UPDATE OF status ON positions
  FOR EACH ROW
  EXECUTE FUNCTION write_trade_outcome();

COMMIT;
