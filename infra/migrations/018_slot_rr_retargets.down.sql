-- 018 DOWN — revert slot retargets to prior values.
-- Only intended for emergency rollback; leaves the R:R-broken state behind.

BEGIN;

UPDATE slot_profiles
   SET target_profit_pct = 0.5,
       updated_at        = now()
 WHERE slot IN (10, 11, 12);

UPDATE slot_profiles
   SET target_profit_pct = 1.5,
       stop_loss_pct     = -1.0,
       updated_at        = now()
 WHERE slot IN (19, 20);

UPDATE slot_profiles
   SET target_profit_pct = 2.5,
       updated_at        = now()
 WHERE slot = 21;

COMMIT;
