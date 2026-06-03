-- 021 DOWN — drop IBS filter scaffolding.

BEGIN;

DELETE FROM config WHERE key = 'IBS_FILTER_ENABLED';
ALTER TABLE signals        DROP COLUMN IF EXISTS ibs_gate_passed;
ALTER TABLE signals        DROP COLUMN IF EXISTS ibs;
ALTER TABLE slot_profiles  DROP COLUMN IF EXISTS ibs_max;

COMMIT;
