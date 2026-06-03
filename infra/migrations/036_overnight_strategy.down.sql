-- 036_overnight_strategy.down.sql
-- Reverses 036_overnight_strategy.sql. Safe to run only if no overnight
-- positions are open — dropping the strategy column erases that metadata.

BEGIN;

-- Refuse to run if overnight positions exist in non-terminal state.
DO $$
DECLARE
    live_count int;
BEGIN
    SELECT COUNT(*) INTO live_count
      FROM positions
     WHERE strategy = 'overnight'
       AND status IN ('opening','open','closing');
    IF live_count > 0 THEN
        RAISE EXCEPTION
            'Cannot roll back 036: % overnight position(s) still live',
            live_count;
    END IF;
END $$;

DELETE FROM config        WHERE key  = 'OVERNIGHT_ENABLED';
DELETE FROM slot_profiles WHERE slot BETWEEN 25 AND 29 AND strategy = 'overnight';

DROP INDEX IF EXISTS signals_strategy_ts_idx;
DROP INDEX IF EXISTS positions_strategy_idx;

ALTER TABLE signals   DROP COLUMN strategy;
ALTER TABLE positions DROP COLUMN strategy;

COMMIT;
