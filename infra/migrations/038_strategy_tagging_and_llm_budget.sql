-- 038: extend strategy tagging to orders + stop_adjust_decisions + llm_*;
-- add per-strategy LLM daily budget table; backfill trade_outcomes.strategy
-- from the positions row when the trigger wrote 'unknown' (overnight had no
-- signal_snapshots row → trigger fell to COALESCE default).

BEGIN;

-- 1. strategy column on orders (denormalised from positions for fast grouping)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy TEXT;

-- Backfill: prefer orders.raw->>'strategy' (overnight writes it explicitly),
-- then fall back to the parent position.
UPDATE orders o
   SET strategy = o.raw->>'strategy'
 WHERE o.strategy IS NULL AND o.raw ? 'strategy';

UPDATE orders o
   SET strategy = p.strategy
  FROM positions p
 WHERE o.strategy IS NULL AND o.position_id = p.id;

UPDATE orders SET strategy = 'mean_rev' WHERE strategy IS NULL;

ALTER TABLE orders ALTER COLUMN strategy SET DEFAULT 'mean_rev';
ALTER TABLE orders ALTER COLUMN strategy SET NOT NULL;
CREATE INDEX IF NOT EXISTS orders_strategy_idx ON orders(strategy);

-- 2. strategy column on stop_adjust_decisions
ALTER TABLE stop_adjust_decisions ADD COLUMN IF NOT EXISTS strategy TEXT;

UPDATE stop_adjust_decisions sad
   SET strategy = p.strategy
  FROM positions p
 WHERE sad.strategy IS NULL AND sad.position_id = p.id;

UPDATE stop_adjust_decisions SET strategy = 'mean_rev' WHERE strategy IS NULL;

ALTER TABLE stop_adjust_decisions ALTER COLUMN strategy SET DEFAULT 'mean_rev';
ALTER TABLE stop_adjust_decisions ALTER COLUMN strategy SET NOT NULL;

-- 3. strategy column on LLM spend tables so per-strategy budget can be enforced
ALTER TABLE llm_spend ADD COLUMN IF NOT EXISTS strategy TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS strategy TEXT;
CREATE INDEX IF NOT EXISTS llm_spend_strategy_ts_idx ON llm_spend(strategy, ts);
CREATE INDEX IF NOT EXISTS llm_calls_strategy_ts_idx ON llm_calls(strategy, ts);

-- 4. Per-strategy daily USD budget. NULL cap = unlimited for that strategy.
-- Enforcement lives in bot/cost.py::budget_allows which checks (global) +
-- (per-strategy) and returns False if either is exhausted.
CREATE TABLE IF NOT EXISTS llm_budget_per_strategy (
    strategy       TEXT PRIMARY KEY,
    daily_usd_cap  NUMERIC,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by     TEXT
);

INSERT INTO llm_budget_per_strategy (strategy, daily_usd_cap, updated_by) VALUES
    ('mean_rev',   3.00, 'migration_038'),
    ('intraday',   1.50, 'migration_038'),
    ('crypto_scalp', 0.50, 'migration_038'),
    ('overnight',  0.50, 'migration_038')
ON CONFLICT (strategy) DO NOTHING;

-- 5. Backfill trade_outcomes.strategy for rows the trigger tagged 'unknown'
-- (pre-034 trigger, or overnight positions that never wrote a signal_snapshots
-- row). The positions table carries the authoritative tag post-migration 036.
UPDATE trade_outcomes t
   SET strategy = p.strategy
  FROM positions p
 WHERE t.strategy = 'unknown'
   AND t.position_id = p.id
   AND p.strategy IS NOT NULL
   AND p.strategy <> 'unknown';

-- 6. MOC closing-auction window per currency. Keys are read by
-- bot/hours.py::moc_window_for_currency; missing key → default (10, 20).
INSERT INTO config (key, value, updated_by) VALUES
    ('MOC_WINDOW_MIN_MINUTES_USD', '10'::jsonb, 'migration_038'),
    ('MOC_WINDOW_MAX_MINUTES_USD', '20'::jsonb, 'migration_038'),
    ('MOC_WINDOW_MIN_MINUTES_EU',  '5'::jsonb,  'migration_038'),
    ('MOC_WINDOW_MAX_MINUTES_EU',  '15'::jsonb, 'migration_038'),
    ('LLM_HTTP_TIMEOUT_SEC',       '45'::jsonb, 'migration_038')
ON CONFLICT (key) DO NOTHING;

-- 7. Kill-switch hardening: bump config_versions row on every BOT_ENABLED
-- flip (used by dashboard for optimistic concurrency). If config_versions
-- is absent, the dashboard falls back to unconditional POST.
-- No DDL here — handled in 032 already; dashboard enforces it on write.

COMMIT;
