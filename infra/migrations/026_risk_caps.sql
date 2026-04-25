-- 026: PR8 — portfolio-level gross risk cap + explicit sector-cap scope.
--
-- MAX_GROSS_RISK_PCT = 6.0 → when aggregate open-position risk exposure
-- (sum of notional × stop-distance-% ÷ equity) reaches this ceiling,
-- new entries size down by halving until aggregate drops below
-- threshold or the multiplier floors at 1/16. Acts as a soft brake on
-- correlated days where every slot wants to load at once.
--
-- MAX_POSITIONS_PER_SECTOR_SCOPE = 'portfolio' formalises what the
-- sector-count query already does today (all open positions across
-- every strategy). Flipping to 'strategy' restores the per-strategy
-- interpretation some operators might prefer. Default 'portfolio'.

BEGIN;

INSERT INTO config (key, value, updated_by) VALUES
  ('MAX_GROSS_RISK_PCT',             '6.0'::jsonb,           'migration:026'),
  ('MAX_POSITIONS_PER_SECTOR_SCOPE', '"portfolio"'::jsonb,   'migration:026')
ON CONFLICT (key) DO NOTHING;

COMMIT;
