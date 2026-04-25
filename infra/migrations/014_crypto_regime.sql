-- 014: separate regime tracking per asset_class.
--
-- Prior to this migration, market_regime held a single equity-driven regime
-- (SPY 20d RV percentile). With crypto slots (013) running 24/7 against a
-- separate reference (BTC), a single regime would force crypto scans to
-- halt whenever equities go risk_off (and vice-versa) even though the
-- underlying vol regimes are uncorrelated.
--
-- New column `asset_class` splits the table. Existing rows backfill to
-- 'stock'. Query sites must either filter by asset_class or accept that
-- ORDER BY ts DESC LIMIT 1 now mixes classes.

BEGIN;

ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS asset_class text NOT NULL DEFAULT 'stock';

CREATE INDEX IF NOT EXISTS market_regime_asset_ts_idx
    ON market_regime (asset_class, ts DESC);

COMMIT;
