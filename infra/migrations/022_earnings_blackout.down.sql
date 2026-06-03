BEGIN;
DELETE FROM config WHERE key = 'EARNINGS_BLACKOUT_ENABLED';
ALTER TABLE signals        DROP COLUMN IF EXISTS earnings_blackout_reason;
ALTER TABLE slot_profiles  DROP COLUMN IF EXISTS earnings_blackout_days;
DROP TABLE IF EXISTS earnings_calendar;
COMMIT;
