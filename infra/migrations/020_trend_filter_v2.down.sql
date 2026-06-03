-- 020 DOWN — drop trend filter v2 scaffolding.

BEGIN;

DELETE FROM config WHERE key IN ('TREND_FILTER_V2_ENABLED', 'TREND_TOLERANCE_PCT_V2');
ALTER TABLE signals        DROP COLUMN IF EXISTS trend_filter_reason;
ALTER TABLE slot_profiles  DROP COLUMN IF EXISTS require_uptrend_50_200;

COMMIT;
