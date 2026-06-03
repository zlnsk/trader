BEGIN;
DROP TRIGGER IF EXISTS trg_write_trade_outcome ON positions;
DROP FUNCTION IF EXISTS write_trade_outcome();
COMMIT;
