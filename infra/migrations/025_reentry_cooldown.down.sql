BEGIN;
DELETE FROM config WHERE key = 'REENTRY_COOLDOWN_ENABLED';
ALTER TABLE slot_profiles DROP COLUMN IF EXISTS cooldown_seconds_override;
DROP TABLE IF EXISTS position_exits_cooldown;
COMMIT;
