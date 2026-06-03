-- 036_overnight_strategy.sql
-- Overnight Edge strategy: MOC-buy at close, MOO-sell at next open.
-- Isolated from mean-reversion: own strategy tag, own slots, own kill switch.

BEGIN;

-- 1. Denormalised `strategy` column on positions + signals so dashboards can
--    filter per-strategy without a slot_profiles join. Default 'mean_rev'
--    keeps existing INSERT statements in strategy.py working unchanged.
ALTER TABLE positions ADD COLUMN strategy text NOT NULL DEFAULT 'mean_rev';
ALTER TABLE signals   ADD COLUMN strategy text NOT NULL DEFAULT 'mean_rev';

-- Backfill any existing position whose slot happens to be non-default strategy.
-- No-op today (slots 1-24 are all swing/intraday, no overnight positions exist
-- yet) but future migrations that reassign slots will benefit.
UPDATE positions p
   SET strategy = sp.strategy
  FROM slot_profiles sp
 WHERE p.slot = sp.slot
   AND sp.strategy <> 'mean_rev'
   AND sp.strategy <> p.strategy;

CREATE INDEX positions_strategy_idx     ON positions(strategy);
CREATE INDEX signals_strategy_ts_idx    ON signals(strategy, ts DESC);

-- 2. Overnight slot profiles (25-29). Five slots = five concurrent overnight
--    positions; spec targets 5-8 names/day, each recycles daily so 5 is the
--    per-day cap.
--
--    target_profit_pct / stop_loss_pct are SENTINEL values — overnight exits
--    via deterministic MOO SELL, not via price-based target/stop. They are
--    populated only because the columns are NOT NULL; the overnight module
--    ignores them. config.validate_slot_rr is patched to skip strategy='overnight'.
--
--    earnings_blackout_days = 3: three calendar days covers the Fri→Mon
--    weekend bridge when the symbol has Monday-BMO earnings.
--
--    scan_interval_sec = 86400: advisory only — the overnight module gates on
--    America/New_York wall-clock windows (15:45 for entry scan, 09:25 for
--    exit check), not on a simple polling interval.
INSERT INTO slot_profiles (
    slot, profile, quant_score_min, rsi_max, sigma_min,
    target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_days,
    sectors_allowed, llm_strict, strategy, max_hold_seconds, scan_interval_sec,
    earnings_blackout_days, stop_mode, trend_filter_enabled, require_uptrend_50_200
) VALUES
    (25, 'balanced', 0, 100, 0, 0.30, -0.30, 0.25, 1, NULL, false, 'overnight', 86400, 86400, 3, 'pct', false, false),
    (26, 'balanced', 0, 100, 0, 0.30, -0.30, 0.25, 1, NULL, false, 'overnight', 86400, 86400, 3, 'pct', false, false),
    (27, 'balanced', 0, 100, 0, 0.30, -0.30, 0.25, 1, NULL, false, 'overnight', 86400, 86400, 3, 'pct', false, false),
    (28, 'balanced', 0, 100, 0, 0.30, -0.30, 0.25, 1, NULL, false, 'overnight', 86400, 86400, 3, 'pct', false, false),
    (29, 'balanced', 0, 100, 0, 0.30, -0.30, 0.25, 1, NULL, false, 'overnight', 86400, 86400, 3, 'pct', false, false);

-- 3. Strategy kill switch. Starts disabled — operator flips to true after
--    smoke test. Mean-rev's BOT_ENABLED is independent; either strategy can
--    be paused without the other.
INSERT INTO config (key, value, updated_by)
VALUES ('OVERNIGHT_ENABLED', 'false'::jsonb, 'migration_036')
ON CONFLICT (key) DO NOTHING;

COMMIT;
