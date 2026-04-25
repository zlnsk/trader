-- 005: tighten max_hold_days (rapid mean-reversion turnover).
UPDATE slot_profiles SET max_hold_days = 3,  updated_at = now() WHERE profile = 'safe';
UPDATE slot_profiles SET max_hold_days = 5,  updated_at = now() WHERE profile = 'balanced';
UPDATE slot_profiles SET max_hold_days = 10, updated_at = now() WHERE profile = 'aggressive';
