-- 025: PR7 — per-strategy re-entry cooldown after losing exits.
--
-- Problem: a losing stop-out on symbol X doesn't prevent the next scan
-- from re-entering X on the same strategy if the signal still fires.
-- During a downtrend this creates a staircase of losses on the same name.
--
-- Data model:
--   position_exits_cooldown    rolling table, one row per losing exit.
--                              cooldown_until_ts computed at insert time
--                              (exit_ts + cooldown_seconds) so the scan
--                              gate is a cheap WHERE clause.
--   slot_profiles.cooldown_seconds_override   per-slot override; NULL
--                              falls back to _COOLDOWN_SECONDS_BY_STRATEGY.
--
-- Defaults (in code, not config — these are per-strategy, not per-slot):
--   swing        86400  (24h)
--   intraday     7200   (2h)
--   crypto_scalp 1800   (30min)
--
-- Flag-gated by REENTRY_COOLDOWN_ENABLED (default false). When the flag
-- flips on, existing cooldown rows will start blocking scans immediately
-- — historical losing exits don't retro-populate the table, which is
-- fine: the first cohort of losing exits after flipping accumulates
-- naturally.

BEGIN;

CREATE TABLE IF NOT EXISTS position_exits_cooldown (
  id                  BIGSERIAL PRIMARY KEY,
  symbol              TEXT NOT NULL,
  strategy            TEXT NOT NULL,
  exit_ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cooldown_until_ts   TIMESTAMPTZ NOT NULL,
  exit_pnl_eur        NUMERIC,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS position_exits_cooldown_active_idx
    ON position_exits_cooldown (symbol, strategy, cooldown_until_ts DESC);
CREATE INDEX IF NOT EXISTS position_exits_cooldown_until_idx
    ON position_exits_cooldown (cooldown_until_ts);

ALTER TABLE slot_profiles
  ADD COLUMN IF NOT EXISTS cooldown_seconds_override INTEGER;

INSERT INTO config (key, value, updated_by) VALUES
  ('REENTRY_COOLDOWN_ENABLED', 'false'::jsonb, 'migration:025')
ON CONFLICT (key) DO NOTHING;

COMMIT;
