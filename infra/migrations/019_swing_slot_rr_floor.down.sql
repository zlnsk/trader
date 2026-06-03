-- 019 DOWN — revert swing slot nudges.

BEGIN;

UPDATE slot_profiles SET target_profit_pct = 2.0  WHERE slot IN (1,2,3);
UPDATE slot_profiles SET target_profit_pct = 3.0  WHERE slot IN (4,5,6);
UPDATE slot_profiles SET target_profit_pct = 5.0  WHERE slot IN (7,8,9);

COMMIT;
