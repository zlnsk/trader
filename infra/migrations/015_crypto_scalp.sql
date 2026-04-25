-- 015: aggressive crypto scalping — short-term fluctuation capture.
--
-- Prior (migration 013) had crypto slots on the generic intraday strategy
-- with 5-min bars, 4h hold, target 3-4% / stop -2.5 to -3%. Too slow for
-- the fast swings crypto produces intraday — most 2-3% moves completed
-- and reversed before a 5-min RSI-2 could flip.
--
-- New: dedicated "crypto_scalp" strategy (wired into run_once with a 30s
-- cadence). 1-min AGGTRADES bars, RSI-2, shorter holds (1h max), tighter
-- targets/stops that still clear fees:
--   fee round-trip ≈ 0.36% of notional with $1.75/side floor. At a $1000
--   slot, fees ≈ $3.60. A 1.2% target = $12 gross → $8.4 net (min_margin
--   $4 clears easily). Below 1.0% gross the fee floor makes the trade
--   uneconomic even with perfect fills.
--
-- Thresholds loosened to accept more triggers per scan:
--   slot 19 (balanced):    score≥50, rsi≤40, σ≥0.5, target 1.5% / stop -1.0%  (R:R 1.5)
--   slot 20 (aggressive):  score≥45, rsi≤50, σ≥0.3, target 2.0% / stop -1.2%  (R:R 1.67)
--   slot 21 (high-beta):   score≥40, rsi≤55, σ≥0.0, target 2.5% / stop -1.5%  (R:R 1.67)
-- All R:R > 1, so even a 45-50% win rate is positive-expectancy after fees.
--
-- MIN_STOP_WIDTH_PCT=0.5 still floors width; crypto 1-min ATR can produce
-- sub-0.5% stops that get chopped out on noise.

BEGIN;

UPDATE slot_profiles
   SET strategy           = 'crypto_scalp',
       quant_score_min    = 50,
       rsi_max            = 40,
       sigma_min          = 0.5,
       target_profit_pct  = 1.5,
       stop_loss_pct      = -1.0,
       min_net_margin_eur = 4.0,
       max_hold_seconds   = 3600,
       scan_interval_sec  = 30,
       updated_at         = now()
 WHERE slot = 19;

UPDATE slot_profiles
   SET strategy           = 'crypto_scalp',
       profile            = 'aggressive',
       quant_score_min    = 45,
       rsi_max            = 50,
       sigma_min          = 0.3,
       target_profit_pct  = 2.0,
       stop_loss_pct      = -1.2,
       min_net_margin_eur = 4.0,
       max_hold_seconds   = 3600,
       scan_interval_sec  = 30,
       updated_at         = now()
 WHERE slot = 20;

UPDATE slot_profiles
   SET strategy           = 'crypto_scalp',
       profile            = 'aggressive',
       quant_score_min    = 40,
       rsi_max            = 55,
       sigma_min          = 0.0,
       target_profit_pct  = 2.5,
       stop_loss_pct      = -1.5,
       min_net_margin_eur = 4.0,
       max_hold_seconds   = 3600,
       scan_interval_sec  = 30,
       updated_at         = now()
 WHERE slot = 21;

COMMIT;
