-- 004: 9 slots, 3 per profile (fixed assignment).

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_slot_check;
ALTER TABLE positions ADD CONSTRAINT positions_slot_check CHECK (slot BETWEEN 1 AND 9);

ALTER TABLE slot_profiles DROP CONSTRAINT IF EXISTS slot_profiles_slot_check;
ALTER TABLE slot_profiles ADD CONSTRAINT slot_profiles_slot_check CHECK (slot BETWEEN 1 AND 9);

TRUNCATE slot_profiles;

INSERT INTO slot_profiles
  (slot, profile,      quant_score_min, rsi_max, sigma_min, target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_days, sectors_allowed,                              llm_strict)
VALUES
  (1,    'safe',        80,  25,  2.0,  2.0, -3.0, 1.0,  5,  '["Healthcare","Consumer"]'::jsonb, true),
  (2,    'safe',        80,  25,  2.0,  2.0, -3.0, 1.0,  5,  '["Healthcare","Consumer"]'::jsonb, true),
  (3,    'safe',        80,  25,  2.0,  2.0, -3.0, 1.0,  5,  '["Healthcare","Consumer"]'::jsonb, true),
  (4,    'balanced',    70,  30,  1.5,  3.0, -5.0, 0.5, 10,  NULL,                               false),
  (5,    'balanced',    70,  30,  1.5,  3.0, -5.0, 0.5, 10,  NULL,                               false),
  (6,    'balanced',    70,  30,  1.5,  3.0, -5.0, 0.5, 10,  NULL,                               false),
  (7,    'aggressive',  60,  40,  1.0,  5.0, -8.0, 0.25, 20, NULL,                               false),
  (8,    'aggressive',  60,  40,  1.0,  5.0, -8.0, 0.25, 20, NULL,                               false),
  (9,    'aggressive',  60,  40,  1.0,  5.0, -8.0, 0.25, 20, NULL,                               false);

UPDATE config SET value='9'::jsonb, updated_by='admin:9slots', updated_at=now() WHERE key='MAX_SLOTS';
