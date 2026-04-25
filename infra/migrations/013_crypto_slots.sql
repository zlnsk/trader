-- 013: crypto slots (19-21) on IBKR PAXOS, 24/7, Crypto-sector-gated.
--
-- IBKR supports BTC/ETH/LTC/BCH in USD on PAXOS only. Commission ~0.18% of
-- trade value with $1.75 floor → round-trip ≈ 0.36%. Targets + stops must
-- exceed this by a comfortable margin, so min_net_margin_eur is raised
-- (8) and targets (3%) + stops (-2.5%) give R:R ≥ 1.2 after fees.
--
-- Scan cadence matches intraday (60s). max_hold_seconds 14400 (4h) so
-- positions don't sit forever when the regime flips — 24/7 means "weekend
-- positions" are a real concern, keep turnover tight.
--
-- sectors_allowed = ["Crypto"] isolates these slots from equity candidates;
-- equity slots 1-18 keep their existing filters and won't be diluted.

BEGIN;

ALTER TABLE slot_profiles DROP CONSTRAINT IF EXISTS slot_profiles_slot_check;
ALTER TABLE slot_profiles ADD CONSTRAINT slot_profiles_slot_check CHECK (slot BETWEEN 1 AND 21);

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_slot_check;
ALTER TABLE positions ADD CONSTRAINT positions_slot_check CHECK (slot BETWEEN 1 AND 21);

INSERT INTO slot_profiles
  (slot, profile,     strategy,   quant_score_min, rsi_max, sigma_min, target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_days, max_hold_seconds, scan_interval_sec, sectors_allowed, llm_strict)
VALUES
  (19, 'balanced',   'intraday',  65, 30, 1.5, 3.0, -2.5, 8.0, 1, 14400, 60, '["Crypto"]'::jsonb, false),
  (20, 'balanced',   'intraday',  65, 30, 1.5, 3.0, -2.5, 8.0, 1, 14400, 60, '["Crypto"]'::jsonb, false),
  (21, 'aggressive', 'intraday',  55, 40, 1.0, 4.0, -3.0, 8.0, 1, 14400, 60, '["Crypto"]'::jsonb, false)
ON CONFLICT (slot) DO NOTHING;

-- Append crypto tickers to the UNIVERSE config so scans pick them up.
-- Keep existing equities intact.
UPDATE config
   SET value = (
         SELECT to_jsonb(array_agg(DISTINCT s ORDER BY s))
         FROM (
           SELECT jsonb_array_elements_text(value) AS s FROM config WHERE key='UNIVERSE'
           UNION SELECT unnest(ARRAY['BTC','ETH','LTC','BCH'])
         ) u
       ),
       updated_at = now(),
       updated_by = 'migration:013'
 WHERE key = 'UNIVERSE';

COMMIT;
