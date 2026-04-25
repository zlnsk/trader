-- 012_ops_and_riskreward.sql
--
-- Operational hygiene + risk/reward recalibration after the 2026-04-20 audit.
--
-- 1. Config cleanup
--    - MAX_SLOTS: unused anywhere (strategy reads slot_profiles directly).
--    - TOTAL_CAPITAL_EUR: not enforced by the bot (informational only). Drop
--      rather than mislead. Real position limit is SLOT_SIZE_EUR * n_slots.
--
-- 2. New guardrails
--    - MIN_STOP_WIDTH_PCT: floors stop distance at 0.5% of entry so tight
--      intraday ATR readings can't generate sub-noise stops.
--    - UNIVERSE: drop NOVO-B (CPH), ROG (EBS), SAN (SBF) which fail IBKR
--      paper qualification every scan. Add NVO + SNY US ADRs as healthcare
--      replacements on SMART/USD.
--
-- 3. Slot profile risk/reward
--    All scalper/intraday slots had R:R < 1 (target < stop). That requires
--    an above-75%-accurate signal to break even after fees. Widen targets
--    so a standard 55-60% win-rate produces positive expectancy.
--
--    Before                          After
--    slot 10-12  +0.5 / -0.7 (0.71)   +1.0 / -0.7 (1.43)
--    slot 13-15  +1.0 / -1.2 (0.83)   +1.5 / -1.2 (1.25)
--    slot 16-18  +1.5 / -2.0 (0.75)   +2.0 / -1.5 (1.33)
--    (swing 1-9 unchanged; per-profile deltas are intraday-only.)

BEGIN;

DELETE FROM config WHERE key IN ('MAX_SLOTS', 'TOTAL_CAPITAL_EUR');

INSERT INTO config (key, value, updated_by) VALUES
  ('MIN_STOP_WIDTH_PCT', '0.5'::jsonb, 'migration:012')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

UPDATE config
   SET value = to_jsonb(ARRAY[
         'AAPL','MSFT','GOOGL','AMZN','META','NVDA','TSLA','AVGO','JPM','V',
         'MA','UNH','HD','PG','JNJ','XOM','CVX','KO','PEP','WMT','COST','MCD',
         'DIS','NFLX','CRM','ORCL','ADBE','INTC','AMD','QCOM',
         'ASML','MC','OR','AIR','TTE','RMS','SAP','SIE','ALV','DTE','BAS',
         'AZN','SHEL','HSBA','ULVR','NESN','NOVN','NVO','SNY'
       ]),
       updated_at = now(),
       updated_by = 'migration:012'
 WHERE key = 'UNIVERSE';

UPDATE slot_profiles SET target_profit_pct = 1.0                      WHERE slot IN (10,11,12);
UPDATE slot_profiles SET target_profit_pct = 1.5                      WHERE slot IN (13,14,15);
UPDATE slot_profiles SET target_profit_pct = 2.0, stop_loss_pct = -1.5 WHERE slot IN (16,17,18);

COMMIT;
