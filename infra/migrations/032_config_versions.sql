-- 032: versioned configuration store.
--
-- Model: every change to an optimizer-managed parameter creates a row in
-- config_versions. config_values stores the key/value pairs for that
-- version. The "active" version is the one with activated_at set and
-- deactivated_at NULL. Multiple active versions can coexist only when
-- scoped to different slot subsets (the canary case) — enforced by the
-- partial unique index below.
--
-- config_versions.parent_id lets us walk back through history to
-- reconstruct the causal chain: version -> parent -> parent's proposal
-- -> finding that motivated it.
--
-- Trader's existing `config` table continues to hold static keys
-- (BOT_ENABLED, UNIVERSE, etc.). Only keys listed in config_managed_keys
-- are routed through the versioned store. This keeps the blast radius
-- narrow and the migration fully backwards-compatible.

BEGIN;

CREATE TABLE IF NOT EXISTS config_versions (
  id                  SERIAL PRIMARY KEY,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by          TEXT NOT NULL,
  source              TEXT NOT NULL CHECK (source IN
                        ('bootstrap','manual','numerical','llm_failure',
                         'llm_strategic','llm_opportunity','rollback','canary')),
  parent_id           INTEGER REFERENCES config_versions(id),
  proposal_id         BIGINT,              -- FK added after tuning_proposals extended
  rationale           TEXT NOT NULL,
  scope               JSONB NOT NULL DEFAULT '{"kind":"global"}'::jsonb,
                        -- {"kind":"global"} or {"kind":"slots","slot_ids":[10,11,12]}
  activated_at        TIMESTAMPTZ,
  activated_by        TEXT,
  deactivated_at      TIMESTAMPTZ,
  deactivated_by      TEXT,
  deactivated_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_versions_active
  ON config_versions (activated_at DESC) WHERE deactivated_at IS NULL;

-- Only ONE global-scope active version allowed. Canary (slot-scoped)
-- versions coexist by using scope->>'kind' = 'slots'.
CREATE UNIQUE INDEX IF NOT EXISTS idx_config_versions_single_active_global
  ON config_versions ((scope->>'kind'))
  WHERE activated_at IS NOT NULL AND deactivated_at IS NULL AND scope->>'kind' = 'global';

CREATE TABLE IF NOT EXISTS config_values (
  version_id          INTEGER NOT NULL REFERENCES config_versions(id) ON DELETE CASCADE,
  key                 TEXT NOT NULL,
  value               JSONB NOT NULL,
  PRIMARY KEY (version_id, key)
);

-- Whitelist of keys the optimizer is permitted to version/mutate.
-- Adding a key here requires a migration (intentional friction, NOT a
-- runtime knob — the optimizer may not tune its own whitelist).
CREATE TABLE IF NOT EXISTS config_managed_keys (
  key                 TEXT PRIMARY KEY,
  dtype               TEXT NOT NULL CHECK (dtype IN ('int','float','string','bool')),
  min_value           NUMERIC,
  max_value           NUMERIC,
  description         TEXT,
  added_in            TEXT NOT NULL
);

INSERT INTO config_managed_keys (key, dtype, min_value, max_value, description, added_in) VALUES
  ('QUANT_SCORE_MIN',      'float',  0,    100, 'Minimum composite score to accept', '032'),
  ('TARGET_PROFIT_PCT',    'float',  0.1,  10,  'Per-slot target profit %',          '032'),
  ('STOP_LOSS_PCT',        'float', -10,  -0.1, 'Per-slot stop loss % (negative)',   '032'),
  ('MIN_NET_MARGIN_EUR',   'float',  0,    50,  'Minimum fee-net expected margin',   '032'),
  ('SIGMA_BELOW_SMA20',    'float',  0,    5,   'Required sigma distance from SMA20','032'),
  ('RSI_BUY_THRESHOLD',    'float',  0,    100, 'RSI threshold for buy gate',        '032')
ON CONFLICT (key) DO NOTHING;

-- Bootstrap row so every query has something to FK against from day one.
-- Captures the baseline on first-migration, mirroring whatever's in config
-- right now. Created with activated_at=NOW() so it becomes the active version.
INSERT INTO config_versions (created_by, source, rationale, activated_at, activated_by, scope)
  SELECT 'migration:032', 'bootstrap',
         'Initial snapshot of managed config keys at migration 032.',
         NOW(), 'migration:032', '{"kind":"global"}'::jsonb
  WHERE NOT EXISTS (SELECT 1 FROM config_versions WHERE source='bootstrap');

INSERT INTO config_values (version_id, key, value)
  SELECT cv.id, ck.key, c.value
    FROM config_versions cv
    CROSS JOIN config_managed_keys ck
    LEFT JOIN config c ON c.key = ck.key
   WHERE cv.source = 'bootstrap'
     AND c.value IS NOT NULL
ON CONFLICT DO NOTHING;

COMMIT;
