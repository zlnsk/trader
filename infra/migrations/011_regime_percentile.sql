-- 011: swap regime detector from z-score to percentile + absolute RV floor.
-- Motivation: z-score denominator is highly sensitive to whether the trailing
-- window includes crisis-era vol tails, producing false "risk_off" signals
-- in normal markets. Percentile is distribution-shape-invariant, and an
-- absolute-RV floor ensures we never flag risk_off when vol is simply
-- not high in any real sense.

ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS realized_vol_percentile numeric;

INSERT INTO config (key, value, updated_by) VALUES
  ('VOL_PERCENTILE_RISKOFF',  '95.0'::jsonb, 'bootstrap:011'),
  ('VOL_PERCENTILE_MOMENTUM', '10.0'::jsonb, 'bootstrap:011'),
  ('VOL_RV_RISKOFF_MIN',      '0.25'::jsonb, 'bootstrap:011'),
  ('SPY_LOOKBACK_CALENDAR_DAYS', '500'::jsonb, 'bootstrap:011')
ON CONFLICT (key) DO NOTHING;
